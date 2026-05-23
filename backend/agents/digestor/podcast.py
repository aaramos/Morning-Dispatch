from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

from backend.agents.agentic import AgentDecision
from backend.agents.digestor.base import NormalizedPayload, pii_filter
from backend.agents.librarian.text_utils import keyword_set
from backend.app.core.config import get_settings
from backend.app.db import database
from backend.db.queries import get_watermark, upsert_watermark

logger = logging.getLogger(__name__)

MAX_PODCAST_EPISODES = 4
MAX_DISCOVERED_FEEDS = 3
REQUEST_TIMEOUT_SECONDS = 20
MIN_EPISODE_SCORE = 0.22
USER_AGENT = "MorningDispatch/0.1 (+https://tailnet.local)"


@dataclass(frozen=True)
class PodcastEpisode:
    show_name: str
    feed_url: str
    episode_id: str
    title: str
    description: str
    published_at: str | None
    episode_url: str | None
    audio_url: str | None
    duration_seconds: int | None = None
    apple_podcasts_url: str | None = None


async def fetch_podcast_episodes(
    *,
    digest_id: str,
    digest_interest: str,
    sources: list[dict[str, Any]],
    lookback_hours: int,
    max_episodes: int = MAX_PODCAST_EPISODES,
    inference_run_id: str | None = None,
) -> tuple[list[NormalizedPayload], list[AgentDecision]]:
    decisions: list[AgentDecision] = []
    try:
        return await _fetch_podcast_episodes(
            digest_id=digest_id,
            digest_interest=digest_interest,
            sources=sources,
            lookback_hours=lookback_hours,
            max_episodes=max_episodes,
            inference_run_id=inference_run_id,
            decisions=decisions,
        )
    except Exception as exc:
        logger.info("Podcast ingestion failed: %s", exc)
        decisions.append(
            _decision(
                target="podcast ingestion",
                decision="source_error",
                action="skip_podcasts",
                confidence=0.82,
                reason="Podcast Scout hit a recoverable source error, so podcasts were skipped for this run.",
                metadata={"error": str(exc)[:240]},
            )
        )
        return [], decisions


