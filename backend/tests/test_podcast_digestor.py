from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from backend.agents.digestor import podcast
from backend.app.db import database


def test_parse_podcast_feed_extracts_episode():
    xml = """
    <rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
      <channel>
        <title>AI Daily Brief</title>
        <itunes:image href="https://podcasts.example.com/artwork.jpg" />
        <item>
          <title>OpenAI ships a new agent workflow</title>
          <description><![CDATA[<p>Agents, coding workflows, and local model updates.</p>]]></description>
          <pubDate>Fri, 22 May 2026 12:30:00 GMT</pubDate>
          <link>https://podcasts.example.com/openai-agent-workflow</link>
          <guid>episode-1</guid>
          <enclosure url="https://cdn.example.com/audio.mp3" type="audio/mpeg" />
          <itunes:duration>12:30</itunes:duration>
        </item>
      </channel>
    </rss>
    """

    episodes = podcast.parse_podcast_feed(
        xml,
        feed_url="https://podcasts.example.com/feed.xml",
    )

    assert len(episodes) == 1
    assert episodes[0].show_name == "AI Daily Brief"
    assert episodes[0].title == "OpenAI ships a new agent workflow"
    assert episodes[0].description == "Agents, coding workflows, and local model updates."
    assert episodes[0].episode_url == "https://podcasts.example.com/openai-agent-workflow"
    assert episodes[0].audio_url == "https://cdn.example.com/audio.mp3"
    assert episodes[0].image_url == "https://podcasts.example.com/artwork.jpg"
    assert episodes[0].duration_seconds == 750
    assert episodes[0].published_at == "2026-05-22T12:30:00+00:00"


