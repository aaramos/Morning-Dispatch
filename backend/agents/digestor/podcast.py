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
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from backend.agents.agentic import AgentDecision
from backend.agents.digestor.base import NormalizedPayload, pii_filter
from backend.agents.digestor.podcast_http import (
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    _apple_url_from_itunes_id,
    _audio_path as _audio_path,
    _clean_html,
    _clean_text,
    _download_audio,
    _download_transcript_text,
    _flatten_json_transcript as _flatten_json_transcript,
    _lookup_apple_podcast_url,
    _normalize_title as _normalize_title,
    _normalize_url_for_match,
    _nullable_str as _nullable_str,
    aclose_shared_podcast_clients as aclose_shared_podcast_clients,
    discover_podcasts,
    shared_podcast_client as shared_podcast_client,
)
from backend.agents.digestor.podcast_resolution import (
    _extract_show_name_from_hit_title as _extract_show_name_from_hit_title,
    _feed_url_from_search_hit,
    _match_episode_in_feed,
    _resolve_feed_url,
    get_cached_resolution,
    set_cached_resolution,
)
from backend.agents.librarian.text_utils import keyword_set
from backend.app.core.config import get_settings
from backend.app.db import database
from backend.db.queries import get_watermark, upsert_watermark
import json

if TYPE_CHECKING:
    from backend.agents.discovery.types import TopicProfile

logger = logging.getLogger(__name__)

MAX_PODCAST_EPISODES = 8
MAX_DISCOVERED_FEEDS = 8
MAX_DISCOVERY_LANES = 8
MIN_EPISODE_SCORE = 0.22
# Podcast namespace that carries <podcast:transcript> elements (Podcasting 2.0).
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
# Hard ceiling for a single audio transcription so one slow episode cannot block
# the whole discovery lane. The loop-level budget below is the primary guard.
MAX_SINGLE_TRANSCRIPTION_SECONDS = 120
# Minimum headroom required before STARTING another audio transcription; below
# this we fall back to an existing transcript / show notes so the lane returns
# partial results instead of timing out to zero.
MIN_TRANSCRIPTION_HEADROOM_SECONDS = 20


def _deadline_remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _deadline_expired(deadline: float | None, *, min_remaining: float = 0.0) -> bool:
    remaining = _deadline_remaining(deadline)
    return remaining is not None and remaining <= min_remaining


async def _await_with_deadline(awaitable: Any, deadline: float | None) -> Any:
    remaining = _deadline_remaining(deadline)
    if remaining is None:
        return await awaitable
    if remaining <= 0:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise asyncio.TimeoutError("podcast lane deadline expired")
    return await asyncio.wait_for(awaitable, timeout=remaining)