async def _fetch_podcast_episodes(
    *,
    digest_id: str,
    digest_interest: str,
    sources: list[dict[str, Any]],
    lookback_hours: int,
    max_episodes: int,
    inference_run_id: str | None,
    decisions: list[AgentDecision],
) -> tuple[list[NormalizedPayload], list[AgentDecision]]:
    podcast_sources = _podcast_sources(sources)
    if not podcast_sources:
        return [], decisions

    feed_sources = [source for source in podcast_sources if _source_feed_url(source)]
    search_sources = [source for source in podcast_sources if not _source_feed_url(source)]

    discovered = await _discover_sources(search_sources, digest_interest)
    for source in discovered:
        decisions.append(
            _decision(
                target=str(source.get("title") or source.get("feed_url") or "podcast"),
                decision="watch",
                action="sample_feed",
                confidence=0.74,
                reason="Podcast Scout found a candidate show through aggregator search.",
                metadata={"feed_url": source.get("feed_url"), "aggregator": source.get("aggregator")},
            )
        )

    feed_sources.extend(discovered)
    if not feed_sources:
        return [], decisions

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}) as client:
        batches = await asyncio.gather(
            *[_timed_fetch_feed_episodes(client, source) for source in feed_sources],
            return_exceptions=True,
        )

    candidates: list[tuple[float, PodcastEpisode, dict[str, Any]]] = []
    feed_fetch_ms_by_url: dict[str, int] = {}
    for source, batch in zip(feed_sources, batches):
        if isinstance(batch, Exception):
            logger.info("Podcast feed failed for %s: %s", _source_feed_url(source), batch)
            decisions.append(
                _decision(
                    target=str(source.get("title") or _source_feed_url(source) or "podcast"),
                    decision="feed_error",
                    action="skip",
                    confidence=0.82,
                    reason="Podcast Scout could not read the feed this run.",
                    metadata={"error": str(batch)[:240]},
                )
            )
            continue
        feed_episodes, feed_fetch_ms = batch
        for episode in feed_episodes:
            feed_fetch_ms_by_url[episode.feed_url] = feed_fetch_ms
        for episode in feed_episodes:
            if not _inside_lookback(episode.published_at, lookback_hours):
                continue
            if _already_seen(digest_id, episode):
                _record_skipped_podcast_metric(
                    digest_id=digest_id,
                    inference_run_id=inference_run_id,
                    episode=episode,
                    status="already_seen",
                    feed_fetch_ms=feed_fetch_ms,
                )
                continue
            score = _score_episode(episode, digest_interest)
            if score < MIN_EPISODE_SCORE:
                _record_skipped_podcast_metric(
                    digest_id=digest_id,
                    inference_run_id=inference_run_id,
                    episode=episode,
                    status="low_score",
                    feed_fetch_ms=feed_fetch_ms,
                    score=score,
                )
                decisions.append(
                    _decision(
                        target=episode.title,
                        decision="skip",
                        action="skip_episode",
                        confidence=0.78,
                        reason="Podcast Triage found weak overlap with the digest interests.",
                        metadata={"score": score, "show": episode.show_name},
                    )
                )
                continue
            candidates.append((score, episode, source))

    ranked = sorted(candidates, key=lambda item: item[0], reverse=True)[:max(1, max_episodes)]
    payloads: list[NormalizedPayload] = []
    for score, episode, source in ranked:
        episode_started = time.monotonic()
        transcript = ""
        transcript_source = "unknown"
        transcript_decisions: list[AgentDecision] = []
        metric: dict[str, Any] = {}
        try:
            transcript, transcript_source, transcript_decisions, metric = await _episode_text(episode, source, score)
        except Exception as exc:
            metric = _podcast_metric_base(
                digest_id=digest_id,
                inference_run_id=inference_run_id,
                episode=episode,
                score=score,
                transcript_source="error",
                status="error",
                error_detail=str(exc)[:240],
                started_at=episode_started,
            )
            metric["feed_fetch_ms"] = feed_fetch_ms_by_url.get(episode.feed_url)
            database.record_podcast_metric(metric)
            logger.info("Podcast episode processing failed for %s: %s", episode.title, exc)
            continue
        metric.update(
            {
                "digest_id": digest_id,
                "inference_run_id": inference_run_id,
                "feed_fetch_ms": feed_fetch_ms_by_url.get(episode.feed_url),
                "total_ms": _elapsed_ms(episode_started),
                "transcript_words": _word_count(transcript),
            }
        )
        decisions.extend(transcript_decisions)
        raw_text = _episode_payload_text(episode, transcript, transcript_source)
        preferred_url = episode.apple_podcasts_url or episode.episode_url or episode.audio_url
        payload = NormalizedPayload(
            source_type="podcast_episode",
            source_name=episode.show_name,
            raw_text=raw_text,
            original_url=preferred_url,
            published_at=episode.published_at,
            metadata={
                "podcast_episode_id": episode.episode_id,
                "podcast_title": episode.show_name,
                "title": episode.title,
                "feed_url": episode.feed_url,
                "apple_podcasts_url": episode.apple_podcasts_url,
                "audio_url": episode.audio_url,
                "episode_url": episode.episode_url,
                "duration_seconds": episode.duration_seconds,
                "episode_quality_score": score,
                "transcript_source": transcript_source,
            },
        )
        if pii_filter(payload):
            payloads.append(payload)
            _mark_seen(digest_id, episode)
            metric["status"] = "success"
        else:
            metric["status"] = "pii_filtered"
        database.record_podcast_metric(metric)

    return payloads, decisions


