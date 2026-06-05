from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, replace, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
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
import json

logger = logging.getLogger(__name__)

MAX_PODCAST_EPISODES = 8
MAX_DISCOVERED_FEEDS = 8
MAX_DISCOVERY_LANES = 8
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
    image_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


async def fetch_podcast_episodes(
    *,
    digest_id: str,
    digest_interest: str,
    sources: list[dict[str, Any]],
    lookback_hours: int,
    max_episodes: int = MAX_PODCAST_EPISODES,
    inference_run_id: str | None = None,
    force_refresh: bool = False,
    mark_seen: bool = True,
    seen_requires_published: bool = False,
    include_seen: bool = False,
    profile: TopicProfile | None = None,
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
            force_refresh=force_refresh,
            mark_seen=mark_seen,
            seen_requires_published=seen_requires_published,
            include_seen=include_seen,
            decisions=decisions,
            profile=profile,
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
    force_refresh: bool,
    mark_seen: bool,
    seen_requires_published: bool,
    include_seen: bool,
    decisions: list[AgentDecision],
    profile: TopicProfile | None = None,
) -> tuple[list[NormalizedPayload], list[AgentDecision]]:
    podcast_sources = _podcast_sources(sources)
    if not podcast_sources:
        return [], decisions

    feed_sources = [source for source in podcast_sources if _source_feed_url(source)]
    search_sources = [source for source in podcast_sources if not _source_feed_url(source)]

    diagnostics = {
        "episode_pages_found": 0,
        "low_relevance_rejects": 0,
        "feed_resolved": 0,
        "episode_matched": 0,
        "no_audio_rejects": 0,
        "date_rejects": 0,
    }

    resolved_search_episodes = []
    discovered = []
    if search_sources:
        resolved_search_episodes = await _episode_first_search_and_resolve(
            digest_interest=digest_interest,
            lookback_hours=lookback_hours,
            search_sources=search_sources,
            profile=profile,
            decisions=decisions,
            diagnostics=diagnostics,
            max_episodes=max_episodes,
        )
        if not resolved_search_episodes:
            decisions.append(
                _decision(
                    target="podcast discovery",
                    decision="fallback",
                    action="show_first_discovery",
                    confidence=0.7,
                    reason="Episode-first search yielded no candidate episodes, falling back to show-first discovery.",
                )
            )
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
    
    batches = []
    if feed_sources:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            batches = await asyncio.gather(
                *[_timed_fetch_feed_episodes(client, source) for source in feed_sources],
                return_exceptions=True,
            )

    candidates: list[tuple[float, PodcastEpisode, dict[str, Any]]] = []
    feed_fetch_ms_by_url: dict[str, int] = {}
    
    if feed_sources and batches:
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
                already_seen = _already_seen(digest_id, episode, require_published=seen_requires_published)
                if already_seen and not force_refresh and not include_seen:
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
                if already_seen:
                    decisions.append(
                        _decision(
                            target=episode.title,
                            decision="recent_episode_reused",
                            action="reuse_cached_episode",
                            confidence=0.82,
                            reason="Podcast Scout reused a recent episode so the regenerated digest keeps podcast coverage without re-discovery.",
                            metadata={"score": score, "show": episode.show_name},
                        )
                    )
                candidates.append((score, episode, source))

    # Add resolved search episodes to candidates
    for episode in resolved_search_episodes:
        already_seen = _already_seen(digest_id, episode, require_published=seen_requires_published)
        if already_seen and not force_refresh and not include_seen:
            _record_skipped_podcast_metric(
                digest_id=digest_id,
                inference_run_id=inference_run_id,
                episode=episode,
                status="already_seen",
                feed_fetch_ms=100,
            )
            continue
        score = _score_episode(episode, digest_interest)
        if score < MIN_EPISODE_SCORE:
            _record_skipped_podcast_metric(
                digest_id=digest_id,
                inference_run_id=inference_run_id,
                episode=episode,
                status="low_score",
                feed_fetch_ms=100,
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
        if already_seen:
            decisions.append(
                _decision(
                    target=episode.title,
                    decision="recent_episode_reused",
                    action="reuse_cached_episode",
                    confidence=0.82,
                    reason="Podcast Scout reused a recent episode so the regenerated brief keeps podcast coverage without re-discovery.",
                    metadata={"score": score, "show": episode.show_name},
                )
            )
        source = search_sources[0] if search_sources else {"type": "podcast_search", "title": episode.show_name}
        candidates.append((score, episode, source))

    if search_sources:
        decisions.append(
            _decision(
                target="diagnostics",
                decision="diagnostics",
                action="report_diagnostics",
                confidence=1.0,
                reason="Episode-first search diagnostics report.",
                metadata=diagnostics,
            )
        )

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
                "image_url": episode.image_url,
                "duration_seconds": episode.duration_seconds,
                "episode_quality_score": score,
                "transcript_source": transcript_source,
                **(episode.metadata or {}),
            },
        )
        if pii_filter(payload):
            payloads.append(payload)
            if mark_seen and not force_refresh:
                _mark_seen(digest_id, episode)
            metric["status"] = "success"
        else:
            metric["status"] = "pii_filtered"
        database.record_podcast_metric(metric)

    return payloads, decisions


