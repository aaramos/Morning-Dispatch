from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import logging
import re
from time import monotonic
from typing import Any

from backend.agents.brief_quality import apply_brief_quality_checks
from backend.agents.critic import apply_critic_repairs
from backend.agents.discovery import (
    DiscoveryRunner,
    DiscoveryResult,
    SourceAdapterContext,
    TopicProfile,
    default_source_registry,
)
from backend.agents.digestor.base import NormalizedPayload
from backend.app.services import brief_settings, mcp_status, model_routing
from backend.app.services.brief_strategy import selected_source_labels, summarize_search_strategy
from backend.app.services.brief_title import tight_brief_title
from backend.agents.editor import prepare_issue_articles
from backend.agents.editorial_decisions import apply_editorial_decisions
from backend.agents.librarian.articles import ArticleFetchResult, fetch_articles_for_payloads
from backend.agents.librarian.enrichment import enrich_articles, refine_ranked_articles_with_model
from backend.agents.source_audit import apply_source_audit
from backend.app.core.config import ensure_runtime_dirs, get_settings
from backend.app.db import database
from backend.agents.discovery.collections_source import collections_status, setup_collections_root
from backend.agents.discovery.markets import markets_available


logger = logging.getLogger(__name__)

_BUILD_QUEUE_TASK: asyncio.Task[None] | None = None
_BUILD_QUEUE_EVENT: asyncio.Event | None = None
_PIPELINE_STAGES = ("discovery", "fetch", "summarize", "audit", "rank", "review", "done")
_EXPLORE_MODEL_REFINEMENT_LIMIT = 150
_STRICT_SOURCE_WINDOW_TYPES = {"gmail_link", "foreign_web", "podcast_episode"}
_DATE_METADATA_KEYS = (
    "published_at",
    "published",
    "publication_date",
    "date",
    "pub_date",
    "created_at",
    "updated_at",
    "search_result_date",
)
_URL_DATE_RE = re.compile(
    r"(?:^|[^\d])(?P<year>20\d{2})[/-](?P<month>0?[1-9]|1[0-2])"
    r"(?:[/-](?P<day>0?[1-9]|[12]\d|3[01]))?(?:[^\d]|$)"
)
_TEXT_DATE_RE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?"
    r"\s+(?P<day>0?[1-9]|[12]\d|3[01]),?\s+(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
async def start_build_queue() -> None:
    global _BUILD_QUEUE_TASK, _BUILD_QUEUE_EVENT
    requeued = database.requeue_running_explorations()
    if requeued:
        logger.info("Requeued %s interrupted exploration build(s)", requeued)
    if _BUILD_QUEUE_TASK is None or _BUILD_QUEUE_TASK.done():
        _BUILD_QUEUE_EVENT = asyncio.Event()
        _BUILD_QUEUE_TASK = asyncio.create_task(_build_queue_worker())
    _BUILD_QUEUE_EVENT.set()


async def stop_build_queue() -> None:
    global _BUILD_QUEUE_TASK, _BUILD_QUEUE_EVENT
    if _BUILD_QUEUE_TASK is None:
        _BUILD_QUEUE_EVENT = None
        return
    _BUILD_QUEUE_TASK.cancel()
    with suppress(asyncio.CancelledError):
        await _BUILD_QUEUE_TASK
    _BUILD_QUEUE_TASK = None
    _BUILD_QUEUE_EVENT = None


def _signal_build_queue() -> None:
    if _BUILD_QUEUE_EVENT is not None:
        _BUILD_QUEUE_EVENT.set()


async def _build_queue_worker() -> None:
    while True:
        if _BUILD_QUEUE_EVENT is None:
            await asyncio.sleep(0.5)
            continue
        await _BUILD_QUEUE_EVENT.wait()
        _BUILD_QUEUE_EVENT.clear()
        while True:
            exploration = database.claim_next_queued_exploration()
            if exploration is None:
                break
            progress = dict(exploration.get("progress") or {})
            queue_options = dict(progress.get("queue_options") or {})
            try:
                raw_lh = queue_options.get("lookback_hours")
                await _run_exploration(
                    str(exploration["topic_id"]),
                    mode=str(exploration.get("mode") or "show_now"),
                    source_selection=dict(exploration.get("source_selection") or {}),
                    candidate_limit=int(queue_options.get("candidate_limit") or 250),
                    lookback_hours=int(raw_lh) if raw_lh is not None else None,
                    existing_exploration=exploration,
                )
            except Exception:
                logger.exception(
                    "Queued exploration %s failed",
                    exploration.get("exploration_id"),
                )


def save_topic_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile = TopicProfile.from_dict(payload)
    return database.upsert_topic_profile(profile.to_dict())


async def source_status() -> dict[str, Any]:
    settings = get_settings()
    try:
        mcp = await mcp_status.status(settings)
    except Exception:
        mcp = {}
    web_enabled = bool(
        settings.web_search_tavily_api_key
        or settings.web_search_brave_api_key
        or settings.web_search_serpapi_api_key
    )
    gmail_enabled = bool(settings.gmail_credentials_path.exists())
    gmail_reason = None
    if not gmail_enabled:
        if not settings.gmail_client_secret_path.exists():
            gmail_reason = "Upload a Gmail OAuth client in Admin Sources, then connect Gmail."
        else:
            gmail_reason = "Finish the Gmail connection in Admin Sources."
    podcast_sources = _all_configured_podcast_sources()
    podcast_enabled = bool(
        settings.podcastindex_api_key
        and settings.podcastindex_api_secret
    ) or bool(podcast_sources)
    youtube_quota = database.youtube_quota_summary()
    youtube_units = int(youtube_quota.get("units_used") or 0)
    youtube_enabled = bool(settings.youtube_api_key)
    youtube_reason = None
    if not youtube_enabled:
        youtube_reason = "Add a YouTube Data API key in Admin Sources."
    elif youtube_units >= 8000:
        youtube_reason = f"YouTube quota is high today ({youtube_units}/10000 units used)."
    collections = collections_status(settings.collections_root)
    collections_enabled = bool(collections.get("root_exists")) and int(collections.get("collection_count") or 0) > 0
    if not collections.get("root_exists"):
        collections_reason = "Create the Collections folder to use local files."
    elif int(collections.get("collection_count") or 0) <= 0:
        collections_reason = "Add a top-level folder inside Collections."
    elif int(collections.get("indexed_count") or 0) <= 0:
        collections_reason = "Ready. Add text or markdown files to improve results."
    else:
        collections_reason = None
    market_enabled = markets_available()
    market_reason = None if market_enabled else "Install yfinance to use free market data."
    return {
        "sources": {
            "web_search": {
                "label": "Web",
                "enabled": web_enabled,
                "setup_required": not web_enabled,
                "reason": None if web_enabled else "Add a Brave, Tavily, or SerpAPI key in Admin Sources.",
            },
            "foreign_media": {
                "label": "Foreign Media",
                "enabled": web_enabled,
                "setup_required": not web_enabled,
                "reason": None if web_enabled else "Foreign media uses Web Search. Add a Brave, Tavily, or SerpAPI key in Admin Sources.",
            },
            "gmail": {
                "label": "Gmail",
                "enabled": gmail_enabled,
                "setup_required": not gmail_enabled,
                "reason": gmail_reason,
            },

            "podcasts": {
                "label": "Podcast",
                "enabled": podcast_enabled,
                "setup_required": not podcast_enabled,
                "reason": None if podcast_enabled else "Add Podcast Index credentials in Admin Sources.",
                "configured_source_count": len(podcast_sources),
            },
            "youtube": {
                "label": "YouTube",
                "enabled": youtube_enabled,
                "setup_required": not youtube_enabled,
                "reason": youtube_reason,
                "quota_units_used": youtube_units,
            },
            "collections": {
                "label": "Collections",
                "enabled": collections_enabled,
                "setup_required": not bool(collections.get("root_exists")),
                "reason": collections_reason,
                "root_path": collections.get("root_path"),
                "collection_count": collections.get("collection_count", 0),
                "indexed_count": collections.get("indexed_count", 0),
                "unsupported_count": collections.get("unsupported_count", 0),
                "failed_count": collections.get("failed_count", 0),
            },
            "markets": {
                "label": "Markets",
                "enabled": market_enabled,
                "setup_required": not market_enabled,
                "reason": market_reason,
                "mode": settings.markets_mode,
                "max_core_companies": settings.markets_max_core_companies,
                "max_related_companies": settings.markets_max_related_companies,
            },
        }
    }


