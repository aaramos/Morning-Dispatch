from __future__ import annotations

import json
import math
from typing import Any

from backend.agents.critic import MAX_CRITIC_ARTICLES, MAX_NEWSLETTER_RECORDS
from backend.agents.digestor.podcast import MAX_DISCOVERED_FEEDS, MAX_PODCAST_EPISODES
from backend.agents.discovery.foreign_media import MAX_FOREIGN_LANGUAGES
from backend.agents.librarian.articles import (
    MAX_ARTICLE_FETCHES,
    MIN_ARTICLE_TEXT_CHARS,
    MIN_CONTEXT_FALLBACK_CHARS,
    REQUEST_TIMEOUT_SECONDS,
)
from backend.agents.source_audit import MAX_AUDIT_CANDIDATES
from backend.agents.editorial_decisions import MAX_EDITORIAL_CANDIDATES
from backend.app.core.config import Settings

MAX_CANDIDATE_BUDGET = 250
MAX_LEAD_ITEMS = 20
MAX_LOOKBACK_HOURS = 8760
MAX_LOOKBACK_DAYS = MAX_LOOKBACK_HOURS // 24
MAX_PER_SOURCE_LIMIT = 40
MAX_TARGET_ITEMS = 250
MODEL_REFINEMENT_LIMIT = 150
MAX_ARTICLE_FETCH_CONCURRENCY = 20

# Single source of truth for per-source ceilings. For each source the configured
# value is BOTH the system maximum AND the default cap a brief gets unless a
# preset scales it down. Adding a source here automatically gives it a discovery
# lane limit, an inclusion cap, and a generated set of presets.
PER_SOURCE_MAX: dict[str, int] = {
    "web_search": 40,
    "foreign_media": 40,
    "gmail": 40,
    "markets": 40,
    "reddit": 30,
    "collections": 25,
    "podcasts": 20,
    "youtube": 20,
}
# Sources not listed above fall back to this cap.
DEFAULT_PER_SOURCE_MAX = 25

# Cross-source "Top Stories" lead section (item 5). The lead count is the number
# of best-of items pulled from every source into the top section of the brief.
TOP_STORIES_MAX = MAX_LEAD_ITEMS
TOP_STORIES_DEFAULT = 5

# Preset tiers expressed as a fraction of each source's max. "max" == the system
# ceiling; the rest are percentage-scaled. Brief defaults use the "medium" (0.6)
# tier, matching the historical _scaled_content_limits(0.6) baseline.
PRESET_SCALE: dict[str, float] = {
    "max": 1.0,
    "large": 0.8,
    "medium": 0.6,
    "focused": 0.4,
}

SYSTEM_CONTENT_LIMITS: dict[str, Any] = {
    "total_items": MAX_CANDIDATE_BUDGET,
    "target_items": 25,
    "lead_items": TOP_STORIES_DEFAULT,
    "quality_floor": "standard",
    "per_source": dict(PER_SOURCE_MAX),
}


def _percent_presets(max_value: int) -> dict[str, int]:
    """Generate focused/medium/large/max presets as percentages of a ceiling."""
    return {
        tier: max(1, round(max_value * scale))
        for tier, scale in PRESET_SCALE.items()
    }


def _normalize_scaled_presets(value: Any, *, bound: int) -> dict[str, int]:
    """Clamp each preset tier to [1, bound], falling back to the % default."""
    raw = value if isinstance(value, dict) else {}
    fallback = _percent_presets(bound)
    return {
        tier: _bounded_int(raw.get(tier), 1, bound) or fallback[tier]
        for tier in ("max", "large", "medium", "focused")
    }


def _scaled_content_limits(scale: float) -> dict[str, Any]:
    def scaled(value: int) -> int:
        return max(1, math.ceil(value * scale))

    return {
        "total_items": scaled(int(SYSTEM_CONTENT_LIMITS["total_items"])),
        "target_items": scaled(int(SYSTEM_CONTENT_LIMITS["target_items"])),
        "lead_items": scaled(int(SYSTEM_CONTENT_LIMITS["lead_items"])),
        "quality_floor": "standard",
        "per_source": {
            key: scaled(int(value))
            for key, value in SYSTEM_CONTENT_LIMITS["per_source"].items()
        },
    }