def _record_skipped_podcast_metric(
    *,
    digest_id: str,
    inference_run_id: str | None,
    episode: PodcastEpisode,
    status: str,
    feed_fetch_ms: int | None,
    score: float | None = None,
) -> None:
    metric = _podcast_metric_base(
        digest_id=digest_id,
        inference_run_id=inference_run_id,
        episode=episode,
        score=score or 0.0,
        transcript_source="not_processed",
        status=status,
        started_at=time.monotonic(),
    )
    metric["feed_fetch_ms"] = feed_fetch_ms
    database.record_podcast_metric(metric)


async def _timed_fetch_feed_episodes(
    client: httpx.AsyncClient,
    source: dict[str, Any],
) -> tuple[list[PodcastEpisode], int]:
    started_at = time.monotonic()
    episodes = await _fetch_feed_episodes(client, source)
    return episodes, _elapsed_ms(started_at)


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
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
        response = await client.get("https://api.podcastindex.org/api/1.0/search/byterm", params=params)
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
                "aggregator": "podcastindex",
                "itunes_id": _nullable_str(feed.get("itunesId") or feed.get("itunes_id")),
                "apple_podcasts_url": _apple_url_from_itunes_id(feed.get("itunesId") or feed.get("itunes_id")),
            }
        )
    return results


def _podcast_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        source
        for source in sources
        if str(source.get("type") or "") in {"podcast_rss", "podcast_search"}
    ]


async def _discover_sources(sources: list[dict[str, Any]], digest_interest: str) -> list[dict[str, Any]]:
    queries = []
    for source in sources:
        query = str(source.get("query") or "").strip()
        if query:
            queries.append(query)
    if not queries:
        queries = [_discovery_query(digest_interest)]
    discovered: dict[str, dict[str, Any]] = {}
    for query in queries[:3]:
        try:
            query_results = await discover_podcasts(query, limit=MAX_DISCOVERED_FEEDS)
        except Exception as exc:
            logger.info("Podcast discovery failed for %s: %s", query, exc)
            continue
        for source in query_results:
            feed_url = str(source.get("feed_url") or "")
            discovered.setdefault(feed_url, source)
    return list(discovered.values())[:MAX_DISCOVERED_FEEDS]


async def _fetch_feed_episodes(client: httpx.AsyncClient, source: dict[str, Any]) -> list[PodcastEpisode]:
    feed_url = _source_feed_url(source)
    if not feed_url:
        return []
    response = await client.get(feed_url)
    response.raise_for_status()
    episodes = parse_podcast_feed(response.text, feed_url=feed_url, fallback_show_name=str(source.get("title") or "Podcast"))
    if not episodes:
        return episodes
    apple_url = _source_apple_url(source) or await _lookup_apple_podcast_url(client, episodes[0], source)
    if not apple_url:
        return episodes
    return [replace(episode, apple_podcasts_url=apple_url) for episode in episodes]


def parse_podcast_feed(xml_text: str, *, feed_url: str, fallback_show_name: str = "Podcast") -> list[PodcastEpisode]:
    root = ElementTree.fromstring(xml_text)
    channel = root.find("channel") if root.tag.lower().endswith("rss") else root.find(".//channel")
    if channel is None:
        return []
    show_name = _child_text(channel, "title") or fallback_show_name
    episodes: list[PodcastEpisode] = []
    for item in channel.findall("item"):
        title = _clean_text(_child_text(item, "title") or "Podcast episode")
        description = _clean_html(_child_text(item, "description") or _child_text(item, "summary"))
        guid = _child_text(item, "guid")
        episode_url = _child_text(item, "link")
        audio_url = _enclosure_url(item)
        published_at = _published_at(_child_text(item, "pubDate"))
        episode_id = _episode_id(feed_url, guid or audio_url or episode_url or title)
        episodes.append(
            PodcastEpisode(
                show_name=_clean_text(show_name),
                feed_url=feed_url,
                episode_id=episode_id,
                title=title,
                description=description,
                published_at=published_at,
                episode_url=episode_url,
                audio_url=audio_url,
                duration_seconds=_duration_seconds(_child_text(item, "duration")),
            )
        )
    return episodes


