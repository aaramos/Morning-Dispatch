from __future__ import annotations

import json
from typing import Any

from backend.agents.critic import MAX_CRITIC_ARTICLES, MAX_NEWSLETTER_RECORDS
from backend.agents.digestor.podcast import MAX_DISCOVERED_FEEDS, MAX_PODCAST_EPISODES
from backend.agents.digestor.reddit import MAX_ACTIVE_SOURCES, MAX_REDDIT_THREADS, POSTS_PER_ACTIVE_SOURCE
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
MAX_PER_SOURCE_LIMIT = 100
MAX_TARGET_ITEMS = 250
MODEL_REFINEMENT_LIMIT = 150
MAX_ARTICLE_FETCH_CONCURRENCY = 20

DEFAULT_BRIEF_CONTROLS: dict[str, Any] = {
    "lookback_hours": 72,
    "content_limits": {
        "total_items": 40,
        "target_items": 12,
        "lead_items": 3,
        "quality_floor": "standard",
        "per_source": {
            "web_search": 15,
            "foreign_media": 4,
            "gmail": 4,
            "reddit": 15,
            "podcasts": 5,
            "youtube": 5,
            "collections": 4,
            "markets": 2,
        },
    },
}

DEFAULT_PIPELINE_LIMITS: dict[str, int] = {
    "article_fetches": MAX_ARTICLE_FETCHES,
    "article_fetch_concurrency": 10,
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
    return {
        "defaults": load_brief_defaults(settings),
        "pipeline_limits": load_pipeline_limits(settings),
        "system_limits": system_limits(settings),
    }


def load_brief_defaults(settings: Settings) -> dict[str, Any]:
    payload = _read_settings_file(settings)
    defaults = payload.get("brief_defaults") if isinstance(payload, dict) else None
    return normalize_brief_controls(defaults)


def save_brief_defaults(settings: Settings, defaults: dict[str, Any]) -> dict[str, Any]:
    payload = _read_settings_file(settings)
    payload["brief_defaults"] = normalize_brief_controls(defaults)
    _write_settings_file(settings, payload)
    return brief_settings_status(settings)


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
                {"label": "Lookback window", "value": f"1-{MAX_LOOKBACK_HOURS} hours", "note": "365-day maximum source window."},
                {"label": "Per-source maximum", "value": f"1-{MAX_PER_SOURCE_LIMIT}", "note": "Maximum per-source diversity target."},
            ],
        },
        {
            "group": "Source discovery caps",
            "items": [
                {"label": "Web results per query", "value": "4-20", "note": "Search provider requests are capped per query."},
                {"label": "Reddit threads", "value": str(MAX_REDDIT_THREADS), "note": f"Up to {MAX_ACTIVE_SOURCES} active communities, {POSTS_PER_ACTIVE_SOURCE} posts each."},
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


def _per_source_limits(value: Any) -> dict[str, int]:
    fallback = DEFAULT_BRIEF_CONTROLS["content_limits"]["per_source"]
    raw = value if isinstance(value, dict) else {}
    limits: dict[str, int] = {}
    for key, fallback_value in fallback.items():
        limits[key] = _bounded_int(raw.get(key), 1, MAX_PER_SOURCE_LIMIT) or int(fallback_value)
    return limits


def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < minimum:
        return None
    return min(number, maximum)
