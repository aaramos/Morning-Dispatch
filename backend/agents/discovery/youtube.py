from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from backend.agents.discovery.types import AdapterUnavailable, RecencyWeighting
from backend.app.core.http_pool import shared_async_client
from backend.app.db import database

YOUTUBE_SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
YOUTUBE_QUOTA_WARNING_UNITS = 8000
YOUTUBE_DAILY_QUOTA_UNITS = 10000


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    title: str
    channel_name: str
    published_at: str | None
    description: str
    thumbnail_url: str
    duration_seconds: int
    score: float


@dataclass(frozen=True)
class YouTubeTranscript:
    text: str
    segments: tuple[dict[str, Any], ...]
    source: str = "native"


@dataclass(frozen=True)
class YouTubeSearchResult:
    videos: tuple[YouTubeVideo, ...]
    quota_units: int


async def search_youtube(
    *,
    api_key: str | None,
    query: str,
    limit: int,
    recency_weighting: RecencyWeighting = "recent",
    duration_filter: str = "medium",
    lookback_hours: int | None = None,
) -> YouTubeSearchResult:
    clean_query = " ".join(str(query or "").split()).strip()
    if not api_key:
        raise AdapterUnavailable("YouTube API key is not configured.")
    if not clean_query:
        return YouTubeSearchResult(videos=(), quota_units=0)

    max_results = max(1, min(limit, 50))
    params = {
        "key": api_key,
        "part": "snippet",
        "type": "video",
        "order": "relevance",
        "q": clean_query,
        "maxResults": str(max_results),
        "videoDuration": _duration_filter(duration_filter),
        "relevanceLanguage": "en",
    }
    if lookback_hours is not None:
        published_after = (datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))).isoformat(timespec="seconds").replace("+00:00", "Z")
    else:
        published_after = _published_after(recency_weighting)
    if published_after:
        params["publishedAfter"] = published_after
    quota_units = 100
    client = shared_async_client(purpose="youtube", timeout=10.0)
    search_response = await client.get(YOUTUBE_SEARCH_ENDPOINT, params=params, timeout=10.0)
    _raise_for_youtube_error(search_response)
    search_response.raise_for_status()
    search_data = search_response.json()

    video_ids = _video_ids(search_data)
    if not video_ids:
        database.record_youtube_quota(quota_units)
        return YouTubeSearchResult(videos=(), quota_units=quota_units)

    videos_response = await client.get(
        YOUTUBE_VIDEOS_ENDPOINT,
        params={
            "key": api_key,
            "part": "snippet,contentDetails",
            "id": ",".join(video_ids),
            "maxResults": str(len(video_ids)),
        },
        timeout=10.0,
    )
    quota_units += 1
    _raise_for_youtube_error(videos_response)
    videos_response.raise_for_status()
    videos_data = videos_response.json()

    database.record_youtube_quota(quota_units)
    return YouTubeSearchResult(videos=tuple(_parse_videos(videos_data, order=video_ids)), quota_units=quota_units)


async def fetch_youtube_transcript(video_id: str) -> YouTubeTranscript | None:
    if not video_id:
        return None
    try:
        chunks = await asyncio.to_thread(_native_transcript_chunks, video_id)
    except Exception:
        return None
    segments = _transcript_segments(chunks)
    text = "\n\n".join(str(segment["text"]) for segment in segments if str(segment.get("text") or "").strip())
    if len(text) < 180:
        return None
    return YouTubeTranscript(text=text, segments=tuple(segments), source="native")


def _native_transcript_chunks(video_id: str) -> list[dict[str, Any]]:
    from youtube_transcript_api import YouTubeTranscriptApi

    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        return list(YouTubeTranscriptApi.get_transcript(video_id, languages=["en"]))

    transcript_api = YouTubeTranscriptApi()
    fetched = transcript_api.fetch(video_id, languages=["en"])
    if hasattr(fetched, "to_raw_data"):
        return list(fetched.to_raw_data())
    return [dict(item) for item in fetched]


