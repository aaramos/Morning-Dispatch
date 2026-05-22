from __future__ import annotations

from collections import Counter
from typing import Any

from backend.agents.critic import apply_critic_repairs
from backend.agents.editorial_decisions import apply_editorial_decisions
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.db import database


async def run_controlled_verification(digest_id: str) -> dict[str, Any] | None:
    digest = database.get_digest(digest_id)
    if digest is None:
        return None

    latest_run = database.get_latest_run_for_digest(digest_id)
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

    after_editorial, editorial_decisions = await apply_editorial_decisions(digest, source_articles)
    after_critic, critic_decisions = await apply_critic_repairs(digest, source_payloads, after_editorial)
    decisions = editorial_decisions + critic_decisions
    stored_count = database.add_agent_decisions_for_run(
        run_id=source_run_id,
        digest_id=digest_id,
        inference_run_id=latest_run.get("inference_run_id"),
        decisions=decisions,
    )

    return {
        "status": "completed",
        "mode": "controlled_verification",
        "published": False,
        "digest_id": digest_id,
        "source_run_id": source_run_id,
        "reviewed_article_count": len(source_articles),
        "active_before_count": _active_count(source_articles),
        "active_after_count": _active_count(after_critic),
        "dropped_count": sum(1 for result in after_critic if result.tier == "dropped"),
        "lead_title": _lead_title(after_critic),
        "decision_count": len(decisions),
        "stored_decision_count": stored_count,
        "action_counts": dict(Counter(decision.action for decision in decisions)),
        "agent_counts": dict(Counter(decision.agent for decision in decisions)),
    }


def _active_count(results: list[ArticleFetchResult]) -> int:
    return sum(1 for result in results if result.tier != "dropped")


def _lead_title(results: list[ArticleFetchResult]) -> str | None:
    lead = next((result for result in results if result.tier == "lead"), None)
    return lead.title if lead else None