def save_web_search_credentials(*, provider: str, api_key: str) -> dict[str, Any]:
    settings = get_settings()
    clean_provider = provider.strip().lower()
    folder_map = {
        "tavily": "tavily",
        "brave": "brave",
        "serpapi": "serpapi",
    }
    folder = folder_map.get(clean_provider)
    if folder is None:
        raise ValueError("Unsupported web-search provider")
    path = settings.secrets_dir / folder / "api_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        logger.warning("Could not tighten permissions on %s", path.parent)
    path.write_text(api_key.strip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning("Could not tighten permissions on %s", path)
    return {
        "provider": clean_provider,
        "configured": True,
    }


def save_youtube_credentials(*, api_key: str) -> dict[str, Any]:
    settings = get_settings()
    path = settings.secrets_dir / "youtube" / "api_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        logger.warning("Could not tighten permissions on %s", path.parent)
    path.write_text(api_key.strip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning("Could not tighten permissions on %s", path)
    return {
        "configured": True,
    }


def setup_collections() -> dict[str, Any]:
    settings = get_settings()
    if settings.collections_root is None:
        raise ValueError("Collections root is not configured")
    return setup_collections_root(settings.collections_root)


def cleanup_expired_exploration_briefs(retention_days: int = 90) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=max(1, retention_days))
    return database.clear_expired_exploration_briefs(
        before_started_at=cutoff.isoformat(timespec="seconds")
    )


def purge_expired_deleted_explorations() -> int:
    return database.purge_expired_deleted_explorations()


async def run_discovery(
    topic_id: str,
    *,
    mode: str = "show_now",
    source_selection: dict[str, bool] | None = None,
    candidate_limit: int = 250,
    lookback_hours: int | None = None,
) -> dict[str, Any] | None:
    record = database.get_topic_profile(topic_id)
    if record is None:
        return None

    profile = _strengthen_profile_for_run(TopicProfile.from_dict(record["profile"]))
    merged_selection = _merged_source_selection(profile, source_selection)
    resolved_lookback_hours = _resolve_lookback_hours(profile, lookback_hours)
    resolved_candidate_limit = _resolve_candidate_limit(profile, candidate_limit)
    exploration = database.create_exploration(
        topic_id=topic_id,
        mode=mode,
        source_selection=merged_selection,
    )
    try:
        context = SourceAdapterContext(
            exploration_id=str(exploration["exploration_id"]),
            candidate_limit=resolved_candidate_limit,
            lookback_hours=resolved_lookback_hours,
        )
        result = await DiscoveryRunner(default_source_registry()).run(
            profile,
            source_selection=merged_selection,
            context=context,
        )
        completed = database.update_exploration_status(
            str(exploration["exploration_id"]),
            status="complete",
        )
        return {
            "exploration": completed or exploration,
            "discovery": result.to_dict(),
        }
    except Exception as exc:  # pragma: no cover - important path for production reliability.
        logger.exception(
            "Exploration discovery %s failed for topic profile %s",
            exploration["exploration_id"],
            topic_id,
        )
        database.update_exploration_status(
            str(exploration["exploration_id"]),
            status="failed",
        )
        raise


async def run_show_now(
    topic_id: str,
    *,
    source_selection: dict[str, bool] | None = None,
    candidate_limit: int = 250,
    lookback_hours: int | None = None,
) -> dict[str, Any] | None:
    return await _run_exploration(
        topic_id,
        mode="show_now",
        source_selection=source_selection,
        candidate_limit=candidate_limit,
        lookback_hours=lookback_hours,
    )


def start_show_now(
    topic_id: str,
    *,
    mode: str = "show_now",
    source_selection: dict[str, bool] | None = None,
    candidate_limit: int = 250,
    lookback_hours: int | None = None,
) -> dict[str, Any] | None:
    record = database.get_topic_profile(topic_id)
    if record is None:
        return None

    profile = TopicProfile.from_dict(record["profile"])
    merged_selection = _merged_source_selection(profile, source_selection)
    resolved_lookback_hours = _resolve_lookback_hours(profile, lookback_hours)
    resolved_candidate_limit = _resolve_candidate_limit(profile, candidate_limit)
    exploration = database.create_exploration(
        topic_id=topic_id,
        mode=mode,
        source_selection=merged_selection,
        status="queued",
    )
    initial_progress = _initial_progress(merged_selection)
    initial_progress["queue"] = {
        "status": "queued",
        "message": "Build queued. Waiting for the current brief build to finish.",
        "action": "build",
    }
    initial_progress["queue_options"] = {
        "candidate_limit": resolved_candidate_limit,
        "lookback_hours": resolved_lookback_hours,
    }
    database.update_exploration_progress(
        str(exploration["exploration_id"]),
        progress=initial_progress,
    )
    _signal_build_queue()
    return database.get_exploration(exploration["exploration_id"])


def start_rebuild(
    exploration_id: str,
    *,
    source_selection: dict[str, bool] | None = None,
    candidate_limit: int = 250,
    lookback_hours: int | None = None,
) -> dict[str, Any] | None:
    exploration = database.get_exploration(exploration_id)
    if exploration is None or exploration.get("deleted_at"):
        return None
    topic_id = str(exploration["topic_id"])
    topic = database.get_topic_profile(topic_id)
    if topic is None:
        return None
    profile = TopicProfile.from_dict(topic["profile"])
    merged_selection = _merged_source_selection(profile, source_selection or exploration.get("source_selection"))
    resolved_lookback_hours = _resolve_lookback_hours(profile, lookback_hours)
    resolved_candidate_limit = _resolve_candidate_limit(profile, candidate_limit)
    progress = _initial_progress(merged_selection)
    progress["queue"] = {
        "status": "queued",
        "message": "Rebuild queued. The full pipeline will run again.",
        "action": "rebuild",
    }
    progress["queue_options"] = {
        "candidate_limit": resolved_candidate_limit,
        "lookback_hours": resolved_lookback_hours,
    }
    database.clear_inference_metrics_for_run(exploration_id)
    rebuilt = database.reset_exploration_for_rebuild(
        exploration_id,
        source_selection=merged_selection,
        progress=progress,
    )
    if rebuilt is None:
        return None
    _signal_build_queue()
    return database.get_exploration(exploration_id)


async def run_scheduled(
    topic_id: str,
    *,
    source_selection: dict[str, bool] | None = None,
    candidate_limit: int = 250,
    lookback_hours: int | None = None,
) -> dict[str, Any] | None:
    return await _run_exploration(
        topic_id,
        mode="scheduled",
        source_selection=source_selection,
        candidate_limit=candidate_limit,
        lookback_hours=lookback_hours,
    )


async def _run_exploration(
    topic_id: str,
    *,
    mode: str,
    source_selection: dict[str, bool] | None,
    candidate_limit: int,
    lookback_hours: int | None,
    existing_exploration: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    started_at = monotonic()
    record = database.get_topic_profile(topic_id)
    if record is None:
        return None

    profile = TopicProfile.from_dict(record["profile"])
    merged_selection = _merged_source_selection(profile, source_selection)
    lookback_hours = _resolve_lookback_hours(profile, lookback_hours)
    candidate_limit = _resolve_candidate_limit(profile, candidate_limit)

    exploration = existing_exploration
    if exploration is None:
        exploration = database.create_exploration(
            topic_id=topic_id,
            mode=mode,
            source_selection=merged_selection,
        )

    exploration_id = str(exploration["exploration_id"])
    database.update_exploration_status(exploration_id, status="running")
    progress = _initial_progress(merged_selection)
    prior_progress = existing_exploration.get("progress") if existing_exploration else {}
    queue_action = ""
    if isinstance(prior_progress, dict):
        queue_action = str((prior_progress.get("queue") or {}).get("action") or "")
    progress["queue"] = {
        "status": "running",
        "message": "Running the full pipeline now." if queue_action == "rebuild" else "Building now.",
        "action": queue_action or "build",
    }
    database.update_exploration_progress(exploration_id, progress=progress)

    registry = default_source_registry()
    _set_pipeline_stage(progress, "discovery", "running")
    _persist_progress(exploration_id, progress)
    try:
        context = SourceAdapterContext(
            exploration_id=exploration_id,
            candidate_limit=candidate_limit,
            lookback_hours=lookback_hours,
        )

        def on_adapter_status(status) -> None:
            _set_source_status(progress, status)
            _persist_progress(exploration_id, progress)

        discovery = await DiscoveryRunner(registry).run(
            profile,
            source_selection=merged_selection,
            context=context,
            on_adapter_status=on_adapter_status,
        )
        _set_pipeline_stage(progress, "discovery", "done")
        _set_exclusion_reasons(progress, discovery.exclusions)
        _set_requested_source_issues(progress, _build_source_issues(profile, discovery, merged_selection))
        _persist_progress(exploration_id, progress)

        for status in discovery.statuses:
            _set_source_status(progress, status)
        _set_candidate_count(progress, len(discovery.payloads()))

        payloads = discovery.payloads()
        stage_seconds: dict[str, float] = {"discovery": round(monotonic() - started_at, 3)}
        _set_pipeline_stage(progress, "fetch", "running")
        _persist_progress(exploration_id, progress)

        stage_started = monotonic()
        pipeline_limits = brief_settings.pipeline_limits_for_profile(get_settings(), profile)
        fetched_articles = await fetch_articles_for_payloads(
            payloads,
            max_articles=pipeline_limits["article_fetches"],
            concurrency=pipeline_limits["article_fetch_concurrency"],
        )
        stage_seconds["fetching"] = round(monotonic() - stage_started, 3)
        fetched_articles, date_review_summary = await _adjudicate_dates_before_source_window_filter(
            profile,
            fetched_articles,
            lookback_hours=lookback_hours,
            inference_run_id=exploration_id,
            max_candidates=pipeline_limits["source_audit_candidates"],
        )
        if date_review_summary:
            progress["source_date_review"] = date_review_summary
            _persist_progress(exploration_id, progress)
        fetched_articles, source_window_issues = _apply_source_window_filter(
            profile,
            fetched_articles,
            lookback_hours=lookback_hours,
        )
        progress["source_window"] = {
            "status": "completed",
            "source_scope": _source_scope_label(lookback_hours),
            "excluded_count": len(source_window_issues),
        }
        if source_window_issues:
            progress["source_filter_notes"] = [
                *list(progress.get("source_filter_notes") or []),
                *source_window_issues,
            ]
        _set_pipeline_stage(progress, "fetch", "done")
        _persist_progress(exploration_id, progress)

        stage_started = monotonic()
        stage_started = monotonic()
        article_results = await _run_digest_core(
            profile=profile,
            payloads=payloads,
            fetched_articles=fetched_articles,
            lookback_hours=lookback_hours,
            inference_run_id=exploration_id,
            progress=progress,
            persist=lambda: _persist_progress(exploration_id, progress),
        )
        stage_seconds["editorial"] = _elapsed_stage_seconds(stage_started)

        # Extract intermediates and compile/save reporting log
        intermediates = progress.pop("_intermediates", {})
        try:
            from backend.app.services.reporting import compile_reporting_data, save_reporting_log
            report_data = compile_reporting_data(
                exploration_id=exploration_id,
                discovery=discovery,
                fetched_articles=fetched_articles,
                source_window_issues=source_window_issues or [],
                enriched_articles=intermediates.get("enriched", []),
                ranked_articles=intermediates.get("ranked", []),
                after_audit=intermediates.get("after_audit", []),
                after_editorial=intermediates.get("after_editorial", []),
                after_critic=intermediates.get("after_critic", []),
                final_results=article_results,
                progress=progress,
            )
            progress["candidate_reporting_data"] = report_data
            save_reporting_log(exploration_id, report_data)
        except Exception as exc:
            logger.exception("Failed to compile or save reporting log for exploration %s: %s", exploration_id, exc)
        _add_final_source_mix_issues(progress, discovery, article_results, merged_selection)
        if mode == "scheduled":
            _promote_explore_sources(topic_id=topic_id, discovery=discovery, article_results=article_results)
        _set_pipeline_stage(progress, "done", "running")
        _set_candidate_count(progress, len(payloads))

        stage_started = monotonic()
        title = _brief_title(profile)
        configured_source_count = _selected_source_count(discovery, merged_selection)
        snapshot = database.ingested_snapshot(payloads, configured_source_count, article_results)
        newsletter_source_notes = _newsletter_source_notes_for_brief(payloads, article_results)

        def build_stats() -> dict[str, Any]:
            stats = database.build_digest_stats(
                configured_source_count=configured_source_count,
                newsletter_count=len(newsletter_source_notes),
                link_count=sum(1 for payload in payloads if payload.source_type == "gmail_link"),
                podcast_episode_count=sum(1 for payload in payloads if payload.source_type == "podcast_episode"),
                article_results=article_results,
                duration_seconds=round(monotonic() - started_at, 3),
                inference_run_id=exploration_id,
                stage_seconds=stage_seconds,
            )
            stats["search_strategy"] = _brief_search_strategy(profile, merged_selection, lookback_hours)
            return stats

        digest_stats = build_stats()
        html = database.render_ingested_issue(
            title,
            snapshot,
            payloads,
            article_results,
            lookback_hours,
            generated_at=database.utc_now(),
            issue_id=exploration_id,
            digest_stats=digest_stats,
            newsletter_payloads=newsletter_source_notes,
        )
        brief_ref = _write_exploration_brief(exploration_id, html)
        stage_seconds["publishing"] = _elapsed_stage_seconds(stage_started)
        # Re-build stats and re-render so the just-measured publishing duration is
        # reflected in the brief's stats sidebar.
        digest_stats = build_stats()
        _apply_model_health_to_progress(progress, digest_stats)
        html = database.render_ingested_issue(
            title,
            snapshot,
            payloads,
            article_results,
            lookback_hours,
            generated_at=database.utc_now(),
            issue_id=exploration_id,
            digest_stats=digest_stats,
            newsletter_payloads=newsletter_source_notes,
        )
        brief_ref = _write_exploration_brief(exploration_id, html)
        database.record_served_undated_items(
            profile.topic_id,
            _served_undated_items_from_results(article_results),
        )

        progress["brief"] = {
            "title": title,
            "html_path": f"/api/explore/explorations/{exploration_id}/brief/html",
            "snapshot": snapshot,
            "stats": digest_stats,
            "candidate_count": len(payloads),
        }
        if progress.get("requested_source_issues"):
            progress["built_with_issues"] = True
        _set_pipeline_stage(progress, "done", "done")
        _persist_progress(exploration_id, progress)

        completed = database.update_exploration_status(
            exploration_id,
            status="complete",
            brief_ref=brief_ref,
        )
        return {
            "exploration": completed or exploration,
            "discovery": discovery.to_dict(),
            "brief": {
                "title": title,
                "html_path": f"/api/explore/explorations/{exploration_id}/brief/html",
                "snapshot": snapshot,
                "stats": digest_stats,
            },
        }
    except Exception as exc:  # pragma: no cover - important path for production reliability.
        logger.exception(
            "Exploration %s failed for topic profile %s",
            exploration_id,
            topic_id,
        )
        _set_pipeline_stage(progress, "done", "failed")
        progress["error"] = str(exc)
        database.update_exploration_progress(exploration_id, progress=progress)
        database.update_exploration_status(
            exploration_id,
            status="failed",
        )
        raise


def _apply_model_health_to_progress(progress: dict[str, Any], stats: dict[str, Any]) -> None:
    model_calls = int(stats.get("model_call_count") or 0)
    model_successes = int(stats.get("model_success_count") or 0)
    model_failures = int(stats.get("model_failure_count") or 0)
    included_articles = int(stats.get("included_article_count") or 0)
    if not model_calls:
        return
    if model_successes > 0 and included_articles > 0:
        progress["model_health"] = {
            "status": "ok",
            "message": f"AI completed {model_successes}/{model_calls} model call(s).",
        }
        return

    if model_successes == 0:
        message = "AI review did not complete; the brief was built with fallback checks."
    elif included_articles == 0:
        message = "AI ran, but no eligible stories survived the source checks."
    else:
        message = "AI review completed with some failed model calls."
    progress["model_health"] = {
        "status": "degraded",
        "message": message,
        "model_call_count": model_calls,
        "model_success_count": model_successes,
        "model_failure_count": model_failures,
        "included_article_count": included_articles,
    }
    issues = list(progress.get("source_audit_issues") or [])
    issue = {"source_name": "AI review", "reason": message}
    if issue not in issues:
        issues.append(issue)
    progress["source_audit_issues"] = issues
    progress["built_with_issues"] = True


def _add_final_source_mix_issues(
    progress: dict[str, Any],
    discovery: DiscoveryResult,
    article_results: list[ArticleFetchResult],
    source_selection: dict[str, bool],
) -> None:
    payload_adapters = {candidate.payload.id: candidate.adapter for candidate in discovery.candidates}
    included_counts: dict[str, int] = {}
    for result in article_results:
        if result.fetched and result.tier != "dropped":
            adapter = payload_adapters.get(result.payload.id, _adapter_from_payload_type(result.payload.source_type, result.payload.metadata))
            if adapter:
                included_counts[adapter] = included_counts.get(adapter, 0) + 1
    candidate_counts = {status.name: status.candidate_count for status in discovery.statuses}
    statuses = {status.name: status for status in discovery.statuses}
    selected_non_gmail = {
        source
        for source, enabled in source_selection.items()
        if enabled and source not in {"gmail", "collections"}
    }
    if not selected_non_gmail:
        return
    issues = list(progress.get("requested_source_issues") or [])
    for source in sorted(selected_non_gmail):
        if included_counts.get(source, 0) > 0:
            continue
        candidate_count = candidate_counts.get(source, 0)
        if candidate_count > 0:
            reason = (
                f"{_adapter_label(source)} returned {candidate_count} candidate(s), "
                "but none survived fetch, audit, ranking, and review into the final brief."
            )
        elif statuses.get(source) and statuses[source].message:
            reason = str(statuses[source].message)
        else:
            reason = f"{_adapter_label(source)} was selected but returned no usable candidates for this run."
        issue = {"source_name": _adapter_label(source), "reason": reason}
        if issue not in issues:
            issues.append(issue)
    if issues != list(progress.get("requested_source_issues") or []):
        progress["requested_source_issues"] = issues
        progress["built_with_issues"] = True
    if not any(included_counts.get(source, 0) for source in selected_non_gmail) and included_counts.get("gmail", 0):
        issues = list(progress.get("requested_source_issues") or [])
        issue = {
            "source_name": "Source mix",
            "reason": "Requested non-Gmail sources produced no included stories; this brief is relying on Gmail/newsletter fallback content.",
        }
        if issue not in issues:
            issues.append(issue)
        progress["requested_source_issues"] = issues
        progress["built_with_issues"] = True


def _adapter_from_payload_type(source_type: str, metadata: dict[str, Any] | None = None) -> str:
    metadata = metadata or {}
    if source_type == "gmail_link":
        if metadata.get("search_query") or metadata.get("search_provider"):
            return "web_search"
        return "gmail"
    return {
        "gmail": "gmail",
        "podcast_episode": "podcasts",
        "youtube_video": "youtube",
        "foreign_web": "foreign_media",
        "market_snapshot": "markets",
        "collection_chunk": "collections",
        "web_search": "web_search",
    }.get(source_type, "")


def _promote_explore_sources(
    *,
    topic_id: str,
    discovery: DiscoveryResult,
    article_results: list[ArticleFetchResult],
) -> None:
    kept_payloads = [
        result.payload
        for result in article_results
        if result.tier != "dropped" and result.status == "fetched"
    ]
    if not kept_payloads:
        return

    payload_adapters: dict[str, str] = {
        candidate.payload.id: candidate.adapter
        for candidate in discovery.candidates
    }
    promoted: list[dict[str, Any]] = []
    seen: set[tuple[str, str, bool, str | None]] = set()
    for payload in kept_payloads:
        adapter = payload_adapters.get(payload.id) or _infer_payload_adapter(payload)
        source = _extract_promoted_source(adapter=adapter, payload=payload)
        if source is None:
            continue
        key = (
            source["adapter"],
            source["ref"],
            source["has_feed"],
            source.get("feed_url"),
        )
        if key in seen:
            continue
        seen.add(key)
        promoted.append(source)

    if not promoted:
        return

    for source in promoted:
        try:
            database.add_promoted_source(
                topic_id=topic_id,
                adapter=source["adapter"],
                ref=source["ref"],
                has_feed=source["has_feed"],
                feed_url=source.get("feed_url"),
            )
        except Exception:
            logger.exception("Failed to persist explore promoted source for topic %s", topic_id)

    topic = database.get_topic_profile(topic_id)
    if topic is None:
        return
    refreshed = _normalize_promoted_sources(
        [*database.list_promoted_sources(topic_id), *topic["profile"].get("promoted_sources", [])],
    )
    profile = dict(topic["profile"])
    profile["promoted_sources"] = refreshed
    profile["topic_id"] = topic_id
    profile["statement"] = str(topic.get("statement") or profile.get("statement") or "")
    profile["schedule"] = topic.get("schedule")
    database.upsert_topic_profile(profile)


def _all_configured_podcast_sources() -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for digest in database.list_digests(include_archived=False):
        for source in digest.get("sources", []):
            if not isinstance(source, dict) or source.get("type") not in {"podcast_rss", "podcast_search"}:
                continue
            key = str(source.get("feed_url") or source.get("query") or source.get("title") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            sources.append(dict(source))
    return sources


def _infer_payload_adapter(payload: Any) -> str | None:
    if not isinstance(payload, ArticleFetchResult):
        source_type = str(payload.source_type).strip()
        metadata = dict(getattr(payload, "metadata", {}))
    else:
        source_type = str(payload.payload.source_type).strip()
        metadata = dict(payload.payload.metadata)
    if source_type == "podcast_episode":
        return "podcasts"
    if source_type == "youtube_video":
        return "youtube"
    if source_type == "foreign_web":
        return "foreign_media"
    if source_type == "collection_chunk":
        return "collections"
    if source_type == "market_snapshot":
        return "markets"
    if source_type in {"gmail", "gmail_link"}:
        if metadata.get("search_query") or metadata.get("search_provider"):
            return "web_search"
        return "gmail"
    return None


def _extract_promoted_source(*, adapter: str | None, payload: Any) -> dict[str, Any] | None:
    if not adapter:
        return None
    normalized_adapter = str(adapter).strip()
    if not normalized_adapter:
        return None

    if isinstance(payload, ArticleFetchResult):
        payload_obj = payload.payload
    else:
        payload_obj = payload
    metadata = dict(payload_obj.metadata)
    source_name = str(payload_obj.source_name).strip()

    if normalized_adapter == "gmail":
        ref = str(metadata.get("sender_email") or source_name).strip()
        if not ref:
            return None
        return {"adapter": normalized_adapter, "ref": ref, "has_feed": False, "feed_url": None}

    if normalized_adapter == "podcasts":
        ref = str(metadata.get("podcast_title") or metadata.get("podcast") or source_name).strip()
        if not ref:
            return None
        feed_url = str(metadata.get("feed_url") or "").strip() or None
        return {
            "adapter": normalized_adapter,
            "ref": ref,
            "has_feed": bool(feed_url),
            "feed_url": feed_url,
        }

    if normalized_adapter == "web_search":
        ref = str(metadata.get("search_query") or payload_obj.original_url or source_name).strip()
        if not ref:
            return None
        return {"adapter": normalized_adapter, "ref": ref, "has_feed": False, "feed_url": None}

    if normalized_adapter == "youtube":
        ref = str(metadata.get("channel_name") or source_name).strip()
        if not ref:
            return None
        return {"adapter": normalized_adapter, "ref": ref, "has_feed": False, "feed_url": None}

    if normalized_adapter == "collections":
        ref = str(metadata.get("collection_name") or source_name).strip()
        if not ref:
            return None
        return {"adapter": normalized_adapter, "ref": ref, "has_feed": False, "feed_url": None}

    if normalized_adapter == "markets":
        ref = str(metadata.get("ticker") or source_name).strip()
        if not ref:
            return None
        return {"adapter": normalized_adapter, "ref": ref, "has_feed": False, "feed_url": None}

    return None


def _normalize_promoted_sources(values: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, bool, str | None]] = set()
    for value in values:
        if isinstance(value, dict):
            candidate = value
            adapter = str(candidate.get("adapter") or "").strip()
            ref = str(candidate.get("ref") or "").strip()
            if not adapter or not ref:
                continue
            has_feed = bool(candidate.get("has_feed"))
            feed_url = str(candidate.get("feed_url") or "").strip() or None
        elif isinstance(value, tuple) and len(value) >= 2:
            adapter = str(value[0]).strip()
            ref = str(value[1]).strip()
            if not adapter or not ref:
                continue
            has_feed = False
            feed_url = None
        else:
            continue

        key = (adapter, ref, has_feed, feed_url)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "adapter": adapter,
            "ref": ref,
            "has_feed": has_feed,
            "feed_url": feed_url,
        })
    return normalized


