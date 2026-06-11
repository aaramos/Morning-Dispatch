from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from datetime import datetime, UTC, timedelta

import pytest

from backend.agents.discovery.adapters import WebSearchSourceAdapter
from backend.agents.discovery import query_refiner
from backend.agents.discovery.types import AdapterUnavailable, Candidate, SourceAdapterContext, TopicProfile
from backend.agents.discovery import web_search


def _runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(tmp_path / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.setenv("MORNING_DISPATCH_BRAVE_API_KEY", "")
    monkeypatch.setenv("MORNING_DISPATCH_SERPAPI_API_KEY", "")
    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "")
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(tmp_path / "missing-hermes.env"))


def test_search_web_is_not_configured_by_default(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    adapter = WebSearchSourceAdapter()
    with pytest.raises(AdapterUnavailable, match="provider is not configured"):
        asyncio.run(
            adapter.query(
                TopicProfile.from_dict({"statement": "AI", "scope": "AI"}),
                SourceAdapterContext(exploration_id="explore-1"),
            )
        )


def test_search_web_uses_tavily_payload_shape(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse(
                {
                    "results": [
                        {
                            "title": "Local AI launch",
                            "url": "https://example.com/a",
                            "content": "A practical AI infrastructure update.",
                            "score": 0.83,
                        },
                    ]
                }
            )

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr(web_search, "shared_async_client", lambda **_kwargs: FakeClient())

    results = asyncio.run(web_search.search_web("local AI", limit=3))

    assert len(results) == 1
    assert results[0].provider == "tavily"
    assert results[0].title == "Local AI launch"
    assert results[0].url == "https://example.com/a"


@pytest.mark.parametrize(
    "provider_alias",
    ["auto", "tavily", "tavily_search", "tavily-search"],
)
def test_search_web_uses_tavily_aliases(monkeypatch, tmp_path, provider_alias: str) -> None:
    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse({"results": []})

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", provider_alias)
    monkeypatch.setattr(web_search, "shared_async_client", lambda **_kwargs: FakeClient())

    results = asyncio.run(web_search.search_web("local AI", limit=3))

    assert isinstance(results, list)
    assert len(results) == 0


def test_search_web_prefers_configured_provider_order(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse(
                {
                    "results": [
                        {
                            "title": "Tavily Source",
                            "url": "https://example.com/t",
                            "content": "Tavily result",
                        }
                    ]
                }
            )

        async def get(self, *_args, **_kwargs) -> FakeResponse:
            raise AssertionError("Brave should not be called while Tavily key is configured")

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_BRAVE_API_KEY", "brave-key")
    monkeypatch.setattr(web_search, "shared_async_client", lambda **_kwargs: FakeClient())

    results = asyncio.run(web_search.search_web("local AI", limit=2))

    assert len(results) == 1
    assert results[0].provider == "tavily"


def test_search_web_uses_brave_aliases(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse(
                {
                    "web": {
                        "results": [
                            {
                                "title": "Brave Story",
                                "url": "https://example.com/b",
                                "description": "A brave source",
                            }
                        ]
                    }
                }
            )

        async def post(self, *_args, **_kwargs) -> FakeResponse:
            raise AssertionError("Tavily should not be called when Brave is explicitly chosen")

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_BRAVE_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(web_search, "shared_async_client", lambda **_kwargs: FakeClient())

    results = asyncio.run(web_search.search_web("local AI", limit=2))

    assert len(results) == 1
    assert results[0].provider == "brave"
    assert results[0].title == "Brave Story"
    assert results[0].url == "https://example.com/b"


def test_search_web_uses_serpapi_aliases(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse(
                {
                    "organic_results": [
                        {
                            "title": "Serp Source",
                            "link": "https://example.com/s",
                            "snippet": "A serp api result",
                        }
                    ]
                }
            )

        async def post(self, *_args, **_kwargs) -> FakeResponse:
            raise AssertionError("Tavily should not be called when SerpAPI is explicitly chosen")

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_SERPAPI_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", "serp-api")
    monkeypatch.setattr(web_search, "shared_async_client", lambda **_kwargs: FakeClient())

    results = asyncio.run(web_search.search_web("local AI", limit=2))

    assert len(results) == 1
    assert results[0].provider == "serpapi"
    assert results[0].title == "Serp Source"
    assert results[0].url == "https://example.com/s"


def test_search_web_uses_serper_aliases(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **kwargs) -> FakeResponse:
            return FakeResponse(
                {
                    "organic": [
                        {
                            "title": "Serper Source",
                            "link": "https://example.com/serper",
                            "snippet": "A serper result",
                        }
                    ]
                }
            )

        async def get(self, *_args, **_kwargs) -> FakeResponse:
            raise AssertionError("Get should not be called for Serper")

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_SERPER_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", "serper")
    monkeypatch.setattr(web_search, "shared_async_client", lambda **_kwargs: FakeClient())

    results = asyncio.run(web_search.search_web("local AI", limit=2))

    assert len(results) == 1
    assert results[0].provider == "serper"
    assert results[0].title == "Serper Source"
    assert results[0].url == "https://example.com/serper"



def test_search_web_rejects_unknown_provider(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", "unsupported_provider")

    with pytest.raises(AdapterUnavailable, match="not configured"):
        asyncio.run(web_search.search_web("local AI", limit=2))


def test_web_search_adapter_maps_hits_to_candidates(monkeypatch, tmp_path) -> None:
    async def fake_search_web(_query: str, *, limit: int, language: str | None = None, days: int | None = None):
        return [
            web_search.SearchHit(
                title="From Adapter",
                url="https://example.com/story",
                snippet="Search result summary.",
                score=0.73,
                provider="fake",
            )
        ]

    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", "auto")
    _runtime(monkeypatch, tmp_path)
    monkeypatch.setattr("backend.agents.discovery.adapters.search_web", fake_search_web)

    adapter = WebSearchSourceAdapter()
    candidates = asyncio.run(
        adapter.query(
            TopicProfile.from_dict({"statement": "AI", "scope": "AI"}),
            SourceAdapterContext(exploration_id="explore-1", candidate_limit=10),
        )
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, Candidate)
    assert candidate.payload.source_type == "gmail_link"
    assert candidate.payload.source_name == "From Adapter"
    assert candidate.payload.original_url == "https://example.com/story"
    assert candidate.payload.metadata["search_provider"] == "fake"


def test_web_search_adapter_query_excludes_avoid_terms(monkeypatch, tmp_path) -> None:
    observed: dict[str, str] = {}

    async def fake_search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None):
        observed["query"] = query
        return []

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("backend.agents.discovery.adapters.search_web", fake_search_web)

    adapter = WebSearchSourceAdapter()
    asyncio.run(
        adapter.query(
            TopicProfile.from_dict(
                {
                    "statement": (
                        "Mexico City; curate a brief on things a traveler might need to know "
                        "to go to mexico city in august 2026. Provide advise on where to stay, "
                        "what to do and things in general area to see. I like hikes, bike rides "
                        "and good food. I also like history and musuems as well as long strolls "
                        "thorugh trendy neighborhoods."
                    ),
                    "scope": "as a 45 year old male traveling to CDMX",
                    "subtopics": ["biking", "good food", "walking tours", "musuems"],
                    "exclusions": ["glbq issues or advice"],
                }
            ),
            SourceAdapterContext(exploration_id="explore-query", candidate_limit=10),
        )
    )

    assert len(observed["query"]) <= 340
    assert "mexico" in observed["query"]
    assert "cdmx" in observed["query"]
    assert "museums" in observed["query"]
    assert "food" in observed["query"]
    assert "Avoid" not in observed["query"]
    assert "glbq" not in observed["query"]


def test_web_search_adapter_prefers_refinement_search_plan(monkeypatch, tmp_path) -> None:
    observed: list[str] = []

    async def fake_search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None):
        observed.append(query)
        return []

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("backend.agents.discovery.adapters.search_web", fake_search_web)

    adapter = WebSearchSourceAdapter()
    asyncio.run(
        adapter.query(
            TopicProfile.from_dict(
                {
                    "statement": "Travel to Mexico City",
                    "scope": "Solo traveler planning",
                    "search_queries": ["Mexico City solo traveler food history biking"],
                    "source_queries": {
                        "web_search": ["Mexico City solo traveler neighborhoods food history biking August 2026"]
                    },
                }
            ),
            SourceAdapterContext(exploration_id="explore-query", candidate_limit=10),
        )
    )

    assert observed[0] == "mexico city solo traveler neighborhoods food history biking august 2026"
    assert "mexico city solo traveler food history biking" in observed


def test_web_search_adapter_fans_out_refinement_queries_and_dedupes(monkeypatch, tmp_path) -> None:
    observed: list[tuple[str, int]] = []

    async def fake_search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None):
        observed.append((query, limit))
        return [
            web_search.SearchHit(
                title=f"{query} primary",
                url=f"https://example.com/{len(observed)}",
                snippet="Useful Mexico City travel result.",
                score=0.7,
                provider="fake",
            ),
            web_search.SearchHit(
                title="Duplicate",
                url="https://example.com/duplicate",
                snippet="Duplicate result.",
                score=0.6,
                provider="fake",
            ),
        ]

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("backend.agents.discovery.adapters.search_web", fake_search_web)

    adapter = WebSearchSourceAdapter()
    candidates = asyncio.run(
        adapter.query(
            TopicProfile.from_dict(
                {
                    "statement": "Travel to Mexico City",
                    "scope": "Solo traveler planning for Mexico City",
                    "search_queries": [
                        "best museums in Mexico City",
                        "Mexico City food tours",
                    ],
                    "source_queries": {
                        "web_search": [
                            "Mexico City walking tours",
                            "Mexico City bike tours",
                        ]
                    },
                }
            ),
            SourceAdapterContext(exploration_id="explore-query", candidate_limit=5),
        )
    )

    queries = [query for query, _limit in observed]
    assert queries[:2] == ["mexico city walking tours", "mexico city bike tours"]
    assert "best museums in mexico city" in queries
    assert "mexico city food tours" in queries
    assert all(limit == 5 for _query, limit in observed)
    assert len(candidates) == 5
    assert len({candidate.payload.original_url for candidate in candidates}) == 5
    assert all(candidate.payload.metadata["search_query"] in queries for candidate in candidates)


def test_web_search_adapter_allows_twenty_refinement_queries(monkeypatch, tmp_path) -> None:
    observed: list[tuple[str, int]] = []

    async def fake_search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None):
        observed.append((query, limit))
        return []

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("backend.agents.discovery.adapters.search_web", fake_search_web)

    adapter = WebSearchSourceAdapter()
    asyncio.run(
        adapter.query(
            TopicProfile.from_dict(
                {
                    "statement": "AI infrastructure",
                    "scope": "AI infrastructure market signals",
                    "search_queries": [f"AI infrastructure query {index}" for index in range(25)],
                }
            ),
            SourceAdapterContext(exploration_id="explore-query", candidate_limit=250),
        )
    )

    assert len(observed) == 20
    assert all(limit == 25 for _query, limit in observed)


def test_search_web_trims_long_provider_queries(monkeypatch, tmp_path) -> None:
    captured: dict[str, str] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": []}

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **kwargs) -> FakeResponse:
            captured["query"] = kwargs["json"]["query"]
            return FakeResponse()

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr(web_search, "shared_async_client", lambda **_kwargs: FakeClient())

    asyncio.run(web_search.search_web("Mexico City travel " * 80, limit=3))

    assert len(captured["query"]) <= 380


def test_web_search_adapter_triggers_query_refinement(monkeypatch, tmp_path) -> None:
    searched_queries = []

    async def fake_search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None):
        searched_queries.append((query, days))
        if "refined" in query:
            return [
                web_search.SearchHit(
                    title="Refined Web Result",
                    url="https://example.com/refined-web-story",
                    snippet="Snippet of refined result.",
                    score=0.85,
                    provider="fake-refined",
                )
            ]
        return []

    async def fake_refine_queries_for_adapter(
        adapter_name: str,
        profile: TopicProfile,
        initial_results: list,
        initial_queries: list[str],
        lookback_hours: int | None = None,
    ) -> list[str]:
        assert lookback_hours == 24
        return ["refined web query"]

    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_WEB_SEARCH_PROVIDER", "auto")
    _runtime(monkeypatch, tmp_path)
    monkeypatch.setattr("backend.agents.discovery.adapters.search_web", fake_search_web)
    monkeypatch.setattr("backend.agents.discovery.query_refiner.refine_queries_for_adapter", fake_refine_queries_for_adapter)

    adapter = WebSearchSourceAdapter()
    candidates = asyncio.run(
        adapter.query(
            TopicProfile.from_dict({"statement": "AI", "scope": "AI"}),
            SourceAdapterContext(exploration_id="explore-1", candidate_limit=10, lookback_hours=24),
        )
    )

    assert len(candidates) == 1
    assert candidates[0].payload.source_name == "Refined Web Result"
    assert candidates[0].payload.original_url == "https://example.com/refined-web-story"
    assert candidates[0].payload.metadata["is_refined_query"] is True
    assert searched_queries[0][0] == "ai"
    assert searched_queries[1][0] == "refined web query"
    # lookback_hours=24 maps to days=1 and remains strict on the refinement retry.
    assert searched_queries[1][1] == 1


