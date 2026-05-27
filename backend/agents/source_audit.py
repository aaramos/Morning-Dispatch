from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Iterable
from urllib.parse import urlparse

from backend.agents.agentic import AgentDecision
from backend.agents.discovery.types import TopicProfile
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.model import ModelClient, ModelClientError
from backend.agents.model.metrics import record_model_error_metric, record_model_response_metric
from backend.app.core.config import get_settings

MAX_AUDIT_CANDIDATES = 80
URL_DATE_RE = re.compile(r"/(20\d{2})[/-](0[1-9]|1[0-2])(?:[/-]([0-3]\d))?")


async def apply_source_audit(
    profile: TopicProfile | dict[str, Any],
    results: Iterable[ArticleFetchResult],
    *,
    lookback_hours: int,
    model_client: ModelClient | None = None,
    inference_run_id: str | None = None,
) -> tuple[list[ArticleFetchResult], list[AgentDecision], dict[str, Any]]:
    result_list = list(results)
    candidates = _candidate_indexes(result_list)
    summary = {
        "status": "skipped",
        "candidate_count": len(candidates),
        "included_count": len(candidates),
        "excluded_count": 0,
        "context_count": 0,
        "issues": [],
    }
    if not candidates:
        return result_list, [], summary

    settings = get_settings()
    client = model_client if model_client is not None else ModelClient.from_settings(settings)
    model_name = _client_model_name(client, settings.librarian_model) if client is not None else settings.librarian_model
    if client is None:
        return result_list, [
            AgentDecision(
                agent="source_audit",
                target="candidate_pool",
                decision="fallback",
                action="pass_through",
                reason="No model client was available, so source audit could not make judgment calls.",
                model_name=model_name,
                metadata={"candidate_count": len(candidates)},
            )
        ], {**summary, "status": "fallback"}

    prompt = _audit_prompt(profile, result_list, candidates, lookback_hours)
    started_at = perf_counter()
    try:
        if hasattr(client, "complete_json_with_metrics"):
            response, payload = await client.complete_json_with_metrics(
                system=SOURCE_AUDIT_SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=2400,
            )
            record_model_response_metric(
                run_id=inference_run_id,
                article_id="source_audit_batch",
                mode="source_audit",
                model_client=client,
                response=response,
                system_prompt=SOURCE_AUDIT_SYSTEM_PROMPT,
                prompt=prompt,
            )
        else:
            payload = await client.complete_json(system=SOURCE_AUDIT_SYSTEM_PROMPT, prompt=prompt, max_tokens=2400)
    except ModelClientError as exc:
        record_model_error_metric(
            run_id=inference_run_id,
            article_id="source_audit_batch",
            mode="source_audit",
            model_client=client,
            system_prompt=SOURCE_AUDIT_SYSTEM_PROMPT,
            prompt=prompt,
            status=exc.status,
            error_detail=str(exc),
            total_ms=exc.total_ms if exc.total_ms is not None else _elapsed_ms(started_at),
            queue_wait_ms=exc.queue_wait_ms,
            ttft_ms=exc.ttft_ms,
            generation_ms=exc.generation_ms,
            prompt_tokens=exc.prompt_tokens,
            completion_tokens=exc.completion_tokens,
            tokens_per_sec=exc.tokens_per_sec,
        )
        return result_list, [
            AgentDecision(
                agent="source_audit",
                target="candidate_pool",
                decision="fallback",
                action="pass_through",
                reason=f"Source audit model failed, so unaudited candidates continued: {exc.status}",
                model_name=model_name,
                metadata={"status": exc.status, "elapsed_ms": _elapsed_ms(started_at), "candidate_count": len(candidates)},
            )
        ], {
            **summary,
            "status": "failed",
            "issues": [{"source_name": "Source Audit", "reason": f"Audit model failed: {exc.status}"}],
        }

    updated, decisions, audit_summary = _apply_audit_payload(
        result_list,
        payload,
        model_name=model_name,
        elapsed_ms=_elapsed_ms(started_at),
    )
    audit_summary["candidate_count"] = len(candidates)
    return updated, decisions, audit_summary


