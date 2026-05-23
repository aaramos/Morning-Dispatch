from __future__ import annotations

import asyncio
from operator import add
from time import monotonic
from typing import Annotated, Any, TypedDict

from backend.agents.critic import apply_critic_repairs
from backend.agents.brief_quality import apply_brief_quality_checks
from backend.agents.agentic import AgentDecision
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.digestor.gmail_mcp_client import fetch_newsletters
from backend.agents.digestor.podcast import fetch_podcast_episodes
from backend.agents.digestor.reddit import fetch_reddit_threads
from backend.agents.editorial_decisions import apply_editorial_decisions
from backend.agents.editor import prepare_issue_articles
from backend.agents.librarian.articles import ArticleFetchResult, fetch_articles_for_payloads
from backend.agents.librarian.enrichment import enrich_articles, refine_ranked_articles_with_model
from backend.app.core.config import get_settings
from backend.app.db import database
from langgraph.graph import END, START, StateGraph

LOOKBACK_HOURS_BY_SCHEDULE = {
    "hourly": 1,
    "daily": 24,
    "weekly": 168,
    "monthly": 720,
}


class DigestGraphState(TypedDict, total=False):
    digest_id: str
    trigger: str
    started_at: float
    stage_started: float
    stage_seconds: dict[str, float]
    digest: dict[str, Any]
    inference_run_id: str
    sender_allowlist: list[str]
    lookback_hours: int
    gmail_payloads: Annotated[list[NormalizedPayload], add]
    reddit_payloads: Annotated[list[NormalizedPayload], add]
    podcast_payloads: Annotated[list[NormalizedPayload], add]
    payloads: list[NormalizedPayload]
    podcast_decisions: Annotated[list[AgentDecision], add]
    editorial_decisions: list[AgentDecision]
    critic_decisions: list[AgentDecision]
    quality_decisions: list[AgentDecision]
    fetched_articles: list[ArticleFetchResult]
    enriched_articles: list[ArticleFetchResult]
    ranked_articles: list[ArticleFetchResult]
    article_results: list[ArticleFetchResult]
    model_cache_hit_count: int
    model_cache_miss_count: int
    model_cache_write_count: int
    run: dict[str, Any]


async def run_digest(digest_id: str, *, trigger: str = "manual") -> dict[str, Any] | None:
    started_at = monotonic()
    digest = database.get_digest(digest_id)
    if digest is None:
        return None

    state = await _digest_graph().ainvoke(
        DigestGraphState(
            digest_id=digest_id,
            trigger=trigger,
            started_at=started_at,
            stage_started=started_at,
            stage_seconds={},
            digest=digest,
            inference_run_id=database.new_id(),
            sender_allowlist=gmail_sender_allowlist(digest.get("sources", [])),
            lookback_hours=LOOKBACK_HOURS_BY_SCHEDULE.get(str(digest.get("schedule", "daily")), 24),
            model_cache_hit_count=0,
            model_cache_miss_count=0,
            model_cache_write_count=0,
        )
    )
    return state.get("run")


def _digest_graph() -> Any:
    graph = StateGraph(DigestGraphState)
    graph.add_node("ingest_sources", _ingest_sources)
    graph.add_node("fetch_articles", _fetch_articles)
    graph.add_node("rank_articles", _rank_articles)
    graph.add_node("refine_with_model", _refine_with_model)
    graph.add_node("review_quality", _review_quality)
    graph.add_node("publish_run", _publish_run)

    graph.add_edge(START, "ingest_sources")
    graph.add_edge("ingest_sources", "fetch_articles")
    graph.add_edge("fetch_articles", "rank_articles")
    graph.add_edge("rank_articles", "refine_with_model")
    graph.add_edge("refine_with_model", "review_quality")
    graph.add_edge("review_quality", "publish_run")
    graph.add_edge("publish_run", END)
    return graph.compile()


async def _ingest_sources(state: DigestGraphState) -> DigestGraphState:
    gmail_result, reddit_result, podcast_result = await asyncio.gather(
        _ingest_gmail(state),
        _ingest_reddit(state),
        _ingest_podcast(state),
    )
    gmail_payloads = gmail_result.get("gmail_payloads", [])
    reddit_payloads = reddit_result.get("reddit_payloads", [])
    podcast_payloads = podcast_result.get("podcast_payloads", [])
    payloads = [*gmail_payloads, *reddit_payloads, *podcast_payloads]

    stage_seconds = dict(state.get("stage_seconds", {}))
    stage_started = _mark_stage(stage_seconds, "ingestion", state["stage_started"])
    return {
        "gmail_payloads": gmail_payloads,
        "reddit_payloads": reddit_payloads,
        "podcast_payloads": podcast_payloads,
        "podcast_decisions": podcast_result.get("podcast_decisions", []),
        "payloads": payloads,
        "stage_seconds": stage_seconds,
        "stage_started": stage_started,
    }


async def _ingest_gmail(state: DigestGraphState) -> DigestGraphState:
    sender_allowlist = state.get("sender_allowlist", [])
    if not sender_allowlist:
        return {"gmail_payloads": []}
    payloads = await fetch_newsletters(
        digest_id=state["digest_id"],
        sender_allowlist=sender_allowlist,
        lookback_hours=state["lookback_hours"],
        db_path=str(database.database_path()),
    )
    return {"gmail_payloads": payloads}


