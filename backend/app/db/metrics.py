from __future__ import annotations

import json
import sqlite3
from typing import Any

from backend.agents.agentic import AgentDecision
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.services.brief_renderer import _nullable_int, _truncate_text

from .core import (
    INFERENCE_METRIC_STATUSES,
    connect,
    new_id,
    utc_now,
    _average,
    _empty_token_summary,
    _json_dict,
    _model_service_ms,
    _normalize_stage_seconds,
    _nullable_float,
    _nullable_str,
    _percentile,
    _rate,
)

def build_digest_stats(
    *,
    configured_source_count: int,
    newsletter_count: int,
    link_count: int,
    podcast_episode_count: int = 0,
    article_results: list[ArticleFetchResult],
    duration_seconds: float | None,
    inference_run_id: str | None,
    stage_seconds: dict[str, float] | None,
) -> dict[str, Any]:
    active_results = [result for result in article_results if result.tier != "dropped"]
    included_count = sum(1 for result in active_results if result.fetched)
    unresolved_count = sum(1 for result in active_results if not result.fetched)
    dropped_count = sum(1 for result in article_results if result.tier == "dropped")
    token_summary = inference_token_summary(inference_run_id) if inference_run_id else _empty_token_summary()
    return {
        "source_count": max(0, int(configured_source_count or 0)),
        "newsletter_count": max(0, int(newsletter_count or 0)),
        "link_count": max(0, int(link_count or 0)),
        "podcast_episode_count": max(0, int(podcast_episode_count or 0)),
        "article_candidate_count": len(article_results),
        "included_article_count": included_count,
        "unresolved_count": unresolved_count,
        "dropped_count": dropped_count,
        "prompt_tokens": token_summary["prompt_tokens"],
        "completion_tokens": token_summary["completion_tokens"],
        "total_tokens": token_summary["total_tokens"],
        "model_call_count": token_summary["model_call_count"],
        "model_success_count": token_summary["model_success_count"],
        "model_failure_count": token_summary["model_failure_count"],
        "completion_unavailable_count": token_summary["completion_unavailable_count"],
        "model_usage": inference_model_usage_summary(inference_run_id),
        "processing_seconds": _nullable_float(duration_seconds),
        "stage_seconds": _normalize_stage_seconds(stage_seconds),
    }

def _build_digest_stats(
    *,
    configured_source_count: int,
    newsletter_count: int,
    link_count: int,
    podcast_episode_count: int = 0,
    article_results: list[ArticleFetchResult],
    duration_seconds: float | None,
    inference_run_id: str | None,
    stage_seconds: dict[str, float] | None,
) -> dict[str, Any]:
    return build_digest_stats(
        configured_source_count=configured_source_count,
        newsletter_count=newsletter_count,
        link_count=link_count,
        podcast_episode_count=podcast_episode_count,
        article_results=article_results,
        duration_seconds=duration_seconds,
        inference_run_id=inference_run_id,
        stage_seconds=stage_seconds,
    )

def inference_token_summary(inference_run_id: str | None) -> dict[str, int]:
    if not inference_run_id:
        return _empty_token_summary()
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
              COUNT(*) AS model_call_count,
              COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS model_success_count,
              COALESCE(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END), 0) AS model_failure_count,
              COALESCE(SUM(CASE WHEN completion_tokens IS NULL THEN 1 ELSE 0 END), 0) AS completion_unavailable_count,
              COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
              COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM inference_metrics
            WHERE run_id = ?
            """,
            (inference_run_id,),
        ).fetchone()
    prompt_tokens = int(row["prompt_tokens"] or 0) if row else 0
    completion_tokens = int(row["completion_tokens"] or 0) if row else 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "model_call_count": int(row["model_call_count"] or 0) if row else 0,
        "model_success_count": int(row["model_success_count"] or 0) if row else 0,
        "model_failure_count": int(row["model_failure_count"] or 0) if row else 0,
        "completion_unavailable_count": int(row["completion_unavailable_count"] or 0) if row else 0,
    }

def inference_model_usage_summary(inference_run_id: str | None) -> list[dict[str, Any]]:
    if not inference_run_id:
        return []
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
              model,
              mode,
              COUNT(*) AS call_count,
              COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS success_count,
              COALESCE(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END), 0) AS failure_count
            FROM inference_metrics
            WHERE run_id = ?
            GROUP BY model, mode
            ORDER BY call_count DESC, model ASC, mode ASC
            """,
            (inference_run_id,),
        ).fetchall()
    return [
        {
            "model": str(row["model"] or "unknown"),
            "mode": str(row["mode"] or "single"),
            "call_count": int(row["call_count"] or 0),
            "success_count": int(row["success_count"] or 0),
            "failure_count": int(row["failure_count"] or 0),
        }
        for row in rows
    ]

