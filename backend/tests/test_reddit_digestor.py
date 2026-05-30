from __future__ import annotations

import asyncio

from backend.agents.digestor import reddit as reddit_digestor
from backend.agents.digestor import reddit_mcp_client
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import fetch_articles_for_payloads


def test_fetch_reddit_threads_uses_source_scout_states(monkeypatch) -> None:
    monkeypatch.setattr(
        reddit_digestor.database,
        "list_reddit_sources",
        lambda *_args, **_kwargs: [
            {"subreddit": "ollama", "state": "active", "score": 0.74, "category": "Privacy"},
            {"subreddit": "Cursor", "state": "search_only", "score": 0.62, "category": "Workflows"},
            {"subreddit": "Midjourney", "state": "candidate", "score": 0.44, "category": "Creative"},
        ],
    )

    calls: dict[str, object] = {}

    async def fake_browse(subreddit: str, **kwargs):
        calls["browse"] = (subreddit, kwargs)
        return [
            {
                "id": "thread-1",
                "title": "Local coding agents are getting useful with small LLMs",
                "subreddit": "ollama",
                "author": "builder",
                "score": 80,
                "num_comments": 30,
                "created_utc": 1779430367,
                "permalink": "https://reddit.com/r/ollama/comments/thread-1/local_agents/",
                "content": "A practical comparison of local LLM agent workflows using Ollama and MCP tools.",
            },
            {
                "id": "thread-2",
                "title": "Weekend photos",
                "subreddit": "ollama",
                "author": "photo",
                "score": 2,
                "num_comments": 0,
                "created_utc": 1779430367,
                "permalink": "https://reddit.com/r/ollama/comments/thread-2/photos/",
                "content": "Off topic camera gear.",
            },
        ]

    async def fake_search(query: str, **kwargs):
        calls["search"] = (query, kwargs)
        return [
            {
                "id": "thread-3",
                "title": "Cursor agent workflows for product teams",
                "subreddit": "Cursor",
                "author": "pm",
                "score": 44,
                "num_comments": 18,
                "created_utc": 1779430367,
                "permalink": "https://reddit.com/r/Cursor/comments/thread-3/workflows/",
                "content": "Users compare AI coding workflows and agent reliability.",
            }
        ]

    monkeypatch.setattr(reddit_digestor.reddit_mcp_client, "browse_subreddit", fake_browse)
    monkeypatch.setattr(reddit_digestor.reddit_mcp_client, "search_reddit", fake_search)

    payloads = asyncio.run(
        reddit_digestor.fetch_reddit_threads(
            digest_id="digest-1",
            digest_interest="local LLM coding agents and product workflows",
            lookback_hours=24,
            max_threads=5,
        )
    )

    assert {payload.metadata["reddit_thread_id"] for payload in payloads} == {"thread-1", "thread-3"}
    assert all(payload.source_type == "reddit_thread" for payload in payloads)
    assert calls["browse"][0] == "ollama"
    assert calls["search"][1]["subreddits"] == ["Cursor"]


def test_reddit_payload_materializes_as_article_result() -> None:
    payload = NormalizedPayload(
        source_type="reddit_thread",
        source_name="r/ollama",
        raw_text="Local model thread. Builders compare small LLM coding agents and workflow reliability.",
        original_url="https://reddit.com/r/ollama/comments/thread-1/local_agents/",
        published_at="2026-05-22T12:00:00+00:00",
        metadata={
            "reddit_thread_id": "thread-1",
            "title": "Local coding agents are getting useful",
            "thread_quality_score": 0.71,
        },
    )

    results = asyncio.run(fetch_articles_for_payloads([payload]))

    assert len(results) == 1
    assert results[0].fetched is True
    assert results[0].title == "Local coding agents are getting useful"
    assert results[0].payload.source_type == "reddit_thread"
    assert results[0].content_type == "reddit_thread"


