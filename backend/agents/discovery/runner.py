from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from collections.abc import Callable
from time import perf_counter
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from backend.agents.librarian.text_utils import keyword_set
from backend.agents.discovery.registry import SourceRegistry
from backend.agents.discovery.types import (
    AdapterStatus,
    AdapterUnavailable,
    Candidate,
    DiscoveryResult,
    SourceAdapter,
    SourceAdapterContext,
    TopicProfile,
)
from backend.app.services import brief_settings

_SECRET_QUERY_RE = re.compile(r"(?i)([?&](?:key|api_key|apikey|token|access_token|client_secret)=)([^&\s'\"<>]+)")


def _redact_status_message(value: object, *, limit: int = 240) -> str:
    text = str(value or "")
    text = _SECRET_QUERY_RE.sub(r"\1REDACTED", text)
    return text[:limit]


_GOOD_FOR_SIGNAL_MAP: dict[str, tuple[str, ...]] = {
    "breaking_news": (
        "breaking",
        "latest",
        "today",
        "current",
        "just in",
        "new",
        "headline",
        "fresh",
        "rapid",
        "now",
    ),
    "broad_discovery": (
        "broad",
        "wide",
        "sweep",
        "overview",
        "across",
        "all",
        "general",
        "surface",
        "summary",
    ),
    "fresh_sources": (
        "fresh",
        "new",
        "recent",
        "latest",
        "this week",
        "this month",
    ),
    "community_signal": (
        "community",
        "forum",
        "discussion",
        "social",
        "group",
        "pulse",
    ),
    "sentiment": (
        "sentiment",
        "tone",
        "mood",
        "reaction",
        "concern",
        "hype",
    ),
    "emerging_workflows": (
        "workflow",
        "adopt",
        "adoption",
        "how teams",
        "pattern",
        "playbook",
        "implementation",
        "deploy",
        "launch",
    ),
    "deep_context": (
        "deep",
        "detailed",
        "in-depth",
        "analysis",
        "technical",
        "architecture",
        "engineering",
        "expert",
        "framework",
    ),
    "interviews": (
        "interview",
        "guest",
        "conversation",
        "hosted",
        "speaker",
        "podcast",
        "episode",
    ),
    "expert_discussion": (
        "expert",
        "deep",
        "technical",
        "researcher",
        "practitioner",
        "founder",
        "cto",
        "engineer",
    ),
    "newsletters": (
        "newsletter",
        "newsletters",
        "daily",
        "digest",
        "roundup",
    ),
    "primary_sources": (
        "source",
        "primary",
        "original",
        "first-hand",
        "announcement",
        "release",
        "changelog",
    ),
    "curated_links": (
        "curated",
        "link",
        "resources",
        "collection",
        "list",
    ),
    "community_discussion": (
        "community",
        "forum",
        "discussion",
        "social",
        "reddit",
        "thread",
        "comments",
    ),
    "emerging_topics": (
        "emerging",
        "trend",
        "latest",
        "new",
        "hype",
        "upcoming",
        "rising",
        "future",
    ),
    "expert_opinion": (
        "expert",
        "opinion",
        "commentary",
        "analysis",
        "review",
        "critique",
        "thought",
    ),
}