def mark_podcast_payloads_seen(digest_id: str, payloads: Iterable[NormalizedPayload]) -> int:
    marked_count = 0
    seen: set[tuple[str, str]] = set()
    for payload in payloads:
        if payload.source_type != "podcast_episode":
            continue
        metadata = payload.metadata or {}
        feed_url = str(metadata.get("feed_url") or "").strip()
        episode_id = str(metadata.get("podcast_episode_id") or "").strip()
        if not feed_url or not episode_id:
            continue
        key = (feed_url, episode_id)
        if key in seen:
            continue
        upsert_watermark(
            str(database.database_path()),
            digest_id,
            _source_key(feed_url),
            payload.published_at or database.utc_now(),
            episode_id,
        )
        marked_count += 1
        seen.add(key)
    return marked_count


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
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
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
    queries = _podcast_discovery_lanes(queries, digest_interest)
    discovered: dict[str, dict[str, Any]] = {}
    for query in queries[:MAX_DISCOVERY_LANES]:
        try:
            query_results = await discover_podcasts(query, limit=MAX_DISCOVERED_FEEDS)
        except Exception as exc:
            logger.info("Podcast discovery failed for %s: %s", query, exc)
            continue
        for source in query_results:
            feed_url = str(source.get("feed_url") or "")
            discovered.setdefault(feed_url, source)
        if len(discovered) >= MAX_DISCOVERED_FEEDS:
            break
    if len(discovered) < MAX_DISCOVERED_FEEDS:
        for source in await _discover_sources_from_web(queries, digest_interest):
            feed_url = str(source.get("feed_url") or "")
            if feed_url:
                discovered.setdefault(feed_url, source)
            if len(discovered) >= MAX_DISCOVERED_FEEDS:
                break
    return list(discovered.values())[:MAX_DISCOVERED_FEEDS]


def _podcast_discovery_lanes(queries: list[str], digest_interest: str) -> list[str]:
    lanes: list[str] = []
    for query in [*queries, _discovery_query(digest_interest)]:
        from backend.agents.librarian.text_utils import tokens, STOPWORDS
        seen = set()
        cleaned_words = []
        for token in tokens(query):
            if token not in STOPWORDS and len(token) > 1 and token not in seen:
                cleaned_words.append(token)
                seen.add(token)
        clean = " ".join(cleaned_words[:3])
        if not clean:
            continue
        for lane in (clean, f"{clean} podcast"):
            key = lane.casefold()
            if key not in {existing.casefold() for existing in lanes}:
                lanes.append(lane[:180])
    return lanes[:MAX_DISCOVERY_LANES]