def read_brief_html(exploration_id: str) -> str | None:
    exploration = database.get_exploration(exploration_id)
    if exploration is None or exploration.get("deleted_at") or not exploration.get("brief_ref"):
        return None
    path = Path(str(exploration["brief_ref"]))
    settings = get_settings()
    output_dir = (settings.data_dir / "digest-output").resolve()
    try:
        resolved_path = path.resolve()
    except OSError:
        return None
    if output_dir not in (resolved_path, *resolved_path.parents):
        return None
    try:
        return resolved_path.read_text(encoding="utf-8")
    except OSError:
        return None


async def _run_digest_core(
    *,
    profile: TopicProfile,
    payloads: list[Any],
    fetched_articles: list[ArticleFetchResult],
    lookback_hours: int | None = 24,
    inference_run_id: str,
    progress: dict[str, Any],
    persist: Callable[[], None],
) -> list[ArticleFetchResult]:
    digest = {
        "id": profile.topic_id,
        "name": _brief_title(profile),
        "interest": profile.search_text(),
        "threshold": 0.45,
        "content_limits": dict(profile.content_limits),
        "recency_weighting": profile.recency_weighting,
    }
    _set_pipeline_stage(progress, "summarize", "running")
    persist()

    enriched_articles = await enrich_articles(fetched_articles, model_max_items=0)
    enriched_articles = database.apply_feedback_to_candidates(profile.topic_id, enriched_articles)
    ranked_articles = prepare_issue_articles(digest, enriched_articles)

    _set_pipeline_stage(progress, "summarize", "done")
    _set_pipeline_stage(progress, "rank", "running")
    persist()

    settings = get_settings()
    pipeline_limits = brief_settings.pipeline_limits_for_profile(settings, profile)
    brief_model = profile.models.get("brief")
    librarian_resolution = model_routing.client_for_agent(
        "librarian",
        settings=settings,
        items=ranked_articles,
        model_override=brief_model,
    )
    librarian_client = librarian_resolution.client

    if librarian_client is not None:
        ranked_articles = database.apply_cached_model_enrichments(
            ranked_articles,
            model_name=_model_client_name(librarian_client, settings.librarian_model),
            limit=settings.librarian_model_max_items,
        )

    article_results = await refine_ranked_articles_with_model(
        ranked_articles,
        model_client=librarian_client,
        model_max_items=min(
            settings.librarian_model_max_items,
            pipeline_limits["model_refinement_items"],
            _EXPLORE_MODEL_REFINEMENT_LIMIT,
        ),
        inference_run_id=inference_run_id,
        metrics_mode="single",
    )
    _set_pipeline_stage(progress, "audit", "running")
    progress["source_audit"] = {
        "status": "running",
        "message": "Auditing candidate sources for freshness, fit, and source quality.",
    }
    persist()
    audit_client = model_routing.client_for_agent(
        "source_audit",
        settings=settings,
        items=article_results,
        model_override=brief_model,
    ).client
    article_results, audit_decisions, audit_summary = await apply_source_audit(
        profile,
        article_results,
        lookback_hours=lookback_hours,
        model_client=audit_client,
        inference_run_id=inference_run_id,
        max_candidates=pipeline_limits["source_audit_candidates"],
    )
    after_audit = list(article_results)
    progress["source_audit"] = audit_summary
    if audit_summary.get("issues"):
        progress["source_filter_notes"] = [
            *list(progress.get("source_filter_notes") or []),
            *list(audit_summary.get("issues") or []),
        ]
    if audit_summary.get("model_issue") or audit_summary.get("status") == "failed":
        issue_reason = str(audit_summary.get("model_issue") or "Source audit could not complete.").strip()
        issue = {"source_name": "Source Audit", "reason": issue_reason}
        progress["source_audit_issues"] = [
            *list(progress.get("source_audit_issues") or []),
            issue,
        ]
        progress["built_with_issues"] = True
    _set_pipeline_stage(progress, "audit", "done" if audit_summary.get("status") != "failed" else "failed")
    persist()
    _init_reasoning_bucket(progress, "editorial", persist)
    _init_reasoning_bucket(progress, "critic", persist)
    _set_pipeline_stage(progress, "rank", "done")
    _set_pipeline_stage(progress, "review", "running")
    persist()

    flush_reasoning = _reasoning_flusher(progress, persist)

    editorial_client = model_routing.client_for_agent(
        "editorial",
        settings=settings,
        items=article_results,
        model_override=brief_model,
    ).client
    article_results, _editorial_decisions = await apply_editorial_decisions(
        digest,
        article_results,
        model_client=editorial_client,
        reasoning_callback=flush_reasoning("editorial"),
        inference_run_id=inference_run_id,
        max_candidates=pipeline_limits["editorial_candidates"],
    )
    after_editorial = list(article_results)
    critic_client = model_routing.client_for_agent(
        "critic",
        settings=settings,
        items=article_results,
        model_override=brief_model,
    ).client
    article_results, _critic_decisions = await apply_critic_repairs(
        digest,
        payloads,
        article_results,
        model_client=critic_client,
        reasoning_callback=flush_reasoning("critic"),
        inference_run_id=inference_run_id,
        max_articles=pipeline_limits["critic_articles"],
        max_newsletter_records=pipeline_limits["critic_newsletter_records"],
    )
    after_critic = list(article_results)
    article_results, _quality_decisions = apply_brief_quality_checks(article_results)
    _set_pipeline_stage(progress, "review", "done")

    if librarian_client is not None:
        database.cache_model_enrichments(article_results, model_name=_model_client_name(librarian_client, settings.librarian_model))
    final_results = _enforce_inclusion_limits(profile, article_results)
    
    progress["_intermediates"] = {
        "enriched": enriched_articles,
        "ranked": ranked_articles,
        "after_audit": after_audit,
        "after_editorial": after_editorial,
        "after_critic": after_critic,
    }
    return final_results