class DiscoveryRunner:
    def __init__(self, registry: SourceRegistry):
        self.registry = registry

    async def run(
        self,
        profile: TopicProfile,
        *,
        source_selection: dict[str, bool] | None = None,
        context: SourceAdapterContext,
        on_adapter_status: Callable[[AdapterStatus], None] | None = None,
        low_yield: bool = False,
    ) -> DiscoveryResult:
        selection = source_selection or profile.source_selection
        profile = _sanitize_bounded_recency_queries(profile, context.lookback_hours)
        # Proactive query expansion (item 1): widen the search strategy for every
        # selected source before any adapter runs. Folding the affiliated angles
        # into per-source queries (not the global topic text) means adapters cast
        # a wider net without loosening the downstream topic-relevance gate.
        profile = await _expand_profile_queries(profile, selection, context, low_yield=low_yield)
        adapters = self.registry.selected(selection)
        all_names = set(self.registry.names())
        callback = on_adapter_status
        results = await asyncio.gather(
            *[
                self._run_adapter(adapter, profile, context, callback=callback)
                for adapter in adapters
            ],
        )
        statuses = [status for _candidates, status in results]
        all_raw_candidates = [candidate for adapter_candidates, _status in results for candidate in adapter_candidates]
        candidates, exclusions = _apply_exclusions(profile, all_raw_candidates)
        candidates, relevance_exclusions = _apply_topic_relevance(profile, candidates, low_yield=low_yield)

        from backend.agents.discovery.query_refiner import screen_candidates
        screening_exclusions = []
        candidates = await screen_candidates(profile, candidates, exclusions=screening_exclusions, low_yield=low_yield)
        pre_limit_candidates = list(candidates)

        # Boost scores of candidates from requested/promoted sources by 0.4 (capped at 1.0)
        boosted_candidates = []
        for candidate in candidates:
            if _is_candidate_from_requested_source(candidate, profile):
                new_score = min(1.0, candidate.score + 0.4)
                boosted_candidates.append(replace(candidate, score=new_score))
            else:
                boosted_candidates.append(candidate)
        candidates = boosted_candidates

        # Dedicated source lanes (item 2): EVERY selected source is judged within
        # its own lane and reserved its own slots, so no source is crowded out of
        # the brief by another source's volume or higher scores. Each lane is
        # bounded only by its own per-source limit, never by a shared global pool.
        sort_key = lambda c: (c.score, c.payload.published_at or c.payload.fetched_at or "")
        lane_sources = sorted({candidate.adapter for candidate in candidates})
        lane_candidates: list[Candidate] = []
        for source in lane_sources:
            if selection.get(source) is False:
                continue
            source_limit = _lane_limit(profile, source, default=250, system_max=250)
            raw_candidates = [candidate for candidate in candidates if candidate.adapter == source]

            if source == "markets":
                # Explicitly requested tickers are never trimmed by the lane limit.
                explicit_candidates = [c for c in raw_candidates if (c.payload.metadata or {}).get("explicit_ticker") is True]
                regular_candidates = [c for c in raw_candidates if (c.payload.metadata or {}).get("explicit_ticker") is not True]
                lane_candidates.extend(
                    _dedupe_candidates(sorted(explicit_candidates, key=sort_key, reverse=True), limit=len(explicit_candidates))
                )
                lane_candidates.extend(
                    _dedupe_candidates(sorted(regular_candidates, key=sort_key, reverse=True), limit=source_limit)
                )
            else:
                lane_candidates.extend(
                    _dedupe_candidates(sorted(raw_candidates, key=sort_key, reverse=True), limit=source_limit)
                )

        # Combine every reserved lane into one ranked stream. Ordering by score is
        # purely cosmetic for downstream stages; the slots themselves are reserved.
        candidates = sorted(lane_candidates, key=sort_key, reverse=True)

        # Capture deduplication / lane limits drops
        final_ids = {c.payload.id for c in candidates}
        limit_exclusions = []
        for c in pre_limit_candidates:
            if c.payload.id not in final_ids:
                limit_exclusions.append({
                    "adapter": c.adapter,
                    "candidate_id": str(c.payload.id),
                    "original_url": c.payload.original_url,
                    "source_type": c.payload.source_type,
                    "source_name": c.payload.source_name,
                    "title": _candidate_title(c),
                    "subject": c.payload.metadata.get("subject") or c.payload.metadata.get("parent_subject"),
                    "link_text": c.payload.metadata.get("link_text"),
                    "metadata": dict(c.payload.metadata or {}),
                    "excluded_by": ["discovery_limits"],
                    "reason": "Duplicate content or exceeded discovery lane/capacity limits.",
                })

        excluded_statuses = [
            AdapterStatus(name=name, status="skipped", message="Source was turned off for this exploration.")
            for name, enabled in selection.items()
            if enabled is False and name in all_names
        ]
        return DiscoveryResult(
            profile=profile,
            candidates=tuple(candidates),
            statuses=tuple([*statuses, *excluded_statuses]),
            exclusions=tuple([*exclusions, *relevance_exclusions, *screening_exclusions, *limit_exclusions]),
        )

    async def _run_adapter(
        self,
        adapter: SourceAdapter,
        profile: TopicProfile,
        context: SourceAdapterContext,
        callback: Callable[[AdapterStatus], None] | None = None,
    ) -> tuple[list[Candidate], AdapterStatus]:
        started_at = perf_counter()
        timeout = adapter.cost_profile.timeout_seconds
        candidates: list[Candidate] = []
        try:
            candidates = await asyncio.wait_for(adapter.query(profile, context), timeout=timeout)
            candidates = _weight_candidates_by_adapter_fit(profile=profile, adapter=adapter, candidates=candidates)
        except TimeoutError:
            status = AdapterStatus(
                name=adapter.name,
                status="timed_out",
                elapsed_ms=_elapsed_ms(started_at),
                timeout_seconds=timeout,
                message="Source timed out and was skipped.",
            )
        except AdapterUnavailable as exc:
            status = AdapterStatus(
                name=adapter.name,
                status="skipped",
                elapsed_ms=_elapsed_ms(started_at),
                timeout_seconds=timeout,
                message=_redact_status_message(exc),
            )
        except Exception as exc:
            status = AdapterStatus(
                name=adapter.name,
                status="failed",
                elapsed_ms=_elapsed_ms(started_at),
                timeout_seconds=timeout,
                message=_redact_status_message(exc),
            )
        else:
            adapter_reason = _candidate_adapter_reason(candidates)
            status = AdapterStatus(
                name=adapter.name,
                status="partial" if adapter_reason else "completed",
                candidate_count=len(candidates),
                elapsed_ms=_elapsed_ms(started_at),
                timeout_seconds=timeout,
                message=_partial_status_message(adapter_reason),
                reason_code=adapter_reason,
            )

        if callback is not None:
            callback(status)
        return candidates, status


