"""Academic & preprint discovery lane.

Two free, key-less providers searched by topic and merged:

* arXiv  — STEM/CS/physics/econ preprints, Atom feed parsed with feedparser.
* OpenAlex — all-discipline scholarly works (JSON), with citation counts and
  reconstructable abstracts.

Both expose clean ISO publication dates and stable URLs, so candidates map
directly onto NormalizedPayload and downstream recency filtering. No API key is
required; OpenAlex only asks for a polite ``mailto`` parameter.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery.types import (
    Candidate,
    CostProfile,
    SourceAdapterContext,
    TopicProfile,
)
from backend.app.core.http_pool import shared_async_client

logger = logging.getLogger(__name__)

ARXIV_API_URL = "http://export.arxiv.org/api/query"
OPENALEX_API_URL = "https://api.openalex.org/works"
# Polite-pool contact for OpenAlex (no key; identifies the client for rate limits).
OPENALEX_MAILTO = "morning-dispatch@example.com"

MAX_ACADEMIC_QUERIES = 3
RESULTS_PER_PROVIDER = 15
_REQUEST_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class AcademicHit:
    title: str
    url: str
    snippet: str  # abstract
    published_at: str | None  # UTC ISO-8601 (seconds)
    provider: str  # "arxiv" | "openalex"
    authors: tuple[str, ...] = ()
    cited_by_count: int = 0
    venue: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _to_iso(value: Any) -> str | None:
    """Normalize a date/datetime-ish value to UTC ISO-8601 seconds."""
    text = str(value or "").strip()
    if not text:
        return None
    # OpenAlex publication_date is YYYY-MM-DD; arxiv published is full ISO.
    for parse in (
        lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
        lambda t: datetime.strptime(t, "%Y-%m-%d"),
    ):
        try:
            dt = parse(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).isoformat(timespec="seconds")
        except (ValueError, TypeError):
            continue
    return None


def reconstruct_abstract(inverted_index: Any) -> str:
    """Rebuild plain text from OpenAlex's abstract_inverted_index map."""
    if not isinstance(inverted_index, dict) or not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted_index.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            try:
                positions.append((int(index), str(word)))
            except (TypeError, ValueError):
                continue
    if not positions:
        return ""
    positions.sort(key=lambda item: item[0])
    return " ".join(word for _, word in positions).strip()


def parse_arxiv(text: str, *, limit: int) -> list[AcademicHit]:
    feed = feedparser.parse(text)
    hits: list[AcademicHit] = []
    for entry in feed.entries[:limit]:
        title = " ".join(str(entry.get("title", "") or "").split()).strip()
        url = str(entry.get("link", "") or "").strip()
        if not title or not url:
            continue
        summary = " ".join(str(entry.get("summary", "") or "").split()).strip()
        authors = tuple(
            str(a.get("name", "") or "").strip()
            for a in (entry.get("authors") or [])
            if str(a.get("name", "") or "").strip()
        )
        published_at = _to_iso(entry.get("published") or entry.get("updated"))
        hits.append(
            AcademicHit(
                title=title,
                url=url,
                snippet=summary,
                published_at=published_at,
                provider="arxiv",
                authors=authors,
            )
        )
    return hits


def parse_openalex(payload: Any, *, limit: int) -> list[AcademicHit]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    hits: list[AcademicHit] = []
    for work in results[:limit]:
        if not isinstance(work, dict):
            continue
        title = " ".join(str(work.get("title", "") or "").split()).strip()
        if not title:
            continue
        # Prefer the DOI/landing URL, fall back to the OpenAlex id.
        url = (
            str(work.get("doi") or "").strip()
            or str(work.get("id") or "").strip()
        )
        if not url:
            continue
        abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
        authorships = work.get("authorships") or []
        authors = tuple(
            str((a.get("author") or {}).get("display_name", "") or "").strip()
            for a in authorships
            if isinstance(a, dict) and str((a.get("author") or {}).get("display_name", "") or "").strip()
        )[:8]
        venue = ""
        primary = work.get("primary_location") or {}
        if isinstance(primary, dict):
            source = primary.get("source") or {}
            if isinstance(source, dict):
                venue = str(source.get("display_name", "") or "").strip()
        try:
            cited = int(work.get("cited_by_count") or 0)
        except (TypeError, ValueError):
            cited = 0
        hits.append(
            AcademicHit(
                title=title,
                url=url,
                snippet=abstract,
                published_at=_to_iso(work.get("publication_date")),
                provider="openalex",
                authors=authors,
                cited_by_count=cited,
                venue=venue,
            )
        )
    return hits