def test_fetch_podcast_episode_payload_uses_show_notes(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.delenv("MORNING_DISPATCH_PODCAST_TRANSCRIBE_COMMAND", raising=False)
    database.init_database()
    digest = database.create_digest(
        {
            "name": "AI Morning Brief",
            "interest": "agentic AI product strategy OpenAI local LLM infrastructure",
            "schedule": "daily",
            "sources": [],
        }
    )

    published_at = datetime.now(UTC).isoformat(timespec="seconds")
    episode = podcast.PodcastEpisode(
        show_name="AI Daily Brief",
        feed_url="https://podcasts.example.com/feed.xml",
        episode_id="episode-1",
        title="Agentic AI workflows for product teams",
        description="A discussion of agentic AI, product strategy, OpenAI, and local LLM infrastructure.",
        published_at=published_at,
        episode_url="https://podcasts.example.com/agentic-ai-workflows",
        audio_url="https://cdn.example.com/audio.mp3",
        duration_seconds=1800,
        apple_podcasts_url="https://podcasts.apple.com/podcast/id123456789",
        image_url="https://podcasts.example.com/artwork.jpg",
    )

    async def fake_fetch_feed_episodes(_client, _source):
        return [episode]

    monkeypatch.setattr(podcast, "_fetch_feed_episodes", fake_fetch_feed_episodes)

    payloads, decisions = asyncio.run(
        podcast.fetch_podcast_episodes(
            digest_id=digest["id"],
            digest_interest="agentic AI product strategy OpenAI local LLM infrastructure",
            sources=[
                {
                    "type": "podcast_rss",
                    "title": "AI Daily Brief",
                    "feed_url": "https://podcasts.example.com/feed.xml",
                }
            ],
            lookback_hours=24,
            inference_run_id="inference-1",
        )
    )

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload.source_type == "podcast_episode"
    assert payload.source_name == "AI Daily Brief"
    assert payload.original_url == "https://podcasts.apple.com/podcast/id123456789"
    assert "Show notes:" in payload.raw_text
    assert payload.metadata["podcast_episode_id"] == "episode-1"
    assert payload.metadata["apple_podcasts_url"] == "https://podcasts.apple.com/podcast/id123456789"
    assert payload.metadata["image_url"] == "https://podcasts.example.com/artwork.jpg"
    assert payload.metadata["transcript_source"] == "show_notes"
    assert any(decision.agent == "podcast_scout" for decision in decisions)
    summary = database.podcast_metrics_summary()
    assert summary["record_count"] == 1
    assert summary["status_counts"]["success"] == 1
    assert summary["transcript_source_counts"]["show_notes"] == 1
    assert summary["recent"][0]["inference_run_id"] == "inference-1"
    assert summary["recent"][0]["episode_id"] == "episode-1"
    assert summary["recent"][0]["feed_fetch_ms"] is not None
    assert summary["recent"][0]["transcript_words"] > 0

    second_payloads, _second_decisions = asyncio.run(
        podcast.fetch_podcast_episodes(
            digest_id=digest["id"],
            digest_interest="agentic AI product strategy OpenAI local LLM infrastructure",
            sources=[
                {
                    "type": "podcast_rss",
                    "title": "AI Daily Brief",
                    "feed_url": "https://podcasts.example.com/feed.xml",
                }
            ],
            lookback_hours=24,
            inference_run_id="inference-2",
        )
    )

    assert second_payloads == []
    updated_summary = database.podcast_metrics_summary()
    assert updated_summary["record_count"] == 2
    assert updated_summary["status_counts"]["already_seen"] == 1

    included_seen_payloads, included_seen_decisions = asyncio.run(
        podcast.fetch_podcast_episodes(
            digest_id=digest["id"],
            digest_interest="agentic AI product strategy OpenAI local LLM infrastructure",
            sources=[
                {
                    "type": "podcast_rss",
                    "title": "AI Daily Brief",
                    "feed_url": "https://podcasts.example.com/feed.xml",
                }
            ],
            lookback_hours=24,
            inference_run_id="inference-2a",
            include_seen=True,
        )
    )

    assert len(included_seen_payloads) == 1
    assert any(decision.action == "reuse_cached_episode" for decision in included_seen_decisions)

    unpublished_retry_payloads, _unpublished_retry_decisions = asyncio.run(
        podcast.fetch_podcast_episodes(
            digest_id=digest["id"],
            digest_interest="agentic AI product strategy OpenAI local LLM infrastructure",
            sources=[
                {
                    "type": "podcast_rss",
                    "title": "AI Daily Brief",
                    "feed_url": "https://podcasts.example.com/feed.xml",
                }
            ],
            lookback_hours=24,
            inference_run_id="inference-2b",
            seen_requires_published=True,
        )
    )

    assert len(unpublished_retry_payloads) == 1

    refreshed_payloads, _refreshed_decisions = asyncio.run(
        podcast.fetch_podcast_episodes(
            digest_id=digest["id"],
            digest_interest="agentic AI product strategy OpenAI local LLM infrastructure",
            sources=[
                {
                    "type": "podcast_rss",
                    "title": "AI Daily Brief",
                    "feed_url": "https://podcasts.example.com/feed.xml",
                }
            ],
            lookback_hours=24,
            inference_run_id="inference-3",
            force_refresh=True,
        )
    )

    assert len(refreshed_payloads) == 1
    refreshed_summary = database.podcast_metrics_summary()
    assert refreshed_summary["record_count"] == 5
    assert refreshed_summary["status_counts"]["success"] == 4
    assert refreshed_summary["status_counts"]["already_seen"] == 1


def test_apple_url_from_itunes_id():
    assert podcast._apple_url_from_itunes_id(123456789) == "https://podcasts.apple.com/podcast/id123456789"
    assert podcast._apple_url_from_itunes_id("0") is None


def test_discover_podcasts_returns_empty_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("MORNING_DISPATCH_PODCASTINDEX_API_KEY", raising=False)
    monkeypatch.delenv("MORNING_DISPATCH_PODCASTINDEX_API_SECRET", raising=False)
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(tmp_path / "secrets"))

    assert asyncio.run(podcast.discover_podcasts("AI daily brief")) == []


def test_discovery_query_extracts_tokens_without_whitelist():
    query = podcast._discovery_query("climate change and global warming impacts")
    assert query == "climate change global warming impacts"
    assert podcast._discovery_query("and the") == "podcast"


