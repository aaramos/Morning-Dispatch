from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from html import escape, unescape
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from backend.agents.agentic import AgentDecision
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.editor import build_issue_snapshot
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.core.config import ensure_runtime_dirs, get_settings
from backend.app.db.schema import SCHEMA_SQL

MODEL_ENRICHMENT_CACHE_VERSION = "librarian-v1"
RAW_URL_RE = re.compile(r"https?://[^\s<>)\"']+", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]{1,180})\]\((https?://[^)\s]+)[^)]*\)", re.IGNORECASE)
IMAGE_PLACEHOLDER_RE = re.compile(r"(?:[-–—]{2,}\s*)?View image:\s*\([^)]*(?:\)|$)\s*(?:Caption:\s*)?", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff\u00ad]+")
REFERENCE_MARK_RE = re.compile(r"\[\d+\]")
SEPARATOR_RE = re.compile(r"(?:[-–—]\s*){3,}")


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
        connection.executescript(SCHEMA_SQL)
        _ensure_digest_run_metric_columns(connection)
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
    }
    for column, definition in columns.items():
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE digest_runs ADD COLUMN {column} {definition}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digest_runs_inference_run_id ON digest_runs(inference_run_id)"
    )


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


def create_placeholder_run(digest_id: str) -> dict[str, Any] | None:
    digest = get_digest(digest_id)
    if digest is None:
        return None

    now = utc_now()
    run_id = new_id()
    issue_id = new_id()
    title = f"{digest['name']} - Preview Issue"
    snapshot = "Pipeline scaffold is running. Gmail ingestion and article fetching are the next build slices."
    html = render_placeholder_issue(title, snapshot)

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
    agent_decisions: list[AgentDecision] | None = None,
) -> dict[str, Any]:
    article_results = article_results or []
    now = utc_now()
    run_id = new_id()
    issue_id = new_id()
    digest_id = str(digest["id"])
    title = f"{digest['name']} - Gmail Issue"
    snapshot = ingested_snapshot(payloads, configured_source_count, article_results)
    html = render_ingested_issue(title, snapshot, payloads, article_results, lookback_hours)
    lookback_days = max(1, math.ceil(lookback_hours / 24))
    body_payloads = [payload for payload in payloads if payload.source_type == "gmail"]
    link_payload_count = sum(1 for payload in payloads if payload.source_type == "gmail_link")
    item_count = len(body_payloads) + len(article_results)
    failed_count = sum(1 for result in article_results if not result.fetched)
    fallback_count = sum(1 for result in article_results if result.content_type == "fallback_snippet")
    fetched_article_count = sum(1 for result in article_results if result.fetched)

    with connect() as connection:
        connection.execute(
            """
            INSERT INTO digest_runs (
              id, digest_id, inference_run_id, run_at, lookback_days, item_count, failed_count,
              fallback_count, newsletter_count, link_count, fetched_article_count,
              model_cache_hit_count, model_cache_miss_count, model_cache_write_count,
              duration_seconds, trigger, cold_start, partial, status, snapshot, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'completed', ?, ?)
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
    fetched_article_count = sum(1 for result in article_results if result.fetched)
    attempted_count = len(article_results)
    if configured_source_count == 0:
        return "No Gmail newsletter sources are configured for this digest."
    if not payloads:
        return f"No matching newsletters found across {configured_source_count} configured Gmail source(s)."
    if attempted_count:
        return build_issue_snapshot(body_count, configured_source_count, article_results)
    return (
        f"Fetched {body_count} newsletter body/bodies and {link_count} linked item(s) "
        f"from {configured_source_count} configured Gmail source(s)."
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


def render_ingested_issue(
    title: str,
    snapshot: str,
    payloads: list[NormalizedPayload],
    article_results: list[ArticleFetchResult] | None,
    lookback_hours: int,
) -> str:
    article_results = article_results or []
    body_payloads = [payload for payload in payloads if payload.source_type == "gmail"]
    fetched_articles = [result for result in article_results if result.fetched and result.tier != "dropped"]
    lead_article = next((result for result in fetched_articles if result.tier == "lead"), None)
    main_articles = [result for result in fetched_articles if result is not lead_article and result.tier == "main"]
    lower_confidence_articles = [result for result in fetched_articles if result.tier == "lower_confidence"]
    unresolved_articles = [result for result in article_results if not result.fetched and result.tier != "dropped"]
    displayed_unresolved = unresolved_articles[:8]
    hidden_article_count = max(0, len(fetched_articles) - len(main_articles) - len(lower_confidence_articles) - (1 if lead_article else 0))

    newsletter_html = "\n".join(_render_newsletter_item(payload) for payload in body_payloads[:8])
    lead_html = _render_article_card(lead_article, variant="lead") if lead_article else ""
    section_html = _render_article_sections(main_articles)
    lower_html = "\n".join(_render_article_card(result, variant="compact") for result in lower_confidence_articles[:8])
    unresolved_html = "\n".join(_render_unresolved_link(result) for result in displayed_unresolved)
    empty_state = ""
    if not payloads:
        empty_state = """
        <section class="empty">
          <strong>No newsletter items were found.</strong>
          Check the source allowlist, Gmail labels, or the digest lookback window.
        </section>
        """
    hidden_html_parts = []
    if hidden_article_count:
        hidden_html_parts.append(f"{hidden_article_count} additional fetched article(s)")
    hidden_html = ""
    if hidden_html_parts:
        hidden_html = f'<p class="more-count">Plus {" and ".join(hidden_html_parts)}.</p>'
    lower_section = ""
    if lower_html:
        lower_section = f"""
        <section class="section lower-confidence">
          <h2>Lower Confidence</h2>
          <div class="article-list">{lower_html}</div>
        </section>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)}</title>
  <style>
    :root {{ color: #171717; background: #f7f3eb; }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
    body {{ margin: 0; font-family: Georgia, 'Times New Roman', serif; color: #171717; background: #f7f3eb; }}
    main {{ width: min(1120px, 100%); margin: 0 auto; padding: 44px 24px 64px; }}
    header {{ border-bottom: 3px solid #171717; padding-bottom: 18px; margin-bottom: 28px; }}
    h1 {{ font-size: clamp(2.4rem, 7vw, 5.4rem); line-height: .9; margin: 0; letter-spacing: 0; }}
    h2 {{ font: 800 0.9rem Arial, sans-serif; letter-spacing: .08em; text-transform: uppercase; margin: 0 0 16px; }}
    h3 {{ font-size: 1.35rem; line-height: 1.15; margin: 0 0 8px; }}
    h1, h2, h3, p, a, .meta {{ overflow-wrap: anywhere; }}
    a {{ color: #173f63; text-decoration-thickness: 1px; text-underline-offset: 3px; }}
    img, video, iframe, table {{ max-width: 100%; }}
    .date {{ margin-top: 12px; font: 700 0.8rem Arial, sans-serif; text-transform: uppercase; }}
    .snapshot {{ font-size: 1.28rem; line-height: 1.45; max-width: 820px; margin-bottom: 28px; }}
    .meta {{ font: 700 0.74rem Arial, sans-serif; color: #5f675f; text-transform: uppercase; overflow-wrap: anywhere; }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr); gap: 32px; align-items: start; }}
    .section {{ border-top: 1px solid #171717; padding-top: 18px; }}
    .section + .section {{ margin-top: 32px; }}
    .grid, .section, .article-card, .newsletter, .link-item {{ min-width: 0; }}
    .article-card {{ padding: 0 0 22px; margin-bottom: 22px; border-bottom: 1px solid #d4cbbd; }}
    .article-card p {{ font-size: 1rem; line-height: 1.55; margin: 10px 0 0; }}
    .article-card a {{ color: inherit; }}
    .article-card.lead {{ padding-bottom: 28px; margin-bottom: 28px; border-bottom: 3px solid #171717; }}
    .article-card.lead h3 {{ font-size: clamp(2rem, 5vw, 3.8rem); line-height: .95; max-width: 850px; }}
    .article-card.lead p {{ font-size: 1.15rem; line-height: 1.55; max-width: 850px; }}
    .article-section {{ margin-bottom: 26px; }}
    .article-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; }}
    .article-list .article-card:last-child, .article-grid .article-card:last-child {{ margin-bottom: 0; }}
    .score {{ display: inline-block; margin-left: 8px; color: #7a4f16; }}
    .keywords {{ margin-top: 10px; font: 700 .72rem Arial, sans-serif; color: #6a746e; text-transform: uppercase; }}
    .newsletter {{ padding: 0 0 20px; margin-bottom: 20px; border-bottom: 1px solid #d4cbbd; }}
    .newsletter p {{ font-size: 1rem; line-height: 1.55; margin: 10px 0 0; }}
    .link-item {{ display: grid; gap: 5px; padding: 12px 0; border-bottom: 1px solid #d4cbbd; }}
    details.source-notes {{ margin-top: 28px; border-top: 1px solid #171717; padding-top: 16px; }}
    details.source-notes summary {{ cursor: pointer; font: 800 .9rem Arial, sans-serif; text-transform: uppercase; }}
    .empty {{ margin-top: 32px; padding: 24px; border: 1px dashed #b9ae9d; font: 1rem Arial, sans-serif; background: #fffaf0; }}
    .more-count {{ font: 700 .9rem Arial, sans-serif; color: #5f675f; margin-top: 16px; }}
    @media (max-width: 820px) {{ .grid, .article-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{escape(title)}</h1>
      <div class="date">Gmail digest · Last {lookback_hours} hours</div>
    </header>
    <p class="snapshot">{escape(snapshot)}</p>
    {empty_state}
    <div class="grid">
      <section class="section">
        <h2>Fetched Articles</h2>
        {lead_html}
        {section_html or '<p class="meta">No article pages were fetched yet.</p>'}
        {lower_section}
        {hidden_html}
      </section>
      <section class="section">
        <h2>Unresolved Links</h2>
        {unresolved_html or '<p class="meta">No attempted links failed.</p>'}
        <details class="source-notes">
          <summary>Newsletter Briefs</summary>
          {newsletter_html or '<p class="meta">No newsletter bodies were available.</p>'}
        </details>
      </section>
    </div>
  </main>
</body>
</html>"""


def render_placeholder_issue(title: str, snapshot: str) -> str:
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
    h1 {{ font-size: clamp(2.5rem, 8vw, 5rem); line-height: .9; margin: 0; letter-spacing: 0; }}
    h1, p {{ overflow-wrap: anywhere; }}
    .date {{ margin-top: 12px; font: 600 0.8rem Arial, sans-serif; text-transform: uppercase; }}
    .snapshot {{ font-size: 1.3rem; line-height: 1.5; max-width: 720px; }}
    .empty {{ margin-top: 32px; padding-top: 24px; border-top: 1px solid #c8bfae; font: 1rem Arial, sans-serif; }}
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
                "ok" if result.fetched else "needs_review",
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
            "ok" if result.fetched else "needs_review",
            now,
            now,
        ),
    )
    return article_id


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
    status = str(row["fetch_status"] or "fetched")
    metadata = {
        "article_id": str(row["article_id"]),
        "gmail_message_id": row["message_id"],
        "sender_email": row["sender_email"],
        "link_text": row["link_text"],
    }
    payload = NormalizedPayload(
        source_type=str(row["discovery_source_type"] or "stored_article"),
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
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            discovery_id,
            article_id,
            payload.source_type,
            payload.source_name,
            metadata.get("sender_email") or metadata.get("sender"),
            metadata.get("gmail_message_id"),
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
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            discovery_id,
            article_id,
            result.payload.source_type,
            result.payload.source_name,
            metadata.get("sender_email") or metadata.get("sender"),
            metadata.get("gmail_message_id"),
            result.payload.published_at,
            metadata.get("link_text") or result.title,
            str(metadata.get("parent_subject") or metadata.get("subject") or ""),
            now,
        ),
    )
    return discovery_id


def _render_newsletter_item(payload: NormalizedPayload) -> str:
    subject = _title_for_payload(payload)
    sender = payload.source_name or "Gmail"
    snippet = _summary_for_payload(payload, max_chars=700)
    published = _format_issue_date(payload.published_at)
    return f"""
      <article class="newsletter">
        <div class="meta">{escape(sender)} · {escape(published)}</div>
        <h3>{escape(subject)}</h3>
        <p>{escape(snippet)}</p>
      </article>
    """


def _render_article_sections(results: list[ArticleFetchResult]) -> str:
    grouped: dict[str, list[ArticleFetchResult]] = {}
    for result in results:
        grouped.setdefault(result.section or "Noteworthy", []).append(result)

    sections: list[str] = []
    for section, section_results in grouped.items():
        cards = "\n".join(_render_article_card(result, variant="compact") for result in section_results[:6])
        sections.append(
            f"""
            <section class="article-section">
              <h2>{escape(section)}</h2>
              <div class="article-grid">{cards}</div>
            </section>
            """
        )
    return "\n".join(sections)


def _render_article_card(result: ArticleFetchResult | None, *, variant: str = "compact") -> str:
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
    return f"""
      <article class="{card_class}">
        <div class="meta">{meta}{score}</div>
        <h3><a href="{escape(url, quote=True)}" target="_blank" rel="noreferrer">{escape(title)}</a></h3>
        <p>{escape(summary)}</p>
        {keyword_html}
      </article>
    """


def _render_unresolved_link(result: ArticleFetchResult) -> str:
    url = result.final_url or result.original_url
    domain = result.domain or _domain(url) or "link"
    reason = result.error or result.status
    published = _format_article_date(result.payload.published_at)
    meta_parts = [domain]
    if published:
        meta_parts.append(published)
    meta_parts.extend([result.status, reason])
    meta = " · ".join(escape(part) for part in meta_parts)
    return f"""
      <article class="link-item">
        <a href="{escape(url, quote=True)}" target="_blank" rel="noreferrer">{escape(result.title)}</a>
        <span class="meta">{meta}</span>
      </article>
    """


def _section_for_payload(payload: NormalizedPayload) -> str:
    return "Newsletter" if payload.source_type == "gmail" else "Discovered Link"


def _editor_note_for_payload(payload: NormalizedPayload) -> str:
    if payload.source_type == "gmail_link":
        return "Extracted from an approved Gmail newsletter. Article fetch and enrichment are pending."
    return "Newsletter body ingested from an approved Gmail sender."


def _editor_note_for_result(result: ArticleFetchResult) -> str:
    if result.fetched:
        score = f" Relevance score: {int((result.relevance_score or 0) * 100)}%." if result.relevance_score else ""
        return f"Fetched from a link discovered in an approved Gmail newsletter.{score}"
    return f"Lower-confidence fallback from newsletter context because article fetch returned: {result.status}."


def _title_for_payload(payload: NormalizedPayload) -> str:
    metadata = payload.metadata or {}
    link_text = metadata.get("link_text")
    if link_text:
        return str(link_text)
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
            paragraph.string = cleaned
            changed = True
    for paragraph in soup.select("article.article-card p"):
        cleaned = _clean_newsletter_text(paragraph.get_text(" ", strip=True))
        if cleaned != paragraph.get_text(" ", strip=True):
            paragraph.string = cleaned
            changed = True
    return str(soup) if changed else html


def _clean_newsletter_text(value: str | None) -> str:
    text = unescape(value or "")
    text = ZERO_WIDTH_RE.sub(" ", text)
    text = IMAGE_PLACEHOLDER_RE.sub(" ", text)
    text = MARKDOWN_LINK_RE.sub(lambda match: f" {match.group(1)} ", text)
    text = RAW_URL_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = REFERENCE_MARK_RE.sub(" ", text)
    text = SEPARATOR_RE.sub(" ", text)
    text = text.replace("**", " ").replace("__", " ").replace("^^", " ").replace("^", " ").replace("`", " ")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -|")


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
            WHERE digest_id = ? AND COALESCE(trigger, '') != 'controlled_verification'
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
              ad.message_id, ad.link_text
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
        "status": str(metric.get("status") or "model_error"),
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
    recent = records[:20]
    return {
        "record_count": total_count,
        "success_count": success_count,
        "failure_count": total_count - success_count,
        "latest_ts": records[0]["ts"] if records else None,
        "status_counts": status_counts,
        "models": model_summaries,
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


def get_latest_issue(digest_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM digest_issues
            WHERE digest_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (digest_id,),
        ).fetchone()
    return row_to_dict(row)


def get_issue(issue_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM digest_issues WHERE id = ?", (issue_id,)).fetchone()
    return row_to_dict(row)