def _audit_prompt(
    profile: TopicProfile | dict[str, Any],
    results: list[ArticleFetchResult],
    indexes: list[int],
    lookback_hours: int,
) -> str:
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))
    profile_record = _profile_record(profile)
    records = [_article_record(index, results[index]) for index in indexes]
    return json.dumps(
        {
            "task": "Audit candidate sources before ranking a Morning Dispatch brief.",
            "user_request": profile_record["statement"],
            "refined_scope": profile_record["scope"],
            "search_strategy": profile_record["search_queries"],
            "source_scope": {
                "lookback_hours": lookback_hours,
                "cutoff_utc": cutoff.isoformat(timespec="seconds"),
                "instruction": (
                    "For strict recent briefs, exclude stale current-looking pages. "
                    "If an article is outside the requested window, choose exclude unless it is essential background; "
                    "essential background must be include_as_context, never ranked as fresh news."
                ),
            },
            "exclusions": profile_record["exclusions"],
            "instructions": (
                "Make judgment calls about freshness, topic fit, originality, and source quality. "
                "Treat provider dates as weak evidence when URL paths, snippets, or article text imply an older date. "
                "Treat MSN/Yahoo-like instructions as a request to avoid syndicated aggregator reposts, even on adjacent domains. "
                "Translated foreign-media items are allowed; judge them on the translated summary and provenance quality, "
                "but do not reject an item solely because it was translated. "
                "Return JSON only."
            ),
            "allowed_decisions": ["include", "exclude", "include_as_context"],
            "articles": records,
            "schema": {
                "decisions": [
                    {
                        "index": "integer article index",
                        "decision": "include|exclude|include_as_context",
                        "confidence": "0.0-1.0",
                        "constraint_failures": ["recency|source_quality|topic_fit|duplicate|thin_content"],
                        "reason": "short, user-readable reason",
                    }
                ],
                "summary": "short audit summary",
            },
        },
        ensure_ascii=False,
    )


def _profile_record(profile: TopicProfile | dict[str, Any]) -> dict[str, Any]:
    if isinstance(profile, TopicProfile):
        return {
            "statement": profile.statement,
            "scope": profile.scope,
            "search_queries": list(profile.search_queries),
            "exclusions": list(profile.exclusions),
        }
    return {
        "statement": str(profile.get("statement") or profile.get("name") or ""),
        "scope": str(profile.get("scope") or profile.get("interest") or profile.get("name") or ""),
        "search_queries": list(profile.get("search_queries") or []),
        "exclusions": list(profile.get("exclusions") or []),
    }


def _candidate_indexes(results: list[ArticleFetchResult]) -> list[int]:
    return [
        index
        for index, result in enumerate(results[:MAX_AUDIT_CANDIDATES])
        if result.tier != "dropped" and (result.fetched or result.link_score >= 0.55)
    ]


def _article_record(index: int, result: ArticleFetchResult) -> dict[str, Any]:
    metadata = dict(result.payload.metadata or {})
    result_metadata = dict(result.metadata or {})
    url = result.final_url or result.original_url or result.payload.original_url or ""
    return {
        "index": index,
        "title": result.title,
        "url": url,
        "domain": result.domain or urlparse(url).netloc.lower().removeprefix("www."),
        "source": result.payload.source_name,
        "source_type": result.payload.source_type,
        "published_at": result.payload.published_at,
        "fetched_at": result.payload.fetched_at,
        "metadata_dates": _metadata_dates(metadata),
        "url_date_hint": _url_date_hint(url),
        "summary": (result.editor_summary or result.excerpt or "")[:700],
        "text_sample": (result.text or "")[:900],
        "translation": result_metadata.get("translation") or metadata.get("translation"),
        "relevance_score": result.relevance_score,
        "link_score": result.link_score,
    }