def _candidate_adapter_reason(candidates: list[Candidate]) -> str | None:
    for candidate in candidates:
        metadata = candidate.payload.metadata or {}
        reason = metadata.get("adapter_reason_code") or candidate.metadata.get("adapter_reason_code")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None


def _partial_status_message(reason: str | None) -> str | None:
    if reason == "time_budget":
        return "Returned partial results before the source time budget expired."
    if reason:
        return "Returned partial results with degraded source coverage."
    return None


async def _expand_profile_queries(
    profile: TopicProfile,
    selection: dict[str, bool] | None,
    context: SourceAdapterContext,
    *,
    low_yield: bool,
) -> TopicProfile:
    """Return a profile whose per-source queries include AI-suggested affiliated
    angles for every selected source (item 1). Fails open to the original profile.
    """
    selected = {name for name, enabled in (selection or {}).items() if enabled}
    if not selected:
        return profile
    from backend.agents.discovery.query_refiner import expand_search_strategy

    expansions = await expand_search_strategy(profile, lookback_hours=context.lookback_hours)
    if not expansions:
        # Even with nothing to add, scrub stale-year markers so a stored strategy's
        # "...2024"/"...2025" queries can't pull out-of-window content that recency
        # then discards. (Previously this early-return skipped sanitization entirely.)
        return _sanitize_bounded_recency_queries(profile, context.lookback_hours)

    source_queries = {key: tuple(value) for key, value in profile.source_queries.items()}
    for source in selected:
        existing = list(source_queries.get(source, ()))
        seen = {q.strip().lower() for q in existing}
        for query in expansions:
            key = query.strip().lower()
            if key and key not in seen:
                existing.append(query)
                seen.add(key)
        source_queries[source] = tuple(existing)
    return _sanitize_bounded_recency_queries(
        replace(profile, source_queries=source_queries),
        context.lookback_hours,
    )


_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|2100)\b")