DEFAULT_BRIEF_CONTROLS: dict[str, Any] = {
    "lookback_hours": 336,
    "content_limits": _scaled_content_limits(0.6),
}

DEFAULT_PIPELINE_LIMITS: dict[str, int] = {
    "article_fetches": MAX_ARTICLE_FETCHES,
    # Fetch is I/O-bound; 15 in-flight shortens the fetch stage on large candidate
    # pools. Kept modest to avoid tripping per-site rate limits (which would reduce
    # successful fetches). Tunable per-profile up to MAX_ARTICLE_FETCH_CONCURRENCY.
    "article_fetch_concurrency": 15,
    "model_refinement_items": MODEL_REFINEMENT_LIMIT,
    "source_audit_candidates": MAX_AUDIT_CANDIDATES,
    "editorial_candidates": MAX_EDITORIAL_CANDIDATES,
    "critic_articles": MAX_CRITIC_ARTICLES,
    "critic_newsletter_records": MAX_NEWSLETTER_RECORDS,
}

PIPELINE_LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "article_fetches": (1, MAX_ARTICLE_FETCHES),
    "article_fetch_concurrency": (1, MAX_ARTICLE_FETCH_CONCURRENCY),
    "model_refinement_items": (0, MODEL_REFINEMENT_LIMIT),
    "source_audit_candidates": (1, MAX_AUDIT_CANDIDATES),
    "editorial_candidates": (1, MAX_EDITORIAL_CANDIDATES),
    "critic_articles": (1, MAX_CRITIC_ARTICLES),
    "critic_newsletter_records": (0, MAX_NEWSLETTER_RECORDS),
}


def brief_settings_status(settings: Settings) -> dict[str, Any]:
    payload = _read_settings_file(settings)
    return {
        "defaults": load_brief_defaults(settings),
        "pipeline_limits": load_pipeline_limits(settings),
        "system_limits": system_limits(settings),
        "youtube_presets": normalize_youtube_presets(payload.get("youtube_presets")),
        "podcast_presets": normalize_podcast_presets(payload.get("podcast_presets")),
        "gmail_presets": normalize_gmail_presets(payload.get("gmail_presets")),
    }


def load_brief_defaults(settings: Settings) -> dict[str, Any]:
    payload = _read_settings_file(settings)
    defaults = payload.get("brief_defaults") if isinstance(payload, dict) else None
    normalized = normalize_brief_controls(defaults)
    normalized["youtube_presets"] = normalize_youtube_presets(payload.get("youtube_presets"))
    normalized["podcast_presets"] = normalize_podcast_presets(payload.get("podcast_presets"))
    normalized["gmail_presets"] = normalize_gmail_presets(payload.get("gmail_presets"))
    return normalized


def save_brief_defaults(settings: Settings, defaults: dict[str, Any]) -> dict[str, Any]:
    payload = _read_settings_file(settings)
    payload["brief_defaults"] = normalize_brief_controls(defaults)
    if "youtube_presets" in defaults:
        payload["youtube_presets"] = normalize_youtube_presets(defaults["youtube_presets"])
    if "podcast_presets" in defaults:
        payload["podcast_presets"] = normalize_podcast_presets(defaults["podcast_presets"])
    if "gmail_presets" in defaults:
        payload["gmail_presets"] = normalize_gmail_presets(defaults["gmail_presets"])
    _write_settings_file(settings, payload)
    return brief_settings_status(settings)


# YouTube and Gmail presets derive their ceiling from the unified per-source map.
# Podcast presets are a distinct UI control (episodes shown) bounded at 5, which
# is intentionally lower than the podcasts inclusion cap in PER_SOURCE_MAX.
PODCAST_PRESET_MAX = 5


def normalize_youtube_presets(value: Any) -> dict[str, int]:
    return _normalize_scaled_presets(value, bound=_source_max_limit("youtube"))


def normalize_podcast_presets(value: Any) -> dict[str, int]:
    return _normalize_scaled_presets(value, bound=PODCAST_PRESET_MAX)


def normalize_gmail_presets(value: Any) -> dict[str, int]:
    return _normalize_scaled_presets(value, bound=_source_max_limit("gmail"))


