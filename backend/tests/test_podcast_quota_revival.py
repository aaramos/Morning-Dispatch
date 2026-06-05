from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import pytest
from dataclasses import replace

from backend.agents.digestor import podcast
from backend.agents.discovery.types import TopicProfile
from backend.agents.discovery.web_search import SearchHit
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.digestor.base import NormalizedPayload
from backend.app.db import database
from backend.app.services import explore


def configure_runtime(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")


def article_for_quota(
    *,
    title: str,
    url: str,
    source_type: str = "podcast_episode",
    score: float = 0.5,
    status: str = "fetched",
    tier: str = "main",
) -> ArticleFetchResult:
    payload = NormalizedPayload(
        source_type=source_type,
        source_name="Test Show",
        original_url=url,
        raw_text="Test Episode Content",
        published_at=datetime.now(UTC).isoformat(),
        metadata={"podcast_title": "Test Show", "title": title, "feed_url": "https://example.com/feed.xml"}
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=url,
        final_url=url,
        canonical_url=url,
        title=title,
        text="Test Episode Content",
        excerpt="Test Episode Content",
        editor_summary="Test Episode Content",
        domain="example.com",
        status=status,
        link_score=score,
        tier=tier,
    )


def test_enforce_inclusion_limits_revives_podcasts(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    profile = TopicProfile.from_dict({
        "topic_id": "test-topic",
        "statement": "AI agents and trends",
        "source_selection": {"podcasts": True},
        "content_limits": {
            "min_items": {"podcasts": 5},
            "per_source": {"podcasts": 5}
        }
    })

    # Create 3 active podcasts, and 4 dropped podcasts (2 passing safety check, 2 failing)
    results = [
        article_for_quota(title="Active 1", url="https://example.com/a1", score=0.8, tier="main"),
        article_for_quota(title="Active 2", url="https://example.com/a2", score=0.7, tier="main"),
        article_for_quota(title="Active 3", url="https://example.com/a3", score=0.6, tier="main"),
        # Dropped, passes safety check (link_score >= 0.22, status != "error")
        article_for_quota(title="Dropped Safe 1", url="https://example.com/d1", score=0.35, tier="dropped"),
        article_for_quota(title="Dropped Safe 2", url="https://example.com/d2", score=0.30, tier="dropped"),
        # Dropped, fails safety check (low score)
        article_for_quota(title="Dropped Low Score", url="https://example.com/d3", score=0.15, tier="dropped"),
        # Dropped, fails safety check (status == error)
        article_for_quota(title="Dropped Error", url="https://example.com/d4", score=0.45, status="error", tier="dropped"),
    ]

    enforced = explore._enforce_inclusion_limits(profile, results)

    # We needed 5, had 3 active.
    # We should have revived the 2 safe dropped ones ("Dropped Safe 1" and "Dropped Safe 2").
    # The active count should now be 5.
    active_items = [r for r in enforced if r.tier != "dropped"]
    assert len(active_items) == 5
    active_titles = {r.title for r in active_items}
    assert "Active 1" in active_titles
    assert "Active 2" in active_titles
    assert "Active 3" in active_titles
    assert "Dropped Safe 1" in active_titles
    assert "Dropped Safe 2" in active_titles
    assert "Dropped Low Score" not in active_titles
    assert "Dropped Error" not in active_titles


def test_episode_first_search_and_resolve_negative_constraints_and_caching(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    # Create a profile with query expansions and negative constraints
    profile = TopicProfile.from_dict({
        "topic_id": "test-topic-1",
        "statement": "AI agents and trends",
        "scope": "AI agents and trends",
        "direct_episode_queries": ["AI agents", "LLM coding"],
        "related_episode_queries": ["Developer agents"],
        "negative_constraints": ["blockchain", "crypto"],
        "priority_terms": ["OpenAI"],
    })

    # Mock search_web to return a mix of good and bad hits
    async def fake_search_web(query, limit, days=None):
        return [
            SearchHit(title="Good AI agents show", url="https://podcasts.example.com/good", snippet="All about OpenAI agents"),
            SearchHit(title="Bad blockchain trend podcast", url="https://podcasts.example.com/blockchain", snippet="Discussing crypto trends"),
        ]

    monkeypatch.setattr("backend.agents.discovery.web_search.search_web", fake_search_web)

    # Mock _screen_episodes_with_agent to keep everything
    async def fake_screen(hits, **kwargs):
        return hits

    monkeypatch.setattr(podcast, "_screen_episodes_with_agent", fake_screen)

    # Mock _resolve_feed_url to return a mock feed
    async def fake_resolve_feed(*args, **kwargs):
        return "https://podcasts.example.com/feed.xml"

    monkeypatch.setattr(podcast, "_resolve_feed_url", fake_resolve_feed)

    # Mock feed fetch response to parse_podcast_feed
    class FakeResponse:
        def __init__(self, text):
            self.text = text
            self.status_code = 200
        def raise_for_status(self):
            pass

    async def fake_client_get(self, url, **kwargs):
        # Return an XML representing the feed
        now_gmt = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        xml = f"""
        <rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
          <channel>
            <title>AI Daily Brief</title>
            <item>
              <title>Good AI agents show</title>
              <description>All about OpenAI agents</description>
              <pubDate>{now_gmt}</pubDate>
              <link>https://podcasts.example.com/good</link>
              <guid>episode-good</guid>
              <enclosure url="https://cdn.example.com/good.mp3" type="audio/mpeg" />
            </item>
            <item>
              <title>Bad blockchain trend podcast</title>
              <description>Discussing crypto trends</description>
              <pubDate>{now_gmt}</pubDate>
              <link>https://podcasts.example.com/blockchain</link>
              <guid>episode-bad</guid>
              <enclosure url="https://cdn.example.com/bad.mp3" type="audio/mpeg" />
            </item>
          </channel>
        </rss>
        """
        return FakeResponse(xml)

    monkeypatch.setattr("httpx.AsyncClient.get", fake_client_get)

    decisions = []
    diagnostics = {
        "episode_pages_found": 0,
        "low_relevance_rejects": 0,
        "feed_resolved": 0,
        "episode_matched": 0,
        "no_audio_rejects": 0,
        "date_rejects": 0,
    }

    resolved = asyncio.run(
        podcast._episode_first_search_and_resolve(
            digest_interest="AI agents",
            lookback_hours=48,
            search_sources=[],
            profile=profile,
            decisions=decisions,
            diagnostics=diagnostics,
            max_episodes=5,
        )
    )

    # Verify that the blockchain/crypto hit was filtered out by negative constraints
    assert len(resolved) == 1
    assert resolved[0].title == "Good AI agents show"

    # Verify provenance metadata is stored in resolved episode's metadata
    assert resolved[0].metadata["discovery_query"] is not None
    assert resolved[0].metadata["discovery_query_type"] is not None
    assert resolved[0].metadata["resolution_method"] == "network"

    # Verify that decision logs contain the negative constraint reject decision
    neg_constraint_decisions = [d for d in decisions if d.action == "exclude_negative_constraint"]
    assert len(neg_constraint_decisions) == 1
    assert "blockchain" in neg_constraint_decisions[0].metadata["rejected_constraints"]


def test_episode_first_search_uses_related_and_priority_queries(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    profile = TopicProfile.from_dict({
        "topic_id": "test-topic-queries",
        "statement": "AI agents and trends",
        "direct_episode_queries": ["AI agents", "LLM coding"],
        "related_episode_queries": ["Developer agents"],
        "priority_terms": ["OpenAI"],
    })

    seen_queries = []

    async def fake_search_web(query, limit, days=None):
        seen_queries.append(query)
        return []

    monkeypatch.setattr("backend.agents.discovery.web_search.search_web", fake_search_web)

    decisions = []
    diagnostics = {
        "episode_pages_found": 0,
        "low_relevance_rejects": 0,
        "feed_resolved": 0,
        "episode_matched": 0,
        "no_audio_rejects": 0,
        "date_rejects": 0,
    }

    resolved = asyncio.run(
        podcast._episode_first_search_and_resolve(
            digest_interest="AI agents",
            lookback_hours=48,
            search_sources=[],
            profile=profile,
            decisions=decisions,
            diagnostics=diagnostics,
            max_episodes=5,
        )
    )

    assert resolved == []
    assert any("OpenAI" in query for query in seen_queries)
    assert any("Developer agents" in query for query in seen_queries)


def test_episode_first_search_caches_failed_discovery_queries(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    profile = TopicProfile.from_dict({
        "topic_id": "test-topic-cache",
        "statement": "AI agents",
        "direct_episode_queries": ["AI agents"],
    })
    calls = []

    async def fake_search_web(query, limit, days=None):
        calls.append(query)
        raise RuntimeError("provider down")

    monkeypatch.setattr("backend.agents.discovery.web_search.search_web", fake_search_web)

    diagnostics = {
        "episode_pages_found": 0,
        "low_relevance_rejects": 0,
        "feed_resolved": 0,
        "episode_matched": 0,
        "no_audio_rejects": 0,
        "date_rejects": 0,
    }

    for _ in range(2):
        resolved = asyncio.run(
            podcast._episode_first_search_and_resolve(
                digest_interest="AI agents",
                lookback_hours=48,
                search_sources=[],
                profile=profile,
                decisions=[],
                diagnostics=diagnostics.copy(),
                max_episodes=5,
            )
        )
        assert resolved == []

    assert len(calls) == 2