async def _discover_sources_from_web(queries: list[str], digest_interest: str) -> list[dict[str, Any]]:
    from backend.agents.discovery.web_search import lookback_to_days, search_web

    days = lookback_to_days(24 * 365)
    discovered: dict[str, dict[str, Any]] = {}
    for query in queries[:3]:
        web_query = f"{query} podcast RSS feed"
        try:
            hits = await search_web(web_query, limit=8, days=days)
        except Exception as exc:
            logger.info("Podcast web discovery failed for %s: %s", web_query, exc)
            continue
        for hit in hits:
            feed_url = _feed_url_from_search_hit(hit.url)
            if feed_url:
                discovered.setdefault(
                    feed_url,
                    {
                        "type": "podcast_rss",
                        "title": hit.title or "Podcast",
                        "feed_url": feed_url,
                        "site_url": hit.url,
                        "aggregator": hit.provider,
                    },
                )
                continue
            title = _podcast_title_from_search_hit(hit.title, hit.url)
            if not title:
                continue
            try:
                for source in await discover_podcasts(title, limit=3):
                    candidate_text = " ".join(
                        str(source.get(field) or "")
                        for field in ("title", "author", "site_url")
                    )
                    if _feed_fit_score(candidate_text, digest_interest) < 0.08:
                        continue
                    feed_url = str(source.get("feed_url") or "")
                    if feed_url:
                        discovered.setdefault(feed_url, {**source, "aggregator": f"{hit.provider}+podcastindex"})
            except Exception as exc:
                logger.info("Podcast title lookup failed for %s: %s", title, exc)
        if len(discovered) >= MAX_DISCOVERED_FEEDS:
            break
    return list(discovered.values())[:MAX_DISCOVERED_FEEDS]