def load_pipeline_limits(settings: Settings) -> dict[str, int]:
    payload = _read_settings_file(settings)
    limits = payload.get("pipeline_limits") if isinstance(payload, dict) else None
    return normalize_pipeline_limits(limits)


def pipeline_limits_for_profile(settings: Settings, profile: Any) -> dict[str, int]:
    raw_profile_limits = getattr(profile, "pipeline_limits", None)
    if raw_profile_limits is None and isinstance(profile, dict):
        raw_profile_limits = profile.get("pipeline_limits")
    profile_limits = raw_profile_limits if isinstance(raw_profile_limits, dict) else {}
    return normalize_pipeline_limits({**load_pipeline_limits(settings), **profile_limits})


def save_pipeline_limits(settings: Settings, limits: dict[str, Any]) -> dict[str, Any]:
    payload = _read_settings_file(settings)
    payload["pipeline_limits"] = normalize_pipeline_limits(limits)
    _write_settings_file(settings, payload)
    return brief_settings_status(settings)


def normalize_brief_controls(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    fallback = DEFAULT_BRIEF_CONTROLS
    return {
        "lookback_hours": _bounded_int(raw.get("lookback_hours"), 1, MAX_LOOKBACK_HOURS)
        or int(fallback["lookback_hours"]),
        "content_limits": normalize_content_limits(raw.get("content_limits")),
    }


def normalize_content_limits(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    fallback = DEFAULT_BRIEF_CONTROLS["content_limits"]
    return {
        "total_items": _bounded_int(raw.get("total_items"), 1, MAX_CANDIDATE_BUDGET)
        or int(fallback["total_items"]),
        "target_items": _bounded_int(raw.get("target_items"), 1, MAX_TARGET_ITEMS)
        or int(fallback["target_items"]),
        "lead_items": _bounded_int(raw.get("lead_items"), 0, MAX_LEAD_ITEMS)
        if _bounded_int(raw.get("lead_items"), 0, MAX_LEAD_ITEMS) is not None
        else int(fallback["lead_items"]),
        "quality_floor": "strong" if str(raw.get("quality_floor") or "").strip() == "strong" else "standard",
        "per_source": _per_source_limits(raw.get("per_source")),
    }


def normalize_pipeline_limits(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    normalized: dict[str, int] = {}
    for key, fallback in DEFAULT_PIPELINE_LIMITS.items():
        minimum, maximum = PIPELINE_LIMIT_BOUNDS[key]
        bounded = _bounded_int(raw.get(key), minimum, maximum)
        normalized[key] = int(fallback) if bounded is None else bounded
    return normalized


def system_limits(settings: Settings) -> list[dict[str, Any]]:
    return [
        {
            "group": "Brief control caps",
            "items": [
                {"label": "Candidate budget", "value": f"1-{MAX_CANDIDATE_BUDGET}", "note": "Maximum deduped candidates a brief can request."},
                {"label": "Target visible stories", "value": f"1-{MAX_TARGET_ITEMS}", "note": "Maximum visible-story target a brief can request."},
                {"label": "Lead stories", "value": f"0-{MAX_LEAD_ITEMS}", "note": "Maximum preferred lead count."},
                {"label": "Source window", "value": f"1-{MAX_LOOKBACK_DAYS} days", "note": "365-day maximum source window."},
                {"label": "Per-source maximum", "value": "1-40", "note": "Maximum per-source diversity target (up to 20 for YouTube/podcasts, 40 for markets/web/gmail/foreign media)."},
            ],
        },
        {
            "group": "Source discovery caps",
            "items": [
                {"label": "Web results per query", "value": "4-20", "note": "Search provider requests are capped per query."},
                {"label": "Podcast episodes", "value": str(MAX_PODCAST_EPISODES), "note": f"Podcast discovery can add up to {MAX_DISCOVERED_FEEDS} feeds."},
                {"label": "YouTube results", "value": str(settings.youtube_max_results), "note": "Runtime setting, capped by the system at 50."},
                {"label": "Collections results", "value": str(settings.collections_max_results), "note": f"File first-slice limit: {settings.collections_max_file_bytes:,} bytes."},
                {"label": "Markets snapshots", "value": str(settings.markets_max_core_companies + settings.markets_max_related_companies), "note": "Core plus related public companies."},
                {"label": "Foreign languages", "value": str(MAX_FOREIGN_LANGUAGES), "note": "Maximum planned native-language search lanes."},
            ],
        },
        {
            "group": "Fetch and extraction caps",
            "items": [
                {"label": "Article fetches", "value": str(MAX_ARTICLE_FETCHES), "note": "Hard ceiling for fetched article URLs per run."},
                {"label": "Fetch concurrency", "value": str(MAX_ARTICLE_FETCH_CONCURRENCY), "note": "Hard ceiling for parallel article fetches."},
                {"label": "Article fetch timeout", "value": f"{REQUEST_TIMEOUT_SECONDS}s", "note": "Per article request."},
                {"label": "Minimum article text", "value": f"{MIN_ARTICLE_TEXT_CHARS} chars", "note": "Shorter pages need fallback context."},
                {"label": "Fallback snippet", "value": f"{MIN_CONTEXT_FALLBACK_CHARS} chars", "note": "Minimum context for fallback snippets."},
            ],
        },
        {
            "group": "AI review caps",
            "items": [
                {"label": "Model-enriched items", "value": str(min(settings.librarian_model_max_items, MODEL_REFINEMENT_LIMIT)), "note": "Hard ceiling for article summarization/refinement."},
                {"label": "Source audit candidates", "value": str(MAX_AUDIT_CANDIDATES), "note": "Hard ceiling for the pre-ranking quality audit window."},
                {"label": "Editorial candidates", "value": str(MAX_EDITORIAL_CANDIDATES), "note": "Hard ceiling for the editorial selection window."},
                {"label": "Critic articles", "value": str(MAX_CRITIC_ARTICLES), "note": "Hard ceiling for critic article review."},
                {"label": "Newsletter records", "value": str(MAX_NEWSLETTER_RECORDS), "note": "Hard ceiling for Gmail newsletter samples in critic review."},
                {"label": "Model timeout", "value": f"{settings.model_timeout_seconds:g}s", "note": "Per local/cloud model request."},
            ],
        },
    ]


def _read_settings_file(settings: Settings) -> dict[str, Any]:
    try:
        payload = json.loads(settings.brief_settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_settings_file(settings: Settings, payload: dict[str, Any]) -> None:
    settings.brief_settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings.brief_settings_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _source_max_limit(source_name: str) -> int:
    return PER_SOURCE_MAX.get(source_name, DEFAULT_PER_SOURCE_MAX)


def source_inclusion_max(source_name: str) -> int:
    """Public per-source inclusion ceiling (single source of truth, item 7)."""
    return _source_max_limit(source_name)


# Default minimum number of items each active source may inject into a brief even
# when its candidates are only loosely tied to the interest (item 3). Sources not
# listed fall back to DEFAULT_SOURCE_FLOOR. A profile can override any of these
# through content_limits["min_items"].
DEFAULT_MIN_ITEMS: dict[str, int] = {
    "podcasts": 5,
}
DEFAULT_SOURCE_FLOOR = 1
# Items revived to satisfy a floor must clear this relevance bar so a floor never
# injects outright junk (mirrors the historical podcast revival threshold).
SOURCE_FLOOR_SCORE_THRESHOLD = 0.22


def source_min_items(source_name: str, content_limits: Any) -> int:
    """Resolve the inclusion floor for a source, honoring profile overrides."""
    floor = DEFAULT_MIN_ITEMS.get(source_name, DEFAULT_SOURCE_FLOOR)
    if isinstance(content_limits, dict):
        overrides = content_limits.get("min_items")
        if isinstance(overrides, dict) and source_name in overrides:
            try:
                floor = int(overrides[source_name])
            except (TypeError, ValueError):
                pass
    return max(0, min(floor, source_inclusion_max(source_name)))


def _per_source_limits(value: Any) -> dict[str, int]:
    fallback = DEFAULT_BRIEF_CONTROLS["content_limits"]["per_source"]
    raw = value if isinstance(value, dict) else {}
    limits: dict[str, int] = {}
    for key, fallback_value in fallback.items():
        max_allowed = _source_max_limit(key)
        val = _bounded_int(raw.get(key), 1, max_allowed) or int(fallback_value)
        val = min(val, max_allowed)
        limits[key] = val
    return limits


def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < minimum:
        return None
    return min(number, maximum)
