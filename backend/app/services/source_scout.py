from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Any

from backend.agents import source_scout as scout_agent
from backend.agents.digestor import reddit_mcp_client
from backend.app.db import database

logger = logging.getLogger(__name__)

MAX_SAMPLED_SOURCES = 18
MAX_DISCOVERY_QUERIES = 8


async def run_source_scout(digest_id: str, *, live_sample: bool = True) -> dict[str, Any] | None:
    digest = database.get_digest(digest_id)
    if digest is None:
        return None

    database.seed_reddit_sources(str(digest["id"]), scout_agent.seed_communities())
    database.retire_reddit_sources_by_name(
        str(digest["id"]),
        [*scout_agent.legacy_alias_names(), *scout_agent.discovery_blocklist()],
        reason="Retired by Source Scout because this community is an alias or too noisy for discovery.",
    )
    current_sources = database.list_reddit_sources(str(digest["id"]), include_retired=True)
    observations: dict[str, scout_agent.SourceObservation] = {}
    discovered: dict[str, int] = {}
    live_error: str | None = None

    if live_sample:
        try:
            observations, discovered = await _sample_reddit(str(digest.get("interest") or ""), current_sources)
        except Exception as exc:  # pragma: no cover - defensive, individual calls are already guarded.
            live_error = str(exc)
            logger.warning("Source Scout live Reddit sample failed: %s", exc)

    review = scout_agent.review_reddit_sources(
        digest_interest=str(digest.get("interest") or ""),
        current_sources=current_sources,
        observations=observations,
        discovered_subreddits=discovered,
    )
    status = "partial" if review.partial or live_error else "completed"
    run = database.save_source_scout_review(
        digest_id=str(digest["id"]),
        review=review,
        status=status,
        error_detail=live_error,
    )
    return {
        **run,
        "sources": database.list_reddit_sources(str(digest["id"]), include_retired=True),
        "decisions": database.list_source_scout_decisions(digest_id=str(digest["id"]), limit=25),
    }


async def _sample_reddit(
    digest_interest: str,
    current_sources: list[dict[str, Any]],
) -> tuple[dict[str, scout_agent.SourceObservation], dict[str, int]]:
    source_names = _sources_to_sample(current_sources)
    browse_tasks = [_sample_source(source, digest_interest) for source in source_names]
    search_tasks = [_discover_sources(query) for query in scout_agent.discovery_queries()[:MAX_DISCOVERY_QUERIES]]
    browse_results, search_results = await asyncio.gather(
        asyncio.gather(*browse_tasks),
        asyncio.gather(*search_tasks),
    )
    observations = {result.subreddit.lower(): result for result in browse_results}
    discovered: Counter[str] = Counter()
    for names in search_results:
        discovered.update(names)
    return observations, dict(discovered)


def _sources_to_sample(current_sources: list[dict[str, Any]]) -> list[str]:
    ranked = sorted(
        current_sources,
        key=lambda row: (
            {"active": 4, "search_only": 3, "candidate": 2, "retired": 1}.get(str(row.get("state")), 0),
            float(row.get("score") or 0),
        ),
        reverse=True,
    )
    names: list[str] = []
    seen: set[str] = set()
    for source in ranked:
        if source.get("state") == "retired":
            continue
        subreddit = str(source.get("subreddit") or "").strip()
        key = subreddit.lower()
        if not subreddit or key in seen:
            continue
        names.append(subreddit)
        seen.add(key)
        if len(names) >= MAX_SAMPLED_SOURCES:
            break
    return names


async def _sample_source(subreddit: str, digest_interest: str) -> scout_agent.SourceObservation:
    try:
        posts = await reddit_mcp_client.browse_subreddit(subreddit, sort="top", time="week", limit=15)
    except Exception as exc:  # pragma: no cover - reddit_mcp_client returns empty on recoverable errors.
        return scout_agent.SourceObservation(subreddit=subreddit, error=str(exc))
    if not posts:
        return scout_agent.SourceObservation(subreddit=subreddit, error="No posts returned from Reddit MCP.")
    return scout_agent.observation_from_posts(subreddit, posts, digest_interest=digest_interest)


async def _discover_sources(query: str) -> list[str]:
    posts = await reddit_mcp_client.search_reddit(query, sort="relevance", time="week", limit=25)
    names = []
    for post in posts:
        subreddit = str(post.get("subreddit") or "").strip()
        if subreddit:
            names.append(subreddit)
    return names