def test_query_refiner_includes_date_and_recency_constraints(monkeypatch, tmp_path) -> None:
    captured: dict[str, str] = {}

    class FakeClient:
        async def complete_json(self, **kwargs):
            captured["system"] = kwargs["system"]
            captured["prompt"] = kwargs["prompt"]
            return {"refined_queries": ["frontier lab capex", "", "frontier lab capex", "model developer compute"]}

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        query_refiner.model_routing,
        "client_for_agent",
        lambda *_args, **_kwargs: SimpleNamespace(client=FakeClient()),
    )

    refined = asyncio.run(
        query_refiner.refine_queries_for_adapter(
            "web_search",
            TopicProfile.from_dict({"statement": "AI infrastructure", "scope": "frontier labs and compute demand"}),
            initial_results=[],
            initial_queries=["AI infrastructure 2026"],
            lookback_hours=168,
        )
    )

    prompt = json.loads(captured["prompt"])
    assert refined == ["frontier lab capex", "model developer compute"]
    assert prompt["lookback_hours"] == 168
    assert "current_date" in prompt
    assert "Do not widen the time window" in captured["system"]


def test_web_search_adapter_refines_on_stale_hits(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    from backend.agents.discovery.web_search import SearchHit

    # Initial search returns only stale (old date) hit
    async def fake_search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None):
        if "refined" in query:
            # Refinement query returns a fresh hit
            return [
                SearchHit(
                    provider="test",
                    title="Fresh refined hit",
                    url="https://example.com/fresh",
                    snippet="Fresh snippet",
                    score=0.9,
                    published_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
            ]
        # Initial query returns stale hit
        return [
            SearchHit(
                provider="test",
                title="Stale initial hit",
                url="https://example.com/stale",
                snippet="Stale snippet",
                score=0.8,
                published_at=(datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds"),
            )
        ]

    monkeypatch.setenv("MORNING_DISPATCH_TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("backend.agents.discovery.adapters.search_web", fake_search_web)

    refinement_queries_called = []
    async def mock_refine_queries(adapter_name, profile, initial_results, initial_queries, lookback_hours) -> list[str]:
        refinement_queries_called.append(initial_results)
        return ["refined_query"]

    monkeypatch.setattr(
        "backend.agents.discovery.query_refiner.refine_queries_for_adapter",
        mock_refine_queries,
    )

    adapter = WebSearchSourceAdapter()
    candidates = asyncio.run(
        adapter.query(
            TopicProfile.from_dict(
                {
                    "statement": "AI infrastructure",
                    "scope": "AI infrastructure market signals",
                    "search_queries": ["initial_query"],
                }
            ),
            SourceAdapterContext(exploration_id="explore-query", candidate_limit=5, lookback_hours=24),
        )
    )

    # Refinement should have been called because the only hit was outside the 24 hour window
    assert len(refinement_queries_called) == 1
    # Check that we got candidates from both initial and refined results
    assert len(candidates) == 2
