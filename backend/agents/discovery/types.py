from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from backend.agents.digestor.base import NormalizedPayload

AdapterName = Literal["gmail", "podcasts", "web_search", "foreign_media", "youtube", "collections", "markets", "reddit", "google_news"]
AdapterStatusValue = Literal["pending", "running", "completed", "partial", "timed_out", "failed", "skipped"]
Depth = Literal["practitioner", "informed-generalist"]
RecencyWeighting = Literal["breaking", "recent", "last_year", "all_available"]
ScheduleValue = Literal["hourly", "daily", "weekdays", "weekly", "monthly"]
VALID_SCHEDULES: set[str] = {"hourly", "daily", "weekdays", "weekly", "monthly"}
VALID_SOURCE_ADAPTERS: set[str] = {"gmail", "podcasts", "web_search", "foreign_media", "youtube", "collections", "markets", "reddit", "google_news"}

DEFAULT_SOURCE_SELECTION: dict[str, bool] = {
    "gmail": True,
    "podcasts": True,
    "web_search": True,
    "foreign_media": False,
    "youtube": False,
    "collections": False,
    "markets": False,
    "reddit": False,
    "google_news": False,
}

DEFAULT_EXPLORE_SOURCE_SELECTION: dict[str, bool] = {
    "gmail": False,
    "podcasts": False,
    "web_search": True,
    "foreign_media": False,
    "youtube": False,
    "collections": False,
    "markets": False,
    "reddit": False,
    "google_news": False,
}


@dataclass(frozen=True)
class CostProfile:
    label: str
    timeout_seconds: float


@dataclass(frozen=True)
class SourceAdapterContext:
    exploration_id: str
    lookback_hours: int | None = 24
    candidate_limit: int = 150


