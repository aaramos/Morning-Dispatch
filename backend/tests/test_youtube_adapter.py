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
