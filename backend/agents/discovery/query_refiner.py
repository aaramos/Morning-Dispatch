from __future__ import annotations

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
