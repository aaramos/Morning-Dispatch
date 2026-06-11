from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from backend.app.services.brief_renderer import _nullable_int


from .core import (
    PODCAST_SOURCE_TYPES,
    connect,
    logger,
    new_id,
    utc_now,
    _nullable_float,
    _nullable_str,
)
from .digests import get_digest, list_digests, update_digest

def get_cached_podcast_discovery(query_normalized: str, provider: str, lookback_bucket: str) -> list[dict[str, Any]] | None:
    now = utc_now()
    with connect() as connection:
        row = connection.execute(
            """
            SELECT results_json FROM podcast_discovery_cache
            WHERE query_normalized = ? AND provider = ? AND lookback_bucket = ? AND expires_at > ?
            """,
            (query_normalized, provider, lookback_bucket, now),
        ).fetchone()
        if row:
            try:
                return json.loads(row["results_json"])
            except Exception:
                return None
    return None

def set_cached_podcast_discovery(query_normalized: str, provider: str, lookback_bucket: str, results: list[dict[str, Any]], ttl_seconds: int) -> None:
    now = datetime.now(UTC)
    created_at = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    results_json = json.dumps(results, ensure_ascii=False)
    with connect() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO podcast_discovery_cache
            (query_normalized, provider, lookback_bucket, results_json, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (query_normalized, provider, lookback_bucket, results_json, created_at, expires_at),
        )

def get_cached_podcast_resolution(episode_url_normalized: str) -> dict[str, Any] | None:
    now = utc_now()
    with connect() as connection:
        row = connection.execute(
            """
            SELECT feed_url, podcast_index_id, apple_url FROM podcast_resolution_cache
            WHERE episode_url_normalized = ? AND expires_at > ?
            """,
            (episode_url_normalized, now),
        ).fetchone()
        if row:
            return {
                "feed_url": row["feed_url"],
                "podcast_index_id": row["podcast_index_id"],
                "apple_url": row["apple_url"],
            }
    return None

def set_cached_podcast_resolution(episode_url_normalized: str, feed_url: str | None, podcast_index_id: str | None, apple_url: str | None, ttl_seconds: int) -> None:
    now = datetime.now(UTC)
    resolved_at = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    with connect() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO podcast_resolution_cache
            (episode_url_normalized, feed_url, podcast_index_id, apple_url, resolved_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (episode_url_normalized, feed_url, podcast_index_id, apple_url, resolved_at, expires_at),
        )

def list_podcast_sources(digest_id: str | None = None) -> list[dict[str, Any]]:
    digests = list_digests(include_archived=True)
    records: list[dict[str, Any]] = []
    for digest in digests:
        if digest_id and str(digest["id"]) != digest_id:
            continue
        sources = digest.get("sources") if isinstance(digest.get("sources"), list) else []
        for source in sources:
            if not isinstance(source, dict) or source.get("type") not in PODCAST_SOURCE_TYPES:
                continue
            record = {**source}
            record["key"] = _podcast_source_key(source)
            record["digest_id"] = digest["id"]
            record["digest_name"] = digest["name"]
            records.append(record)
    return records

def add_podcast_source(digest_id: str, source: dict[str, Any]) -> dict[str, Any] | None:
    digest = get_digest(digest_id)
    if digest is None:
        return None
    normalized = _normalize_podcast_source(source)
    if normalized is None:
        raise ValueError("Podcast source needs either a feed URL or a search query")

    existing_sources = list(digest.get("sources") if isinstance(digest.get("sources"), list) else [])
    source_key = _podcast_source_key(normalized)
    updated_sources = [
        item
        for item in existing_sources
        if not (isinstance(item, dict) and item.get("type") in PODCAST_SOURCE_TYPES and _podcast_source_key(item) == source_key)
    ]
    updated_sources.append(normalized)
    return update_digest(digest_id, {"sources": updated_sources})

def remove_podcast_source(digest_id: str, source_key: str) -> dict[str, Any] | None:
    digest = get_digest(digest_id)
    if digest is None:
        return None
    existing_sources = list(digest.get("sources") if isinstance(digest.get("sources"), list) else [])
    updated_sources = [
        item
        for item in existing_sources
        if not (isinstance(item, dict) and item.get("type") in PODCAST_SOURCE_TYPES and _podcast_source_key(item) == source_key)
    ]
    return update_digest(digest_id, {"sources": updated_sources})

