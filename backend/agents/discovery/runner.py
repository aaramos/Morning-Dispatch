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
        "reddit",
        "forum",
        "discussion",
        "social",
        "group",
        "discussion",
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
        candidates, exclusions = _apply_exclusions(
            profile,
            [candidate for adapter_candidates, _status in results for candidate in adapter_candidates],
        )
        candidates, relevance_exclusions = _apply_topic_relevance(profile, candidates)
        candidates = _apply_source_limits(profile, candidates)
        candidates = _dedupe_candidates(
            candidates,
            limit=context.candidate_limit,
        )
        excluded_statuses = [
            AdapterStatus(name=name, status="skipped", message="Source was turned off for this exploration.")
            for name, enabled in selection.items()
            if enabled is False and name in all_names
        ]
        return DiscoveryResult(
            profile=profile,
            candidates=tuple(candidates),
            statuses=tuple([*statuses, *excluded_statuses]),
            exclusions=tuple([*exclusions, *relevance_exclusions]),
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
    ranked = sorted(candidates, key=lambda candidate: candidate.score, reverse=True)
    seen: set[str] = set()
    deduped: list[Candidate] = []
    for candidate in ranked:
        key = _candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
        if len(deduped) >= max(1, limit):
            break
    return deduped


def _apply_source_limits(profile: TopicProfile, candidates: list[Candidate]) -> list[Candidate]:
    per_source = profile.content_limits.get("per_source") if isinstance(profile.content_limits, dict) else None
    if not isinstance(per_source, dict) or not per_source:
        return candidates

    counts: dict[str, int] = {}
    kept: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        limit = _source_limit(per_source.get(candidate.adapter))
        if limit is None:
            kept.append(candidate)
            continue
        current = counts.get(candidate.adapter, 0)
        if current >= limit:
            continue
        counts[candidate.adapter] = current + 1
        kept.append(candidate)
    return kept


def _source_limit(value: Any) -> int | None:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None
    if limit < 1:
        return None
    return min(limit, 100)


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
        str(candidate.payload.metadata.get("subreddit", "")),
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


def _apply_topic_relevance(profile: TopicProfile, candidates: list[Candidate]) -> tuple[list[Candidate], list[dict[str, Any]]]:
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
        if _candidate_matches_topic(candidate, topic_tokens):
            kept.append(candidate)
        else:
            dropped.append(
                {
                    "adapter": candidate.adapter,
                    "candidate_id": str(candidate.payload.id),
                    "original_url": candidate.payload.original_url,
                    "source_type": candidate.payload.source_type,
                    "source_name": candidate.payload.source_name,
                    "title": candidate.reason,
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


def _candidate_matches_topic(candidate: Candidate, topic_tokens: set[str]) -> bool:
    candidate_tokens = keyword_set(_candidate_relevance_text(candidate))
    if not candidate_tokens:
        return False
    overlap = candidate_tokens & topic_tokens
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
    generic = {"candidate", "gmail", "item", "newsletter", "reddit", "signal", "source", "web"}
    return len({token for token in tokens if token not in generic}) >= 3


def _candidate_key(candidate: Candidate) -> str:
    payload = candidate.payload
    if payload.original_url:
        return "url:" + _canonical_url(payload.original_url)
    native_id = (
        payload.metadata.get("gmail_message_id")
        or payload.metadata.get("reddit_thread_id")
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