def test_episode_first_search_and_resolve_flow(monkeypatch, tmp_path):
    # Set up environment
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    database.init_database()
    digest = database.create_digest(
        {
            "name": "AI Morning Brief",
            "interest": "agentic coding workflows local inference",
            "schedule": "daily",
            "sources": [],
        }
    )

    from backend.agents.discovery.web_search import SearchHit
    from backend.agents.discovery.types import TopicProfile

    # 1. Mock search_web
    mock_hits = [
        SearchHit(
            title="Episode 42: Agentic Workflows with DeepMind",
            url="https://podcasts.apple.com/us/podcast/id123456789",
            snippet="A deep discussion on agentic workflows and local coding models.",
            score=0.9,
            provider="brave",
        )
    ]
    async def fake_search_web(*args, **kwargs):
        return mock_hits

    monkeypatch.setattr("backend.agents.discovery.web_search.search_web", fake_search_web)

    # 2. Mock relevance agent (LLM client)
    class FakeRelevanceClient:
        async def complete_json(self, **kwargs):
            return {
                "decisions": [
                    {
                        "index": 0,
                        "decision": "keep",
                        "score": 0.88,
                        "reason": "Highly relevant to agentic coding.",
                    }
                ]
            }

    monkeypatch.setattr(
        "backend.app.services.model_routing.client_for_agent",
        lambda *_args, **_kwargs: type("Res", (), {"client": FakeRelevanceClient()})(),
    )

    # 3. Mock HTTP requests for iTunes lookup and RSS parsing
    itunes_response = {
        "results": [
            {
                "feedUrl": "https://feeds.example.com/podcast.xml",
                "collectionViewUrl": "https://podcasts.apple.com/us/podcast/id123456789",
            }
        ]
    }

    from datetime import datetime, UTC
    from email.utils import format_datetime
    pub_date_str = format_datetime(datetime.now(UTC))

    podcast_rss_xml = f"""
    <rss version="2.0">
      <channel>
        <title>The Agentic Developer</title>
        <item>
          <title>Episode 42: Agentic Workflows with DeepMind</title>
          <link>https://podcasts.apple.com/us/podcast/id123456789</link>
          <description>A deep discussion on agentic workflows and local coding models.</description>
          <pubDate>{pub_date_str}</pubDate>
          <enclosure url="https://cdn.example.com/episode42.mp3" type="audio/mpeg" length="45000000"/>
          <guid>episode-42</guid>
        </item>
      </channel>
    </rss>
    """

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def get(self, url, *args, **kwargs):
            class MockResponse:
                def __init__(self, text_val, json_val):
                    self.text = text_val
                    self._json = json_val
                    self.status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return self._json

            if "itunes.apple.com" in url:
                return MockResponse("", itunes_response)
            else:
                return MockResponse(podcast_rss_xml, {})

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    # 4. Execute fetch_podcast_episodes
    profile = TopicProfile.from_dict({
        "statement": "agentic coding workflows",
        "scope": "local coding automation",
    })

    payloads, decisions = asyncio.run(
        podcast.fetch_podcast_episodes(
            digest_id=digest["id"],
            digest_interest="agentic coding workflows",
            sources=[
                {
                    "type": "podcast_search",
                    "title": "agentic coding",
                    "query": "agentic coding",
                }
            ],
            lookback_hours=72,
            profile=profile,
            include_seen=True,
        )
    )

    # 5. Assertions
    assert len(payloads) == 1
    assert payloads[0].source_name == "The Agentic Developer"
    assert payloads[0].original_url == "https://podcasts.apple.com/us/podcast/id123456789"
    assert payloads[0].metadata["audio_url"] == "https://cdn.example.com/episode42.mp3"
    assert payloads[0].metadata["podcast_episode_id"] is not None

    # Check decisions
    decision_actions = {d.action for d in decisions}
    assert "keep_episode_candidate" in decision_actions
    assert "itunes_lookup" in decision_actions
    assert "report_diagnostics" in decision_actions


def test_resolve_feed_url_via_rss_autodiscovery(monkeypatch):
    from backend.agents.digestor import podcast_resolution

    async def fake_discover_podcasts(*args, **kwargs):
        return []
    # _resolve_feed_url lives in podcast_resolution (re-exported by podcast), so
    # its Podcast Index lookup must be patched where it is looked up.
    monkeypatch.setattr(podcast_resolution, "discover_podcasts", fake_discover_podcasts)

    class MockClient:
        async def get(self, url, *args, **kwargs):
            class MockResponse:
                def __init__(self):
                    self.status_code = 200
                    self.text = """
                    <html>
                      <head>
                        <link rel="alternate" type="application/rss+xml" title="Podcast RSS" href="https://example.com/discovered_feed.xml">
                      </head>
                    </html>
                    """
                def raise_for_status(self):
                    pass
            return MockResponse()

    decisions = []
    feed_url = asyncio.run(
        podcast._resolve_feed_url(
            client=MockClient(),
            url="https://example.com/episode-page",
            title="Some Episode Title",
            decisions=decisions,
        )
    )
    
    assert feed_url == "https://example.com/discovered_feed.xml"
    assert len(decisions) == 1
    assert decisions[0].action == "rss_autodiscovery"
    assert decisions[0].metadata["feed_url"] == "https://example.com/discovered_feed.xml"