def podcast_episode_was_published(digest_id: str, episode_id: str) -> bool:
    if not episode_id:
        return False
    with connect() as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM digest_items di
            JOIN article_discoveries ad ON ad.id = di.discovery_id
            WHERE di.digest_id = ?
              AND ad.discovery_source_type = 'podcast_episode'
              AND ad.thread_id = ?
              AND COALESCE(di.tier, '') != 'dropped'
            LIMIT 1
            """,
            (digest_id, episode_id),
        ).fetchone()
    return row is not None

def _normalize_podcast_source(source: dict[str, Any]) -> dict[str, Any] | None:
    feed_url = str(source.get("feed_url") or source.get("url") or "").strip()
    query = str(source.get("query") or "").strip()
    source_type = str(source.get("type") or "").strip()
    if feed_url:
        parsed = urlparse(feed_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        title = str(source.get("title") or parsed.netloc.removeprefix("www.") or "Podcast").strip()
        return {
            "type": "podcast_rss",
            "title": title[:180],
            "feed_url": feed_url,
            "site_url": _nullable_str(source.get("site_url")),
            "author": _nullable_str(source.get("author")),
            "aggregator": _nullable_str(source.get("aggregator")),
            "itunes_id": _nullable_str(source.get("itunes_id") or source.get("itunesId")),
            "apple_podcasts_url": _nullable_str(source.get("apple_podcasts_url")),
            "transcription": str(source.get("transcription") or "auto"),
        }
    if source_type == "podcast_search" or query:
        if not query:
            return None
        return {
            "type": "podcast_search",
            "title": str(source.get("title") or f"Search: {query}").strip()[:180],
            "query": query[:220],
            "aggregator": _nullable_str(source.get("aggregator") or "podcastindex"),
            "transcription": str(source.get("transcription") or "auto"),
        }
    return None

def _podcast_source_key(source: dict[str, Any]) -> str:
    if source.get("type") == "podcast_search":
        value = str(source.get("query") or "").strip().lower()
        return "search:" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    value = str(source.get("feed_url") or source.get("url") or "").strip().lower()
    return "feed:" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]

def record_podcast_metric(metric: dict[str, Any]) -> str | None:
    digest_id = str(metric.get("digest_id") or "")
    if digest_id:
        with connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM digests WHERE id = ?", (digest_id,)
            ).fetchone()
            if not exists:
                logger.info(
                    "Skipping record_podcast_metric: digest_id %s not in digests table (exploration run)",
                    digest_id,
                )
                return None
    metric_id = str(metric.get("id") or new_id())
    record = {
        "id": metric_id,
        "digest_id": str(metric.get("digest_id") or ""),
        "inference_run_id": _nullable_str(metric.get("inference_run_id")),
        "ts": str(metric.get("ts") or utc_now()),
        "show_name": _nullable_str(metric.get("show_name")),
        "episode_id": _nullable_str(metric.get("episode_id")),
        "episode_title": _nullable_str(metric.get("episode_title")),
        "feed_url": _nullable_str(metric.get("feed_url")),
        "audio_url": _nullable_str(metric.get("audio_url")),
        "episode_url": _nullable_str(metric.get("episode_url")),
        "apple_podcasts_url": _nullable_str(metric.get("apple_podcasts_url")),
        "published_at": _nullable_str(metric.get("published_at")),
        "duration_seconds": _nullable_int(metric.get("duration_seconds")),
        "quality_score": _nullable_float(metric.get("quality_score")),
        "transcript_source": _nullable_str(metric.get("transcript_source")),
        "status": str(metric.get("status") or "unknown"),
        "error_detail": _nullable_str(metric.get("error_detail")),
        "feed_fetch_ms": _nullable_int(metric.get("feed_fetch_ms")),
        "audio_download_ms": _nullable_int(metric.get("audio_download_ms")),
        "transcription_ms": _nullable_int(metric.get("transcription_ms")),
        "total_ms": _nullable_int(metric.get("total_ms")),
        "audio_bytes": _nullable_int(metric.get("audio_bytes")),
        "transcript_words": _nullable_int(metric.get("transcript_words")),
        "cache_hit": int(bool(metric.get("cache_hit"))),
    }
    columns = ", ".join(record.keys())
    placeholders = ", ".join("?" for _ in record)
    with connect() as connection:
        connection.execute(
            f"INSERT INTO podcast_metrics ({columns}) VALUES ({placeholders})",
            tuple(record.values()),
        )
    return metric_id

def podcast_metrics_summary(*, limit: int = 500) -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM podcast_metrics
            ORDER BY ts DESC
            LIMIT ?
            """,
            (max(1, min(limit, 5000)),),
        ).fetchall()

    records = [dict(row) for row in rows]
    status_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    total_download_ms = 0
    total_transcription_ms = 0
    total_ms = 0
    download_count = 0
    transcription_count = 0
    total_counted = 0
    cache_hits = 0
    audio_bytes = 0
    transcript_words = 0
    latest_ts: str | None = None
    for record in records:
        status = str(record.get("status") or "unknown")
        source = str(record.get("transcript_source") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
        latest_ts = latest_ts or record.get("ts")
        cache_hits += int(record.get("cache_hit") or 0)
        audio_bytes += int(record.get("audio_bytes") or 0)
        transcript_words += int(record.get("transcript_words") or 0)
        if record.get("audio_download_ms") is not None:
            total_download_ms += int(record["audio_download_ms"])
            download_count += 1
        if record.get("transcription_ms") is not None:
            total_transcription_ms += int(record["transcription_ms"])
            transcription_count += 1
        if record.get("total_ms") is not None:
            total_ms += int(record["total_ms"])
            total_counted += 1

    return {
        "record_count": len(records),
        "latest_ts": latest_ts,
        "status_counts": status_counts,
        "transcript_source_counts": source_counts,
        "cache_hit_count": cache_hits,
        "audio_bytes": audio_bytes,
        "transcript_words": transcript_words,
        "avg_download_ms": round(total_download_ms / download_count) if download_count else None,
        "avg_transcription_ms": round(total_transcription_ms / transcription_count) if transcription_count else None,
        "avg_total_ms": round(total_ms / total_counted) if total_counted else None,
        "recent": records[:20],
    }