def _sanitize_bounded_recency_queries(profile: TopicProfile, lookback_hours: int | None) -> TopicProfile:
    if lookback_hours is None or int(lookback_hours or 0) > 24 * 90:
        return profile
    current_year = datetime_now_year()

    def clean_text(value: str) -> str:
        def replace_year(match: re.Match[str]) -> str:
            year = int(match.group(1))
            return str(current_year) if year < current_year else match.group(1)

        return " ".join(_YEAR_RE.sub(replace_year, str(value or "")).split()).strip()

    def clean_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            query = clean_text(value)
            key = query.casefold()
            if query and key not in seen:
                cleaned.append(query)
                seen.add(key)
        return tuple(cleaned)

    source_queries = {
        source: clean_tuple(tuple(values))
        for source, values in profile.source_queries.items()
    }
    cleaned_search = clean_tuple(profile.search_queries)
    cleaned_direct = clean_tuple(profile.direct_episode_queries)
    cleaned_related = clean_tuple(profile.related_episode_queries)
    cleaned_priority = clean_tuple(profile.priority_terms)
    # Nothing stale to rewrite -> return the original object unchanged (keeps the
    # fail-open "no-op" contract for clean profiles).
    if (
        cleaned_search == profile.search_queries
        and source_queries == {k: tuple(v) for k, v in profile.source_queries.items()}
        and cleaned_direct == profile.direct_episode_queries
        and cleaned_related == profile.related_episode_queries
        and cleaned_priority == profile.priority_terms
    ):
        return profile
    return replace(
        profile,
        search_queries=cleaned_search,
        source_queries=source_queries,
        direct_episode_queries=cleaned_direct,
        related_episode_queries=cleaned_related,
        priority_terms=cleaned_priority,
    )


def datetime_now_year() -> int:
    from datetime import UTC, datetime

    return datetime.now(UTC).year


def _dedupe_candidates(candidates: list[Candidate], *, limit: int) -> list[Candidate]:
    if limit <= 0:
        return []
    ranked = sorted(candidates, key=lambda candidate: candidate.score, reverse=True)
    seen: set[str] = set()
    deduped: list[Candidate] = []
    for candidate in ranked:
        key = _candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
        if len(deduped) >= limit:
            break
    return deduped


def _lane_limit(profile: TopicProfile, adapter_name: str, *, default: int, system_max: int) -> int:
    return brief_settings.source_lane_cap(adapter_name)


def _source_limit(value: Any) -> int | None:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None
    if limit < 1:
        return None
    return min(limit * 10, 250)


def _apply_exclusions(profile: TopicProfile, candidates: list[Candidate]) -> tuple[list[Candidate], list[dict[str, Any]]]:
    exclusions = {str(item).strip().lower() for item in profile.exclusions if str(item).strip()}
    if not exclusions:
        return candidates, []

    kept: list[Candidate] = []
    exclusion_reasons: list[dict[str, Any]] = []
    for candidate in candidates:
        excluded_terms = _matched_exclusion_terms(candidate, exclusions)
        if excluded_terms:
            exclusion_reasons.append(
                {
                    "adapter": candidate.adapter,
                    "candidate_id": str(candidate.payload.id),
                    "original_url": candidate.payload.original_url,
                    "source_type": candidate.payload.source_type,
                    "source_name": candidate.payload.source_name,
                    "title": candidate.reason,
                    "excluded_by": list(excluded_terms),
                    "reason": f"Filtered by exclusions: {', '.join(sorted(excluded_terms))}",
                },
            )
            continue
        kept.append(candidate)
    return kept, exclusion_reasons


def _matched_exclusion_terms(candidate: Candidate, exclusions: set[str]) -> set[str]:
    if not exclusions:
        return set()
    fields = [
        candidate.payload.id,
        candidate.payload.source_name or "",
        candidate.payload.source_type or "",
        candidate.payload.raw_text or "",
        candidate.payload.original_url or "",
        str(candidate.payload.metadata.get("source", "")),
        str(candidate.payload.metadata.get("sender_email", "")),
        str(candidate.payload.metadata.get("podcast_title", "")),
    ]
    for key, value in candidate.payload.metadata.items():
        if str(key).lower() == "search_query":
            continue
        fields.extend(_flatten_metadata_value(key))
        fields.extend(_flatten_metadata_value(value))

    haystack = " ".join(value.lower() for value in fields if value)
    return {term for term in exclusions if term in haystack}


def _matches_exclusion(candidate: Candidate, exclusions: set[str]) -> bool:
    if not exclusions:
        return False
    return bool(_matched_exclusion_terms(candidate, exclusions))


def _flatten_metadata_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, bool):
        return [str(value)]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_flatten_metadata_value(item))
        return values
    if isinstance(value, dict):
        values: list[str] = []
        for key, nested in value.items():
            values.extend(_flatten_metadata_value(key))
            values.extend(_flatten_metadata_value(nested))
        return values
    return [str(value)]


