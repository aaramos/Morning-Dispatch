from __future__ import annotations

import json
import re
from dataclasses import replace
from time import perf_counter
from typing import Any, Iterable

from backend.agents.agentic import AgentDecision
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.model import ModelClient, ModelClientError
from backend.app.core.config import get_settings

MAX_CRITIC_ARTICLES = 28
MAX_NEWSLETTER_RECORDS = 8
AUTO_REPAIR_ACTIONS = {"drop_article", "demote_article", "replace_lead", "clean_text"}
DROP_FINDING_TYPES = {"duplicate", "promotional", "broken_link_noise", "low_value"}
DEMOTE_FINDING_TYPES = {"weak_lead", "thin_context", "low_confidence"}


async def apply_critic_repairs(
    digest: dict[str, Any],
    payloads: list[NormalizedPayload],
    results: Iterable[ArticleFetchResult],
    *,
    model_client: ModelClient | None = None,
) -> tuple[list[ArticleFetchResult], list[AgentDecision]]:
    result_list = _deterministic_repairs(list(results))
    active_count = sum(1 for result in result_list if result.tier != "dropped")
    settings = get_settings()
    if active_count < 2:
        return result_list, [
            AgentDecision(
                agent="critic",
                target="issue",
                decision="skipped",
                action="single_candidate",
                reason="Only one active article was available, so no critic model call was needed.",
                model_name=settings.librarian_model,
                metadata={"active_count": active_count},
            )
        ]

    client = model_client if model_client is not None else ModelClient.from_settings(settings)
    model_name = _client_model_name(client, settings.librarian_model) if client is not None else settings.librarian_model
    if client is None or not result_list:
        return result_list, [
            AgentDecision(
                agent="critic",
                target="issue",
                decision="fallback",
                action="deterministic_repairs",
                reason="No model client was available, so deterministic critic repairs were applied.",
                model_name=model_name,
            )
        ]

    prompt = _critic_prompt(digest, payloads, result_list)
    started_at = perf_counter()
    try:
        payload = await client.complete_json(
            system=CRITIC_SYSTEM_PROMPT,
            prompt=prompt,
            max_tokens=1400,
        )
    except ModelClientError as exc:
        return result_list, [
            AgentDecision(
                agent="critic",
                target="issue",
                decision="fallback",
                action="deterministic_repairs",
                reason=f"Critic model failed, so deterministic repairs were used: {exc.status}",
                model_name=model_name,
                metadata={"status": exc.status, "elapsed_ms": _elapsed_ms(started_at)},
            )
        ]

    updated, decisions = _apply_critic_payload(result_list, payload, model_name=model_name)
    decisions.append(
        AgentDecision(
            agent="critic",
            target="issue",
            decision="publishable" if payload.get("publishable", True) else "needs_repair",
            action="critic_review",
            reason=str(payload.get("summary") or "Critic reviewed the draft issue.")[:280],
            model_name=model_name,
            metadata={"elapsed_ms": _elapsed_ms(started_at)},
        )
    )
    return _normalize_lead(updated), decisions


def _deterministic_repairs(results: list[ArticleFetchResult]) -> list[ArticleFetchResult]:
    repaired: list[ArticleFetchResult] = []
    seen_titles: set[str] = set()
    for result in results:
        normalized_title = _normalized_title(result.title)
        if normalized_title and normalized_title in seen_titles:
            repaired.append(replace(result, tier="dropped"))
            continue
        if normalized_title:
            seen_titles.add(normalized_title)
        repaired.append(result)
    return _normalize_lead(repaired)


