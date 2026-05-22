from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def connect_db(db_path: str) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def get_watermark(db_path: str, digest_id: str, source_key: str) -> dict[str, str | None] | None:
    with connect_db(db_path) as connection:
        row = connection.execute(
            """
            SELECT last_fetched, last_id
            FROM source_watermarks
            WHERE digest_id = ? AND source_key = ?
            """,
            (digest_id, source_key),
        ).fetchone()
    return dict(row) if row else None


def upsert_watermark(
    db_path: str,
    digest_id: str,
    source_key: str,
    last_fetched: str,
    last_id: str | None,
) -> None:
    with connect_db(db_path) as connection:
        connection.execute(
            """
            INSERT INTO source_watermarks (digest_id, source_key, last_fetched, last_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(digest_id, source_key) DO UPDATE SET
                last_fetched = excluded.last_fetched,
                last_id = excluded.last_id
            """,
            (digest_id, source_key, last_fetched, last_id),
        )