async def _episode_text(
    episode: PodcastEpisode,
    source: dict[str, Any],
    score: float,
) -> tuple[str, str, list[AgentDecision], dict[str, Any]]:
    started_at = time.monotonic()
    decisions: list[AgentDecision] = []
    transcript_path = _transcript_path(episode)
    if transcript_path.exists():
        transcript = transcript_path.read_text(encoding="utf-8", errors="replace")
        decisions.append(
            _decision(
                target=episode.title,
                decision="transcript_cache",
                action="use_cached_transcript",
                confidence=0.91,
                reason="Podcast Librarian reused the cached transcript.",
                metadata={"path": str(transcript_path), "score": score},
            )
        )
        metric = _podcast_metric_base(
            episode=episode,
            score=score,
            transcript_source="transcript_cache",
            status="success",
            started_at=started_at,
            cache_hit=True,
        )
        metric["transcript_words"] = _word_count(transcript)
        return transcript, "transcript_cache", decisions, metric

    should_transcribe = _should_transcribe(episode, source, score)
    if should_transcribe and episode.audio_url and _transcribe_command():
        try:
            download_started = time.monotonic()
            audio_path = await _download_audio(episode)
            download_ms = _elapsed_ms(download_started)
            audio_bytes = audio_path.stat().st_size if audio_path.exists() else None
            transcription_started = time.monotonic()
            transcript = _run_transcription(audio_path, transcript_path)
            transcription_ms = _elapsed_ms(transcription_started)
            decisions.append(
                _decision(
                    target=episode.title,
                    decision="transcribe",
                    action="transcribe_episode",
                    confidence=0.86,
                    reason="Podcast Triage found enough signal to transcribe the episode audio.",
                    metadata={"audio_url": episode.audio_url, "score": score},
                )
            )
            metric = _podcast_metric_base(
                episode=episode,
                score=score,
                transcript_source="transcript",
                status="success",
                started_at=started_at,
            )
            metric.update(
                {
                    "audio_download_ms": download_ms,
                    "transcription_ms": transcription_ms,
                    "audio_bytes": audio_bytes,
                    "transcript_words": _word_count(transcript),
                }
            )
            return transcript, "transcript", decisions, metric
        except Exception as exc:
            logger.info("Podcast transcription failed for %s: %s", episode.title, exc)
            decisions.append(
                _decision(
                    target=episode.title,
                    decision="transcript_failed",
                    action="use_show_notes",
                    confidence=0.72,
                    reason="Podcast transcription failed, so the agent used show notes as fallback context.",
                    metadata={"error": str(exc)[:240], "score": score},
                )
            )

    fallback_reason = (
        "Podcast Triage found the episode relevant, but no local transcription command is configured."
        if not _transcribe_command()
        else "Podcast Triage used show notes because local transcription was not available for this episode."
    )
    decisions.append(
        _decision(
            target=episode.title,
            decision="show_notes_summary",
            action="summarize_show_notes",
            confidence=0.68,
            reason=fallback_reason,
            metadata={"score": score, "audio_url": episode.audio_url},
        )
    )
    metric = _podcast_metric_base(
        episode=episode,
        score=score,
        transcript_source="show_notes",
        status="success",
        started_at=started_at,
    )
    metric["transcript_words"] = _word_count(episode.description)
    return episode.description, "show_notes", decisions, metric