def _critic_prompt(
    digest: dict[str, Any],
    payloads: list[NormalizedPayload],
    results: list[ArticleFetchResult],
) -> str:
    newsletter_records = [
        {
            "sender": payload.source_name,
            "subject": str((payload.metadata or {}).get("subject") or "")[:140],
            "text_sample": " ".join(payload.raw_text.split())[:420],
        }
        for payload in payloads
        if payload.source_type == "gmail"
    ][:MAX_NEWSLETTER_RECORDS]
    article_records = [
        {
            "index": index,
            "title": result.title,
            "domain": result.domain,
            "tier": result.tier,
            "section": result.section,
            "summary": (result.editor_summary or result.excerpt)[:520],
            "status": result.status,
            "score": result.relevance_score,
        }
        for index, result in enumerate(results[:MAX_CRITIC_ARTICLES])
        if result.tier != "dropped"
    ]
    return json.dumps(
        {
            "digest_name": digest.get("name"),
            "digest_interest": digest.get("interest"),
            "instructions": (
                "Review this draft Morning Dispatch issue for quality. "
                "Prefer safe repairs only: drop duplicate/promotional/low-value stories, demote weak stories, "
                "replace the lead with a stronger existing article, or request text cleanup. "
                "Do not ask for new sources or broad web research."
            ),
            "auto_repair_actions": sorted(AUTO_REPAIR_ACTIONS),
            "articles": article_records,
            "newsletter_samples": newsletter_records,
            "schema": {
                "publishable": "boolean",
                "summary": "short overall critique",
                "findings": [
                    {
                        "type": "duplicate|promotional|weak_lead|raw_newsletter_junk|thin_context|broken_link_noise|low_value",
                        "target_index": "optional integer article index",
                        "severity": "low|medium|high",
                        "recommended_action": "drop_article|demote_article|replace_lead|clean_text|none",
                        "reason": "short reason",
                    }
                ],
            },
        },
        ensure_ascii=False,
    )


def _apply_critic_payload(
    results: list[ArticleFetchResult],
    payload: dict[str, Any],
    *,
    model_name: str,
) -> tuple[list[ArticleFetchResult], list[AgentDecision]]:
    updated = list(results)
    decisions: list[AgentDecision] = []
    raw_findings = payload.get("findings", [])
    if not isinstance(raw_findings, list):
        return updated, decisions

    for raw in raw_findings:
        if not isinstance(raw, dict):
            continue
        finding_type = str(raw.get("type") or "quality").strip().lower()
        action = str(raw.get("recommended_action") or "none").strip().lower()
        severity = str(raw.get("severity") or "low").strip().lower()
        reason = str(raw.get("reason") or "").strip()[:280]
        index = _safe_int(raw.get("target_index"))
        target = "issue" if index is None or not (0 <= index < len(updated)) else _target_for(updated[index])
        applied_action = "none"

        if action in AUTO_REPAIR_ACTIONS and index is not None and 0 <= index < len(updated):
            result = updated[index]
            if action == "drop_article" and finding_type in DROP_FINDING_TYPES:
                updated[index] = replace(result, tier="dropped")
                applied_action = "drop_article"
            elif action == "demote_article" and finding_type in DEMOTE_FINDING_TYPES | DROP_FINDING_TYPES:
                updated[index] = replace(result, tier="lower_confidence")
                applied_action = "demote_article"
            elif action == "replace_lead" and result.fetched and result.tier != "dropped":
                updated = _set_lead(updated, index)
                applied_action = "replace_lead"
            elif action == "clean_text":
                applied_action = "clean_text"
        elif action == "clean_text":
            applied_action = "clean_text"

        decisions.append(
            AgentDecision(
                agent="critic",
                target=target,
                decision=finding_type,
                action=applied_action,
                reason=reason,
                model_name=model_name,
                metadata={"severity": severity, "recommended_action": action, "target_index": index},
            )
        )

    return updated, decisions


def _normalize_lead(results: list[ArticleFetchResult]) -> list[ArticleFetchResult]:
    lead_index = next(
        (index for index, result in enumerate(results) if result.tier == "lead" and result.fetched),
        None,
    )
    if lead_index is None:
        lead_index = next(
            (index for index, result in enumerate(results) if result.tier != "dropped" and result.fetched),
            None,
        )
    if lead_index is None:
        return results
    return _set_lead(results, lead_index)


def _set_lead(results: list[ArticleFetchResult], lead_index: int) -> list[ArticleFetchResult]:
    updated: list[ArticleFetchResult] = []
    for index, result in enumerate(results):
        if result.tier == "dropped":
            updated.append(result)
        elif index == lead_index:
            updated.append(replace(result, tier="lead"))
        elif result.tier == "lead":
            updated.append(replace(result, tier="main"))
        else:
            updated.append(result)
    return updated


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _target_for(result: ArticleFetchResult) -> str:
    return result.canonical_url or result.final_url or result.original_url or result.title


def _normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _elapsed_ms(started_at: float) -> int:
    return round((perf_counter() - started_at) * 1000)


def _client_model_name(client: Any, fallback: str | None) -> str | None:
    config = getattr(client, "config", None)
    model = getattr(config, "model", None)
    return str(model) if model else fallback


CRITIC_SYSTEM_PROMPT = """You are the Morning Dispatch Critic Agent.
Review a draft personal intelligence brief for quality issues.
Return compact valid JSON. Recommend only safe repairs from the allowed list.
Do not invent facts, add sources, or request broad research."""