def _feed_url_from_search_hit(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    lowered = url.lower()
    if any(marker in lowered for marker in ("rss", "feed", ".xml", "podcast.xml")):
        return url
    return ""


def _podcast_title_from_search_hit(title: str, url: str) -> str:
    clean_title = re.sub(r"\s*[-|•]\s*(apple podcasts|spotify|podcast addict|listen notes|podcasts?)\s*$", "", str(title or ""), flags=re.I).strip()
    if clean_title and len(clean_title.split()) <= 12:
        return clean_title
    parsed = urlparse(str(url or ""))
    path = parsed.path.strip("/")
    if not path:
        return ""
    slug = path.rsplit("/", 1)[-1]
    slug = re.sub(r"[-_]+", " ", slug).strip()
    return slug[:120]


def _feed_fit_score(text: str, digest_interest: str) -> float:
    interest = keyword_set(digest_interest)
    if not interest:
        return 0.2
    tokens = keyword_set(text)
    return len(tokens & interest) / max(1, len(interest))


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
    show_image_url = _image_url(channel)
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
                image_url=_image_url(item) or show_image_url,
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
        "image_url": episode.image_url,
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
    denominator = min(10, len(interest_tokens)) if interest_tokens else 1
    overlap = len(episode_tokens & interest_tokens) / max(1, denominator) if interest_tokens else 0.35
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


def _already_seen(digest_id: str, episode: PodcastEpisode, *, require_published: bool = False) -> bool:
    watermark = get_watermark(str(database.database_path()), digest_id, _source_key(episode.feed_url))
    if not watermark:
        return False
    if watermark.get("last_id") == episode.episode_id:
        return _seen_watermark_counts(digest_id, episode, require_published=require_published)
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
    if episode_at <= last_at:
        return _seen_watermark_counts(digest_id, episode, require_published=require_published)
    return False


def _seen_watermark_counts(digest_id: str, episode: PodcastEpisode, *, require_published: bool) -> bool:
    if not require_published:
        return True
    return database.podcast_episode_was_published(digest_id, episode.episode_id)


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


def _image_url(element: ElementTree.Element) -> str | None:
    for child in element.iter():
        if _local_name(child.tag).lower() != "image":
            continue
        href = str(child.attrib.get("href") or child.attrib.get("url") or "").strip()
        if href.startswith(("http://", "https://")):
            return href
        nested_url = _child_text(child, "url")
        if nested_url and nested_url.startswith(("http://", "https://")):
            return nested_url
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
    from backend.agents.librarian.text_utils import tokens, STOPWORDS
    seen = set()
    cleaned = []
    for token in tokens(digest_interest):
        if token not in STOPWORDS and len(token) > 1 and token not in seen:
            cleaned.append(token)
            seen.add(token)
    return " ".join(cleaned[:8]) or "podcast"



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


async def _episode_first_search_and_resolve(
    digest_interest: str,
    lookback_hours: int,
    search_sources: list[dict[str, Any]],
    profile: TopicProfile | None,
    decisions: list[AgentDecision],
    diagnostics: dict[str, int],
    max_episodes: int = MAX_PODCAST_EPISODES,
) -> list[PodcastEpisode]:
    from backend.agents.discovery.web_search import lookback_to_days, search_web, SearchHit
    from backend.agents.discovery.types import TopicProfile
    from backend.app.db.database import (
        get_cached_podcast_discovery,
        set_cached_podcast_discovery,
        get_cached_podcast_resolution,
        set_cached_podcast_resolution,
    )
    days = lookback_to_days(lookback_hours)

    direct_queries = []
    related_queries = []
    negative_constraints = []
    priority_terms = []
    if profile:
        direct_queries = list(profile.direct_episode_queries)
        related_queries = list(profile.related_episode_queries)
        negative_constraints = list(profile.negative_constraints)
        priority_terms = list(profile.priority_terms)

    if not direct_queries:
        for source in search_sources:
            q = str(source.get("query") or source.get("title") or "").strip()
            if q:
                direct_queries.append(q)
        if not direct_queries:
            direct_queries = [digest_interest]

    # Dynamically scale limits based on requested episodes (Issue 3)
    queries_limit = max(6, min(12, max_episodes * 2))
    search_hits_limit = max(8, max_episodes * 2)
    resolution_attempts_limit = max(6, max_episodes + 2)

    web_queries_provenance = {}
    web_queries = []

    # 1. Direct Queries
    for dq in direct_queries:
        wq1 = f'"{dq}" podcast episode'
        web_queries.append(wq1)
        web_queries_provenance[wq1.strip().lower()] = {
            "discovery_query": dq,
            "discovery_query_type": "direct"
        }

        wq2 = f'"{dq}" interview conversation podcast'
        web_queries.append(wq2)
        web_queries_provenance[wq2.strip().lower()] = {
            "discovery_query": dq,
            "discovery_query_type": "direct"
        }

        for pt in priority_terms[:2]:
            wq3 = f'"{dq}" "{pt}" podcast'
            web_queries.append(wq3)
            web_queries_provenance[wq3.strip().lower()] = {
                "discovery_query": dq,
                "discovery_query_type": "direct",
                "priority_term": pt
            }

    # 2. Related Queries
    for rq in related_queries:
        wq1 = f'"{rq}" podcast episode'
        web_queries.append(wq1)
        web_queries_provenance[wq1.strip().lower()] = {
            "discovery_query": rq,
            "discovery_query_type": "related"
        }

        wq2 = f'"{rq}" interview conversation podcast'
        web_queries.append(wq2)
        web_queries_provenance[wq2.strip().lower()] = {
            "discovery_query": rq,
            "discovery_query_type": "related"
        }

    # Deduplicate keeping order
    unique_web_queries = []
    seen_wq = set()
    for wq in web_queries:
        wq_norm = wq.strip().lower()
        if wq_norm not in seen_wq:
            seen_wq.add(wq_norm)
            unique_web_queries.append(wq)

    final_web_queries = unique_web_queries[:queries_limit]

    # Caching / Execution for Web Searches
    lookback_bucket = str(lookback_hours)
    hits_by_query = {}
    queries_to_fetch = []

    for wq in final_web_queries:
        wq_norm = wq.strip().lower()
        cached = get_cached_podcast_discovery(wq_norm, "web_search", lookback_bucket)
        if cached is not None:
            hits_by_query[wq] = [
                SearchHit(
                    title=d.get("title", ""),
                    url=d.get("url", ""),
                    snippet=d.get("snippet", ""),
                    score=d.get("score", 0.5),
                    provider=d.get("provider", "web_search_cache"),
                    published_at=d.get("published_at"),
                )
                for d in cached
            ]
        else:
            queries_to_fetch.append(wq)

    if queries_to_fetch:
        search_tasks = [search_web(wq, limit=search_hits_limit, days=days) for wq in queries_to_fetch]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        for wq, res in zip(queries_to_fetch, search_results):
            if isinstance(res, list):
                hits_by_query[wq] = res
                results_dict = [
                    {
                        "title": hit.title,
                        "url": hit.url,
                        "snippet": hit.snippet,
                        "score": hit.score,
                        "provider": hit.provider,
                        "published_at": hit.published_at,
                    }
                    for hit in res
                ]
                set_cached_podcast_discovery(wq.strip().lower(), "web_search", lookback_bucket, results_dict, 7 * 24 * 3600)
            else:
                logger.info("Search failed for query '%s': %s", wq, res)
                hits_by_query[wq] = []
                set_cached_podcast_discovery(wq.strip().lower(), "web_search", lookback_bucket, [], 3600)

    # Merge hits and assign provenance
    hits = []
    seen_urls = set()
    provenance_by_url = {}

    for wq in final_web_queries:
        wq_hits = hits_by_query.get(wq, [])
        wq_norm = wq.strip().lower()
        prov = web_queries_provenance.get(wq_norm, {"discovery_query": wq, "discovery_query_type": "direct"})

        for hit in wq_hits:
            url_norm = _normalize_url_for_match(hit.url)
            if url_norm not in seen_urls:
                seen_urls.add(url_norm)
                hits.append(hit)
                provenance_by_url[url_norm] = prov

    diagnostics["episode_pages_found"] += len(hits)
    if not hits:
        return []

    # Negative constraints filtering
    non_blocked_hits = []
    for hit in hits:
        matched_constraint = None
        title_lower = hit.title.lower() if hit.title else ""
        snippet_lower = hit.snippet.lower() if hit.snippet else ""

        for nc in negative_constraints:
            nc_lower = nc.strip().lower()
            if not nc_lower:
                continue
            if nc_lower in title_lower or nc_lower in snippet_lower:
                matched_constraint = nc
                break

        if matched_constraint:
            decisions.append(
                _decision(
                    target=hit.title,
                    decision="drop",
                    action="exclude_negative_constraint",
                    confidence=1.0,
                    reason=f"Filtered out because title or snippet matched negative constraint: '{matched_constraint}'",
                    metadata={"rejected_constraints": [matched_constraint]},
                )
            )
            logger.info("Filtered candidate '%s' due to negative constraint: '%s'", hit.title, matched_constraint)
        else:
            non_blocked_hits.append(hit)

    if not non_blocked_hits:
        return []

    kept = await _screen_episodes_with_agent(
        hits=non_blocked_hits,
        digest_interest=digest_interest,
        profile=profile,
        decisions=decisions,
        diagnostics=diagnostics,
    )

    resolved_episodes = []
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as http_client:
        for hit in kept[:resolution_attempts_limit]:
            url_norm = _normalize_url_for_match(hit.url)
            cached_res = get_cached_podcast_resolution(url_norm)

            if cached_res is not None:
                feed_url = cached_res.get("feed_url")
                resolution_method = "cache"
            else:
                feed_url = await _resolve_feed_url(http_client, hit.url, hit.title, decisions)
                resolution_method = "network"

            if not feed_url:
                # Cache failure for 1 hour (3600 seconds)
                set_cached_podcast_resolution(url_norm, None, None, None, 3600)
                continue

            # Cache success for 7 days (604800 seconds)
            if cached_res is None:
                set_cached_podcast_resolution(url_norm, feed_url, None, None, 7 * 24 * 3600)

            diagnostics["feed_resolved"] += 1

            try:
                response = await http_client.get(feed_url)
                response.raise_for_status()
                episodes = parse_podcast_feed(response.text, feed_url=feed_url)
            except Exception as exc:
                logger.info("Failed to fetch resolved feed %s: %s", feed_url, exc)
                continue

            matched_ep = _match_episode_in_feed(episodes, hit)
            if not matched_ep:
                continue

            diagnostics["episode_matched"] += 1

            if not matched_ep.audio_url:
                diagnostics["no_audio_rejects"] += 1
                decisions.append(
                    _decision(
                        target=matched_ep.title,
                        decision="skip",
                        action="exclude_no_audio",
                        confidence=0.9,
                        reason="Excluded because the resolved episode has no playable audio URL.",
                        metadata={"feed_url": feed_url},
                    )
                )
                continue

            if not _inside_lookback(matched_ep.published_at, lookback_hours):
                diagnostics["date_rejects"] += 1
                continue

            # Check if apple_url is cached
            apple_url = cached_res.get("apple_url") if cached_res else None
            if not apple_url:
                apple_url = await _lookup_apple_podcast_url(http_client, matched_ep, {"title": matched_ep.show_name})
                if apple_url:
                    # Update resolution cache with the apple_url
                    set_cached_podcast_resolution(url_norm, feed_url, None, apple_url, 7 * 24 * 3600)

            if apple_url:
                matched_ep = replace(matched_ep, apple_podcasts_url=apple_url)

            # Inject provenance/query metadata
            prov = provenance_by_url.get(url_norm, {})
            meta = {
                "discovery_query": prov.get("discovery_query"),
                "discovery_query_type": prov.get("discovery_query_type"),
                "resolution_method": resolution_method,
            }
            if prov.get("priority_term"):
                meta["priority_term"] = prov["priority_term"]

            matched_ep = replace(matched_ep, metadata=meta)
            resolved_episodes.append(matched_ep)

    return resolved_episodes


async def _screen_episodes_with_agent(
    hits: list[Any],
    digest_interest: str,
    profile: TopicProfile | None,
    decisions: list[AgentDecision],
    diagnostics: dict[str, int],
) -> list[Any]:
    from backend.app.services import model_routing
    from backend.agents.discovery.types import TopicProfile
    from backend.app.core.prompt_loader import load_prompt
    settings = get_settings()
    try:
        resolution = model_routing.client_for_agent("refinement", settings=settings)
        client = resolution.client
    except Exception as exc:
        logger.warning("Failed to obtain client for podcast relevance agent: %s", exc)
        client = None

    if client is None:
        kept = []
        for hit in hits:
            score = _feed_fit_score(f"{hit.title} {hit.snippet}", digest_interest)
            if score >= 0.08:
                kept.append(hit)
            else:
                diagnostics["low_relevance_rejects"] += 1
        return kept

    statement = profile.statement if profile else digest_interest
    scope = profile.scope if profile else digest_interest
    exclusions = ", ".join(profile.exclusions) if (profile and profile.exclusions) else ""

    cand_list = []
    for idx, hit in enumerate(hits):
        cand_list.append({
            "index": idx,
            "title": hit.title,
            "url": hit.url,
            "snippet": hit.snippet[:300] if hit.snippet else "",
        })

    system_prompt = load_prompt("podcast_relevance")
    system_prompt = system_prompt.replace("{{statement}}", statement)
    system_prompt = system_prompt.replace("{{scope}}", scope)
    system_prompt = system_prompt.replace("{{exclusions}}", exclusions)

    try:
        payload = await client.complete_json(
            system=system_prompt,
            prompt=json.dumps(cand_list, ensure_ascii=False),
            max_tokens=2000,
        )
        decisions_list = payload.get("decisions", [])
        decisions_map = {}
        for d in decisions_list:
            if isinstance(d, dict) and d.get("index") is not None:
                decisions_map[int(d["index"])] = (
                    str(d.get("decision")).strip().lower(),
                    float(d.get("score") if d.get("score") is not None else 0.0),
                    str(d.get("reason") or ""),
                )
    except Exception as exc:
        logger.warning("LLM podcast relevance screening failed: %s", exc)
        kept = []
        for hit in hits:
            score = _feed_fit_score(f"{hit.title} {hit.snippet}", digest_interest)
            if score >= 0.08:
                kept.append(hit)
            else:
                diagnostics["low_relevance_rejects"] += 1
        return kept

    kept = []
    borderline_candidates = []
    reject_decisions = {"drop", "skip"}
    for idx, hit in enumerate(hits):
        decision_info = decisions_map.get(idx)
        if decision_info:
            decision, score, reason = decision_info
            if decision == "keep" and score >= 0.35:
                kept.append(hit)
                decisions.append(
                    _decision(
                        target=hit.title,
                        decision="keep",
                        action="keep_episode_candidate",
                        confidence=score,
                        reason=f"Podcast relevance agent approved: {reason}",
                        metadata={"url": hit.url},
                    )
                )
            elif decision in reject_decisions:
                # Explicit drop/skip decision (Issue 5 / Drop Bug Fix)
                diagnostics["low_relevance_rejects"] += 1
                decisions.append(
                    _decision(
                        target=hit.title,
                        decision="skip",
                        action="drop_episode_candidate",
                        confidence=score,
                        reason=f"Podcast relevance agent rejected: {reason}",
                        metadata={"url": hit.url},
                    )
                )
                if 0.25 <= score < 0.35:
                    borderline_candidates.append(hit)
            else:
                # Uncertain or invalid decision string - keep it (Issue 5)
                kept.append(hit)
                decisions.append(
                    _decision(
                        target=hit.title,
                        decision="keep_uncertain",
                        action="keep_episode_candidate",
                        confidence=score,
                        reason=f"Podcast relevance agent uncertain (decision: {decision}): {reason}",
                        metadata={"url": hit.url},
                    )
                )
        else:
            kept.append(hit)

    # If everything got screened out, pick up to 2 borderline candidates as fallback (Issue 5)
    if not kept and borderline_candidates:
        fallback_sample = borderline_candidates[:2]
        kept.extend(fallback_sample)
        # Log fallback sample inclusion
        for hit in fallback_sample:
            decisions.append(
                _decision(
                    target=hit.title,
                    decision="keep_fallback",
                    action="keep_episode_candidate",
                    confidence=0.3,
                    reason="Retained candidate as borderline LLM fallback to preserve discovery recall.",
                    metadata={"url": hit.url},
                )
            )

    return kept


async def _resolve_feed_url(
    client: httpx.AsyncClient,
    url: str,
    title: str,
    decisions: list[AgentDecision],
) -> str | None:
    if "podcasts.apple.com" in url or "itunes.apple.com" in url:
        match = re.search(r"/id(\d+)", url)
        if match:
            itunes_id = match.group(1)
            try:
                response = await client.get(
                    "https://itunes.apple.com/lookup",
                    params={"id": itunes_id},
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])
                if results and isinstance(results[0], dict):
                    feed_url = results[0].get("feedUrl")
                    if feed_url:
                        decisions.append(
                            _decision(
                                target=title,
                                decision="resolved",
                                action="itunes_lookup",
                                confidence=0.95,
                                reason=f"Resolved RSS feed from Apple iTunes ID {itunes_id}.",
                                metadata={"feed_url": feed_url},
                            )
                        )
                        return feed_url
            except Exception as exc:
                logger.info("iTunes lookup failed for ID %s: %s", itunes_id, exc)

    show_name = _extract_show_name_from_hit_title(title)
    if show_name:
        try:
            results = await discover_podcasts(show_name, limit=3)
            if results:
                feed_url = results[0].get("feed_url")
                if feed_url:
                    decisions.append(
                        _decision(
                            target=title,
                            decision="resolved",
                            action="podcast_index_lookup",
                            confidence=0.85,
                            reason=f"Resolved RSS feed from Podcast Index show search for '{show_name}'.",
                            metadata={"feed_url": feed_url},
                        )
                    )
                    return feed_url
        except Exception as exc:
            logger.info("Podcast Index lookup failed for show '%s': %s", show_name, exc)

    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            link_type = str(link.get("type") or "").lower()
            link_href = str(link.get("href") or "").strip()
            if ("rss" in link_type or "xml" in link_type) and link_href:
                from urllib.parse import urljoin
                feed_url = urljoin(url, link_href)
                decisions.append(
                    _decision(
                        target=title,
                        decision="resolved",
                        action="rss_autodiscovery",
                        confidence=0.88,
                        reason="Resolved RSS feed via autodiscovery link on page.",
                        metadata={"feed_url": feed_url},
                    )
                )
                return feed_url
    except Exception as exc:
        logger.info("RSS Autodiscovery failed for URL %s: %s", url, exc)

    if show_name:
        web_query = f"{show_name} podcast RSS feed"
        try:
            from backend.agents.discovery.web_search import lookback_to_days, search_web
            days = lookback_to_days(24 * 365)
            hits = await search_web(web_query, limit=3, days=days)
            for hit in hits:
                feed_url = _feed_url_from_search_hit(hit.url)
                if feed_url:
                    decisions.append(
                        _decision(
                            target=title,
                            decision="resolved",
                            action="rss_web_search",
                            confidence=0.80,
                            reason=f"Resolved RSS feed via web search for '{show_name} RSS feed'.",
                            metadata={"feed_url": feed_url},
                        )
                    )
                    return feed_url
        except Exception as exc:
            logger.info("RSS web search fallback failed for show '%s': %s", show_name, exc)

    return None