def _metadata_dates(metadata: dict[str, Any]) -> dict[str, Any]:
    keys = ("published_at", "published", "date", "created_at", "updated_at", "search_result_date", "pub_date")
    return {key: metadata.get(key) for key in keys if metadata.get(key)}


def _url_date_hint(url: str) -> str | None:
    match = URL_DATE_RE.search(str(url or ""))
    if not match:
        return None
    year, month, day = match.groups()
    return "-".join(part for part in (year, month, day) if part)


def _apply_audit_payload(
    results: list[ArticleFetchResult],
    payload: dict[str, Any],
    *,
    model_name: str,
    elapsed_ms: int,
) -> tuple[list[ArticleFetchResult], list[AgentDecision], dict[str, Any]]:
    updated = list(results)
    decisions: list[AgentDecision] = []
    issues: list[dict[str, str]] = []
    included_count = 0
    excluded_count = 0
    context_count = 0
    raw_decisions = payload.get("decisions", [])
    if not isinstance(raw_decisions, list):
        raw_decisions = []

    for raw in raw_decisions:
        if not isinstance(raw, dict):
            continue
        index = _safe_int(raw.get("index"))
        if index is None or not (0 <= index < len(updated)):
            continue
        result = updated[index]
        decision = str(raw.get("decision") or "").strip().lower()
        confidence = _safe_confidence(raw.get("confidence"))
        failures = _string_list(raw.get("constraint_failures"))
        reason = str(raw.get("reason") or "").strip()[:320]
        action = "include"
        metadata = {**dict(result.metadata or {}), "source_audit": {
            "decision": decision,
            "confidence": confidence,
            "constraint_failures": failures,
            "reason": reason,
        }}

        if decision == "exclude" and confidence >= 0.5:
            updated[index] = replace(result, tier="dropped", metadata=metadata)
            excluded_count += 1
            action = "drop_article"
            issues.append({"source_name": result.title[:120], "reason": reason or "Excluded by source audit."})
        elif decision == "include_as_context" and confidence >= 0.45:
            updated[index] = replace(result, tier="lower_confidence", section="Context", metadata=metadata)
            context_count += 1
            action = "include_as_context"
        else:
            updated[index] = replace(result, metadata=metadata)
            included_count += 1

        decisions.append(
            AgentDecision(
                agent="source_audit",
                target=_target_for(result),
                decision=decision or "include",
                action=action,
                confidence=confidence,
                reason=reason,
                model_name=model_name,
                metadata={"index": index, "constraint_failures": failures},
            )
        )

    decisions.append(
        AgentDecision(
            agent="source_audit",
            target="candidate_pool",
            decision="completed",
            action="pre_rank_audit",
            reason=str(payload.get("summary") or "Source audit reviewed candidates before ranking.")[:320],
            model_name=model_name,
            metadata={"elapsed_ms": elapsed_ms, "decision_count": len(raw_decisions)},
        )
    )
    return updated, decisions, {
        "status": "completed",
        "included_count": included_count,
        "excluded_count": excluded_count,
        "context_count": context_count,
        "issues": issues,
        "summary": str(payload.get("summary") or "").strip()[:500],
    }


def _target_for(result: ArticleFetchResult) -> str:
    return result.final_url or result.original_url or result.title


def _client_model_name(client: ModelClient | None, fallback: str | None) -> str | None:
    if client is None:
        return fallback
    config = getattr(client, "config", None)
    return str(getattr(config, "model", None) or fallback or "")


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_confidence(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


SOURCE_AUDIT_SYSTEM_PROMPT = """You are Morning Dispatch's Source Audit Agent.
Your job is to protect the user's retrieval constraints before Editorial ranks the brief.
You are not a deterministic filter: use judgment about freshness, source originality, topic fit, and whether a source deserves to be ranked as current news.
Be strict when the user gave strict time windows or source-quality preferences.
Return strict JSON only."""
