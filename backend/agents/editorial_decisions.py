from __future__ import annotations

import json
from dataclasses import replace
from time import perf_counter
from typing import Any, Iterable
from urllib.parse import urlparse
from collections.abc import Callable

from backend.agents.agentic import AgentDecision
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.model import ModelClient, ModelClientError
from backend.agents.model.metrics import record_model_response_metric
from backend.app.core.config import get_settings
from backend.app.core.prompt_loader import load_prompt

MAX_EDITORIAL_CANDIDATES = 500
ALLOWED_SECTIONS = {
    "Models & Labs",
    "Agents & Developer Tools",
    "AI Infrastructure",
    "Business & Markets",
    "Security & Policy",
    "Product & Work",
    "Noteworthy",
}


async def apply_editorial_decisions(
    digest: dict[str, Any],
    results: Iterable[ArticleFetchResult],
    *,
    model_client: ModelClient | None = None,
    reasoning_callback: Callable[[str], None] | None = None,
    inference_run_id: str | None = None,
    max_candidates: int | None = None,
) -> tuple[list[ArticleFetchResult], list[AgentDecision]]:
    result_list = list(results)
    candidates = _candidate_indexes(result_list, max_candidates=max_candidates)
    if not candidates:
        return result_list, []
    if len(candidates) < 2:
        return _normalize_lead(result_list), [
            AgentDecision(
                agent="editorial",
                target="issue",
                decision="skipped",
                action="single_candidate",
                reason="Only one candidate article was available, so no batch editorial model call was needed.",
                model_name=get_settings().librarian_model,
                metadata={"candidate_count": len(candidates)},
            )
        ]

    settings = get_settings()
    client = model_client if model_client is not None else ModelClient.from_settings(settings)
    model_name = _client_model_name(client, settings.librarian_model) if client is not None else settings.librarian_model
    if client is None:
        return _normalize_lead(result_list), [
            AgentDecision(
                agent="editorial",
                target="issue",
                decision="fallback",
                action="deterministic_ranking",
                reason="No model client was available, so deterministic ranking selected the issue order.",
                model_name=model_name,
            )
        ]

    prompt = _editorial_prompt(digest, result_list, candidates)
    started_at = perf_counter()
    try:
        if hasattr(client, "complete_json_with_metrics"):
            response, payload = await client.complete_json_with_metrics(
                system=load_prompt("editorial"),
                prompt=prompt,
                max_tokens=1600,
                on_token=reasoning_callback,
            )
            record_model_response_metric(
                run_id=inference_run_id,
                article_id="editorial_batch",
                mode="editorial",
                model_client=client,
                response=response,
                system_prompt=load_prompt("editorial"),
                prompt=prompt,
            )
        else:
            payload = await client.complete_json(
                system=load_prompt("editorial"),
                prompt=prompt,
                max_tokens=1600,
                on_token=reasoning_callback,
            )
    except ModelClientError as exc:
        return _normalize_lead(result_list), [
            AgentDecision(
                agent="editorial",
                target="issue",
                decision="fallback",
                action="deterministic_ranking",
                reason=f"Editorial model failed, so deterministic ranking was used: {exc.status}",
                model_name=model_name,
                metadata={"status": exc.status, "elapsed_ms": _elapsed_ms(started_at)},
            )
        ]

    updated, decisions = _apply_editorial_payload(result_list, payload, model_name=model_name)
    decisions.append(
        AgentDecision(
            agent="editorial",
            target="issue",
            decision="completed",
            action="batch_article_selection",
            confidence=None,
            reason="Editorial agent reviewed candidate articles in one compact batch.",
            model_name=model_name,
            metadata={"candidate_count": len(candidates), "elapsed_ms": _elapsed_ms(started_at)},
        )
    )
    return _normalize_lead(updated), decisions


def _candidate_indexes(results: list[ArticleFetchResult], *, max_candidates: int | None = None) -> list[int]:
    limit = _candidate_limit(max_candidates, MAX_EDITORIAL_CANDIDATES)
    return [
        index
        for index, result in enumerate(results[:limit])
        if result.tier != "dropped" and (result.fetched or result.link_score >= 0.55)
    ]


def _candidate_limit(value: int | None, maximum: int) -> int:
    if value is None:
        return maximum
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return maximum


def _editorial_prompt(digest: dict[str, Any], results: list[ArticleFetchResult], indexes: list[int]) -> str:
    records = [_article_record(index, results[index]) for index in indexes]
    return json.dumps(
        {
            "digest_name": digest.get("name"),
            "digest_interest": digest.get("interest"),
            "coverage_goal": _coverage_goal(digest.get("content_limits")),
            "instructions": (
                "Choose the best Morning Dispatch issue from these already-approved sources. "
                "Prefer concrete, timely, high-signal AI/product/infrastructure stories. "
                "Aim for the requested visible story count when enough relevant candidates exist. "
                "Reject ads, signup pages, thin promos, duplicates, and weakly related items. "
                "Return JSON only."
            ),
            "allowed_decisions": ["lead", "include", "exclude", "demote"],
            "allowed_sections": sorted(ALLOWED_SECTIONS),
            "articles": records,
            "schema": {
                "decisions": [
                    {
                        "index": "integer article index",
                        "decision": "lead|include|exclude|demote",
                        "section": "optional allowed section",
                        "confidence": "0.0-1.0",
                        "reason": "short reason",
                    }
                ]
            },
        },
        ensure_ascii=False,
    )


