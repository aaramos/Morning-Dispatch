from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from time import monotonic
from typing import Any

from backend.agents.agentic import AgentDecision
from backend.agents.brief_quality import apply_brief_quality_checks
from backend.agents.critic import apply_critic_repairs
from backend.agents.editorial_decisions import apply_editorial_decisions
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.db import database


async def run_controlled_verification(digest_id: str, *, publish: bool = False) -> dict[str, Any] | None:
    started_at = monotonic()
    digest = database.get_digest(digest_id)
    if digest is None:
        return None

    latest_run = database.get_latest_source_run_for_digest(digest_id) if publish else database.get_latest_run_for_digest(digest_id)
    if latest_run is None:
        return {
            "status": "no_source_run",
            "digest_id": digest_id,
            "message": "No completed digest run exists to verify yet.",
        }

    source_run_id = str(latest_run["id"])
    source_articles = database.list_article_results_for_run(source_run_id)
    source_payloads = database.list_newsletter_payloads_for_run(source_run_id)
    if not source_articles:
        return {
            "status": "no_articles",
            "digest_id": digest_id,
            "source_run_id": source_run_id,
            "message": "The latest run has no stored article candidates to verify.",
        }

    reused_decision_records = database.list_latest_agent_decisions_for_run(source_run_id) if publish else []
    if reused_decision_records:
        after_critic = _apply_stored_decisions(source_articles, reused_decision_records)
        decisions = _records_to_decisions(reused_decision_records)
    else:
        after_editorial, editorial_decisions = await apply_editorial_decisions(digest, source_articles)
        after_critic, critic_decisions = await apply_critic_repairs(digest, source_payloads, after_editorial)
        decisions = editorial_decisions + critic_decisions
    after_quality, quality_decisions = apply_brief_quality_checks(after_critic)
    decisions = decisions + quality_decisions
    published_run = None
    published_issue = None
    if publish:
        source_stats = _run_digest_stats(latest_run)
        stage_seconds = {
            "editorial": round(monotonic() - started_at, 3),
            "publishing": 0.0,
        }
        published_run = database.create_ingested_run(
            digest=digest,
            payloads=source_payloads,
            article_results=after_quality,
            lookback_hours=max(24, int(latest_run.get("lookback_days") or 1) * 24),
            configured_source_count=len(digest.get("sources", [])),
            trigger="controlled_verification",
            duration_seconds=round(monotonic() - started_at, 3),
            model_cache_hit_count=int(latest_run.get("model_cache_hit_count") or 0),
            model_cache_miss_count=int(latest_run.get("model_cache_miss_count") or 0),
            model_cache_write_count=0,
            inference_run_id=str(latest_run.get("inference_run_id") or database.new_id()),
            stage_seconds=stage_seconds,
            stats_overrides={
                "source_count": int(source_stats.get("source_count") or len(digest.get("sources", []))),
                "newsletter_count": int(source_stats.get("newsletter_count") or latest_run.get("newsletter_count") or len(source_payloads)),
                "link_count": int(source_stats.get("link_count") or latest_run.get("link_count") or 0),
                "podcast_episode_count": int(source_stats.get("podcast_episode_count") or _podcast_count(source_articles)),
                "processing_seconds": latest_run.get("duration_seconds"),
            },
            agent_decisions=decisions,
        )
        published_issue = database.get_latest_issue(digest_id)
        stored_count = len(decisions)
    else:
        stored_count = database.add_agent_decisions_for_run(
            run_id=source_run_id,
            digest_id=digest_id,
            inference_run_id=latest_run.get("inference_run_id"),
            decisions=decisions,
        )

    return {
        "status": "completed",
        "mode": "controlled_verification",
        "published": publish,
        "digest_id": digest_id,
        "source_run_id": source_run_id,
        "published_run_id": published_run.get("id") if published_run else None,
        "published_issue_id": published_issue.get("id") if published_issue else None,
        "reviewed_article_count": len(source_articles),
        "active_before_count": _active_count(source_articles),
        "active_after_count": _active_count(after_quality),
        "dropped_count": sum(1 for result in after_quality if result.tier == "dropped"),
        "lead_title": _lead_title(after_quality),
        "decision_count": len(decisions),
        "stored_decision_count": stored_count,
        "reused_verified_decisions": bool(reused_decision_records),
        "action_counts": dict(Counter(decision.action for decision in decisions)),
        "agent_counts": dict(Counter(decision.agent for decision in decisions)),
    }


def _active_count(results: list[ArticleFetchResult]) -> int:
    return sum(1 for result in results if result.tier != "dropped")


def _podcast_count(results: list[ArticleFetchResult]) -> int:
    return sum(1 for result in results if result.payload.source_type == "podcast_episode")


def _run_digest_stats(run: dict[str, Any]) -> dict[str, Any]:
    try:
        metadata = json.loads(str(run.get("run_metadata") or "{}"))
    except json.JSONDecodeError:
        return {}
    stats = metadata.get("digest_stats") if isinstance(metadata, dict) else {}
    return stats if isinstance(stats, dict) else {}


def _lead_title(results: list[ArticleFetchResult]) -> str | None:
    lead = next((result for result in results if result.tier == "lead"), None)
    return lead.title if lead else None


def _apply_stored_decisions(
    articles: list[ArticleFetchResult],
    records: list[dict[str, Any]],
) -> list[ArticleFetchResult]:
    updated = list(articles)
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        index = _record_index(metadata)
        if index is None or not (0 <= index < len(updated)):
            continue
        action = str(record.get("action") or "")
        result = updated[index]
        if action in {"drop", "drop_article"}:
            updated[index] = replace(result, tier="dropped")
        elif action in {"demote", "demote_article"}:
            updated[index] = replace(result, tier="lower_confidence")
        elif action == "include":
            updated[index] = replace(result, tier="main" if result.fetched else "lower_confidence")
        elif action in {"candidate_lead", "replace_lead"} and result.fetched:
            updated = _set_lead(updated, index)
    return _normalize_lead(updated)


def _records_to_decisions(records: list[dict[str, Any]]) -> list[AgentDecision]:
    decisions: list[AgentDecision] = []
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        decisions.append(
            AgentDecision(
                agent=str(record.get("agent") or "agent"),
                target=str(record.get("target") or "issue"),
                decision=str(record.get("decision") or "verified"),
                action=str(record.get("action") or "none"),
                confidence=_optional_float(record.get("confidence")),
                reason=str(record.get("reason") or ""),
                model_name=record.get("model_name"),
                metadata={**metadata, "reused_for_publish": True},
            )
        )
    return decisions


def _record_index(metadata: dict[str, Any]) -> int | None:
    raw_value = metadata.get("index")
    if raw_value is None:
        raw_value = metadata.get("target_index")
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_lead(results: list[ArticleFetchResult]) -> list[ArticleFetchResult]:
    lead_index = next((index for index, result in enumerate(results) if result.tier == "lead" and result.fetched), None)
    if lead_index is None:
        lead_index = next((index for index, result in enumerate(results) if result.tier != "dropped" and result.fetched), None)
    return _set_lead(results, lead_index) if lead_index is not None else results


def _set_lead(results: list[ArticleFetchResult], lead_index: int) -> list[ArticleFetchResult]:
    updated: list[ArticleFetchResult] = []
    for index, result in enumerate(results):
        if result.tier == "dropped":
            updated.append(result)
        elif index == lead_index:
            updated.append(replace(result, tier="lead"))
        elif result.tier == "lead":
            updated.append(replace(result, tier="main"))
        else:
            updated.append(result)
    return updated