async def _gather_with_deadline(awaitables: Iterable[Any], deadline: float | None) -> list[Any]:
    tasks = [asyncio.create_task(awaitable) for awaitable in awaitables]
    if not tasks:
        return []
    if deadline is None:
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    remaining = _deadline_remaining(deadline)
    if remaining is None:
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    done, pending = await asyncio.wait(tasks, timeout=remaining)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    results: list[Any] = []
    for task in tasks:
        if task in pending:
            results.append(asyncio.TimeoutError("podcast lane deadline expired"))
            continue
        try:
            results.append(task.result())
        except Exception as exc:
            results.append(exc)
    return results


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
    transcript_url: str | None = None
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
    transcription_budget_seconds: float | None = None,
    deadline: float | None = None,
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
            transcription_budget_seconds=transcription_budget_seconds,
            deadline=deadline,
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
    transcription_budget_seconds: float | None = None,
    deadline: float | None = None,
) -> tuple[list[NormalizedPayload], list[AgentDecision]]:
    podcast_sources = _podcast_sources(sources)
    if not podcast_sources:
        return [], decisions
    # Stop attempting audio transcription once this deadline passes so the lane
    # returns partial results (transcript-feed / show-notes) instead of timing out
    # to zero (P5). None preserves the legacy "always transcribe" behavior.
    transcription_deadline = (
        time.monotonic() + max(0.0, float(transcription_budget_seconds))
        if transcription_budget_seconds is not None
        else None
    )

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
            deadline=deadline,
        )
        # Skip the (unbounded) show-first fallback discovery when the lane is already
        # out of time, so we return partial results instead of overrunning the wall.
        if not resolved_search_episodes and not _deadline_expired(deadline):
            decisions.append(
                _decision(
                    target="podcast discovery",
                    decision="fallback",
                    action="show_first_discovery",
                    confidence=0.7,
                    reason="Episode-first search yielded no candidate episodes, falling back to show-first discovery.",
                )
            )
            try:
                discovered = await _await_with_deadline(
                    _discover_sources(search_sources, digest_interest),
                    deadline,
                )
            except asyncio.TimeoutError:
                logger.info("Podcast show-first fallback skipped because the lane deadline was reached.")
                discovered = []
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
    if feed_sources and not _deadline_expired(deadline):
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            batches = await _gather_with_deadline(
                (_timed_fetch_feed_episodes(client, source) for source in feed_sources),
                deadline,
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
        # Honor the overall lane deadline: stop processing further episodes and
        # return whatever has been gathered so far rather than risking a hard
        # adapter timeout that discards everything.
        if deadline is not None and time.monotonic() >= deadline:
            logger.info("Podcast lane hit its time budget; returning %d partial payload(s).", len(payloads))
            break
        episode_started = time.monotonic()
        transcript = ""
        transcript_source = "unknown"
        transcript_decisions: list[AgentDecision] = []
        metric: dict[str, Any] = {}
        if transcription_deadline is None:
            allow_transcription = True
            transcription_timeout: float | None = None
        else:
            remaining = transcription_deadline - time.monotonic()
            allow_transcription = remaining >= MIN_TRANSCRIPTION_HEADROOM_SECONDS
            transcription_timeout = remaining if allow_transcription else None
        try:
            transcript, transcript_source, transcript_decisions, metric = await _episode_text(
                episode,
                source,
                score,
                allow_transcription=allow_transcription,
                transcription_timeout=transcription_timeout,
            )
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


def _within_staleness(published_at: str | None, staleness_days: int) -> bool:
    """True only when a parseable publish date falls within the staleness window.

    Unlike _inside_lookback, an undated/unparseable episode is treated as STALE
    (returns False) so a confirmed show with no datable recent episode is
    suppressed rather than surfacing audio of unknown age.
    """
    if not published_at:
        return False
    try:
        parsed = datetime.fromisoformat(published_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed >= datetime.now(UTC) - timedelta(days=max(1, staleness_days))


def _latest_episode_with_audio(episodes: list[PodcastEpisode]) -> PodcastEpisode | None:
    playable = [ep for ep in episodes if ep.audio_url]
    if not playable:
        return None
    return max(playable, key=lambda ep: ep.published_at or "")


async def fetch_subscribed_show_latest(
    shows: list[dict[str, Any]],
    *,
    digest_id: str,
    staleness_days: int = 60,
    inference_run_id: str | None = None,
    transcription_budget_seconds: float | None = None,
    deadline: float | None = None,
) -> tuple[list[NormalizedPayload], list[AgentDecision]]:
    """Curated-subscription inclusion (the show-subscription model).

    For each confirmed show, fetch its feed and take the most recent episode with
    playable audio. The latest episode is always summarized REGARDLESS of topic fit
    or the brief's interest lookback; the only gate is a show-level staleness cutoff
    (default 60 days) so dead shows are suppressed with an honest note rather than
    surfacing very old audio.
    """
    decisions: list[AgentDecision] = []
    payloads: list[NormalizedPayload] = []
    if not shows:
        return payloads, decisions

    transcription_deadline = (
        time.monotonic() + max(0.0, float(transcription_budget_seconds))
        if transcription_budget_seconds is not None
        else None
    )

    feed_sources = [
        {"type": "podcast_rss", "feed_url": str(s.get("feed_url") or "").strip(), "title": str(s.get("title") or "Podcast").strip()}
        for s in shows
        if str(s.get("feed_url") or "").strip()
    ]
    if not feed_sources:
        return payloads, decisions

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        batches = await asyncio.gather(
            *[_timed_fetch_feed_episodes(client, source) for source in feed_sources],
            return_exceptions=True,
        )

        for source, batch in zip(feed_sources, batches, strict=False):
            show_title = source["title"]
            if isinstance(batch, BaseException):
                decisions.append(
                    _decision(
                        target=show_title,
                        decision="feed_error",
                        action="skip_show",
                        confidence=0.8,
                        reason="Subscribed show feed could not be read this run.",
                        metadata={"feed_url": source["feed_url"], "error": str(batch)[:200]},
                    )
                )
                continue
            episodes, feed_fetch_ms = batch
            latest = _latest_episode_with_audio(episodes)
            if latest is None:
                decisions.append(
                    _decision(
                        target=show_title,
                        decision="skip",
                        action="exclude_no_audio",
                        confidence=0.85,
                        reason="Subscribed show had no episode with playable audio.",
                        metadata={"feed_url": source["feed_url"]},
                    )
                )
                continue
            if not _within_staleness(latest.published_at, staleness_days):
                decisions.append(
                    _decision(
                        target=show_title,
                        decision="stale_show",
                        action="suppress_stale_show",
                        confidence=0.8,
                        reason=f"Subscribed show's latest episode is older than the {staleness_days}-day staleness cutoff.",
                        metadata={"feed_url": source["feed_url"], "published_at": latest.published_at},
                    )
                )
                continue

            episode_started = time.monotonic()
            if transcription_deadline is None:
                allow_transcription = True
                transcription_timeout: float | None = None
            else:
                remaining = transcription_deadline - time.monotonic()
                allow_transcription = remaining >= MIN_TRANSCRIPTION_HEADROOM_SECONDS
                transcription_timeout = remaining if allow_transcription else None

            try:
                transcript, transcript_source, transcript_decisions, metric = await _episode_text(
                    latest,
                    {"transcription": "auto", "title": show_title},
                    0.8,
                    allow_transcription=allow_transcription,
                    transcription_timeout=transcription_timeout,
                )
            except Exception as exc:
                logger.info("Subscribed show episode processing failed for %s: %s", show_title, exc)
                continue
            decisions.extend(transcript_decisions)
            metric.update(
                {
                    "digest_id": digest_id,
                    "inference_run_id": inference_run_id,
                    "feed_fetch_ms": feed_fetch_ms,
                    "total_ms": _elapsed_ms(episode_started),
                    "transcript_words": _word_count(transcript),
                }
            )
            raw_text = _episode_payload_text(latest, transcript, transcript_source)
            preferred_url = latest.apple_podcasts_url or latest.episode_url or latest.audio_url
            payload = NormalizedPayload(
                source_type="podcast_episode",
                source_name=latest.show_name or show_title,
                raw_text=raw_text,
                original_url=preferred_url,
                published_at=latest.published_at,
                metadata={
                    "podcast_episode_id": latest.episode_id,
                    "podcast_title": latest.show_name or show_title,
                    "title": latest.title,
                    "feed_url": latest.feed_url or source["feed_url"],
                    "apple_podcasts_url": latest.apple_podcasts_url,
                    "audio_url": latest.audio_url,
                    "episode_url": latest.episode_url,
                    "image_url": latest.image_url,
                    "duration_seconds": latest.duration_seconds,
                    "episode_quality_score": 0.8,
                    "transcript_source": transcript_source,
                    "subscribed_show": True,
                    "approved_podcast_latest": True,
                    **(latest.metadata or {}),
                },
            )
            if pii_filter(payload):
                payloads.append(payload)
                metric["status"] = "success"
            else:
                metric["status"] = "pii_filtered"
            database.record_podcast_metric(metric)

    return payloads, decisions


async def discover_candidate_shows(
    queries: list[str],
    *,
    limit: int = 12,
    staleness_days: int = 60,
    enrich: bool = True,
) -> list[dict[str, Any]]:
    """Show-first discovery for the picker: shows whose content matches the interest.

    Returns shows with a 'usual content' summary plus latest-episode metadata and a
    staleness flag, deduped by feed_url. Discovery is NOT bounded by the interest
    lookback — it finds relevant shows regardless of when they last matched.
    """
    discovered: dict[str, dict[str, Any]] = {}
    for query in [q for q in queries if str(q or "").strip()][:6]:
        try:
            for show in await discover_podcasts(query, limit=8):
                feed_url = str(show.get("feed_url") or "").strip()
                if feed_url:
                    discovered.setdefault(feed_url, show)
        except Exception as exc:
            logger.info("Candidate show discovery failed for %s: %s", query, exc)
        if len(discovered) >= limit:
            break

    candidates = [
        {
            "feed_url": show["feed_url"],
            "title": show.get("title") or "Podcast",
            "description": show.get("description") or "",
            "author": show.get("author"),
            "site_url": show.get("site_url"),
            "apple_podcasts_url": show.get("apple_podcasts_url"),
            "latest_episode_title": None,
            "latest_published_at": None,
            "stale": None,
        }
        for show in list(discovered.values())[:limit]
    ]

    if enrich and candidates:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            batches = await asyncio.gather(
                *[_timed_fetch_feed_episodes(client, {"type": "podcast_rss", "feed_url": c["feed_url"], "title": c["title"]}) for c in candidates],
                return_exceptions=True,
            )
        for cand, batch in zip(candidates, batches, strict=False):
            if isinstance(batch, BaseException):
                continue
            episodes, _ms = batch
            latest = _latest_episode_with_audio(episodes) or (episodes[0] if episodes else None)
            if latest is not None:
                cand["latest_episode_title"] = latest.title
                cand["latest_published_at"] = latest.published_at
                cand["stale"] = not _within_staleness(latest.published_at, staleness_days)
                if not cand["description"]:
                    cand["description"] = (latest.description or "")[:600]
    return candidates


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
                transcript_url=_feed_transcript_url(item),
            )
        )
    return episodes


