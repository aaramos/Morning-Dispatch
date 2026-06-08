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

# --- Adapter Query/Fetch Tests ---

def test_reddit_adapter_query_rss_success(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    # Mock search hits from web search (since search.json is bypassed)
    from backend.agents.discovery.web_search import SearchHit
    
    async def mock_search_web(query: str, limit: int, days: int | None = None) -> list[SearchHit]:
        return [
            SearchHit(
                provider="test",
                title="LocalLLaMA is awesome",
                url="https://www.reddit.com/r/LocalLLaMA/comments/post2/localllama_is_awesome/",
                snippet="Check out this post.",
                score=0.8,
                published_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        ]
        
    class FakeResponse:
        def __init__(self, status_code: int, text_content: str) -> None:
            self.status_code = status_code
            self._text = text_content

        @property
        def text(self) -> str:
            return self._text

    recent_published = datetime.now(UTC).isoformat(timespec="seconds")
    hot_rss = f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>hot posts in r/python</title>
      <entry>
        <title>LLM Coding Assistant</title>
        <link href="https://www.reddit.com/r/python/comments/post1/llm_coding_assistant/"/>
        <content type="html">&lt;p&gt;Check out this coding assistant built locally.&lt;/p></content>
        <published>{recent_published}</published>
        <author><name>/u/user_a</name></author>
      </entry>
    </feed>"""

    comments_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>comments in LLM Coding Assistant</title>
      <entry>
        <title>/u/user_a on LLM Coding Assistant</title>
        <link href="https://www.reddit.com/r/python/comments/post1/llm_coding_assistant/comment1/"/>
        <content type="html">&lt;p&gt;This is a great comment.&lt;/p&gt;</content>
        <author><name>/u/user_a</name></author>
      </entry>
      <entry>
        <title>/u/user_b on LLM Coding Assistant</title>
        <link href="https://www.reddit.com/r/python/comments/post1/llm_coding_assistant/comment2/"/>
        <content type="html">&lt;p&gt;Totally agree with user_a.&lt;/p&gt;</content>
        <author><name>/u/user_b</name></author>
      </entry>
      <entry>
        <title>/u/AutoModerator on LLM Coding Assistant</title>
        <link href="https://www.reddit.com/r/python/comments/post1/llm_coding_assistant/comment3/"/>
        <content type="html">&lt;p&gt;Automoderator message&lt;/p></content>
        <author><name>/u/AutoModerator</name></author>
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
            if "hot/.rss" in url:
                return FakeResponse(status_code=200, text_content=hot_rss)
            elif "/comments/" in url:
                return FakeResponse(status_code=200, text_content=comments_rss)
            return FakeResponse(status_code=404, text_content="")

    # Mock expansion
    async def mock_expand(*_args) -> Any:
        return SimpleNamespace(
            subreddits=["python"],
            search_queries=["localllama"]
        )

    async def mock_refine_queries(*args, **kwargs) -> list[str]:
        return []

    monkeypatch.setattr(adapters, "expand_reddit_targets", mock_expand)
    monkeypatch.setattr(adapters, "search_web", mock_search_web)
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    monkeypatch.setattr(
        "backend.agents.discovery.query_refiner.refine_queries_for_adapter",
        mock_refine_queries,
    )

    adapter = RedditSourceAdapter()
    candidates = asyncio.run(
        adapter.query(
            TopicProfile.from_dict({"statement": "AI Coding", "scope": "AI Coding"}),
            SourceAdapterContext(exploration_id="explore-test", lookback_hours=24),
        )
    )

    # Verify we got both posts (1 from subreddit browse RSS, 1 from web search)
    assert len(candidates) == 2
    
    # Check Candidate 1 (python self post)
    cand1 = next(c for c in candidates if c.payload.metadata["subreddit"] == "python")
    assert cand1.payload.source_name == "r/python"
    assert cand1.payload.original_url == "https://www.reddit.com/r/python/comments/post1/llm_coding_assistant/"
    assert "LLM Coding Assistant" in cand1.payload.raw_text
    assert "--- Top Comments ---" in cand1.payload.raw_text
    assert "[user_a]: This is a great comment." in cand1.payload.raw_text
    assert "[user_b]: Totally agree with user_a." in cand1.payload.raw_text
    assert "AutoModerator" not in cand1.payload.raw_text  # AutoMod filtered out
    assert cand1.score > 0.0
    
    # Check Candidate 2 (LocalLLaMA link post from web search)
    cand2 = next(c for c in candidates if c.payload.metadata["subreddit"] == "LocalLLaMA")
    assert cand2.payload.source_name == "r/LocalLLaMA"
    assert cand2.payload.original_url == "https://www.reddit.com/r/LocalLLaMA/comments/post2/localllama_is_awesome/"


def test_reddit_adapter_query_refinement(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    from backend.agents.discovery.web_search import SearchHit
    refinement_queries_called = []
    
    async def mock_search_web(query: str, limit: int, days: int | None = None) -> list[SearchHit]:
        if "refined" in query:
            return [
                SearchHit(
                    provider="test",
                    title="Refined query success",
                    url="https://www.reddit.com/r/artificial/comments/refpost1/refined_success/",
                    snippet="Successfully found via refinement.",
                    score=0.9,
                    published_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
            ]
        return []

    class FakeResponse:
        def __init__(self, status_code: int, text_content: str) -> None:
            self.status_code = status_code
            self._text = text_content

        @property
        def text(self) -> str:
            return self._text

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            # Empty feeds for initial subreddits to trigger low-yield refinement
            return FakeResponse(status_code=200, text_content="""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>""")

    async def mock_expand(*_args) -> Any:
        return SimpleNamespace(
            subreddits=["python"],
            search_queries=["original_query"]
        )

    async def mock_refine_queries(adapter_name, profile, initial_results, initial_queries, lookback_hours) -> list[str]:
        refinement_queries_called.append("called")
        return ["refined_query"]

    monkeypatch.setattr(adapters, "expand_reddit_targets", mock_expand)
    monkeypatch.setattr(adapters, "search_web", mock_search_web)
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
    async def mock_search_web(*args, **kwargs) -> list:
        return []

    class FakeResponse:
        def __init__(self, *args, **kwargs) -> None:
            pass
        @property
        def text(self) -> str:
            return """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>"""
        @property
        def status_code(self) -> int:
            return 200

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
    monkeypatch.setattr(adapters, "search_web", mock_search_web)
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