def _is_candidate_from_requested_source(candidate: Candidate, profile: TopicProfile) -> bool:
    explicit_sources = list(getattr(profile, "requested_sources", None) or [])
    explicit_sources.extend(getattr(profile, "promoted_sources", None) or [])
    for source in explicit_sources:
        if not isinstance(source, dict):
            continue
        adapter = str(source.get("adapter") or "").strip().lower()
        if adapter != candidate.adapter.lower():
            continue
        ref = str(source.get("ref") or source.get("source_name") or "").strip().lower()
        if not ref:
            continue

        cand_name = str(candidate.payload.source_name or "").strip().lower()
        cand_email = str(candidate.payload.metadata.get("sender_email") or "").strip().lower()
        cand_podcast = str(candidate.payload.metadata.get("podcast_title") or "").strip().lower()

        if ref in (cand_name, cand_email, cand_podcast):
            return True

        cand_url = str(candidate.payload.original_url or "").strip().lower()
        if ref in cand_url:
            return True

    return False


def _apply_topic_relevance(profile: TopicProfile, candidates: list[Candidate], low_yield: bool = False) -> tuple[list[Candidate], list[dict[str, Any]]]:
    topic_tokens = _topic_tokens(profile)
    if len(topic_tokens) < 2:
        return candidates, []
    if not any(_candidate_has_judgeable_topic_text(candidate) for candidate in candidates):
        return candidates, []

    kept: list[Candidate] = []
    dropped: list[dict[str, Any]] = []
    for candidate in candidates:
        if not _candidate_has_judgeable_topic_text(candidate):
            kept.append(candidate)
            continue
        # Foreign-media results are native-language; the English keyword gate cannot
        # judge them fairly and would silently drop on-topic coverage. Exempt them
        # here and let the model-based audit judge them on translated text instead.
        if candidate.adapter == "foreign_media" or candidate.payload.source_type == "foreign_web":
            kept.append(candidate)
            continue
        if _is_candidate_from_requested_source(candidate, profile):
            kept.append(candidate)
            continue
        if _candidate_matches_topic(candidate, topic_tokens, low_yield=low_yield):
            kept.append(candidate)
        else:
            dropped.append(
                {
                    "adapter": candidate.adapter,
                    "candidate_id": str(candidate.payload.id),
                    "original_url": candidate.payload.original_url,
                    "source_type": candidate.payload.source_type,
                    "source_name": candidate.payload.source_name,
                    "title": _candidate_title(candidate),
                    "subject": candidate.payload.metadata.get("subject") or candidate.payload.metadata.get("parent_subject"),
                    "link_text": candidate.payload.metadata.get("link_text"),
                    "metadata": dict(candidate.payload.metadata or {}),
                    "excluded_by": ["low_topic_overlap"],
                    "reason": "Filtered because the item did not overlap the confirmed topic.",
                }
            )

    if kept:
        return kept, dropped
    if _topic_gate_is_specific(topic_tokens):
        return [], dropped
    return candidates, []


def _topic_tokens(profile: TopicProfile) -> set[str]:
    tokens = keyword_set(profile.discovery_text())
    generic = {
        "advice",
        "advise",
        "area",
        "august",
        "brief",
        "city",
        "curate",
        "general",
        "good",
        "interest",
        "know",
        "like",
        "long",
        "male",
        "might",
        "need",
        "old",
        "provide",
        "see",
        "things",
        "traveler",
        "traveling",
        "well",
        "year",
    }
    return {token for token in tokens if token not in generic and len(token) > 2}


def _candidate_matches_topic(candidate: Candidate, topic_tokens: set[str], low_yield: bool = False) -> bool:
    candidate_tokens = keyword_set(_candidate_relevance_text(candidate))
    if not candidate_tokens:
        return False
    overlap = candidate_tokens & topic_tokens
    if low_yield:
        return len(overlap) >= 1
    if len(overlap) >= 2:
        return True
    if len(overlap) == 1 and len(topic_tokens) <= 4:
        return True
    return False


def _topic_gate_is_specific(topic_tokens: set[str]) -> bool:
    return len(topic_tokens) >= 5