def test_resolve_feed_url_via_rss_web_search(monkeypatch):
    from backend.agents.discovery.web_search import SearchHit
    import httpx
    
    mock_hits = [
        SearchHit(
            title="The Feed page",
            url="https://feeds.example.com/podcast.xml",
            snippet="Feed XML RSS url",
            score=0.9,
            provider="brave",
        )
    ]
    async def fake_search_web(*args, **kwargs):
        return mock_hits

    monkeypatch.setattr("backend.agents.discovery.web_search.search_web", fake_search_web)

    decisions = []
    class MockClient:
        async def get(self, url, *args, **kwargs):
            class MockResponse:
                def __init__(self):
                    self.status_code = 404
                    self.text = ""
                def raise_for_status(self):
                    raise httpx.HTTPStatusError("Not Found", request=None, response=None)
            return MockResponse()

    feed_url = asyncio.run(
        podcast._resolve_feed_url(
            client=MockClient(),
            url="https://example.com/episode-page",
            title="Clean Show Title - Episode 1",
            decisions=decisions,
        )
    )
    
    assert feed_url == "https://feeds.example.com/podcast.xml"
    assert len(decisions) == 1
    assert decisions[0].action == "rss_web_search"
    assert decisions[0].metadata["feed_url"] == "https://feeds.example.com/podcast.xml"


def test_episode_first_diagnostics_reports_resolution_failures(monkeypatch, tmp_path):
    import httpx
    # Set up environment
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    database.init_database()
    digest = database.create_digest(
        {
            "name": "AI Morning Brief",
            "interest": "agentic workflows",
            "schedule": "daily",
            "sources": [],
        }
    )

    from backend.agents.discovery.web_search import SearchHit
    from backend.agents.discovery.types import TopicProfile

    mock_hits = [
        SearchHit(
            title="Episode 42: Agentic Workflows with DeepMind",
            url="https://example.com/unresolvable-page",
            snippet="A deep discussion.",
            score=0.9,
            provider="brave",
        )
    ]
    async def fake_search_web(*args, **kwargs):
        return mock_hits

    monkeypatch.setattr("backend.agents.discovery.web_search.search_web", fake_search_web)

    class FakeRelevanceClient:
        async def complete_json(self, **kwargs):
            return {
                "decisions": [
                    {
                        "index": 0,
                        "decision": "keep",
                        "score": 0.88,
                        "reason": "Highly relevant.",
                    }
                ]
            }

    monkeypatch.setattr(
        "backend.app.services.model_routing.client_for_agent",
        lambda *_args, **_kwargs: type("Res", (), {"client": FakeRelevanceClient()})(),
    )

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def get(self, url, *args, **kwargs):
            class MockResponse:
                def __init__(self):
                    self.status_code = 404
                    self.text = ""
                def raise_for_status(self):
                    raise httpx.HTTPStatusError("Not Found", request=None, response=None)
            return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    profile = TopicProfile.from_dict({
        "statement": "agentic workflows",
        "scope": "local coding automation",
    })

    payloads, decisions = asyncio.run(
        podcast.fetch_podcast_episodes(
            digest_id=digest["id"],
            digest_interest="agentic workflows",
            sources=[
                {
                    "type": "podcast_search",
                    "title": "agentic",
                    "query": "agentic",
                }
            ],
            lookback_hours=72,
            profile=profile,
            include_seen=True,
        )
    )

    assert len(payloads) == 0
    diag_decision = next((d for d in decisions if d.decision == "diagnostics"), None)
    assert diag_decision is not None
    assert diag_decision.metadata["episode_pages_found"] == 1
    assert diag_decision.metadata["feed_resolved"] == 0
    assert diag_decision.metadata["episode_matched"] == 0


def test_podcast_relevance_drop_decision_is_rejected(monkeypatch):
    from backend.agents.discovery.web_search import SearchHit

    mock_hits = [
        SearchHit(
            title="Episode 42: Agentic Workflows with DeepMind",
            url="https://podcasts.apple.com/us/podcast/id123456789",
            snippet="A deep discussion on agentic workflows and local coding models.",
            score=0.9,
            provider="brave",
        )
    ]
    
    class FakeRelevanceClient:
        async def complete_json(self, **kwargs):
            return {
                "decisions": [
                    {
                        "index": 0,
                        "decision": "drop",
                        "score": 0.1,
                        "reason": "off topic"
                    }
                ]
            }

    monkeypatch.setattr(
        "backend.app.services.model_routing.client_for_agent",
        lambda *_args, **_kwargs: type("Res", (), {"client": FakeRelevanceClient()})(),
    )

    decisions = []
    diagnostics = {
        "episode_pages_found": 0,
        "low_relevance_rejects": 0,
        "feed_resolved": 0,
        "episode_matched": 0,
        "no_audio_rejects": 0,
        "date_rejects": 0,
    }

    kept = asyncio.run(
        podcast._screen_episodes_with_agent(
            hits=mock_hits,
            digest_interest="agentic coding workflows",
            profile=None,
            decisions=decisions,
            diagnostics=diagnostics,
        )
    )

    assert len(kept) == 0
    assert diagnostics["low_relevance_rejects"] == 1
    
    decision_actions = {d.action for d in decisions}
    assert "drop_episode_candidate" in decision_actions
    assert not any(d.decision == "keep_uncertain" for d in decisions)