def latest_digest_stats() -> dict[str, Any]:
    latest = _latest_run_row()
    if latest is None:
        return {
            "run_id": None,
            "generated_at": None,
            "source_count": 0,
            "newsletter_count": 0,
            "link_count": 0,
            "podcast_episode_count": 0,
            "article_candidate_count": 0,
            "included_article_count": 0,
            "unresolved_count": 0,
            "dropped_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "model_call_count": 0,
            "processing_seconds": None,
            "stage_seconds": {},
        }
    stats = _digest_stats_from_run_row(latest)
    stats["run_id"] = latest["id"]
    stats["generated_at"] = latest["completed_at"] or latest["run_at"]
    return stats

def _digest_stats_from_run_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    metadata = _json_dict(record.get("run_metadata"))
    stats = metadata.get("digest_stats") if isinstance(metadata.get("digest_stats"), dict) else {}
    if stats:
        normalized = {
            "source_count": int(stats.get("source_count") or 0),
            "newsletter_count": int(stats.get("newsletter_count") or 0),
            "link_count": int(stats.get("link_count") or 0),
            "podcast_episode_count": int(stats.get("podcast_episode_count") or 0),
            "article_candidate_count": int(stats.get("article_candidate_count") or 0),
            "included_article_count": int(stats.get("included_article_count") or 0),
            "unresolved_count": int(stats.get("unresolved_count") or 0),
            "dropped_count": int(stats.get("dropped_count") or 0),
            "prompt_tokens": int(stats.get("prompt_tokens") or 0),
            "completion_tokens": int(stats.get("completion_tokens") or 0),
            "total_tokens": int(stats.get("total_tokens") or 0),
            "model_call_count": int(stats.get("model_call_count") or 0),
            "processing_seconds": _nullable_float(stats.get("processing_seconds")),
            "stage_seconds": _normalize_stage_seconds(stats.get("stage_seconds")),
        }
        return normalized

    token_summary = inference_token_summary(record.get("inference_run_id"))
    included = int(record.get("fetched_article_count") or 0)
    unresolved = int(record.get("failed_count") or 0)
    return {
        "source_count": 0,
        "newsletter_count": int(record.get("newsletter_count") or 0),
        "link_count": int(record.get("link_count") or 0),
        "podcast_episode_count": 0,
        "article_candidate_count": included + unresolved,
        "included_article_count": included,
        "unresolved_count": unresolved,
        "dropped_count": 0,
        "prompt_tokens": token_summary["prompt_tokens"],
        "completion_tokens": token_summary["completion_tokens"],
        "total_tokens": token_summary["total_tokens"],
        "model_call_count": token_summary["model_call_count"],
        "processing_seconds": _nullable_float(record.get("duration_seconds")),
        "stage_seconds": {},
    }

