from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA_SQL)
        _ensure_explore_tables(connection)
        _ensure_digest_run_metric_columns(connection)
        _ensure_digest_delivery_settings_table(connection)
        _ensure_podcast_metrics_table(connection)
        _ensure_youtube_quota_table(connection)
        _ensure_collection_tables(connection)
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


def _ensure_youtube_quota_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS youtube_quota_usage (
          usage_date       TEXT PRIMARY KEY,
          units_used       INTEGER NOT NULL DEFAULT 0,
          updated_at       TEXT NOT NULL
        ) STRICT
        """
    )


def _ensure_collection_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_files (
          id                TEXT PRIMARY KEY,
          collection_name   TEXT NOT NULL,
          file_path         TEXT NOT NULL UNIQUE,
          relative_path     TEXT NOT NULL,
          file_type         TEXT NOT NULL,
          last_modified     REAL NOT NULL,
          last_indexed      REAL,
          status            TEXT NOT NULL CHECK(status IN ('pending','indexed','failed','unsupported')),
          error_message     TEXT,
          chunk_count       INTEGER NOT NULL DEFAULT 0,
          updated_at        TEXT NOT NULL
        ) STRICT
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_chunks (
          id                TEXT PRIMARY KEY,
          file_id           TEXT NOT NULL REFERENCES collection_files(id) ON DELETE CASCADE,
          collection_name   TEXT NOT NULL,
          file_path         TEXT NOT NULL,
          relative_path     TEXT NOT NULL,
          chunk_index       INTEGER NOT NULL,
          text              TEXT NOT NULL,
          created_at        TEXT NOT NULL
        ) STRICT
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_collection_files_collection ON collection_files(collection_name)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_collection_files_status ON collection_files(status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_collection_chunks_collection ON collection_chunks(collection_name)")


def record_youtube_quota(units_used: int, *, usage_date: str | None = None) -> dict[str, Any]:
    units = max(0, int(units_used or 0))
    if units <= 0:
        return youtube_quota_summary(usage_date=usage_date)
    date_key = usage_date or _pacific_date_key()
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO youtube_quota_usage (usage_date, units_used, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(usage_date) DO UPDATE SET
              units_used = youtube_quota_usage.units_used + excluded.units_used,
              updated_at = excluded.updated_at
            """,
            (date_key, units, now),
        )
        row = connection.execute(
            "SELECT * FROM youtube_quota_usage WHERE usage_date = ?",
            (date_key,),
        ).fetchone()
    return row_to_dict(row) or {"usage_date": date_key, "units_used": units, "updated_at": now}


def youtube_quota_summary(*, usage_date: str | None = None) -> dict[str, Any]:
    date_key = usage_date or _pacific_date_key()
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM youtube_quota_usage WHERE usage_date = ?",
            (date_key,),
        ).fetchone()
    record = row_to_dict(row) or {"usage_date": date_key, "units_used": 0, "updated_at": None}
    record["units_used"] = int(record.get("units_used") or 0)
    return record