def _candidate_relevance_text(candidate: Candidate) -> str:
    metadata = {
        key: value
        for key, value in candidate.payload.metadata.items()
        if str(key).lower() not in {"search_query"}
    }
    fields = [
        candidate.payload.source_name,
        candidate.payload.source_type,
        candidate.payload.raw_text,
        candidate.payload.original_url,
        candidate.reason,
    ]
    for key, value in metadata.items():
        fields.extend(_flatten_metadata_value(key))
        fields.extend(_flatten_metadata_value(value))
    return " ".join(str(value) for value in fields if value)


def _candidate_has_judgeable_topic_text(candidate: Candidate) -> bool:
    fields = [
        candidate.payload.source_name,
        candidate.payload.raw_text,
        candidate.reason,
    ]
    metadata = {
        key: value
        for key, value in candidate.payload.metadata.items()
        if str(key).lower() not in {"search_query"}
    }
    for key, value in metadata.items():
        fields.extend(_flatten_metadata_value(key))
        fields.extend(_flatten_metadata_value(value))
    tokens = keyword_set(" ".join(str(value) for value in fields if value))
    generic = {"candidate", "gmail", "item", "newsletter", "signal", "source", "web"}
    return len({token for token in tokens if token not in generic}) >= 3


def _candidate_key(candidate: Candidate) -> str:
    payload = candidate.payload
    if payload.original_url:
        if "mail.google.com" in payload.original_url:
            return "url:" + payload.original_url.strip().lower()
        return "url:" + _canonical_url(payload.original_url)
    native_id = (
        payload.metadata.get("gmail_message_id")
        or payload.metadata.get("podcast_episode_id")
        or payload.id
    )
    return f"{payload.source_type}:{native_id}"


def _canonical_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        if not key.lower().startswith("utm_"):
            query_items.append((key, value))
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query_items), ""))


def _elapsed_ms(started_at: float) -> int:
    return round((perf_counter() - started_at) * 1000)


def _weight_candidates_by_adapter_fit(
    *,
    profile: TopicProfile,
    adapter: SourceAdapter,
    candidates: list[Candidate],
) -> list[Candidate]:
    profile_signals = _derive_topic_signals(profile)
    if not candidates or not profile_signals:
        return candidates
    bonus = _adapter_signal_bonus(profile_signals=profile_signals, adapter_good_for=adapter.good_for)
    if bonus <= 0:
        return candidates
    weighted = []
    for candidate in candidates:
        weighted.append(replace(candidate, score=min(1.0, max(0.0, candidate.score + bonus)))
        )
    return weighted


def _derive_topic_signals(profile: TopicProfile) -> set[str]:
    profile_text = " ".join(
        [
            str(profile.statement or ""),
            str(profile.scope or ""),
            " ".join(profile.keywords),
            " ".join(profile.subtopics),
        ]
    ).lower()
    signals: set[str] = set()
    for signal, terms in _GOOD_FOR_SIGNAL_MAP.items():
        if any(term in profile_text for term in terms):
            signals.add(signal)

    if profile.depth == "practitioner":
        signals.update({"deep_context", "primary_sources"})
    if profile.recency_weighting == "breaking":
        signals.update({"breaking_news", "fresh_sources"})
    elif profile.recency_weighting in {"last_year", "all_available"}:
        signals.add("broad_discovery")
    return signals


def _adapter_signal_bonus(profile_signals: set[str], adapter_good_for: tuple[str, ...]) -> float:
    if not profile_signals:
        return 0.0
    matching = len(set(adapter_good_for).intersection(profile_signals))
    if matching <= 0:
        return 0.0
    return min(0.18, matching * 0.045)


def _candidate_title(c: Candidate) -> str:
    metadata = c.payload.metadata or {}
    title = (
        metadata.get("title")
        or metadata.get("link_text")
        or metadata.get("youtube_title")
        or metadata.get("podcast_title")
        or metadata.get("subject")
        or metadata.get("parent_subject")
    )
    if title:
        title_str = str(title).strip()
        if title_str.lower() not in {"", "approved gmail newsletter item.", "approved gmail newsletter item", "candidate item", "excluded candidate"}:
            return title_str
    if c.payload.source_name:
        return c.payload.source_name
    if c.payload.original_url:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(c.payload.original_url)
            host = parsed.netloc.removeprefix("www.").strip()
            if host:
                return host
        except Exception:
            pass
    return c.reason or "Source item"
