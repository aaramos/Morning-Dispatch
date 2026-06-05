from __future__ import annotations

import asyncio
import json
import pytest
from types import SimpleNamespace
from datetime import datetime, UTC, timedelta

from backend.agents.discovery import adapters
from backend.agents.discovery.adapters import RedditSourceAdapter
from backend.agents.discovery.types import AdapterUnavailable, Candidate, SourceAdapterContext, TopicProfile
from backend.agents.discovery.reddit_expander import expand_reddit_targets, clean_subreddit_name
import backend.app.services.model_routing as model_routing


def _runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(tmp_path / "data" / "db" / "morning_dispatch.sqlite3"),
    )


# --- Subreddit Cleaner Tests ---

def test_clean_subreddit_name() -> None:
    assert clean_subreddit_name("machinelearning") == "machinelearning"
    assert clean_subreddit_name("/r/machinelearning") == "machinelearning"
    assert clean_subreddit_name("r/machinelearning") == "machinelearning"
    assert clean_subreddit_name("/r/machine_learning") == "machine_learning"
    assert clean_subreddit_name("MachineLearning!") is None
    assert clean_subreddit_name("") is None


# --- Target Expansion Tests ---

def test_expand_reddit_targets_success(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    class FakeClient:
        async def complete_json(self, **kwargs):
            return {
                "subreddits": ["machinelearning", "/r/LocalLLaMA", "invalid-sub!"],
                "search_queries": ["llm fine tuning", "agentic workflows"],
            }

    monkeypatch.setattr(
        model_routing,
        "client_for_agent",
        lambda *_args, **_kwargs: SimpleNamespace(client=FakeClient()),
    )

    profile = TopicProfile.from_dict({
        "statement": "AI coding tools",
        "scope": "agentic AI developments",
        "requested_sources": [
            {"adapter": "reddit", "ref": "/r/artificial"},
            {"adapter": "web_search", "ref": "TechCrunch"},
        ],
    })

    targets = asyncio.run(expand_reddit_targets(profile))

    # artificial is requested, so it goes first and is cleaned
    assert targets.subreddits == ["artificial", "machinelearning", "LocalLLaMA"]
    assert targets.search_queries == ["llm fine tuning", "agentic workflows"]


def test_expand_reddit_targets_fallback(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    class FakeClient:
        async def complete_json(self, **kwargs):
            raise RuntimeError("LLM Failure")

    monkeypatch.setattr(
        model_routing,
        "client_for_agent",
        lambda *_args, **_kwargs: SimpleNamespace(client=FakeClient()),
    )

    profile = TopicProfile.from_dict({
        "statement": "AI coding tools",
        "scope": "agentic AI developments",
        "keywords": ["llm", "coding"],
        "search_queries": ["best ai coding tools 2026"],
        "requested_sources": [{"adapter": "reddit", "ref": "python"}],
    })

    targets = asyncio.run(expand_reddit_targets(profile))

    # fallback keeps requested subreddits
    assert targets.subreddits == ["python"]
    # queries fallback to search_queries + keywords (capped at 5)
    assert targets.search_queries == ["best ai coding tools 2026", "llm", "coding"]


# --- Adapter Query/Fetch Tests ---

def test_reddit_adapter_query_json_success(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    # Fake Reddit HTTP response
    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | list | str, headers: dict | None = None) -> None:
            self.status_code = status_code
            self._payload = payload
            self.headers = headers or {}

        def json(self) -> Any:
            return self._payload

        @property
        def text(self) -> str:
            return str(self._payload)

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url: str, params: dict | None = None, **kwargs) -> FakeResponse:
            if "hot.json" in url:
                # Subreddit fetch
                return FakeResponse(
                    status_code=200,
                    payload={
                        "kind": "Listing",
                        "data": {
                            "children": [
                                {
                                    "kind": "t3",
                                    "data": {
                                        "id": "post1",
                                        "title": "LLM Coding Assistant",
                                        "selftext": "Check out this coding assistant built locally.",
                                        "permalink": "/r/python/comments/post1/llm_coding_assistant/",
                                        "url": "https://www.reddit.com/r/python/comments/post1/llm_coding_assistant/",
                                        "score": 150,
                                        "upvote_ratio": 0.95,
                                        "num_comments": 25,
                                        "link_flair_text": "Showcase",
                                        "subreddit": "python",
                                        "created_utc": datetime.now(UTC).timestamp() - 3600,
                                        "is_self": True,
                                    }
                                }
                            ]
                        }
                    }
                )
            elif "search.json" in url:
                # Search fetch
                return FakeResponse(
                    status_code=200,
                    payload={
                        "kind": "Listing",
                        "data": {
                            "children": [
                                {
                                    "kind": "t3",
                                    "data": {
                                        "id": "post2",
                                        "title": "LocalLLaMA is awesome",
                                        "selftext": "",
                                        "permalink": "/r/LocalLLaMA/comments/post2/localllama_is_awesome/",
                                        "url": "https://example.com/external-article",
                                        "score": 80,
                                        "upvote_ratio": 0.90,
                                        "num_comments": 15,
                                        "link_flair_text": "Discussion",
                                        "subreddit": "LocalLLaMA",
                                        "created_utc": datetime.now(UTC).timestamp() - 7200,
                                        "is_self": False,
                                    }
                                }
                            ]
                        }
                    }
                )
            elif "/comments/" in url:
                # Comments fetch
                return FakeResponse(
                    status_code=200,
                    payload=[
                        {},
                        {
                            "kind": "Listing",
                            "data": {
                                "children": [
                                    {
                                        "kind": "t1",
                                        "data": {
                                            "author": "user_a",
                                            "body": "This is a great comment.",
                                            "score": 15,
                                            "distinguished": None,
                                            "replies": {
                                                "kind": "Listing",
                                                "data": {
                                                    "children": [
                                                        {
                                                            "kind": "t1",
                                                            "data": {
                                                                "author": "user_b",
                                                                "body": "Totally agree with user_a.",
                                                                "score": 5,
                                                                "distinguished": None,
                                                                "replies": "",
                                                            }
                                                        }
                                                    ]
                                                }
                                            }
                                        }
                                    },
                                    {
                                        "kind": "t1",
                                        "data": {
                                            "author": "AutoModerator",
                                            "body": "Automoderator warning",
                                            "score": 1,
                                            "distinguished": "moderator",
                                            "replies": "",
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                )
            return FakeResponse(status_code=404, payload={})

    # Mock expansion
    async def mock_expand(*_args) -> Any:
        return SimpleNamespace(
            subreddits=["python"],
            search_queries=["localllama"]
        )

    monkeypatch.setattr(adapters, "expand_reddit_targets", mock_expand)
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    adapter = RedditSourceAdapter()
    candidates = asyncio.run(
        adapter.query(
            TopicProfile.from_dict({"statement": "AI Coding", "scope": "AI Coding"}),
            SourceAdapterContext(exploration_id="explore-test", lookback_hours=24),
        )
    )

    # Verify we got both posts (1 from subreddit browse, 1 from search)
    assert len(candidates) == 2
    
    # Check Candidate 1 (python self post)
    cand1 = next(c for c in candidates if c.payload.metadata["subreddit"] == "python")
    assert cand1.payload.source_name == "r/python"
    assert cand1.payload.original_url == "https://www.reddit.com/r/python/comments/post1/llm_coding_assistant/"
    assert "LLM Coding Assistant" in cand1.payload.raw_text
    assert "--- Top Comments ---" in cand1.payload.raw_text
    assert "[user_a (15 pts)]: This is a great comment." in cand1.payload.raw_text
    assert "  [user_b (5 pts)]: Totally agree with user_a." in cand1.payload.raw_text
    assert "AutoModerator" not in cand1.payload.raw_text  # AutoMod filtered out
    assert cand1.score > 0.0
    
    # Check Candidate 2 (LocalLLaMA link post)
    cand2 = next(c for c in candidates if c.payload.metadata["subreddit"] == "LocalLLaMA")
    assert cand2.payload.source_name == "r/LocalLLaMA"
    assert cand2.payload.original_url == "https://example.com/external-article"
    assert cand2.payload.metadata["discussion_url"] == "https://www.reddit.com/r/LocalLLaMA/comments/post2/localllama_is_awesome/"


def test_reddit_adapter_rss_fallback(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    class FakeResponse:
        def __init__(self, status_code: int, text_content: str) -> None:
            self.status_code = status_code
            self._text = text_content

        @property
        def text(self) -> str:
            return self._text

    rss_payload = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>hot posts in r/python</title>
      <entry>
        <title>Python RSS Fallback Post</title>
        <link href="https://www.reddit.com/r/python/comments/rss1/fallback/"/>
        <content type="html">&lt;p&gt;This is parsed via feedparser fallback&lt;/p&gt;</content>
        <published>2026-06-05T20:50:00+00:00</published>
        <author><name>/u/author_rss</name></author>
      </entry>
    </feed>"""

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            if "hot.json" in url:
                # Simulate a block/403
                return FakeResponse(status_code=403, text_content="Forbidden")
            elif "hot/.rss" in url:
                return FakeResponse(status_code=200, text_content=rss_payload)
            return FakeResponse(status_code=404, text_content="")

    async def mock_expand(*_args) -> Any:
        return SimpleNamespace(
            subreddits=["python"],
            search_queries=[]
        )

    monkeypatch.setattr(adapters, "expand_reddit_targets", mock_expand)
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    adapter = RedditSourceAdapter()
    candidates = asyncio.run(
        adapter.query(
            TopicProfile.from_dict({"statement": "AI Coding", "scope": "AI Coding"}),
            SourceAdapterContext(exploration_id="explore-test", lookback_hours=24),
        )
    )

    assert len(candidates) == 1
    assert candidates[0].payload.source_name == "r/python"
    assert candidates[0].payload.original_url == "https://www.reddit.com/r/python/comments/rss1/fallback/"
    assert "Python RSS Fallback Post" in candidates[0].payload.raw_text


def test_reddit_adapter_query_refinement(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    # Initial search returns 0 results, forcing query refinement.
    # Refinement query search will then return 1 result.
    refinement_queries_called = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url: str, params: dict | None = None, **kwargs) -> FakeResponse:
            if "hot.json" in url:
                return FakeResponse(status_code=200, payload={"kind": "Listing", "data": {"children": []}})
            elif "search.json" in url:
                q = (params or {}).get("q", "")
                if "refined" in q:
                    return FakeResponse(
                        status_code=200,
                        payload={
                            "kind": "Listing",
                            "data": {
                                "children": [
                                    {
                                        "kind": "t3",
                                        "data": {
                                            "id": "refpost1",
                                            "title": "Refined query success",
                                            "selftext": "Successfully found via refinement.",
                                            "permalink": "/r/artificial/comments/refpost1/refined_success/",
                                            "url": "https://www.reddit.com/r/artificial/comments/refpost1/refined_success/",
                                            "score": 35,
                                            "upvote_ratio": 0.88,
                                            "num_comments": 2,
                                            "link_flair_text": None,
                                            "subreddit": "artificial",
                                            "created_utc": datetime.now(UTC).timestamp() - 3600,
                                            "is_self": True,
                                        }
                                    }
                                ]
                            }
                        }
                    )
                return FakeResponse(status_code=200, payload={"kind": "Listing", "data": {"children": []}})
            elif "/comments/" in url:
                return FakeResponse(status_code=200, payload=[{}, {"kind": "Listing", "data": {"children": []}}])
            return FakeResponse(status_code=404, payload={})

    async def mock_expand(*_args) -> Any:
        return SimpleNamespace(
            subreddits=["python"],
            search_queries=["original_query"]
        )

    async def mock_refine_queries(adapter_name, profile, initial_results, initial_queries, lookback_hours) -> list[str]:
        refinement_queries_called.append("called")
        return ["refined_query"]

    monkeypatch.setattr(adapters, "expand_reddit_targets", mock_expand)
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("backend.agents.discovery.query_refiner.refine_queries_for_adapter", mock_refine_queries)

    adapter = RedditSourceAdapter()
    candidates = asyncio.run(
        adapter.query(
            TopicProfile.from_dict({"statement": "AI Coding", "scope": "AI Coding"}),
            SourceAdapterContext(exploration_id="explore-test", lookback_hours=24),
        )
    )

    assert len(refinement_queries_called) == 1
    assert len(candidates) == 1
    assert candidates[0].payload.metadata["is_refined_query"] is True
    assert candidates[0].payload.metadata["subreddit"] == "artificial"


def test_reddit_adapter_low_yield_empty_raises_unavailable(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    # Search returns empty even after refinement
    class FakeResponse:
        def json(self) -> dict:
            return {"kind": "Listing", "data": {"children": []}}

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, *args, **kwargs) -> FakeResponse:
            return FakeResponse()

    async def mock_expand(*_args) -> Any:
        return SimpleNamespace(
            subreddits=["python"],
            search_queries=["original_query"]
        )

    async def mock_refine_queries(*args, **kwargs) -> list[str]:
        return ["refined_query"]

    monkeypatch.setattr(adapters, "expand_reddit_targets", mock_expand)
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("backend.agents.discovery.query_refiner.refine_queries_for_adapter", mock_refine_queries)

    adapter = RedditSourceAdapter()
    with pytest.raises(AdapterUnavailable, match="Reddit adapter found zero recent candidates"):
        asyncio.run(
            adapter.query(
                TopicProfile.from_dict({"statement": "AI Coding", "scope": "AI Coding"}),
                SourceAdapterContext(exploration_id="explore-test", lookback_hours=24),
            )
        )