def upsert_collection_file(
    *,
    collection_name: str,
    file_path: str,
    relative_path: str,
    file_type: str,
    last_modified: float,
    status: str,
    error_message: str | None = None,
    chunk_count: int = 0,
) -> dict[str, Any]:
    now = utc_now()
    with connect() as connection:
        existing = connection.execute(
            "SELECT id FROM collection_files WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        file_id = str(existing["id"]) if existing else new_id()
        connection.execute(
            """
            INSERT INTO collection_files (
              id, collection_name, file_path, relative_path, file_type, last_modified,
              last_indexed, status, error_message, chunk_count, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
              collection_name = excluded.collection_name,
              relative_path = excluded.relative_path,
              file_type = excluded.file_type,
              last_modified = excluded.last_modified,
              last_indexed = excluded.last_indexed,
              status = excluded.status,
              error_message = excluded.error_message,
              chunk_count = excluded.chunk_count,
              updated_at = excluded.updated_at
            """,
            (
                file_id,
                collection_name,
                file_path,
                relative_path,
                file_type,
                float(last_modified),
                float(last_modified) if status == "indexed" else None,
                status,
                error_message,
                max(0, int(chunk_count or 0)),
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM collection_files WHERE file_path = ?",
            (file_path,),
        ).fetchone()
    return row_to_dict(row) or {}


def replace_collection_chunks(
    *,
    file_id: str,
    collection_name: str,
    file_path: str,
    relative_path: str,
    chunks: list[str],
) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute("DELETE FROM collection_chunks WHERE file_id = ?", (file_id,))
        for index, text in enumerate(chunks):
            clean_text = str(text or "").strip()
            if not clean_text:
                continue
            connection.execute(
                """
                INSERT INTO collection_chunks (
                  id, file_id, collection_name, file_path, relative_path, chunk_index, text, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    file_id,
                    collection_name,
                    file_path,
                    relative_path,
                    index,
                    clean_text,
                    now,
                ),
            )


def delete_collection_files_not_seen(*, root_path: str, seen_paths: set[str]) -> int:
    prefix = str(Path(root_path).expanduser())
    deleted = 0
    with connect() as connection:
        rows = connection.execute("SELECT id, file_path FROM collection_files").fetchall()
        for row in rows:
            file_path = str(row["file_path"] or "")
            if not file_path.startswith(prefix):
                continue
            if file_path in seen_paths:
                continue
            connection.execute("DELETE FROM collection_files WHERE id = ?", (row["id"],))
            deleted += 1
    return deleted


def collection_index_summary(*, root_path: str | None = None) -> dict[str, Any]:
    prefix = str(Path(root_path).expanduser()) if root_path else None
    with connect() as connection:
        rows = connection.execute("SELECT * FROM collection_files").fetchall()
    records = [row_to_dict(row) or {} for row in rows]
    if prefix:
        records = [record for record in records if str(record.get("file_path") or "").startswith(prefix)]
    collections = sorted({str(record.get("collection_name") or "") for record in records if record.get("collection_name")})
    return {
        "collection_count": len(collections),
        "collections": collections,
        "file_count": len(records),
        "indexed_count": sum(1 for record in records if record.get("status") == "indexed"),
        "unsupported_count": sum(1 for record in records if record.get("status") == "unsupported"),
        "failed_count": sum(1 for record in records if record.get("status") == "failed"),
        "chunk_count": sum(int(record.get("chunk_count") or 0) for record in records),
    }


def list_collection_chunks(*, collection_names: list[str] | None = None, limit: int = 1000) -> list[dict[str, Any]]:
    names = {name.strip().lower() for name in collection_names or [] if name.strip()}
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT cc.*, cf.status
            FROM collection_chunks cc
            JOIN collection_files cf ON cf.id = cc.file_id
            WHERE cf.status = 'indexed'
            ORDER BY cc.created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit or 1000)),),
        ).fetchall()
    records = [row_to_dict(row) or {} for row in rows]
    if names:
        records = [record for record in records if str(record.get("collection_name") or "").strip().lower() in names]
    return records


def _pacific_date_key() -> str:
    try:
        timezone = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        timezone = UTC
    return datetime.now(timezone).date().isoformat()


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


def _ensure_explore_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS topic_profiles (
          topic_id        TEXT PRIMARY KEY,
          statement       TEXT NOT NULL,
          profile_json    TEXT NOT NULL,
          schedule        TEXT,
          created_at      TEXT NOT NULL,
          updated_at      TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS refinement_sessions (
          session_id     TEXT PRIMARY KEY,
          statement      TEXT NOT NULL,
          profile_json   TEXT NOT NULL,
          messages_json  TEXT NOT NULL,
          pending_field  TEXT,
          turn_count     INTEGER NOT NULL DEFAULT 0,
          status         TEXT NOT NULL CHECK(status IN ('active','finalized')),
          topic_id       TEXT REFERENCES topic_profiles(topic_id),
          created_at     TEXT NOT NULL,
          updated_at     TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS explorations (
          exploration_id         TEXT PRIMARY KEY,
          topic_id               TEXT NOT NULL REFERENCES topic_profiles(topic_id),
          mode                   TEXT NOT NULL CHECK(mode IN ('show_now','scheduled')),
          source_selection_json  TEXT NOT NULL,
          progress_json          TEXT NOT NULL DEFAULT '{}',
          status                 TEXT NOT NULL CHECK(status IN ('queued','running','complete','failed')),
          brief_ref              TEXT,
          emailed                INTEGER NOT NULL DEFAULT 0,
          started_at             TEXT NOT NULL,
          finished_at            TEXT,
          deleted_at             TEXT,
          delete_after           TEXT,
          purged_at              TEXT
        ) STRICT;

        CREATE TABLE IF NOT EXISTS promoted_sources (
          id          TEXT PRIMARY KEY,
          topic_id    TEXT NOT NULL REFERENCES topic_profiles(topic_id),
          adapter     TEXT NOT NULL,
          ref         TEXT NOT NULL,
          has_feed    INTEGER NOT NULL DEFAULT 0,
          feed_url    TEXT,
          created_at  TEXT NOT NULL
        ) STRICT;

        CREATE INDEX IF NOT EXISTS idx_topic_profiles_updated_at ON topic_profiles(updated_at);
        CREATE INDEX IF NOT EXISTS idx_refinement_sessions_updated_at ON refinement_sessions(updated_at);
        CREATE INDEX IF NOT EXISTS idx_explorations_topic_id ON explorations(topic_id);
        CREATE INDEX IF NOT EXISTS idx_explorations_status ON explorations(status);
        CREATE INDEX IF NOT EXISTS idx_promoted_sources_topic_id ON promoted_sources(topic_id);
        """
    )
    existing_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(explorations)").fetchall()
    }
    if "progress_json" not in existing_columns:
        connection.execute(
            "ALTER TABLE explorations ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}'"
        )
    for column in ("deleted_at", "delete_after", "purged_at"):
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE explorations ADD COLUMN {column} TEXT")
    _ensure_exploration_status_allows_queued(connection)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_explorations_topic_id ON explorations(topic_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_explorations_status ON explorations(status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_explorations_deleted_at ON explorations(deleted_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_explorations_delete_after ON explorations(delete_after)")


def _ensure_exploration_status_allows_queued(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'explorations'"
    ).fetchone()
    table_sql = str(row["sql"] or "") if row else ""
    if "'queued'" in table_sql:
        return
    connection.execute("ALTER TABLE explorations RENAME TO explorations_old")
    connection.execute(
        """
        CREATE TABLE explorations (
          exploration_id         TEXT PRIMARY KEY,
          topic_id               TEXT NOT NULL REFERENCES topic_profiles(topic_id),
          mode                   TEXT NOT NULL CHECK(mode IN ('show_now','scheduled')),
          source_selection_json  TEXT NOT NULL,
          progress_json          TEXT NOT NULL DEFAULT '{}',
          status                 TEXT NOT NULL CHECK(status IN ('queued','running','complete','failed')),
          brief_ref              TEXT,
          emailed                INTEGER NOT NULL DEFAULT 0,
          started_at             TEXT NOT NULL,
          finished_at            TEXT,
          deleted_at             TEXT,
          delete_after           TEXT,
          purged_at              TEXT
        ) STRICT
        """
    )
    connection.execute(
        """
        INSERT INTO explorations (
          exploration_id, topic_id, mode, source_selection_json, progress_json,
          status, brief_ref, emailed, started_at, finished_at, deleted_at,
          delete_after, purged_at
        )
        SELECT
          exploration_id, topic_id, mode, source_selection_json,
          COALESCE(progress_json, '{}'), status, brief_ref, emailed, started_at,
          finished_at, deleted_at, delete_after, purged_at
        FROM explorations_old
        """
    )
    connection.execute("DROP TABLE explorations_old")


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


def upsert_topic_profile(profile: dict[str, Any]) -> dict[str, Any]:
    topic_id = str(profile.get("topic_id") or new_id())
    statement = str(profile.get("statement") or "").strip()
    if not statement:
        raise ValueError("Topic profile statement is required")
    profile = {**profile, "topic_id": topic_id}
    now = utc_now()
    with connect() as connection:
        existing = connection.execute(
            "SELECT created_at FROM topic_profiles WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        created_at = str(existing["created_at"]) if existing else now
        connection.execute(
            """
            INSERT INTO topic_profiles (
              topic_id, statement, profile_json, schedule, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_id) DO UPDATE SET
              statement = excluded.statement,
              profile_json = excluded.profile_json,
              schedule = excluded.schedule,
              updated_at = excluded.updated_at
            """,
            (
                topic_id,
                statement,
                json.dumps(profile, sort_keys=True),
                profile.get("schedule"),
                created_at,
                now,
            ),
        )
    record = get_topic_profile(topic_id)
    if record is None:
        raise RuntimeError("Topic profile was not saved")
    return record


def list_topic_profiles(*, include_deleted: bool = False) -> list[dict[str, Any]]:
    deleted_filter = "" if include_deleted else "WHERE COALESCE(json_extract(profile_json, '$.deleted'), 0) != 1"
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM topic_profiles
            {deleted_filter}
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [_topic_profile_row_to_dict(row) for row in rows]


def list_scheduled_topic_profiles(
    *,
    include_archived: bool = False,
    include_paused: bool = False,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    archived_filter = "" if include_archived else "AND COALESCE(json_extract(profile_json, '$.archived'), 0) != 1"
    paused_filter = "" if include_paused else "AND COALESCE(json_extract(profile_json, '$.status'), 'active') != 'paused'"
    deleted_filter = "" if include_deleted else "AND COALESCE(json_extract(profile_json, '$.deleted'), 0) != 1"
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM topic_profiles
            WHERE schedule IS NOT NULL
              AND TRIM(schedule) != ''
              {archived_filter}
              {paused_filter}
              {deleted_filter}
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [_topic_profile_row_to_dict(row) for row in rows]


def get_topic_profile(topic_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM topic_profiles WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
    return _topic_profile_row_to_dict(row) if row else None


def create_refinement_session(
    *,
    statement: str,
    profile: dict[str, Any],
    messages: list[dict[str, str]],
    pending_field: str | None,
    status: str = "active",
) -> dict[str, Any]:
    now = utc_now()
    session_id = new_id()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO refinement_sessions (
              session_id, statement, profile_json, messages_json, pending_field,
              turn_count, status, topic_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?, ?)
            """,
            (
                session_id,
                statement,
                json.dumps(profile, sort_keys=True),
                json.dumps(messages),
                pending_field,
                status,
                now,
                now,
            ),
        )
    record = get_refinement_session(session_id)
    if record is None:
        raise RuntimeError("Refinement session was not created")
    return record


def get_refinement_session(session_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM refinement_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return _refinement_session_row_to_dict(row) if row else None


def update_refinement_session(
    session_id: str,
    *,
    profile: dict[str, Any],
    messages: list[dict[str, str]],
    pending_field: str | None,
    turn_count: int,
    status: str,
    topic_id: str | None,
) -> dict[str, Any] | None:
    if get_refinement_session(session_id) is None:
        return None
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            UPDATE refinement_sessions
            SET profile_json = ?,
                messages_json = ?,
                pending_field = ?,
                turn_count = ?,
                status = ?,
                topic_id = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (
                json.dumps(profile, sort_keys=True),
                json.dumps(messages),
                pending_field,
                int(turn_count),
                status,
                topic_id,
                now,
                session_id,
            ),
        )
    return get_refinement_session(session_id)


def delete_refinement_session(session_id: str) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM refinement_sessions WHERE session_id = ?",
            (session_id,),
        )
        return bool(cursor.rowcount)


def create_exploration(
    *,
    topic_id: str,
    mode: str,
    source_selection: dict[str, bool],
    status: str = "running",
) -> dict[str, Any]:
    if status not in {"queued", "running", "complete", "failed"}:
        raise ValueError("Unsupported exploration status")
    now = utc_now()
    exploration_id = new_id()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO explorations (
              exploration_id, topic_id, mode, source_selection_json, status,
              progress_json, brief_ref, emailed, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, '{}', NULL, 0, ?, NULL)
            """,
            (
                exploration_id,
                topic_id,
                mode,
                json.dumps(source_selection, sort_keys=True),
                status,
                now,
            ),
        )
    record = get_exploration(exploration_id)
    if record is None:
        raise RuntimeError("Exploration was not created")
    return record


def get_latest_exploration(
    *,
    topic_id: str,
    mode: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    conditions = ["topic_id = ?", "deleted_at IS NULL"]
    values: list[Any] = [topic_id]
    if mode is not None:
        conditions.append("mode = ?")
        values.append(mode)
    if status is not None:
        conditions.append("status = ?")
        values.append(status)
    query = f"""
        SELECT *
        FROM explorations
        WHERE {" AND ".join(conditions)}
        ORDER BY COALESCE(finished_at, started_at) DESC
        LIMIT 1
    """
    with connect() as connection:
        row = connection.execute(query, tuple(values)).fetchone()
    return _exploration_row_to_dict(row) if row else None


def update_exploration_status(
    exploration_id: str,
    *,
    status: str,
    brief_ref: str | None = None,
    emailed: bool | None = None,
) -> dict[str, Any] | None:
    existing = get_exploration(exploration_id)
    if existing is None:
        return None
    finished_at = utc_now() if status in {"complete", "failed"} else existing.get("finished_at")
    with connect() as connection:
        connection.execute(
            """
            UPDATE explorations
            SET status = ?,
                brief_ref = COALESCE(?, brief_ref),
                emailed = COALESCE(?, emailed),
                finished_at = ?
            WHERE exploration_id = ?
            """,
            (
                status,
                brief_ref,
                None if emailed is None else int(emailed),
                finished_at,
                exploration_id,
            ),
        )
    return get_exploration(exploration_id)


def update_exploration_progress(
    exploration_id: str,
    *,
    progress: dict[str, Any],
) -> dict[str, Any] | None:
    if get_exploration(exploration_id) is None:
        return None
    with connect() as connection:
        connection.execute(
            """
            UPDATE explorations
            SET progress_json = ?
            WHERE exploration_id = ?
            """,
            (json.dumps(progress, sort_keys=True), exploration_id),
        )
    return get_exploration(exploration_id)


def claim_next_queued_exploration() -> dict[str, Any] | None:
    now = utc_now()
    with connect() as connection:
        row = connection.execute(
            """
            SELECT exploration_id
            FROM explorations
            WHERE status = 'queued'
              AND deleted_at IS NULL
            ORDER BY started_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        exploration_id = str(row["exploration_id"])
        connection.execute(
            """
            UPDATE explorations
            SET status = 'running',
                started_at = ?,
                finished_at = NULL
            WHERE exploration_id = ? AND status = 'queued'
            """,
            (now, exploration_id),
        )
    return get_exploration(exploration_id)


def requeue_running_explorations() -> int:
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE explorations
            SET status = 'queued',
                finished_at = NULL
            WHERE status = 'running'
              AND deleted_at IS NULL
            """
        )
        return int(cursor.rowcount or 0)


def mark_exploration_emailed(exploration_id: str) -> dict[str, Any] | None:
    if get_exploration(exploration_id) is None:
        return None
    with connect() as connection:
        connection.execute(
            """
            UPDATE explorations
            SET emailed = 1
            WHERE exploration_id = ?
            """,
            (exploration_id,),
        )
    return get_exploration(exploration_id)


def get_exploration(exploration_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM explorations WHERE exploration_id = ?",
            (exploration_id,),
        ).fetchone()
    return _exploration_row_to_dict(row) if row else None


def list_explorations(
    topic_id: str | None = None,
    *,
    limit: int | None = None,
    include_deleted: bool = False,
    only_deleted: bool = False,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    values: list[Any] = []
    if topic_id:
        conditions.append("topic_id = ?")
        values.append(topic_id)
    if only_deleted:
        conditions.append("deleted_at IS NOT NULL")
    elif not include_deleted:
        conditions.append("deleted_at IS NULL")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit_clause = "LIMIT ?" if limit is not None else ""
    if limit is not None:
        values.append(max(1, int(limit)))
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM explorations
            {where}
            ORDER BY started_at DESC
            {limit_clause}
            """,
            tuple(values),
        ).fetchall()
    return [_exploration_row_to_dict(row) for row in rows]


def soft_delete_exploration(
    exploration_id: str,
    *,
    retention_days: int = 7,
) -> dict[str, Any] | None:
    existing = get_exploration(exploration_id)
    if existing is None:
        return None
    now_dt = datetime.now(UTC)
    deleted_at = now_dt.isoformat(timespec="seconds")
    delete_after = (now_dt + timedelta(days=max(1, retention_days))).isoformat(timespec="seconds")
    with connect() as connection:
        connection.execute(
            """
            UPDATE explorations
            SET deleted_at = COALESCE(deleted_at, ?),
                delete_after = COALESCE(delete_after, ?)
            WHERE exploration_id = ?
            """,
            (deleted_at, delete_after, exploration_id),
        )
        _hide_standalone_topic_if_fully_deleted(connection, str(existing["topic_id"]))
    return get_exploration(exploration_id)


def restore_exploration(exploration_id: str) -> dict[str, Any] | None:
    existing = get_exploration(exploration_id)
    if existing is None or not existing.get("deleted_at"):
        return existing
    now = utc_now()
    if str(existing.get("delete_after") or "") <= now:
        return None
    with connect() as connection:
        connection.execute(
            """
            UPDATE explorations
            SET deleted_at = NULL,
                delete_after = NULL,
                purged_at = NULL
            WHERE exploration_id = ?
            """,
            (exploration_id,),
        )
        _restore_topic_after_exploration_undo(connection, str(existing["topic_id"]))
    return get_exploration(exploration_id)


def purge_expired_deleted_explorations() -> int:
    now = utc_now()
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM explorations
            WHERE deleted_at IS NOT NULL
              AND delete_after IS NOT NULL
              AND delete_after <= ?
            """,
            (now,),
        ).fetchall()
        purged = 0
        for row in rows:
            record = _exploration_row_to_dict(row)
            topic_id = str(record.get("topic_id") or "")
            _delete_exploration_artifacts(record)
            connection.execute(
                "DELETE FROM explorations WHERE exploration_id = ?",
                (record["exploration_id"],),
            )
            _delete_standalone_topic_if_orphaned(connection, topic_id)
            purged += 1
    return purged


def reset_exploration_for_rebuild(
    exploration_id: str,
    *,
    source_selection: dict[str, bool],
    progress: dict[str, Any],
) -> dict[str, Any] | None:
    existing = get_exploration(exploration_id)
    if existing is None or existing.get("deleted_at"):
        return None
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            UPDATE explorations
            SET status = 'queued',
                source_selection_json = ?,
                progress_json = ?,
                brief_ref = NULL,
                emailed = 0,
                started_at = ?,
                finished_at = NULL
            WHERE exploration_id = ?
            """,
            (
                json.dumps(source_selection, sort_keys=True),
                json.dumps(progress, sort_keys=True),
                now,
                exploration_id,
            ),
        )
    return get_exploration(exploration_id)


def clear_expired_exploration_briefs(*, before_started_at: str) -> int:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT exploration_id, brief_ref
            FROM explorations
            WHERE mode = 'show_now'
              AND brief_ref IS NOT NULL
              AND started_at < ?
            """,
            (before_started_at,),
        ).fetchall()
        cleared = 0
        for row in rows:
            brief_ref = str(row["brief_ref"] or "")
            if brief_ref:
                try:
                    Path(brief_ref).unlink(missing_ok=True)
                except OSError:
                    pass
            connection.execute(
                "UPDATE explorations SET brief_ref = NULL WHERE exploration_id = ?",
                (row["exploration_id"],),
            )
            cleared += 1
    return cleared


def _delete_exploration_artifacts(record: dict[str, Any]) -> None:
    settings = get_settings()
    output_dir = (settings.data_dir / "digest-output").resolve()
    exploration_id = str(record.get("exploration_id") or "").strip()
    paths: set[Path] = set()
    brief_ref = str(record.get("brief_ref") or "").strip()
    if brief_ref:
        paths.add(Path(brief_ref))
    if exploration_id:
        paths.update(output_dir.glob(f"exploration-{exploration_id}.*"))
    for path in paths:
        _unlink_if_under(path, output_dir)


def _unlink_if_under(path: Path, parent: Path) -> None:
    try:
        resolved_path = path.resolve()
    except OSError:
        return
    if parent not in (resolved_path, *resolved_path.parents):
        return
    try:
        resolved_path.unlink(missing_ok=True)
    except OSError:
        pass


def _hide_standalone_topic_if_fully_deleted(connection: sqlite3.Connection, topic_id: str) -> None:
    if not topic_id:
        return
    topic = connection.execute(
        "SELECT schedule, profile_json FROM topic_profiles WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()
    if topic is None or str(topic["schedule"] or "").strip():
        return
    active_count = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM explorations WHERE topic_id = ? AND deleted_at IS NULL",
            (topic_id,),
        ).fetchone()["count"]
    )
    if active_count:
        return
    _set_topic_deleted(connection, topic_id, topic, deleted=True)


def _restore_topic_after_exploration_undo(connection: sqlite3.Connection, topic_id: str) -> None:
    if not topic_id:
        return
    topic = connection.execute(
        "SELECT schedule, profile_json FROM topic_profiles WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()
    if topic is None or str(topic["schedule"] or "").strip():
        return
    _set_topic_deleted(connection, topic_id, topic, deleted=False)


def _delete_standalone_topic_if_orphaned(connection: sqlite3.Connection, topic_id: str) -> None:
    if not topic_id:
        return
    topic = connection.execute(
        "SELECT schedule, profile_json FROM topic_profiles WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()
    if topic is None or str(topic["schedule"] or "").strip():
        return
    remaining_count = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM explorations WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()["count"]
    )
    if remaining_count:
        return
    connection.execute("DELETE FROM promoted_sources WHERE topic_id = ?", (topic_id,))
    connection.execute("DELETE FROM topic_profiles WHERE topic_id = ?", (topic_id,))


def _set_topic_deleted(connection: sqlite3.Connection, topic_id: str, topic: sqlite3.Row, *, deleted: bool) -> None:
    try:
        profile = json.loads(str(topic["profile_json"] or "{}"))
    except json.JSONDecodeError:
        profile = {}
    if not isinstance(profile, dict):
        profile = {}
    profile["deleted"] = deleted
    profile["archived"] = deleted
    if deleted:
        profile["status"] = "deleted"
    elif profile.get("status") == "deleted":
        profile["status"] = "active"
    now = utc_now()
    connection.execute(
        """
        UPDATE topic_profiles
        SET profile_json = ?,
            updated_at = ?
        WHERE topic_id = ?
        """,
        (json.dumps(profile, sort_keys=True), now, topic_id),
    )


def add_promoted_source(
    *,
    topic_id: str,
    adapter: str,
    ref: str,
    has_feed: bool = False,
    feed_url: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    promoted_id = new_id()
    with connect() as connection:
        existing = connection.execute(
            """
            SELECT * FROM promoted_sources
            WHERE topic_id = ? AND adapter = ? AND ref = ?
            """,
            (
                topic_id,
                str(adapter).strip(),
                str(ref).strip(),
            ),
        ).fetchone()
        if existing is not None:
            return _promoted_source_row_to_dict(existing)
        connection.execute(
            """
            INSERT INTO promoted_sources (
              id, topic_id, adapter, ref, has_feed, feed_url, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (promoted_id, topic_id, adapter, ref, int(has_feed), feed_url, now),
        )
    record = get_promoted_source(promoted_id)
    if record is None:
        raise RuntimeError("Promoted source was not created")
    return record


def get_promoted_source(promoted_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM promoted_sources WHERE id = ?",
            (promoted_id,),
        ).fetchone()
    return _promoted_source_row_to_dict(row) if row else None


def _topic_profile_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    try:
        profile = json.loads(record.pop("profile_json") or "{}")
    except json.JSONDecodeError:
        profile = {}
    record["profile"] = _hydrate_promoted_sources(
        record.get("topic_id") or "",
        profile if isinstance(profile, dict) else {},
    )
    return record


def _refinement_session_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    try:
        profile = json.loads(record.pop("profile_json") or "{}")
    except json.JSONDecodeError:
        profile = {}
    try:
        messages = json.loads(record.pop("messages_json") or "[]")
    except json.JSONDecodeError:
        messages = []
    record["profile"] = profile if isinstance(profile, dict) else {}
    record["messages"] = messages if isinstance(messages, list) else []
    return record


def _exploration_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    try:
        source_selection = json.loads(record.pop("source_selection_json") or "{}")
    except json.JSONDecodeError:
        source_selection = {}
    record["source_selection"] = source_selection if isinstance(source_selection, dict) else {}
    try:
        progress = json.loads(record.pop("progress_json") or "{}")
    except json.JSONDecodeError:
        progress = {}
    record["progress"] = progress if isinstance(progress, dict) else {}
    record["emailed"] = bool(record.get("emailed"))
    return record


def _hydrate_promoted_sources(topic_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    profile_dict = dict(profile)
    try:
        rows = list_promoted_sources(topic_id)
    except Exception:
        rows = []
    if rows:
        profile_dict["promoted_sources"] = [
            {
                "adapter": row.get("adapter"),
                "ref": row.get("ref"),
                "has_feed": bool(row.get("has_feed")),
                "feed_url": row.get("feed_url"),
            }
            for row in rows
            if isinstance(row, dict) and row.get("adapter") and row.get("ref")
        ]
    else:
        existing = profile_dict.get("promoted_sources")
        if existing is None:
            profile_dict["promoted_sources"] = []
        elif not isinstance(existing, list):
            profile_dict["promoted_sources"] = []
    return profile_dict


def _promoted_source_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["has_feed"] = bool(record.get("has_feed"))
    return record


def list_promoted_sources(topic_id: str) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM promoted_sources WHERE topic_id = ? ORDER BY created_at DESC, id DESC",
            (topic_id,),
        ).fetchall()
    return [_promoted_source_row_to_dict(row) for row in rows]


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


def build_digest_stats(
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
        "model_success_count": token_summary["model_success_count"],
        "model_failure_count": token_summary["model_failure_count"],
        "completion_unavailable_count": token_summary["completion_unavailable_count"],
        "model_usage": inference_model_usage_summary(inference_run_id),
        "processing_seconds": _nullable_float(duration_seconds),
        "stage_seconds": _normalize_stage_seconds(stage_seconds),
    }


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
    return build_digest_stats(
        configured_source_count=configured_source_count,
        newsletter_count=newsletter_count,
        link_count=link_count,
        podcast_episode_count=podcast_episode_count,
        article_results=article_results,
        duration_seconds=duration_seconds,
        inference_run_id=inference_run_id,
        stage_seconds=stage_seconds,
    )


def _empty_token_summary() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "model_call_count": 0,
        "model_success_count": 0,
        "model_failure_count": 0,
        "completion_unavailable_count": 0,
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
              COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS model_success_count,
              COALESCE(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END), 0) AS model_failure_count,
              COALESCE(SUM(CASE WHEN completion_tokens IS NULL THEN 1 ELSE 0 END), 0) AS completion_unavailable_count,
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
        "model_success_count": int(row["model_success_count"] or 0) if row else 0,
        "model_failure_count": int(row["model_failure_count"] or 0) if row else 0,
        "completion_unavailable_count": int(row["completion_unavailable_count"] or 0) if row else 0,
    }


def inference_model_usage_summary(inference_run_id: str | None) -> list[dict[str, Any]]:
    if not inference_run_id:
        return []
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
              model,
              mode,
              COUNT(*) AS call_count,
              COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS success_count,
              COALESCE(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END), 0) AS failure_count
            FROM inference_metrics
            WHERE run_id = ?
            GROUP BY model, mode
            ORDER BY call_count DESC, model ASC, mode ASC
            """,
            (inference_run_id,),
        ).fetchall()
    return [
        {
            "model": str(row["model"] or "unknown"),
            "mode": str(row["mode"] or "single"),
            "call_count": int(row["call_count"] or 0),
            "success_count": int(row["success_count"] or 0),
            "failure_count": int(row["failure_count"] or 0),
        }
        for row in rows
    ]


def render_ingested_issue(
    title: str,
    snapshot: str,
    payloads: list[NormalizedPayload],
    article_results: list[ArticleFetchResult] | None,
    lookback_hours: int,
    generated_at: str | None = None,
    issue_id: str | None = None,
    digest_stats: dict[str, Any] | None = None,
    newsletter_payloads: list[NormalizedPayload] | None = None,
) -> str:
    article_results = article_results or []
    body_payloads = (
        list(newsletter_payloads)
        if newsletter_payloads is not None
        else [payload for payload in payloads if payload.source_type == "gmail"]
    )
    fetched_articles = [result for result in article_results if result.fetched and result.tier != "dropped"]
    story_articles = [result for result in fetched_articles if not _is_media_result(result)]
    media_articles = [result for result in fetched_articles if _is_media_result(result)]
    lead_article = next((result for result in story_articles if result.tier == "lead"), None)
    if lead_article is None and story_articles:
        lead_article = story_articles[0]
    ranked_articles = [
        result
        for result in story_articles
        if result is not lead_article and result.tier != "lower_confidence"
    ]
    lower_confidence_articles = [
        result
        for result in story_articles
        if result is not lead_article and result.tier == "lower_confidence"
    ]

    newsletter_items = [_render_newsletter_item(payload) for payload in body_payloads]
    newsletter_html = "\n".join(item for item in newsletter_items if item)
    effective_stats = digest_stats or _build_digest_stats(
        configured_source_count=0,
        newsletter_count=len(body_payloads),
        link_count=sum(1 for payload in payloads if payload.source_type == "gmail_link"),
        podcast_episode_count=sum(1 for payload in payloads if payload.source_type == "podcast_episode"),
        article_results=article_results,
        duration_seconds=None,
        inference_run_id=None,
        stage_seconds=None,
    )
    lead_html = _render_lead_story(lead_article, issue_id=issue_id) if lead_article else ""
    image_strip_html = _render_image_strip([result for result in [lead_article, *ranked_articles, *media_articles] if result])
    ranked_html = "\n".join(
        _render_ranked_story(result, index=index, issue_id=issue_id)
        for index, result in enumerate(ranked_articles, start=1)
    )
    media_html = "\n".join(_render_media_card(result, issue_id=issue_id) for result in media_articles)
    lower_html = "\n".join(
        _render_lower_confidence_story(result, index=index, issue_id=issue_id)
        for index, result in enumerate(lower_confidence_articles, start=len(ranked_articles) + 1)
    )
    sidebar_html = _render_brief_sidebar(
        stats=effective_stats,
        newsletter_html=newsletter_html,
        newsletter_count=len(newsletter_items),
        article_count=len(story_articles),
        media_count=len(media_articles),
        lookback_hours=lookback_hours,
    )
    empty_state = ""
    if not payloads:
        empty_state = """
        <section class="empty">
          <strong>No newsletter items were found.</strong>
          Check the source allowlist, Gmail labels, or the digest lookback window.
        </section>
        """
    ranked_empty = ""
    if not lead_html and not ranked_html and not media_html:
        ranked_empty = '<p class="meta">No article pages were fetched yet.</p>'
    media_section = ""
    if media_html:
        media_section = f"""
        <section class="media-section" aria-labelledby="media-heading">
          <div class="section-kicker">Media</div>
          <h2 id="media-heading">Watch & listen</h2>
          <div class="media-grid">{media_html}</div>
        </section>
        """
    lower_section = ""
    if lower_html:
        lower_section = f"""
        <section class="lower-confidence" aria-labelledby="lower-confidence-heading">
          <div class="section-kicker">Lower confidence</div>
          <h2 id="lower-confidence-heading">Worth a skim</h2>
          <div class="low-conf-list">{lower_html}</div>
        </section>
        """
    generated_value = generated_at or utc_now()
    masthead_meta = _render_masthead_meta(generated_value, lookback_hours, effective_stats)
    generated_footer = _render_generated_footer(generated_value)
    feedback_script = _render_feedback_script(issue_id)
    podcast_script = _render_podcast_modal_script()

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700;800&family=Playfair+Display:wght@700;800;900&display=swap" rel="stylesheet" />
  <style>
    :root {{
      color: #221d18;
      background: #f4efe6;
      --paper: #fffaf1;
      --paper-deep: #f4efe6;
      --ink: #221d18;
      --muted: #796f65;
      --line: #d8cbbc;
      --accent: #b53a32;
      --accent-dark: #84251f;
      --sidebar: #ebe3d5;
      --shadow: 0 24px 80px rgba(48, 35, 24, .13);
      --display: 'Playfair Display', Georgia, 'Times New Roman', serif;
      --body: 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      --mono: 'DM Mono', 'SFMono-Regular', Consolas, monospace;
    }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
    body {{ margin: 0; font-family: var(--body); color: var(--ink); background: radial-gradient(circle at top left, #fffaf1 0, #f4efe6 38%, #eee4d5 100%); }}
    .brief-shell {{ width: min(1180px, 100%); margin: 0 auto; padding: 34px 24px 64px; }}
    .brief-masthead {{ display: flex; justify-content: space-between; gap: 18px; align-items: center; border-bottom: 1px solid var(--ink); padding-bottom: 16px; margin-bottom: 28px; }}
    .masthead-brand {{ font-family: var(--display); font-size: clamp(2rem, 5vw, 4.2rem); font-weight: 900; line-height: .9; letter-spacing: 0; }}
    .masthead-meta, .dateline, .section-kicker, .meta {{ font: 700 .74rem/1.35 var(--mono); color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
    .masthead-meta {{ max-width: 48ch; text-align: right; }}
    .brief-header {{ display: grid; gap: 14px; max-width: 920px; margin-bottom: 30px; }}
    h1 {{ font-family: var(--display); font-size: 2.85rem; font-weight: 900; line-height: .98; margin: 0; letter-spacing: 0; }}
    .brief-header h1 {{ display: -webkit-box; max-height: 11.18rem; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 4; overflow-wrap: break-word; word-break: normal; hyphens: auto; }}
    h2 {{ font-family: var(--display); font-size: clamp(2rem, 4vw, 3.2rem); line-height: .95; margin: 0 0 18px; letter-spacing: 0; }}
    h3 {{ font-family: var(--display); font-size: clamp(1.35rem, 3vw, 2.05rem); line-height: 1.05; margin: 0; letter-spacing: 0; }}
    h1, h2, h3, h4, p, a, .meta, .story-title, .side-value {{ overflow-wrap: anywhere; }}
    a {{ color: inherit; text-decoration-thickness: 1px; text-underline-offset: 4px; }}
    img, video, iframe, table {{ max-width: 100%; }}
    .snapshot {{ font-size: clamp(1.12rem, 2vw, 1.42rem); line-height: 1.45; margin: 0; color: #3f382f; max-width: 820px; }}
    .brief-body {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 340px); gap: 34px; align-items: start; }}
    .story-column, .brief-sidebar, .lead-block, .story-row, .media-card, .low-conf-row, .newsletter {{ min-width: 0; }}
    .story-column {{ display: grid; gap: 30px; }}
    .img-strip {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .strip-frame, .story-thumb, .media-thumb {{ position: relative; overflow: hidden; background: #e5dacb; border: 1px solid var(--line); }}
    .strip-frame {{ aspect-ratio: 4 / 3; }}
    .strip-frame img, .story-thumb img, .media-thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .fallback-art {{ display: grid; place-items: center; min-height: 100%; color: var(--accent); background: linear-gradient(135deg, #fbf3e5, #e9ddcb); }}
    .fallback-art svg {{ width: 34px; height: 34px; }}
    .lead-block {{ display: grid; grid-template-columns: 10px minmax(0, 1fr); gap: 18px; padding: 24px 0 28px; border-top: 1px solid var(--ink); border-bottom: 1px solid var(--ink); }}
    .lead-bar {{ background: var(--accent); border-radius: 999px; }}
    .lead-content {{ display: grid; gap: 13px; }}
    .lead-title {{ font-family: var(--display); font-size: clamp(2.25rem, 5vw, 4.4rem); line-height: .9; font-weight: 900; }}
    .lead-summary {{ font-size: 1.14rem; line-height: 1.55; margin: 0; color: #3f382f; }}
    .story-meta, .chip-row, .keywords, .feedback-controls {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    .source-type, .score {{ display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 999px; padding: 4px 8px; font: 700 .68rem/1 var(--mono); color: var(--muted); text-transform: uppercase; letter-spacing: .04em; background: rgba(255, 250, 241, .72); }}
    .source-type.youtube, .source-type.podcast, .source-type.foreign-media, .translation-badge {{ color: var(--accent-dark); border-color: rgba(181, 58, 50, .34); background: rgba(181, 58, 50, .08); }}
    .translation-original {{ margin-top: 10px; color: var(--muted); font-size: .88rem; }}
    .translation-original summary {{ cursor: pointer; font-weight: 800; color: var(--accent-dark); }}
    .translation-original p {{ margin: 8px 0 0; line-height: 1.45; }}
    .keywords {{ margin-top: 10px; font: 500 .72rem/1.5 var(--mono); color: var(--muted); }}
    .keywords span {{ border-bottom: 1px dotted var(--line); }}
    .ranked-section, .media-section, .lower-confidence {{ border-top: 1px solid var(--line); padding-top: 20px; }}
    .story-list {{ display: grid; gap: 0; }}
    .story-row {{ display: grid; grid-template-columns: 64px minmax(0, 1fr) 132px; gap: 18px; padding: 22px 0; border-bottom: 1px solid var(--line); align-items: start; }}
    .story-num {{ font-family: var(--display); font-size: 2.45rem; line-height: .9; color: var(--accent); font-weight: 900; }}
    .story-copy {{ display: grid; gap: 9px; }}
    .story-title {{ font-family: var(--display); font-size: clamp(1.42rem, 2.5vw, 2.05rem); line-height: 1.02; font-weight: 800; }}
    .story-summary, .low-conf-row p, .newsletter p, .youtube-summary p, .podcast-transcript p {{ font-size: .98rem; line-height: 1.58; margin: 0; color: #4a4138; }}
    .story-thumb {{ aspect-ratio: 1; border-radius: 2px; }}
    .media-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .media-card {{ display: grid; gap: 12px; padding: 14px; background: rgba(255, 250, 241, .68); border: 1px solid var(--line); box-shadow: 0 10px 34px rgba(48, 35, 24, .06); }}
    .media-thumb {{ aspect-ratio: 16 / 9; }}
    .media-title {{ font-family: var(--display); font-size: 1.42rem; line-height: 1.04; font-weight: 800; }}
    .media-cta {{ justify-self: start; display: inline-flex; align-items: center; gap: 7px; border: 1px solid var(--accent); border-radius: 999px; color: var(--accent-dark); padding: 8px 12px; font: 800 .76rem/1 var(--body); text-decoration: none; }}
    .low-conf-list {{ display: grid; gap: 0; }}
    .low-conf-row {{ display: grid; grid-template-columns: 50px minmax(0, 1fr); gap: 14px; padding: 16px 0; border-bottom: 1px solid var(--line); opacity: .78; }}
    .low-conf-row .story-num {{ font-size: 1.7rem; color: var(--muted); }}
    .brief-sidebar {{ position: sticky; top: 22px; display: grid; gap: 16px; }}
    .side-panel {{ background: var(--sidebar); border: 1px solid var(--line); padding: 18px; box-shadow: var(--shadow); }}
    .side-panel h2 {{ font-size: 1.55rem; margin-bottom: 14px; }}
    .side-stats {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .side-stat {{ border-top: 1px solid rgba(34, 29, 24, .22); padding-top: 9px; }}
    .side-stat span {{ display: block; font: 700 .66rem/1.25 var(--mono); color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
    .side-stat strong {{ display: block; margin-top: 3px; font-family: var(--display); font-size: 1.45rem; line-height: 1; }}
    .side-note {{ margin-top: 16px; border-top: 1px solid rgba(34, 29, 24, .22); padding-top: 13px; }}
    .side-note h3 {{ margin: 0 0 7px; font: 800 .74rem/1.25 var(--mono); color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
    .side-note p {{ margin: 0; color: #4f463d; font-size: .86rem; line-height: 1.45; }}
    .stage-list {{ margin: 10px 0 0; padding-left: 18px; color: var(--muted); font: 600 .78rem/1.6 var(--body); }}
    details.source-notes {{ margin-top: 18px; border-top: 1px solid rgba(34, 29, 24, .26); padding-top: 14px; }}
    details.source-notes summary {{ cursor: pointer; font: 800 .74rem/1.25 var(--mono); color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
    .newsletter {{ padding: 14px 0; border-bottom: 1px solid rgba(34, 29, 24, .18); }}
    .newsletter h3 {{ font-size: 1.1rem; line-height: 1.1; margin-top: 6px; }}
    .feedback-controls {{ margin-top: 12px; }}
    .feedback-controls button {{ border: 1px solid var(--line); border-radius: 999px; background: var(--paper); color: var(--accent-dark); padding: 7px 11px; font: 800 .72rem/1 var(--body); cursor: pointer; }}
    .feedback-controls button:hover {{ background: #efe3d1; }}
    .feedback-controls[data-feedback='sent'] button {{ opacity: .55; }}
    .feedback-state {{ color: var(--muted); font: 700 .72rem var(--mono); text-transform: uppercase; }}
    .podcast-modal-link, .youtube-modal-link {{ color: inherit; }}
    .podcast-modal {{ position: fixed; inset: 0; z-index: 20; display: none; place-items: center; padding: 24px; background: rgba(34, 29, 24, .62); }}
    .podcast-modal:target {{ display: grid; }}
    .podcast-panel {{ width: min(920px, 100%); max-height: min(86vh, 980px); overflow: auto; background: var(--paper); border: 1px solid var(--ink); box-shadow: 0 24px 90px rgba(34, 29, 24, .34); padding: 24px; }}
    .podcast-close {{ float: right; border: 1px solid var(--ink); border-radius: 999px; background: var(--ink); color: var(--paper); padding: 9px 13px; font: 800 .72rem/1 var(--body); cursor: pointer; text-decoration: none; }}
    .podcast-brand {{ display: grid; grid-template-columns: 132px minmax(0, 1fr); gap: 18px; align-items: center; margin: 14px 0 20px; }}
    .podcast-art {{ width: 132px; aspect-ratio: 1; object-fit: cover; border: 1px solid var(--line); background: #e5dacb; }}
    .podcast-art.fallback {{ display: grid; place-items: center; font-family: var(--display); font-weight: 900; font-size: 2rem; color: var(--accent); }}
    .podcast-panel h3 {{ font-size: clamp(1.8rem, 4vw, 3.1rem); line-height: .95; margin: 0 0 10px; }}
    .podcast-actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0 18px; font: 800 .75rem var(--body); text-transform: uppercase; }}
    .podcast-actions a {{ color: var(--accent-dark); }}
    .podcast-player {{ width: 100%; margin: 4px 0 20px; }}
    .youtube-panel {{ width: min(1040px, 100%); }}
    .youtube-player {{ width: 100%; aspect-ratio: 16 / 9; border: 1px solid var(--ink); background: var(--ink); margin: 8px 0 20px; }}
    .youtube-summary, .podcast-transcript {{ border-top: 1px solid var(--line); padding-top: 16px; margin-top: 16px; }}
    .youtube-summary h4, .podcast-transcript h4 {{ margin: 0 0 10px; font: 800 .78rem/1.2 var(--mono); color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
    .foreign-tabs {{ display: flex; gap: 8px; margin: 14px 0; flex-wrap: wrap; }}
    .foreign-tabs button {{ border: 1px solid var(--line); border-radius: 999px; background: var(--paper); padding: 8px 12px; font: 800 .74rem/1 var(--body); cursor: pointer; }}
    .foreign-tabs button.active {{ background: var(--ink); color: var(--paper); border-color: var(--ink); }}
    .foreign-view[hidden] {{ display: none; }}
    .foreign-view {{ border-top: 1px solid var(--line); padding-top: 16px; }}
    .foreign-status, .foreign-notice {{ color: var(--muted); font: 700 .82rem/1.45 var(--body); }}
    .foreign-body p {{ margin: 0 0 12px; font-size: 1rem; line-height: 1.62; color: #3f382f; }}
    body.modal-open {{ overflow: hidden; }}
    .empty {{ margin-top: 32px; padding: 24px; border: 1px dashed #b9ae9d; font: 1rem var(--body); background: var(--paper); }}
    .issue-footer {{ margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--line); font: 700 .76rem var(--mono); color: var(--muted); text-transform: uppercase; }}
    @media (max-width: 860px) {{
      .brief-shell {{ padding: 26px 16px 48px; }}
      .brief-header h1 {{ font-size: 2.35rem; line-height: 1; max-height: 9.4rem; }}
      .brief-masthead {{ align-items: flex-start; flex-direction: column; }}
      .masthead-meta {{ max-width: none; text-align: left; }}
      .brief-body, .media-grid, .podcast-brand {{ grid-template-columns: 1fr; }}
      .brief-sidebar {{ position: static; }}
      .story-row {{ grid-template-columns: 48px minmax(0, 1fr); }}
      .story-thumb {{ display: none; }}
      .img-strip {{ grid-template-columns: 1fr; }}
      .strip-frame {{ aspect-ratio: 16 / 9; }}
      .podcast-panel {{ max-height: 90vh; }}
    }}
    @media (max-width: 480px) {{
      .brief-shell {{ padding-inline: 12px; }}
      .brief-header h1 {{ font-size: 1.9rem; line-height: 1; max-height: 7.6rem; }}
      .lead-block {{ grid-template-columns: 7px minmax(0, 1fr); gap: 12px; }}
      .side-stats {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main class="brief-shell">
    <header class="brief-masthead">
      <div class="masthead-brand">Morning Dispatch</div>
      <div class="masthead-meta">{masthead_meta}</div>
    </header>
    <section class="brief-header">
      <div class="dateline">Editorial brief</div>
      <h1>{escape(title)}</h1>
      <p class="snapshot">{escape(snapshot)}</p>
    </section>
    {empty_state}
    <div class="brief-body">
      <div class="story-column">
        {image_strip_html}
        {lead_html}
        <section class="ranked-section" aria-labelledby="ranked-heading">
          <div class="section-kicker">Ranked stories</div>
          <h2 id="ranked-heading">Ranked stories</h2>
          <div class="story-list">{ranked_html or ranked_empty or '<p class="meta">No additional ranked stories.</p>'}</div>
        </section>
        {media_section}
        {lower_section}
      </div>
      {sidebar_html}
    </div>
    {generated_footer}
	  </main>
	  {feedback_script}
	  {podcast_script}
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
    h1 {{ font-size: 2.6rem; line-height: 1; margin: 0; letter-spacing: 0; display: -webkit-box; max-height: 10.4rem; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 4; overflow-wrap: break-word; word-break: normal; hyphens: auto; }}
    h1, p {{ overflow-wrap: anywhere; }}
    .date {{ margin-top: 12px; font: 600 0.8rem Arial, sans-serif; text-transform: uppercase; }}
    .snapshot {{ font-size: 1.3rem; line-height: 1.5; max-width: 720px; }}
    .empty {{ margin-top: 32px; padding-top: 24px; border-top: 1px solid #c8bfae; font: 1rem Arial, sans-serif; }}
    .issue-footer {{ margin-top: 36px; padding-top: 16px; border-top: 1px solid #d4cbbd; font: 700 .76rem Arial, sans-serif; color: #5f675f; text-transform: uppercase; }}
    @media (max-width: 640px) {{
      h1 {{ font-size: 1.9rem; line-height: 1; max-height: 7.6rem; }}
    }}
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


def _is_media_result(result: ArticleFetchResult) -> bool:
    return result.payload.source_type in {"podcast_episode", "youtube_video"} or result.content_type in {"podcast", "video"}


def _result_metadata(result: ArticleFetchResult) -> dict[str, Any]:
    return {**(result.payload.metadata or {}), **(result.metadata or {})}


def _result_url(result: ArticleFetchResult) -> str:
    return result.final_url or result.original_url or result.canonical_url or "#"


def _result_image_url(result: ArticleFetchResult) -> str | None:
    metadata = _result_metadata(result)
    for key in ("image_url", "thumbnail_url"):
        image_url = _safe_web_url(metadata.get(key))
        if image_url:
            return image_url
    if result.payload.source_type == "youtube_video":
        video_id = _youtube_video_id(metadata.get("video_id"), _result_url(result))
        if video_id:
            return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    return None


def _youtube_video_id(raw_video_id: Any, youtube_url: str) -> str:
    video_id = str(raw_video_id or "").strip()
    if not video_id and youtube_url:
        parsed = urlparse(youtube_url)
        hostname = parsed.hostname or ""
        if "youtu.be" in hostname:
            video_id = parsed.path.strip("/")
        elif "youtube" in hostname:
            query = dict(parse_qsl(parsed.query, keep_blank_values=False))
            video_id = str(query.get("v") or "").strip()
    return video_id if re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id) else ""


def _source_label(result: ArticleFetchResult) -> str:
    source_type = result.payload.source_type
    if source_type == "youtube_video":
        return "YouTube"
    if source_type == "podcast_episode":
        return "Podcast"
    if source_type == "reddit_thread":
        return "Reddit"
    if source_type == "collection_chunk":
        return "Collection"
    if source_type == "market_snapshot":
        return "Markets"
    if source_type == "foreign_web":
        return "Foreign Media"
    return "Web"


def _source_class(result: ArticleFetchResult) -> str:
    return _source_label(result).lower().replace(" ", "-")


def _meta_line_for_result(result: ArticleFetchResult) -> str:
    url = _result_url(result)
    domain = result.domain or _domain(url) or _source_label(result).lower()
    source = result.payload.source_name or _source_label(result)
    translation = _translation_metadata(result)
    if translation.get("translated"):
        source = _story_title(result) or source
    published = _format_article_date(result.payload.published_at)
    parts = [domain]
    if published:
        parts.append(published)
    parts.append(f"via {source}")
    return " · ".join(part for part in parts if part)


def _score_badge(result: ArticleFetchResult) -> str:
    if result.relevance_score is None:
        return ""
    return f'<span class="score">{int(result.relevance_score * 100)}%</span>'


def _source_badge(result: ArticleFetchResult) -> str:
    label = _source_label(result)
    return f'<span class="source-type {_source_class(result)}">{escape(label)}</span>{_translation_badge_html(result)}'


def _translation_metadata(result: ArticleFetchResult) -> dict[str, Any]:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    payload_metadata = result.payload.metadata if isinstance(result.payload.metadata, dict) else {}
    translation = metadata.get("translation") or payload_metadata.get("translation")
    return dict(translation) if isinstance(translation, dict) else {}


def _translation_badge_html(result: ArticleFetchResult) -> str:
    translation = _translation_metadata(result)
    source_language = str(translation.get("source_language") or (result.payload.metadata or {}).get("source_language") or "").strip()
    if not source_language:
        return ""
    if translation and not translation.get("translated"):
        label = f"{source_language.upper()} translation unavailable"
    else:
        label = f"{source_language.upper()} -> EN"
    return f'<span class="source-type translation-badge">{escape(label)}</span>'


def _translation_original_html(result: ArticleFetchResult) -> str:
    translation = _translation_metadata(result)
    original_title = str(translation.get("original_title") or (result.payload.metadata or {}).get("original_search_title") or "").strip()
    original_summary = str(translation.get("original_summary") or (result.payload.metadata or {}).get("original_search_summary") or "").strip()
    if not original_title and not original_summary:
        return ""
    source_language_name = str(translation.get("source_language_name") or (result.payload.metadata or {}).get("source_language_name") or "original").strip()
    title_html = f"<p><strong>Title:</strong> {escape(original_title)}</p>" if original_title else ""
    summary_html = f"<p><strong>Summary:</strong> {escape(original_summary)}</p>" if original_summary else ""
    return (
        f'<details class="translation-original">'
        f"<summary>Original {escape(source_language_name)} text</summary>"
        f"{title_html}{summary_html}"
        f"</details>"
    )


def _keyword_html(result: ArticleFetchResult) -> str:
    keywords = [keyword for keyword in result.keywords[:5] if keyword]
    if not keywords:
        return ""
    return '<div class="keywords">' + " ".join(f"<span>{escape(keyword)}</span>" for keyword in keywords) + "</div>"


def _story_summary(result: ArticleFetchResult) -> str:
    return _clean_newsletter_text(result.editor_summary or result.excerpt or result.text)


def _story_title(result: ArticleFetchResult) -> str:
    return _clean_newsletter_text(result.title) or result.title or _result_url(result)


def _story_link_parts(result: ArticleFetchResult, *, issue_id: str | None) -> tuple[str, str, str, str, str]:
    url = _result_url(result)
    if not _supports_foreign_article_modal(result, issue_id=issue_id):
        return url, ' target="_blank" rel="noreferrer"', "", "", ""
    modal_id = _foreign_article_modal_id(result)
    attributes = _foreign_article_attributes(result, modal_id=modal_id)
    return f"#{modal_id}", "", ' class="foreign-article-link"', attributes, _render_foreign_article_modal(result, modal_id, issue_id)


def _supports_foreign_article_modal(result: ArticleFetchResult, *, issue_id: str | None) -> bool:
    if not issue_id:
        return False
    translation = _translation_metadata(result)
    payload_metadata = result.payload.metadata or {}
    source_language = str(translation.get("source_language") or payload_metadata.get("source_language") or "").strip()
    return bool(source_language and _result_url(result).startswith(("http://", "https://")))


def _foreign_article_attributes(result: ArticleFetchResult, *, modal_id: str) -> str:
    translation = _translation_metadata(result)
    payload_metadata = result.payload.metadata or {}
    values = {
        "foreign-article-target": modal_id,
        "foreign-url": _result_url(result),
        "foreign-title": _story_title(result),
        "foreign-summary": _story_summary(result),
        "foreign-source-language": str(translation.get("source_language") or payload_metadata.get("source_language") or ""),
        "foreign-source-language-name": str(translation.get("source_language_name") or payload_metadata.get("source_language_name") or ""),
        "foreign-original-title": str(translation.get("original_title") or payload_metadata.get("original_search_title") or ""),
        "foreign-original-summary": str(translation.get("original_summary") or payload_metadata.get("original_search_summary") or ""),
    }
    return "".join(
        f' data-{escape(key, quote=True)}="{escape(value, quote=True)}"'
        for key, value in values.items()
        if value
    )


def _render_image_strip(results: list[ArticleFetchResult]) -> str:
    frames = []
    for result in results:
        image_url = _result_image_url(result)
        if not image_url:
            continue
        frames.append(
            f"""
            <figure class="strip-frame">
              <img src="{escape(image_url, quote=True)}" alt="{escape(_story_title(result), quote=True)}" loading="lazy" />
            </figure>
            """
        )
        if len(frames) == 3:
            break
    if not frames:
        return ""
    return f'<section class="img-strip" aria-label="Story images">{"".join(frames)}</section>'


def _render_thumbnail(result: ArticleFetchResult, class_name: str) -> str:
    image_url = _result_image_url(result)
    if image_url:
        return (
            f'<figure class="{class_name}">'
            f'<img src="{escape(image_url, quote=True)}" alt="{escape(_story_title(result), quote=True)}" loading="lazy" />'
            f'</figure>'
        )
    return f'<figure class="{class_name}">{_render_fallback_art(result)}</figure>'


def _render_fallback_art(result: ArticleFetchResult) -> str:
    return f'<div class="fallback-art" aria-hidden="true">{_source_icon_svg(result)}</div>'


def _source_icon_svg(result: ArticleFetchResult) -> str:
    label = _source_label(result)
    if label == "YouTube":
        path = '<path d="M9 7.5v9l8-4.5-8-4.5Z" fill="currentColor"/><rect x="3" y="5" width="18" height="14" rx="4" fill="none" stroke="currentColor" stroke-width="1.8"/>'
    elif label == "Podcast":
        path = '<path d="M12 4a4 4 0 0 1 4 4v4a4 4 0 0 1-8 0V8a4 4 0 0 1 4-4Z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M6 11v1a6 6 0 0 0 12 0v-1M12 18v3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>'
    else:
        path = '<path d="M4 18 9.5 9l4 5 2.5-3 4 7H4Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><circle cx="16" cy="7" r="2" fill="currentColor"/><rect x="3" y="4" width="18" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/>'
    return f'<svg viewBox="0 0 24 24" role="img" aria-label="{escape(label)}">{path}</svg>'


def _render_lead_story(result: ArticleFetchResult, *, issue_id: str | None = None) -> str:
    url = _result_url(result)
    link_url, link_target, link_class, link_attributes, modal_html = _story_link_parts(result, issue_id=issue_id)
    feedback_html = _render_feedback_controls(issue_id, url) if result.fetched else ""
    return f"""
      <article class="lead-block">
        <div class="lead-bar" aria-hidden="true"></div>
        <div class="lead-content">
          <div class="story-meta">{_source_badge(result)}{_score_badge(result)}<span class="meta">{escape(_meta_line_for_result(result))}</span></div>
          <h2 class="lead-title"><a href="{escape(link_url, quote=True)}"{link_target}{link_class}{link_attributes}>{escape(_story_title(result))}</a></h2>
          <p class="lead-summary">{escape(_story_summary(result))}</p>
          {_translation_original_html(result)}
          {_keyword_html(result)}
          {feedback_html}
          {modal_html}
        </div>
      </article>
    """


def _render_ranked_story(
    result: ArticleFetchResult,
    *,
    index: int,
    issue_id: str | None = None,
) -> str:
    url = _result_url(result)
    link_url, link_target, link_class, link_attributes, modal_html = _story_link_parts(result, issue_id=issue_id)
    feedback_html = _render_feedback_controls(issue_id, url) if result.fetched else ""
    return f"""
      <article class="story-row">
        <div class="story-num">{index:02d}</div>
        <div class="story-copy">
          <div class="story-meta">{_source_badge(result)}{_score_badge(result)}<span class="meta">{escape(_meta_line_for_result(result))}</span></div>
          <h3 class="story-title"><a href="{escape(link_url, quote=True)}"{link_target}{link_class}{link_attributes}>{escape(_story_title(result))}</a></h3>
          <p class="story-summary">{escape(_story_summary(result))}</p>
          {_translation_original_html(result)}
          {_keyword_html(result)}
          {feedback_html}
          {modal_html}
        </div>
        {_render_thumbnail(result, "story-thumb")}
      </article>
    """


def _render_lower_confidence_story(
    result: ArticleFetchResult,
    *,
    index: int,
    issue_id: str | None = None,
) -> str:
    url = _result_url(result)
    link_url, link_target, link_class, link_attributes, modal_html = _story_link_parts(result, issue_id=issue_id)
    feedback_html = _render_feedback_controls(issue_id, url) if result.fetched else ""
    return f"""
      <article class="low-conf-row">
        <div class="story-num">{index:02d}</div>
        <div>
          <div class="story-meta">{_source_badge(result)}{_score_badge(result)}<span class="meta">{escape(_meta_line_for_result(result))}</span></div>
          <h3 class="story-title"><a href="{escape(link_url, quote=True)}"{link_target}{link_class}{link_attributes}>{escape(_story_title(result))}</a></h3>
          <p>{escape(_story_summary(result))}</p>
          {_translation_original_html(result)}
          {_keyword_html(result)}
          {feedback_html}
          {modal_html}
        </div>
      </article>
    """


def _render_media_card(result: ArticleFetchResult, *, issue_id: str | None = None) -> str:
    url = _result_url(result)
    title_attributes = ""
    title_class = ""
    title_target = ' target="_blank" rel="noreferrer"'
    modal_html = ""
    cta_copy = "Open"
    if result.payload.source_type == "podcast_episode":
        modal_id = _podcast_modal_id(result)
        url = f"#{modal_id}"
        title_attributes = f' data-podcast-modal-target="{escape(modal_id, quote=True)}"'
        title_class = ' class="podcast-modal-link"'
        title_target = ""
        modal_html = _render_podcast_modal(result, modal_id)
        cta_copy = "Listen"
    elif result.payload.source_type == "youtube_video":
        modal_id = _youtube_modal_id(result)
        url = f"#{modal_id}"
        title_attributes = f' data-youtube-modal-target="{escape(modal_id, quote=True)}"'
        title_class = ' class="youtube-modal-link"'
        title_target = ""
        modal_html = _render_youtube_modal(result, modal_id)
        cta_copy = "Watch"
    feedback_html = _render_feedback_controls(issue_id, _result_url(result)) if result.fetched else ""
    return f"""
      <article class="media-card">
        {_render_thumbnail(result, "media-thumb")}
        <div class="story-meta">{_source_badge(result)}{_score_badge(result)}<span class="meta">{escape(_meta_line_for_result(result))}</span></div>
        <h3 class="media-title"><a href="{escape(url, quote=True)}"{title_target}{title_class}{title_attributes}>{escape(_story_title(result))}</a></h3>
        <p class="story-summary">{escape(_story_summary(result))}</p>
        {_translation_original_html(result)}
        {_keyword_html(result)}
        <a class="media-cta" href="{escape(url, quote=True)}"{title_target}{title_attributes}>{escape(cta_copy)}</a>
        {feedback_html}
        {modal_html}
      </article>
    """


def _foreign_article_modal_id(result: ArticleFetchResult) -> str:
    raw_key = _result_url(result) or result.title or result.payload.id
    return f"foreign-{hashlib.sha1(raw_key.encode('utf-8')).hexdigest()[:12]}"


def _render_foreign_article_modal(result: ArticleFetchResult, modal_id: str, issue_id: str) -> str:
    url = _result_url(result)
    translation = _translation_metadata(result)
    payload_metadata = result.payload.metadata or {}
    source_language_name = str(translation.get("source_language_name") or payload_metadata.get("source_language_name") or "Original").strip()
    original_title = str(translation.get("original_title") or payload_metadata.get("original_search_title") or _story_title(result)).strip()
    original_summary = str(translation.get("original_summary") or payload_metadata.get("original_search_summary") or "").strip()
    original_seed = "\n\n".join(part for part in (original_title, original_summary) if part)
    original_html = _render_transcript_paragraphs(original_seed)
    return f"""
        <div class="podcast-modal foreign-modal" id="{escape(modal_id, quote=True)}" data-foreign-exploration-id="{escape(issue_id, quote=True)}" role="dialog" aria-modal="true" aria-labelledby="{escape(modal_id, quote=True)}-title">
          <div class="podcast-panel youtube-panel">
            <a class="podcast-close" data-foreign-close href="#">Close</a>
            <div class="section-kicker">Machine translated</div>
            <h3 id="{escape(modal_id, quote=True)}-title">{escape(_story_title(result))}</h3>
            <div class="podcast-actions">
              <a href="{escape(url, quote=True)}" target="_blank" rel="noreferrer">View original source</a>
            </div>
            <p class="foreign-status" aria-live="polite">Open this article to translate the full body.</p>
            <div class="foreign-tabs" role="tablist" aria-label="Article language view">
              <button type="button" class="active" data-foreign-tab="translated">Translated</button>
              <button type="button" data-foreign-tab="original">Original {escape(source_language_name)}</button>
            </div>
            <section class="foreign-view" data-foreign-view="translated">
              <div class="foreign-notice"></div>
              <div class="foreign-body" data-foreign-translated-body>
                <p>{escape(_story_summary(result))}</p>
              </div>
            </section>
            <section class="foreign-view" data-foreign-view="original" hidden>
              <div class="foreign-body" data-foreign-original-body>{original_html}</div>
            </section>
          </div>
        </div>
    """


def _render_brief_sidebar(
    *,
    stats: dict[str, Any],
    newsletter_html: str,
    newsletter_count: int,
    article_count: int,
    media_count: int,
    lookback_hours: int,
) -> str:
    total_model_calls = int(stats.get("model_call_count") or 0)
    successful_model_calls = int(stats.get("model_success_count") or 0)
    failed_model_calls = int(stats.get("model_failure_count") or 0)
    ai_call_value = (
        f"{_format_int(successful_model_calls)}/{_format_int(total_model_calls)} ok"
        if total_model_calls and failed_model_calls
        else _format_int(total_model_calls)
    )
    side_stats = [
        ("Articles", _format_int(article_count)),
        ("Media", _format_int(media_count)),
        ("Sources", _format_int(stats.get("source_count"))),
        ("Newsletters", _format_int(newsletter_count)),
        ("Links", _format_int(stats.get("link_count"))),
        ("AI tokens", _format_int(stats.get("total_tokens"))),
        ("AI calls", ai_call_value),
        ("Processing", _format_duration(stats.get("processing_seconds"))),
        ("Recency", f"{lookback_hours}h"),
    ]
    stat_html = "\n".join(
        f'<div class="side-stat"><span>{escape(label)}</span><strong class="side-value">{escape(value)}</strong></div>'
        for label, value in side_stats
    )
    stage_seconds = stats.get("stage_seconds") if isinstance(stats.get("stage_seconds"), dict) else {}
    stage_html = ""
    if stage_seconds:
        stage_labels = {
            "ingestion": "Ingestion",
            "fetching": "Fetching",
            "classification": "Classification",
            "editorial": "Editorial + review",
            "publishing": "Publishing",
        }
        stage_items = "\n".join(
            f"<li>{escape(stage_labels.get(str(key), str(key).replace('_', ' ').title()))}: {escape(_format_stage_duration(value))}</li>"
            for key, value in stage_seconds.items()
        )
        stage_html = f'<ul class="stage-list">{stage_items}</ul>'
    token_detail = _render_token_detail(stats)
    completion_unavailable_count = int(stats.get("completion_unavailable_count") or 0)
    token_warning = ""
    if failed_model_calls:
        unavailable_note = (
            f" Completion tokens were unavailable for {_format_int(completion_unavailable_count)} failed call(s)."
            if completion_unavailable_count
            else ""
        )
        token_warning = (
            f'<p class="meta">AI warning: {_format_int(failed_model_calls)} model call(s) failed before completion; '
            f"this token total may be incomplete.{unavailable_note}</p>"
        )
    source_notes_html = ""
    if newsletter_html:
        source_notes_html = f"""
          <details class="source-notes" open>
            <summary>Source notes</summary>
            {newsletter_html}
          </details>
        """
    strategy_html = _render_sidebar_note("Search strategy", _search_strategy_text(stats))
    model_usage_html = _render_sidebar_note("AI used", _model_usage_text(stats))
    return f"""
      <aside class="brief-sidebar" aria-label="Brief sources and process">
        <section class="side-panel provenance">
          <div class="section-kicker">Sources & process</div>
          <h2>How this was made</h2>
          <div class="side-stats">{stat_html}</div>
          {strategy_html}
          {model_usage_html}
          {stage_html}
          {token_detail}
          {token_warning}
          {source_notes_html}
        </section>
      </aside>
    """


def _render_sidebar_note(title: str, text: str | None) -> str:
    body = str(text or "").strip()
    if not body:
        return ""
    return f"""
          <div class="side-note">
            <h3>{escape(title)}</h3>
            <p>{escape(body)}</p>
          </div>
    """


def _search_strategy_text(stats: dict[str, Any]) -> str:
    strategy = stats.get("search_strategy") if isinstance(stats.get("search_strategy"), dict) else {}
    summary = str(strategy.get("summary") or "").strip()
    if summary:
        return summary
    queries = _string_values(strategy.get("queries") if isinstance(strategy, dict) else None, limit=2)
    source_names = _string_values(strategy.get("sources") if isinstance(strategy, dict) else None, limit=5)
    scope = str(strategy.get("source_scope") or stats.get("source_scope_label") or "").strip()
    pieces: list[str] = []
    if source_names:
        pieces.append("Looked across " + ", ".join(source_names))
    if queries:
        pieces.append("Query examples: " + "; ".join(queries))
    if scope:
        pieces.append("Source scope: " + scope)
    return ". ".join(pieces).strip()


def _model_usage_text(stats: dict[str, Any]) -> str:
    usage = stats.get("model_usage") if isinstance(stats.get("model_usage"), list) else []
    if usage:
        model_names: list[str] = []
        total_calls = 0
        successful = 0
        failed = 0
        modes: set[str] = set()
        for row in usage:
            if not isinstance(row, dict):
                continue
            model = str(row.get("model") or "").strip()
            if model and model not in model_names:
                model_names.append(model)
            mode = str(row.get("mode") or "").strip()
            if mode:
                modes.add(_model_mode_label(mode))
            total_calls += int(row.get("call_count") or 0)
            successful += int(row.get("success_count") or 0)
            failed += int(row.get("failure_count") or 0)
        if model_names:
            model_part = ", ".join(model_names[:3])
            if len(model_names) > 3:
                model_part += f" +{len(model_names) - 3} more"
            task_part = ", ".join(sorted(modes)) if modes else "brief generation"
            call_part = f"{successful}/{total_calls} calls completed" if failed else f"{total_calls} calls"
            return f"{model_part} supported {task_part}; {call_part}."
    fallback = str(stats.get("model_usage_summary") or "").strip()
    if fallback:
        return fallback
    total_calls = int(stats.get("model_call_count") or 0)
    if total_calls:
        successful = int(stats.get("model_success_count") or 0)
        failed = int(stats.get("model_failure_count") or 0)
        return f"AI assisted the brief generation; {successful}/{total_calls} calls completed." if failed else f"AI assisted the brief generation across {total_calls} calls."
    return ""


def _model_mode_label(mode: str) -> str:
    labels = {
        "single": "article summaries",
        "source_audit": "source audit",
        "editorial": "ranking",
        "critic": "review",
        "refinement": "interest refinement",
    }
    return labels.get(mode, mode.replace("_", " "))


def _string_values(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _render_article_sections(results: list[ArticleFetchResult], *, issue_id: str | None = None) -> str:
    grouped: dict[str, list[ArticleFetchResult]] = {}
    for result in results:
        grouped.setdefault(result.section or "Noteworthy", []).append(result)

    sections: list[str] = []
    for section, section_results in grouped.items():
        cards = "\n".join(
            _render_article_card(result, variant="compact", issue_id=issue_id)
            for result in section_results
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
    podcast_modal_id = _podcast_modal_id(result) if result.payload.source_type == "podcast_episode" else ""
    youtube_modal_id = _youtube_modal_id(result) if result.payload.source_type == "youtube_video" else ""
    title_attributes = ""
    title_class = ""
    title_target = ' target="_blank" rel="noreferrer"'
    modal_html = ""
    if podcast_modal_id:
        url = f"#{podcast_modal_id}"
        title_attributes = f' data-podcast-modal-target="{escape(podcast_modal_id, quote=True)}"'
        title_class = ' class="podcast-modal-link"'
        title_target = ""
        modal_html = _render_podcast_modal(result, podcast_modal_id)
    elif youtube_modal_id:
        url = f"#{youtube_modal_id}"
        title_attributes = f' data-youtube-modal-target="{escape(youtube_modal_id, quote=True)}"'
        title_class = ' class="youtube-modal-link"'
        title_target = ""
        modal_html = _render_youtube_modal(result, youtube_modal_id)
    elif _supports_foreign_article_modal(result, issue_id=issue_id):
        url, title_target, title_class, title_attributes, modal_html = _story_link_parts(result, issue_id=issue_id)
    return f"""
      <article class="{card_class}">
        <div class="meta">{meta}{score}</div>
        <h3><a href="{escape(url, quote=True)}"{title_target}{title_class}{title_attributes}>{escape(title)}</a></h3>
        <p>{escape(summary)}</p>
        {keyword_html}
        {feedback_html}
        {modal_html}
      </article>
    """


def _podcast_modal_id(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    raw_key = str(metadata.get("podcast_episode_id") or result.original_url or result.title or result.payload.id)
    return f"podcast-{hashlib.sha1(raw_key.encode('utf-8')).hexdigest()[:12]}"


def _render_podcast_modal(result: ArticleFetchResult, modal_id: str) -> str:
    metadata = result.payload.metadata or {}
    show_name = str(metadata.get("podcast_title") or result.payload.source_name or "Podcast")
    episode_title = _clean_newsletter_text(str(metadata.get("title") or result.title or "Podcast episode"))
    image_url = _safe_web_url(metadata.get("image_url"))
    audio_url = _safe_web_url(metadata.get("audio_url"))
    apple_url = _safe_web_url(metadata.get("apple_podcasts_url"))
    episode_url = _safe_web_url(metadata.get("episode_url"))
    transcript_source = str(metadata.get("transcript_source") or "show_notes")
    transcript_label = "Transcript" if transcript_source in {"transcript", "transcript_cache"} else "Show Notes"
    transcript_html = _render_transcript_paragraphs(_podcast_transcript_text(result))
    duration = _format_duration(metadata.get("duration_seconds"))
    meta_parts = [show_name]
    if duration:
        meta_parts.append(duration)
    if result.payload.published_at:
        meta_parts.append(_format_article_date(result.payload.published_at))
    brand_html = (
        f'<img class="podcast-art" src="{escape(image_url, quote=True)}" alt="{escape(show_name, quote=True)} artwork" loading="lazy" />'
        if image_url
        else f'<div class="podcast-art fallback" aria-hidden="true">{escape(_podcast_initials(show_name))}</div>'
    )
    player_html = (
        f'<audio class="podcast-player" controls preload="none" src="{escape(audio_url, quote=True)}"></audio>'
        if audio_url
        else '<p class="meta">Audio is not available for this episode.</p>'
    )
    action_links = []
    if apple_url:
        action_links.append(f'<a href="{escape(apple_url, quote=True)}" target="_blank" rel="noreferrer">Apple Podcasts</a>')
    if episode_url and episode_url != apple_url:
        action_links.append(f'<a href="{escape(episode_url, quote=True)}" target="_blank" rel="noreferrer">Listen</a>')
    actions_html = f'<div class="podcast-actions">{" ".join(action_links)}</div>' if action_links else ""
    return f"""
        <div class="podcast-modal" id="{escape(modal_id, quote=True)}" role="dialog" aria-modal="true" aria-labelledby="{escape(modal_id, quote=True)}-title">
          <div class="podcast-panel">
            <a class="podcast-close" data-podcast-close href="#">Close</a>
            <div class="podcast-brand">
              {brand_html}
              <div>
                <div class="meta">{escape(" · ".join(part for part in meta_parts if part))}</div>
                <h3 id="{escape(modal_id, quote=True)}-title">{escape(episode_title)}</h3>
                {actions_html}
              </div>
            </div>
            {player_html}
            <section class="podcast-transcript">
              <h4>{escape(transcript_label)}</h4>
              {transcript_html}
            </section>
          </div>
        </div>
    """


def _youtube_modal_id(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    raw_key = str(metadata.get("video_id") or result.original_url or result.title or result.payload.id)
    return f"youtube-{hashlib.sha1(raw_key.encode('utf-8')).hexdigest()[:12]}"


def _render_youtube_modal(result: ArticleFetchResult, modal_id: str) -> str:
    metadata = result.payload.metadata or {}
    channel_name = str(metadata.get("channel_name") or result.payload.source_name or "YouTube")
    video_title = _clean_newsletter_text(str(metadata.get("youtube_title") or metadata.get("title") or result.title or "YouTube video"))
    youtube_url = _safe_web_url(metadata.get("youtube_url")) or _safe_web_url(result.final_url or result.original_url) or ""
    embed_url = _youtube_embed_url(metadata.get("video_id"), youtube_url)
    image_url = _result_image_url(result)
    summary = _clean_newsletter_text(result.editor_summary or result.excerpt)
    transcript_html = _render_transcript_paragraphs(_youtube_transcript_text(result))
    duration = _format_duration(metadata.get("duration_seconds"))
    meta_parts = [channel_name]
    if duration:
        meta_parts.append(duration)
    if result.payload.published_at:
        meta_parts.append(_format_article_date(result.payload.published_at))
    player_html = (
        f'<iframe class="youtube-player" data-youtube-src="{escape(embed_url, quote=True)}" title="{escape(video_title, quote=True)}" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen loading="lazy"></iframe>'
        if embed_url
        else '<p class="meta">Video playback is not available for this item.</p>'
    )
    action_links = []
    if youtube_url:
        action_links.append(f'<a href="{escape(youtube_url, quote=True)}" target="_blank" rel="noreferrer">Watch on YouTube</a>')
    actions_html = f'<div class="podcast-actions">{" ".join(action_links)}</div>' if action_links else ""
    brand_art = (
        f'<img class="podcast-art" src="{escape(image_url, quote=True)}" alt="{escape(channel_name, quote=True)} thumbnail" loading="lazy" />'
        if image_url
        else '<div class="podcast-art fallback" aria-hidden="true">YT</div>'
    )
    return f"""
        <div class="podcast-modal youtube-modal" id="{escape(modal_id, quote=True)}" role="dialog" aria-modal="true" aria-labelledby="{escape(modal_id, quote=True)}-title">
          <div class="podcast-panel youtube-panel">
            <a class="podcast-close" data-youtube-close href="#">Close</a>
            <div class="podcast-brand">
              {brand_art}
              <div>
                <div class="meta">{escape(" · ".join(part for part in meta_parts if part))}</div>
                <h3 id="{escape(modal_id, quote=True)}-title">{escape(video_title)}</h3>
                {actions_html}
              </div>
            </div>
            {player_html}
            <section class="youtube-summary">
              <h4>Summary</h4>
              <p>{escape(summary)}</p>
            </section>
            <section class="podcast-transcript">
              <h4>Transcript</h4>
              {transcript_html}
            </section>
          </div>
        </div>
    """


def _youtube_embed_url(raw_video_id: Any, youtube_url: str) -> str:
    video_id = str(raw_video_id or "").strip()
    if not video_id and youtube_url:
        parsed = urlparse(youtube_url)
        if parsed.hostname and "youtu.be" in parsed.hostname:
            video_id = parsed.path.strip("/")
        elif parsed.hostname and "youtube" in parsed.hostname:
            query = dict(parse_qsl(parsed.query, keep_blank_values=False))
            video_id = str(query.get("v") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
        return ""
    return f"https://www.youtube-nocookie.com/embed/{video_id}?rel=0"


def _youtube_transcript_text(result: ArticleFetchResult) -> str:
    return " ".join((result.text or result.payload.raw_text or result.excerpt or "").split())


def _safe_web_url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith(("http://", "https://")) else None


def _podcast_initials(value: str) -> str:
    parts = [part[:1].upper() for part in re.findall(r"[A-Za-z0-9]+", value)[:3]]
    return "".join(parts) or "P"


def _format_duration(value: Any) -> str:
    seconds = _nullable_int(value)
    if not seconds:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, _seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _podcast_transcript_text(result: ArticleFetchResult) -> str:
    text = " ".join((result.text or result.payload.raw_text or result.excerpt or "").split())
    match = re.search(r"(?:Transcript|Show notes):\s*(.+)", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _render_transcript_paragraphs(text: str) -> str:
    paragraphs = _transcript_paragraphs(text)
    if not paragraphs:
        return '<p>No transcript text is available yet.</p>'
    return "\n".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)


def _transcript_paragraphs(text: str) -> list[str]:
    cleaned = _clean_newsletter_text(text)
    parts = [part.strip() for part in re.split(r"\n{2,}", cleaned) if part.strip()]
    if len(parts) > 1:
        return parts
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", cleaned) if sentence.strip()]
    if not sentences:
        return [cleaned] if cleaned else []
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > 760:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


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


def _render_podcast_modal_script() -> str:
    return """
  <script>
    (() => {
      const activeModal = () => {
        if (!window.location.hash) return null;
        const id = window.location.hash.slice(1);
        return document.getElementById(id);
      };

      const syncModalState = () => {
        const modal = activeModal();
        document.body.classList.toggle("modal-open", Boolean(modal && modal.classList.contains("podcast-modal")));
        document.querySelectorAll(".podcast-modal audio").forEach((player) => {
          if (!modal || !modal.contains(player)) player.pause();
        });
        document.querySelectorAll(".youtube-modal iframe[data-youtube-src]").forEach((player) => {
          if (modal && modal.contains(player)) {
            if (!player.getAttribute("src")) player.setAttribute("src", player.getAttribute("data-youtube-src"));
          } else {
            player.removeAttribute("src");
          }
        });
      };

      const closeModal = () => {
        document.querySelectorAll(".podcast-modal audio").forEach((player) => player.pause());
        document.querySelectorAll(".youtube-modal iframe[data-youtube-src]").forEach((player) => player.removeAttribute("src"));
        document.body.classList.remove("modal-open");
        if (window.location.hash) history.pushState("", document.title, window.location.pathname + window.location.search);
      };

      const paragraphs = (value) => {
        const text = String(value || "").trim();
        if (!text) return "<p>No article text is available.</p>";
        return text
          .split(/\\n{2,}/)
          .map((part) => part.trim())
          .filter(Boolean)
          .map((part) => `<p>${part.replace(/[&<>"']/g, (char) => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;"
          })[char])}</p>`)
          .join("");
      };

      const setForeignView = (modal, viewName) => {
        modal.querySelectorAll("[data-foreign-view]").forEach((view) => {
          view.hidden = view.getAttribute("data-foreign-view") !== viewName;
        });
        modal.querySelectorAll("[data-foreign-tab]").forEach((button) => {
          button.classList.toggle("active", button.getAttribute("data-foreign-tab") === viewName);
        });
      };

      const loadForeignArticle = async (trigger, modal) => {
        if (!modal || modal.getAttribute("data-foreign-loaded") === "true") return;
        const status = modal.querySelector(".foreign-status");
        const notice = modal.querySelector(".foreign-notice");
        const translatedBody = modal.querySelector("[data-foreign-translated-body]");
        const originalBody = modal.querySelector("[data-foreign-original-body]");
        const explorationId = modal.getAttribute("data-foreign-exploration-id");
        if (!explorationId) return;
        modal.setAttribute("data-foreign-loaded", "loading");
        if (status) status.textContent = "Fetching and translating the full article...";
        try {
          const response = await fetch(`/api/explore/explorations/${encodeURIComponent(explorationId)}/foreign-article/translation`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              url: trigger.getAttribute("data-foreign-url"),
              title: trigger.getAttribute("data-foreign-title"),
              summary: trigger.getAttribute("data-foreign-summary"),
              source_language: trigger.getAttribute("data-foreign-source-language"),
              source_language_name: trigger.getAttribute("data-foreign-source-language-name"),
              original_title: trigger.getAttribute("data-foreign-original-title"),
              original_summary: trigger.getAttribute("data-foreign-original-summary")
            })
          });
          if (!response.ok) throw new Error("Translation request failed");
          const data = await response.json();
          if (translatedBody) translatedBody.innerHTML = paragraphs(data.translated_body || data.translated_summary);
          if (originalBody) originalBody.innerHTML = paragraphs([data.original_title, data.original_body].filter(Boolean).join("\\n\\n"));
          if (notice) notice.textContent = data.notice || "";
          if (status) status.textContent = data.cached ? "Loaded from translation cache." : "Translated article ready.";
          modal.setAttribute("data-foreign-loaded", "true");
        } catch (_error) {
          modal.removeAttribute("data-foreign-loaded");
          if (status) status.textContent = "Could not translate this article. Try again or open the original source.";
        }
      };

      document.addEventListener("click", (event) => {
        const tab = event.target.closest("[data-foreign-tab]");
        if (tab) {
          const modal = tab.closest(".foreign-modal");
          if (modal) setForeignView(modal, tab.getAttribute("data-foreign-tab"));
          return;
        }

        const trigger = event.target.closest("[data-podcast-modal-target], [data-youtube-modal-target], [data-foreign-article-target]");
        if (trigger) {
          const modalId = trigger.getAttribute("data-podcast-modal-target") || trigger.getAttribute("data-youtube-modal-target") || trigger.getAttribute("data-foreign-article-target");
          if (modalId) {
            event.preventDefault();
            window.location.hash = modalId;
            syncModalState();
            const modal = document.getElementById(modalId);
            if (trigger.hasAttribute("data-foreign-article-target")) loadForeignArticle(trigger, modal);
          }
          return;
        }

        const closeButton = event.target.closest("[data-podcast-close], [data-youtube-close], [data-foreign-close]");
        if (closeButton) {
          event.preventDefault();
          closeModal();
          return;
        }

        if (event.target.classList && event.target.classList.contains("podcast-modal")) {
          closeModal();
        }
      });

      document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        closeModal();
      });

      window.addEventListener("hashchange", syncModalState);
      syncModalState();
    })();
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
            "editorial": "Editorial + review",
            "publishing": "Publishing",
        }
        stage_items = "\n".join(
            f"<li>{escape(stage_labels.get(str(key), str(key).replace('_', ' ').title()))}: "
            f"{escape(_format_stage_duration(value))}</li>"
            for key, value in stage_seconds.items()
        )
        stage_html = f'<div class="digest-stat"><span>Stage timing</span><ul class="stage-list">{stage_items}</ul></div>'

    total_model_calls = int(stats.get("model_call_count") or 0)
    successful_model_calls = int(stats.get("model_success_count") or 0)
    failed_model_calls = int(stats.get("model_failure_count") or 0)
    ai_call_value = (
        f"{_format_int(successful_model_calls)}/{_format_int(total_model_calls)} ok"
        if total_model_calls and failed_model_calls
        else _format_int(total_model_calls)
    )
    stat_items = [
        ("Sources", _format_int(stats.get("source_count"))),
        ("Newsletters", _format_int(stats.get("newsletter_count"))),
        ("Links extracted", _format_int(stats.get("link_count"))),
        ("Podcast episodes", _format_int(stats.get("podcast_episode_count"))),
        ("Articles included", _format_int(stats.get("included_article_count"))),
        ("Items filtered", _format_int(int(stats.get("dropped_count") or 0) + int(stats.get("unresolved_count") or 0))),
        ("AI tokens", _format_int(stats.get("total_tokens"))),
        ("AI calls", ai_call_value),
        ("Processing time", _format_duration(stats.get("processing_seconds"))),
    ]
    stat_html = "\n".join(
        f'<div class="digest-stat"><span>{escape(label)}</span><strong>{escape(value)}</strong></div>'
        for label, value in stat_items
    )
    token_detail = _render_token_detail(stats)
    completion_unavailable_count = int(stats.get("completion_unavailable_count") or 0)
    token_warning = ""
    if failed_model_calls:
        unavailable_note = (
            f" Completion tokens were unavailable for {_format_int(completion_unavailable_count)} failed call(s)."
            if completion_unavailable_count
            else ""
        )
        token_warning = (
            f'<p class="meta">AI warning: {_format_int(failed_model_calls)} model call(s) failed before completion; '
            f"this token total may be incomplete.{unavailable_note}</p>"
        )
    return f"""
      <div class="digest-stats">
        {stat_html}
        {stage_html}
      </div>
      {token_detail}
      {token_warning}
    """


def _format_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def _render_token_detail(stats: dict[str, Any]) -> str:
    if not int(stats.get("total_tokens") or 0):
        return ""
    prompt = _format_int(stats.get("prompt_tokens"))
    completion = int(stats.get("completion_tokens") or 0)
    completion_display = _format_int(completion)
    unavailable_count = int(stats.get("completion_unavailable_count") or 0)
    failed_count = int(stats.get("model_failure_count") or 0)
    if unavailable_count and failed_count and completion == 0:
        return f'<p class="meta">Token detail: {prompt} prompt tokens recorded; completion tokens unavailable.</p>'
    if unavailable_count and failed_count:
        return (
            f'<p class="meta">Token detail: {prompt} prompt + {completion_display} completion recorded; '
            "some completion tokens unavailable.</p>"
        )
    return f'<p class="meta">Token detail: {prompt} prompt + {completion_display} completion.</p>'


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


def _format_stage_duration(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "not measured"
    if seconds <= 0:
        return "not measured"
    return _format_duration(seconds)


def _section_for_payload(payload: NormalizedPayload) -> str:
    if payload.source_type == "podcast_episode":
        return "Podcast Signals"
    if payload.source_type == "youtube_video":
        return "YouTube Videos"
    if payload.source_type == "collection_chunk":
        return "Collections"
    if payload.source_type == "market_snapshot":
        tier = str((payload.metadata or {}).get("tier") or "").strip().lower()
        return "Core Companies" if tier == "core" else "Related Companies" if tier == "related" else "Markets"
    return "Newsletter" if payload.source_type == "gmail" else "Discovered Link"


def _editor_note_for_payload(payload: NormalizedPayload) -> str:
    if payload.source_type == "podcast_episode":
        return "Podcast episode ingested from a configured feed or aggregator search."
    if payload.source_type == "youtube_video":
        return "YouTube video transcript ingested from a configured API search."
    if payload.source_type == "collection_chunk":
        return "Local collection file content retrieved from the selected Collections source."
    if payload.source_type == "market_snapshot":
        return "Public-market snapshot retrieved from free market data."
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
    if result.payload.source_type == "youtube_video":
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        source = str((result.payload.metadata or {}).get("transcript_source") or "transcript").replace("_", " ")
        return f"YouTube video summarized from {source}.{score}"
    if result.payload.source_type == "collection_chunk":
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        collection = str((result.payload.metadata or {}).get("collection_name") or result.payload.source_name or "collection")
        return f"Local collection context from {collection}.{score}"
    if result.payload.source_type == "market_snapshot":
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        ticker = str((result.payload.metadata or {}).get("ticker") or result.payload.source_name or "market")
        return f"Public-market context for {ticker}.{score}"
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


def _render_masthead_meta(generated_at: str | None, lookback_hours: int, stats: dict[str, Any] | None = None) -> str:
    source_scope = str((stats or {}).get("source_scope_label") or "").strip()
    if not source_scope:
        source_scope = _format_source_scope(lookback_hours)
    return escape(f"Generated {_format_generated_timestamp(generated_at)} · Source scope: {source_scope}")


def _format_source_scope(lookback_hours: int) -> str:
    try:
        hours = max(1, int(lookback_hours))
    except (TypeError, ValueError):
        hours = 24
    if hours % 24 == 0:
        days = hours // 24
        if days == 1:
            return "last 24 hours"
        return f"last {days} days"
    if hours == 1:
        return "last hour"
    return f"last {hours} hours"


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
    route_groups: dict[tuple[str, str, str | None, str | None], list[dict[str, Any]]] = {}
    for row in records:
        key = (
            str(row.get("mode") or "single"),
            str(row.get("model") or "unknown"),
            _nullable_str(row.get("backend")),
            _nullable_str(row.get("model_tag")),
        )
        route_groups.setdefault(key, []).append(row)

    route_summaries = []
    for (mode, model, backend, model_tag), group in route_groups.items():
        durations = sorted(_model_service_ms(row) for row in group if row.get("total_ms") is not None)
        success = sum(1 for row in group if row["status"] == "success")
        route_summaries.append(
            {
                "mode": mode,
                "model": model,
                "backend": backend,
                "model_tag": model_tag,
                "record_count": len(group),
                "success_count": success,
                "failure_count": len(group) - success,
                "avg_total_ms": _average(durations),
                "fallback_rate": _rate(sum(1 for row in group if row.get("fallback_triggered")), len(group)),
            }
        )

    route_summaries.sort(key=lambda row: (row["mode"], -row["record_count"]))
    recent = records[:20]
    return {
        "record_count": total_count,
        "success_count": success_count,
        "failure_count": total_count - success_count,
        "latest_ts": records[0]["ts"] if records else None,
        "status_counts": status_counts,
        "models": model_summaries,
        "routes": route_summaries,
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
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (digest_id,),
        ).fetchone()
    return row_to_dict(row)


def get_issue(issue_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM digest_issues WHERE id = ?", (issue_id,)).fetchone()
    return row_to_dict(row)
