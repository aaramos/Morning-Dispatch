"""Tests for the curated podcast-show subscription model.

Covers: subscribed-show latest-episode inclusion with the 60-day staleness cutoff,
the subscribed-show extraction helper, show discovery for the picker, subscription
persistence, and podcast Top Stories eligibility in the rendered brief.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from backend.agents.digestor import podcast
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery import adapters
from backend.agents.discovery.types import TopicProfile
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.db import database


def _episode(eid: str, *, days_ago: int, audio: str | None = "https://cdn/x.mp3") -> podcast.PodcastEpisode:
    return podcast.PodcastEpisode(
        show_name="Test Show",
        feed_url="https://feed/x",
        episode_id=eid,
        title=f"Episode {eid}",
        description="Episode notes about AI infrastructure and memory.",
        published_at=(datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds"),
        episode_url=f"https://show/{eid}",
        audio_url=audio,
    )


# ---------------------------------------------------------------------------
# Subscribed-show extraction
# ---------------------------------------------------------------------------


def test_subscribed_shows_extracted_from_requested_sources() -> None:
    profile = TopicProfile.from_dict(
        {
            "topic_id": "t1",
            "statement": "AI infrastructure",
            "source_selection": {"podcasts": True},
            "requested_sources": [
                {"adapter": "podcasts", "ref": "Latent Space", "feed_url": "https://feed/latent"},
                {"adapter": "podcasts", "ref": "No Feed Show"},  # ignored (no feed_url)
                {"adapter": "reddit", "ref": "r/ai"},  # ignored (wrong adapter)
            ],
        }
    )
    shows = adapters._subscribed_podcast_shows(profile)
    assert shows == [{"feed_url": "https://feed/latent", "title": "Latent Space"}]


# ---------------------------------------------------------------------------
# Latest-episode inclusion + staleness
# ---------------------------------------------------------------------------


def test_fetch_subscribed_show_latest_uses_freshest_within_staleness(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path / "rt"))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(tmp_path / "rt" / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(tmp_path / "rt" / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_DB_PATH", str(tmp_path / "rt" / "data" / "db" / "md.sqlite3"))
    database.init_database()

    async def fake_fetch_feed_episodes(_client, _source):
        return [_episode("old", days_ago=400), _episode("fresh", days_ago=3), _episode("mid", days_ago=20)]

    monkeypatch.setattr(podcast, "_fetch_feed_episodes", fake_fetch_feed_episodes)

    payloads, _decisions = asyncio.run(
        podcast.fetch_subscribed_show_latest(
            [{"feed_url": "https://feed/x", "title": "Test Show"}],
            digest_id="d1",
            staleness_days=60,
        )
    )
    assert len(payloads) == 1
    assert payloads[0].metadata["podcast_episode_id"] == "fresh"
    assert payloads[0].metadata["subscribed_show"] is True


def test_fetch_subscribed_show_latest_suppresses_stale_show(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path / "rt"))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(tmp_path / "rt" / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(tmp_path / "rt" / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_DB_PATH", str(tmp_path / "rt" / "data" / "db" / "md.sqlite3"))
    database.init_database()

    async def fake_fetch_feed_episodes(_client, _source):
        return [_episode("old", days_ago=120)]

    monkeypatch.setattr(podcast, "_fetch_feed_episodes", fake_fetch_feed_episodes)

    payloads, decisions = asyncio.run(
        podcast.fetch_subscribed_show_latest(
            [{"feed_url": "https://feed/x", "title": "Test Show"}],
            digest_id="d1",
            staleness_days=60,
        )
    )
    assert payloads == []
    assert any(getattr(d, "decision", "") == "stale_show" for d in decisions)


def test_within_staleness_treats_undated_as_stale() -> None:
    assert podcast._within_staleness(None, 60) is False
    assert podcast._within_staleness((datetime.now(UTC) - timedelta(days=5)).isoformat(), 60) is True
    assert podcast._within_staleness((datetime.now(UTC) - timedelta(days=90)).isoformat(), 60) is False


# ---------------------------------------------------------------------------
# Show discovery for the picker
# ---------------------------------------------------------------------------


def test_discover_candidate_shows_returns_summaries(monkeypatch) -> None:
    async def fake_discover_podcasts(query, *, limit=8):
        return [
            {"feed_url": "https://feed/a", "title": "Show A", "description": "Usual content A", "author": "x"},
            {"feed_url": "https://feed/b", "title": "Show B", "description": "Usual content B", "author": "y"},
        ]

    monkeypatch.setattr(podcast, "discover_podcasts", fake_discover_podcasts)
    shows = asyncio.run(podcast.discover_candidate_shows(["ai infrastructure"], enrich=False))
    feeds = {s["feed_url"] for s in shows}
    assert feeds == {"https://feed/a", "https://feed/b"}
    assert all("description" in s for s in shows)


# ---------------------------------------------------------------------------
# Subscription persistence
# ---------------------------------------------------------------------------


def test_save_podcast_subscriptions_persists_feeds(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path / "rt"))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(tmp_path / "rt" / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(tmp_path / "rt" / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_DB_PATH", str(tmp_path / "rt" / "data" / "db" / "md.sqlite3"))
    database.init_database()
    from backend.app.services import explore

    saved = explore.save_topic_profile(
        {"topic_id": "topic-pods", "statement": "AI infra", "source_selection": {"podcasts": True}}
    )
    topic_id = saved["topic_id"]

    explore.save_podcast_subscriptions(
        topic_id,
        [
            {"feed_url": "https://feed/latent", "title": "Latent Space"},
            {"feed_url": "https://feed/latent", "title": "dupe"},  # deduped
            {"feed_url": "https://feed/hardfork", "title": "Hard Fork"},
        ],
    )
    record = database.get_topic_profile(topic_id)
    profile = TopicProfile.from_dict(record["profile"])
    shows = adapters._subscribed_podcast_shows(profile)
    feeds = sorted(s["feed_url"] for s in shows)
    assert feeds == ["https://feed/hardfork", "https://feed/latent"]


# ---------------------------------------------------------------------------
# Top Stories eligibility
# ---------------------------------------------------------------------------


def _podcast_result(score: float) -> ArticleFetchResult:
    payload = NormalizedPayload(
        source_type="podcast_episode",
        source_name="Test Show",
        original_url="https://show/ep",
        metadata={"podcast_episode_id": "ep", "audio_url": "https://cdn/ep.mp3", "title": "Compelling Episode"},
    )
    return ArticleFetchResult(
        payload=payload,
        original_url="https://show/ep",
        final_url="https://show/ep",
        canonical_url="https://show/ep",
        title="Compelling Episode",
        text="A compelling discussion.",
        excerpt="A compelling discussion.",
        editor_summary="A compelling discussion.",
        domain="show",
        status="fetched",
        tier="main",
        link_score=score,
        relevance_score=score,
        content_type="podcast",
    )


def test_compelling_podcast_enters_top_stories() -> None:
    html = database.render_ingested_issue(
        "Podcast Brief",
        "snap",
        [],
        [_podcast_result(0.92)],
        lookback_hours=24,
    )
    top_section = html.split('id="top-stories-heading"', 1)
    assert len(top_section) == 2
    # The compelling podcast leads the brief as a media card (rendered above the
    # top-stories heading as the lead), so its media card + audio are present.
    assert "media-card" in html
    assert "https://cdn/ep.mp3" in html