def _enforce_inclusion_limits(profile: TopicProfile, results: list[ArticleFetchResult]) -> list[ArticleFetchResult]:
    per_source = profile.content_limits.get("per_source") if isinstance(profile.content_limits, dict) else {}
    counts: dict[str, int] = {}
    updated: list[ArticleFetchResult] = []
    for r in results:
        adapter = _adapter_from_payload_type(r.payload.source_type, r.payload.metadata)
        if not adapter:
            adapter = r.payload.source_type or "web_search"

        max_allowed = 20 if adapter in ("youtube", "podcasts") else (40 if adapter in ("markets", "web_search", "gmail", "foreign_media") else 25)
        limit = per_source.get(adapter, max_allowed) if isinstance(per_source, dict) else max_allowed
        limit = min(limit, max_allowed)

        if r.tier != "dropped":
            current = counts.get(adapter, 0)
            if current >= limit:
                updated.append(replace(r, tier="dropped"))
            else:
                counts[adapter] = current + 1
                updated.append(r)
        else:
            updated.append(r)
    return updated


async def _adjudicate_dates_before_source_window_filter(
    profile: TopicProfile,
    article_results: list[ArticleFetchResult],
    *,
    lookback_hours: int | None,
    inference_run_id: str,
    max_candidates: int | None,
) -> tuple[list[ArticleFetchResult], dict[str, Any]]:
    if lookback_hours is None:
        return article_results, {}
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))
    at_risk_indexes = _source_window_date_adjudication_indexes(profile, article_results, cutoff)
    if not at_risk_indexes:
        return article_results, {
            "status": "skipped",
            "candidate_count": 0,
            "message": "No articles needed AI date review before source-window filtering.",
        }

    limit = max(1, min(int(max_candidates or len(at_risk_indexes)), len(at_risk_indexes)))
    selected_indexes = at_risk_indexes[:limit]
    selected_results = [article_results[index] for index in selected_indexes]
    settings = get_settings()
    audit_client = model_routing.client_for_agent(
        "source_audit",
        settings=settings,
        items=selected_results,
        model_override=profile.models.get("brief"),
    ).client
    reviewed_results, _decisions, audit_summary = await apply_source_audit(
        profile,
        selected_results,
        lookback_hours=lookback_hours,
        model_client=audit_client,
        inference_run_id=inference_run_id,
        max_candidates=limit,
    )
    updated = list(article_results)
    resolved_count = 0
    for original_index, reviewed in zip(selected_indexes, reviewed_results, strict=False):
        if _article_published_at(reviewed) is not None and _article_published_at(article_results[original_index]) is None:
            resolved_count += 1
        updated[original_index] = reviewed

    status = str(audit_summary.get("status") or "completed")
    return updated, {
        "status": status,
        "candidate_count": len(selected_indexes),
        "at_risk_count": len(at_risk_indexes),
        "resolved_count": resolved_count,
        "excluded_count": int(audit_summary.get("excluded_count") or 0),
        "message": (
            "AI reviewed ambiguous dates before source-window filtering."
            if status not in {"failed", "fallback"}
            else "AI date review could not fully complete before source-window filtering."
        ),
        "summary": str(audit_summary.get("summary") or "").strip(),
        "issues": list(audit_summary.get("issues") or []),
    }


