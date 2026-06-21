"""Hacker News discovery lane.

Uses the key-less Algolia HN Search API (one JSON call per query). Returns
stories with title, linked URL, points and comment count; the story title plus
any self-text is the candidate body, and downstream fetching pulls the linked
article. No comment-thread enrichment (stories + snippet only).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery.types import (
    Candidate,
    CostProfile,
    SourceAdapterContext,
    TopicProfile,
)
from backend.app.core.http_pool import shared_async_client

logger = logging.getLogger(__name__)

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_URL = "https://news.ycombinator.com/item?id="

MAX_HN_QUERIES = 3
RESULTS_PER_QUERY = 20
_REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class HackerNewsHit:
    object_id: str
    title: str
    url: str  # linked article URL, or the HN item URL for text posts
    snippet: str
    points: int
    num_comments: int
    published_at: str | None  # UTC ISO-8601 (seconds)


def _to_iso(created_at_i: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(created_at_i), tz=UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def parse_hn(payload: Any, *, limit: int) -> list[HackerNewsHit]:
    hits_raw = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits_raw, list):
        return []
    hits: list[HackerNewsHit] = []
    for item in hits_raw[:limit]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or item.get("story_title") or "").split()).strip()
        if not title:
            continue
        object_id = str(item.get("objectID") or "").strip()
        story_url = str(item.get("url") or item.get("story_url") or "").strip()
        url = story_url or (f"{HN_ITEM_URL}{object_id}" if object_id else "")
        if not url:
            continue
        story_text = " ".join(str(item.get("story_text") or item.get("comment_text") or "").split()).strip()
        try:
            points = int(item.get("points") or 0)
        except (TypeError, ValueError):
            points = 0
        try:
            num_comments = int(item.get("num_comments") or 0)
        except (TypeError, ValueError):
            num_comments = 0
        hits.append(
            HackerNewsHit(
                object_id=object_id,
                title=title,
                url=url,
                snippet=story_text or title,
                points=points,
                num_comments=num_comments,
                published_at=_to_iso(item.get("created_at_i")),
            )
        )
    return hits


async def fetch_hn(
    query: str,
    *,
    limit: int = RESULTS_PER_QUERY,
    since_ts: int | None = None,
) -> list[HackerNewsHit]:
    params: dict[str, Any] = {
        "query": query,
        "tags": "story",
        "hitsPerPage": limit,
    }
    if since_ts is not None:
        params["numericFilters"] = f"created_at_i>{since_ts}"
    client = shared_async_client(purpose="hacker_news", timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    response = await client.get(HN_SEARCH_URL, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_hn(response.json(), limit=limit)


def _build_queries(profile: TopicProfile, *, limit: int) -> list[str]:
    from backend.agents.discovery.adapters import _requested_refs, _web_search_queries

    queries = _web_search_queries(profile, _requested_refs(profile, "hacker_news"), adapter="hacker_news")
    return queries[:limit]


class HackerNewsSourceAdapter:
    name = "hacker_news"
    cost_profile = CostProfile(label="fast", timeout_seconds=25.0)
    good_for = ("community_discussion", "emerging_topics", "broad_discovery")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        queries = _build_queries(profile, limit=MAX_HN_QUERIES)
        if not queries:
            return []

        since_ts: int | None = None
        if context.lookback_hours:
            since_ts = int(
                (datetime.now(UTC) - timedelta(hours=int(context.lookback_hours))).timestamp()
            )

        async def run(query: str) -> list[HackerNewsHit]:
            try:
                return await fetch_hn(query, limit=RESULTS_PER_QUERY, since_ts=since_ts)
            except Exception as exc:  # noqa: BLE001 - isolate one query's failure
                logger.info("Hacker News search failed for %r: %s", query, exc)
                return []

        per_query = await asyncio.gather(*(run(q) for q in queries))

        merged: list[HackerNewsHit] = []
        seen: set[str] = set()
        for hits in per_query:
            for hit in hits:
                key = hit.url.rstrip("/").lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(hit)

        # Surface the most-discussed stories first.
        merged.sort(key=lambda h: (h.points + h.num_comments), reverse=True)

        candidate_limit = max(1, context.candidate_limit)
        candidates: list[Candidate] = []
        for rank, hit in enumerate(merged[:candidate_limit]):
            score = round(max(0.55, 0.90 - rank * 0.02), 3)
            candidates.append(
                Candidate(
                    adapter=self.name,
                    payload=NormalizedPayload(
                        source_type="hacker_news_story",
                        source_name="Hacker News",
                        raw_text=hit.snippet,
                        original_url=hit.url,
                        published_at=hit.published_at,
                        metadata={
                            "link_quality_score": score,
                            "search_provider": "hacker_news_algolia",
                            "points": hit.points,
                            "num_comments": hit.num_comments,
                            "hn_url": f"{HN_ITEM_URL}{hit.object_id}" if hit.object_id else None,
                            "title": hit.title,
                        },
                    ),
                    score=score,
                    reason=f"Hacker News story ({hit.points} points, {hit.num_comments} comments): {hit.title}",
                )
            )
        logger.info("Hacker News lane: %d queries -> %d merged, %d candidates", len(queries), len(merged), len(candidates))
        return candidates

    async def fetch(self, candidate: Candidate) -> NormalizedPayload:
        return candidate.payload
