from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from html import escape, unescape
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup

from backend.agents.agentic import AgentDecision
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.editor import build_issue_snapshot
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.core.config import ensure_runtime_dirs, get_settings
from backend.app.db.schema import SCHEMA_SQL

MODEL_ENRICHMENT_CACHE_VERSION = "librarian-v1"
PODCAST_SOURCE_TYPES = {"podcast_rss", "podcast_search"}
INFERENCE_METRIC_STATUSES = (
    "success",
    "timeout",
    "parse_error",
    "empty_output",
    "truncated",
    "rate_limited",
    "model_capacity",
    "http_error",
    "model_error",
)
RAW_URL_RE = re.compile(r"https?://[^\s<>)\"']+", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]{1,180})\]\((https?://[^)\s]+)[^)]*\)", re.IGNORECASE)
IMAGE_PLACEHOLDER_RE = re.compile(r"(?:[-–—]{2,}\s*)?View image:\s*\([^)]*(?:\)|$)\s*(?:Caption:\s*)?", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff\u00ad]+")
REFERENCE_MARK_RE = re.compile(r"\[\d+\]")
SEPARATOR_RE = re.compile(r"(?:[-–—]\s*){3,}")
NEWSLETTER_UTILITY_LABELS = {
    "advertise",
    "archive",
    "click here",
    "follow on x",
    "manage preferences",
    "read online",
    "read it in full online",
    "sign up",
    "signup",
    "subscribe",
    "unsubscribe",
    "view in browser",
    "view online",
    "work with us",
}
NEWSLETTER_BOILERPLATE_PATTERNS = (
    re.compile(r"\bOops!\s*Looks like your email provider is scrambling the email.*?(?=(?:[A-Z][a-z]+[,.:;!?]|\Z))", re.IGNORECASE),
    re.compile(r"\bClick here to read it in full online:?", re.IGNORECASE),
    re.compile(r"\bWe'd hate to see you go,\s*but if you want to unsubscribe.*$", re.IGNORECASE),
    re.compile(r"\bIf you want to unsubscribe,\s*please click here:?.*$", re.IGNORECASE),
    re.compile(r"\bTogether with\s*·?\s*Today's Author\b.*$", re.IGNORECASE),
    re.compile(r"\bToday's Author\b.*$", re.IGNORECASE),
    re.compile(r"\bView Online\s+TLDR\s+TOGETHER WITH\b.*$", re.IGNORECASE),
    re.compile(r"\bTLDR\s+TOGETHER WITH\b.*$", re.IGNORECASE),
    re.compile(r"\bSignup\s*\|\s*Work With Us\s*\|\s*Follow on X\s*\|\s*Archive\b", re.IGNORECASE),
    re.compile(r"\bSign up\s*\|\s*Work With Us\s*\|\s*Follow on X\s*\|\s*Archive\b", re.IGNORECASE),
)
FOLLOW_IMAGE_RE = re.compile(r"Follow image link:\s*(?:\([^)]*\)|\S*)\s*(?:Caption:\s*)?", re.IGNORECASE)
NEWSLETTER_LOW_VALUE_RE = re.compile(
    r"\b(?:"
    r"email provider is scrambling|"
    r"read it in full online|"
    r"unsubscribe|"
    r"manage preferences|"
    r"view in browser|"
    r"view online"
    r")\b",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


def database_path() -> Path:
    return get_settings().database_path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    ensure_runtime_dirs(settings)
    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_database() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA_SQL)
        _ensure_digest_run_metric_columns(connection)
        _ensure_digest_delivery_settings_table(connection)
        _ensure_podcast_metrics_table(connection)
        _ensure_inference_metric_status_values(connection)
        _ensure_default_profile(connection)


def _ensure_default_profile(connection: sqlite3.Connection) -> None:
    existing = connection.execute("SELECT id FROM profiles WHERE is_default = 1 LIMIT 1").fetchone()
    if existing:
        return

    now = utc_now()
    connection.execute(
        """
        INSERT INTO profiles (id, name, is_default, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?)
        """,
        (new_id(), "Adrian", now, now),
    )


def _ensure_digest_run_metric_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(digest_runs)").fetchall()
    }
    columns = {
        "inference_run_id": "TEXT",
        "newsletter_count": "INTEGER DEFAULT 0",
        "link_count": "INTEGER DEFAULT 0",
        "fetched_article_count": "INTEGER DEFAULT 0",
        "model_cache_hit_count": "INTEGER DEFAULT 0",
        "model_cache_miss_count": "INTEGER DEFAULT 0",
        "model_cache_write_count": "INTEGER DEFAULT 0",
        "duration_seconds": "REAL",
        "trigger": "TEXT DEFAULT 'manual'",
        "run_metadata": "TEXT NOT NULL DEFAULT '{}'",
    }
    for column, definition in columns.items():
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE digest_runs ADD COLUMN {column} {definition}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digest_runs_inference_run_id ON digest_runs(inference_run_id)"
    )


def _ensure_digest_delivery_settings_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS digest_delivery_settings (
          digest_id             TEXT PRIMARY KEY REFERENCES digests(id),
          recipient_email       TEXT,
          enabled               INTEGER NOT NULL DEFAULT 0,
          last_delivery_status  TEXT,
          last_delivered_at     TEXT,
          last_error            TEXT,
          updated_at            TEXT NOT NULL
        ) STRICT
        """
    )


def _ensure_podcast_metrics_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS podcast_metrics (
          id                    TEXT PRIMARY KEY,
          digest_id             TEXT NOT NULL REFERENCES digests(id),
          inference_run_id      TEXT,
          ts                    TEXT NOT NULL,
          show_name             TEXT,
          episode_id            TEXT,
          episode_title         TEXT,
          feed_url              TEXT,
          audio_url             TEXT,
          episode_url           TEXT,
          apple_podcasts_url    TEXT,
          published_at          TEXT,
          duration_seconds      INTEGER,
          quality_score         REAL,
          transcript_source     TEXT,
          status                TEXT NOT NULL,
          error_detail          TEXT,
          feed_fetch_ms         INTEGER,
          audio_download_ms     INTEGER,
          transcription_ms      INTEGER,
          total_ms              INTEGER,
          audio_bytes           INTEGER,
          transcript_words      INTEGER,
          cache_hit             INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_podcast_metrics_digest_ts ON podcast_metrics(digest_id, ts)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_podcast_metrics_inference_run_id ON podcast_metrics(inference_run_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_podcast_metrics_status ON podcast_metrics(status)"
    )


def _ensure_inference_metric_status_values(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'inference_metrics'"
    ).fetchone()
    table_sql = str(row["sql"] if row else "")
    if "model_capacity" in table_sql:
        return

    columns = [
        "id",
        "run_id",
        "article_id",
        "ts",
        "model",
        "model_tag",
        "quantization",
        "backend",
        "mode",
        "queue_wait_ms",
        "ttft_ms",
        "generation_ms",
        "total_ms",
        "prompt_tokens",
        "completion_tokens",
        "tokens_per_sec",
        "classification_label",
        "classification_confidence",
        "schema_valid",
        "summary_word_count",
        "fallback_triggered",
        "status",
        "error_detail",
    ]
    column_list = ", ".join(columns)
    status_values = ",\n    ".join(f"'{status}'" for status in INFERENCE_METRIC_STATUSES)
    connection.execute("ALTER TABLE inference_metrics RENAME TO inference_metrics_legacy")
    connection.execute(
        f"""
        CREATE TABLE inference_metrics (
          id                    TEXT PRIMARY KEY,
          run_id                TEXT NOT NULL,
          article_id            TEXT NOT NULL,
          ts                    TEXT NOT NULL,
          model                 TEXT NOT NULL,
          model_tag             TEXT,
          quantization          TEXT,
          backend               TEXT,
          mode                  TEXT NOT NULL,
          queue_wait_ms         INTEGER,
          ttft_ms               INTEGER,
          generation_ms         INTEGER,
          total_ms              INTEGER NOT NULL,
          prompt_tokens         INTEGER,
          completion_tokens     INTEGER,
          tokens_per_sec        REAL,
          classification_label  TEXT,
          classification_confidence REAL,
          schema_valid          INTEGER NOT NULL DEFAULT 0,
          summary_word_count    INTEGER,
          fallback_triggered    INTEGER NOT NULL DEFAULT 0,
          status                TEXT NOT NULL CHECK(status IN (
            {status_values}
          )),
          error_detail          TEXT
        ) STRICT
        """
    )
    connection.execute(
        f"""
        INSERT INTO inference_metrics ({column_list})
        SELECT {column_list}
        FROM inference_metrics_legacy
        """
    )
    connection.execute("DROP TABLE inference_metrics_legacy")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    if "sources" in result:
        result["sources"] = json.loads(result["sources"])
    return result


def _reddit_source_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    try:
        metadata = json.loads(record.get("metadata") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    record["metadata"] = metadata if isinstance(metadata, dict) else {}
    return record


def _normalize_subreddit_name(value: str) -> str:
    name = value.strip()
    if name.startswith("/r/"):
        name = name[3:]
    if name.startswith("r/"):
        name = name[2:]
    return re.sub(r"[^A-Za-z0-9_]", "", name)[:80]


def list_profiles() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute("SELECT * FROM profiles ORDER BY is_default DESC, name").fetchall()
    return [row_to_dict(row) for row in rows if row is not None]


def get_default_profile_id(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT id FROM profiles WHERE is_default = 1 LIMIT 1").fetchone()
    if row is None:
        _ensure_default_profile(connection)
        row = connection.execute("SELECT id FROM profiles WHERE is_default = 1 LIMIT 1").fetchone()
    return str(row["id"])


def list_digests(*, include_archived: bool = False) -> list[dict[str, Any]]:
    with connect() as connection:
        where_clause = "" if include_archived else "WHERE COALESCE(status, 'active') != 'archived'"
        rows = connection.execute(
            f"""
            SELECT * FROM digests
            {where_clause}
            ORDER BY json_array_length(sources) DESC, created_at DESC
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows if row is not None]