async def _ingest_reddit(state: DigestGraphState) -> DigestGraphState:
    digest = state["digest"]
    payloads = await fetch_reddit_threads(
        digest_id=state["digest_id"],
        digest_interest=str(digest.get("interest") or ""),
        lookback_hours=state["lookback_hours"],
    )
    return {"reddit_payloads": payloads}


async def _ingest_podcast(state: DigestGraphState) -> DigestGraphState:
    digest = state["digest"]
    payloads, decisions = await fetch_podcast_episodes(
        digest_id=state["digest_id"],
        digest_interest=str(digest.get("interest") or ""),
        sources=digest.get("sources", []),
        lookback_hours=state["lookback_hours"],
        inference_run_id=state["inference_run_id"],
    )
    return {"podcast_payloads": payloads, "podcast_decisions": decisions}


async def _fetch_articles(state: DigestGraphState) -> DigestGraphState:
    fetched_articles = await fetch_articles_for_payloads(state.get("payloads", []))
    stage_seconds = dict(state.get("stage_seconds", {}))
    stage_started = _mark_stage(stage_seconds, "fetching", state["stage_started"])
    return {
        "fetched_articles": fetched_articles,
        "stage_seconds": stage_seconds,
        "stage_started": stage_started,
    }


async def _rank_articles(state: DigestGraphState) -> DigestGraphState:
    digest = state["digest"]
    enriched_articles = await enrich_articles(state.get("fetched_articles", []), model_max_items=0)
    enriched_articles = database.apply_feedback_to_candidates(state["digest_id"], enriched_articles)
    ranked_articles = prepare_issue_articles(digest, enriched_articles)

    settings = get_settings()
    cache_hit_count = 0
    cache_miss_count = 0
    if settings.librarian_use_model:
        candidate_count = _model_cache_candidate_count(ranked_articles, settings.librarian_model_max_items)
        ranked_articles = database.apply_cached_model_enrichments(
            ranked_articles,
            model_name=settings.librarian_model,
            limit=settings.librarian_model_max_items,
        )
        cache_hit_count = sum(1 for result in ranked_articles if result.enrichment_source == "model_cache")
        cache_miss_count = max(0, candidate_count - cache_hit_count)

    return {
        "enriched_articles": enriched_articles,
        "ranked_articles": ranked_articles,
        "model_cache_hit_count": cache_hit_count,
        "model_cache_miss_count": cache_miss_count,
    }


async def _refine_with_model(state: DigestGraphState) -> DigestGraphState:
    article_results = await refine_ranked_articles_with_model(
        state.get("ranked_articles", []),
        inference_run_id=state["inference_run_id"],
        metrics_mode="batch" if state.get("trigger") == "scheduled" else "single",
    )
    stage_seconds = dict(state.get("stage_seconds", {}))
    stage_started = _mark_stage(stage_seconds, "classification", state["stage_started"])
    return {
        "article_results": article_results,
        "stage_seconds": stage_seconds,
        "stage_started": stage_started,
    }


async def _review_quality(state: DigestGraphState) -> DigestGraphState:
    digest = state["digest"]
    article_results, editorial_decisions = await apply_editorial_decisions(digest, state.get("article_results", []))
    article_results, critic_decisions = await apply_critic_repairs(digest, state.get("payloads", []), article_results)
    article_results, quality_decisions = apply_brief_quality_checks(article_results)
    stage_seconds = dict(state.get("stage_seconds", {}))
    stage_started = _mark_stage(stage_seconds, "editorial", state["stage_started"])

    cache_write_count = 0
    settings = get_settings()
    if settings.librarian_use_model:
        cache_write_count = database.cache_model_enrichments(article_results, model_name=settings.librarian_model)

    return {
        "article_results": article_results,
        "editorial_decisions": editorial_decisions,
        "critic_decisions": critic_decisions,
        "quality_decisions": quality_decisions,
        "model_cache_write_count": cache_write_count,
        "stage_seconds": stage_seconds,
        "stage_started": stage_started,
    }


async def _publish_run(state: DigestGraphState) -> DigestGraphState:
    digest = state["digest"]
    sender_allowlist = gmail_sender_allowlist(digest.get("sources", []))
    configured_source_count = (
        len(sender_allowlist)
        + len(database.list_reddit_sources(state["digest_id"], include_retired=False))
        + len(podcast_sources(digest.get("sources", [])))
    )

    stage_seconds = dict(state.get("stage_seconds", {}))
    stage_seconds["publishing"] = 0.0
    run = database.create_ingested_run(
        digest=digest,
        payloads=state.get("payloads", []),
        article_results=state.get("article_results", []),
        lookback_hours=state["lookback_hours"],
        configured_source_count=configured_source_count,
        trigger=state.get("trigger", "manual"),
        duration_seconds=round(monotonic() - state["started_at"], 3),
        model_cache_hit_count=state.get("model_cache_hit_count", 0),
        model_cache_miss_count=state.get("model_cache_miss_count", 0),
        model_cache_write_count=state.get("model_cache_write_count", 0),
        inference_run_id=state["inference_run_id"],
        stage_seconds=stage_seconds,
        agent_decisions=(
            state.get("podcast_decisions", [])
            + state.get("editorial_decisions", [])
            + state.get("critic_decisions", [])
            + state.get("quality_decisions", [])
        ),
    )
    return {"run": run, "stage_seconds": stage_seconds}


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


def podcast_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        source
        for source in sources
        if source.get("type") in {"podcast_rss", "podcast_search"}
    ]
