from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery import adapters, youtube
from backend.agents.discovery.adapters import YouTubeSourceAdapter
from backend.agents.discovery.types import AdapterUnavailable, SourceAdapterContext, TopicProfile
from backend.agents.librarian.articles import ArticleFetchResult, direct_article_results
from backend.app.core.config import get_settings
from backend.app.db import database
from backend.app.main import create_app


def _runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))
    monkeypatch.delenv("MORNING_DISPATCH_YOUTUBE_API_KEY", raising=False)
    return runtime


def test_search_youtube_requires_api_key(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    with pytest.raises(AdapterUnavailable, match="API key"):
        asyncio.run(youtube.search_youtube(api_key=None, query="local AI", limit=3))


def test_search_youtube_maps_videos_and_records_quota(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, **_kwargs: object) -> FakeResponse:
            if url == youtube.YOUTUBE_SEARCH_ENDPOINT:
                return FakeResponse(
                    {
                        "items": [
                            {"id": {"videoId": "video-1"}},
                            {"id": {"videoId": "video-2"}},
                        ]
                    }
                )
            return FakeResponse(
                {
                    "items": [
                        {
                            "id": "video-2",
                            "snippet": {
                                "title": "Second AI Systems Talk",
                                "channelTitle": "Systems Channel",
                                "publishedAt": "2026-05-20T12:00:00Z",
                                "description": "A second relevant talk.",
                                "thumbnails": {"high": {"url": "https://img.example.com/2.jpg"}},
                            },
                            "contentDetails": {"duration": "PT12M03S"},
                        },
                        {
                            "id": "video-1",
                            "snippet": {
                                "title": "Local AI Systems Talk",
                                "channelTitle": "Local AI Channel",
                                "publishedAt": "2026-05-21T12:00:00Z",
                                "description": "A relevant talk.",
                                "thumbnails": {"medium": {"url": "https://img.example.com/1.jpg"}},
                            },
                            "contentDetails": {"duration": "PT1H02M"},
                        },
                    ]
                }
            )

    _runtime(monkeypatch, tmp_path)
    database.init_database()
    monkeypatch.setattr(youtube.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(youtube.search_youtube(api_key="key", query="local AI", limit=2))

    assert result.quota_units == 101
    assert [video.video_id for video in result.videos] == ["video-1", "video-2"]
    assert result.videos[0].title == "Local AI Systems Talk"
    assert result.videos[0].duration_seconds == 3720
    assert database.youtube_quota_summary()["units_used"] == 101


def test_youtube_adapter_maps_transcripts_to_candidates(monkeypatch, tmp_path) -> None:
    async def fake_search_youtube(**_kwargs: object) -> youtube.YouTubeSearchResult:
        return youtube.YouTubeSearchResult(
            videos=(
                youtube.YouTubeVideo(
                    video_id="video-1",
                    title="Local AI Systems Talk",
                    channel_name="Local AI Channel",
                    published_at="2026-05-21T12:00:00Z",
                    description="A relevant talk.",
                    thumbnail_url="https://img.example.com/1.jpg",
                    duration_seconds=900,
                    score=0.91,
                ),
            ),
            quota_units=101,
        )

    async def fake_fetch_transcript(_video_id: str) -> youtube.YouTubeTranscript:
        return youtube.YouTubeTranscript(
            text="Local AI systems need careful source-aware evaluation. " * 6,
            segments=({"start_seconds": 0, "text": "Local AI systems need careful evaluation."},),
        )

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_YOUTUBE_API_KEY", "key")
    monkeypatch.setattr(adapters, "search_youtube", fake_search_youtube)
    monkeypatch.setattr(adapters, "fetch_youtube_transcript", fake_fetch_transcript)

    candidates = asyncio.run(
        YouTubeSourceAdapter().query(
            TopicProfile.from_dict(
                {
                    "statement": "local AI systems",
                    "scope": "local AI systems",
                    "source_selection": {"youtube": True},
                }
            ),
            SourceAdapterContext(exploration_id="explore-1", candidate_limit=3),
        )
    )

    assert len(candidates) == 1
    assert candidates[0].adapter == "youtube"
    assert candidates[0].payload.source_type == "youtube_video"
    assert candidates[0].payload.source_name == "Local AI Channel"
    assert candidates[0].payload.original_url == "https://www.youtube.com/watch?v=video-1"
    assert candidates[0].payload.metadata["youtube_title"] == "Local AI Systems Talk"
    assert candidates[0].payload.metadata["transcript_source"] == "native"


def test_youtube_credentials_enable_source_status(monkeypatch, tmp_path) -> None:
    runtime = _runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        before = client.get("/api/explore/source-status")
        assert before.status_code == 200
        assert before.json()["sources"]["youtube"]["enabled"] is False

        saved = client.post("/api/admin/youtube/credentials", json={"api_key": " youtube-key "})
        assert saved.status_code == 200
        assert saved.json()["configured"] is True

        after = client.get("/api/explore/source-status")
        assert after.status_code == 200
        assert after.json()["sources"]["youtube"]["enabled"] is True

    assert (runtime / "secrets" / "youtube" / "api_key").read_text(encoding="utf-8") == "youtube-key\n"
    assert get_settings().youtube_api_key == "youtube-key"


def test_youtube_payloads_are_direct_brief_inputs() -> None:
    payload = NormalizedPayload(
        source_type="youtube_video",
        source_name="Local AI Channel",
        raw_text="Transcript text about local AI evaluation and deployment. " * 8,
        original_url="https://www.youtube.com/watch?v=video-1",
        metadata={"youtube_quality_score": 0.91, "youtube_title": "Local AI Systems Talk"},
    )

    results = direct_article_results([payload])

    assert len(results) == 1
    assert results[0].section == "YouTube Videos"
    assert results[0].content_type == "video"
    assert results[0].link_score == 0.91


def test_youtube_brief_card_opens_video_review_modal() -> None:
    payload = NormalizedPayload(
        source_type="youtube_video",
        source_name="Local AI Channel",
        raw_text="Transcript text about local AI evaluation and deployment. " * 12,
        original_url="https://www.youtube.com/watch?v=video-1",
        published_at="2026-05-21T12:00:00Z",
        metadata={
            "video_id": "video-1",
            "youtube_quality_score": 0.91,
            "youtube_title": "Local AI Systems Talk",
            "channel_name": "Local AI Channel",
            "duration_seconds": 905,
            "youtube_url": "https://www.youtube.com/watch?v=video-1",
        },
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url="https://www.youtube.com/watch?v=video-1",
        final_url="https://www.youtube.com/watch?v=video-1",
        canonical_url="https://www.youtube.com/watch?v=video-1",
        title="Local AI Systems Talk",
        text=payload.raw_text,
        excerpt="AI summary for a transcript-backed video.",
        domain="youtube.com",
        status="fetched",
        link_score=0.91,
        section="YouTube Videos",
        content_type="video",
        editor_summary="AI summary for a transcript-backed video.",
    )

    html = database.render_ingested_issue(
        "YouTube Brief",
        "Video-heavy brief",
        [payload],
        [result],
        lookback_hours=48,
    )

    assert "data-youtube-modal-target" in html
    assert "youtube-modal" in html
    assert "youtube-player" in html
    assert "https://www.youtube-nocookie.com/embed/video-1?rel=0" in html
    assert "https://img.youtube.com/vi/video-1/hqdefault.jpg" in html
    assert "Local AI Systems Talk" in html
    assert "AI summary for a transcript-backed video." in html
    assert "Transcript text about local AI evaluation and deployment." in html
    assert "Watch" in html


def test_youtube_adapter_inclusion_transcript_unavailable(monkeypatch, tmp_path) -> None:
    async def fake_search_youtube(**_kwargs: object) -> youtube.YouTubeSearchResult:
        return youtube.YouTubeSearchResult(
            videos=(
                youtube.YouTubeVideo(
                    video_id="video-no-transcript",
                    title="Local AI Systems Talk (No Transcript)",
                    channel_name="Local AI Channel",
                    published_at="2026-05-21T12:00:00Z",
                    description="A metadata description of the talk.",
                    thumbnail_url="https://img.example.com/1.jpg",
                    duration_seconds=900,
                    score=0.91,
                ),
            ),
            quota_units=101,
        )

    async def fake_fetch_transcript(_video_id: str) -> youtube.YouTubeTranscript | None:
        return None

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_YOUTUBE_API_KEY", "key")
    monkeypatch.setattr(adapters, "search_youtube", fake_search_youtube)
    monkeypatch.setattr(adapters, "fetch_youtube_transcript", fake_fetch_transcript)

    candidates = asyncio.run(
        YouTubeSourceAdapter().query(
            TopicProfile.from_dict(
                {
                    "statement": "local AI systems",
                    "scope": "local AI systems",
                    "source_selection": {"youtube": True},
                }
            ),
            SourceAdapterContext(exploration_id="explore-1", candidate_limit=3),
        )
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.adapter == "youtube"
    assert candidate.payload.source_type == "youtube_video"
    assert candidate.payload.source_name == "Local AI Channel"
    assert candidate.payload.original_url == "https://www.youtube.com/watch?v=video-no-transcript"
    assert candidate.payload.metadata["youtube_title"] == "Local AI Systems Talk (No Transcript)"
    assert candidate.payload.metadata["transcript_source"] == "unavailable"
    assert candidate.payload.metadata["content_basis"] == "youtube_metadata"
    assert candidate.payload.metadata["description"] == "A metadata description of the talk."
    assert candidate.payload.raw_text == "A metadata description of the talk."


def test_youtube_librarian_prompt_metadata_only() -> None:
    from backend.agents.librarian.enrichment import _librarian_prompt
    
    payload = NormalizedPayload(
        source_type="youtube_video",
        source_name="Local AI Channel",
        raw_text="A metadata description of the talk.",
        original_url="https://www.youtube.com/watch?v=video-no-transcript",
        published_at="2026-05-21T12:00:00Z",
        metadata={
            "youtube_quality_score": 0.91,
            "video_id": "video-no-transcript",
            "youtube_title": "Local AI Systems Talk (No Transcript)",
            "channel_name": "Local AI Channel",
            "thumbnail_url": "https://img.example.com/1.jpg",
            "duration_seconds": 900,
            "transcript_source": "unavailable",
            "content_basis": "youtube_metadata",
            "description": "A metadata description of the talk.",
            "youtube_url": "https://www.youtube.com/watch?v=video-no-transcript",
        },
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url=payload.original_url,
        final_url=payload.original_url,
        title="Local AI Systems Talk (No Transcript)",
        text=payload.raw_text,
        excerpt="A metadata description of the talk.",
        domain="youtube.com",
        status="fetched",
        link_score=0.91,
    )
    
    prompt = _librarian_prompt(result)
    assert "You are summarizing a YouTube video based ONLY on its metadata" in prompt
    assert "Title: Local AI Systems Talk (No Transcript)" in prompt
    assert "Channel: Local AI Channel" in prompt
    assert "Metadata Description:" in prompt
    assert "Do NOT imply the video was watched" in prompt


def test_youtube_presets_saving_and_loading(monkeypatch, tmp_path) -> None:
    from backend.app.services.brief_settings import (
        brief_settings_status,
        save_brief_defaults,
    )
    
    _runtime(monkeypatch, tmp_path)
    database.init_database()
    settings = get_settings()
    
    status = brief_settings_status(settings)
    assert status["youtube_presets"] == {"max": 20, "large": 16, "medium": 12, "focused": 8}
    assert status["podcast_presets"] == {"max": 5, "large": 4, "medium": 3, "focused": 2}
    assert status["gmail_presets"] == {"max": 40, "large": 32, "medium": 24, "focused": 16}
    
    # Save modified defaults
    modified_defaults = {
        "lookback_hours": 168,
        "content_limits": {
            "total_items": 150,
            "target_items": 25,
            "lead_items": 5,
            "quality_floor": "standard",
            "per_source": {"youtube": 8},
        },
        "youtube_presets": {"max": 10, "large": 9, "medium": 6, "focused": 4},
        "podcast_presets": {"max": 10, "large": 7, "medium": 4, "focused": 2},
        "gmail_presets": {"max": 12, "large": 10, "medium": 8, "focused": 6},
    }
    
    updated_status = save_brief_defaults(settings, modified_defaults)
    assert updated_status["youtube_presets"] == {"max": 10, "large": 9, "medium": 6, "focused": 4}
    assert updated_status["podcast_presets"] == {"max": 5, "large": 5, "medium": 4, "focused": 2}
    assert updated_status["gmail_presets"] == {"max": 12, "large": 10, "medium": 8, "focused": 6}
    # Verify the YouTube preset limit is correctly saved
    assert updated_status["defaults"]["youtube_presets"] == {"max": 10, "large": 9, "medium": 6, "focused": 4}
    assert updated_status["defaults"]["podcast_presets"] == {"max": 5, "large": 5, "medium": 4, "focused": 2}
    assert updated_status["defaults"]["gmail_presets"] == {"max": 12, "large": 10, "medium": 8, "focused": 6}


def test_youtube_email_html_link_transformation() -> None:
    from backend.app.services.email_delivery import _email_html
    
    html_input = """
    <html>
      <body>
        <div class="feedback-controls">Rate this brief</div>
        <a href="#modal-123" data-youtube-modal-target="modal-123" data-youtube-url="https://www.youtube.com/watch?v=123">
          Watch Local AI Systems Talk
        </a>
        <div class="youtube-modal" id="modal-123">
          <iframe class="youtube-player" src="https://www.youtube-nocookie.com/embed/123"></iframe>
        </div>
        <script>alert('hello');</script>
      </body>
    </html>
    """
    
    transformed_html = _email_html(html_input)
    
    assert "feedback-controls" not in transformed_html
    assert "youtube-modal" not in transformed_html
    assert "<script>" not in transformed_html
    assert 'href="https://www.youtube.com/watch?v=123"' in transformed_html
    assert 'target="_blank"' in transformed_html
    assert 'data-youtube-url' not in transformed_html
    assert 'data-youtube-modal-target' not in transformed_html


def test_podcast_media_card_modal_summary_transcript_and_controls() -> None:
    payload = NormalizedPayload(
        source_type="podcast_episode",
        source_name="AI Insight Podcast",
        raw_text="Transcript text about practical podcast engineering workflows and validation.",
        original_url="https://podcast.example.com/show/episode-1",
        published_at="2026-05-21T12:00:00Z",
        metadata={
            "podcast_title": "AI Insight Podcast",
            "title": "The Future of local model orchestration",
            "duration_seconds": 1680,
            "podcast_episode_id": "ep-101",
            "transcript_source": "transcript",
            "episode_url": "https://podcast.example.com/show/episode-1",
            "audio_url": "https://cdn.example.com/audio.mp3",
            "apple_podcasts_url": "https://podcasts.apple.com/podcast/ep-1",
        },
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url="https://podcast.example.com/show/episode-1",
        final_url="https://podcast.example.com/show/episode-1",
        canonical_url="https://podcast.example.com/show/episode-1",
        title="The Future of local model orchestration",
        text=payload.raw_text,
        excerpt="A practical discussion of local model orchestration workflows.",
        domain="podcast.example.com",
        status="fetched",
        link_score=0.95,
        section="Podcasts",
        content_type="podcast",
        editor_summary="A practical summary of local model orchestration.",
    )

    html = database.render_ingested_issue(
        "Podcast Brief",
        "Production-ready podcast test",
        [payload],
        [result],
        lookback_hours=48,
    )

    assert "data-podcast-modal-target" in html
    assert "podcast-modal" in html
    assert "podcast-player" in html
    assert "A practical summary of local model orchestration." in html
    assert "Summary" in html
    assert "Transcript" in html
    assert "data-podcast-speed=\"0.75\"" in html
    assert "data-podcast-speed=\"1.25\"" in html
    assert "data-podcast-url" in html


def test_podcast_without_audio_links_out_without_modal() -> None:
    payload = NormalizedPayload(
        source_type="podcast_episode",
        source_name="AI Insight Podcast",
        raw_text="Show notes about local model orchestration.",
        original_url="https://podcasts.apple.com/podcast/ep-1",
        published_at="2026-05-21T12:00:00Z",
        metadata={
            "podcast_title": "AI Insight Podcast",
            "title": "Local model orchestration roundup",
            "podcast_episode_id": "ep-102",
            "apple_podcasts_url": "https://podcasts.apple.com/podcast/ep-1",
            "transcript_source": "show_notes",
        },
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url="https://podcasts.apple.com/podcast/ep-1",
        final_url="https://podcasts.apple.com/podcast/ep-1",
        canonical_url="https://podcasts.apple.com/podcast/ep-1",
        title="Local model orchestration roundup",
        text=payload.raw_text,
        excerpt="Show notes about local model orchestration.",
        domain="podcasts.apple.com",
        status="fetched",
        link_score=0.72,
        section="Podcasts",
        content_type="podcast",
        editor_summary="A show-notes summary without playable audio.",
    )

    html = database.render_ingested_issue(
        "Podcast Brief",
        "Podcast without audio test",
        [payload],
        [result],
        lookback_hours=48,
    )

    assert 'href="https://podcasts.apple.com/podcast/ep-1"' in html
    assert "Open podcast" in html
    assert '<div class="podcast-modal"' not in html
    assert 'data-podcast-modal-target="' not in html
    assert '<audio class="podcast-player"' not in html
    assert "Audio is not available" not in html


def test_podcast_email_html_link_transformation() -> None:
    from backend.app.services.email_delivery import _email_html

    html_input = """
    <html>
      <body>
        <div class="feedback-controls">Rate this brief</div>
        <a href="#podcast-episode" data-podcast-modal-target="podcast-episode" data-podcast-url="https://podcasts.apple.com/podcast/ep-1">
          Listen to AI Insight with Jane
        </a>
        <div class="podcast-modal" id="podcast-episode">
          <section class="podcast-summary">
            <h4>Summary</h4>
            <p>Episode summary</p>
          </section>
          <section class="podcast-transcript">
            <p>Transcript content</p>
          </section>
        </div>
        <script>alert('hello');</script>
      </body>
    </html>
    """
    
    transformed_html = _email_html(html_input)
    
    assert "feedback-controls" not in transformed_html
    assert "podcast-modal" not in transformed_html
    assert "<script>" not in transformed_html
    assert 'href="https://podcasts.apple.com/podcast/ep-1"' in transformed_html
    assert 'target="_blank"' in transformed_html
    assert 'data-podcast-url' not in transformed_html
    assert 'data-podcast-modal-target' not in transformed_html


def test_youtube_capacity_protection_and_lane_sorting() -> None:
    from backend.agents.discovery.runner import DiscoveryRunner
    from backend.agents.discovery.registry import SourceRegistry
    from backend.agents.discovery.types import TopicProfile, SourceAdapterContext
    from backend.tests.test_explore_discovery import FakeAdapter, candidate
    
    # 1. Create a YouTube adapter returning 12 videos (more than system max of 10)
    yt_candidates = [
        candidate("youtube", f"https://youtube.com/watch?v=yt-{i}", 0.5 + i * 0.03)
        for i in range(12)
    ]
    yt_adapter = FakeAdapter("youtube", yt_candidates)
    yt_adapter.good_for = ("broad_discovery",)
    
    # 2. Create a web search adapter returning 5 candidates
    web_candidates = [
        candidate("web_search", f"https://example.com/web-{i}", 0.7 + i * 0.02)
        for i in range(5)
    ]
    web_adapter = FakeAdapter("web_search", web_candidates)
    web_adapter.good_for = ("breaking_news",)
    
    # 3. Create topic profile with youtube and web_search enabled
    profile = TopicProfile.from_dict(
        {
            "statement": "test topic",
            "scope": "test topic",
            "source_selection": {"youtube": True, "web_search": True},
            "content_limits": {
                "per_source": {"youtube": 5}, # youtube limit is set to 5
                "total_items": 10,
            }
        }
    )
    
    # Run discovery runner with limit of 8 candidates in context
    registry = SourceRegistry([yt_adapter, web_adapter])
    runner = DiscoveryRunner(registry)
    result = asyncio.run(
        runner.run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-yt-lane", candidate_limit=8),
        )
    )
    
    candidates = result.candidates
    assert len(candidates) == 12
    
    yt_results = [c for c in candidates if c.adapter == "youtube"]
    web_results = [c for c in candidates if c.adapter == "web_search"]
    
    assert len(yt_results) == 12
    assert len(web_results) == 0
    
    # Verify that the youtube candidates selected are the ones with highest scores
    yt_urls = {c.payload.original_url for c in yt_results}
    assert "https://youtube.com/watch?v=yt-11" in yt_urls
    assert "https://youtube.com/watch?v=yt-7" in yt_urls
    assert "https://youtube.com/watch?v=yt-0" in yt_urls


def test_podcast_lane_isolation_from_web_candidates(monkeypatch, tmp_path) -> None:
    from backend.agents.discovery.runner import DiscoveryRunner
    from backend.agents.discovery.registry import SourceRegistry
    from backend.agents.discovery.types import TopicProfile, SourceAdapterContext
    from backend.tests.test_explore_discovery import FakeAdapter, candidate, configure_runtime

    configure_runtime(monkeypatch, tmp_path)

    podcast_candidates = [
        candidate("podcasts", f"https://podcast.example.com/episode-{i}", 0.55 + i * 0.02)
        for i in range(12)
    ]
    podcast_adapter = FakeAdapter("podcasts", podcast_candidates)
    podcast_adapter.good_for = ("breaking_news",)

    web_candidates = [
        candidate("web_search", f"https://example.com/web-{i}", 0.9 - i * 0.01)
        for i in range(8)
    ]
    web_adapter = FakeAdapter("web_search", web_candidates)
    web_adapter.good_for = ("breaking_news",)

    profile = TopicProfile.from_dict(
        {
            "statement": "AI agents for local infrastructure",
            "scope": "AI agents for local infrastructure",
            "source_selection": {"podcasts": True, "web_search": True},
            "content_limits": {
                "per_source": {"podcasts": 10},
                "total_items": 10,
            },
        }
    )

    registry = SourceRegistry([podcast_adapter, web_adapter])
    runner = DiscoveryRunner(registry)
    result = asyncio.run(
        runner.run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-podcast-lane", candidate_limit=4),
        )
    )

    candidates = result.candidates
    podcast_results = [c for c in candidates if c.adapter == "podcasts"]
    web_results = [c for c in candidates if c.adapter == "web_search"]

    assert len(candidates) == 12
    assert len(podcast_results) == 12
    assert len(web_results) == 0

    # podcast items should come from highest scoring podcast candidates up to the lane limit.
    assert "https://podcast.example.com/episode-11" in {c.payload.original_url for c in podcast_results}
    assert "https://podcast.example.com/episode-10" in {c.payload.original_url for c in podcast_results}
    assert "https://podcast.example.com/episode-0" in {c.payload.original_url for c in podcast_results}


def test_youtube_adapter_triggers_query_refinement(monkeypatch, tmp_path) -> None:
    searched_queries = []

    async def fake_search_youtube(**kwargs: Any) -> youtube.YouTubeSearchResult:
        searched_queries.append(kwargs)
        if "refined" in kwargs.get("query", ""):
            return youtube.YouTubeSearchResult(
                videos=(
                    youtube.YouTubeVideo(
                        video_id="video-refined",
                        title="Refined Video Title",
                        channel_name="Refined YouTube Channel",
                        published_at="2026-05-25T12:00:00Z",
                        description="Refined video description.",
                        thumbnail_url="https://img.example.com/refined.jpg",
                        duration_seconds=600,
                        score=0.95,
                    ),
                ),
                quota_units=101,
            )
        return youtube.YouTubeSearchResult(videos=(), quota_units=101)

    async def fake_fetch_transcript(video_id: str) -> youtube.YouTubeTranscript | None:
        return None

    async def fake_refine_queries_for_adapter(
        adapter_name: str,
        profile: TopicProfile,
        initial_results: list,
        initial_queries: list[str],
        lookback_hours: int | None = None,
    ) -> list[str]:
        assert lookback_hours == 10
        return ["refined youtube query"]

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_YOUTUBE_API_KEY", "key")
    monkeypatch.setattr(adapters, "search_youtube", fake_search_youtube)
    monkeypatch.setattr(adapters, "fetch_youtube_transcript", fake_fetch_transcript)
    monkeypatch.setattr("backend.agents.discovery.query_refiner.refine_queries_for_adapter", fake_refine_queries_for_adapter)

    adapter = YouTubeSourceAdapter()
    candidates = asyncio.run(
        adapter.query(
            TopicProfile.from_dict({"statement": "local robotics", "scope": "local robotics"}),
            SourceAdapterContext(exploration_id="explore-1", candidate_limit=5, lookback_hours=10),
        )
    )

    assert len(candidates) == 1
    assert candidates[0].payload.source_name == "Refined YouTube Channel"
    assert candidates[0].payload.original_url == "https://www.youtube.com/watch?v=video-refined"
    assert candidates[0].payload.metadata["is_refined_query"] is True

    # Validate retry pass keeps the strict recency window while relaxing duration only.
    assert len(searched_queries) == 2
    assert searched_queries[0]["query"] == "local robotics"
    assert searched_queries[0]["duration_filter"] == "medium"
    assert searched_queries[0]["lookback_hours"] == 10

    assert searched_queries[1]["query"] == "refined youtube query"
    assert searched_queries[1]["duration_filter"] == "any"
    assert searched_queries[1]["lookback_hours"] == 10
