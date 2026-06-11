from __future__ import annotations

import json
import logging
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.app.core.config import ensure_runtime_dirs, get_settings
from backend.app.db.schema import SCHEMA_SQL

logger = logging.getLogger(__name__)

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

_RUNTIME_DIRS_READY: set[Path] = set()

def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

def new_id() -> str:
    return str(uuid.uuid4())

def database_path() -> Path:
    return get_settings().database_path

@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    if settings.data_dir not in _RUNTIME_DIRS_READY:
        facade = sys.modules.get("backend.app.db.database")
        runtime_dir_ensurer = getattr(facade, "ensure_runtime_dirs", ensure_runtime_dirs) if facade else ensure_runtime_dirs
        runtime_dir_ensurer(settings)
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

def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
