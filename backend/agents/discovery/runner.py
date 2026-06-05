from __future__ import annotations

import asyncio
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

        # Dedicated source ranking lanes (sorted against each other before competing with
        # other sources for pipeline capacity). These sources should be judged within
        # their own lanes rather than crowded out by high-volume web results.
        target_capacity = max(1, context.candidate_limit)
        profile_total_limit = _explicit_total_limit(profile)
        if profile_total_limit is not None:
            target_capacity = min(target_capacity, profile_total_limit)
        source_plan: tuple[tuple[str, int], ...] = (
            ("markets", _lane_limit(profile, "markets", default=250, system_max=250)),
            ("youtube", _lane_limit(profile, "youtube", default=250, system_max=250)),
            ("podcasts", _lane_limit(profile, "podcasts", default=250, system_max=250)),
            ("gmail", _lane_limit(profile, "gmail", default=250, system_max=250)),
            ("foreign_media", _lane_limit(profile, "foreign_media", default=250, system_max=250)),
            ("reddit", _lane_limit(profile, "reddit", default=250, system_max=250)),
        )

        lane_candidates: list[Candidate] = []
        lane_adapters: set[str] = set()
        for source, source_limit in source_plan:
            if selection.get(source) is not True:
                continue
            raw_candidates = [candidate for candidate in candidates if candidate.adapter == source]
            lane_adapters.add(source)

            if source == "markets":
                explicit_candidates = [c for c in raw_candidates if (c.payload.metadata or {}).get("explicit_ticker") is True]
                regular_candidates = [c for c in raw_candidates if (c.payload.metadata or {}).get("explicit_ticker") is not True]

                deduped_explicit = _dedupe_candidates(
                    sorted(explicit_candidates, key=lambda c: (c.score, c.payload.published_at or c.payload.fetched_at or ""), reverse=True),
                    limit=len(explicit_candidates),
                )

                deduped_regular = _dedupe_candidates(
                    sorted(regular_candidates, key=lambda c: (c.score, c.payload.published_at or c.payload.fetched_at or ""), reverse=True),
                    limit=source_limit,
                )

                lane_candidates.extend(deduped_explicit)
                lane_candidates.extend(deduped_regular)
            else:
                lane_candidates.extend(
                    _dedupe_candidates(
                        sorted(raw_candidates, key=lambda c: (c.score, c.payload.published_at or c.payload.fetched_at or ""), reverse=True),
                        limit=source_limit,
                    )
                )

        other_candidates = [candidate for candidate in candidates if candidate.adapter not in lane_adapters]

        # Apply source limits to non-lane candidates first, then trim to the remaining
        # capacity after reserved lane quotas.
        other_candidates = _apply_source_limits(
            profile,
            other_candidates,
            target_limit=_explicit_total_limit(profile),
        )
        is_constrained = (
            (_explicit_total_limit(profile) is not None and _explicit_total_limit(profile) < 150) or
            (context.candidate_limit < 150)
        )
        if is_constrained:
            non_lane_capacity = max(0, target_capacity - len(lane_candidates))
        else:
            non_lane_capacity = _lane_limit(profile, "web_search", default=250, system_max=250)

        deduped_other = _dedupe_candidates(other_candidates, limit=non_lane_capacity)

        # Combine reserved lane results with the backfilled candidate stream.
        candidates = sorted(list(lane_candidates) + list(deduped_other), key=lambda c: (c.score, c.payload.published_at or c.payload.fetched_at or ""), reverse=True)

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
                message=str(exc),
            )
        except Exception as exc:
            status = AdapterStatus(
                name=adapter.name,
                status="failed",
                elapsed_ms=_elapsed_ms(started_at),
                timeout_seconds=timeout,
                message=str(exc)[:240],
            )
        else:
            status = AdapterStatus(
                name=adapter.name,
                status="completed",
                candidate_count=len(candidates),
                elapsed_ms=_elapsed_ms(started_at),
                timeout_seconds=timeout,
            )

        if callback is not None:
            callback(status)
        return candidates, status


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


def _apply_source_limits(profile: TopicProfile, candidates: list[Candidate], *, target_limit: int | None = None) -> list[Candidate]:
    per_source = profile.content_limits.get("per_source") if isinstance(profile.content_limits, dict) else None
    if not isinstance(per_source, dict) or not per_source:
        return candidates

    counts: dict[str, int] = {}
    kept: list[Candidate] = []
    ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
    kept_ids: set[int] = set()
    for candidate in ranked:
        limit = _source_limit(per_source.get(candidate.adapter))
        if limit is None:
            kept.append(candidate)
            kept_ids.add(id(candidate))
            continue
        current = counts.get(candidate.adapter, 0)
        if current >= limit:
            continue
        counts[candidate.adapter] = current + 1
        kept.append(candidate)
        kept_ids.add(id(candidate))

    if target_limit is not None and len(kept) < target_limit:
        for candidate in ranked:
            if id(candidate) in kept_ids:
                continue
            kept.append(candidate)
            kept_ids.add(id(candidate))
            if len(kept) >= target_limit:
                break
    return kept


def _explicit_total_limit(profile: TopicProfile) -> int | None:
    if not isinstance(profile.content_limits, dict) or "total_items" not in profile.content_limits:
        return None
    try:
        total = int(profile.content_limits.get("total_items"))
    except (TypeError, ValueError):
        return None
    if total < 1:
        return None
    return min(total, 250)


def _lane_limit(profile: TopicProfile, adapter_name: str, *, default: int, system_max: int) -> int:
    per_source = profile.content_limits.get("per_source") if isinstance(profile.content_limits, dict) else None
    if not isinstance(per_source, dict):
        return min(default, system_max)
    raw_limit = _source_limit(per_source.get(adapter_name))
    if raw_limit is None:
        return min(default, system_max)
    return min(raw_limit, system_max)


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
