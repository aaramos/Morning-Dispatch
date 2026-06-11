from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from backend.agents.agentic import AgentDecision
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.editor import build_issue_snapshot
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.services.brief_title import tight_brief_title
from backend.app.services.brief_renderer import (
    _clean_newsletter_text,
    _domain,
    _summary_for_payload,
    _title_for_payload,
    _truncate_text,
    render_ingested_issue,
    render_placeholder_issue,
)

from .core import (
    connect,
    get_default_profile_id,
    new_id,
    row_to_dict,
    utc_now,
    _decode_keywords,
    _nullable_float,
)
from .metrics import _build_digest_stats, _insert_agent_decisions

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