def test_podcast_relevance_screening_falls_back_at_deadline(monkeypatch):
    from backend.agents.discovery.web_search import SearchHit

    mock_hits = [
        SearchHit(
            title="Agentic AI workflows for local LLMs",
            url="https://example.com/podcast/agentic-ai",
            snippet="A podcast conversation about agentic AI workflows and local LLM infrastructure.",
            score=0.9,
            provider="brave",
        )
    ]

    class SlowRelevanceClient:
        async def complete_json(self, **kwargs):
            await asyncio.sleep(60)
            return {"decisions": []}

    monkeypatch.setattr(
        "backend.app.services.model_routing.client_for_agent",
        lambda *_args, **_kwargs: type("Res", (), {"client": SlowRelevanceClient()})(),
    )

    decisions = []
    diagnostics = {
        "episode_pages_found": 0,
        "low_relevance_rejects": 0,
        "feed_resolved": 0,
        "episode_matched": 0,
        "no_audio_rejects": 0,
        "date_rejects": 0,
    }

    started = time.monotonic()
    kept = asyncio.run(
        podcast._screen_episodes_with_agent(
            hits=mock_hits,
            digest_interest="agentic AI local LLM infrastructure",
            profile=None,
            decisions=decisions,
            diagnostics=diagnostics,
            deadline=time.monotonic() + 0.01,
        )
    )

    assert time.monotonic() - started < 0.5
    assert kept == mock_hits


def test_episode_first_search_respects_deadline(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_DB_PATH", str(runtime / "data" / "db" / "morning_dispatch.sqlite3"))
    database.init_database()

    async def slow_search_web(*args, **kwargs):
        await asyncio.sleep(60)
        return []

    monkeypatch.setattr("backend.agents.discovery.web_search.search_web", slow_search_web)

    decisions = []
    diagnostics = {
        "episode_pages_found": 0,
        "low_relevance_rejects": 0,
        "feed_resolved": 0,
        "episode_matched": 0,
        "no_audio_rejects": 0,
        "date_rejects": 0,
    }

    started = time.monotonic()
    episodes = asyncio.run(
        podcast._episode_first_search_and_resolve(
            digest_interest="agentic AI local LLM infrastructure",
            lookback_hours=24,
            search_sources=[{"type": "podcast_search", "title": "Agentic AI", "query": "Agentic AI"}],
            profile=None,
            decisions=decisions,
            diagnostics=diagnostics,
            deadline=time.monotonic() + 0.01,
        )
    )

    assert time.monotonic() - started < 0.5
    assert episodes == []


def test_fetch_podcast_episodes_returns_partial_when_deadline_passed(monkeypatch, tmp_path):
    """An expired overall deadline returns partial results instead of timing out."""
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_DB_PATH", str(runtime / "data" / "db" / "morning_dispatch.sqlite3"))
    database.init_database()

    episode = podcast.PodcastEpisode(
        show_name="AI Daily Brief",
        feed_url="https://podcasts.example.com/feed.xml",
        episode_id="episode-1",
        title="Agentic AI workflows",
        description="A discussion of agentic AI and local LLM infrastructure.",
        published_at=datetime.now(UTC).isoformat(timespec="seconds"),
        episode_url="https://podcasts.example.com/agentic-ai",
        audio_url="https://cdn.example.com/audio.mp3",
        duration_seconds=1800,
    )

    async def fake_fetch_feed_episodes(_client, _source):
        return [episode]

    def explode(*_args, **_kwargs):  # episode processing must NOT run past the deadline
        raise AssertionError("ranked loop should break before processing when the deadline passed")

    monkeypatch.setattr(podcast, "_fetch_feed_episodes", fake_fetch_feed_episodes)
    monkeypatch.setattr(podcast, "_episode_text", explode)

    payloads, _decisions = asyncio.run(
        podcast.fetch_podcast_episodes(
            digest_id="digest-deadline",
            digest_interest="agentic AI local LLM infrastructure",
            sources=[{"type": "podcast_rss", "title": "AI Daily Brief", "feed_url": "https://podcasts.example.com/feed.xml"}],
            lookback_hours=24,
            deadline=time.monotonic() - 1,  # already expired
        )
    )
    # The lane returns (empty) partial results instead of raising / hanging.
    assert payloads == []



