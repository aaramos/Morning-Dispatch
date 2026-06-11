from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from typing import Any

from backend.agents.librarian.articles import ArticleFetchResult

from .core import (
    MODEL_ENRICHMENT_CACHE_VERSION,
    connect,
    new_id,
    row_to_dict,
    utc_now,
    _decode_keywords,
)
from .digests import _article_row_to_fetch_result

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
