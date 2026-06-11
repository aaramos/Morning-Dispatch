from __future__ import annotations

from pathlib import Path
from typing import Any


from .core import (
    connect,
    new_id,
    row_to_dict,
    utc_now,
    _nullable_str,
    _pacific_date_key,
)

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
