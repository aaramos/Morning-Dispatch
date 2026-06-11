"""HTTP helpers for the podcast pipeline (feeds, audio, Podcast Index, Apple).

Extracted from backend/agents/digestor/podcast.py (M8) — pure moves, zero
behavior change. podcast.py re-exports these names for compatibility.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from backend.app.core.config import get_settings

if TYPE_CHECKING:
    from backend.agents.digestor.podcast import PodcastEpisode

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = "MorningDispatch/0.1 (+https://tailnet.local)"
# Long read timeout for large audio files, but never unbounded: a stuck
# connection must not hang the whole podcast lane (P9).
AUDIO_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0)

# TODO: unify with backend/app/core/http_pool.py once the generalized pool lands.
# Process-wide pool of httpx clients so podcast HTTP calls reuse keep-alive
# connections instead of paying connection/TLS setup on every request. Keyed by
# purpose and bound to the event loop that created the client (a different loop
# — e.g. a fresh asyncio.run in tests — transparently gets its own client).
# Modeled on backend/agents/model/client.py.
_CLIENT_CONFIGS: dict[str, dict[str, Any]] = {
    "feed": {
        "timeout": REQUEST_TIMEOUT_SECONDS,
        "follow_redirects": True,
        "headers": {"User-Agent": USER_AGENT},
    },
    "podcastindex": {
        "timeout": REQUEST_TIMEOUT_SECONDS,
        "follow_redirects": True,
    },
    "audio": {
        "timeout": AUDIO_DOWNLOAD_TIMEOUT,
        "follow_redirects": True,
        "headers": {"User-Agent": USER_AGENT},
    },
}
_SHARED_HTTP_CLIENTS: dict[str, tuple[Any, httpx.AsyncClient]] = {}


def shared_podcast_client(purpose: str) -> httpx.AsyncClient:
    """Return the shared keep-alive client for *purpose*, bound to the running loop."""
    config = _CLIENT_CONFIGS[purpose]
    loop = asyncio.get_running_loop()
    existing = _SHARED_HTTP_CLIENTS.get(purpose)
    if existing is not None:
        bound_loop, client = existing
        if bound_loop is loop and not getattr(client, "is_closed", False):
            return client
    client = httpx.AsyncClient(**config)
    _SHARED_HTTP_CLIENTS[purpose] = (loop, client)
    return client


async def aclose_shared_podcast_clients() -> None:
    clients = list(_SHARED_HTTP_CLIENTS.values())
    _SHARED_HTTP_CLIENTS.clear()
    for _loop, client in clients:
        if hasattr(client, "is_closed") and not client.is_closed:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - best-effort shutdown
                pass


async def discover_podcasts(query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    settings = get_settings()
    key = settings.podcastindex_api_key
    secret = settings.podcastindex_api_secret
    if not key or not secret or not query.strip():
        return []

    auth_date = str(int(time.time()))
    authorization = hashlib.sha1(f"{key}{secret}{auth_date}".encode("utf-8")).hexdigest()
    headers = {
        "User-Agent": USER_AGENT,
        "X-Auth-Date": auth_date,
        "X-Auth-Key": key,
        "Authorization": authorization,
    }
    params = {"q": query.strip(), "max": max(1, min(limit, 25))}
    client = shared_podcast_client("podcastindex")
    response = await client.get(
        "https://api.podcastindex.org/api/1.0/search/byterm",
        params=params,
        headers=headers,
    )
    response.raise_for_status()
    data = response.json()

    feeds = data.get("feeds") if isinstance(data, dict) else []
    results = []
    for feed in feeds if isinstance(feeds, list) else []:
        feed_url = str(feed.get("url") or "").strip()
        if not feed_url:
            continue
        results.append(
            {
                "type": "podcast_rss",
                "title": str(feed.get("title") or "Podcast").strip(),
                "feed_url": feed_url,
                "site_url": feed.get("link"),
                "author": feed.get("author"),
                "description": _clean_html(str(feed.get("description") or ""))[:600],
                "aggregator": "podcastindex",
                "itunes_id": _nullable_str(feed.get("itunesId") or feed.get("itunes_id")),
                "apple_podcasts_url": _apple_url_from_itunes_id(feed.get("itunesId") or feed.get("itunes_id")),
            }
        )
    return results


async def _lookup_apple_podcast_url(
    client: httpx.AsyncClient,
    episode: PodcastEpisode,
    source: dict[str, Any],
) -> str | None:
    query = str(source.get("title") or episode.show_name or "").strip()
    if not query:
        return None
    try:
        response = await client.get(
            "https://itunes.apple.com/search",
            params={
                "term": query,
                "media": "podcast",
                "entity": "podcast",
                "country": "US",
                "limit": 10,
            },
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.info("Apple Podcasts lookup failed for %s: %s", episode.show_name, exc)
        return None

    results = data.get("results") if isinstance(data, dict) else []
    if not isinstance(results, list):
        return None
    feed_url = _normalize_url_for_match(episode.feed_url)
    show_name = _normalize_title(episode.show_name)
    fallback_url: str | None = None
    for result in results:
        if not isinstance(result, dict):
            continue
        collection_url = str(result.get("collectionViewUrl") or "").strip()
        if collection_url.startswith(("https://podcasts.apple.com/", "https://itunes.apple.com/")):
            fallback_url = fallback_url or collection_url
        result_feed_url = _normalize_url_for_match(str(result.get("feedUrl") or ""))
        result_name = _normalize_title(str(result.get("collectionName") or ""))
        if collection_url and (result_feed_url == feed_url or result_name == show_name):
            return collection_url
    return fallback_url


async def _download_audio(episode: PodcastEpisode) -> Path:
    if not episode.audio_url:
        raise ValueError("Episode has no audio URL")
    path = _audio_path(episode)
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    client = shared_podcast_client("audio")
    async with client.stream("GET", episode.audio_url) as response:
        response.raise_for_status()
        with path.open("wb") as handle:
            async for chunk in response.aiter_bytes():
                handle.write(chunk)
    return path


async def _download_transcript_text(url: str) -> str:
    """Fetch a publisher transcript URL and normalize it to plain text."""
    try:
        client = shared_podcast_client("feed")
        response = await client.get(url)
        response.raise_for_status()
        body = response.text
        content_type = str(response.headers.get("content-type", "")).lower()
    except Exception as exc:
        logger.info("Podcast transcript fetch failed for %s: %s", url, exc)
        return ""
    lowered = url.lower()
    if "html" in content_type or lowered.endswith((".html", ".htm")):
        return _clean_html(body)
    if "json" in content_type or lowered.endswith(".json"):
        try:
            return _clean_text(_flatten_json_transcript(json.loads(body)))
        except (ValueError, TypeError):
            return ""
    # SRT / VTT / plain text: drop cue indexes, timestamps, and WEBVTT headers.
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.upper().startswith("WEBVTT"):
            continue
        if "-->" in stripped:
            continue
        if stripped.isdigit():
            continue
        lines.append(stripped)
    return _clean_text(" ".join(lines))


def _flatten_json_transcript(data: Any) -> str:
    """Extract spoken text from common JSON transcript shapes (segments[].body/text)."""
    segments = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for segment in segments:
        if isinstance(segment, dict):
            text = segment.get("body") or segment.get("text") or segment.get("transcript")
            if text:
                parts.append(str(text))
        elif isinstance(segment, str):
            parts.append(segment)
    return " ".join(parts)


def _audio_path(episode: PodcastEpisode) -> Path:
    suffix = Path(urlparse(episode.audio_url or "").path).suffix or ".mp3"
    return get_settings().data_dir / "podcast-audio" / f"{episode.episode_id}{suffix}"


def _apple_url_from_itunes_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"0", "-1"}:
        return None
    if not text.isdigit():
        return None
    return f"https://podcasts.apple.com/podcast/id{text}"


def _nullable_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_url_for_match(value: str) -> str:
    parsed = urlparse(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return value.strip().rstrip("/")
    path = parsed.path.rstrip("/")
    return f"{parsed.netloc.lower()}{path}"


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _clean_html(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    return _clean_text(soup.get_text(" ", strip=True))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
