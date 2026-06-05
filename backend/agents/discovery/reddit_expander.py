from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from backend.agents.discovery.types import TopicProfile
from backend.app.core.config import get_settings
from backend.app.core.prompt_loader import load_prompt
from backend.app.services import model_routing

logger = logging.getLogger(__name__)

SUBREDDIT_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")


@dataclass
class RedditTargets:
    subreddits: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)


def clean_subreddit_name(name: str) -> str | None:
    """Cleans and validates a subreddit name.
    Strips leading /r/ or r/ prefixes and checks if it's alphanumeric + underscore.
    """
    cleaned = str(name).strip()
    if not cleaned:
        return None
    # Strip leading /r/ or r/
    cleaned = re.sub(r"^/?r/", "", cleaned)
    # Check alphanumeric + underscore
    if SUBREDDIT_PATTERN.match(cleaned):
        return cleaned
    return None


async def expand_reddit_targets(profile: TopicProfile) -> RedditTargets:
    """Semantically expands the user's interest profile into subreddits and search queries
    using the refinement AI model. Falls back gracefully if expansion fails or is unconfigured.
    """
    settings = get_settings()
    
    # 1. Extract requested subreddits (these take priority and are always included)
    requested_subs: list[str] = []
    for src in profile.requested_sources:
        if isinstance(src, dict) and src.get("adapter") == "reddit":
            ref = src.get("ref") or src.get("source_name")
            if ref:
                cleaned = clean_subreddit_name(str(ref))
                if cleaned and cleaned not in requested_subs:
                    requested_subs.append(cleaned)

    # 2. Try AI Expansion
    ai_subreddits: list[str] = []
    ai_queries: list[str] = []
    ai_failed = False

    try:
        resolution = model_routing.client_for_agent("refinement", settings=settings)
        client = resolution.client
        if client is None:
            logger.warning("No LLM client configured for refinement agent. Falling back to default Reddit queries.")
            ai_failed = True
        else:
            system_prompt = load_prompt("reddit_expansion")
            prompt_data = {
                "statement": profile.statement,
                "scope": profile.scope,
                "keywords": list(profile.keywords),
                "subtopics": list(profile.subtopics),
                "search_queries": list(profile.search_queries),
            }
            prompt_str = json.dumps(prompt_data, ensure_ascii=False)

            logger.info("Calling LLM to expand Reddit targets...")
            payload = await client.complete_json(
                system=system_prompt,
                prompt=prompt_str,
                max_tokens=600,
            )

            if isinstance(payload, dict):
                # Parse subreddits
                raw_subs = payload.get("subreddits") or []
                if isinstance(raw_subs, list):
                    for sub in raw_subs:
                        cleaned = clean_subreddit_name(str(sub))
                        if cleaned and cleaned not in ai_subreddits:
                            ai_subreddits.append(cleaned)

                # Parse search queries
                raw_queries = payload.get("search_queries") or []
                if isinstance(raw_queries, list):
                    for q in raw_queries:
                        q_str = str(q or "").strip()
                        if q_str and q_str not in ai_queries:
                            ai_queries.append(q_str)
            else:
                logger.warning("LLM expansion returned invalid response format: %s", payload)
                ai_failed = True

    except Exception as exc:
        logger.exception("Failed to expand Reddit targets via LLM: %s", exc)
        ai_failed = True

    # 3. Handle Merge and Fallbacks
    if ai_failed:
        # Graceful fallback: derive search queries directly from keywords and search_queries,
        # with no subreddit browsing beyond explicitly requested subreddits.
        fallback_queries = []
        for q in list(profile.search_queries) + list(profile.keywords):
            q_str = str(q or "").strip()
            if q_str and q_str not in fallback_queries:
                fallback_queries.append(q_str)
        
        final_subs = requested_subs
        final_queries = fallback_queries[:5]
    else:
        # Merge requested subreddits at the front (highest priority)
        merged_subs = list(requested_subs)
        for sub in ai_subreddits:
            if sub not in merged_subs:
                merged_subs.append(sub)

        max_subreddits = max(1, int(getattr(settings, "reddit_max_subreddits", 8) or 8))
        final_subs = merged_subs[:max_subreddits]
        final_queries = ai_queries[:5]

    logger.info(
        "Reddit expansion complete. Target subreddits: %s, search queries: %s",
        final_subs,
        final_queries,
    )
    return RedditTargets(subreddits=final_subs, search_queries=final_queries)