def get_digest(digest_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    return row_to_dict(row)


def create_digest(payload: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    digest_id = new_id()
    with connect() as connection:
        profile_id = payload.get("profile_id") or get_default_profile_id(connection)
        connection.execute(
            """
            INSERT INTO digests (
              id, profile_id, name, interest, schedule, sources, status,
              threshold, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                digest_id,
                profile_id,
                payload["name"],
                payload["interest"],
                payload.get("schedule", "daily"),
                json.dumps(payload.get("sources", [])),
                payload.get("status", "active"),
                payload.get("threshold", 0.45),
                now,
                now,
            ),
        )
    created = get_digest(digest_id)
    if created is None:
        raise RuntimeError("Digest was not created")
    return created


def update_digest(digest_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    existing = get_digest(digest_id)
    if existing is None:
        return None

    updated = {**existing, **{key: value for key, value in payload.items() if value is not None}}
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            UPDATE digests
            SET name = ?, interest = ?, schedule = ?, sources = ?, status = ?,
                threshold = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                updated["name"],
                updated["interest"],
                updated["schedule"],
                json.dumps(updated["sources"]),
                updated["status"],
                updated["threshold"],
                now,
                digest_id,
            ),
        )
    return get_digest(digest_id)


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


def seed_reddit_sources(digest_id: str, seed_sources: list[dict[str, Any]]) -> int:
    now = utc_now()
    affected = 0
    with connect() as connection:
        for source in seed_sources:
            subreddit = _normalize_subreddit_name(str(source.get("subreddit") or ""))
            if not subreddit:
                continue
            result = connection.execute(
                """
                INSERT INTO reddit_sources (
                  id, digest_id, subreddit, state, category, score, reason,
                  metadata, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(digest_id, subreddit) DO UPDATE SET
                  category = CASE
                    WHEN reddit_sources.category IS NULL
                      OR reddit_sources.category = ''
                      OR reddit_sources.category = 'Discovered'
                    THEN excluded.category
                    ELSE reddit_sources.category
                  END,
                  reason = CASE
                    WHEN reddit_sources.category IS NULL
                      OR reddit_sources.category = ''
                      OR reddit_sources.category = 'Discovered'
                    THEN excluded.reason
                    ELSE reddit_sources.reason
                  END,
                  metadata = CASE
                    WHEN reddit_sources.category IS NULL
                      OR reddit_sources.category = ''
                      OR reddit_sources.category = 'Discovered'
                    THEN excluded.metadata
                    ELSE reddit_sources.metadata
                  END,
                  updated_at = excluded.updated_at
                """,
                (
                    new_id(),
                    digest_id,
                    subreddit,
                    str(source.get("state") or "candidate"),
                    source.get("category"),
                    float(source.get("score") or 0),
                    source.get("reason"),
                    json.dumps({key: value for key, value in source.items() if key not in {"subreddit", "state", "category", "score", "reason"}}),
                    now,
                    now,
                ),
            )
            affected += int(result.rowcount > 0)
    return affected


def retire_reddit_sources_by_name(digest_id: str, subreddits: list[str], *, reason: str) -> int:
    names = [_normalize_subreddit_name(name) for name in subreddits]
    names = [name for name in names if name]
    if not names:
        return 0

    now = utc_now()
    updated = 0
    with connect() as connection:
        for name in names:
            result = connection.execute(
                """
                UPDATE reddit_sources
                SET state = 'retired',
                    reason = ?,
                    updated_at = ?
                WHERE digest_id = ?
                  AND lower(subreddit) = lower(?)
                """,
                (reason, now, digest_id, name),
            )
            updated += result.rowcount
    return updated


def list_reddit_sources(digest_id: str | None = None, *, include_retired: bool = False) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if digest_id:
        where.append("digest_id = ?")
        params.append(digest_id)
    if not include_retired:
        where.append("state != 'retired'")
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM reddit_sources
            {where_clause}
            ORDER BY
              CASE state
                WHEN 'active' THEN 1
                WHEN 'search_only' THEN 2
                WHEN 'candidate' THEN 3
                ELSE 4
              END,
              score DESC,
              subreddit COLLATE NOCASE
            """,
            params,
        ).fetchall()
    return [_reddit_source_row_to_dict(row) for row in rows]


def save_source_scout_review(
    *,
    digest_id: str,
    review: Any,
    status: str = "completed",
    error_detail: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    scout_run_id = new_id()
    with connect() as connection:
        for update in review.updates:
            connection.execute(
                """
                INSERT INTO reddit_sources (
                  id, digest_id, subreddit, state, category, score, reason,
                  last_reviewed_at, last_seen_post_at, consecutive_stale_runs,
                  metadata, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(digest_id, subreddit) DO UPDATE SET
                  state = excluded.state,
                  category = excluded.category,
                  score = excluded.score,
                  reason = excluded.reason,
                  last_reviewed_at = excluded.last_reviewed_at,
                  last_seen_post_at = COALESCE(excluded.last_seen_post_at, reddit_sources.last_seen_post_at),
                  consecutive_stale_runs = excluded.consecutive_stale_runs,
                  metadata = excluded.metadata,
                  updated_at = excluded.updated_at
                """,
                (
                    new_id(),
                    digest_id,
                    _normalize_subreddit_name(str(update.subreddit)),
                    update.state,
                    update.category,
                    float(update.score),
                    update.reason,
                    now,
                    update.last_seen_post_at,
                    int(update.consecutive_stale_runs),
                    json.dumps(update.metadata),
                    now,
                    now,
                ),
            )
        connection.execute(
            """
            INSERT INTO source_scout_runs (
              id, digest_id, run_at, status, sampled_count, active_count,
              candidate_count, retired_count, summary, error_detail
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scout_run_id,
                digest_id,
                now,
                status,
                int(review.sampled_count),
                int(review.active_count),
                int(review.candidate_count),
                int(review.retired_count),
                review.summary,
                error_detail,
            ),
        )
        for decision in review.decisions:
            connection.execute(
                """
                INSERT INTO source_scout_decisions (
                  id, scout_run_id, digest_id, agent, subreddit, decision,
                  action, confidence, reason, metadata, created_at
                )
                VALUES (?, ?, ?, 'source_scout', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    scout_run_id,
                    digest_id,
                    _normalize_subreddit_name(str(decision.subreddit)),
                    decision.decision,
                    decision.action,
                    float(decision.confidence),
                    decision.reason,
                    json.dumps(decision.metadata),
                    now,
                ),
            )
    run = get_source_scout_run(scout_run_id)
    if run is None:
        raise RuntimeError("Source Scout run was not created")
    return run


def get_source_scout_run(run_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM source_scout_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return dict(row) if row else None


def source_scout_summary(digest_id: str | None = None) -> dict[str, Any]:
    where = "WHERE digest_id = ?" if digest_id else ""
    params: tuple[Any, ...] = (digest_id,) if digest_id else ()
    with connect() as connection:
        source_rows = connection.execute(
            f"""
            SELECT state, COUNT(*) AS count
            FROM reddit_sources
            {where}
            GROUP BY state
            """,
            params,
        ).fetchall()
        latest = connection.execute(
            f"""
            SELECT *
            FROM source_scout_runs
            {where}
            ORDER BY run_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
    state_counts = {str(row["state"]): int(row["count"]) for row in source_rows}
    return {
        "source_count": sum(state_counts.values()),
        "active_count": state_counts.get("active", 0),
        "search_only_count": state_counts.get("search_only", 0),
        "candidate_count": state_counts.get("candidate", 0),
        "retired_count": state_counts.get("retired", 0),
        "latest_run": dict(latest) if latest else None,
    }


def list_source_scout_decisions(*, digest_id: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
    where = "WHERE digest_id = ?" if digest_id else ""
    params: list[Any] = [digest_id] if digest_id else []
    params.append(max(1, min(limit, 100)))
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT id, scout_run_id, digest_id, agent, subreddit, decision,
                   action, confidence, reason, metadata, created_at
            FROM source_scout_decisions
            {where}
            ORDER BY created_at DESC, confidence DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    decisions = []
    for row in rows:
        record = dict(row)
        try:
            metadata = json.loads(record.get("metadata") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        record["metadata"] = metadata if isinstance(metadata, dict) else {}
        decisions.append(record)
    return decisions


def create_placeholder_run(digest_id: str) -> dict[str, Any] | None:
    digest = get_digest(digest_id)
    if digest is None:
        return None

    now = utc_now()
    run_id = new_id()
    issue_id = new_id()
    title = f"{digest['name']} - Preview Issue"
    snapshot = "Pipeline scaffold is running. Gmail ingestion and article fetching are the next build slices."
    html = render_placeholder_issue(title, snapshot, generated_at=now)

    with connect() as connection:
        connection.execute(
            """
            INSERT INTO digest_runs (
              id, digest_id, run_at, lookback_days, item_count, failed_count,
              fallback_count, cold_start, partial, status, snapshot, completed_at
            )
            VALUES (?, ?, ?, 1, 0, 0, 0, 1, 0, 'completed', ?, ?)
            """,
            (run_id, digest_id, now, snapshot, now),
        )
        connection.execute(
            """
            INSERT INTO digest_issues (
              id, run_id, digest_id, title, snapshot, html_path, html_content, created_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (issue_id, run_id, digest_id, title, snapshot, html, now),
        )
    return get_run(run_id)


def create_ingested_run(
    *,
    digest: dict[str, Any],
    payloads: list[NormalizedPayload],
    article_results: list[ArticleFetchResult] | None = None,
    lookback_hours: int,
    configured_source_count: int,
    trigger: str = "manual",
    duration_seconds: float | None = None,
    model_cache_hit_count: int = 0,
    model_cache_miss_count: int = 0,
    model_cache_write_count: int = 0,
    inference_run_id: str | None = None,
    stage_seconds: dict[str, float] | None = None,
    stats_overrides: dict[str, Any] | None = None,
    agent_decisions: list[AgentDecision] | None = None,
) -> dict[str, Any]:
    article_results = article_results or []
    now = utc_now()
    run_id = new_id()
    issue_id = new_id()
    digest_id = str(digest["id"])
    title = f"{digest['name']} - Morning Dispatch Issue"
    snapshot = ingested_snapshot(payloads, configured_source_count, article_results)
    lookback_days = max(1, math.ceil(lookback_hours / 24))
    body_payloads = [payload for payload in payloads if payload.source_type == "gmail"]
    link_payload_count = sum(1 for payload in payloads if payload.source_type == "gmail_link")
    podcast_payload_count = sum(1 for payload in payloads if payload.source_type == "podcast_episode")
    item_count = len(body_payloads) + len(article_results)
    failed_count = sum(1 for result in article_results if not result.fetched)
    fallback_count = sum(1 for result in article_results if result.content_type == "fallback_snippet")
    fetched_article_count = sum(1 for result in article_results if result.fetched)
    digest_stats = _build_digest_stats(
        configured_source_count=configured_source_count,
        newsletter_count=len(body_payloads),
        link_count=link_payload_count,
        podcast_episode_count=podcast_payload_count,
        article_results=article_results,
        duration_seconds=duration_seconds,
        inference_run_id=inference_run_id,
        stage_seconds=stage_seconds,
    )
    if isinstance(stats_overrides, dict):
        for key in (
            "source_count",
            "newsletter_count",
            "link_count",
            "podcast_episode_count",
            "processing_seconds",
            "stage_seconds",
        ):
            if key in stats_overrides:
                digest_stats[key] = stats_overrides[key]
    run_metadata = {"digest_stats": digest_stats}
    html = render_ingested_issue(
        title,
        snapshot,
        payloads,
        article_results,
        lookback_hours,
        generated_at=now,
        issue_id=issue_id,
        digest_stats=digest_stats,
    )

    with connect() as connection:
        connection.execute(
            """
            INSERT INTO digest_runs (
              id, digest_id, inference_run_id, run_at, lookback_days, item_count, failed_count,
              fallback_count, newsletter_count, link_count, fetched_article_count,
              model_cache_hit_count, model_cache_miss_count, model_cache_write_count,
              duration_seconds, trigger, cold_start, partial, status, snapshot, run_metadata, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'completed', ?, ?, ?)
            """,
            (
                run_id,
                digest_id,
                inference_run_id,
                now,
                lookback_days,
                item_count,
                failed_count,
                fallback_count,
                len(body_payloads),
                link_payload_count,
                fetched_article_count,
                model_cache_hit_count,
                model_cache_miss_count,
                model_cache_write_count,
                duration_seconds,
                trigger,
                int(failed_count > 0),
                snapshot,
                json.dumps(run_metadata),
                now,
            ),
        )

        for payload in body_payloads:
            article_id = _upsert_article(connection, payload, now)
            discovery_id = _insert_discovery(connection, article_id, payload, now)
            connection.execute(
                """
                INSERT INTO digest_items (
                  id, run_id, digest_id, article_id, discovery_id, relevance_score,
                  tier, section, editor_summary, editor_note, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    run_id,
                    digest_id,
                    article_id,
                    discovery_id,
                    None,
                    "source",
                    _section_for_payload(payload),
                    _summary_for_payload(payload),
                    _editor_note_for_payload(payload),
                    now,
                ),
            )

        for result in article_results:
            article_id = _upsert_article_result(connection, result, now)
            discovery_id = _insert_discovery_for_result(connection, article_id, result, now)
            connection.execute(
                """
                INSERT INTO digest_items (
                  id, run_id, digest_id, article_id, discovery_id, relevance_score,
                  tier, section, editor_summary, editor_note, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    run_id,
                    digest_id,
                    article_id,
                    discovery_id,
                    result.relevance_score,
                    result.tier,
                    result.section,
                    result.editor_summary or result.excerpt or result.title,
                    _editor_note_for_result(result),
                    now,
                ),
            )

        connection.execute(
            """
            INSERT INTO digest_issues (
              id, run_id, digest_id, title, snapshot, html_path, html_content, created_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (issue_id, run_id, digest_id, title, snapshot, html, now),
        )
        _insert_agent_decisions(
            connection,
            run_id=run_id,
            digest_id=digest_id,
            inference_run_id=inference_run_id,
            decisions=agent_decisions or [],
            now=now,
        )
    run = get_run(run_id)
    if run is None:
        raise RuntimeError("Digest run was not created")
    return run


def ingested_snapshot(
    payloads: list[NormalizedPayload],
    configured_source_count: int,
    article_results: list[ArticleFetchResult] | None = None,
) -> str:
    article_results = article_results or []
    body_count = sum(1 for payload in payloads if payload.source_type == "gmail")
    link_count = sum(1 for payload in payloads if payload.source_type == "gmail_link")
    podcast_count = sum(1 for payload in payloads if payload.source_type == "podcast_episode")
    fetched_article_count = sum(1 for result in article_results if result.fetched)
    attempted_count = len(article_results)
    model_capacity_count = sum(
        1 for result in article_results if result.enrichment_source == "model_capacity_fallback"
    )
    if configured_source_count == 0:
        return "No sources are configured for this digest."
    if not payloads:
        return f"No matching items found across {configured_source_count} configured source(s)."
    if attempted_count:
        snapshot = build_issue_snapshot(body_count, configured_source_count, article_results)
        if model_capacity_count:
            snapshot += (
                f" Model capacity limited AI enrichment for {model_capacity_count} article(s); "
                "deterministic summaries were used for those items."
            )
        return snapshot
    return (
        f"Fetched {body_count} newsletter body/bodies, {link_count} linked item(s), "
        f"and {podcast_count} podcast episode(s) from {configured_source_count} configured source(s)."
    )


def apply_cached_model_enrichments(
    article_results: list[ArticleFetchResult],
    *,
    model_name: str | None,
    limit: int,
) -> list[ArticleFetchResult]:
    if not model_name or limit <= 0 or not article_results:
        return article_results

    enriched: list[ArticleFetchResult] = []
    with connect() as connection:
        for index, result in enumerate(article_results):
            if index >= limit or not result.fetched or result.tier == "dropped":
                enriched.append(result)
                continue
            cache_identity = _model_cache_identity(result, model_name)
            if cache_identity is None:
                enriched.append(result)
                continue
            cache_key, _canonical_url, _source_text_hash = cache_identity
            row = connection.execute(
                """
                SELECT title, summary, keywords, content_type
                FROM model_enrichment_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
            enriched.append(_apply_model_cache_row(result, row) if row is not None else result)
    return enriched


def cache_model_enrichments(article_results: list[ArticleFetchResult], *, model_name: str | None) -> int:
    if not model_name or not article_results:
        return 0

    cached_count = 0
    now = utc_now()
    with connect() as connection:
        for result in article_results:
            if result.enrichment_source != "model" or not result.fetched:
                continue
            summary = result.editor_summary or result.excerpt
            if not summary:
                continue
            cache_identity = _model_cache_identity(result, model_name)
            if cache_identity is None:
                continue
            cache_key, canonical_url, source_text_hash = cache_identity
            connection.execute(
                """
                INSERT INTO model_enrichment_cache (
                  id, cache_key, canonical_url, source_text_hash, model_name,
                  title, summary, keywords, content_type, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  title = excluded.title,
                  summary = excluded.summary,
                  keywords = excluded.keywords,
                  content_type = excluded.content_type,
                  updated_at = excluded.updated_at
                """,
                (
                    new_id(),
                    cache_key,
                    canonical_url,
                    source_text_hash,
                    model_name,
                    result.title,
                    summary,
                    json.dumps(list(result.keywords)),
                    result.content_type,
                    now,
                    now,
                ),
            )
            cached_count += 1
    return cached_count


def _build_digest_stats(
    *,
    configured_source_count: int,
    newsletter_count: int,
    link_count: int,
    podcast_episode_count: int = 0,
    article_results: list[ArticleFetchResult],
    duration_seconds: float | None,
    inference_run_id: str | None,
    stage_seconds: dict[str, float] | None,
) -> dict[str, Any]:
    active_results = [result for result in article_results if result.tier != "dropped"]
    included_count = sum(1 for result in active_results if result.fetched)
    unresolved_count = sum(1 for result in active_results if not result.fetched)
    dropped_count = sum(1 for result in article_results if result.tier == "dropped")
    token_summary = inference_token_summary(inference_run_id) if inference_run_id else _empty_token_summary()
    return {
        "source_count": max(0, int(configured_source_count or 0)),
        "newsletter_count": max(0, int(newsletter_count or 0)),
        "link_count": max(0, int(link_count or 0)),
        "podcast_episode_count": max(0, int(podcast_episode_count or 0)),
        "article_candidate_count": len(article_results),
        "included_article_count": included_count,
        "unresolved_count": unresolved_count,
        "dropped_count": dropped_count,
        "prompt_tokens": token_summary["prompt_tokens"],
        "completion_tokens": token_summary["completion_tokens"],
        "total_tokens": token_summary["total_tokens"],
        "model_call_count": token_summary["model_call_count"],
        "processing_seconds": _nullable_float(duration_seconds),
        "stage_seconds": _normalize_stage_seconds(stage_seconds),
    }


def _empty_token_summary() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "model_call_count": 0,
    }


def _normalize_stage_seconds(stage_seconds: dict[str, float] | None) -> dict[str, float]:
    if not isinstance(stage_seconds, dict):
        return {}
    normalized: dict[str, float] = {}
    for key, value in stage_seconds.items():
        try:
            normalized[str(key)] = round(max(0.0, float(value)), 3)
        except (TypeError, ValueError):
            continue
    return normalized


def inference_token_summary(inference_run_id: str | None) -> dict[str, int]:
    if not inference_run_id:
        return _empty_token_summary()
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
              COUNT(*) AS model_call_count,
              COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
              COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM inference_metrics
            WHERE run_id = ?
            """,
            (inference_run_id,),
        ).fetchone()
    prompt_tokens = int(row["prompt_tokens"] or 0) if row else 0
    completion_tokens = int(row["completion_tokens"] or 0) if row else 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "model_call_count": int(row["model_call_count"] or 0) if row else 0,
    }


def render_ingested_issue(
    title: str,
    snapshot: str,
    payloads: list[NormalizedPayload],
    article_results: list[ArticleFetchResult] | None,
    lookback_hours: int,
    generated_at: str | None = None,
    issue_id: str | None = None,
    digest_stats: dict[str, Any] | None = None,
) -> str:
    article_results = article_results or []
    body_payloads = [payload for payload in payloads if payload.source_type == "gmail"]
    fetched_articles = [result for result in article_results if result.fetched and result.tier != "dropped"]
    lead_article = next((result for result in fetched_articles if result.tier == "lead"), None)
    main_articles = [result for result in fetched_articles if result is not lead_article and result.tier == "main"]
    lower_confidence_articles = [result for result in fetched_articles if result.tier == "lower_confidence"]
    hidden_article_count = max(0, len(fetched_articles) - len(main_articles) - len(lower_confidence_articles) - (1 if lead_article else 0))

    newsletter_items = [_render_newsletter_item(payload) for payload in body_payloads[:8]]
    newsletter_html = "\n".join(item for item in newsletter_items if item)
    lead_html = _render_article_card(lead_article, variant="lead", issue_id=issue_id) if lead_article else ""
    section_html = _render_article_sections(main_articles, issue_id=issue_id)
    lower_html = "\n".join(
        _render_article_card(result, variant="compact", issue_id=issue_id)
        for result in lower_confidence_articles[:8]
    )
    stats_html = _render_digest_stats(
        digest_stats
        or _build_digest_stats(
            configured_source_count=0,
            newsletter_count=len(body_payloads),
            link_count=sum(1 for payload in payloads if payload.source_type == "gmail_link"),
            podcast_episode_count=sum(1 for payload in payloads if payload.source_type == "podcast_episode"),
            article_results=article_results,
            duration_seconds=None,
            inference_run_id=None,
            stage_seconds=None,
        )
    )
    empty_state = ""
    if not payloads:
        empty_state = """
        <section class="empty">
          <strong>No newsletter items were found.</strong>
          Check the source allowlist, Gmail labels, or the digest lookback window.
        </section>
        """
    hidden_html_parts = []
    if hidden_article_count:
        hidden_html_parts.append(f"{hidden_article_count} additional fetched article(s)")
    hidden_html = ""
    if hidden_html_parts:
        hidden_html = f'<p class="more-count">Plus {" and ".join(hidden_html_parts)}.</p>'
    lower_section = ""
    if lower_html:
        lower_section = f"""
        <section class="section lower-confidence">
          <h2>Lower Confidence</h2>
          <div class="article-list">{lower_html}</div>
        </section>
        """
    generated_footer = _render_generated_footer(generated_at or utc_now())
    feedback_script = _render_feedback_script(issue_id)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)}</title>
  <style>
    :root {{ color: #171717; background: #f7f3eb; }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
    body {{ margin: 0; font-family: Georgia, 'Times New Roman', serif; color: #171717; background: #f7f3eb; }}
    main {{ width: min(1120px, 100%); margin: 0 auto; padding: 44px 24px 64px; }}
    header {{ border-bottom: 3px solid #171717; padding-bottom: 18px; margin-bottom: 28px; }}
    h1 {{ font-size: clamp(2.4rem, 7vw, 5.4rem); line-height: .9; margin: 0; letter-spacing: 0; }}
    h2 {{ font: 800 0.9rem Arial, sans-serif; letter-spacing: .08em; text-transform: uppercase; margin: 0 0 16px; }}
    h3 {{ font-size: 1.35rem; line-height: 1.15; margin: 0 0 8px; }}
    h1, h2, h3, p, a, .meta {{ overflow-wrap: anywhere; }}
    a {{ color: #173f63; text-decoration-thickness: 1px; text-underline-offset: 3px; }}
    img, video, iframe, table {{ max-width: 100%; }}
    .date {{ margin-top: 12px; font: 700 0.8rem Arial, sans-serif; text-transform: uppercase; }}
    .snapshot {{ font-size: 1.28rem; line-height: 1.45; max-width: 820px; margin-bottom: 28px; }}
    .meta {{ font: 700 0.74rem Arial, sans-serif; color: #5f675f; text-transform: uppercase; overflow-wrap: anywhere; }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr); gap: 32px; align-items: start; }}
    .section {{ border-top: 1px solid #171717; padding-top: 18px; }}
    .section + .section {{ margin-top: 32px; }}
    .grid, .section, .article-card, .newsletter, .link-item {{ min-width: 0; }}
    .article-card {{ padding: 0 0 22px; margin-bottom: 22px; border-bottom: 1px solid #d4cbbd; }}
    .article-card p {{ font-size: 1rem; line-height: 1.55; margin: 10px 0 0; }}
    .article-card a {{ color: inherit; }}
    .article-card.lead {{ padding-bottom: 28px; margin-bottom: 28px; border-bottom: 3px solid #171717; }}
    .article-card.lead h3 {{ font-size: clamp(2rem, 5vw, 3.8rem); line-height: .95; max-width: 850px; }}
    .article-card.lead p {{ font-size: 1.15rem; line-height: 1.55; max-width: 850px; }}
    .article-section {{ margin-bottom: 26px; }}
    .article-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; }}
    .article-list .article-card:last-child, .article-grid .article-card:last-child {{ margin-bottom: 0; }}
    .score {{ display: inline-block; margin-left: 8px; color: #7a4f16; }}
    .keywords {{ margin-top: 10px; font: 700 .72rem Arial, sans-serif; color: #6a746e; text-transform: uppercase; }}
    .feedback-controls {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 12px; font-family: Arial, sans-serif; }}
    .feedback-controls button {{ border: 1px solid #d4cbbd; border-radius: 8px; background: #fffaf0; color: #173f63; padding: 6px 10px; font: 800 .72rem Arial, sans-serif; cursor: pointer; }}
    .feedback-controls button:hover {{ background: #efe7d8; }}
    .feedback-controls[data-feedback='sent'] button {{ opacity: .55; }}
    .feedback-state {{ color: #5f675f; font: 700 .72rem Arial, sans-serif; text-transform: uppercase; }}
    .digest-stats {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 24px; }}
    .digest-stat {{ border-top: 1px solid #d4cbbd; padding-top: 10px; min-width: 0; }}
    .digest-stat span {{ display: block; font: 800 .68rem Arial, sans-serif; color: #6a746e; text-transform: uppercase; }}
    .digest-stat strong {{ display: block; margin-top: 4px; font: 900 1.25rem Arial, sans-serif; color: #171717; overflow-wrap: anywhere; }}
    .stage-list {{ margin-top: 4px; padding-left: 18px; color: #5f675f; font: 700 .74rem Arial, sans-serif; line-height: 1.6; }}
    .newsletter {{ padding: 0 0 20px; margin-bottom: 20px; border-bottom: 1px solid #d4cbbd; }}
    .newsletter p {{ font-size: 1rem; line-height: 1.55; margin: 10px 0 0; }}
    .link-item {{ display: grid; gap: 5px; padding: 12px 0; border-bottom: 1px solid #d4cbbd; }}
    details.source-notes {{ margin-top: 28px; border-top: 1px solid #171717; padding-top: 16px; }}
    details.source-notes summary {{ cursor: pointer; font: 800 .9rem Arial, sans-serif; text-transform: uppercase; }}
    .empty {{ margin-top: 32px; padding: 24px; border: 1px dashed #b9ae9d; font: 1rem Arial, sans-serif; background: #fffaf0; }}
    .more-count {{ font: 700 .9rem Arial, sans-serif; color: #5f675f; margin-top: 16px; }}
    .issue-footer {{ margin-top: 36px; padding-top: 16px; border-top: 1px solid #d4cbbd; font: 700 .76rem Arial, sans-serif; color: #5f675f; text-transform: uppercase; }}
    @media (max-width: 820px) {{ .grid, .article-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{escape(title)}</h1>
      <div class="date">Morning Dispatch · Last {lookback_hours} hours</div>
    </header>
    <p class="snapshot">{escape(snapshot)}</p>
    {empty_state}
    <div class="grid">
      <section class="section">
        <h2>Fetched Articles</h2>
        {lead_html}
        {section_html or '<p class="meta">No article pages were fetched yet.</p>'}
        {lower_section}
        {hidden_html}
      </section>
      <section class="section">
        <h2>Digest Stats</h2>
        {stats_html}
        <details class="source-notes" open>
          <summary>Newsletter Briefs</summary>
          {newsletter_html or '<p class="meta">No newsletter bodies were available.</p>'}
        </details>
      </section>
    </div>
    {generated_footer}
  </main>
  {feedback_script}
</body>
</html>"""


def render_placeholder_issue(title: str, snapshot: str, generated_at: str | None = None) -> str:
    generated_footer = _render_generated_footer(generated_at or utc_now())
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
    body {{ margin: 0; font-family: Georgia, 'Times New Roman', serif; color: #171717; background: #f7f3eb; }}
    main {{ width: min(900px, 100%); margin: 0 auto; padding: 48px 24px; }}
    header {{ border-bottom: 2px solid #171717; padding-bottom: 18px; margin-bottom: 28px; }}
    h1 {{ font-size: clamp(2.5rem, 8vw, 5rem); line-height: .9; margin: 0; letter-spacing: 0; }}
    h1, p {{ overflow-wrap: anywhere; }}
    .date {{ margin-top: 12px; font: 600 0.8rem Arial, sans-serif; text-transform: uppercase; }}
    .snapshot {{ font-size: 1.3rem; line-height: 1.5; max-width: 720px; }}
    .empty {{ margin-top: 32px; padding-top: 24px; border-top: 1px solid #c8bfae; font: 1rem Arial, sans-serif; }}
    .issue-footer {{ margin-top: 36px; padding-top: 16px; border-top: 1px solid #d4cbbd; font: 700 .76rem Arial, sans-serif; color: #5f675f; text-transform: uppercase; }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{title}</h1>
      <div class="date">Local preview issue</div>
    </header>
    <p class="snapshot">{snapshot}</p>
    <section class="empty">
      <strong>No article items yet.</strong>
      The next slice will connect approved Gmail newsletters, filter links, and fetch primary articles.
    </section>
    {generated_footer}
  </main>
</body>
</html>"""


def _upsert_article(connection: sqlite3.Connection, payload: NormalizedPayload, now: str) -> str:
    canonical_url = payload.original_url if payload.original_url else None
    if canonical_url:
        existing = connection.execute(
            "SELECT id FROM articles WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()
        if existing:
            return str(existing["id"])

    article_id = new_id()
    connection.execute(
        """
        INSERT INTO articles (
          id, canonical_url, original_url, domain, publisher, author, published_at,
          title, cleaned_text, summary, keywords, content_type, embedding,
          fetch_status, quality_flag, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, NULL, 'fetched', 'ok', ?, ?)
        """,
        (
            article_id,
            canonical_url,
            payload.original_url,
            _domain(payload.original_url),
            payload.source_name,
            payload.published_at,
            _title_for_payload(payload),
            payload.raw_text,
            _summary_for_payload(payload),
            payload.source_type,
            now,
            now,
        ),
    )
    return article_id


def _upsert_article_result(connection: sqlite3.Connection, result: ArticleFetchResult, now: str) -> str:
    canonical_url = result.canonical_url or result.final_url or result.original_url
    existing = connection.execute(
        "SELECT id FROM articles WHERE canonical_url = ?",
        (canonical_url,),
    ).fetchone()
    if existing:
        article_id = str(existing["id"])
        connection.execute(
            """
            UPDATE articles
            SET original_url = ?, domain = ?, publisher = ?, published_at = ?,
                title = ?, cleaned_text = ?, summary = ?, content_type = ?,
                keywords = ?, fetch_status = ?, quality_flag = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                result.original_url,
                result.domain,
                result.payload.source_name,
                result.payload.published_at,
                result.title,
                result.text,
                result.editor_summary or result.excerpt,
                result.content_type,
                json.dumps(list(result.keywords)),
                result.status,
                _quality_flag_for_result(result),
                now,
                article_id,
            ),
        )
        return article_id

    article_id = new_id()
    connection.execute(
        """
        INSERT INTO articles (
          id, canonical_url, original_url, domain, publisher, author, published_at,
          title, cleaned_text, summary, keywords, content_type, embedding,
          fetch_status, quality_flag, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            article_id,
            canonical_url,
            result.original_url,
            result.domain,
            result.payload.source_name,
            result.payload.published_at,
            result.title,
            result.text,
            result.editor_summary or result.excerpt,
            json.dumps(list(result.keywords)),
            result.content_type,
            result.status,
            _quality_flag_for_result(result),
            now,
            now,
        ),
    )
    return article_id


def _quality_flag_for_result(result: ArticleFetchResult) -> str:
    if result.fetched:
        return "ok"
    reason = _truncate_text(str(result.error or result.status or "needs review"), 180)
    if reason.lower().startswith(str(result.status).lower()):
        return reason
    return _truncate_text(f"{result.status}: {reason}", 180)


def _model_cache_identity(result: ArticleFetchResult, model_name: str) -> tuple[str, str, str] | None:
    canonical_url = result.canonical_url or result.final_url or result.original_url
    source_text = " ".join((result.text or "").split())
    if not canonical_url or not source_text:
        return None
    source_text_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    raw_key = "\n".join((MODEL_ENRICHMENT_CACHE_VERSION, model_name, canonical_url, source_text_hash))
    cache_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return cache_key, canonical_url, source_text_hash


def _apply_model_cache_row(result: ArticleFetchResult, row: sqlite3.Row) -> ArticleFetchResult:
    return replace(
        result,
        title=str(row["title"] or result.title),
        excerpt=str(row["summary"] or result.excerpt),
        editor_summary=str(row["summary"] or result.editor_summary),
        keywords=tuple(_decode_keywords(row["keywords"])),
        content_type=str(row["content_type"] or result.content_type),
        enrichment_source="model_cache",
    )


def _decode_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _article_row_to_fetch_result(row: sqlite3.Row) -> ArticleFetchResult:
    canonical_url = str(row["canonical_url"] or row["original_url"] or "")
    payload = NormalizedPayload(
        source_type="stored_article",
        source_name=str(row["publisher"] or row["domain"] or "stored article"),
        original_url=canonical_url,
        published_at=row["published_at"],
        metadata={"article_id": str(row["id"])},
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=str(row["original_url"] or canonical_url),
        final_url=canonical_url,
        canonical_url=canonical_url,
        title=str(row["title"] or canonical_url or "Stored article"),
        text=str(row["cleaned_text"] or ""),
        excerpt=str(row["summary"] or ""),
        domain=row["domain"],
        status="fetched",
        keywords=tuple(_decode_keywords(row["keywords"])),
        content_type=str(row["content_type"] or "article"),
    )


def _digest_item_row_to_fetch_result(row: sqlite3.Row) -> ArticleFetchResult:
    canonical_url = str(row["canonical_url"] or row["original_url"] or "")
    source_type = str(row["discovery_source_type"] or "stored_article")
    thread_id = row["thread_id"]
    status = str(row["fetch_status"] or "fetched")
    metadata = {
        "article_id": str(row["article_id"]),
        "gmail_message_id": row["message_id"],
        "reddit_thread_id": thread_id if source_type == "reddit_thread" else None,
        "podcast_episode_id": thread_id if source_type == "podcast_episode" else None,
        "sender_email": row["sender_email"],
        "link_text": row["link_text"],
    }
    payload = NormalizedPayload(
        source_type=source_type,
        source_name=str(row["sender_email"] or row["publisher"] or row["discovery_source_name"] or "stored article"),
        original_url=canonical_url,
        published_at=row["published_at"],
        metadata=metadata,
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=str(row["original_url"] or canonical_url),
        final_url=canonical_url,
        canonical_url=canonical_url,
        title=str(row["title"] or canonical_url or "Stored article"),
        text=str(row["cleaned_text"] or ""),
        excerpt=str(row["summary"] or ""),
        domain=row["domain"],
        status=status,
        error=None if status == "fetched" else row["quality_flag"],
        keywords=tuple(_decode_keywords(row["keywords"])),
        content_type=str(row["content_type"] or "article"),
        relevance_score=_nullable_float(row["relevance_score"]),
        tier=str(row["tier"] or "main"),
        section=str(row["section"] or "Fetched Articles"),
        editor_summary=str(row["editor_summary"] or row["summary"] or ""),
        enrichment_source="stored",
    )


def _nullable_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _nullable_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nullable_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_service_ms(row: dict[str, Any]) -> int:
    total_ms = int(row.get("total_ms") or 0)
    queue_wait_ms = int(row.get("queue_wait_ms") or 0)
    return max(0, total_ms - queue_wait_ms)


def _average(values: list[int] | list[float]) -> float | None:
    if not values:
        return None
    return round(float(sum(values)) / len(values), 2)


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    index = round((percentile / 100) * (len(values) - 1))
    return values[min(max(index, 0), len(values) - 1)]


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _insert_discovery(
    connection: sqlite3.Connection,
    article_id: str,
    payload: NormalizedPayload,
    now: str,
) -> str:
    metadata = payload.metadata or {}
    discovery_id = new_id()
    connection.execute(
        """
        INSERT INTO article_discoveries (
          id, article_id, discovery_source_type, discovery_source_name, sender_email,
          message_id, thread_id, issue_date, link_text, newsletter_snippet, discovered_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            discovery_id,
            article_id,
            payload.source_type,
            payload.source_name,
            metadata.get("sender_email") or metadata.get("sender"),
            metadata.get("gmail_message_id"),
            metadata.get("reddit_thread_id") or metadata.get("podcast_episode_id") or metadata.get("thread_id"),
            payload.published_at,
            _title_for_payload(payload),
            _summary_for_payload(payload),
            now,
        ),
    )
    return discovery_id


def _insert_agent_decisions(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    digest_id: str,
    inference_run_id: str | None,
    decisions: list[AgentDecision],
    now: str,
) -> None:
    for decision in decisions:
        connection.execute(
            """
            INSERT INTO agent_decisions (
              id, run_id, digest_id, inference_run_id, agent, target, decision,
              action, confidence, reason, model_name, metadata, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                run_id,
                digest_id,
                inference_run_id,
                decision.agent,
                decision.target,
                decision.decision,
                decision.action,
                decision.confidence,
                decision.reason,
                decision.model_name,
                json.dumps(decision.metadata),
                now,
            ),
        )


def _insert_discovery_for_result(
    connection: sqlite3.Connection,
    article_id: str,
    result: ArticleFetchResult,
    now: str,
) -> str:
    metadata = result.payload.metadata or {}
    discovery_id = new_id()
    connection.execute(
        """
        INSERT INTO article_discoveries (
          id, article_id, discovery_source_type, discovery_source_name, sender_email,
          message_id, thread_id, issue_date, link_text, newsletter_snippet, discovered_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            discovery_id,
            article_id,
            result.payload.source_type,
            result.payload.source_name,
            metadata.get("sender_email") or metadata.get("sender"),
            metadata.get("gmail_message_id"),
            metadata.get("reddit_thread_id") or metadata.get("podcast_episode_id") or metadata.get("thread_id"),
            result.payload.published_at,
            metadata.get("link_text") or result.title,
            _discovery_snippet_for_result(result),
            now,
        ),
    )
    return discovery_id


def _discovery_snippet_for_result(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    context = " ".join(
        str(value)
        for value in (
            metadata.get("parent_subject"),
            metadata.get("subject"),
            result.payload.raw_text,
            result.excerpt if not result.fetched else "",
        )
        if value
    )
    return _truncate_text(_clean_newsletter_text(context), 700)


def _render_newsletter_item(payload: NormalizedPayload) -> str:
    subject = _title_for_payload(payload)
    sender = payload.source_name or "Gmail"
    snippet = _summary_for_payload(payload, max_chars=700)
    if _weak_newsletter_snippet(snippet):
        return ""
    published = _format_issue_date(payload.published_at)
    return f"""
      <article class="newsletter">
        <div class="meta">{escape(sender)} · {escape(published)}</div>
        <h3>{escape(subject)}</h3>
        <p>{escape(snippet)}</p>
      </article>
    """


def _render_article_sections(results: list[ArticleFetchResult], *, issue_id: str | None = None) -> str:
    grouped: dict[str, list[ArticleFetchResult]] = {}
    for result in results:
        grouped.setdefault(result.section or "Noteworthy", []).append(result)

    sections: list[str] = []
    for section, section_results in grouped.items():
        cards = "\n".join(
            _render_article_card(result, variant="compact", issue_id=issue_id)
            for result in section_results[:6]
        )
        sections.append(
            f"""
            <section class="article-section">
              <h2>{escape(section)}</h2>
              <div class="article-grid">{cards}</div>
            </section>
            """
        )
    return "\n".join(sections)


def _render_article_card(
    result: ArticleFetchResult | None,
    *,
    variant: str = "compact",
    issue_id: str | None = None,
) -> str:
    if result is None:
        return ""
    url = result.final_url or result.original_url
    domain = result.domain or _domain(url) or "article"
    source = result.payload.source_name or "Gmail"
    published = _format_article_date(result.payload.published_at)
    meta_parts = [domain]
    if published:
        meta_parts.append(published)
    meta_parts.append(f"via {source}")
    meta = " · ".join(escape(part) for part in meta_parts)
    score = f'<span class="score">{int((result.relevance_score or 0) * 100)}%</span>' if result.relevance_score else ""
    keywords = ", ".join(result.keywords[:5])
    keyword_html = f'<div class="keywords">{escape(keywords)}</div>' if keywords else ""
    card_class = "article-card lead" if variant == "lead" else "article-card"
    summary = _clean_newsletter_text(result.editor_summary or result.excerpt)
    title = _clean_newsletter_text(result.title) or result.title
    feedback_html = _render_feedback_controls(issue_id, url) if result.fetched else ""
    return f"""
      <article class="{card_class}">
        <div class="meta">{meta}{score}</div>
        <h3><a href="{escape(url, quote=True)}" target="_blank" rel="noreferrer">{escape(title)}</a></h3>
        <p>{escape(summary)}</p>
        {keyword_html}
        {feedback_html}
      </article>
    """


def _render_feedback_controls(issue_id: str | None, url: str | None) -> str:
    if not issue_id or not url:
        return ""
    return f"""
        <div class="feedback-controls" data-feedback-url="{escape(url, quote=True)}">
          <button type="button" data-feedback-signal="up">Useful</button>
          <button type="button" data-feedback-signal="down">Not useful</button>
          <span class="feedback-state" aria-live="polite"></span>
        </div>
    """


def _render_feedback_script(issue_id: str | None) -> str:
    if not issue_id:
        return ""
    return f"""
  <script>
    (() => {{
      const issueId = {json.dumps(issue_id)};
      document.addEventListener("click", async (event) => {{
        const button = event.target.closest("[data-feedback-signal]");
        if (!button) return;
        const controls = button.closest(".feedback-controls");
        const state = controls ? controls.querySelector(".feedback-state") : null;
        if (!controls) return;
        const url = controls.getAttribute("data-feedback-url");
        const signal = button.getAttribute("data-feedback-signal");
        if (!url || !signal) return;
        controls.querySelectorAll("button").forEach((item) => item.disabled = true);
        if (state) state.textContent = "Saving";
        try {{
          const response = await fetch("/api/feedback", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ issue_id: issueId, url, signal }}),
          }});
          if (!response.ok) throw new Error("Feedback failed");
          controls.setAttribute("data-feedback", "sent");
          if (state) state.textContent = "Saved";
        }} catch (_error) {{
          controls.querySelectorAll("button").forEach((item) => item.disabled = false);
          if (state) state.textContent = "Try again";
        }}
      }});
    }})();
  </script>
    """


def _render_digest_stats(stats: dict[str, Any]) -> str:
    stage_seconds = stats.get("stage_seconds") if isinstance(stats.get("stage_seconds"), dict) else {}
    stage_html = ""
    if stage_seconds:
        stage_labels = {
            "ingestion": "Ingestion",
            "fetching": "Article fetching",
            "classification": "AI classification",
            "editorial": "Editor review",
            "publishing": "Publishing",
        }
        stage_items = "\n".join(
            f"<li>{escape(stage_labels.get(str(key), str(key).replace('_', ' ').title()))}: "
            f"{escape(_format_duration(value))}</li>"
            for key, value in stage_seconds.items()
        )
        stage_html = f'<div class="digest-stat"><span>Stage timing</span><ul class="stage-list">{stage_items}</ul></div>'

    stat_items = [
        ("Sources", _format_int(stats.get("source_count"))),
        ("Newsletters", _format_int(stats.get("newsletter_count"))),
        ("Links extracted", _format_int(stats.get("link_count"))),
        ("Podcast episodes", _format_int(stats.get("podcast_episode_count"))),
        ("Articles included", _format_int(stats.get("included_article_count"))),
        ("Items filtered", _format_int(int(stats.get("dropped_count") or 0) + int(stats.get("unresolved_count") or 0))),
        ("Model tokens", _format_int(stats.get("total_tokens"))),
        ("Model calls", _format_int(stats.get("model_call_count"))),
        ("Processing time", _format_duration(stats.get("processing_seconds"))),
    ]
    stat_html = "\n".join(
        f'<div class="digest-stat"><span>{escape(label)}</span><strong>{escape(value)}</strong></div>'
        for label, value in stat_items
    )
    token_detail = ""
    if int(stats.get("total_tokens") or 0):
        token_detail = (
            f"<p class=\"meta\">Token detail: {_format_int(stats.get('prompt_tokens'))} prompt + "
            f"{_format_int(stats.get('completion_tokens'))} completion.</p>"
        )
    return f"""
      <div class="digest-stats">
        {stat_html}
        {stage_html}
      </div>
      {token_detail}
    """


def _format_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def _format_duration(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if seconds < 1:
        return f"{round(seconds * 1000):,} ms"
    minutes, remaining = divmod(round(seconds), 60)
    if minutes:
        return f"{minutes}m {remaining:02d}s"
    return f"{remaining}s"


def _section_for_payload(payload: NormalizedPayload) -> str:
    if payload.source_type == "podcast_episode":
        return "Podcast Signals"
    return "Newsletter" if payload.source_type == "gmail" else "Discovered Link"


def _editor_note_for_payload(payload: NormalizedPayload) -> str:
    if payload.source_type == "podcast_episode":
        return "Podcast episode ingested from a configured feed or aggregator search."
    if payload.source_type == "gmail_link":
        return "Extracted from an approved Gmail newsletter. Article fetch and enrichment are pending."
    return "Newsletter body ingested from an approved Gmail sender."


def _editor_note_for_result(result: ArticleFetchResult) -> str:
    if result.payload.source_type == "reddit_thread":
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        return f"Reddit thread selected from {result.payload.source_name} by Source Scout.{score}"
    if result.payload.source_type == "podcast_episode":
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        source = str((result.payload.metadata or {}).get("transcript_source") or "show notes").replace("_", " ")
        return f"Podcast episode summarized from {source}.{score}"
    if result.fetched:
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        return f"Fetched from a link discovered in an approved Gmail newsletter.{score}"
    return f"Lower-confidence fallback from newsletter context because article fetch returned: {result.status}."


def _title_for_payload(payload: NormalizedPayload) -> str:
    metadata = payload.metadata or {}
    link_text = metadata.get("link_text")
    if link_text:
        return str(link_text)
    reddit_title = metadata.get("title")
    if reddit_title:
        return str(reddit_title)
    subject = metadata.get("subject") or metadata.get("parent_subject")
    if subject:
        return str(subject)
    if payload.original_url:
        parsed = urlparse(payload.original_url)
        path = parsed.path.strip("/").replace("-", " ").replace("_", " ")
        return path[:120] or parsed.netloc
    return payload.source_name or "Gmail item"


def _summary_for_payload(payload: NormalizedPayload, max_chars: int = 320) -> str:
    text = _clean_newsletter_text(payload.raw_text)
    if not text and payload.original_url:
        text = payload.original_url
    if not text:
        text = _title_for_payload(payload)
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}..."


def clean_issue_html_for_display(html: str) -> str:
    """Apply display cleanup to issues generated before the newsletter scrubber existed."""
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for article in soup.select("article.newsletter"):
        meta = article.select_one(".meta")
        if meta is not None:
            sender, _, published = meta.get_text(" ", strip=True).partition("·")
            date = _format_issue_date(published.strip())
            meta.string = f"{sender.strip()} · {date}"
            changed = True

        paragraph = article.find("p")
        if paragraph is not None:
            cleaned = _truncate_text(_clean_newsletter_text(paragraph.get_text(" ", strip=True)), 700)
            if _weak_newsletter_snippet(cleaned):
                article.decompose()
                changed = True
                continue
            paragraph.string = cleaned
            changed = True
    for details in soup.select("details.source-notes"):
        if not details.has_attr("open"):
            details["open"] = ""
            changed = True
    for paragraph in soup.select("article.article-card p"):
        cleaned = _clean_newsletter_text(paragraph.get_text(" ", strip=True))
        if cleaned != paragraph.get_text(" ", strip=True):
            paragraph.string = cleaned
            changed = True
    return str(soup) if changed else html


def ensure_generated_footer(html: str, generated_at: str | None) -> str:
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("footer.issue-footer"):
        return html

    target = soup.find("main") or soup.body
    if target is None:
        return html

    _ensure_generated_footer_style(soup)
    footer = soup.new_tag("footer", attrs={"class": "issue-footer"})
    footer.string = f"Generated {_format_generated_timestamp(generated_at)}"
    target.append(footer)
    return str(soup)


def _ensure_generated_footer_style(soup: BeautifulSoup) -> None:
    if ".issue-footer" in soup.get_text(" ", strip=True):
        return
    head = soup.find("head")
    if head is None:
        return
    existing_style = "".join(style.get_text() for style in soup.find_all("style"))
    if ".issue-footer" in existing_style:
        return
    style = soup.new_tag("style", id="morning-dispatch-generated-footer-style")
    style.string = (
        ".issue-footer { margin-top: 36px; padding-top: 16px; border-top: 1px solid #d4cbbd; "
        "font: 700 .76rem Arial, sans-serif; color: #5f675f; text-transform: uppercase; }"
    )
    head.append(style)


def _clean_newsletter_text(value: str | None) -> str:
    text = unescape(value or "")
    text = ZERO_WIDTH_RE.sub(" ", text)
    text = IMAGE_PLACEHOLDER_RE.sub(" ", text)
    text = FOLLOW_IMAGE_RE.sub(" ", text)
    text = MARKDOWN_LINK_RE.sub(_newsletter_markdown_label, text)
    for pattern in NEWSLETTER_BOILERPLATE_PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(
        r"\b(?:read online|sign\s*up|signup|work with us|advertise|follow on x|archive|subscribe|unsubscribe|view online|view in browser)\b"
        r"(?:\s*\|\s*\b(?:read online|sign\s*up|signup|work with us|advertise|follow on x|archive|subscribe|unsubscribe|view online|view in browser)\b)+",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = RAW_URL_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = REFERENCE_MARK_RE.sub(" ", text)
    text = SEPARATOR_RE.sub(" ", text)
    text = text.replace("**", " ").replace("__", " ").replace("^^", " ").replace("^", " ").replace("`", " ")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" -|")
    if re.fullmatch(r"(?:online|read online|click here)[:.!?]?", text, flags=re.IGNORECASE):
        return ""
    return text


def _newsletter_markdown_label(match: re.Match[str]) -> str:
    label = _clean_markdown_label(match.group(1))
    if _is_newsletter_utility_label(label):
        return " "
    return f" {label} "


def _clean_markdown_label(label: str) -> str:
    text = ZERO_WIDTH_RE.sub(" ", unescape(label or ""))
    text = HTML_TAG_RE.sub(" ", text)
    text = text.replace("**", " ").replace("__", " ").replace("`", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -|")


def _is_newsletter_utility_label(label: str) -> bool:
    normalized = re.sub(r"\s+", " ", label.lower()).strip(" -|")
    return normalized in NEWSLETTER_UTILITY_LABELS


def _weak_newsletter_snippet(snippet: str) -> bool:
    text = re.sub(r"\s+", " ", snippet or "").strip()
    if not text:
        return True
    words = re.findall(r"[A-Za-z0-9]+", text)
    if len(words) <= 3 and re.fullmatch(r"(?:online|read online|click here)[:.!?]?", text, flags=re.IGNORECASE):
        return True
    if len(words) < 8 and NEWSLETTER_LOW_VALUE_RE.search(text):
        return True
    return not words


def _format_issue_date(value: str | None) -> str:
    if not value:
        return "Unknown date"
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return f"{parsed:%b} {parsed.day}, {parsed.year}"
    except ValueError:
        pass
    if "T" in text:
        return text.split("T", 1)[0]
    if "," in text:
        return text
    if " " in text:
        return text.split(" ", 1)[0]
    return text


def _format_article_date(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year:04d}"
        except ValueError:
            pass
    for pattern in ("%b %d, %Y", "%B %d, %Y"):
        try:
            parsed = datetime.strptime(text, pattern)
            return f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year:04d}"
        except ValueError:
            pass
    if "T" in text:
        date_part = text.split("T", 1)[0]
        try:
            parsed = datetime.strptime(date_part, "%Y-%m-%d")
            return f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year:04d}"
        except ValueError:
            return date_part
    return text


def _render_generated_footer(generated_at: str | None) -> str:
    return f'<footer class="issue-footer">Generated {escape(_format_generated_timestamp(generated_at))}</footer>'


def _format_generated_timestamp(value: str | None) -> str:
    if not value:
        value = utc_now()
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        local_zone = ZoneInfo(get_settings().scheduler_timezone)
    except ZoneInfoNotFoundError:
        local_zone = UTC
    parsed = parsed.astimezone(local_zone)
    hour = parsed.strftime("%I").lstrip("0") or "0"
    zone_label = parsed.tzname() or "UTC"
    return f"{parsed:%m/%d/%Y} {hour}:{parsed:%M} {parsed:%p} {zone_label}"


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}..."


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc.removeprefix("www.") or None


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM digest_runs WHERE id = ?", (run_id,)).fetchone()
    return row_to_dict(row)


def list_runs(digest_id: str) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM digest_runs WHERE digest_id = ? ORDER BY run_at DESC",
            (digest_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows if row is not None]


def get_latest_run_for_digest(digest_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM digest_runs
            WHERE digest_id = ?
            ORDER BY run_at DESC
            LIMIT 1
            """,
            (digest_id,),
        ).fetchone()
    return row_to_dict(row)


def get_latest_source_run_for_digest(digest_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM digest_runs
            WHERE digest_id = ?
              AND COALESCE(trigger, '') NOT IN ('controlled_verification', 'controlled_podcast_refresh')
            ORDER BY run_at DESC
            LIMIT 1
            """,
            (digest_id,),
        ).fetchone()
    return row_to_dict(row)


def list_article_results_for_run(run_id: str) -> list[ArticleFetchResult]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
              di.relevance_score, di.tier, di.section, di.editor_summary,
              a.id AS article_id,
              a.canonical_url, a.original_url, a.domain, a.publisher, a.published_at,
              a.title, a.cleaned_text, a.summary, a.keywords, a.content_type,
              a.fetch_status, a.quality_flag,
              ad.discovery_source_type, ad.discovery_source_name, ad.sender_email,
              ad.message_id, ad.thread_id, ad.link_text
            FROM digest_items di
            JOIN articles a ON a.id = di.article_id
            LEFT JOIN article_discoveries ad ON ad.id = di.discovery_id
            WHERE di.run_id = ? AND COALESCE(di.tier, '') != 'source'
            ORDER BY
              CASE di.tier
                WHEN 'lead' THEN 0
                WHEN 'main' THEN 1
                WHEN 'lower_confidence' THEN 2
                WHEN 'dropped' THEN 4
                ELSE 3
              END,
              COALESCE(di.relevance_score, 0) DESC
            """,
            (run_id,),
        ).fetchall()
    return [_digest_item_row_to_fetch_result(row) for row in rows]


def list_newsletter_payloads_for_run(run_id: str) -> list[NormalizedPayload]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
              a.original_url, a.publisher, a.published_at, a.title, a.cleaned_text, a.summary,
              ad.discovery_source_type, ad.discovery_source_name, ad.sender_email, ad.message_id, ad.link_text
            FROM digest_items di
            JOIN articles a ON a.id = di.article_id
            LEFT JOIN article_discoveries ad ON ad.id = di.discovery_id
            WHERE di.run_id = ? AND COALESCE(di.tier, '') = 'source'
            ORDER BY a.published_at DESC
            """,
            (run_id,),
        ).fetchall()

    payloads: list[NormalizedPayload] = []
    for row in rows:
        payloads.append(
            NormalizedPayload(
                source_type=str(row["discovery_source_type"] or "gmail"),
                source_name=str(row["sender_email"] or row["publisher"] or row["discovery_source_name"] or "Gmail"),
                raw_text=str(row["cleaned_text"] or row["summary"] or ""),
                original_url=row["original_url"],
                published_at=row["published_at"],
                metadata={
                    "gmail_message_id": row["message_id"],
                    "sender_email": row["sender_email"],
                    "subject": row["title"],
                    "link_text": row["link_text"],
                },
            )
        )
    return payloads


def list_digest_overviews(*, include_archived: bool = False) -> list[dict[str, Any]]:
    with connect() as connection:
        where_clause = "" if include_archived else "WHERE COALESCE(d.status, 'active') != 'archived'"
        rows = connection.execute(
            f"""
            SELECT
              d.id, d.name, d.schedule, d.status, d.sources, d.updated_at,
              r.id AS latest_run_id,
              r.inference_run_id AS latest_inference_run_id,
              r.run_at AS latest_run_at,
              r.completed_at AS latest_completed_at,
              r.item_count AS latest_item_count,
              r.failed_count AS latest_failed_count,
              r.fallback_count AS latest_fallback_count,
              r.newsletter_count AS latest_newsletter_count,
              r.link_count AS latest_link_count,
              r.fetched_article_count AS latest_fetched_article_count,
              r.model_cache_hit_count AS latest_model_cache_hit_count,
              r.model_cache_miss_count AS latest_model_cache_miss_count,
              r.model_cache_write_count AS latest_model_cache_write_count,
              r.duration_seconds AS latest_duration_seconds,
              r.trigger AS latest_trigger,
              r.status AS latest_run_status,
              i.id AS latest_issue_id,
              i.title AS latest_issue_title,
              i.created_at AS latest_issue_created_at
            FROM digests d
            LEFT JOIN digest_runs r ON r.id = (
              SELECT id FROM digest_runs
              WHERE digest_id = d.id
              ORDER BY run_at DESC
              LIMIT 1
            )
            LEFT JOIN digest_issues i ON i.run_id = r.id
            {where_clause}
            ORDER BY d.updated_at DESC
            """
        ).fetchall()

    overviews: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        try:
            sources = json.loads(record.pop("sources") or "[]")
        except json.JSONDecodeError:
            sources = []
        record["source_count"] = len(sources) if isinstance(sources, list) else 0
        overviews.append(record)
    return overviews


def latest_digest_stats() -> dict[str, Any]:
    latest = _latest_run_row()
    if latest is None:
        return {
            "run_id": None,
            "generated_at": None,
            "source_count": 0,
            "newsletter_count": 0,
            "link_count": 0,
            "podcast_episode_count": 0,
            "article_candidate_count": 0,
            "included_article_count": 0,
            "unresolved_count": 0,
            "dropped_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "model_call_count": 0,
            "processing_seconds": None,
            "stage_seconds": {},
        }
    stats = _digest_stats_from_run_row(latest)
    stats["run_id"] = latest["id"]
    stats["generated_at"] = latest["completed_at"] or latest["run_at"]
    return stats


def _digest_stats_from_run_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    metadata = _json_dict(record.get("run_metadata"))
    stats = metadata.get("digest_stats") if isinstance(metadata.get("digest_stats"), dict) else {}
    if stats:
        normalized = {
            "source_count": int(stats.get("source_count") or 0),
            "newsletter_count": int(stats.get("newsletter_count") or 0),
            "link_count": int(stats.get("link_count") or 0),
            "podcast_episode_count": int(stats.get("podcast_episode_count") or 0),
            "article_candidate_count": int(stats.get("article_candidate_count") or 0),
            "included_article_count": int(stats.get("included_article_count") or 0),
            "unresolved_count": int(stats.get("unresolved_count") or 0),
            "dropped_count": int(stats.get("dropped_count") or 0),
            "prompt_tokens": int(stats.get("prompt_tokens") or 0),
            "completion_tokens": int(stats.get("completion_tokens") or 0),
            "total_tokens": int(stats.get("total_tokens") or 0),
            "model_call_count": int(stats.get("model_call_count") or 0),
            "processing_seconds": _nullable_float(stats.get("processing_seconds")),
            "stage_seconds": _normalize_stage_seconds(stats.get("stage_seconds")),
        }
        return normalized

    token_summary = inference_token_summary(record.get("inference_run_id"))
    included = int(record.get("fetched_article_count") or 0)
    unresolved = int(record.get("failed_count") or 0)
    return {
        "source_count": 0,
        "newsletter_count": int(record.get("newsletter_count") or 0),
        "link_count": int(record.get("link_count") or 0),
        "podcast_episode_count": 0,
        "article_candidate_count": included + unresolved,
        "included_article_count": included,
        "unresolved_count": unresolved,
        "dropped_count": 0,
        "prompt_tokens": token_summary["prompt_tokens"],
        "completion_tokens": token_summary["completion_tokens"],
        "total_tokens": token_summary["total_tokens"],
        "model_call_count": token_summary["model_call_count"],
        "processing_seconds": _nullable_float(record.get("duration_seconds")),
        "stage_seconds": {},
    }


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_delivery_settings(digest_id: str) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT digest_id, recipient_email, enabled, last_delivery_status,
                   last_delivered_at, last_error, updated_at
            FROM digest_delivery_settings
            WHERE digest_id = ?
            """,
            (digest_id,),
        ).fetchone()
    if row is None:
        return {
            "digest_id": digest_id,
            "recipient_email": "",
            "enabled": False,
            "last_delivery_status": None,
            "last_delivered_at": None,
            "last_error": None,
            "updated_at": None,
        }
    record = dict(row)
    record["enabled"] = bool(record.get("enabled"))
    return record


def update_delivery_settings(*, digest_id: str, recipient_email: str, enabled: bool) -> dict[str, Any] | None:
    if get_digest(digest_id) is None:
        return None
    now = utc_now()
    email = recipient_email.strip()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO digest_delivery_settings (
              digest_id, recipient_email, enabled, updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(digest_id) DO UPDATE SET
              recipient_email = excluded.recipient_email,
              enabled = excluded.enabled,
              updated_at = excluded.updated_at
            """,
            (digest_id, email, int(bool(enabled and email)), now),
        )
    return get_delivery_settings(digest_id)


def record_delivery_result(
    *,
    digest_id: str,
    status: str,
    error: str | None = None,
    delivered_at: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO digest_delivery_settings (
              digest_id, recipient_email, enabled, last_delivery_status,
              last_delivered_at, last_error, updated_at
            )
            VALUES (?, '', 0, ?, ?, ?, ?)
            ON CONFLICT(digest_id) DO UPDATE SET
              last_delivery_status = excluded.last_delivery_status,
              last_delivered_at = excluded.last_delivered_at,
              last_error = excluded.last_error,
              updated_at = excluded.updated_at
            """,
            (digest_id, status, delivered_at, error, now),
        )
    return get_delivery_settings(digest_id)


def enabled_delivery_settings() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT digest_id, recipient_email, enabled, last_delivery_status,
                   last_delivered_at, last_error, updated_at
            FROM digest_delivery_settings
            WHERE enabled = 1 AND COALESCE(recipient_email, '') != ''
            """
        ).fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["enabled"] = bool(record.get("enabled"))
        records.append(record)
    return records


def model_cache_summary() -> dict[str, Any]:
    with connect() as connection:
        total = connection.execute("SELECT COUNT(*) FROM model_enrichment_cache").fetchone()[0]
        latest = connection.execute("SELECT MAX(updated_at) FROM model_enrichment_cache").fetchone()[0]
        rows = connection.execute(
            """
            SELECT model_name, COUNT(*) AS record_count, MAX(updated_at) AS latest_updated_at
            FROM model_enrichment_cache
            GROUP BY model_name
            ORDER BY latest_updated_at DESC
            """
        ).fetchall()
    return {
        "record_count": int(total or 0),
        "latest_updated_at": latest,
        "models": [dict(row) for row in rows],
    }


INFERENCE_METRIC_COLUMNS = {
    "id",
    "run_id",
    "article_id",
    "ts",
    "model",
    "model_tag",
    "quantization",
    "backend",
    "mode",
    "queue_wait_ms",
    "ttft_ms",
    "generation_ms",
    "total_ms",
    "prompt_tokens",
    "completion_tokens",
    "tokens_per_sec",
    "classification_label",
    "classification_confidence",
    "schema_valid",
    "summary_word_count",
    "fallback_triggered",
    "status",
    "error_detail",
}


def record_inference_metric(metric: dict[str, Any]) -> str:
    metric_id = str(metric.get("id") or new_id())
    status = str(metric.get("status") or "model_error")
    if status not in INFERENCE_METRIC_STATUSES:
        status = "model_error"
    row = {
        "id": metric_id,
        "run_id": str(metric.get("run_id") or "manual"),
        "article_id": str(metric.get("article_id") or "unknown"),
        "ts": str(metric.get("ts") or utc_now()),
        "model": str(metric.get("model") or "unknown"),
        "model_tag": _nullable_str(metric.get("model_tag")),
        "quantization": _nullable_str(metric.get("quantization")),
        "backend": _nullable_str(metric.get("backend")),
        "mode": str(metric.get("mode") or "single"),
        "queue_wait_ms": _nullable_int(metric.get("queue_wait_ms")),
        "ttft_ms": _nullable_int(metric.get("ttft_ms")),
        "generation_ms": _nullable_int(metric.get("generation_ms")),
        "total_ms": max(0, int(metric.get("total_ms") or 0)),
        "prompt_tokens": _nullable_int(metric.get("prompt_tokens")),
        "completion_tokens": _nullable_int(metric.get("completion_tokens")),
        "tokens_per_sec": _nullable_float(metric.get("tokens_per_sec")),
        "classification_label": _nullable_str(metric.get("classification_label")),
        "classification_confidence": _nullable_float(metric.get("classification_confidence")),
        "schema_valid": int(bool(metric.get("schema_valid"))),
        "summary_word_count": _nullable_int(metric.get("summary_word_count")),
        "fallback_triggered": int(bool(metric.get("fallback_triggered"))),
        "status": status,
        "error_detail": _nullable_str(metric.get("error_detail")),
    }
    placeholders = ", ".join("?" for _column in row)
    columns = ", ".join(row)
    with connect() as connection:
        connection.execute(
            f"INSERT INTO inference_metrics ({columns}) VALUES ({placeholders})",
            tuple(row.values()),
        )
    return metric_id


def inference_metrics_summary(*, limit: int = 5000) -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM inference_metrics
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    records = [dict(row) for row in rows]
    total_count = len(records)
    success_count = sum(1 for row in records if row["status"] == "success")
    status_counts: dict[str, int] = {}
    for row in records:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1

    groups: dict[tuple[str, str | None, str | None, str | None], list[dict[str, Any]]] = {}
    for row in records:
        key = (
            str(row["model"]),
            _nullable_str(row.get("backend")),
            _nullable_str(row.get("model_tag")),
            _nullable_str(row.get("quantization")),
        )
        groups.setdefault(key, []).append(row)

    model_summaries = []
    for (model, backend, model_tag, quantization), group in groups.items():
        durations = sorted(_model_service_ms(row) for row in group if row.get("total_ms") is not None)
        queue_waits = sorted(int(row["queue_wait_ms"]) for row in group if row.get("queue_wait_ms") is not None)
        prompt_tokens = [int(row["prompt_tokens"]) for row in group if row.get("prompt_tokens") is not None]
        completion_tokens = [int(row["completion_tokens"]) for row in group if row.get("completion_tokens") is not None]
        token_rates = [float(row["tokens_per_sec"]) for row in group if row.get("tokens_per_sec") is not None]
        average_ms = _average(durations)
        success = sum(1 for row in group if row["status"] == "success")
        model_summaries.append(
            {
                "model": model,
                "backend": backend,
                "model_tag": model_tag,
                "quantization": quantization,
                "record_count": len(group),
                "success_count": success,
                "failure_count": len(group) - success,
                "avg_total_ms": average_ms,
                "p50_total_ms": _percentile(durations, 50),
                "p95_total_ms": _percentile(durations, 95),
                "avg_queue_wait_ms": _average(queue_waits),
                "avg_prompt_tokens": _average(prompt_tokens),
                "avg_completion_tokens": _average(completion_tokens),
                "avg_tokens_per_sec": _average(token_rates),
                "schema_valid_rate": _rate(sum(1 for row in group if row.get("schema_valid")), len(group)),
                "fallback_rate": _rate(sum(1 for row in group if row.get("fallback_triggered")), len(group)),
                "articles_per_minute": round(60000 / average_ms, 2) if average_ms and average_ms > 0 else None,
                "estimated_100_seconds": round((average_ms * 100) / 1000, 1) if average_ms else None,
                "estimated_500_seconds": round((average_ms * 500) / 1000, 1) if average_ms else None,
            }
        )

    model_summaries.sort(key=lambda row: (row["record_count"], row["success_count"]), reverse=True)
    recent = records[:20]
    return {
        "record_count": total_count,
        "success_count": success_count,
        "failure_count": total_count - success_count,
        "latest_ts": records[0]["ts"] if records else None,
        "status_counts": status_counts,
        "models": model_summaries,
        "recent": recent,
        "ttft_available": any(row.get("ttft_ms") is not None for row in records),
    }


def agent_decisions_summary(*, limit: int = 500) -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT agent, decision, action, model_name, created_at
            FROM agent_decisions
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    agent_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    decision_counts: dict[str, int] = {}
    latest_created_at: str | None = None
    latest_model_name: str | None = None
    for row in rows:
        agent = str(row["agent"] or "unknown")
        action = str(row["action"] or "none")
        decision = str(row["decision"] or "unknown")
        agent_counts[agent] = agent_counts.get(agent, 0) + 1
        action_counts[action] = action_counts.get(action, 0) + 1
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        latest_created_at = latest_created_at or row["created_at"]
        latest_model_name = latest_model_name or row["model_name"]

    return {
        "record_count": len(rows),
        "latest_created_at": latest_created_at,
        "latest_model_name": latest_model_name,
        "agent_counts": agent_counts,
        "action_counts": action_counts,
        "decision_counts": decision_counts,
    }


def record_podcast_metric(metric: dict[str, Any]) -> str:
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


def list_agent_decisions(*, limit: int = 25) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT id, run_id, digest_id, inference_run_id, agent, target, decision,
                   action, confidence, reason, model_name, metadata, created_at
            FROM agent_decisions
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()

    decisions: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        try:
            metadata = json.loads(record.get("metadata") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        record["metadata"] = metadata if isinstance(metadata, dict) else {}
        decisions.append(record)
    return decisions


def list_latest_agent_decisions_for_run(run_id: str) -> list[dict[str, Any]]:
    with connect() as connection:
        latest = connection.execute(
            """
            SELECT created_at
            FROM agent_decisions
            WHERE run_id = ?
              AND decision NOT IN ('fallback', 'skipped')
              AND action NOT IN ('deterministic_ranking', 'deterministic_repairs', 'single_candidate')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if latest is None:
            return []
        rows = connection.execute(
            """
            SELECT id, run_id, digest_id, inference_run_id, agent, target, decision,
                   action, confidence, reason, model_name, metadata, created_at
            FROM agent_decisions
            WHERE run_id = ? AND created_at = ?
            ORDER BY id
            """,
            (run_id, latest["created_at"]),
        ).fetchall()

    records: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        try:
            metadata = json.loads(record.get("metadata") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        record["metadata"] = metadata if isinstance(metadata, dict) else {}
        records.append(record)
    return records


def add_agent_decisions_for_run(
    *,
    run_id: str,
    digest_id: str,
    inference_run_id: str | None,
    decisions: list[AgentDecision],
) -> int:
    if not decisions:
        return 0
    now = utc_now()
    with connect() as connection:
        _insert_agent_decisions(
            connection,
            run_id=run_id,
            digest_id=digest_id,
            inference_run_id=inference_run_id,
            decisions=decisions,
            now=now,
        )
    return len(decisions)


def create_model_enrichment_job(*, model_name: str, limit_count: int, include_cached: bool = False) -> dict[str, Any]:
    job_id = new_id()
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO model_enrichment_jobs (
              id, model_name, status, limit_count, include_cached, created_at
            )
            VALUES (?, ?, 'queued', ?, ?, ?)
            """,
            (job_id, model_name, max(1, limit_count), int(include_cached), now),
        )
    job = get_model_enrichment_job(job_id)
    if job is None:
        raise RuntimeError("Model enrichment job was not created")
    return job


def update_model_enrichment_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {
        "status",
        "processed_count",
        "success_count",
        "cache_hit_count",
        "failure_count",
        "avg_total_ms",
        "estimated_100_seconds",
        "error_detail",
        "started_at",
        "completed_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return get_model_enrichment_job(job_id)
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with connect() as connection:
        connection.execute(
            f"UPDATE model_enrichment_jobs SET {assignments} WHERE id = ?",
            (*updates.values(), job_id),
        )
    return get_model_enrichment_job(job_id)


def get_model_enrichment_job(job_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM model_enrichment_jobs WHERE id = ?", (job_id,)).fetchone()
    return row_to_dict(row)


def list_model_enrichment_jobs(*, limit: int = 10) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM model_enrichment_jobs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [row_to_dict(row) for row in rows if row is not None]


def list_model_enrichment_candidates(
    *,
    model_name: str,
    limit_count: int,
    include_cached: bool = False,
) -> list[ArticleFetchResult]:
    query_limit = max(limit_count * 4, limit_count, 25)
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM articles
            WHERE fetch_status = 'fetched'
              AND cleaned_text IS NOT NULL
              AND LENGTH(TRIM(cleaned_text)) > 0
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (query_limit,),
        ).fetchall()

        candidates: list[ArticleFetchResult] = []
        for row in rows:
            result = _article_row_to_fetch_result(row)
            if not include_cached:
                cache_identity = _model_cache_identity(result, model_name)
                if cache_identity is not None:
                    cached = connection.execute(
                        "SELECT 1 FROM model_enrichment_cache WHERE cache_key = ?",
                        (cache_identity[0],),
                    ).fetchone()
                    if cached is not None:
                        continue
            candidates.append(result)
            if len(candidates) >= limit_count:
                break
    return candidates


def apply_feedback_to_candidates(digest_id: str, article_results: list[ArticleFetchResult]) -> list[ArticleFetchResult]:
    if not article_results:
        return article_results
    with connect() as connection:
        weights = {
            str(row["source_name"]): float(row["weight"])
            for row in connection.execute(
                "SELECT source_name, weight FROM source_weights WHERE digest_id = ?",
                (digest_id,),
            ).fetchall()
        }
        rows = connection.execute(
            """
            SELECT a.canonical_url, a.original_url, a.domain, f.signal, COUNT(*) AS signal_count
            FROM feedback f
            JOIN articles a ON a.id = f.article_id
            WHERE f.digest_id = ?
            GROUP BY a.canonical_url, a.original_url, a.domain, f.signal
            """,
            (digest_id,),
        ).fetchall()

    exact_signals: dict[str, int] = {}
    domain_signals: dict[str, int] = {}
    for row in rows:
        value = int(row["signal_count"] or 0)
        delta = value if row["signal"] == "up" else -value
        for url in (row["canonical_url"], row["original_url"]):
            key = _url_match_key(url)
            if key:
                exact_signals[key] = exact_signals.get(key, 0) + delta
        domain = str(row["domain"] or "")
        if domain:
            domain_signals[domain] = domain_signals.get(domain, 0) + delta

    adjusted: list[ArticleFetchResult] = []
    for result in article_results:
        url_key = _url_match_key(result.canonical_url or result.final_url or result.original_url)
        domain = result.domain or _domain(result.final_url or result.original_url) or result.payload.source_name
        source_weight = weights.get(domain, 1.0)
        exact_delta = max(-0.25, min(0.25, exact_signals.get(url_key, 0) * 0.08)) if url_key else 0.0
        domain_delta = max(-0.12, min(0.12, domain_signals.get(domain, 0) * 0.02)) if domain else 0.0
        adjusted_score = max(0.0, min(1.0, (result.link_score * source_weight) + exact_delta + domain_delta))
        adjusted.append(replace(result, link_score=round(adjusted_score, 3)))
    return adjusted


def record_feedback(*, issue_id: str, url: str, signal: str) -> dict[str, Any] | None:
    if signal not in {"up", "down"}:
        raise ValueError("Feedback signal must be up or down")

    url_key = _url_match_key(url)
    if not url_key:
        return None
    now = utc_now()
    with connect() as connection:
        issue = connection.execute(
            "SELECT id, run_id, digest_id FROM digest_issues WHERE id = ?",
            (issue_id,),
        ).fetchone()
        if issue is None:
            return None

        rows = connection.execute(
            """
            SELECT di.id AS digest_item_id, di.digest_id, a.id AS article_id,
                   a.canonical_url, a.original_url, a.domain, a.publisher
            FROM digest_items di
            JOIN articles a ON a.id = di.article_id
            WHERE di.run_id = ? AND COALESCE(di.tier, '') != 'source'
            """,
            (issue["run_id"],),
        ).fetchall()
        matched = next(
            (
                row
                for row in rows
                if url_key in {_url_match_key(row["canonical_url"]), _url_match_key(row["original_url"])}
            ),
            None,
        )
        if matched is None:
            return None

        feedback_id = new_id()
        connection.execute(
            """
            INSERT INTO feedback (id, digest_item_id, article_id, digest_id, signal, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_id,
                matched["digest_item_id"],
                matched["article_id"],
                issue["digest_id"],
                signal,
                now,
            ),
        )
        source_name = str(matched["domain"] or matched["publisher"] or "")
        if source_name:
            _update_source_weight(connection, str(issue["digest_id"]), source_name, signal, now)

    return {
        "id": feedback_id,
        "issue_id": issue_id,
        "signal": signal,
        "url": url,
        "source_name": source_name,
        "created_at": now,
    }


def fetch_failure_breakdown(*, limit: int = 5) -> dict[str, Any]:
    latest = _latest_run_row()
    if latest is None:
        return {"run_id": None, "total_count": 0, "groups": [], "examples": []}

    with connect() as connection:
        rows = connection.execute(
            """
            SELECT di.id AS digest_item_id, a.id AS article_id, a.title, a.canonical_url,
                   a.original_url, a.domain, a.fetch_status, a.quality_flag,
                   di.editor_summary, di.editor_note, ad.newsletter_snippet, ad.link_text
            FROM digest_items di
            JOIN articles a ON a.id = di.article_id
            LEFT JOIN article_discoveries ad ON ad.id = di.discovery_id
            WHERE di.run_id = ?
              AND COALESCE(di.tier, '') != 'source'
              AND COALESCE(a.fetch_status, 'fetched') != 'fetched'
            ORDER BY COALESCE(di.relevance_score, 0) DESC, di.created_at DESC
            """,
            (latest["id"],),
        ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    examples: list[dict[str, Any]] = []
    for row in rows:
        status = str(row["fetch_status"] or "unknown")
        group = groups.setdefault(
            status,
            {
                "status": status,
                "count": 0,
                "fixability": _fetch_fixability(status),
            },
        )
        group["count"] += 1
        if len(examples) < limit:
            examples.append(_failure_example(row))

    return {
        "run_id": latest["id"],
        "run_at": latest["run_at"],
        "digest_id": latest["digest_id"],
        "total_count": len(rows),
        "groups": sorted(groups.values(), key=lambda item: item["count"], reverse=True),
        "examples": examples,
    }


def brief_review(*, limit: int = 8) -> dict[str, Any]:
    latest = _latest_run_row()
    if latest is None:
        return {
            "run_id": None,
            "issue_id": None,
            "generated_at": None,
            "counts": {"included": 0, "unresolved": 0, "dropped": 0, "duplicate": 0, "repaired": 0},
            "included": [],
            "unresolved": [],
            "dropped": [],
            "duplicates": [],
            "repaired": [],
        }

    with connect() as connection:
        issue = connection.execute(
            "SELECT id, created_at FROM digest_issues WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (latest["id"],),
        ).fetchone()
        item_rows = connection.execute(
            """
            SELECT di.tier, di.section, di.relevance_score, di.editor_summary,
                   a.title, a.canonical_url, a.original_url, a.domain, a.fetch_status,
                   a.quality_flag
            FROM digest_items di
            JOIN articles a ON a.id = di.article_id
            WHERE di.run_id = ? AND COALESCE(di.tier, '') != 'source'
            ORDER BY
              CASE di.tier WHEN 'lead' THEN 0 WHEN 'main' THEN 1 WHEN 'lower_confidence' THEN 2 ELSE 3 END,
              COALESCE(di.relevance_score, 0) DESC
            """,
            (latest["id"],),
        ).fetchall()
        decision_rows = connection.execute(
            """
            SELECT agent, target, decision, action, reason, confidence, created_at
            FROM agent_decisions
            WHERE run_id = ?
            ORDER BY created_at DESC
            """,
            (latest["id"],),
        ).fetchall()

    included = [_review_item(row) for row in item_rows if row["fetch_status"] == "fetched" and row["tier"] != "dropped"]
    unresolved = [_review_item(row) for row in item_rows if row["fetch_status"] != "fetched" and row["tier"] != "dropped"]
    dropped_rows = [
        row for row in decision_rows
        if str(row["action"] or "") in {"drop", "drop_article"} or str(row["decision"] or "") in {"exclude", "weak_fallback"}
    ]
    duplicate_rows = [row for row in decision_rows if str(row["decision"] or "") == "duplicate"]
    repaired_rows = [row for row in decision_rows if str(row["action"] or "") == "repair_article"]
    return {
        "run_id": latest["id"],
        "issue_id": issue["id"] if issue else None,
        "generated_at": issue["created_at"] if issue else latest["completed_at"],
        "counts": {
            "included": len(included),
            "unresolved": len(unresolved),
            "dropped": len(dropped_rows),
            "duplicate": len(duplicate_rows),
            "repaired": len(repaired_rows),
        },
        "included": included[:limit],
        "unresolved": unresolved[:limit],
        "dropped": [_review_decision(row) for row in dropped_rows[:limit]],
        "duplicates": [_review_decision(row) for row in duplicate_rows[:limit]],
        "repaired": [_review_decision(row) for row in repaired_rows[:limit]],
    }


def _latest_run_row() -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute(
            """
            SELECT *
            FROM digest_runs
            WHERE status = 'completed'
            ORDER BY completed_at DESC, run_at DESC
            LIMIT 1
            """
        ).fetchone()


def _failure_example(row: sqlite3.Row) -> dict[str, Any]:
    status = str(row["fetch_status"] or "unknown")
    reason = str(row["quality_flag"] or status)
    return {
        "title": row["title"] or row["link_text"] or "Untitled link",
        "url": row["canonical_url"] or row["original_url"],
        "domain": row["domain"],
        "status": status,
        "reason": reason,
        "fixability": _fetch_fixability(status),
        "context": row["newsletter_snippet"] or row["editor_summary"] or row["editor_note"],
    }


def _review_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "title": row["title"] or "Untitled article",
        "url": row["canonical_url"] or row["original_url"],
        "domain": row["domain"],
        "tier": row["tier"],
        "section": row["section"],
        "status": row["fetch_status"],
        "reason": row["quality_flag"],
        "score": _nullable_float(row["relevance_score"]),
        "summary": _truncate_text(str(row["editor_summary"] or ""), 220),
    }


def _review_decision(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "agent": row["agent"],
        "target": row["target"],
        "decision": row["decision"],
        "action": row["action"],
        "reason": row["reason"],
        "confidence": _nullable_float(row["confidence"]),
        "created_at": row["created_at"],
    }


def _fetch_fixability(status: str) -> str:
    if status in {"blocked", "rate_limited"}:
        return "Usually fixable with retry, reader mode, or a different fetch path."
    if status in {"site_error", "fetch_error", "http_error"}:
        return "Worth retrying; may be temporary."
    if status == "no_content":
        return "Use newsletter context unless reader extraction improves."
    if status in {"not_found", "non_html"}:
        return "Usually safe to ignore unless the title looks important."
    return "Needs review."


def _update_source_weight(
    connection: sqlite3.Connection,
    digest_id: str,
    source_name: str,
    signal: str,
    now: str,
) -> None:
    row = connection.execute(
        "SELECT weight FROM source_weights WHERE digest_id = ? AND source_name = ?",
        (digest_id, source_name),
    ).fetchone()
    current = float(row["weight"]) if row else 1.0
    delta = 0.04 if signal == "up" else -0.06
    updated = max(0.55, min(1.45, round(current + delta, 3)))
    connection.execute(
        """
        INSERT INTO source_weights (digest_id, source_name, weight, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(digest_id, source_name) DO UPDATE SET
          weight = excluded.weight,
          updated_at = excluded.updated_at
        """,
        (digest_id, source_name, updated, now),
    )


def _url_match_key(url: Any) -> str:
    if not url:
        return ""
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.lower().startswith("utm_")
    ]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query_items), ""))


def get_latest_issue(digest_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM digest_issues
            WHERE digest_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (digest_id,),
        ).fetchone()
    return row_to_dict(row)


def get_issue(issue_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM digest_issues WHERE id = ?", (issue_id,)).fetchone()
    return row_to_dict(row)
