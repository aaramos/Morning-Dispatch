from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.agents.agentic import AgentDecision
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.editor import build_issue_snapshot
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.core.config import ensure_runtime_dirs, get_settings
from backend.app.db.schema import SCHEMA_SQL
from backend.app.services.brief_title import tight_brief_title
from backend.app.services.brief_renderer import (
    _clean_newsletter_text as _clean_newsletter_text,
    _domain as _domain,
    _nullable_int as _nullable_int,
    _origin_source_label as _origin_source_label,
    _render_foreign_article_modal as _render_foreign_article_modal,
    _summary_for_payload as _summary_for_payload,
    _title_for_payload as _title_for_payload,
    _translation_original_html as _translation_original_html,
    _truncate_text as _truncate_text,
    _weak_newsletter_snippet as _weak_newsletter_snippet,
    clean_issue_html_for_display as clean_issue_html_for_display,
    ensure_generated_footer as ensure_generated_footer,
    render_ingested_issue as render_ingested_issue,
    render_placeholder_issue as render_placeholder_issue,
)

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


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


def database_path() -> Path:
    return get_settings().database_path


# Tracks data_dirs whose runtime directories have already been created, so
# connect() doesn't redo ~18 mkdir/chmod calls on every connection.
_RUNTIME_DIRS_READY: set[Path] = set()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    if settings.data_dir not in _RUNTIME_DIRS_READY:
        ensure_runtime_dirs(settings)
        _RUNTIME_DIRS_READY.add(settings.data_dir)
    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # The database runs in WAL mode (set at init); these per-connection pragmas
    # keep concurrent readers/writers from failing fast on short lock contention
    # and relax fsync to the WAL-safe NORMAL level.
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = NORMAL")
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
        _ensure_updated_feedback_table(connection)
        _ensure_updated_exploration_feedback_table(connection)
        _ensure_podcast_cache_tables(connection)
        _ensure_digest_run_metric_columns(connection)
        _ensure_digest_delivery_settings_table(connection)
        _ensure_podcast_metrics_table(connection)
        _ensure_youtube_quota_table(connection)
        _ensure_collection_tables(connection)
        _ensure_served_undated_items_table(connection)
        _ensure_gmail_senders_table(connection)
        _ensure_inference_metric_status_values(connection)
        _ensure_inference_metric_route_name_column(connection)
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
        (new_id(), "Default", now, now),
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


def _ensure_served_undated_items_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS served_undated_items (
          id              TEXT PRIMARY KEY,
          topic_id        TEXT NOT NULL,
          item_key        TEXT NOT NULL,
          title           TEXT,
          source_name     TEXT,
          url             TEXT,
          first_seen_at   TEXT NOT NULL,
          UNIQUE(topic_id, item_key)
        ) STRICT
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_served_undated_items_topic ON served_undated_items(topic_id)"
    )


