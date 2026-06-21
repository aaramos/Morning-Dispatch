from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from backend.agents.discovery import hacker_news
from backend.agents.discovery.hacker_news import HackerNewsHit, HackerNewsSourceAdapter
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


HN_JSON = {
    "hits": [
        {
            "objectID": "111",
            "title": "Show HN: A new vector database",
            "url": "https://example.com/vectordb",
            "points": 320,
            "num_comments": 145,
            "created_at_i": 1780000000,
        },
        {
            "objectID": "222",
            "title": "Ask HN: How do you scale Postgres?",
            "url": "",
            "story_text": "I have a 2TB table and...",
            "points": 90,
            "num_comments": 60,
            "created_at_i": 1780000500,
        },
    ]
}


def test_parse_hn_maps_fields_and_text_post_url() -> None:
    hits = hacker_news.parse_hn(HN_JSON, limit=10)
    assert len(hits) == 2
    first, second = hits
    assert first.title == "Show HN: A new vector database"
    assert first.url == "https://example.com/vectordb"
    assert first.points == 320 and first.num_comments == 145
    assert first.published_at == datetime.fromtimestamp(1780000000, tz=UTC).isoformat(timespec="seconds")
    # Text post with no URL falls back to the HN item page.
    assert second.url == "https://news.ycombinator.com/item?id=222"
    assert second.snippet == "I have a 2TB table and..."


def test_fetch_hn_uses_client(monkeypatch) -> None:
    monkeypatch.setattr(hacker_news, "shared_async_client", lambda **_k: FakeAsyncClient(HN_JSON))
    hits = asyncio.run(hacker_news.fetch_hn("vector database", limit=5, since_ts=123))
    assert len(hits) == 2


def test_hacker_news_adapter_query_sorts_and_dedupes(monkeypatch) -> None:
    def hit(object_id, title, url, points, num_comments):
        return HackerNewsHit(
            object_id=object_id,
            title=title,
            url=url,
            snippet=title,
            points=points,
            num_comments=num_comments,
            published_at=None,
        )

    async def fake_fetch(query, **kwargs):
        return [
            hit("1", "Low", "https://a.com/low", 5, 1),
            hit("2", "High", "https://a.com/high", 400, 200),
            hit("3", "Dup", "https://a.com/high", 10, 2),
        ]

    monkeypatch.setattr(hacker_news, "fetch_hn", fake_fetch)
    adapter = HackerNewsSourceAdapter()
    profile = TopicProfile.from_dict({"statement": "databases", "search_queries": ["vector database"]})
    candidates = asyncio.run(adapter.query(profile, SourceAdapterContext(exploration_id="x", candidate_limit=10)))

    assert len(candidates) == 2  # dup by URL removed
    assert candidates[0].payload.metadata["title"] == "High"  # highest points+comments first
    assert all(isinstance(c, Candidate) for c in candidates)
    assert all(c.adapter == "hacker_news" for c in candidates)
    assert all(c.payload.source_type == "hacker_news_story" for c in candidates)


def test_hacker_news_adapter_handles_provider_failure(monkeypatch) -> None:
    async def boom(query, **kwargs):
        raise RuntimeError("algolia down")

    monkeypatch.setattr(hacker_news, "fetch_hn", boom)
    adapter = HackerNewsSourceAdapter()
    profile = TopicProfile.from_dict({"statement": "x", "search_queries": ["y"]})
    candidates = asyncio.run(adapter.query(profile, SourceAdapterContext(exploration_id="x", candidate_limit=5)))
    assert candidates == []