def _source_window_date_adjudication_indexes(
    profile: TopicProfile,
    article_results: list[ArticleFetchResult],
    cutoff: datetime,
) -> list[int]:
    indexes: list[int] = []
    for index, result in enumerate(article_results):
        if result.tier == "dropped" or not result.fetched:
            continue
        if _article_published_at(result) is not None:
            continue
        reason = _source_window_rejection_reason(profile, result, cutoff)
        if reason:
            indexes.append(index)
    return indexes


def _apply_source_window_filter(
    profile: TopicProfile,
    article_results: list[ArticleFetchResult],
    *,
    lookback_hours: int | None,
) -> tuple[list[ArticleFetchResult], list[dict[str, str]]]:
    if lookback_hours is None:
        return article_results, []
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))
    kept: list[ArticleFetchResult] = []
    issues: list[dict[str, str]] = []
    for result in article_results:
        reason = _source_window_rejection_reason(profile, result, cutoff)
        if reason:
            issues.append(
                {
                    "source_name": _source_window_issue_name(result),
                    "source": _source_label_for_result(result),
                    "item": _source_window_issue_name(result),
                    "item_url": str(result.final_url or result.original_url or result.payload.original_url or "").strip(),
                    "reason": reason,
                }
            )
            continue
        kept.append(_mark_undated_once(result) if _is_strict_undated_result(result) else result)
    return kept, issues