def _ensure_gmail_senders_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS gmail_senders (
          id            TEXT PRIMARY KEY,
          sender        TEXT NOT NULL UNIQUE,
          sender_name   TEXT,
          state         TEXT NOT NULL CHECK(state IN ('approved','candidate','rejected')),
          reason        TEXT,
          source        TEXT,
          message_count INTEGER NOT NULL DEFAULT 0,
          last_seen_at  TEXT,
          metadata      TEXT NOT NULL DEFAULT '{}',
          created_at    TEXT NOT NULL,
          updated_at    TEXT NOT NULL
        ) STRICT
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_gmail_senders_state ON gmail_senders(state)")


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

        CREATE TABLE IF NOT EXISTS exploration_feedback (
          id                  TEXT PRIMARY KEY,
          exploration_id      TEXT NOT NULL REFERENCES explorations(exploration_id),
          topic_id            TEXT NOT NULL REFERENCES topic_profiles(topic_id),
          digest_id           TEXT REFERENCES digests(id),
          digest_item_id      TEXT REFERENCES digest_items(id),
          url                 TEXT NOT NULL,
          stable_id           TEXT,
          source_type         TEXT,
          source_name         TEXT,
          adapter             TEXT,
          tags_json           TEXT,
          query_metadata_json TEXT,
          signal              TEXT NOT NULL CHECK(signal IN ('click', 'love', 'like', 'neutral', 'dislike', 'up', 'down')),
          created_at          TEXT NOT NULL
        ) STRICT;

        CREATE INDEX IF NOT EXISTS idx_topic_profiles_updated_at ON topic_profiles(updated_at);
        CREATE INDEX IF NOT EXISTS idx_refinement_sessions_updated_at ON refinement_sessions(updated_at);
        CREATE INDEX IF NOT EXISTS idx_explorations_topic_id ON explorations(topic_id);
        CREATE INDEX IF NOT EXISTS idx_explorations_status ON explorations(status);
        CREATE INDEX IF NOT EXISTS idx_promoted_sources_topic_id ON promoted_sources(topic_id);
        CREATE INDEX IF NOT EXISTS idx_exploration_feedback_topic_id ON exploration_feedback(topic_id);
        CREATE INDEX IF NOT EXISTS idx_exploration_feedback_exploration_id ON exploration_feedback(exploration_id);
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
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS exploration_feedback (
          id                  TEXT PRIMARY KEY,
          exploration_id      TEXT NOT NULL REFERENCES explorations(exploration_id),
          topic_id            TEXT NOT NULL REFERENCES topic_profiles(topic_id),
          digest_id           TEXT REFERENCES digests(id),
          digest_item_id      TEXT REFERENCES digest_items(id),
          url                 TEXT NOT NULL,
          stable_id           TEXT,
          source_type         TEXT,
          source_name         TEXT,
          adapter             TEXT,
          tags_json           TEXT,
          query_metadata_json TEXT,
          signal              TEXT NOT NULL CHECK(signal IN ('click', 'love', 'like', 'neutral', 'dislike', 'up', 'down')),
          created_at          TEXT NOT NULL
        ) STRICT
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_exploration_feedback_topic_id ON exploration_feedback(topic_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_exploration_feedback_exploration_id ON exploration_feedback(exploration_id)")


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


def _ensure_podcast_cache_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS podcast_discovery_cache (
          query_normalized TEXT NOT NULL,
          provider         TEXT NOT NULL,
          lookback_bucket  TEXT NOT NULL,
          results_json     TEXT NOT NULL,
          created_at       TEXT NOT NULL,
          expires_at       TEXT NOT NULL,
          PRIMARY KEY (query_normalized, provider, lookback_bucket)
        ) STRICT
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS podcast_resolution_cache (
          episode_url_normalized TEXT PRIMARY KEY,
          feed_url               TEXT,
          podcast_index_id       TEXT,
          apple_url              TEXT,
          resolved_at            TEXT NOT NULL,
          expires_at             TEXT NOT NULL
        ) STRICT
        """
    )


def _ensure_updated_feedback_table(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'feedback'"
    ).fetchone()
    table_sql = str(row["sql"] or "") if row else ""
    if "tags_json" in table_sql and "'click'" in table_sql:
        return

    connection.execute("ALTER TABLE feedback RENAME TO feedback_old")
    connection.execute(
        """
        CREATE TABLE feedback (
          id                  TEXT PRIMARY KEY,
          digest_item_id      TEXT REFERENCES digest_items(id),
          article_id          TEXT REFERENCES articles(id),
          digest_id           TEXT NOT NULL REFERENCES digests(id),
          exploration_id      TEXT REFERENCES explorations(exploration_id),
          url                 TEXT,
          source_type         TEXT,
          source_name         TEXT,
          adapter             TEXT,
          tags_json           TEXT,
          query_metadata_json TEXT,
          signal              TEXT NOT NULL CHECK(signal IN ('click', 'love', 'like', 'neutral', 'dislike', 'up', 'down')),
          created_at          TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO feedback (id, digest_item_id, article_id, digest_id, signal, created_at)
        SELECT id, digest_item_id, article_id, digest_id,
               CASE signal WHEN 'up' THEN 'like' WHEN 'down' THEN 'dislike' ELSE signal END,
               created_at
        FROM feedback_old
        """
    )
    connection.execute("DROP TABLE feedback_old")


def _ensure_updated_exploration_feedback_table(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'exploration_feedback'"
    ).fetchone()
    table_sql = str(row["sql"] or "") if row else ""
    if "tags_json" in table_sql and "'click'" in table_sql:
        return

    connection.execute("ALTER TABLE exploration_feedback RENAME TO exploration_feedback_old")
    connection.execute(
        """
        CREATE TABLE exploration_feedback (
          id                  TEXT PRIMARY KEY,
          exploration_id      TEXT NOT NULL REFERENCES explorations(exploration_id),
          topic_id            TEXT NOT NULL REFERENCES topic_profiles(topic_id),
          digest_id           TEXT REFERENCES digests(id),
          digest_item_id      TEXT REFERENCES digest_items(id),
          url                 TEXT NOT NULL,
          stable_id           TEXT,
          source_type         TEXT,
          source_name         TEXT,
          adapter             TEXT,
          tags_json           TEXT,
          query_metadata_json TEXT,
          signal              TEXT NOT NULL CHECK(signal IN ('click', 'love', 'like', 'neutral', 'dislike', 'up', 'down')),
          created_at          TEXT NOT NULL
        ) STRICT
        """
    )
    connection.execute(
        """
        INSERT INTO exploration_feedback (id, exploration_id, topic_id, url, source_name, signal, created_at)
        SELECT id, exploration_id, topic_id, url, source_name,
               CASE signal WHEN 'up' THEN 'like' WHEN 'down' THEN 'dislike' ELSE signal END,
               created_at
        FROM exploration_feedback_old
        """
    )
    connection.execute("DROP TABLE exploration_feedback_old")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_exploration_feedback_topic_id ON exploration_feedback(topic_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_exploration_feedback_exploration_id ON exploration_feedback(exploration_id)")


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


def get_feedback_profile(digest_id_or_topic_id: str) -> dict[str, Any]:
    profile = {
        "liked_domains": set(),
        "disliked_domains": set(),
        "liked_keywords": set(),
        "disliked_keywords": set(),
        "clicks": {}
    }
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT f.signal, a.domain, a.keywords, f.url
            FROM feedback f
            LEFT JOIN articles a ON a.id = f.article_id
            WHERE f.digest_id = ? OR f.exploration_id = ?
            """,
            (digest_id_or_topic_id, digest_id_or_topic_id),
        ).fetchall()

        exp_rows = connection.execute(
            """
            SELECT signal, source_name AS domain, tags_json AS keywords, url
            FROM exploration_feedback
            WHERE topic_id = ? OR exploration_id = ?
            """,
            (digest_id_or_topic_id, digest_id_or_topic_id),
        ).fetchall()

        for r in [*rows, *exp_rows]:
            signal = r["signal"]
            domain = r["domain"]
            keywords_raw = r["keywords"]
            url = r["url"]

            keywords = []
            if isinstance(keywords_raw, str) and keywords_raw:
                if keywords_raw.startswith("["):
                    try:
                        keywords = json.loads(keywords_raw)
                    except Exception:
                        pass
                else:
                    keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
            elif isinstance(keywords_raw, (list, tuple)):
                keywords = [str(k).lower() for k in keywords_raw]

            if signal == "click" and url:
                profile["clicks"][url] = profile["clicks"].get(url, 0) + 1
                continue

            if signal in ("love", "like", "up"):
                if domain:
                    profile["liked_domains"].add(domain.lower())
                for kw in keywords:
                    profile["liked_keywords"].add(kw.lower())
            elif signal in ("dislike", "down"):
                if domain:
                    profile["disliked_domains"].add(domain.lower())
                for kw in keywords:
                    profile["disliked_keywords"].add(kw.lower())

    return {
        "liked_domains": list(profile["liked_domains"]),
        "disliked_domains": list(profile["disliked_domains"]),
        "liked_keywords": list(profile["liked_keywords"]),
        "disliked_keywords": list(profile["disliked_keywords"]),
        "clicks": profile["clicks"]
    }


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
          route_name            TEXT,
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


def _ensure_inference_metric_route_name_column(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(inference_metrics)").fetchall()
    }
    if "route_name" in columns:
        return
    connection.execute("ALTER TABLE inference_metrics ADD COLUMN route_name TEXT")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    if "sources" in result:
        result["sources"] = json.loads(result["sources"])
    return result


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


def update_topic_delivery_config(topic_id: str, updates: dict[str, Any], *, clear_failure: bool = False) -> dict[str, Any] | None:
    record = get_topic_profile(topic_id)
    if record is None:
        return None
    delivery_config = dict(record["profile"].get("delivery_config") or {})
    if clear_failure:
        for key in (
            "delivery_disabled_after_failure",
            "last_delivery_status",
            "last_delivery_error",
            "last_error",
            "last_delivery_attempted_at",
        ):
            delivery_config.pop(key, None)
    delivery_config.update(updates)
    profile = {
        **record["profile"],
        "topic_id": topic_id,
        "statement": record["statement"],
        "delivery_config": delivery_config,
    }
    return upsert_topic_profile(profile)


def record_topic_delivery_result(
    *,
    topic_id: str,
    status: str,
    error: str | None = None,
    delivered_at: str | None = None,
) -> dict[str, Any] | None:
    updates: dict[str, Any] = {
        "last_delivery_status": status,
        "last_delivery_attempted_at": utc_now(),
        "last_error": error,
        "last_delivery_error": error,
        "last_delivered_at": delivered_at,
    }
    if status == "failed":
        updates["delivery_disabled_after_failure"] = True
    elif status == "sent":
        updates["delivery_disabled_after_failure"] = False
        updates["last_error"] = None
        updates["last_delivery_error"] = None
        updates["last_delivered_at"] = delivered_at or utc_now()
    return update_topic_delivery_config(topic_id, updates)


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
    progress = dict(existing.get("progress") or {})
    if status in {"complete", "failed"}:
        queue = dict(progress.get("queue") or {})
        queue["status"] = status
        if status == "complete":
            queue["message"] = "Brief ready."
        elif progress.get("cancel_requested"):
            queue["message"] = str(progress.get("error") or queue.get("message") or "Build stopped by user.")
        else:
            queue["message"] = "Build failed."
        progress["queue"] = queue
    with connect() as connection:
        connection.execute(
            """
            UPDATE explorations
            SET status = ?,
                brief_ref = COALESCE(?, brief_ref),
                emailed = COALESCE(?, emailed),
                finished_at = ?,
                progress_json = ?
            WHERE exploration_id = ?
            """,
            (
                status,
                brief_ref,
                None if emailed is None else int(emailed),
                finished_at,
                json.dumps(progress, sort_keys=True),
                exploration_id,
            ),
        )
    return get_exploration(exploration_id)


def cancel_exploration(exploration_id: str, *, reason: str = "Build stopped by user.") -> dict[str, Any] | None:
    existing = get_exploration(exploration_id)
    if existing is None:
        return None
    if existing.get("status") not in {"queued", "running"}:
        return existing
    progress = dict(existing.get("progress") or {})
    progress["cancel_requested"] = True
    progress["error"] = reason
    queue = dict(progress.get("queue") or {})
    queue["status"] = "failed"
    queue["message"] = reason
    progress["queue"] = queue
    pipeline = dict(progress.get("pipeline") or {})
    for stage, state in list(pipeline.items()):
        if state == "running":
            pipeline[stage] = "failed"
    progress["pipeline"] = pipeline
    with connect() as connection:
        connection.execute(
            """
            UPDATE explorations
            SET status = 'failed',
                finished_at = ?,
                progress_json = ?
            WHERE exploration_id = ?
              AND status IN ('queued', 'running')
            """,
            (utc_now(), json.dumps(progress, sort_keys=True), exploration_id),
        )
    return get_exploration(exploration_id)


def update_exploration_progress(
    exploration_id: str,
    *,
    progress: dict[str, Any],
) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE explorations
            SET progress_json = ?
            WHERE exploration_id = ?
            """,
            (json.dumps(progress, sort_keys=True), exploration_id),
        )
        return cursor.rowcount > 0


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


# Progress keys the frontend list view (App.tsx ProgressPanel + helpers) reads
# from `/api/explore/explorations` rows. Heavy diagnostic payloads (reasoning
# buckets, exclusion lists, intermediates, brief snapshots) are detail-only.
_EXPLORATION_SUMMARY_PROGRESS_KEYS = (
    "queue",
    "pipeline",
    "sources",
    "candidate_count",
    "source_audit",
    "source_audit_issues",
    "source_filter_notes",
    "requested_source_issues",
    "model_health",
    "built_with_issues",
    "error",
)
_EXPLORATION_SUMMARY_BRIEF_KEYS = ("title", "stats", "html_path", "candidate_count")


def _summarize_exploration_progress(progress: dict[str, Any]) -> dict[str, Any]:
    summary = {key: progress[key] for key in _EXPLORATION_SUMMARY_PROGRESS_KEYS if key in progress}
    brief = progress.get("brief")
    if isinstance(brief, dict):
        summary["brief"] = {key: brief[key] for key in _EXPLORATION_SUMMARY_BRIEF_KEYS if key in brief}
    return summary


def list_explorations(
    topic_id: str | None = None,
    *,
    limit: int | None = None,
    include_deleted: bool = False,
    only_deleted: bool = False,
    summary_only: bool = False,
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
    records = [_exploration_row_to_dict(row) for row in rows]
    if summary_only:
        for record in records:
            record["progress"] = _summarize_exploration_progress(record["progress"])
    return records


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
        connection.execute(
            "DELETE FROM source_watermarks WHERE digest_id = ?",
            (exploration_id,),
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
    try:
        connection.execute("DELETE FROM exploration_feedback WHERE topic_id = ?", (topic_id,))
        connection.execute("UPDATE refinement_sessions SET topic_id = NULL WHERE topic_id = ?", (topic_id,))
        connection.execute("DELETE FROM promoted_sources WHERE topic_id = ?", (topic_id,))
        connection.execute("DELETE FROM topic_profiles WHERE topic_id = ?", (topic_id,))
    except sqlite3.IntegrityError as exc:
        logger.warning(
            "Failed to delete standalone topic_profile '%s' due to constraint violation: %s",
            topic_id,
            exc,
        )


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


def delete_digest(digest_id: str) -> bool:
    if not digest_id:
        return False
    with connect() as connection:
        existing = connection.execute("SELECT id FROM digests WHERE id = ?", (digest_id,)).fetchone()
        if existing is None:
            return False
        run_rows = connection.execute(
            "SELECT id FROM digest_runs WHERE digest_id = ?",
            (digest_id,),
        ).fetchall()
        run_ids = [str(row["id"]) for row in run_rows]
        item_rows = connection.execute(
            "SELECT id FROM digest_items WHERE digest_id = ?",
            (digest_id,),
        ).fetchall()
        item_ids = [str(row["id"]) for row in item_rows]

        if item_ids:
            placeholders = ", ".join("?" for _ in item_ids)
            connection.execute(
                f"DELETE FROM feedback WHERE digest_item_id IN ({placeholders})",
                tuple(item_ids),
            )
            connection.execute(
                f"UPDATE exploration_feedback SET digest_item_id = NULL WHERE digest_item_id IN ({placeholders})",
                tuple(item_ids),
            )

        if run_ids:
            placeholders = ", ".join("?" for _ in run_ids)
            connection.execute(
                f"DELETE FROM agent_decisions WHERE run_id IN ({placeholders})",
                tuple(run_ids),
            )
            connection.execute(
                f"DELETE FROM digest_issues WHERE run_id IN ({placeholders})",
                tuple(run_ids),
            )

        connection.execute("DELETE FROM feedback WHERE digest_id = ?", (digest_id,))
        connection.execute("UPDATE exploration_feedback SET digest_id = NULL WHERE digest_id = ?", (digest_id,))
        connection.execute("DELETE FROM agent_decisions WHERE digest_id = ?", (digest_id,))
        connection.execute("DELETE FROM digest_items WHERE digest_id = ?", (digest_id,))
        connection.execute("DELETE FROM digest_issues WHERE digest_id = ?", (digest_id,))
        connection.execute("DELETE FROM digest_runs WHERE digest_id = ?", (digest_id,))
        connection.execute("DELETE FROM podcast_metrics WHERE digest_id = ?", (digest_id,))
        connection.execute("DELETE FROM digest_delivery_settings WHERE digest_id = ?", (digest_id,))
        connection.execute("DELETE FROM source_weights WHERE digest_id = ?", (digest_id,))
        connection.execute("DELETE FROM source_watermarks WHERE digest_id = ?", (digest_id,))
        connection.execute("DELETE FROM digests WHERE id = ?", (digest_id,))
    return True


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


def _gmail_sender_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    try:
        metadata = json.loads(record.get("metadata") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    record["metadata"] = metadata if isinstance(metadata, dict) else {}
    return record


def _normalize_sender_address(value: str) -> str:
    return str(value or "").strip().lower()


def list_gmail_senders(*, states: list[str] | None = None) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if states:
        placeholders = ", ".join("?" for _ in states)
        where = f"WHERE state IN ({placeholders})"
        params.extend(states)
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM gmail_senders
            {where}
            ORDER BY
              CASE state
                WHEN 'approved' THEN 1
                WHEN 'candidate' THEN 2
                ELSE 3
              END,
              message_count DESC,
              sender COLLATE NOCASE
            """,
            params,
        ).fetchall()
    return [_gmail_sender_row_to_dict(row) for row in rows]


