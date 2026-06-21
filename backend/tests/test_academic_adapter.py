from __future__ import annotations

import asyncio

from backend.agents.discovery import academic
from backend.agents.discovery.academic import AcademicHit, AcademicSourceAdapter
from backend.agents.discovery.types import Candidate, SourceAdapterContext, TopicProfile


class FakeResponse:
    def __init__(self, *, text: str = "", json_data=None) -> None:
        self.text = text
        self._json = json_data

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, *, text: str = "", json_data=None) -> None:
        self._response = FakeResponse(text=text, json_data=json_data)

    async def get(self, url: str, **kwargs) -> FakeResponse:
        return self._response


ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Scaling Laws for Neural Language Models</title>
    <summary>We study empirical scaling laws for language model performance.</summary>
    <link href="http://arxiv.org/abs/2001.00001v1" rel="alternate" type="text/html"/>
    <published>2026-01-15T10:00:00Z</published>
    <author><name>Jane Researcher</name></author>
  </entry>
</feed>
"""

OPENALEX_JSON = {
    "results": [
        {
            "title": "A Survey of Foundation Models",
            "doi": "https://doi.org/10.1234/abcd",
            "id": "https://openalex.org/W123",
            "publication_date": "2026-02-01",
            "cited_by_count": 42,
            "abstract_inverted_index": {"Foundation": [0], "models": [1], "matter": [2]},
            "authorships": [{"author": {"display_name": "Sam Author"}}],
            "primary_location": {"source": {"display_name": "Journal of AI"}},
        }
    ]
}


# --- pure parsing ---

def test_reconstruct_abstract_orders_words() -> None:
    inv = {"the": [0, 4], "quick": [1], "brown": [2], "fox": [3], "jumps": [5]}
    assert academic.reconstruct_abstract(inv) == "the quick brown fox the jumps"


def test_reconstruct_abstract_empty() -> None:
    assert academic.reconstruct_abstract(None) == ""
    assert academic.reconstruct_abstract({}) == ""


def test_parse_arxiv() -> None:
    hits = academic.parse_arxiv(ARXIV_XML, limit=5)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.title == "Scaling Laws for Neural Language Models"
    assert hit.url == "http://arxiv.org/abs/2001.00001v1"
    assert hit.snippet.startswith("We study empirical scaling laws")
    assert hit.published_at == "2026-01-15T10:00:00+00:00"
    assert hit.authors == ("Jane Researcher",)
    assert hit.provider == "arxiv"


def test_parse_openalex() -> None:
    hits = academic.parse_openalex(OPENALEX_JSON, limit=5)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.title == "A Survey of Foundation Models"
    assert hit.url == "https://doi.org/10.1234/abcd"  # DOI preferred over openalex id
    assert hit.snippet == "Foundation models matter"
    assert hit.published_at == "2026-02-01T00:00:00+00:00"
    assert hit.cited_by_count == 42
    assert hit.venue == "Journal of AI"


def test_fetch_arxiv_uses_client(monkeypatch) -> None:
    monkeypatch.setattr(academic, "shared_async_client", lambda **_k: FakeAsyncClient(text=ARXIV_XML))
    hits = asyncio.run(academic.fetch_arxiv("language models", limit=3))
    assert len(hits) == 1 and hits[0].provider == "arxiv"


def test_fetch_openalex_uses_client(monkeypatch) -> None:
    monkeypatch.setattr(academic, "shared_async_client", lambda **_k: FakeAsyncClient(json_data=OPENALEX_JSON))
    hits = asyncio.run(academic.fetch_openalex("foundation models", limit=3))
    assert len(hits) == 1 and hits[0].provider == "openalex"


# --- adapter query ---

def test_academic_adapter_query_merges_and_dedupes(monkeypatch) -> None:
    async def fake_arxiv(query, **kwargs):
        return [
            AcademicHit("Paper A", "http://arxiv.org/abs/1", "abstract a", "2026-01-10T00:00:00+00:00", "arxiv"),
            AcademicHit("Dup", "https://doi.org/10.1/x", "dup", "2026-01-09T00:00:00+00:00", "arxiv"),
        ]

    async def fake_openalex(query, **kwargs):
        return [
            AcademicHit("Paper B", "https://doi.org/10.2/y", "abstract b", "2026-02-10T00:00:00+00:00", "openalex", cited_by_count=100),
            AcademicHit("Dup", "https://doi.org/10.1/x", "dup", "2026-01-09T00:00:00+00:00", "openalex"),  # same URL as arxiv dup
        ]

    monkeypatch.setattr(academic, "fetch_arxiv", fake_arxiv)
    monkeypatch.setattr(academic, "fetch_openalex", fake_openalex)

    adapter = AcademicSourceAdapter()
    profile = TopicProfile.from_dict({"statement": "LLM scaling", "search_queries": ["llm scaling laws"]})
    candidates = asyncio.run(adapter.query(profile, SourceAdapterContext(exploration_id="x", candidate_limit=10)))

    urls = [c.payload.original_url for c in candidates]
    assert len(candidates) == 3  # one dup removed
    assert urls.count("https://doi.org/10.1/x") == 1
    assert all(isinstance(c, Candidate) for c in candidates)
    assert all(c.adapter == "academic" for c in candidates)
    assert all(c.payload.source_type == "academic_paper" for c in candidates)


def test_academic_adapter_no_queries_returns_empty(monkeypatch) -> None:
    adapter = AcademicSourceAdapter()
    # Empty profile still yields a fallback query; ensure provider failure -> no candidates.
    async def boom(query, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(academic, "fetch_arxiv", boom)
    monkeypatch.setattr(academic, "fetch_openalex", boom)
    profile = TopicProfile.from_dict({"statement": "anything", "search_queries": ["topic"]})
    candidates = asyncio.run(adapter.query(profile, SourceAdapterContext(exploration_id="x", candidate_limit=5)))
    assert candidates == []