def _podcast_metric_base(
    *,
    episode: PodcastEpisode,
    score: float,
    transcript_source: str,
    status: str,
    started_at: float,
    digest_id: str | None = None,
    inference_run_id: str | None = None,
    error_detail: str | None = None,
    cache_hit: bool = False,
) -> dict[str, Any]:
    return {
        "digest_id": digest_id,
        "inference_run_id": inference_run_id,
        "show_name": episode.show_name,
        "episode_id": episode.episode_id,
        "episode_title": episode.title,
        "feed_url": episode.feed_url,
        "audio_url": episode.audio_url,
        "episode_url": episode.episode_url,
        "apple_podcasts_url": episode.apple_podcasts_url,
        "published_at": episode.published_at,
        "duration_seconds": episode.duration_seconds,
        "quality_score": score,
        "transcript_source": transcript_source,
        "status": status,
        "error_detail": error_detail,
        "total_ms": _elapsed_ms(started_at),
        "cache_hit": cache_hit,
    }


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _word_count(value: str | None) -> int:
    return len(re.findall(r"\w+", value or ""))


def _score_episode(episode: PodcastEpisode, digest_interest: str) -> float:
    interest_tokens = keyword_set(digest_interest)
    episode_tokens = keyword_set(f"{episode.title} {episode.description} {episode.show_name}")
    if not episode_tokens:
        return 0.0
    overlap = len(episode_tokens & interest_tokens) / max(1, len(interest_tokens)) if interest_tokens else 0.35
    title_overlap = len(keyword_set(episode.title) & interest_tokens) / max(1, len(keyword_set(episode.title)) or 1) if interest_tokens else 0.25
    recency = _recency_score(episode.published_at)
    duration_bonus = 0.08 if not episode.duration_seconds or episode.duration_seconds <= 3600 else -0.08
    score = 0.18 + (0.38 * overlap) + (0.20 * title_overlap) + (0.12 * recency) + duration_bonus
    if episode.audio_url:
        score += 0.04
    return round(max(0.0, min(score, 1.0)), 3)


def _should_transcribe(episode: PodcastEpisode, source: dict[str, Any], score: float) -> bool:
    if str(source.get("transcription") or "").lower() == "off":
        return False
    max_duration = int(source.get("max_duration_seconds") or 5400)
    if episode.duration_seconds and episode.duration_seconds > max_duration:
        return False
    return score >= float(source.get("transcribe_threshold") or 0.34)


async def _download_audio(episode: PodcastEpisode) -> Path:
    if not episode.audio_url:
        raise ValueError("Episode has no audio URL")
    path = _audio_path(episode)
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        async with client.stream("GET", episode.audio_url) as response:
            response.raise_for_status()
            with path.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)
    return path


def _run_transcription(audio_path: Path, transcript_path: Path) -> str:
    command = _transcribe_command()
    if not command:
        raise RuntimeError("No transcription command configured")
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = command.format(audio_path=str(audio_path), transcript_path=str(transcript_path))
    subprocess.run(shlex.split(rendered), check=True, timeout=7200)
    if not transcript_path.exists():
        raise RuntimeError("Transcription command did not create a transcript")
    return transcript_path.read_text(encoding="utf-8", errors="replace")


def _episode_payload_text(episode: PodcastEpisode, text: str, transcript_source: str) -> str:
    source_label = "Transcript" if transcript_source in {"transcript", "transcript_cache"} else "Show notes"
    parts = [
        episode.title,
        f"Podcast: {episode.show_name}",
        f"{source_label}: {_clean_text(text)}",
    ]
    if episode.duration_seconds:
        parts.insert(2, f"Duration: {episode.duration_seconds // 60} minutes")
    return "\n\n".join(part for part in parts if part)


def _already_seen(digest_id: str, episode: PodcastEpisode) -> bool:
    watermark = get_watermark(str(database.database_path()), digest_id, _source_key(episode.feed_url))
    if not watermark:
        return False
    if watermark.get("last_id") == episode.episode_id:
        return True
    last_fetched = watermark.get("last_fetched")
    if not last_fetched or not episode.published_at:
        return False
    try:
        last_at = datetime.fromisoformat(str(last_fetched))
        episode_at = datetime.fromisoformat(episode.published_at)
    except ValueError:
        return False
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=UTC)
    if episode_at.tzinfo is None:
        episode_at = episode_at.replace(tzinfo=UTC)
    return episode_at <= last_at