def _coverage_goal(content_limits: Any) -> dict[str, Any]:
    if not isinstance(content_limits, dict):
        return {"target_visible_items": None}
    try:
        target = int(content_limits.get("target_items") or content_limits.get("total_items"))
    except (TypeError, ValueError):
        target = None
    return {
        "target_visible_items": target,
        "lead_items": content_limits.get("lead_items"),
        "quality_floor": content_limits.get("quality_floor"),
    }


def _article_record(index: int, result: ArticleFetchResult) -> dict[str, Any]:
    return {
        "index": index,
        "title": result.title,
        "domain": result.domain or _domain(result.final_url or result.original_url),
        "source": result.payload.source_name,
        "summary": (result.editor_summary or result.excerpt)[:520],
        "keywords": list(result.keywords[:6]),
        "section": result.section,
        "tier": result.tier,
        "fetched": result.fetched,
        "relevance_score": result.relevance_score,
        "link_score": result.link_score,
    }


def _apply_editorial_payload(
    results: list[ArticleFetchResult],
    payload: dict[str, Any],
    *,
    model_name: str,
) -> tuple[list[ArticleFetchResult], list[AgentDecision]]:
    updated = list(results)
    decisions: list[AgentDecision] = []
    raw_decisions = payload.get("decisions", [])
    if not isinstance(raw_decisions, list):
        return updated, decisions

    lead_index: int | None = None
    lead_confidence = -1.0

    for raw in raw_decisions:
        if not isinstance(raw, dict):
            continue
        index = _safe_int(raw.get("index"))
        if index is None or not (0 <= index < len(updated)):
            continue
        result = updated[index]
        decision = str(raw.get("decision") or "").strip().lower()
        confidence = _safe_confidence(raw.get("confidence"))
        reason = str(raw.get("reason") or "").strip()[:280]
        section = _safe_section(raw.get("section"))
        action = "none"

        if decision == "exclude" and confidence >= 0.55 and _is_protected_from_editorial_exclusion(result):
            action = "preserve_approved_source"
        elif decision == "exclude" and confidence >= 0.55 and result.payload.source_type != "market_snapshot":
            updated[index] = replace(result, tier="dropped")
            action = "drop"
        elif decision == "demote":
            updated[index] = replace(result, tier="lower_confidence", section=section or result.section)
            action = "demote"
        elif decision == "include":
            next_tier = "main" if result.fetched else "lower_confidence"
            updated[index] = replace(result, tier=next_tier, section=section or result.section)
            action = "include"
        elif decision == "lead" and result.fetched and confidence > lead_confidence:
            lead_index = index
            lead_confidence = confidence
            if section:
                updated[index] = replace(result, section=section)
            action = "candidate_lead"
        elif section and result.tier != "dropped":
            updated[index] = replace(result, section=section)
            action = "section_update"

        if decision in {"lead", "include", "exclude", "demote"}:
            decisions.append(
                AgentDecision(
                    agent="editorial",
                    target=_target_for(result),
                    decision=decision,
                    action=action,
                    confidence=confidence,
                    reason=reason,
                    model_name=model_name,
                    metadata={"index": index, "section": section},
                )
            )

    if lead_index is not None and updated[lead_index].tier != "dropped":
        updated = [
            replace(result, tier="lead" if index == lead_index else ("main" if result.tier == "lead" else result.tier))
            for index, result in enumerate(updated)
        ]

    return updated, decisions


def _normalize_lead(results: list[ArticleFetchResult]) -> list[ArticleFetchResult]:
    lead_seen = False
    normalized = list(results)
    for index, result in enumerate(normalized):
        if result.tier == "dropped" or not result.fetched:
            if result.tier == "lead":
                normalized[index] = replace(result, tier="lower_confidence")
            continue
        if not lead_seen:
            normalized[index] = replace(result, tier="lead")
            lead_seen = True
        elif result.tier == "lead":
            normalized[index] = replace(result, tier="main")
    return normalized


def _is_protected_from_editorial_exclusion(result: ArticleFetchResult) -> bool:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    payload_metadata = result.payload.metadata if isinstance(result.payload.metadata, dict) else {}
    return (
        result.payload.source_type == "podcast_episode"
        and bool(metadata.get("subscribed_show") or payload_metadata.get("subscribed_show"))
    )


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


def _safe_section(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    section = value.strip()
    return section if section in ALLOWED_SECTIONS else None


def _target_for(result: ArticleFetchResult) -> str:
    return result.canonical_url or result.final_url or result.original_url or result.title


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).netloc.removeprefix("www.") or None


def _elapsed_ms(started_at: float) -> int:
    return round((perf_counter() - started_at) * 1000)


def _client_model_name(client: Any, fallback: str | None) -> str | None:
    config = getattr(client, "config", None)
    model = getattr(config, "model", None)
    return str(model) if model else fallback