async def fetch_arxiv(query: str, *, limit: int = RESULTS_PER_PROVIDER) -> list[AcademicHit]:
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": limit,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    client = shared_async_client(purpose="academic", timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    response = await client.get(ARXIV_API_URL, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_arxiv(response.text, limit=limit)


async def fetch_openalex(
    query: str,
    *,
    limit: int = RESULTS_PER_PROVIDER,
    from_date: str | None = None,
) -> list[AcademicHit]:
    params: dict[str, Any] = {
        "search": query,
        "per-page": limit,
        "sort": "publication_date:desc",
        "mailto": OPENALEX_MAILTO,
    }
    if from_date:
        params["filter"] = f"from_publication_date:{from_date}"
    client = shared_async_client(purpose="academic", timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    response = await client.get(OPENALEX_API_URL, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_openalex(response.json(), limit=limit)


def _build_queries(profile: TopicProfile, *, limit: int) -> list[str]:
    # Reuse the shared query-builder so this lane benefits from the same
    # expansion as web_search/google_news. Lazy import avoids an import cycle
    # (adapters.py and this module are both pulled in by the registry).
    from backend.agents.discovery.adapters import _requested_refs, _web_search_queries

    queries = _web_search_queries(profile, _requested_refs(profile, "academic"), adapter="academic")
    return queries[:limit]


class AcademicSourceAdapter:
    name = "academic"
    cost_profile = CostProfile(label="medium", timeout_seconds=40.0)
    good_for = ("primary_sources", "deep_context", "expert_opinion")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        queries = _build_queries(profile, limit=MAX_ACADEMIC_QUERIES)
        if not queries:
            return []

        from_date: str | None = None
        if context.lookback_hours:
            from_date = (
                datetime.now(UTC) - timedelta(hours=int(context.lookback_hours))
            ).date().isoformat()

        async def run(query: str) -> list[AcademicHit]:
            results = await asyncio.gather(
                fetch_arxiv(query, limit=RESULTS_PER_PROVIDER),
                fetch_openalex(query, limit=RESULTS_PER_PROVIDER, from_date=from_date),
                return_exceptions=True,
            )
            hits: list[AcademicHit] = []
            for result in results:
                if isinstance(result, BaseException):
                    logger.info("Academic provider failed for %r: %s", query, result)
                    continue
                hits.extend(result)
            return hits

        per_query = await asyncio.gather(*(run(q) for q in queries))

        merged: list[AcademicHit] = []
        seen: set[str] = set()
        for hits in per_query:
            for hit in hits:
                key = hit.url.rstrip("/").lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(hit)

        candidate_limit = max(1, context.candidate_limit)
        candidates: list[Candidate] = []
        for rank, hit in enumerate(merged[:candidate_limit]):
            # Recency-ordered position score, with a small nudge for well-cited work.
            score = round(max(0.55, 0.90 - rank * 0.02) + min(0.05, hit.cited_by_count / 2000.0), 3)
            candidates.append(
                Candidate(
                    adapter=self.name,
                    payload=NormalizedPayload(
                        source_type="academic_paper",
                        source_name=hit.venue or ("arXiv" if hit.provider == "arxiv" else "OpenAlex"),
                        raw_text=hit.snippet or hit.title,
                        original_url=hit.url,
                        published_at=hit.published_at,
                        metadata={
                            "link_quality_score": score,
                            "search_provider": f"academic_{hit.provider}",
                            "academic_provider": hit.provider,
                            "authors": list(hit.authors),
                            "cited_by_count": hit.cited_by_count,
                            "venue": hit.venue,
                            "title": hit.title,
                        },
                    ),
                    score=score,
                    reason=f"{'arXiv preprint' if hit.provider == 'arxiv' else 'OpenAlex paper'}: {hit.title}",
                )
            )
        logger.info("Academic lane: %d queries -> %d merged hits, %d candidates", len(queries), len(merged), len(candidates))
        return candidates

    async def fetch(self, candidate: Candidate) -> NormalizedPayload:
        return candidate.payload