def _transcript_segments(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    current_text: list[str] = []
    current_start = 0
    for chunk in chunks:
        text = _clean_transcript_text(chunk.get("text"))
        if not text:
            continue
        try:
            start = int(float(chunk.get("start") or 0))
        except (TypeError, ValueError):
            start = 0
        if not current_text:
            current_start = start
        if current_text and start - current_start >= 60:
            segments.append({"start_seconds": current_start, "text": " ".join(current_text)})
            current_text = []
            current_start = start
        current_text.append(text)
    if current_text:
        segments.append({"start_seconds": current_start, "text": " ".join(current_text)})
    return segments


def _clean_transcript_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")


def _published_after(recency_weighting: RecencyWeighting) -> str | None:
    days = 30
    if recency_weighting == "breaking":
        days = 2
    elif recency_weighting == "last_year":
        days = 365
    elif recency_weighting == "all_available":
        return None
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _duration_filter(value: str) -> str:
    clean = str(value or "medium").strip().lower()
    return clean if clean in {"any", "short", "medium", "long"} else "medium"


def _raise_for_youtube_error(response: httpx.Response) -> None:
    if response.status_code in {400, 401, 403, 429} or response.status_code >= 500:
        raise AdapterUnavailable(_youtube_error_message(response))


def _video_ids(payload: Any) -> list[str]:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    video_ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id") if isinstance(item.get("id"), dict) else {}
        video_id = str(raw_id.get("videoId") or "").strip()
        if video_id and video_id not in seen:
            video_ids.append(video_id)
            seen.add(video_id)
    return video_ids


def _parse_videos(payload: Any, *, order: list[str]) -> list[YouTubeVideo]:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    rank = {video_id: index for index, video_id in enumerate(order)}
    videos: list[YouTubeVideo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id") or "").strip()
        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        content_details = item.get("contentDetails") if isinstance(item.get("contentDetails"), dict) else {}
        title = str(snippet.get("title") or "").strip()
        if not video_id or not title:
            continue
        thumbnails = snippet.get("thumbnails") if isinstance(snippet.get("thumbnails"), dict) else {}
        thumbnail = _best_thumbnail(thumbnails)
        duration_seconds = _parse_iso8601_duration(str(content_details.get("duration") or ""))
        videos.append(
            YouTubeVideo(
                video_id=video_id,
                title=title,
                channel_name=str(snippet.get("channelTitle") or "YouTube").strip() or "YouTube",
                published_at=str(snippet.get("publishedAt") or "").strip() or None,
                description=str(snippet.get("description") or "").strip(),
                thumbnail_url=thumbnail,
                duration_seconds=duration_seconds,
                score=round(max(0.4, 0.92 - (rank.get(video_id, len(rank)) * 0.035)), 3),
            )
        )
    return sorted(videos, key=lambda video: rank.get(video.video_id, 999))


def _best_thumbnail(thumbnails: dict[str, Any]) -> str:
    for key in ("maxres", "standard", "high", "medium", "default"):
        value = thumbnails.get(key)
        if isinstance(value, dict):
            url = str(value.get("url") or "").strip()
            if url:
                return url
    return ""


def _parse_iso8601_duration(value: str) -> int:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        value,
    )
    if not match:
        return 0
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return (((days * 24) + hours) * 60 + minutes) * 60 + seconds


def _youtube_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"YouTube API returned HTTP {response.status_code}."
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return f"YouTube API returned HTTP {response.status_code}."
    reason_values: list[str] = []
    for item in error.get("errors") or ():
        if isinstance(item, dict):
            reason = str(item.get("reason") or "").strip()
            if reason:
                reason_values.append(reason)
    message = str(error.get("message") or "").strip()
    if not message:
        return f"YouTube API returned HTTP {response.status_code}."
    reasons = " ".join(reason_values).lower()
    message_lower = message.lower()
    if "quota" in reasons or "quota" in message_lower:
        return "YouTube quota exceeded."
    if response.status_code == 429 or "ratelimit" in reasons or "rate limit" in message_lower:
        return "YouTube API rate limit exceeded."
    if response.status_code >= 500:
        return "YouTube API is temporarily unavailable."
    return f"YouTube API error: {message}"