def approved_gmail_senders() -> list[str]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT sender FROM gmail_senders WHERE state = 'approved' ORDER BY sender COLLATE NOCASE"
        ).fetchall()
    return [str(row["sender"]) for row in rows if row["sender"]]


def get_gmail_sender(sender: str) -> dict[str, Any] | None:
    address = _normalize_sender_address(sender)
    if not address:
        return None
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM gmail_senders WHERE sender = ?",
            (address,),
        ).fetchone()
    return _gmail_sender_row_to_dict(row) if row else None


def record_gmail_sender_candidate(
    sender: str,
    *,
    sender_name: str | None = None,
    source: str = "discovery",
    reason: str | None = None,
    message_count: int = 0,
    last_seen_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Insert a discovered sender as a candidate without ever downgrading an approved/rejected sender."""
    address = _normalize_sender_address(sender)
    if "@" not in address:
        return None
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO gmail_senders (
              id, sender, sender_name, state, reason, source,
              message_count, last_seen_at, metadata, created_at, updated_at
            )
            VALUES (?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sender) DO UPDATE SET
              sender_name = COALESCE(NULLIF(excluded.sender_name, ''), gmail_senders.sender_name),
              message_count = MAX(gmail_senders.message_count, excluded.message_count),
              last_seen_at = COALESCE(excluded.last_seen_at, gmail_senders.last_seen_at),
              updated_at = excluded.updated_at
            """,
            (
                new_id(),
                address,
                (sender_name or "").strip()[:120] or None,
                (reason or "").strip()[:240] or None,
                (source or "discovery").strip()[:40],
                max(0, int(message_count or 0)),
                last_seen_at,
                json.dumps(metadata or {}),
                now,
                now,
            ),
        )
    return get_gmail_sender(address)


