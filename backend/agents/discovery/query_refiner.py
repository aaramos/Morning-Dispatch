from __future__ import annotations

import asyncio
import json
import logging
import random
import unicodedata
from datetime import UTC, datetime
from typing import Any

from backend.agents.discovery.types import TopicProfile
from backend.app.core.config import get_settings
from backend.app.core.prompt_loader import load_prompt
from backend.app.services import model_routing

logger = logging.getLogger(__name__)

_SCREENING_BATCH_SIZE = 15
_SCREENING_MAX_CANDIDATES_PER_SOURCE = 500
_SCREENING_MAX_CONCURRENCY = 8
_SCREENING_BATCH_TIMEOUT_SECONDS = 90.0


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
            "must_have_terms": list(profile.must_have_terms),
            "must_have_aliases": {key: list(value) for key, value in (profile.must_have_aliases or {}).items()},
            "results_count": len(initial_results),
            "current_date": datetime.now(UTC).date().isoformat(),
            "lookback_hours": lookback_hours,
            "instructions": (
                "Review the original search queries and the number of results found for this source. "
                "Suggest up to 4 alternative or refined queries (e.g., using synonyms, related terms, broader concepts) "
                "that will help find relevant recent content for the user's topic profile without widening the source recency window. "
                "If must_have_terms are provided, every query must include at least one must-have term or one of its aliases. "
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
            return enforce_must_have_on_queries(profile, cleaned_queries)

        logger.warning(
            "Query refinement agent returned invalid payload for %s: %s",
            adapter_name,
            payload,
        )
        return []

    except Exception as exc:
        logger.exception("Failed to run query refinement for adapter %s: %s", adapter_name, exc)
        return []


async def expand_search_strategy(
    profile: TopicProfile,
    *,
    lookback_hours: int | None = None,
    max_expansions: int = 4,
) -> list[str]:
    """Proactively widen the search strategy for ALL sources (item 1).

    Asks the refinement agent for affiliated, adjacent, or synonymous angles that
    surface related items the original queries would miss — even loosely-related
    ones that will rate lower. Returns only the NEW expansion queries (callers
    fold them into per-source queries). Fails open to an empty list so a missing
    model client or any error simply leaves the original strategy untouched.
    """
    settings = get_settings()
    try:
        resolution = model_routing.client_for_agent("refinement", settings=settings)
        client = resolution.client
        if client is None:
            return []

        base_queries = [q for q in list(profile.search_queries) if str(q or "").strip()]
        if not base_queries:
            seed = profile.discovery_text().strip()
            if seed:
                base_queries = [seed]

        prompt_data = {
            "statement": profile.statement,
            "scope": profile.scope,
            "keywords": list(profile.keywords),
            "subtopics": list(profile.subtopics),
            "original_queries": base_queries,
            "must_have_terms": list(profile.must_have_terms),
            "must_have_aliases": {key: list(value) for key, value in (profile.must_have_aliases or {}).items()},
            "current_date": datetime.now(UTC).date().isoformat(),
            "lookback_hours": lookback_hours,
            "mode": "proactive_expansion",
            "instructions": (
                "Proactively broaden the search strategy. Suggest up to "
                f"{max_expansions} affiliated, adjacent, or synonymous search angles "
                "that surface related items the original queries would miss — even if "
                "they are only loosely tied to the interest and would rate lower. "
                "Do not repeat the original queries. Keep them within the same broad "
                "interest. If must_have_terms are provided, every query must include "
                "at least one must-have term or one of its aliases. "
                "Provide them in the 'refined_queries' list."
            ),
        }
        system_prompt = load_prompt("query_refinement")

        payload = await client.complete_json(
            system=system_prompt,
            prompt=json.dumps(prompt_data, ensure_ascii=False),
            max_tokens=600,
        )
        refined = payload.get("refined_queries")
        if not isinstance(refined, list):
            return []

        existing = {q.strip().lower() for q in base_queries}
        cleaned: list[str] = []
        for q in refined:
            value = str(q or "").strip()
            key = value.lower()
            if not value or key in existing or key in {c.lower() for c in cleaned}:
                continue
            cleaned.append(value)
            if len(cleaned) >= max_expansions:
                break
        if cleaned:
            logger.info("Proactive search-strategy expansion added queries: %s", cleaned)
        return enforce_must_have_on_queries(profile, cleaned)
    except Exception as exc:
        logger.warning("Proactive search-strategy expansion failed: %s", exc)
        return []


async def screen_candidates(
    profile: TopicProfile,
    candidates: list[Any],
    exclusions: list[dict[str, Any]] | None = None,
    low_yield: bool = False,
) -> list[Any]:
    """Applies an LLM-led screening pass to Gmail and Podcast candidates before lane capacity limits are enforced.
    Drops ads, promotional spam, and items with titles not aligned to the query.
    """
    settings = get_settings()
    gmail_cands = [c for c in candidates if c.adapter == "gmail"]
    podcast_cands = [c for c in candidates if c.adapter == "podcasts"]

    if not gmail_cands and not podcast_cands:
        return candidates

    to_screen = [
        *_screening_sample(gmail_cands),
        *_screening_sample(podcast_cands),
    ]
    if not to_screen:
        return candidates

    skipped_count = len(gmail_cands) + len(podcast_cands) - len(to_screen)
    logger.info(
        "Running bounded agentic screening pass on %d candidates (Gmail: %d, Podcasts: %d, unscreened kept: %d)...",
        len(to_screen),
        sum(1 for c in to_screen if c.adapter == "gmail"),
        sum(1 for c in to_screen if c.adapter == "podcasts"),
        max(0, skipped_count),
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

    batches = [to_screen[i : i + _SCREENING_BATCH_SIZE] for i in range(0, len(to_screen), _SCREENING_BATCH_SIZE)]
    semaphore = asyncio.Semaphore(_SCREENING_MAX_CONCURRENCY)

    async def screen_batch(batch: list[Any]) -> dict[str, str]:
        async with semaphore:
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
                "must_have_terms": list(profile.must_have_terms),
                "must_have_aliases": {key: list(value) for key, value in (profile.must_have_aliases or {}).items()},
                "candidates_json": json.dumps(cand_list, ensure_ascii=False),
            }

            system_prompt = load_prompt("candidate_screening")
            system_prompt = system_prompt.replace("{{statement}}", profile.statement)
            system_prompt = system_prompt.replace("{{exclusions}}", ", ".join(profile.exclusions))
            system_prompt = system_prompt.replace("{{candidates_json}}", json.dumps(cand_list, ensure_ascii=False))

            if low_yield:
                system_prompt += (
                    "\n\nCRITICAL: We are in a low-yield retrieval mode. "
                    "Please be EXTREMELY permissive. Only drop candidates if they are "
                    "unquestionably spam, advertising, or completely unrelated to the topic. "
                    "If a candidate has any reasonable connection to the topic statement, "
                    "choose 'keep' instead of 'drop'."
                )

            try:
                timeout_seconds = max(_SCREENING_BATCH_TIMEOUT_SECONDS, float(settings.model_timeout_seconds or 0.0))
                payload = await asyncio.wait_for(
                    client.complete_json(
                        system=system_prompt,
                        prompt=json.dumps(prompt_data, ensure_ascii=False),
                        max_tokens=2000,
                    ),
                    timeout=timeout_seconds,
                )
                decisions = payload.get("decisions", [])
                return {
                    str(d.get("id")): str(d.get("decision")).strip().lower()
                    for d in decisions
                    if isinstance(d, dict) and "id" in d
                }
            except Exception as exc:
                logger.warning("Screening call failed or timed out for batch: %s", exc)
                return {}

    # Run batches in parallel
    results = await asyncio.gather(*(screen_batch(b) for b in batches), return_exceptions=True)

    decisions_map = {}
    for res in results:
        if isinstance(res, dict):
            decisions_map.update(res)

    screened_candidates = []
    dropped_candidates: list[Any] = []
    dropped_count = 0
    for c in candidates:
        if c.adapter in {"gmail", "podcasts"}:
            decision = decisions_map.get(str(c.payload.id))
            if decision == "drop":
                dropped_count += 1
                dropped_candidates.append(c)
                continue
        screened_candidates.append(c)

    preserved_ids: set[str] = set()
    for adapter in ("gmail", "podcasts"):
        source_selected = bool(profile.source_selection.get(adapter))
        if not source_selected:
            continue
        had_candidates = any(c.adapter == adapter for c in candidates)
        kept_candidates = [c for c in screened_candidates if c.adapter == adapter]
        if not had_candidates or kept_candidates:
            continue
        restoration_limit = 3 if adapter == "gmail" else 2
        restorable = sorted(
            (c for c in dropped_candidates if c.adapter == adapter),
            key=lambda c: (
                getattr(c, "score", 0.0),
                getattr(c.payload, "published_at", None) or getattr(c.payload, "fetched_at", None) or "",
            ),
            reverse=True,
        )[:restoration_limit]
        for candidate in restorable:
            candidate.payload.metadata = {
                **dict(candidate.payload.metadata or {}),
                "screening_preserved_low_yield": True,
            }
            screened_candidates.append(candidate)
            preserved_ids.add(str(candidate.payload.id))

    if exclusions is not None:
        for c in dropped_candidates:
            if str(c.payload.id) in preserved_ids:
                continue
            metadata = c.payload.metadata or {}
            title = (
                metadata.get("title")
                or metadata.get("link_text")
                or metadata.get("subject")
                or metadata.get("parent_subject")
                or metadata.get("youtube_title")
                or metadata.get("podcast_title")
                or c.payload.source_name
                or c.reason
            )
            exclusions.append({
                "adapter": c.adapter,
                "candidate_id": str(c.payload.id),
                "original_url": c.payload.original_url,
                "source_type": c.payload.source_type,
                "source_name": c.payload.source_name,
                "title": title,
                "subject": metadata.get("subject") or metadata.get("parent_subject"),
                "link_text": metadata.get("link_text"),
                "metadata": dict(metadata),
                "excluded_by": ["agentic_screening"],
                "reason": "Filtered by agentic screening (spam, promotion, or off-topic).",
            })

    logger.info(
        "Agentic screening pass complete. Dropped %d candidates of %d screened; preserved %d selected-source fallback(s); kept %d unscreened overflow candidates.",
        dropped_count,
        len(to_screen),
        len(preserved_ids),
        max(0, skipped_count),
    )
    return screened_candidates


def _screening_sample(candidates: list[Any]) -> list[Any]:
    if len(candidates) <= _SCREENING_MAX_CANDIDATES_PER_SOURCE:
        return candidates
    # Do not screen only the highest-ranked items. Those scores can be noisy at
    # this stage, and source-quality failures often hide in the long tail.
    return random.sample(candidates, _SCREENING_MAX_CANDIDATES_PER_SOURCE)


def enforce_must_have_on_queries(profile: TopicProfile, queries: list[str]) -> list[str]:
    """Ensure model-generated queries remain anchored to user-required terms."""
    anchor_sets = _must_have_query_alias_sets(profile)
    if not anchor_sets:
        return queries
    primary_anchor = str(profile.must_have_terms[0]).strip()
    if not primary_anchor:
        return queries

    anchored: list[str] = []
    seen: set[str] = set()
    for query in queries:
        value = str(query or "").strip()
        if not value:
            continue
        if not query_mentions_must_have(value, profile):
            value = f"{value} {primary_anchor}".strip()
        key = value.casefold()
        if key not in seen:
            anchored.append(value)
            seen.add(key)
    return anchored


def query_mentions_must_have(query: str, profile: TopicProfile) -> bool:
    haystack = _fold_query_text(query)
    for _anchor, aliases in _must_have_query_alias_sets(profile):
        if any(alias and alias in haystack for alias in aliases):
            return True
    return False


def _must_have_query_alias_sets(profile: TopicProfile) -> list[tuple[str, set[str]]]:
    aliases_by_key = {
        _fold_query_text(key): {_fold_query_text(alias) for alias in aliases if _fold_query_text(alias)}
        for key, aliases in (profile.must_have_aliases or {}).items()
    }
    alias_sets: list[tuple[str, set[str]]] = []
    for term in profile.must_have_terms:
        anchor = str(term or "").strip()
        folded = _fold_query_text(anchor)
        if not folded:
            continue
        alias_sets.append((anchor, {folded, *aliases_by_key.get(folded, set())}))
    return alias_sets


def _fold_query_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(char for char in text if not unicodedata.combining(char))
