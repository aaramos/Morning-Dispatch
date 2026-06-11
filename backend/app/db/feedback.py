from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.services.brief_renderer import _domain

from .core import (
    connect,
    new_id,
    utc_now,
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
