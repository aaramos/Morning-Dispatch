from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from backend.agents.discovery.types import TopicProfile
from backend.app.core.config import get_settings
from backend.app.core.prompt_loader import load_prompt
from backend.app.services import model_routing

logger = logging.getLogger(__name__)


async def refine_queries_for_adapter(
    adapter_name: str,
    profile: TopicProfile,
    initial_results: list[Any],
    initial_queries: list[str],
    lookback_hours: int | None = None,
) -> list[str]:
    """Uses the LLM strategy refinement agent to generate a list of alternative search queries
    when the initial search results are sparse or empty.
    """
    settings = get_settings()
    try:
        # Route to refinement agent
        resolution = model_routing.client_for_agent("refinement", settings=settings)
        client = resolution.client
        if client is None:
            logger.warning("No LLM client configured for refinement agent. Skipping query refinement.")
            return []

        # Prepare refinement prompt
        prompt_data = {
            "adapter": adapter_name,
            "statement": profile.statement,
            "scope": profile.scope,
            "keywords": list(profile.keywords),
            "original_queries": initial_queries,
            "results_count": len(initial_results),
            "current_date": datetime.now(UTC).date().isoformat(),
            "lookback_hours": lookback_hours,
            "instructions": (
                "Review the original search queries and the number of results found for this source. "
                "Suggest up to 4 alternative or refined queries (e.g., using synonyms, related terms, broader concepts) "
                "that will help find relevant recent content for the user's topic profile without widening the source recency window. "
                "Provide the queries in the 'refined_queries' list."
            ),
        }
        prompt_str = json.dumps(prompt_data, ensure_ascii=False)
        system_prompt = load_prompt("query_refinement")

        logger.info(
            "Running query refinement agent for %s with %d initial results...",
            adapter_name,
            len(initial_results),
        )

        # Complete JSON query
        payload = await client.complete_json(
            system=system_prompt,
            prompt=prompt_str,
            max_tokens=600,
        )

        refined_queries = payload.get("refined_queries")
        if isinstance(refined_queries, list):
            # Clean and filter empty queries
            cleaned_queries = []
            for q in refined_queries:
                q_str = str(q or "").strip()
                if q_str and q_str not in cleaned_queries:
                    cleaned_queries.append(q_str)
            logger.info(
                "Query refinement agent suggested queries for %s: %s",
                adapter_name,
                cleaned_queries,
            )
            return cleaned_queries

        logger.warning(
            "Query refinement agent returned invalid payload for %s: %s",
            adapter_name,
            payload,
        )
        return []

    except Exception as exc:
        logger.exception("Failed to run query refinement for adapter %s: %s", adapter_name, exc)
        return []


async def screen_candidates(
    profile: TopicProfile,
    candidates: list[Any],
) -> list[Any]:
    """Applies an LLM-led screening pass to Gmail and Podcast candidates before lane capacity limits are enforced.
    Drops ads, promotional spam, and items with titles not aligned to the query.
    """
    settings = get_settings()
    gmail_cands = [c for c in candidates if c.adapter == "gmail"]
    podcast_cands = [c for c in candidates if c.adapter == "podcasts"]

    if not gmail_cands and not podcast_cands:
        return candidates

    to_screen = gmail_cands + podcast_cands
    logger.info(
        "Running agentic screening pass on %d candidates (Gmail: %d, Podcasts: %d)...",
        len(to_screen),
        len(gmail_cands),
        len(podcast_cands),
    )

    try:
        resolution = model_routing.client_for_agent("refinement", settings=settings)
        client = resolution.client
        if client is None:
            logger.warning("No LLM client configured for candidate screening. Skipping screening pass.")
            return candidates
    except Exception as exc:
        logger.warning("Failed to obtain model client for candidate screening: %s", exc)
        return candidates

    batch_size = 15
    batches = [to_screen[i : i + batch_size] for i in range(0, len(to_screen), batch_size)]

    async def screen_batch(batch: list[Any]) -> dict[str, str]:
        cand_list = []
        for c in batch:
            metadata = c.payload.metadata or {}
            title = (
                metadata.get("title")
                or metadata.get("subject")
                or metadata.get("link_text")
                or c.payload.source_name
            )
            cand_list.append({
                "id": c.payload.id,
                "title": title,
                "source": c.payload.source_name,
                "snippet": c.payload.raw_text[:200] if c.payload.raw_text else "",
            })

        prompt_data = {
            "statement": profile.statement,
            "scope": profile.scope,
            "exclusions": list(profile.exclusions),
            "candidates_json": json.dumps(cand_list, ensure_ascii=False),
        }

        # Render custom template fields by replacing variables manually (or let the model handle it if supported)
        system_prompt = load_prompt("candidate_screening")
        system_prompt = system_prompt.replace("{{statement}}", profile.statement)
        system_prompt = system_prompt.replace("{{exclusions}}", ", ".join(profile.exclusions))
        system_prompt = system_prompt.replace("{{candidates_json}}", json.dumps(cand_list, ensure_ascii=False))

        try:
            payload = await client.complete_json(
                system=system_prompt,
                prompt=json.dumps(cand_list, ensure_ascii=False),
                max_tokens=2000,
            )
            decisions = payload.get("decisions", [])
            return {
                str(d.get("id")): str(d.get("decision")).strip().lower()
                for d in decisions
                if isinstance(d, dict) and "id" in d
            }
        except Exception as exc:
            logger.warning("Screening call failed for batch: %s", exc)
            return {}

    # Run batches in parallel
    results = await asyncio.gather(*(screen_batch(b) for b in batches), return_exceptions=True)

    decisions_map = {}
    for res in results:
        if isinstance(res, dict):
            decisions_map.update(res)

    screened_candidates = []
    dropped_count = 0
    for c in candidates:
        if c.adapter in {"gmail", "podcasts"}:
            decision = decisions_map.get(c.payload.id)
            if decision == "drop":
                dropped_count += 1
                continue
        screened_candidates.append(c)

    logger.info(
        "Agentic screening pass complete. Dropped %d candidates of %d screened.",
        dropped_count,
        len(to_screen),
    )
    return screened_candidates