def _source_window_rejection_reason(profile: TopicProfile, result: ArticleFetchResult, cutoff: datetime) -> str:
    source_type = str(result.payload.source_type or "")

    # Check URL date hint first. URL dates are highly specific and indicate the original path publication date.
    # If the URL date hint is older than the cutoff, reject it even if metadata says it was updated recently.
    for value in (result.final_url, result.original_url, result.payload.original_url):
        url_date = _date_from_url(value)
        if url_date is not None and url_date < cutoff:
            return f"URL date hint places it outside the requested source window ({_format_window_cutoff(cutoff)} or newer required)."

    published = _article_published_at(result)
    if published is not None:
        if published < cutoff:
            return f"Published outside the requested source window ({_format_window_cutoff(cutoff)} or newer required)."
        return ""

    # If no published date, check general text date hints (which may be less precise, e.g. body text dates)
    hinted_date = _article_text_or_url_date(result)
    if hinted_date is not None:
        if hinted_date < cutoff:
            return f"Date hints place it outside the requested source window ({_format_window_cutoff(cutoff)} or newer required)."
        return ""

    if source_type in _STRICT_SOURCE_WINDOW_TYPES:
        item_key = _undated_item_key(result)
        if database.has_served_undated_item(profile.topic_id, item_key):
            return "Undated item was already shown once and is hidden from future editions."
        return "Date is missing for this strict source, so it is excluded under the bounded window."
    return ""


def _source_label_for_result(result: ArticleFetchResult) -> str:
    source_type = str(result.payload.source_type or "")
    if source_type == "gmail":
        return "Gmail"
    if source_type == "podcast_episode":
        return "Podcast"
    if source_type == "youtube_video":
        return "YouTube"
    if source_type == "market_snapshot":
        return "Markets"
    if source_type == "foreign_web":
        return "Foreign Media"
    return "Web"


def _is_strict_undated_result(result: ArticleFetchResult) -> bool:
    return (
        str(result.payload.source_type or "") in _STRICT_SOURCE_WINDOW_TYPES
        and _article_published_at(result) is None
        and _article_text_or_url_date(result) is None
    )


def _mark_undated_once(result: ArticleFetchResult) -> ArticleFetchResult:
    metadata = {
        **dict(result.metadata or {}),
        "date_status": "unknown",
        "served_once": True,
        "served_once_note": "Date unknown; shown once.",
        "served_once_key": _undated_item_key(result),
    }
    return replace(result, metadata=metadata)