@dataclass(frozen=True)
class TopicProfile:
    topic_id: str
    statement: str
    scope: str
    subtopics: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    search_queries: tuple[str, ...] = ()
    source_queries: dict[str, tuple[str, ...]] = field(default_factory=dict)
    foreign_language_plan: tuple[dict[str, Any], ...] = ()
    foreign_regions: tuple[str, ...] = ()
    depth: Depth = "informed-generalist"
    recency_weighting: RecencyWeighting = "recent"
    lookback_hours: int | None = None
    exclusions: tuple[str, ...] = ()
    must_have_terms: tuple[str, ...] = ()
    must_have_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source_selection: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_SOURCE_SELECTION))
    requested_sources: tuple[dict[str, Any], ...] = ()
    promoted_sources: tuple[dict[str, Any], ...] = ()
    gmail_rules: dict[str, Any] = field(default_factory=dict)
    models: dict[str, str | None] = field(default_factory=lambda: {"refinement": None, "brief": None})
    schedule: ScheduleValue | None = None
    schedule_config: dict[str, Any] = field(default_factory=dict)
    delivery_config: dict[str, Any] = field(default_factory=dict)
    content_limits: dict[str, Any] = field(default_factory=dict)
    pipeline_limits: dict[str, Any] = field(default_factory=dict)
    direct_episode_queries: tuple[str, ...] = ()
    related_episode_queries: tuple[str, ...] = ()
    negative_constraints: tuple[str, ...] = ()
    priority_terms: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TopicProfile:
        topic_id = _clean_text(payload.get("topic_id")) or str(uuid.uuid4())
        statement = _clean_text(payload.get("statement"))
        scope = _clean_text(payload.get("scope")) or statement
        depth = _depth(payload.get("depth"))
        recency = _recency(payload.get("recency_weighting"))
        lookback_hours = (
            _lookback_hours(payload.get("lookback_hours"))
            or _lookback_hours_from_text(statement)
            or _lookback_hours_from_text(scope)
        )
        return cls(
            topic_id=topic_id,
            statement=statement,
            scope=scope,
            subtopics=tuple(_string_list(payload.get("subtopics"))),
            keywords=tuple(_string_list(payload.get("keywords"))),
            search_queries=tuple(_string_list(payload.get("search_queries"))),
            source_queries=_source_queries(payload.get("source_queries")),
            foreign_language_plan=tuple(_dict_list(payload.get("foreign_language_plan"))),
            foreign_regions=tuple(_string_list(payload.get("foreign_regions"))[:16]),
            depth=depth,
            recency_weighting=recency,
            lookback_hours=lookback_hours,
            exclusions=tuple(_string_list(payload.get("exclusions"))),
            must_have_terms=tuple(_string_list(payload.get("must_have_terms"))[:6]),
            must_have_aliases=_must_have_aliases(payload.get("must_have_aliases")),
            source_selection=_source_selection(payload.get("source_selection")),
            requested_sources=tuple(_dict_list(payload.get("requested_sources"))),
            promoted_sources=tuple(_dict_list(payload.get("promoted_sources"))),
            gmail_rules=_dict(payload.get("gmail_rules")),
            models=_models(payload.get("models")),
            schedule=_schedule(payload.get("schedule")),
            schedule_config=_dict(payload.get("schedule_config")),
            delivery_config=_dict(payload.get("delivery_config")),
            content_limits=_content_limits(payload.get("content_limits")),
            pipeline_limits=_dict(payload.get("pipeline_limits")),
            direct_episode_queries=tuple(_string_list(payload.get("direct_episode_queries"))),
            related_episode_queries=tuple(_string_list(payload.get("related_episode_queries"))),
            negative_constraints=tuple(_string_list(payload.get("negative_constraints"))),
            priority_terms=tuple(_string_list(payload.get("priority_terms"))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "statement": self.statement,
            "scope": self.scope,
            "subtopics": list(self.subtopics),
            "keywords": list(self.keywords),
            "search_queries": list(self.search_queries),
            "source_queries": {key: list(value) for key, value in self.source_queries.items()},
            "foreign_language_plan": [dict(item) for item in self.foreign_language_plan],
            "foreign_regions": list(self.foreign_regions),
            "depth": self.depth,
            "recency_weighting": self.recency_weighting,
            "lookback_hours": self.lookback_hours,
            "exclusions": list(self.exclusions),
            "must_have_terms": list(self.must_have_terms),
            "must_have_aliases": {key: list(value) for key, value in self.must_have_aliases.items()},
            "source_selection": dict(self.source_selection),
            "requested_sources": [dict(source) for source in self.requested_sources],
            "promoted_sources": [dict(source) for source in self.promoted_sources],
            "gmail_rules": dict(self.gmail_rules),
            "models": dict(self.models),
            "schedule": self.schedule,
            "schedule_config": dict(self.schedule_config),
            "delivery_config": dict(self.delivery_config),
            "content_limits": dict(self.content_limits),
            "pipeline_limits": dict(self.pipeline_limits),
            "direct_episode_queries": list(self.direct_episode_queries),
            "related_episode_queries": list(self.related_episode_queries),
            "negative_constraints": list(self.negative_constraints),
            "priority_terms": list(self.priority_terms),
        }

    def search_text(self) -> str:
        parts = [self.scope or self.statement, *self.subtopics, *self.keywords, *self.search_queries]
        if self.exclusions:
            parts.append("Avoid: " + ", ".join(self.exclusions))
        return " ".join(part for part in parts if part).strip()

    def discovery_text(self) -> str:
        """Positive topic text for external source queries."""
        parts = [self.statement, self.scope, *self.subtopics, *self.keywords, *self.search_queries]
        seen: set[str] = set()
        cleaned: list[str] = []
        for part in parts:
            value = str(part or "").strip()
            key = value.lower()
            if value and key not in seen:
                cleaned.append(value)
                seen.add(key)
        return " ".join(cleaned).strip()

    def query_for_source(self, adapter: str) -> str:
        source_queries = self.source_queries.get(adapter, ())
        if source_queries:
            return " ".join(query for query in source_queries if query).strip()
        if self.search_queries:
            return " ".join(query for query in self.search_queries if query).strip()
        return self.discovery_text()


@dataclass(frozen=True)
class Candidate:
    adapter: str
    payload: NormalizedPayload
    score: float = 0.0
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "score": self.score,
            "reason": self.reason,
            "payload": {
                "id": self.payload.id,
                "source_type": self.payload.source_type,
                "source_name": self.payload.source_name,
                "raw_text": self.payload.raw_text,
                "original_url": self.payload.original_url,
                "published_at": self.payload.published_at,
                "fetched_at": self.payload.fetched_at,
                "metadata": dict(self.payload.metadata or {}),
            },
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AdapterStatus:
    name: str
    status: AdapterStatusValue
    candidate_count: int = 0
    elapsed_ms: int = 0
    timeout_seconds: float | None = None
    message: str | None = None
    reason_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "status": self.status,
            "candidate_count": self.candidate_count,
            "elapsed_ms": self.elapsed_ms,
            "timeout_seconds": self.timeout_seconds,
            "message": self.message,
        }
        if self.reason_code:
            payload["reason_code"] = self.reason_code
        return payload


@dataclass(frozen=True)
class DiscoveryResult:
    profile: TopicProfile
    candidates: tuple[Candidate, ...]
    statuses: tuple[AdapterStatus, ...]
    exclusions: tuple[dict[str, Any], ...] = ()
    notes: tuple[dict[str, Any], ...] = ()

    def payloads(self) -> list[NormalizedPayload]:
        return [candidate.payload for candidate in self.candidates]

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_profile": self.profile.to_dict(),
            "candidate_count": len(self.candidates),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "statuses": [status.to_dict() for status in self.statuses],
            "exclusions": [dict(exclusion) for exclusion in self.exclusions],
            "notes": [dict(note) for note in self.notes],
        }


