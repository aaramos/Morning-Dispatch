from __future__ import annotations

import asyncio

from backend.agents.digestor import reddit as reddit_digestor
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
