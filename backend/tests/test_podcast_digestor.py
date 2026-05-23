from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from backend.agents.digestor import podcast
from backend.app.db import database


def test_parse_podcast_feed_extracts_episode():
    xml = """
    <rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
      <channel>
        <title>AI Daily Brief</title>
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
    assert refreshed_summary["record_count"] == 3
    assert refreshed_summary["status_counts"]["success"] == 2
    assert refreshed_summary["status_counts"]["already_seen"] == 1


def test_apple_url_from_itunes_id():
    assert podcast._apple_url_from_itunes_id(123456789) == "https://podcasts.apple.com/podcast/id123456789"
    assert podcast._apple_url_from_itunes_id("0") is None


def test_discover_podcasts_returns_empty_without_credentials(monkeypatch):
    monkeypatch.delenv("MORNING_DISPATCH_PODCASTINDEX_API_KEY", raising=False)
    monkeypatch.delenv("MORNING_DISPATCH_PODCASTINDEX_API_SECRET", raising=False)

    assert asyncio.run(podcast.discover_podcasts("AI daily brief")) == []
