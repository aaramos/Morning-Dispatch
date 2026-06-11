from __future__ import annotations

import json
import sqlite3
from typing import Any


from .core import (
    connect,
    new_id,
    utc_now,
)

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