def set_gmail_sender_state(
    sender: str,
    state: str,
    *,
    reason: str | None = None,
) -> dict[str, Any] | None:
    if state not in {"approved", "candidate", "rejected"}:
        raise ValueError(f"Unknown gmail sender state: {state}")
    address = _normalize_sender_address(sender)
    if not address:
        return None
    now = utc_now()
    with connect() as connection:
        result = connection.execute(
            """
            UPDATE gmail_senders
            SET state = ?, reason = COALESCE(?, reason), updated_at = ?
            WHERE sender = ?
            """,
            (state, (reason or "").strip()[:240] or None, now, address),
        )
        if result.rowcount == 0:
            return None
    return get_gmail_sender(address)


def add_gmail_sender(
    sender: str,
    *,
    sender_name: str | None = None,
    state: str = "approved",
    source: str = "manual",
    reason: str | None = None,
) -> dict[str, Any] | None:
    if state not in {"approved", "candidate", "rejected"}:
        raise ValueError(f"Unknown gmail sender state: {state}")
    address = _normalize_sender_address(sender)
    if "@" not in address:
        return None
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO gmail_senders (
              id, sender, sender_name, state, reason, source,
              message_count, last_seen_at, metadata, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, NULL, '{}', ?, ?)
            ON CONFLICT(sender) DO UPDATE SET
              sender_name = COALESCE(NULLIF(excluded.sender_name, ''), gmail_senders.sender_name),
              state = excluded.state,
              reason = COALESCE(excluded.reason, gmail_senders.reason),
              source = excluded.source,
              updated_at = excluded.updated_at
            """,
            (
                new_id(),
                address,
                (sender_name or "").strip()[:120] or None,
                state,
                (reason or "").strip()[:240] or None,
                (source or "manual").strip()[:40],
                now,
                now,
            ),
        )
    return get_gmail_sender(address)


def delete_gmail_sender(sender: str) -> bool:
    address = _normalize_sender_address(sender)
    if not address:
        return False
    with connect() as connection:
        result = connection.execute("DELETE FROM gmail_senders WHERE sender = ?", (address,))
    return result.rowcount > 0


def gmail_allowlist_summary() -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT state, COUNT(*) AS count FROM gmail_senders GROUP BY state"
        ).fetchall()
    state_counts = {str(row["state"]): int(row["count"]) for row in rows}
    return {
        "sender_count": sum(state_counts.values()),
        "approved_count": state_counts.get("approved", 0),
        "candidate_count": state_counts.get("candidate", 0),
        "rejected_count": state_counts.get("rejected", 0),
    }


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
    title = tight_brief_title(str(digest["name"] or "Morning Brief"))
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
        "gmail_message_id": row["message_id"],
        "reddit_thread_id": None,
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
            metadata.get("podcast_episode_id") or metadata.get("thread_id"),
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
            metadata.get("podcast_episode_id") or metadata.get("thread_id"),
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
        if (payload.metadata or {}).get("search_provider") == "google_news_rss":
            return "Article candidate discovered through Google News RSS. Article fetch and enrichment are pending."
        return "Extracted from an approved Gmail newsletter. Article fetch and enrichment are pending."
    return "Newsletter body ingested from an approved Gmail sender."


def _editor_note_for_result(result: ArticleFetchResult) -> str:
    if result.payload.source_type == "reddit_thread":
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        return f"Legacy discussion selected from {result.payload.source_name}.{score}"
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
    if (result.payload.metadata or {}).get("search_provider") == "google_news_rss":
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        if result.fetched:
            return f"Fetched from a link discovered through Google News RSS.{score}"
        return f"Lower-confidence Google News fallback because article fetch returned: {result.status}."
    if result.fetched:
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        return f"Fetched from a link discovered in an approved Gmail newsletter.{score}"
    return f"Lower-confidence fallback from newsletter context because article fetch returned: {result.status}."


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
    "route_name",
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
        "route_name": _nullable_str(metric.get("route_name")),
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


def clear_inference_metrics_for_run(inference_run_id: str | None) -> int:
    run_id = str(inference_run_id or "").strip()
    if not run_id:
        return 0
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM inference_metrics WHERE run_id = ?",
            (run_id,),
        )
        return int(cursor.rowcount or 0)


def has_served_undated_item(topic_id: str, item_key: str) -> bool:
    if not topic_id or not item_key:
        return False
    with connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM served_undated_items WHERE topic_id = ? AND item_key = ? LIMIT 1",
            (topic_id, item_key),
        ).fetchone()
    return row is not None


def record_served_undated_items(topic_id: str, items: list[dict[str, str]]) -> int:
    if not topic_id or not items:
        return 0
    now = utc_now()
    written = 0
    with connect() as connection:
        for item in items:
            item_key = str(item.get("item_key") or "").strip()
            if not item_key:
                continue
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO served_undated_items (
                  id, topic_id, item_key, title, source_name, url, first_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    topic_id,
                    item_key,
                    _nullable_str(item.get("title")),
                    _nullable_str(item.get("source_name")),
                    _nullable_str(item.get("url")),
                    now,
                ),
            )
            written += int(cursor.rowcount or 0)
    return written


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
            str(row.get("route_name") or row.get("mode") or "default"),
            str(row.get("model") or "unknown"),
            _nullable_str(row.get("backend")),
            _nullable_str(row.get("model_tag")),
        )
        route_groups.setdefault(key, []).append(row)

    route_summaries = []
    for (route_name, model, backend, model_tag), group in route_groups.items():
        durations = sorted(_model_service_ms(row) for row in group if row.get("total_ms") is not None)
        queue_waits = sorted(int(row["queue_wait_ms"]) for row in group if row.get("queue_wait_ms") is not None)
        prompt_tokens = [int(row["prompt_tokens"]) for row in group if row.get("prompt_tokens") is not None]
        completion_tokens = [int(row["completion_tokens"]) for row in group if row.get("completion_tokens") is not None]
        token_rates = [float(row["tokens_per_sec"]) for row in group if row.get("tokens_per_sec") is not None]
        total_tokens = [
            int(row["prompt_tokens"]) + int(row["completion_tokens"])
            for row in group
            if row.get("prompt_tokens") is not None and row.get("completion_tokens") is not None
        ]
        success = sum(1 for row in group if row["status"] == "success")
        route_summaries.append(
            {
                "route_name": route_name,
                "model": model,
                "backend": backend,
                "model_tag": model_tag,
                "record_count": len(group),
                "success_count": success,
                "failure_count": len(group) - success,
                "avg_total_ms": _average(durations),
                "p95_total_ms": _percentile(durations, 95),
                "avg_queue_wait_ms": _average(queue_waits),
                "avg_prompt_tokens": _average(prompt_tokens),
                "avg_completion_tokens": _average(completion_tokens),
                "avg_tokens_per_sec": _average(token_rates),
                "avg_total_tokens": _average(total_tokens),
                "fallback_rate": _rate(sum(1 for row in group if row.get("fallback_triggered")), len(group)),
            }
        )

    route_summaries.sort(key=lambda row: (row["route_name"], -row["record_count"]))
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
        exploration_rows = connection.execute(
            """
            SELECT url AS canonical_url, url AS original_url, source_name AS domain, signal, COUNT(*) AS signal_count
            FROM exploration_feedback
            WHERE topic_id = ?
            GROUP BY url, source_name, signal
            """,
            (digest_id,),
        ).fetchall()

    exact_signals: dict[str, float] = {}
    domain_signals: dict[str, float] = {}
    for row in [*rows, *exploration_rows]:
        value = int(row["signal_count"] or 0)
        sig = row["signal"]

        if sig in ("love", "up"):
            delta = value * 1.5
        elif sig == "like":
            delta = float(value)
        elif sig == "click":
            delta = value * 0.3
        elif sig in ("dislike", "down"):
            delta = float(-value)
        else:
            delta = 0.0

        for url in (row["canonical_url"], row["original_url"]):
            key = _url_match_key(url)
            if key:
                exact_signals[key] = exact_signals.get(key, 0.0) + delta
        domain = str(row["domain"] or "")
        if domain:
            domain_signals[domain] = domain_signals.get(domain, 0.0) + delta

    feedback_profile = get_feedback_profile(digest_id)
    liked_keywords = {kw.lower() for kw in feedback_profile.get("liked_keywords", [])}
    disliked_keywords = {kw.lower() for kw in feedback_profile.get("disliked_keywords", [])}

    adjusted: list[ArticleFetchResult] = []
    for result in article_results:
        url_key = _url_match_key(result.canonical_url or result.final_url or result.original_url)
        domain = result.domain or _domain(result.final_url or result.original_url) or result.payload.source_name
        source_weight = weights.get(domain, 1.0)
        exact_delta = max(-0.25, min(0.25, exact_signals.get(url_key, 0.0) * 0.08)) if url_key else 0.0
        domain_delta = max(-0.12, min(0.12, domain_signals.get(domain, 0.0) * 0.02)) if domain else 0.0

        # Keyword/tag biasing
        kw_boost = 0.0
        kw_suppress = 0.0
        res_kws = {k.lower() for k in result.keywords}
        title_words = set(re.findall(r"\w+", (result.title or "").lower()))

        for kw in liked_keywords:
            if kw in res_kws or kw in title_words:
                kw_boost += 0.02
        kw_boost = min(0.10, kw_boost)

        for kw in disliked_keywords:
            if kw in res_kws or kw in title_words:
                kw_suppress += 0.04
        kw_suppress = min(0.15, kw_suppress)

        keyword_delta = kw_boost - kw_suppress

        adjusted_score = max(0.0, min(1.0, (result.link_score * source_weight) + exact_delta + domain_delta + keyword_delta))
        adjusted.append(replace(result, link_score=round(adjusted_score, 3)))
    return adjusted


def record_feedback(*, issue_id: str, url: str, signal: str) -> dict[str, Any] | None:
    valid_signals = {"up", "down", "click", "love", "like", "neutral", "dislike"}
    if signal not in valid_signals:
        raise ValueError(f"Feedback signal must be one of {valid_signals}")

    url_key = _url_match_key(url)
    if not url_key:
        return None
    now = utc_now()
    with connect() as connection:
        issue = connection.execute(
            "SELECT id, run_id, digest_id FROM digest_issues WHERE id = ?",
            (issue_id,),
        ).fetchone()

        article_row = connection.execute(
            "SELECT id, keywords, content_type, domain FROM articles WHERE canonical_url = ? OR original_url = ?",
            (url, url)
        ).fetchone()

        tags_json = None
        source_type = None
        domain = _domain(url)
        if article_row:
            source_type = article_row["content_type"]
            if article_row["domain"]:
                domain = article_row["domain"]
            kw = article_row["keywords"]
            if kw:
                if kw.startswith("["):
                    tags_json = kw
                else:
                    tags_json = json.dumps([k.strip().lower() for k in kw.split(",") if k.strip()], ensure_ascii=False)

        adapter = None
        if source_type:
            adapter = {
                "gmail": "gmail",
                "podcast": "podcasts",
                "video": "youtube",
                "foreign_web": "foreign_media",
                "market": "markets",
                "collection": "collections",
            }.get(source_type, "web_search")

        if issue is None:
            exploration = connection.execute(
                "SELECT exploration_id, topic_id FROM explorations WHERE exploration_id = ?",
                (issue_id,),
            ).fetchone()
            if exploration is None:
                return None
            feedback_id = new_id()
            connection.execute(
                """
                INSERT INTO exploration_feedback
                (id, exploration_id, topic_id, url, source_name, signal, created_at, source_type, adapter, tags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    exploration["exploration_id"],
                    exploration["topic_id"],
                    url,
                    domain,
                    signal,
                    now,
                    source_type,
                    adapter,
                    tags_json,
                ),
            )
            if domain:
                _update_source_weight(connection, str(exploration["topic_id"]), domain, signal, now)
            return {
                "id": feedback_id,
                "issue_id": issue_id,
                "signal": signal,
                "url": url,
                "source_name": domain,
                "created_at": now,
            }

        rows = connection.execute(
            """
            SELECT di.id AS digest_item_id, di.digest_id, a.id AS article_id,
                   a.canonical_url, a.original_url, a.domain, a.publisher, a.keywords, a.content_type
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

        if matched["content_type"]:
            source_type = matched["content_type"]
            adapter = {
                "gmail": "gmail",
                "podcast": "podcasts",
                "video": "youtube",
                "foreign_web": "foreign_media",
                "market": "markets",
                "collection": "collections",
            }.get(source_type, "web_search")
        if matched["keywords"]:
            kw = matched["keywords"]
            if kw.startswith("["):
                tags_json = kw
            else:
                tags_json = json.dumps([k.strip().lower() for k in kw.split(",") if k.strip()], ensure_ascii=False)

        feedback_id = new_id()
        connection.execute(
            """
            INSERT INTO feedback
            (id, digest_item_id, article_id, digest_id, signal, created_at, url, source_type, source_name, adapter, tags_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_id,
                matched["digest_item_id"],
                matched["article_id"],
                issue["digest_id"],
                signal,
                now,
                url,
                source_type,
                matched["domain"] or matched["publisher"] or domain,
                adapter,
                tags_json,
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
        "source_name": source_name or domain,
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
    if signal == "love":
        delta = 0.06
    elif signal in ("like", "up"):
        delta = 0.04
    elif signal == "click":
        delta = 0.01
    elif signal in ("dislike", "down"):
        delta = -0.06
    else:
        delta = 0.0
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