class SourceAdapter(Protocol):
    name: str
    cost_profile: CostProfile
    good_for: tuple[str, ...]

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        ...

    async def fetch(self, candidate: Candidate) -> NormalizedPayload:
        ...


class AdapterUnavailable(RuntimeError):
    pass


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _schedule(value: Any) -> ScheduleValue | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    if cleaned not in VALID_SCHEDULES:
        raise ValueError(f"Unsupported topic profile schedule: {cleaned}")
    return cleaned  # type: ignore[return-value]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = [_clean_text(item) for item in value]
    return [item for item in cleaned if item]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _source_queries(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {}
    queries: dict[str, tuple[str, ...]] = {}
    for raw_key, raw_queries in value.items():
        key = _clean_text(raw_key)
        if key not in VALID_SOURCE_ADAPTERS:
            continue
        cleaned = tuple(_string_list(raw_queries)[:20])
        if cleaned:
            queries[key] = cleaned
    return queries


def _must_have_aliases(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {}
    aliases: dict[str, tuple[str, ...]] = {}
    for raw_key, raw_values in value.items():
        key = _clean_text(raw_key).casefold()
        if not key:
            continue
        cleaned = tuple(_string_list(raw_values)[:12])
        if cleaned:
            aliases[key] = cleaned
    return aliases


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _content_limits(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    limits: dict[str, Any] = {}
    total_items = _positive_int(value.get("total_items"), maximum=1000)
    if total_items is not None:
        limits["total_items"] = total_items
    target_items = _positive_int(value.get("target_items"), maximum=250)
    if target_items is not None:
        limits["target_items"] = target_items
    lead_items = _positive_int(value.get("lead_items"), maximum=20, allow_zero=True)
    if lead_items is not None:
        limits["lead_items"] = lead_items
    if _clean_text(value.get("quality_floor")) in {"standard", "strong"}:
        limits["quality_floor"] = _clean_text(value.get("quality_floor"))
    per_source: dict[str, int] = {}
    raw_per_source = value.get("per_source")
    if isinstance(raw_per_source, dict):
        for raw_key, raw_limit in raw_per_source.items():
            key = _clean_text(raw_key)
            source_limit = _positive_int(raw_limit, maximum=80)
            if key in VALID_SOURCE_ADAPTERS and source_limit is not None:
                per_source[key] = source_limit
    if per_source:
        limits["per_source"] = per_source
    # Per-source inclusion floors (item 3): minimum items a source may inject into
    # the brief even when only loosely related. Validated like per_source caps.
    min_items: dict[str, int] = {}
    raw_min_items = value.get("min_items")
    if isinstance(raw_min_items, dict):
        for raw_key, raw_floor in raw_min_items.items():
            key = _clean_text(raw_key)
            floor = _positive_int(raw_floor, maximum=80, allow_zero=True)
            if key in VALID_SOURCE_ADAPTERS and floor is not None:
                min_items[key] = floor
    if min_items:
        limits["min_items"] = min_items
    return limits


def _positive_int(value: Any, *, maximum: int, allow_zero: bool = False) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    minimum = 0 if allow_zero else 1
    if number < minimum:
        return None
    return min(number, maximum)


def _source_selection(value: Any) -> dict[str, bool]:
    selection = dict(DEFAULT_SOURCE_SELECTION)
    if isinstance(value, dict):
        for key, enabled in value.items():
            clean_key = _clean_text(key)
            if clean_key in DEFAULT_SOURCE_SELECTION:
                selection[clean_key] = bool(enabled)
    return selection


def _models(value: Any) -> dict[str, str | None]:
    models: dict[str, str | None] = {"refinement": None, "brief": None}
    if isinstance(value, dict):
        for key in models:
            raw_model = value.get(key)
            models[key] = _optional_model_name(raw_model)
    return models


def _depth(value: Any) -> Depth:
    return "practitioner" if _clean_text(value) == "practitioner" else "informed-generalist"


def _recency(value: Any) -> RecencyWeighting:
    cleaned = _clean_text(value)
    if cleaned in {"breaking", "recent", "last_year", "all_available"}:
        return cleaned  # type: ignore[return-value]
    if cleaned == "evergreen":
        return "all_available"
    return "recent"


def _lookback_hours(value: Any) -> int | None:
    if value is None:
        return None
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return None
    if hours < 1:
        return None
    return min(hours, 262800)


def _lookback_hours_from_text(value: Any) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    match = re.search(
        r"\b(?:last|past|previous|prior|trailing|within)\s+(\d{1,3})\s+"
        r"(hour|hours|hr|hrs|day|days|week|weeks|month|months)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    multiplier = 1
    if unit.startswith("day"):
        multiplier = 24
    elif unit.startswith("week"):
        multiplier = 24 * 7
    elif unit.startswith("month"):
        multiplier = 24 * 30
    return _lookback_hours(amount * multiplier)


def _optional_model_name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def fold_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(char for char in text if not unicodedata.combining(char))