def record_inference_metric(metric: dict[str, Any]) -> str:
    metric_id = str(metric.get("id") or new_id())
    status = str(metric.get("status") or "model_error")
    if status not in INFERENCE_METRIC_STATUSES:
        status = "model_error"
    row = {
        "id": metric_id,
        "run_id": str(metric.get("run_id") or "manual"),
        "article_id": str(metric.get("article_id") or "unknown"),
        "ts": str(metric.get("ts") or utc_now()),
        "model": str(metric.get("model") or "unknown"),
        "model_tag": _nullable_str(metric.get("model_tag")),
        "quantization": _nullable_str(metric.get("quantization")),
        "backend": _nullable_str(metric.get("backend")),
        "route_name": _nullable_str(metric.get("route_name")),
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
        "status": status,
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

def clear_inference_metrics_for_run(inference_run_id: str | None) -> int:
    run_id = str(inference_run_id or "").strip()
    if not run_id:
        return 0
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM inference_metrics WHERE run_id = ?",
            (run_id,),
        )
        return int(cursor.rowcount or 0)

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
    route_groups: dict[tuple[str, str, str | None, str | None], list[dict[str, Any]]] = {}
    for row in records:
        key = (
            str(row.get("route_name") or row.get("mode") or "default"),
            str(row.get("model") or "unknown"),
            _nullable_str(row.get("backend")),
            _nullable_str(row.get("model_tag")),
        )
        route_groups.setdefault(key, []).append(row)

    route_summaries = []
    for (route_name, model, backend, model_tag), group in route_groups.items():
        durations = sorted(_model_service_ms(row) for row in group if row.get("total_ms") is not None)
        queue_waits = sorted(int(row["queue_wait_ms"]) for row in group if row.get("queue_wait_ms") is not None)
        prompt_tokens = [int(row["prompt_tokens"]) for row in group if row.get("prompt_tokens") is not None]
        completion_tokens = [int(row["completion_tokens"]) for row in group if row.get("completion_tokens") is not None]
        token_rates = [float(row["tokens_per_sec"]) for row in group if row.get("tokens_per_sec") is not None]
        total_tokens = [
            int(row["prompt_tokens"]) + int(row["completion_tokens"])
            for row in group
            if row.get("prompt_tokens") is not None and row.get("completion_tokens") is not None
        ]
        success = sum(1 for row in group if row["status"] == "success")
        route_summaries.append(
            {
                "route_name": route_name,
                "model": model,
                "backend": backend,
                "model_tag": model_tag,
                "record_count": len(group),
                "success_count": success,
                "failure_count": len(group) - success,
                "avg_total_ms": _average(durations),
                "p95_total_ms": _percentile(durations, 95),
                "avg_queue_wait_ms": _average(queue_waits),
                "avg_prompt_tokens": _average(prompt_tokens),
                "avg_completion_tokens": _average(completion_tokens),
                "avg_tokens_per_sec": _average(token_rates),
                "avg_total_tokens": _average(total_tokens),
                "fallback_rate": _rate(sum(1 for row in group if row.get("fallback_triggered")), len(group)),
            }
        )

    route_summaries.sort(key=lambda row: (row["route_name"], -row["record_count"]))
    recent = records[:20]
    return {
        "record_count": total_count,
        "success_count": success_count,
        "failure_count": total_count - success_count,
        "latest_ts": records[0]["ts"] if records else None,
        "status_counts": status_counts,
        "models": model_summaries,
        "routes": route_summaries,
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

def fetch_failure_breakdown(*, limit: int = 5) -> dict[str, Any]:
    latest = _latest_run_row()
    if latest is None:
        return {"run_id": None, "total_count": 0, "groups": [], "examples": []}

    with connect() as connection:
        rows = connection.execute(
            """
            SELECT di.id AS digest_item_id, a.id AS article_id, a.title, a.canonical_url,
                   a.original_url, a.domain, a.fetch_status, a.quality_flag,
                   di.editor_summary, di.editor_note, ad.newsletter_snippet, ad.link_text
            FROM digest_items di
            JOIN articles a ON a.id = di.article_id
            LEFT JOIN article_discoveries ad ON ad.id = di.discovery_id
            WHERE di.run_id = ?
              AND COALESCE(di.tier, '') != 'source'
              AND COALESCE(a.fetch_status, 'fetched') != 'fetched'
            ORDER BY COALESCE(di.relevance_score, 0) DESC, di.created_at DESC
            """,
            (latest["id"],),
        ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    examples: list[dict[str, Any]] = []
    for row in rows:
        status = str(row["fetch_status"] or "unknown")
        group = groups.setdefault(
            status,
            {
                "status": status,
                "count": 0,
                "fixability": _fetch_fixability(status),
            },
        )
        group["count"] += 1
        if len(examples) < limit:
            examples.append(_failure_example(row))

    return {
        "run_id": latest["id"],
        "run_at": latest["run_at"],
        "digest_id": latest["digest_id"],
        "total_count": len(rows),
        "groups": sorted(groups.values(), key=lambda item: item["count"], reverse=True),
        "examples": examples,
    }

def brief_review(*, limit: int = 8) -> dict[str, Any]:
    latest = _latest_run_row()
    if latest is None:
        return {
            "run_id": None,
            "issue_id": None,
            "generated_at": None,
            "counts": {"included": 0, "unresolved": 0, "dropped": 0, "duplicate": 0, "repaired": 0},
            "included": [],
            "unresolved": [],
            "dropped": [],
            "duplicates": [],
            "repaired": [],
        }

    with connect() as connection:
        issue = connection.execute(
            "SELECT id, created_at FROM digest_issues WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (latest["id"],),
        ).fetchone()
        item_rows = connection.execute(
            """
            SELECT di.tier, di.section, di.relevance_score, di.editor_summary,
                   a.title, a.canonical_url, a.original_url, a.domain, a.fetch_status,
                   a.quality_flag
            FROM digest_items di
            JOIN articles a ON a.id = di.article_id
            WHERE di.run_id = ? AND COALESCE(di.tier, '') != 'source'
            ORDER BY
              CASE di.tier WHEN 'lead' THEN 0 WHEN 'main' THEN 1 WHEN 'lower_confidence' THEN 2 ELSE 3 END,
              COALESCE(di.relevance_score, 0) DESC
            """,
            (latest["id"],),
        ).fetchall()
        decision_rows = connection.execute(
            """
            SELECT agent, target, decision, action, reason, confidence, created_at
            FROM agent_decisions
            WHERE run_id = ?
            ORDER BY created_at DESC
            """,
            (latest["id"],),
        ).fetchall()

    included = [_review_item(row) for row in item_rows if row["fetch_status"] == "fetched" and row["tier"] != "dropped"]
    unresolved = [_review_item(row) for row in item_rows if row["fetch_status"] != "fetched" and row["tier"] != "dropped"]
    dropped_rows = [
        row for row in decision_rows
        if str(row["action"] or "") in {"drop", "drop_article"} or str(row["decision"] or "") in {"exclude", "weak_fallback"}
    ]
    duplicate_rows = [row for row in decision_rows if str(row["decision"] or "") == "duplicate"]
    repaired_rows = [row for row in decision_rows if str(row["action"] or "") == "repair_article"]
    return {
        "run_id": latest["id"],
        "issue_id": issue["id"] if issue else None,
        "generated_at": issue["created_at"] if issue else latest["completed_at"],
        "counts": {
            "included": len(included),
            "unresolved": len(unresolved),
            "dropped": len(dropped_rows),
            "duplicate": len(duplicate_rows),
            "repaired": len(repaired_rows),
        },
        "included": included[:limit],
        "unresolved": unresolved[:limit],
        "dropped": [_review_decision(row) for row in dropped_rows[:limit]],
        "duplicates": [_review_decision(row) for row in duplicate_rows[:limit]],
        "repaired": [_review_decision(row) for row in repaired_rows[:limit]],
    }

def _latest_run_row() -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute(
            """
            SELECT *
            FROM digest_runs
            WHERE status = 'completed'
            ORDER BY completed_at DESC, run_at DESC
            LIMIT 1
            """
        ).fetchone()

def _failure_example(row: sqlite3.Row) -> dict[str, Any]:
    status = str(row["fetch_status"] or "unknown")
    reason = str(row["quality_flag"] or status)
    return {
        "title": row["title"] or row["link_text"] or "Untitled link",
        "url": row["canonical_url"] or row["original_url"],
        "domain": row["domain"],
        "status": status,
        "reason": reason,
        "fixability": _fetch_fixability(status),
        "context": row["newsletter_snippet"] or row["editor_summary"] or row["editor_note"],
    }

def _review_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "title": row["title"] or "Untitled article",
        "url": row["canonical_url"] or row["original_url"],
        "domain": row["domain"],
        "tier": row["tier"],
        "section": row["section"],
        "status": row["fetch_status"],
        "reason": row["quality_flag"],
        "score": _nullable_float(row["relevance_score"]),
        "summary": _truncate_text(str(row["editor_summary"] or ""), 220),
    }

def _review_decision(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "agent": row["agent"],
        "target": row["target"],
        "decision": row["decision"],
        "action": row["action"],
        "reason": row["reason"],
        "confidence": _nullable_float(row["confidence"]),
        "created_at": row["created_at"],
    }

def _fetch_fixability(status: str) -> str:
    if status in {"blocked", "rate_limited"}:
        return "Usually fixable with retry, reader mode, or a different fetch path."
    if status in {"site_error", "fetch_error", "http_error"}:
        return "Worth retrying; may be temporary."
    if status == "no_content":
        return "Use newsletter context unless reader extraction improves."
    if status in {"not_found", "non_html"}:
        return "Usually safe to ignore unless the title looks important."
    return "Needs review."