def _extract_show_name_from_hit_title(title: str) -> str:
    cleaned = re.sub(r"\s*[-|•:|]\s*(apple podcasts|spotify|podcast addict|listen notes|podcasts?|youtube)\s*$", "", title, flags=re.I).strip()
    for sep in ("|", "-", "•", ":"):
        if sep in cleaned:
            parts = cleaned.split(sep)
            for part in parts:
                p = part.strip()
                if "episode" not in p.lower() and "interview" not in p.lower() and len(p.split()) <= 5 and len(p) > 2:
                    return p
    return cleaned


def _match_episode_in_feed(episodes: list[PodcastEpisode], hit: Any) -> PodcastEpisode | None:
    cand_url = _normalize_url_for_match(hit.url).lower()
    for ep in episodes:
        if ep.episode_url:
            ep_url_norm = _normalize_url_for_match(ep.episode_url).lower()
            if ep_url_norm in cand_url or cand_url in ep_url_norm:
                return ep
        if ep.audio_url:
            ep_audio_norm = _normalize_url_for_match(ep.audio_url).lower()
            if ep_audio_norm in cand_url or cand_url in ep_audio_norm:
                return ep

    # Try Szymkiewicz-Simpson token overlap matching
    from backend.agents.librarian.text_utils import keyword_set
    cand_tokens = keyword_set(hit.title)
    if cand_tokens:
        for ep in episodes:
            ep_tokens = keyword_set(ep.title)
            if not ep_tokens:
                continue
            overlap = len(cand_tokens & ep_tokens) / max(1, min(len(cand_tokens), len(ep_tokens)))
            if overlap >= 0.65:
                return ep

    # Fallback to normalized subtitle/substring matching (Issue 4)
    def clean_title_for_soft_match(t: str, show_name: str | None = None) -> str:
        t_clean = t.lower()
        if show_name:
            # Strip show name suffix/prefix
            sn = show_name.lower()
            t_clean = re.sub(rf"\b{re.escape(sn)}\b", "", t_clean)
        # Strip common podcast markers & episode numbering patterns
        t_clean = re.sub(r"\b(episode|ep|show)\s*\d+\b", "", t_clean)
        t_clean = re.sub(r"[^\w\s]", " ", t_clean)
        return " ".join(t_clean.split())

    for ep in episodes:
        cand_clean = clean_title_for_soft_match(hit.title, ep.show_name)
        ep_clean = clean_title_for_soft_match(ep.title, ep.show_name)
        if not cand_clean or not ep_clean:
            continue
        # Check substring containment
        if cand_clean in ep_clean or ep_clean in cand_clean:
            return ep
        # Cleaned token overlap
        cand_clean_tokens = set(cand_clean.split())
        ep_clean_tokens = set(ep_clean.split())
        # Filter short/worthless tokens
        cand_clean_tokens = {tok for tok in cand_clean_tokens if len(tok) > 2}
        ep_clean_tokens = {tok for tok in ep_clean_tokens if len(tok) > 2}
        if cand_clean_tokens and ep_clean_tokens:
            overlap = len(cand_clean_tokens & ep_clean_tokens) / max(1, min(len(cand_clean_tokens), len(ep_clean_tokens)))
            if overlap >= 0.75:
                return ep

    return None
