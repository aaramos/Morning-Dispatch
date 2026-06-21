"""Regulatory & policy discovery lane.

Primary-source government/regulatory disclosures searched by topic:

US (key-less native JSON search):
* Federal Register — rules, proposed rules, notices.
* CourtListener — court opinions / filings.

International (key-less, reuses the configured web-search provider, scoped to
regulator/disclosure domains so topic search works without per-exchange APIs):
* RNS via Investegate (UK), HKEXnews (HK), ASX (AU), EDINET (JP), ESMA (EU).

All candidates normalize to source_type ``regulatory_filing``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery.types import (
    Candidate,
    CostProfile,
    SourceAdapterContext,
    TopicProfile,
)
from backend.agents.discovery.web_search import lookback_to_days, search_web
from backend.app.core.http_pool import shared_async_client

logger = logging.getLogger(__name__)

FEDERAL_REGISTER_URL = "https://www.federalregister.gov/api/v1/documents.json"
COURTLISTENER_URL = "https://www.courtlistener.com/api/rest/v4/search/"
COURTLISTENER_BASE = "https://www.courtlistener.com"

# Regulator / disclosure domains used to scope international topic search.
INTERNATIONAL_REGULATOR_DOMAINS = (
    "investegate.co.uk",       # UK — RNS aggregator
    "hkexnews.hk",             # Hong Kong — HKEXnews
    "asx.com.au",              # Australia — ASX announcements
    "disclosure.edinet-fsa.go.jp",  # Japan — EDINET
    "esma.europa.eu",          # EU — ESMA
)

MAX_REGULATORY_QUERIES = 2
RESULTS_PER_PROVIDER = 12
_REQUEST_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class RegulatoryHit:
    title: str
    url: str
    snippet: str
    published_at: str | None  # UTC ISO-8601 (seconds)
    source: str  # human-readable issuer / publisher
    jurisdiction: str  # "us" | "international"
    provider: str  # "federal_register" | "courtlistener" | "web"
    extra: dict[str, Any] = field(default_factory=dict)


def _to_iso(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
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


def parse_federal_register(payload: Any, *, limit: int) -> list[RegulatoryHit]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    hits: list[RegulatoryHit] = []
    for doc in results[:limit]:
        if not isinstance(doc, dict):
            continue
        title = " ".join(str(doc.get("title", "") or "").split()).strip()
        url = str(doc.get("html_url", "") or "").strip()
        if not title or not url:
            continue
        agencies = doc.get("agencies") or []
        agency_names = [
            str(a.get("name", "") or "").strip()
            for a in agencies
            if isinstance(a, dict) and str(a.get("name", "") or "").strip()
        ]
        doc_type = str(doc.get("type", "") or "").strip()
        hits.append(
            RegulatoryHit(
                title=title,
                url=url,
                snippet=" ".join(str(doc.get("abstract", "") or "").split()).strip() or title,
                published_at=_to_iso(doc.get("publication_date")),
                source=agency_names[0] if agency_names else "Federal Register",
                jurisdiction="us",
                provider="federal_register",
                extra={"document_type": doc_type, "agencies": agency_names},
            )
        )
    return hits


def parse_courtlistener(payload: Any, *, limit: int) -> list[RegulatoryHit]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    hits: list[RegulatoryHit] = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("caseName") or item.get("case_name") or "").split()).strip()
        rel_url = str(item.get("absolute_url", "") or "").strip()
        if not title or not rel_url:
            continue
        url = rel_url if rel_url.startswith("http") else f"{COURTLISTENER_BASE}{rel_url}"
        snippet = " ".join(
            str(item.get("snippet") or item.get("text") or title).split()
        ).strip()
        court = str(item.get("court") or "").strip()
        hits.append(
            RegulatoryHit(
                title=title,
                url=url,
                snippet=snippet,
                published_at=_to_iso(item.get("dateFiled") or item.get("date_filed")),
                source=court or "CourtListener",
                jurisdiction="us",
                provider="courtlistener",
                extra={"court": court},
            )
        )
    return hits


async def fetch_federal_register(query: str, *, limit: int = RESULTS_PER_PROVIDER) -> list[RegulatoryHit]:
    params: list[tuple[str, Any]] = [
        ("conditions[term]", query),
        ("order", "newest"),
        ("per_page", limit),
        ("fields[]", "title"),
        ("fields[]", "abstract"),
        ("fields[]", "html_url"),
        ("fields[]", "publication_date"),
        ("fields[]", "type"),
        ("fields[]", "agencies"),
    ]
    client = shared_async_client(purpose="regulatory", timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    response = await client.get(FEDERAL_REGISTER_URL, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_federal_register(response.json(), limit=limit)


async def fetch_courtlistener(query: str, *, limit: int = RESULTS_PER_PROVIDER) -> list[RegulatoryHit]:
    params = {"q": query, "order_by": "dateFiled desc", "type": "o"}
    client = shared_async_client(purpose="regulatory", timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    response = await client.get(COURTLISTENER_URL, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_courtlistener(response.json(), limit=limit)


async def fetch_international(
    query: str,
    *,
    limit: int = RESULTS_PER_PROVIDER,
    days: int | None = None,
) -> list[RegulatoryHit]:
    site_filter = " OR ".join(f"site:{domain}" for domain in INTERNATIONAL_REGULATOR_DOMAINS)
    scoped_query = f"{query} ({site_filter})"
    try:
        results = await search_web(scoped_query, limit=limit, days=days, vertical="news")
    except Exception as exc:  # noqa: BLE001 - isolate the international sweep
        logger.info("Regulatory international search failed for %r: %s", query, exc)
        return []
    hits: list[RegulatoryHit] = []
    for hit in results:
        url = str(getattr(hit, "url", "") or "").strip()
        title = " ".join(str(getattr(hit, "title", "") or "").split()).strip()
        if not url or not title:
            continue
        host = urlparse(url).netloc.lower()
        hits.append(
            RegulatoryHit(
                title=title,
                url=url,
                snippet=" ".join(str(getattr(hit, "snippet", "") or "").split()).strip() or title,
                published_at=getattr(hit, "published_at", None),
                source=host or "International regulator",
                jurisdiction="international",
                provider="web",
            )
        )
    return hits


def _build_queries(profile: TopicProfile, *, limit: int) -> list[str]:
    from backend.agents.discovery.adapters import _requested_refs, _web_search_queries

    queries = _web_search_queries(profile, _requested_refs(profile, "regulatory"), adapter="regulatory")
    return queries[:limit]


class RegulatorySourceAdapter:
    name = "regulatory"
    cost_profile = CostProfile(label="medium", timeout_seconds=45.0)
    good_for = ("primary_sources", "policy_signal", "deep_context")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        queries = _build_queries(profile, limit=MAX_REGULATORY_QUERIES)
        if not queries:
            return []

        days = lookback_to_days(context.lookback_hours)

        async def run(query: str) -> list[RegulatoryHit]:
            results = await asyncio.gather(
                fetch_federal_register(query, limit=RESULTS_PER_PROVIDER),
                fetch_courtlistener(query, limit=RESULTS_PER_PROVIDER),
                fetch_international(query, limit=RESULTS_PER_PROVIDER, days=days),
                return_exceptions=True,
            )
            hits: list[RegulatoryHit] = []
            for result in results:
                if isinstance(result, BaseException):
                    logger.info("Regulatory provider failed for %r: %s", query, result)
                    continue
                hits.extend(result)
            return hits

        per_query = await asyncio.gather(*(run(q) for q in queries))

        merged: list[RegulatoryHit] = []
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
            score = round(max(0.55, 0.90 - rank * 0.02), 3)
            candidates.append(
                Candidate(
                    adapter=self.name,
                    payload=NormalizedPayload(
                        source_type="regulatory_filing",
                        source_name=hit.source,
                        raw_text=hit.snippet,
                        original_url=hit.url,
                        published_at=hit.published_at,
                        metadata={
                            "link_quality_score": score,
                            "search_provider": f"regulatory_{hit.provider}",
                            "regulatory_provider": hit.provider,
                            "jurisdiction": hit.jurisdiction,
                            "title": hit.title,
                            **hit.extra,
                        },
                    ),
                    score=score,
                    reason=f"Regulatory ({hit.jurisdiction}, {hit.provider}): {hit.title}",
                )
            )
        logger.info("Regulatory lane: %d queries -> %d merged, %d candidates", len(queries), len(merged), len(candidates))
        return candidates

    async def fetch(self, candidate: Candidate) -> NormalizedPayload:
        return candidate.payload
