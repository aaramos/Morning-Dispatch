from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agents.discovery import regulatory
from backend.agents.discovery.regulatory import RegulatoryHit, RegulatorySourceAdapter
from backend.agents.discovery.types import Candidate, SourceAdapterContext, TopicProfile


class FakeResponse:
    def __init__(self, json_data) -> None:
        self._json = json_data

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, json_data) -> None:
        self._response = FakeResponse(json_data)

    async def get(self, url: str, **kwargs) -> FakeResponse:
        return self._response


FED_JSON = {
    "results": [
        {
            "title": "Safeguards for AI Model Disclosures",
            "abstract": "A proposed rule governing AI model disclosures.",
            "html_url": "https://www.federalregister.gov/documents/2026/03/01/ai-rule",
            "publication_date": "2026-03-01",
            "type": "Proposed Rule",
            "agencies": [{"name": "Securities and Exchange Commission"}],
        }
    ]
}

COURT_JSON = {
    "results": [
        {
            "caseName": "SEC v. Example Corp",
            "absolute_url": "/opinion/12345/sec-v-example/",
            "dateFiled": "2026-02-15",
            "court": "S.D.N.Y.",
            "snippet": "The court finds...",
        }
    ]
}


def test_parse_federal_register() -> None:
    hits = regulatory.parse_federal_register(FED_JSON, limit=5)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.title == "Safeguards for AI Model Disclosures"
    assert hit.url == "https://www.federalregister.gov/documents/2026/03/01/ai-rule"
    assert hit.source == "Securities and Exchange Commission"
    assert hit.jurisdiction == "us" and hit.provider == "federal_register"
    assert hit.published_at == "2026-03-01T00:00:00+00:00"
    assert hit.extra["document_type"] == "Proposed Rule"


def test_parse_courtlistener_absolutizes_url() -> None:
    hits = regulatory.parse_courtlistener(COURT_JSON, limit=5)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.title == "SEC v. Example Corp"
    assert hit.url == "https://www.courtlistener.com/opinion/12345/sec-v-example/"
    assert hit.source == "S.D.N.Y."
    assert hit.published_at == "2026-02-15T00:00:00+00:00"


def test_fetch_federal_register_uses_client(monkeypatch) -> None:
    monkeypatch.setattr(regulatory, "shared_async_client", lambda **_k: FakeAsyncClient(FED_JSON))
    hits = asyncio.run(regulatory.fetch_federal_register("ai disclosure", limit=3))
    assert len(hits) == 1 and hits[0].provider == "federal_register"


def test_fetch_international_scopes_to_regulator_domains(monkeypatch) -> None:
    captured = {}

    async def fake_search_web(query, **kwargs):
        captured["query"] = query
        return [
            SimpleNamespace(
                url="https://www.investegate.co.uk/announcement/abc",
                title="Trading Update",
                snippet="Half-year results.",
                published_at="2026-04-01T00:00:00+00:00",
                provider="serper",
                score=0.7,
            )
        ]

    monkeypatch.setattr(regulatory, "search_web", fake_search_web)
    hits = asyncio.run(regulatory.fetch_international("chip stocks", limit=5, days=30))
    assert "site:investegate.co.uk" in captured["query"]
    assert "site:hkexnews.hk" in captured["query"]
    assert len(hits) == 1
    assert hits[0].jurisdiction == "international"
    assert hits[0].source == "www.investegate.co.uk"


def test_regulatory_adapter_query_merges_us_and_international(monkeypatch) -> None:
    async def fake_fed(query, **kwargs):
        return [RegulatoryHit("US Rule", "https://fed.gov/r1", "us rule", "2026-03-01T00:00:00+00:00", "SEC", "us", "federal_register")]

    async def fake_court(query, **kwargs):
        return [RegulatoryHit("US Case", "https://courtlistener.com/c1", "case", "2026-02-01T00:00:00+00:00", "SDNY", "us", "courtlistener")]

    async def fake_intl(query, **kwargs):
        return [RegulatoryHit("UK RNS", "https://investegate.co.uk/a1", "rns", "2026-04-01T00:00:00+00:00", "investegate.co.uk", "international", "web")]

    monkeypatch.setattr(regulatory, "fetch_federal_register", fake_fed)
    monkeypatch.setattr(regulatory, "fetch_courtlistener", fake_court)
    monkeypatch.setattr(regulatory, "fetch_international", fake_intl)

    adapter = RegulatorySourceAdapter()
    profile = TopicProfile.from_dict({"statement": "AI regulation", "search_queries": ["ai regulation"]})
    candidates = asyncio.run(adapter.query(profile, SourceAdapterContext(exploration_id="x", candidate_limit=10)))

    assert len(candidates) == 3
    jurisdictions = {c.payload.metadata["jurisdiction"] for c in candidates}
    assert jurisdictions == {"us", "international"}
    assert all(isinstance(c, Candidate) for c in candidates)
    assert all(c.adapter == "regulatory" for c in candidates)
    assert all(c.payload.source_type == "regulatory_filing" for c in candidates)


def test_regulatory_adapter_survives_partial_provider_failure(monkeypatch) -> None:
    async def fake_fed(query, **kwargs):
        return [RegulatoryHit("US Rule", "https://fed.gov/r1", "us rule", None, "SEC", "us", "federal_register")]

    async def boom(query, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(regulatory, "fetch_federal_register", fake_fed)
    monkeypatch.setattr(regulatory, "fetch_courtlistener", boom)
    monkeypatch.setattr(regulatory, "fetch_international", boom)

    adapter = RegulatorySourceAdapter()
    profile = TopicProfile.from_dict({"statement": "x", "search_queries": ["y"]})
    candidates = asyncio.run(adapter.query(profile, SourceAdapterContext(exploration_id="x", candidate_limit=5)))
    assert len(candidates) == 1
    assert candidates[0].payload.source_name == "SEC"
