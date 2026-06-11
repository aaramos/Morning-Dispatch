from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.app.core.config import get_settings

from .core import (
    connect,
    logger,
    new_id,
    utc_now,
)

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
