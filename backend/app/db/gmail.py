from __future__ import annotations

import json
import sqlite3
from typing import Any


from .core import (
    connect,
    new_id,
    utc_now,
)

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