def _feed_transcript_url(item: ElementTree.Element) -> str | None:
    """Pick the cheapest usable <podcast:transcript> URL from a feed item.

    Prefers plain text / SRT / VTT / JSON over HTML. Returning a transcript URL
    lets the pipeline skip audio download + local transcription entirely (PC1).
    """
    preference = {
        "text/plain": 0,
        "application/srt": 1,
        "text/srt": 1,
        "application/x-subrip": 1,
        "text/vtt": 2,
        "application/json": 3,
        "text/html": 4,
    }
    best_url: str | None = None
    best_rank = 99
    for element in item.iter():
        tag = element.tag.split("}")[-1].lower()
        if tag != "transcript":
            continue
        url = str(element.get("url") or "").strip()
        if not url:
            continue
        mime = str(element.get("type") or "").strip().lower()
        rank = preference.get(mime, 5)
        if rank < best_rank:
            best_rank = rank
            best_url = url
    return best_url


async def _episode_text(
    episode: PodcastEpisode,
    source: dict[str, Any],
    score: float,
    *,
    allow_transcription: bool = True,
    transcription_timeout: float | None = None,
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

    # Prefer a publisher-provided transcript (Podcasting 2.0) — a cheap HTTP fetch
    # that avoids audio download + local transcription entirely (PC1).
    if episode.transcript_url:
        transcript = await _download_transcript_text(episode.transcript_url)
        if _word_count(transcript) >= 50:
            try:
                transcript_path.parent.mkdir(parents=True, exist_ok=True)
                transcript_path.write_text(transcript, encoding="utf-8")
            except OSError:
                pass
            decisions.append(
                _decision(
                    target=episode.title,
                    decision="transcript_feed",
                    action="use_feed_transcript",
                    confidence=0.88,
                    reason="Podcast Librarian used the publisher's transcript instead of transcribing audio.",
                    metadata={"transcript_url": episode.transcript_url, "score": score},
                )
            )
            metric = _podcast_metric_base(
                episode=episode,
                score=score,
                transcript_source="transcript_feed",
                status="success",
                started_at=started_at,
            )
            metric["transcript_words"] = _word_count(transcript)
            return transcript, "transcript_feed", decisions, metric

    should_transcribe = allow_transcription and _should_transcribe(episode, source, score)
    if should_transcribe and episode.audio_url and _transcribe_command():
        try:
            download_started = time.monotonic()
            audio_path = await _download_audio(episode)
            download_ms = _elapsed_ms(download_started)
            audio_bytes = audio_path.stat().st_size if audio_path.exists() else None
            transcription_started = time.monotonic()
            transcript = _run_transcription(
                audio_path, transcript_path, timeout_seconds=transcription_timeout
            )
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


def _run_transcription(
    audio_path: Path,
    transcript_path: Path,
    *,
    timeout_seconds: float | None = None,
) -> str:
    command = _transcribe_command()
    if not command:
        raise RuntimeError("No transcription command configured")
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = command.format(audio_path=str(audio_path), transcript_path=str(transcript_path))
    # When a discovery-lane budget is supplied, bound a single transcription so one
    # slow episode cannot exceed the lane timeout (falls back to show notes on
    # timeout, caught by the caller). Legacy/offline callers (timeout_seconds=None)
    # keep the generous ceiling so full transcriptions can complete.
    if timeout_seconds is None:
        timeout = 7200.0
    else:
        timeout = max(1.0, min(float(timeout_seconds), float(MAX_SINGLE_TRANSCRIPTION_SECONDS)))
    subprocess.run(shlex.split(rendered), check=True, timeout=timeout)
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


def _source_key(feed_url: str) -> str:
    digest = hashlib.sha1(feed_url.encode("utf-8")).hexdigest()[:16]
    return f"podcast:{digest}"


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
    deadline: float | None = None,
) -> list[PodcastEpisode]:
    from backend.agents.discovery.web_search import lookback_to_days, search_web, SearchHit
    from backend.app.db.database import (
        get_cached_podcast_discovery,
        set_cached_podcast_discovery,
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

    if queries_to_fetch and not _deadline_expired(deadline):
        search_results = await _gather_with_deadline(
            (search_web(wq, limit=search_hits_limit, days=days) for wq in queries_to_fetch),
            deadline,
        )

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
    elif queries_to_fetch:
        for wq in queries_to_fetch:
            hits_by_query[wq] = []

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
        deadline=deadline,
    )

    resolved_episodes = []
    sem = asyncio.Semaphore(3)

    async def resolve_one(hit: Any, http_client: httpx.AsyncClient) -> PodcastEpisode | None:
        url_norm = _normalize_url_for_match(hit.url)
        cached_res = get_cached_resolution(url_norm)

        if cached_res is not None:
            feed_url = cached_res.get("feed_url")
            resolution_method = "cache"
        else:
            async with sem:
                feed_url = await _resolve_feed_url(http_client, hit.url, hit.title, decisions)
            resolution_method = "network"

        if not feed_url:
            # Cache failure for 1 hour (3600 seconds)
            set_cached_resolution(url_norm, None, None, None, 3600)
            return None

        # Cache success for 7 days (604800 seconds)
        if cached_res is None:
            set_cached_resolution(url_norm, feed_url, None, None, 7 * 24 * 3600)

        diagnostics["feed_resolved"] += 1

        try:
            response = await http_client.get(feed_url)
            response.raise_for_status()
            episodes = parse_podcast_feed(response.text, feed_url=feed_url)
        except Exception as exc:
            logger.info("Failed to fetch resolved feed %s: %s", feed_url, exc)
            return None

        matched_ep = _match_episode_in_feed(episodes, hit)
        if matched_ep is not None:
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
                matched_ep = None
            elif not _inside_lookback(matched_ep.published_at, lookback_hours):
                diagnostics["date_rejects"] += 1
                matched_ep = None

        if matched_ep is None:
            # The show's feed resolved but the specific episode could not be matched
            # (or was stale / had no audio). Fall back to the show's freshest in-window
            # episode with playable audio so a relevant show still contributes instead
            # of the lane yielding nothing.
            fallback_ep = _latest_in_window_episode(episodes, lookback_hours)
            if fallback_ep is None:
                return None
            diagnostics["episode_matched"] += 1
            decisions.append(
                _decision(
                    target=fallback_ep.title,
                    decision="fallback",
                    action="use_latest_show_episode",
                    confidence=0.6,
                    reason="Exact episode match failed; used the show's most recent in-window episode.",
                    metadata={"feed_url": feed_url, "hit": getattr(hit, "title", "")},
                )
            )
            matched_ep = fallback_ep

        # Check if apple_url is cached
        apple_url = cached_res.get("apple_url") if cached_res else None
        if not apple_url:
            async with sem:
                apple_url = await _lookup_apple_podcast_url(http_client, matched_ep, {"title": matched_ep.show_name})
            if apple_url:
                # Update resolution cache with the apple_url
                set_cached_resolution(url_norm, feed_url, None, apple_url, 7 * 24 * 3600)

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
        return matched_ep

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as http_client:
        tasks = [asyncio.ensure_future(resolve_one(hit, http_client)) for hit in kept[:resolution_attempts_limit]]
        if not tasks:
            return resolved_episodes
        # Feed resolution is the slowest, most timeout-prone phase. Bound it by the
        # overall lane deadline and keep whatever resolved in time, cancelling the
        # rest, so the lane returns partial results instead of timing out to zero.
        wait_timeout = _deadline_remaining(deadline)
        done, pending = await asyncio.wait(tasks, timeout=wait_timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            logger.info("Podcast resolution hit its time budget; %d resolution(s) cancelled.", len(pending))
        seen_episode_ids: set[str] = set()
        for task in done:
            try:
                res = task.result()
            except Exception as exc:
                logger.warning("Failed to resolve candidate episode: %s", exc)
                continue
            if res is None:
                continue
            # Multiple hits can fall back to the same show's latest episode; keep one.
            if res.episode_id in seen_episode_ids:
                continue
            seen_episode_ids.add(res.episode_id)
            resolved_episodes.append(res)

    return resolved_episodes


async def _screen_episodes_with_agent(
    hits: list[Any],
    digest_interest: str,
    profile: TopicProfile | None,
    decisions: list[AgentDecision],
    diagnostics: dict[str, int],
    deadline: float | None = None,
) -> list[Any]:
    from backend.app.services import model_routing
    from backend.app.core.prompt_loader import load_prompt
    settings = get_settings()
    try:
        resolution = model_routing.client_for_agent("refinement", settings=settings)
        client = resolution.client
    except Exception as exc:
        logger.warning("Failed to obtain client for podcast relevance agent: %s", exc)
        client = None

    if client is None or _deadline_expired(deadline, min_remaining=1.0):
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
        payload = await _await_with_deadline(
            client.complete_json(
                system=system_prompt,
                prompt=json.dumps(cand_list, ensure_ascii=False),
                max_tokens=2000,
            ),
            deadline,
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


def _latest_in_window_episode(
    episodes: list[PodcastEpisode], lookback_hours: int
) -> PodcastEpisode | None:
    """Most recent episode with playable audio that falls inside the lookback window."""
    in_window = [
        ep
        for ep in episodes
        if ep.audio_url and _inside_lookback(ep.published_at, lookback_hours)
    ]
    if not in_window:
        return None
    return max(in_window, key=lambda ep: ep.published_at or "")