def test_reddit_mcp_client_uses_jordanburke_tool_contract(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((tool_name, arguments))
        if tool_name in {"fetch_reddit_hot_threads", "browse_subreddit"}:
            raise RuntimeError("legacy tool unavailable")
        return {
            "text": """# Top Posts from r/LocalLLaMA (week)

### 1. MLX and llama.cpp benchmark notes
- Author: u/builder
- Score: 123 (97.0% upvoted)
- Comments: 45
- Posted: 5/29/2026, 1:00:00 PM
- Link: https://reddit.com/r/LocalLLaMA/comments/abc123/mlx_bench/
"""
        }

    monkeypatch.setattr(reddit_mcp_client, "_call_reddit_tool", fake_call)

    posts = asyncio.run(reddit_mcp_client.browse_subreddit("LocalLLaMA", time="week", limit=5))

    assert calls == [
        ("fetch_reddit_hot_threads", {"subreddit": "LocalLLaMA", "limit": 5}),
        (
            "browse_subreddit",
            {
                "subreddit": "LocalLLaMA",
                "sort": "top",
                "time": "week",
                "limit": 5,
                "include_nsfw": False,
                "include_subreddit_info": False,
            },
        ),
        ("get_top_posts", {"subreddit": "LocalLLaMA", "time_filter": "week", "limit": 5}),
    ]
    assert posts[0]["title"] == "MLX and llama.cpp benchmark notes"
    assert posts[0]["subreddit"] == "LocalLLaMA"
    assert posts[0]["score"] == 123
    assert posts[0]["num_comments"] == 45
    assert posts[0]["id"] == "abc123"


def test_reddit_mcp_client_searches_subreddits_one_at_a_time(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((tool_name, arguments))
        if "subreddits" in arguments:
            raise RuntimeError("jordan server expects one subreddit at a time")
        if tool_name == "fetch_reddit_hot_threads":
            raise RuntimeError("new server unavailable in this test")
        subreddit = str(arguments.get("subreddit"))
        return {
            "text": f"""# Reddit Search Results for: "local LLM" in r/{subreddit}

### 1. {subreddit} local model discussion
- Author: u/builder
- Subreddit: r/{subreddit}
- Score: 90 (95.0% upvoted)
- Comments: 20
- Posted: 5/29/2026, 2:00:00 PM
- Link: https://reddit.com/r/{subreddit}/comments/{subreddit.lower()}1/thread/
"""
        }

    monkeypatch.setattr(reddit_mcp_client, "_call_reddit_tool", fake_call)

    posts = asyncio.run(
        reddit_mcp_client.search_reddit("local LLM", subreddits=["LocalLLaMA", "MachineLearning"], limit=10)
    )

    per_subreddit_calls = [call for call in calls if call[0] == "search_reddit" and "subreddit" in call[1]]
    assert [call[1]["subreddit"] for call in per_subreddit_calls] == ["LocalLLaMA", "MachineLearning"]
    assert all(call[1]["time_filter"] == "week" for call in per_subreddit_calls)
    assert {post["subreddit"] for post in posts} == {"LocalLLaMA", "MachineLearning"}


def test_reddit_mcp_client_uses_adhikasp_hot_threads_contract(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((tool_name, arguments))
        return {
            "text": """Title: MLX benchmark notes
Score: 123
Comments: 45
Author: builder
Type: text
Content: Local builders compare MLX and llama.cpp.
Link: https://reddit.com/r/LocalLLaMA/comments/abc123/mlx_bench/
---"""
        }

    monkeypatch.setattr(reddit_mcp_client, "_call_reddit_tool", fake_call)

    posts = asyncio.run(reddit_mcp_client.browse_subreddit("LocalLLaMA", time="week", limit=5))

    assert calls == [("fetch_reddit_hot_threads", {"subreddit": "LocalLLaMA", "limit": 5})]
    assert posts[0]["title"] == "MLX benchmark notes"
    assert posts[0]["subreddit"] == "LocalLLaMA"
    assert posts[0]["score"] == 123
    assert posts[0]["num_comments"] == 45
    assert posts[0]["id"] == "abc123"


def test_reddit_mcp_client_keeps_reddit_buddy_contract(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((tool_name, arguments))
        return {
            "results": [
                {
                    "id": "abc123",
                    "title": "Local model discussion",
                    "subreddit": "LocalLLaMA",
                    "author": "builder",
                    "score": 10,
                    "num_comments": 5,
                }
            ]
        }

    monkeypatch.setattr(reddit_mcp_client, "_call_reddit_tool", fake_call)

    posts = asyncio.run(reddit_mcp_client.search_reddit("local LLM", subreddits=["LocalLLaMA"], limit=10))

    assert calls == [
        (
            "search_reddit",
            {
                "query": "local LLM",
                "sort": "relevance",
                "time_filter": "week",
                "limit": 10,
                "type": "link",
                "subreddits": ["LocalLLaMA"],
            },
        )
    ]
    assert posts[0]["id"] == "abc123"