def _mark_seen(digest_id: str, episode: PodcastEpisode) -> None:
    upsert_watermark(
        str(database.database_path()),
        digest_id,
        _source_key(episode.feed_url),
        episode.published_at or database.utc_now(),
        episode.episode_id,
    )


def _source_feed_url(source: dict[str, Any]) -> str:
    return str(source.get("feed_url") or source.get("url") or "").strip()


def _source_apple_url(source: dict[str, Any]) -> str | None:
    explicit_url = str(source.get("apple_podcasts_url") or "").strip()
    if explicit_url.startswith(("https://podcasts.apple.com/", "https://itunes.apple.com/")):
        return explicit_url
    return _apple_url_from_itunes_id(source.get("itunes_id") or source.get("itunesId"))


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


def _source_key(feed_url: str) -> str:
    digest = hashlib.sha1(feed_url.encode("utf-8")).hexdigest()[:16]
    return f"podcast:{digest}"


def _audio_path(episode: PodcastEpisode) -> Path:
    suffix = Path(urlparse(episode.audio_url or "").path).suffix or ".mp3"
    return get_settings().data_dir / "podcast-audio" / f"{episode.episode_id}{suffix}"


def _transcript_path(episode: PodcastEpisode) -> Path:
    return get_settings().data_dir / "podcast-transcripts" / f"{episode.episode_id}.txt"


def _transcribe_command() -> str:
    return (get_settings().podcast_transcribe_command or "").strip()


def _inside_lookback(published_at: str | None, lookback_hours: int) -> bool:
    if not published_at:
        return True
    try:
        parsed = datetime.fromisoformat(published_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed >= datetime.now(UTC) - timedelta(hours=max(1, lookback_hours))


def _published_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _recency_score(published_at: str | None) -> float:
    if not published_at:
        return 0.45
    try:
        parsed = datetime.fromisoformat(published_at)
    except ValueError:
        return 0.45
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    age_hours = max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds() / 3600)
    return max(0.0, min(1.0, 1 - (age_hours / 168)))


def _child_text(element: ElementTree.Element, local_name: str) -> str | None:
    for child in list(element):
        if _local_name(child.tag).lower() == local_name.lower() and child.text:
            return child.text.strip()
    return None


def _enclosure_url(item: ElementTree.Element) -> str | None:
    for child in list(item):
        if _local_name(child.tag).lower() == "enclosure":
            url = str(child.attrib.get("url") or "").strip()
            if url:
                return url
    return None


def _duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    parts = [int(part) for part in text.split(":") if part.isdigit()]
    if not parts:
        return None
    total = 0
    for part in parts:
        total = (total * 60) + part
    return total


def _episode_id(feed_url: str, identity: str) -> str:
    return hashlib.sha1(f"{feed_url}|{identity}".encode("utf-8")).hexdigest()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean_html(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    return _clean_text(soup.get_text(" ", strip=True))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _discovery_query(digest_interest: str) -> str:
    tokens = [
        token
        for token in keyword_set(digest_interest)
        if token
        in {
            "agent",
            "agentic",
            "ai",
            "artificial",
            "coding",
            "infrastructure",
            "intelligence",
            "llm",
            "model",
            "openai",
            "product",
            "workflow",
        }
    ]
    return " ".join(tokens[:8]) or "artificial intelligence news"


def _decision(
    *,
    target: str,
    decision: str,
    action: str,
    confidence: float,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> AgentDecision:
    return AgentDecision(
        agent="podcast_scout",
        target=target,
        decision=decision,
        action=action,
        confidence=confidence,
        reason=reason,
        metadata=metadata or {},
    )
