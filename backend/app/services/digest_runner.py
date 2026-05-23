from __future__ import annotations

from time import monotonic
from typing import Any

from backend.agents.critic import apply_critic_repairs
from backend.agents.brief_quality import apply_brief_quality_checks
from backend.agents.digestor.gmail_mcp_client import fetch_newsletters
from backend.agents.digestor.reddit import fetch_reddit_threads
from backend.agents.editorial_decisions import apply_editorial_decisions
from backend.agents.editor import prepare_issue_articles
from backend.agents.librarian.articles import fetch_articles_for_payloads
from backend.agents.librarian.enrichment import enrich_articles, refine_ranked_articles_with_model
from backend.app.core.config import get_settings
from backend.app.db import database

LOOKBACK_HOURS_BY_SCHEDULE = {
    "hourly": 1,
    "daily": 24,
    "weekly": 168,
    "monthly": 720,
}


async def run_digest(digest_id: str, *, trigger: str = "manual") -> dict[str, Any] | None:
    started_at = monotonic()
    stage_started = started_at
    stage_seconds: dict[str, float] = {}
    digest = database.get_digest(digest_id)
    if digest is None:
        return None
    inference_run_id = database.new_id()

    sender_allowlist = gmail_sender_allowlist(digest.get("sources", []))
    lookback_hours = LOOKBACK_HOURS_BY_SCHEDULE.get(str(digest.get("schedule", "daily")), 24)

    payloads = []
    if sender_allowlist:
        payloads = await fetch_newsletters(
            digest_id=digest_id,
            sender_allowlist=sender_allowlist,
            lookback_hours=lookback_hours,
            db_path=str(database.database_path()),
        )
    reddit_payloads = await fetch_reddit_threads(
        digest_id=digest_id,
        digest_interest=str(digest.get("interest") or ""),
        lookback_hours=lookback_hours,
    )
    payloads.extend(reddit_payloads)
    stage_started = _mark_stage(stage_seconds, "ingestion", stage_started)
    fetched_articles = await fetch_articles_for_payloads(payloads)
    stage_started = _mark_stage(stage_seconds, "fetching", stage_started)
    enriched_articles = await enrich_articles(fetched_articles, model_max_items=0)
    enriched_articles = database.apply_feedback_to_candidates(digest_id, enriched_articles)
    ranked_articles = prepare_issue_articles(digest, enriched_articles)
    settings = get_settings()
    model_cache_hit_count = 0
    model_cache_miss_count = 0
    model_cache_write_count = 0
    if settings.librarian_use_model:
        model_cache_candidate_count = _model_cache_candidate_count(ranked_articles, settings.librarian_model_max_items)
        ranked_articles = database.apply_cached_model_enrichments(
            ranked_articles,
            model_name=settings.librarian_model,
            limit=settings.librarian_model_max_items,
        )
        model_cache_hit_count = sum(1 for result in ranked_articles if result.enrichment_source == "model_cache")
        model_cache_miss_count = max(0, model_cache_candidate_count - model_cache_hit_count)
    article_results = await refine_ranked_articles_with_model(
        ranked_articles,
        inference_run_id=inference_run_id,
        metrics_mode="batch" if trigger == "scheduled" else "single",
    )
    stage_started = _mark_stage(stage_seconds, "classification", stage_started)
    editorial_decisions = []
    critic_decisions = []
    article_results, editorial_decisions = await apply_editorial_decisions(digest, article_results)
    article_results, critic_decisions = await apply_critic_repairs(digest, payloads, article_results)
    article_results, quality_decisions = apply_brief_quality_checks(article_results)
    stage_started = _mark_stage(stage_seconds, "editorial", stage_started)
    if settings.librarian_use_model:
        model_cache_write_count = database.cache_model_enrichments(article_results, model_name=settings.librarian_model)

    configured_source_count = len(sender_allowlist) + len(
        database.list_reddit_sources(digest_id, include_retired=False)
    )

    stage_seconds["publishing"] = 0.0
    return database.create_ingested_run(
        digest=digest,
        payloads=payloads,
        article_results=article_results,
        lookback_hours=lookback_hours,
        configured_source_count=configured_source_count,
        trigger=trigger,
        duration_seconds=round(monotonic() - started_at, 3),
        model_cache_hit_count=model_cache_hit_count,
        model_cache_miss_count=model_cache_miss_count,
        model_cache_write_count=model_cache_write_count,
        inference_run_id=inference_run_id,
        stage_seconds=stage_seconds,
        agent_decisions=editorial_decisions + critic_decisions + quality_decisions,
    )


def _model_cache_candidate_count(results: list[Any], limit: int) -> int:
    if limit <= 0:
        return 0
    return sum(
        1
        for result in results[:limit]
        if getattr(result, "fetched", False) and getattr(result, "tier", None) != "dropped"
    )


def _mark_stage(stage_seconds: dict[str, float], name: str, started_at: float) -> float:
    now = monotonic()
    stage_seconds[name] = round(now - started_at, 3)
    return now


def gmail_sender_allowlist(sources: list[dict[str, Any]]) -> list[str]:
    senders: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if source.get("type") not in {"gmail", "gmail_newsletter"}:
            continue
        sender = str(source.get("sender", "")).strip().lower()
        if not sender or sender in seen:
            continue
        senders.append(sender)
        seen.add(sender)
    return senders