def _served_undated_items_from_results(article_results: list[ArticleFetchResult]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for result in article_results:
        if result.tier == "dropped":
            continue
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        if metadata.get("served_once") is not True:
            continue
        items.append(
            {
                "item_key": str(metadata.get("served_once_key") or _undated_item_key(result)),
                "title": result.title,
                "source_name": result.payload.source_name,
                "url": _result_identity_url(result),
            }
        )
    return items


def _undated_item_key(result: ArticleFetchResult) -> str:
    identity = "|".join(
        part
        for part in (
            _result_identity_url(result),
            result.canonical_url or "",
            result.title,
            result.payload.source_name,
        )
        if part
    )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()


def _result_identity_url(result: ArticleFetchResult) -> str:
    return str(result.canonical_url or result.final_url or result.original_url or result.payload.original_url or "").strip()


def _article_published_at(result: ArticleFetchResult) -> datetime | None:
    values: list[Any] = [result.payload.published_at]
    for metadata in (result.payload.metadata, result.metadata):
        if not isinstance(metadata, dict):
            continue
        for key in _DATE_METADATA_KEYS:
            values.append(metadata.get(key))
    for value in values:
        parsed = _parse_datetime_hint(value)
        if parsed is not None:
            return parsed
    return None


def _article_text_or_url_date(result: ArticleFetchResult) -> datetime | None:
    for value in (result.final_url, result.original_url, result.payload.original_url):
        parsed = _date_from_url(value)
        if parsed is not None:
            return parsed
    text_sample = " ".join(
        part
        for part in (
            result.title,
            result.excerpt,
            result.editor_summary,
            result.payload.raw_text,
        )
        if part
    )
    return _date_from_text(text_sample[:4000])


def _parse_datetime_hint(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = _parse_datetime_string(text)
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_datetime_string(text: str) -> datetime | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    with suppress(ValueError):
        normalized = cleaned.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    with suppress(Exception):
        parsed = parsedate_to_datetime(cleaned)
        if parsed is not None:
            return parsed
    date_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", cleaned)
    if date_match:
        with suppress(ValueError):
            return datetime(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
                23,
                59,
                59,
                tzinfo=UTC,
            )
    for pattern in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        with suppress(ValueError):
            return datetime.strptime(cleaned, pattern).replace(hour=23, minute=59, second=59, tzinfo=UTC)
    return _date_from_text(cleaned)


def _date_from_url(value: str | None) -> datetime | None:
    text = str(value or "")
    match = _URL_DATE_RE.search(text)
    if not match:
        return None
    year = int(match.group("year"))
    month = int(match.group("month"))
    day = int(match.group("day") or 1)
    hour = 23 if match.group("day") else 0
    minute = 59 if match.group("day") else 0
    second = 59 if match.group("day") else 0
    with suppress(ValueError):
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    return None


def _date_from_text(value: str | None) -> datetime | None:
    text = str(value or "")
    match = _TEXT_DATE_RE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    with suppress(ValueError):
        return datetime(
            int(match.group("year")),
            month,
            int(match.group("day")),
            23,
            59,
            59,
            tzinfo=UTC,
        )
    return None


def _source_window_issue_name(result: ArticleFetchResult) -> str:
    title = (result.title or result.payload.source_name or result.original_url or "Source").strip()
    return title[:120]


def _format_window_cutoff(cutoff: datetime) -> str:
    return cutoff.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _newsletter_source_notes_for_brief(
    payloads: list[NormalizedPayload],
    article_results: list[ArticleFetchResult],
) -> list[NormalizedPayload]:
    kept_gmail_links = [
        result.payload
        for result in article_results
        if result.fetched and result.tier != "dropped" and result.payload.source_type in {"gmail", "gmail_link"}
    ]
    if not kept_gmail_links:
        return []

    kept_message_ids = {
        str(payload.metadata.get("gmail_message_id") or "").strip()
        for payload in kept_gmail_links
        if str(payload.metadata.get("gmail_message_id") or "").strip()
    }
    kept_sources = {
        str(payload.source_name or payload.metadata.get("sender_email") or "").strip().lower()
        for payload in kept_gmail_links
        if str(payload.source_name or payload.metadata.get("sender_email") or "").strip()
    }

    notes: list[NormalizedPayload] = []
    seen: set[str] = set()
    for payload in payloads:
        if payload.source_type != "gmail":
            continue
        message_id = str(payload.metadata.get("gmail_message_id") or "").strip()
        source_name = str(payload.source_name or payload.metadata.get("sender_email") or "").strip().lower()
        if message_id and message_id in kept_message_ids:
            key = f"message:{message_id}"
        elif source_name and source_name in kept_sources:
            key = f"source:{source_name}"
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        notes.append(payload)
    return notes


def _model_client_name(model_client: Any, fallback: str | None) -> str:
    config = getattr(model_client, "config", None)
    model = getattr(config, "model", None)
    return str(model or fallback or "unknown")


def _write_exploration_brief(exploration_id: str, html: str) -> str:
    settings = get_settings()
    ensure_runtime_dirs(settings)
    output_dir = settings.data_dir / "digest-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"exploration-{exploration_id}.html"
    path.write_text(html, encoding="utf-8")
    return str(path)


def _initial_progress(
    source_selection: dict[str, bool],
    source_names: list[str] | None = None,
) -> dict[str, Any]:
    if source_names is None:
        source_names = sorted(default_source_registry().names())
    progress_sources = {
        name: {"status": "disabled", "candidate_count": 0}
        if not bool(source_selection.get(name, False))
        else {"status": "pending", "candidate_count": 0}
        for name in source_names
    }
    return {
        "pipeline": {stage: "pending" for stage in _PIPELINE_STAGES},
        "sources": progress_sources,
        "candidate_count": 0,
        "exclusions": [],
    }


def _set_pipeline_stage(progress: dict[str, Any], stage: str, value: str) -> None:
    pipeline = dict(progress.get("pipeline", {}))
    pipeline[stage] = value
    progress["pipeline"] = pipeline


def _set_source_status(progress: dict[str, Any], adapter_status: Any) -> None:
    sources = dict(progress.get("sources", {}))
    sources[adapter_status.name] = {
        "status": adapter_status.status,
        "candidate_count": adapter_status.candidate_count,
        "message": adapter_status.message,
        "elapsed_ms": adapter_status.elapsed_ms,
        "timeout_seconds": adapter_status.timeout_seconds,
    }
    progress["sources"] = sources


def _init_reasoning_bucket(progress: dict[str, Any], stage: str, persist: Callable[[], None]) -> None:
    reasoning = dict(progress.get("reasoning", {}))
    if reasoning.get(stage) is None:
        reasoning[stage] = ""
        progress["reasoning"] = reasoning
        persist()


def _reasoning_flusher(progress: dict[str, Any], persist: Callable[[], None]) -> Callable[[str], Callable[[str], None]]:
    from time import monotonic

    state: dict[str, dict[str, Any]] = {
        "editorial": {"len": 0, "last_flush": monotonic()},
        "critic": {"len": 0, "last_flush": monotonic()},
    }

    def _make_callback(stage: str) -> Callable[[str], None]:
        def _callback(chunk: str) -> None:
            if not chunk:
                return
            reasoning = dict(progress.get("reasoning", {}))
            current = str(reasoning.get(stage, ""))
            current += chunk
            reasoning[stage] = current
            progress["reasoning"] = reasoning
            now = monotonic()
            state_info = state.get(stage)
            if state_info is None:
                state[stage] = {"len": len(current), "last_flush": now}
                persist()
                return
            if len(current) - int(state_info["len"]) >= 240 or now - float(state_info["last_flush"]) >= 0.35:
                state_info["len"] = len(current)
                state_info["last_flush"] = now
                persist()

        return _callback

    return _make_callback


def _set_candidate_count(progress: dict[str, Any], value: int) -> None:
    progress["candidate_count"] = value


def _set_exclusion_reasons(progress: dict[str, Any], reasons: Any) -> None:
    if not reasons:
        return
    progress["exclusions"] = list(reasons)


def _set_requested_source_issues(progress: dict[str, Any], issues: list[dict[str, str]]) -> None:
    progress["requested_source_issues"] = issues
    progress["built_with_issues"] = bool(issues)


def exploration_requested_source_issues(exploration: dict[str, Any]) -> list[dict[str, str]]:
    return exploration_build_issues(exploration, include_source_audit=False)


def exploration_build_issues(
    exploration: dict[str, Any],
    *,
    include_source_audit: bool = True,
) -> list[dict[str, str]]:
    progress = exploration.get("progress")
    if not isinstance(progress, dict):
        return []
    issues: list[dict[str, str]] = []
    requested = progress.get("requested_source_issues")
    if isinstance(requested, list):
        issues.extend(_normalize_issue_list(requested))
    if include_source_audit:
        audit = progress.get("source_audit_issues")
        if isinstance(audit, list):
            issues.extend(_normalize_issue_list(audit, action_required_only=True))
    return issues


def _normalize_issue_list(raw_issues: list[Any], *, action_required_only: bool = False) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for issue in raw_issues:
        if not isinstance(issue, dict):
            continue
        source_name = str(issue.get("source_name") or issue.get("ref") or "").strip()
        reason = str(issue.get("reason") or "").strip()
        if action_required_only and not _is_action_required_audit_issue(source_name, reason):
            continue
        if source_name and reason:
            issues.append({"source_name": source_name, "reason": reason})
    return issues


def _is_action_required_audit_issue(source_name: str, reason: str) -> bool:
    normalized_name = source_name.strip().lower()
    normalized = reason.strip().lower()
    return normalized_name in {"source audit", "ai review"} or normalized.startswith("audit could not complete")


def _requested_source_issues(
    profile: TopicProfile,
    discovery: DiscoveryResult,
    source_selection: dict[str, bool],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for requested in profile.requested_sources:
        adapter = str(requested.get("adapter") or "").strip()
        source_name = str(requested.get("ref") or requested.get("source_name") or "").strip()
        if not adapter or not source_name:
            continue
        if source_selection.get(adapter) is False:
            issues.append({
                "source_name": source_name,
                "reason": f"{_adapter_label(adapter)} was not selected for this brief.",
            })
            continue
        if _requested_source_found(adapter=adapter, source_name=source_name, discovery=discovery):
            continue
        status = next((item for item in discovery.statuses if item.name == adapter), None)
        if status and status.message:
            reason = status.message
        elif adapter == "markets":
            reason = "Markets price data was requested, but no usable ticker or price items were returned for this run."
        elif status and status.status in {"timed_out", "failed", "skipped"}:
            reason = f"{_adapter_label(adapter)} {status.status.replace('_', ' ')}."
        else:
            reason = "Source could not be found or returned no usable items."
        issues.append({"source_name": source_name, "reason": reason})
    return issues


def _build_source_issues(
    profile: TopicProfile,
    discovery: DiscoveryResult,
    source_selection: dict[str, bool],
) -> list[dict[str, str]]:
    issues = _requested_source_issues(profile, discovery, source_selection)
    statuses = {status.name: status for status in discovery.statuses}
    for source, enabled in source_selection.items():
        if not enabled or source in {"collections"}:
            continue
        status = statuses.get(source)
        if status is None:
            continue
        if status.candidate_count > 0:
            continue
        if status.status == "failed" and status.message:
            reason = status.message
        elif source == "markets":
            reason = "Markets price data returned no usable ticker items."

        elif source == "podcasts":
            reason = "Podcasts were selected but returned no usable episodes. Check show targets or broaden the query."
        elif source == "youtube":
            reason = "YouTube was selected but returned no videos with usable transcripts."
        elif source == "foreign_media":
            reason = "Foreign media was selected but no native-language results survived filtering."
        elif source == "web_search":
            reason = "Web search returned no usable results."
        elif source == "gmail":
            reason = "Gmail was selected but no approved newsletter items were usable."
        else:
            reason = f"{_adapter_label(source)} returned no usable items."
        issue = {"source_name": _adapter_label(source), "reason": reason}
        if issue not in issues:
            issues.append(issue)
    return issues


def _requested_source_found(*, adapter: str, source_name: str, discovery: DiscoveryResult) -> bool:
    needle = source_name.strip().lower()
    if not needle:
        return False
    for candidate in discovery.candidates:
        if candidate.adapter != adapter:
            continue
        payload = candidate.payload
        haystack_parts = [
            payload.source_name,
            payload.original_url,
            *[str(value) for value in payload.metadata.values() if isinstance(value, (str, int, float))],
        ]
        haystack = " ".join(str(value) for value in haystack_parts if value).lower()
        if needle in haystack or haystack in needle:
            return True
    return False


def _resolve_lookback_hours(profile: TopicProfile, lookback_hours: int | None) -> int | None:
    if lookback_hours is not None:
        return _bounded_lookback_hours(lookback_hours)
    if profile.lookback_hours is not None:
        return _bounded_lookback_hours(profile.lookback_hours)
    if profile.recency_weighting == "all_available":
        return None  # No temporal constraint — cast widest net, skip age filtering
    if profile.recency_weighting == "last_year":
        return 8760
    if profile.recency_weighting == "recent":
        return 72
    return 24


def _resolve_candidate_limit(profile: TopicProfile, candidate_limit: int | None) -> int:
    saved_limit = None
    if isinstance(profile.content_limits, dict):
        saved_limit = profile.content_limits.get("total_items")
    if saved_limit is not None:
        try:
            return max(1, min(int(saved_limit), 250))
        except (TypeError, ValueError):
            pass
    if candidate_limit is not None:
        return max(1, min(int(candidate_limit), 250))
    return 250


def _strengthen_profile_for_run(profile: TopicProfile) -> TopicProfile:
    """Repair older saved profiles with deterministic, source-agnostic facts from the statement."""
    detected = _exclusions_from_text(" ".join([profile.statement, profile.scope]))
    if not detected:
        return profile
    exclusions = tuple(_merge_terms(profile.exclusions, detected))
    if exclusions == profile.exclusions:
        return profile
    return replace(profile, exclusions=exclusions)


def _exclusions_from_text(text: str) -> list[str]:
    lowered = text.lower()
    exclusions: list[str] = []
    if "msn" in lowered:
        exclusions.append("MSN")
    if "yahoo" in lowered:
        exclusions.append("Yahoo News")
    if exclusions and re.search(r"\bnot\s+(?:like|from)\b", lowered):
        exclusions.append("syndicated aggregator reposts")
    return exclusions


def _merge_terms(existing: tuple[str, ...] | list[str], incoming: list[str] | tuple[str, ...], *, limit: int = 16) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        value = str(item or "").strip()
        key = value.lower()
        if not value or key in seen:
            continue
        merged.append(value)
        seen.add(key)
        if len(merged) >= limit:
            break
    return merged


def _bounded_lookback_hours(value: int) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return 24
    return min(8760, max(1, hours))


def _adapter_label(adapter: str) -> str:
    labels = {
        "web_search": "Web Search",
        "foreign_media": "Foreign Media",
        "gmail": "Gmail",
        "podcasts": "Podcasts",
        "youtube": "YouTube",
        "collections": "Collections",
        "markets": "Markets",
    }
    return labels.get(adapter, adapter.replace("_", " ").title())


def _brief_search_strategy(
    profile: TopicProfile,
    source_selection: dict[str, bool],
    lookback_hours: int | None,
) -> dict[str, Any]:
    profile = _strengthen_profile_for_run(profile)
    selected_sources = selected_source_labels(source_selection)
    queries = _strategy_queries(profile)
    exclusions = [item for item in profile.exclusions[:3] if item]
    scope = _source_scope_label(lookback_hours)
    return {
        "summary": summarize_search_strategy(
            statement=profile.scope or profile.statement,
            sources=selected_sources,
            source_scope=scope,
            exclusions=exclusions,
            keywords=list(profile.keywords),
        ),
        "queries": queries,
        "strategy_axes": _strategy_axes(profile),
        "sources": selected_sources,
        "source_scope": scope,
        "exclusions": exclusions,
    }


def _strategy_axes(profile: TopicProfile) -> list[str]:
    combined = " ".join(
        [
            profile.scope or profile.statement,
            *profile.keywords,
            *profile.subtopics,
            *profile.search_queries,
            *[query for queries in profile.source_queries.values() for query in queries],
        ]
    ).lower()
    axes: list[str] = []
    if any(term in combined for term in ("frontier ai", "frontier lab", "openai", "anthropic", "model developer", "scaling law")):
        axes.append("Frontier lab demand signals")
    if any(term in combined for term in ("hbm", "dram", "nand", "memory")):
        axes.append("Memory and storage supply chain")
    if any(term in combined for term in ("capex", "capital expenditure", "spending")):
        axes.append("AI infrastructure spending and CapEx")
    return axes[:5]


def _strategy_queries(profile: TopicProfile) -> list[str]:
    queries: list[str] = []
    for query in profile.search_queries:
        if query and query not in queries:
            queries.append(query)
        if len(queries) >= 3:
            return queries
    for adapter_queries in profile.source_queries.values():
        for query in adapter_queries:
            if query and query not in queries:
                queries.append(query)
            if len(queries) >= 3:
                return queries
    if not queries and profile.keywords:
        queries.append(" ".join(profile.keywords[:8]))
    return queries[:3]


def _source_scope_label(lookback_hours: int | None) -> str:
    if lookback_hours is None:
        return "all available"
    try:
        hours = max(1, int(lookback_hours))
    except (TypeError, ValueError):
        hours = 24
    if hours % 24 == 0:
        days = hours // 24
        return "last 24 hours" if days == 1 else f"last {days} days"
    return "last hour" if hours == 1 else f"last {hours} hours"


def _persist_progress(exploration_id: str, progress: dict[str, Any]) -> None:
    database.update_exploration_progress(
        exploration_id,
        progress=dict(progress),
    )


def _elapsed_stage_seconds(started_at: float) -> float:
    return round(max(0.001, monotonic() - started_at), 3)


def _brief_title(profile: TopicProfile) -> str:
    scope = profile.scope or profile.statement or "Explore"
    return tight_brief_title(scope, keywords=profile.keywords)


def _selected_source_count(discovery: DiscoveryResult, source_selection: dict[str, bool]) -> int:
    discovered_names = {status.name for status in discovery.statuses}
    selected = [name for name, enabled in source_selection.items() if enabled and name in discovered_names]
    return len(selected)


def _merged_source_selection(
    profile: TopicProfile,
    source_selection: dict[str, bool] | None,
) -> dict[str, bool]:
    return {**profile.source_selection, **(source_selection or {})}


async def re_enrich_deterministic_articles(exploration_id: str) -> dict[str, Any] | None:
    import sqlite3
    exploration = database.get_exploration(exploration_id)
    if exploration is None or exploration.get("deleted_at"):
        return None

    # Load topic profile
    topic_id = str(exploration["topic_id"])
    topic = database.get_topic_profile(topic_id)
    if topic is None:
        return None
    profile = TopicProfile.from_dict(topic["profile"])

    # Load HTML brief
    html = read_brief_html(exploration_id)
    if html is None:
        return None

    # Extract all article URLs from the brief HTML
    urls = set(re.findall(r'href="(https?://[^"#\s]+)"', html))
    if not urls:
        return {"status": "success", "message": "No article URLs found in brief"}

    # Query articles from DB
    article_results = []
    settings = get_settings()

    # Get model client
    from backend.agents.librarian.enrichment import enrich_article_with_model
    temp_payload = NormalizedPayload(
        source_type="web_search",
        source_name="Web",
        original_url="http://example.com",
    )
    temp_result = ArticleFetchResult(
        payload=temp_payload,
        original_url="http://example.com",
        final_url="http://example.com",
        canonical_url="http://example.com",
        title="Temp",
        text="",
        excerpt="",
        domain="example.com",
        status="fetched",
        keywords=(),
    )
    brief_model = profile.models.get("brief")
    librarian_resolution = model_routing.client_for_agent(
        "librarian",
        settings=settings,
        items=[temp_result],
        model_override=brief_model,
    )
    librarian_client = librarian_resolution.client
    model_name = _model_client_name(librarian_client, settings.librarian_model)

    re_enriched_count = 0

    with database.connect() as connection:
        connection.row_factory = sqlite3.Row
        for url in urls:
            row = connection.execute(
                "SELECT * FROM articles WHERE canonical_url = ? OR original_url = ?",
                (url, url)
            ).fetchone()
            if not row:
                continue

            # Hydrate ArticleFetchResult
            metadata = {}
            if row["metadata"]:
                try:
                    metadata = json.loads(row["metadata"])
                except Exception:
                    pass
            metadata["article_id"] = row["id"]

            payload = NormalizedPayload(
                source_type=row["content_type"] or "web_search",
                source_name=row["publisher"] or "Web",
                original_url=row["original_url"] or row["canonical_url"],
                published_at=row["published_at"],
                metadata=metadata,
            )
            result = ArticleFetchResult(
                payload=payload,
                original_url=row["original_url"] or row["canonical_url"],
                final_url=row["canonical_url"],
                canonical_url=row["canonical_url"],
                title=row["title"],
                text=row["cleaned_text"] or "",
                excerpt=row["summary"] or "",
                domain=row["domain"],
                status=row["fetch_status"] or "fetched",
                keywords=tuple(json.loads(row["keywords"]) if row["keywords"] else []),
                content_type=row["content_type"] or "article",
            )

            # Check if this article has a model cache entry
            cache_row = connection.execute(
                "SELECT * FROM model_enrichment_cache WHERE canonical_url = ? AND model_name = ?",
                (result.canonical_url, model_name)
            ).fetchone()

            # If no cache entry exists, it means it fell back to deterministic summarization
            # Rerun model enrichment
            if not cache_row and librarian_client is not None and result.text:
                try:
                    enriched = await enrich_article_with_model(result, model_client=librarian_client)
                    if enriched.enrichment_source in {"model", "model_fallback"}:
                        # Update article summary/keywords in DB
                        connection.execute(
                            """
                            UPDATE articles
                            SET summary = ?, keywords = ?, content_type = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                enriched.editor_summary or enriched.excerpt,
                                json.dumps(list(enriched.keywords)),
                                enriched.content_type,
                                database.utc_now(),
                                row["id"]
                            )
                        )
                        # Cache the model enrichment
                        database.cache_model_enrichments(
                            [enriched],
                            model_name=model_name
                        )
                        result = enriched
                        re_enriched_count += 1
                except Exception as exc:
                    logger.warning("Failed to re-enrich %s: %s", url, exc)

            article_results.append(result)

    # Re-render HTML brief if any articles were enriched
    if re_enriched_count > 0:
        payloads = [result.payload for result in article_results]
        lookback_hours = _resolve_lookback_hours(profile, exploration.get("source_selection").get("lookback_hours") if (exploration.get("source_selection") and isinstance(exploration.get("source_selection"), dict)) else None)
        configured_source_count = len(exploration.get("source_selection") or {})
        # Generate stats and render
        snapshot = database.ingested_snapshot(payloads, configured_source_count, article_results)
        newsletter_source_notes = _newsletter_source_notes_for_brief(payloads, article_results)

        # Build stats (use a dummy start time or exploration started_at)
        try:
            started_dt = datetime.fromisoformat(exploration["started_at"])
            duration = (datetime.now(UTC) - started_dt).total_seconds()
        except Exception:
            duration = 10.0

        digest_stats = database.build_digest_stats(
            configured_source_count=configured_source_count,
            newsletter_count=len(newsletter_source_notes),
            link_count=sum(1 for payload in payloads if payload.source_type == "gmail_link"),
            podcast_episode_count=sum(1 for payload in payloads if payload.source_type == "podcast_episode"),
            article_results=article_results,
            duration_seconds=duration,
            inference_run_id=exploration_id,
            stage_seconds={"re_enrich": 1.0},
        )
        if exploration.get("source_selection"):
            digest_stats["search_strategy"] = _brief_search_strategy(profile, exploration.get("source_selection"), lookback_hours)

        html = database.render_ingested_issue(
            title=_brief_title(profile),
            snapshot=snapshot,
            payloads=payloads,
            article_results=article_results,
            lookback_hours=lookback_hours,
            generated_at=database.utc_now(),
            issue_id=exploration_id,
            digest_stats=digest_stats,
            newsletter_payloads=newsletter_source_notes,
        )
        _write_exploration_brief(exploration_id, html)

        # Update progress snapshot / stats in DB
        progress = dict(exploration["progress"] or {})
        progress["brief"] = {
            "title": _brief_title(profile),
            "html_path": f"/api/explore/explorations/{exploration_id}/brief/html",
            "snapshot": snapshot,
            "stats": digest_stats,
            "candidate_count": len(payloads),
        }
        database.update_exploration_progress(exploration_id, progress=progress)

    return database.get_exploration(exploration_id)
