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
from backend.app.core.prompt_loader import load_prompt

MAX_AUDIT_CANDIDATES = 150
RETRY_AUDIT_CANDIDATES = 4
URL_DATE_RE = re.compile(r"/(20\d{2})[/-](0?[1-9]|1[0-2])(?:[/-](0?[1-9]|[12]\d|3[01]))?")
SYNDICATED_AGGREGATOR_DOMAINS = {
    "finance.yahoo.com",
    "news.yahoo.com",
    "yahoo.com",
    "msn.com",
    "marketbeat.com",
}
LOW_QUALITY_DOMAINS = {
    "blog.maxthon.com",
    "katacoto.com",
    "marketgrowthreports.com",
}
SOCIAL_OR_VIDEO_DOMAINS = {
    "instagram.com",
    "threads.net",
    "youtube.com",
    "youtu.be",
}
MARKET_REPORT_PHRASES = (
    "market size",
    "market share",
    "market report",
    "market growth",
    "industry analysis",
    "forecast",
)
ENTITY_ALIASES = {
    "micron": ("micron", "mu"),
    "hynix": ("hynix", "sk hynix", "sk하이닉스", "하이닉스"),
    "kioxia": ("kioxia", "キオクシア"),
    "sandisk": ("sandisk", "sndk", "san disk"),
    "samsung": ("samsung", "samsung electronics", "삼성전자"),
    "tsmc": ("tsmc", "taiwan semiconductor", "台積電", "台积电"),
}


async def apply_source_audit(
    profile: TopicProfile | dict[str, Any],
    results: Iterable[ArticleFetchResult],
    *,
    lookback_hours: int | None,
    model_client: ModelClient | None = None,
    inference_run_id: str | None = None,
    max_candidates: int | None = None,
    low_yield: bool = False,
) -> tuple[list[ArticleFetchResult], list[AgentDecision], dict[str, Any]]:
    result_list = list(results)
    candidates = _candidate_indexes(result_list, max_candidates=max_candidates)
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

    try:
        payload, elapsed_ms = await _complete_audit(
            client,
            profile,
            result_list,
            candidates,
            lookback_hours=lookback_hours,
            inference_run_id=inference_run_id,
            article_id="source_audit_batch",
            max_tokens=1600,
            compact=False,
            low_yield=low_yield,
        )
    except ModelClientError as first_error:
        retry_candidates = _retry_candidate_indexes(result_list, candidates)
        if retry_candidates:
            try:
                payload, elapsed_ms = await _complete_audit(
                    client,
                    profile,
                    result_list,
                    retry_candidates,
                    lookback_hours=lookback_hours,
                    inference_run_id=inference_run_id,
                    article_id="source_audit_retry",
                    max_tokens=900,
                    compact=True,
                    low_yield=low_yield,
                )
            except ModelClientError as retry_error:
                return _heuristic_audit_result(
                    profile,
                    result_list,
                    candidates,
                    model_name=model_name,
                    error=retry_error,
                    first_error=first_error,
                )
        else:
            return _heuristic_audit_result(profile, result_list, candidates, model_name=model_name, error=first_error)

    cutoff = None
    if lookback_hours is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))
    updated, decisions, audit_summary = _apply_audit_payload(
        result_list,
        payload,
        model_name=model_name,
        elapsed_ms=elapsed_ms,
        cutoff=cutoff,
    )
    audit_summary["candidate_count"] = len(candidates)
    return updated, decisions, audit_summary


async def _complete_audit(
    client: ModelClient,
    profile: TopicProfile | dict[str, Any],
    result_list: list[ArticleFetchResult],
    candidates: list[int],
    *,
    lookback_hours: int | None,
    inference_run_id: str | None,
    article_id: str,
    max_tokens: int,
    compact: bool,
    low_yield: bool = False,
) -> tuple[dict[str, Any], int]:
    prompt = _audit_prompt(profile, result_list, candidates, lookback_hours, compact=compact)
    system_prompt = load_prompt("source_audit")
    if low_yield:
        adjacent_terms = _profile_adjacent_terms(profile)
        adjacent_clause = (
            " Tangential angles for this interest include: "
            + ", ".join(adjacent_terms)
            + "."
            if adjacent_terms
            else ""
        )
        system_prompt += (
            "\n\nCRITICAL: We are in a low-yield recovery mode. Core on-topic coverage "
            "was thin, so we are WIDENING the topic to accept tangentially-related, "
            "aligned items in addition to direct hits." + adjacent_clause + " "
            "Keep any article that is on-topic OR clearly adjacent to the stated "
            "interest (e.g. accessories, gear, sub-communities, or activities that go "
            "hand-in-hand with it). Still EXCLUDE articles that are genuinely unrelated "
            "to the interest — an unrelated subject is not rescued by low yield. "
            "Do NOT relax freshness or recency: stale items must still be excluded "
            "exactly as in normal mode."
        )
    started_at = perf_counter()
    try:
        if hasattr(client, "complete_json_with_metrics"):
            response, payload = await client.complete_json_with_metrics(
                system=system_prompt,
                prompt=prompt,
                max_tokens=max_tokens,
            )
            record_model_response_metric(
                run_id=inference_run_id,
                article_id=article_id,
                mode="source_audit",
                model_client=client,
                response=response,
                system_prompt=system_prompt,
                prompt=prompt,
            )
            return payload, _elapsed_ms(started_at)
        payload = await client.complete_json(system=system_prompt, prompt=prompt, max_tokens=max_tokens)
        return payload, _elapsed_ms(started_at)
    except ModelClientError as exc:
        _record_audit_error(
            exc,
            client=client,
            inference_run_id=inference_run_id,
            article_id=article_id,
            prompt=prompt,
            started_at=started_at,
            system_prompt=system_prompt,
        )
        raise


def _record_audit_error(
    exc: ModelClientError,
    *,
    client: ModelClient,
    inference_run_id: str | None,
    article_id: str,
    prompt: str,
    started_at: float,
    system_prompt: str,
) -> None:
    record_model_error_metric(
        run_id=inference_run_id,
        article_id=article_id,
        mode="source_audit",
        model_client=client,
        system_prompt=system_prompt,
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


def _failed_audit_result(
    result_list: list[ArticleFetchResult],
    candidates: list[int],
    *,
    model_name: str | None,
    error: ModelClientError,
    first_error: ModelClientError | None = None,
) -> tuple[list[ArticleFetchResult], list[AgentDecision], dict[str, Any]]:
    friendly_reason = _friendly_model_error(error)
    metadata: dict[str, Any] = {
        "status": error.status,
        "reason": friendly_reason,
        "candidate_count": len(candidates),
    }
    if first_error is not None:
        metadata["first_attempt_status"] = first_error.status
        metadata["first_attempt_reason"] = _friendly_model_error(first_error)
    return result_list, [
        AgentDecision(
            agent="source_audit",
            target="candidate_pool",
            decision="fallback",
            action="pass_through",
            reason=f"Source audit could not complete, so unaudited candidates continued: {friendly_reason}",
            model_name=model_name,
            metadata=metadata,
        )
    ], {
        "status": "failed",
        "candidate_count": len(candidates),
        "included_count": len(candidates),
        "excluded_count": 0,
        "context_count": 0,
        "issues": [{"source_name": "Source Audit", "reason": f"Audit could not complete: {friendly_reason}"}],
    }


def _heuristic_audit_result(
    profile: TopicProfile | dict[str, Any],
    result_list: list[ArticleFetchResult],
    candidates: list[int],
    *,
    model_name: str | None,
    error: ModelClientError,
    first_error: ModelClientError | None = None,
) -> tuple[list[ArticleFetchResult], list[AgentDecision], dict[str, Any]]:
    profile_record = _profile_record(profile)
    updated = list(result_list)
    decisions: list[AgentDecision] = []
    issues: list[dict[str, str]] = []
    included_count = 0
    excluded_count = 0
    context_count = 0

    for index in candidates:
        result = updated[index]
        reason = _heuristic_exclusion_reason(profile_record, result)
        if reason:
            updated[index] = replace(
                result,
                tier="dropped",
                metadata={
                    **dict(result.metadata or {}),
                    "source_audit": {
                        "decision": "exclude",
                        "confidence": 0.72,
                        "constraint_failures": ["source_quality"],
                        "reason": reason,
                        "mode": "deterministic_fallback",
                    },
                },
            )
            excluded_count += 1
            issues.append({"source_name": result.title[:120], "reason": reason})
            decisions.append(
                AgentDecision(
                    agent="source_audit",
                    target=_target_for(result),
                    decision="exclude",
                    action="drop_article",
                    confidence=0.72,
                    reason=reason,
                    model_name=model_name,
                    metadata={"index": index, "mode": "deterministic_fallback"},
                )
            )
        else:
            included_count += 1

    friendly_reason = _friendly_model_error(error)
    metadata: dict[str, Any] = {
        "status": error.status,
        "reason": friendly_reason,
        "candidate_count": len(candidates),
        "mode": "deterministic_fallback",
        "excluded_count": excluded_count,
    }
    if first_error is not None:
        metadata["first_attempt_status"] = first_error.status
        metadata["first_attempt_reason"] = _friendly_model_error(first_error)
    decisions.append(
        AgentDecision(
            agent="source_audit",
            target="candidate_pool",
            decision="fallback",
            action="pre_rank_audit",
            reason=(
                "Model audit could not complete, so deterministic source-quality checks "
                f"excluded {excluded_count} obvious low-quality item(s)."
            ),
            model_name=model_name,
            metadata=metadata,
        )
    )
    return updated, decisions, {
        "status": "fallback",
        "candidate_count": len(candidates),
        "included_count": included_count,
        "excluded_count": excluded_count,
        "context_count": context_count,
        "issues": issues,
        "summary": (
            "Model audit could not complete; deterministic source-quality checks were applied "
            f"and excluded {excluded_count} obvious low-quality item(s)."
        ),
        "model_issue": friendly_reason,
    }


def _friendly_model_error(exc: ModelClientError) -> str:
    text = str(exc).strip()
    lowered = text.lower()
    if "401" in text or "unauthorized" in lowered:
        return "The local model server rejected the request; check the model API key in the Models tab."
    if "peer closed connection" in lowered or "incomplete chunked read" in lowered:
        return "The local model connection closed before the audit finished."
    if exc.status == "parse_error":
        return "The model returned text that was not valid audit JSON."
    if exc.status == "timeout":
        return "The model did not finish the audit in time."
    return text or exc.status or "The model call failed."


def _retry_candidate_indexes(results: list[ArticleFetchResult], candidates: list[int]) -> list[int]:
    if len(candidates) <= 1:
        return []
    ranked = sorted(
        candidates,
        key=lambda index: (
            float(results[index].relevance_score or 0.0),
            float(results[index].link_score or 0.0),
        ),
        reverse=True,
    )
    return ranked[: min(RETRY_AUDIT_CANDIDATES, len(ranked))]


def _heuristic_exclusion_reason(profile_record: dict[str, Any], result: ArticleFetchResult) -> str | None:
    metadata = dict(result.payload.metadata or {})
    url = result.final_url or result.original_url or result.payload.original_url or ""
    host = (result.domain or urlparse(url).netloc.lower().removeprefix("www.")).lower()
    host = host.removeprefix("www.")
    title = str(result.title or result.payload.source_name or "")
    title_lower = title.casefold()
    url_lower = str(url or "").casefold()
    text_lower = " ".join(
        [
            str(profile_record.get("statement") or ""),
            str(profile_record.get("scope") or ""),
            " ".join(str(item) for item in profile_record.get("search_queries") or []),
            " ".join(str(item) for item in profile_record.get("exclusions") or []),
            title,
            result.editor_summary or "",
            result.excerpt or "",
        ]
    ).casefold()
    source_type = result.payload.source_type

    if _host_matches(host, LOW_QUALITY_DOMAINS):
        return "Excluded by deterministic fallback because this domain is a low-quality blog or SEO source."
    if (
        source_type != "market_snapshot"
        and _host_matches(host, SYNDICATED_AGGREGATOR_DOMAINS)
        and _profile_discourages_aggregators(text_lower)
    ):
        return "Excluded by deterministic fallback because the brief asked to avoid Yahoo/MSN-like syndicated sources."
    if source_type == "foreign_web" and _host_matches(host, SOCIAL_OR_VIDEO_DOMAINS):
        return "Excluded by deterministic fallback because Foreign Media should not rank social or video pages as article coverage."
    if "/tag/" in url_lower or title_lower.startswith("tag ") or title_lower == "tag - blocksandfiles":
        return "Excluded by deterministic fallback because tag/archive pages are not article coverage."
    if any(phrase in title_lower for phrase in MARKET_REPORT_PHRASES):
        return "Excluded by deterministic fallback because generic market-report pages are low-signal for a current news brief."
    if source_type == "foreign_web" and _looks_like_english_page_for_foreign_result(metadata, title, result.excerpt or ""):
        return "Excluded by deterministic fallback because the result is not native-language foreign coverage."
    return None


def _host_matches(host: str, domains: set[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _profile_discourages_aggregators(text: str) -> bool:
    return any(marker in text for marker in ("yahoo", "msn", "aggregator", "syndicated", "not like msn", "not like yahoo"))


def _matches_requested_entity(profile_text: str, result: ArticleFetchResult) -> bool:
    requested_groups = [
        aliases
        for key, aliases in ENTITY_ALIASES.items()
        if key in profile_text or any(alias in profile_text for alias in aliases)
    ]
    if not requested_groups:
        return True
    result_text = " ".join(
        [
            result.title,
            result.payload.source_name,
            result.domain or "",
            result.editor_summary or "",
            result.excerpt or "",
        ]
    ).casefold()
    return any(any(alias.casefold() in result_text for alias in aliases) for aliases in requested_groups)


def _looks_like_english_page_for_foreign_result(metadata: dict[str, Any], title: str, summary: str) -> bool:
    source_language = str(metadata.get("source_language") or "").strip().lower()
    if not source_language or source_language == "en":
        return False
    combined = f"{title} {summary}"
    if "(english)" in combined.casefold() or " united states (english)" in combined.casefold():
        return True
    letters = re.findall(r"[A-Za-z]", combined)
    non_ascii = re.findall(r"[^\x00-\x7f]", combined)
    return len(letters) > 120 and len(non_ascii) < 4


def _audit_prompt(
    profile: TopicProfile | dict[str, Any],
    results: list[ArticleFetchResult],
    indexes: list[int],
    lookback_hours: int | None,
    *,
    compact: bool = False,
) -> str:
    if lookback_hours is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))
        source_scope = {
            "lookback_hours": lookback_hours,
            "cutoff_utc": cutoff.isoformat(timespec="seconds"),
            "instruction": (
                "For strict recent briefs, exclude stale current-looking pages. "
                "If an article is outside the requested window, choose exclude unless it is essential background; "
                "essential background must be include_as_context, never ranked as fresh news."
            ),
        }
    else:
        source_scope = {
            "lookback_hours": None,
            "cutoff_utc": None,
            "instruction": (
                "No time window is set — all available content is in scope. "
                "Do not exclude articles solely based on age."
            ),
        }
    profile_record = _profile_record(profile)
    records = [_article_record(index, results[index], compact=compact) for index in indexes]
    return json.dumps(
        {
            "task": "Audit candidate sources before ranking a Morning Dispatch brief.",
            "user_request": profile_record["statement"],
            "refined_scope": profile_record["scope"],
            "search_strategy": profile_record["search_queries"],
            "source_scope": source_scope,
            "coverage_goal": _coverage_goal(profile),
            "exclusions": profile_record["exclusions"],
            "instructions": (
                "Make judgment calls about freshness, topic fit, originality, and source quality. "
                "For broad landscape or learning briefs, do not collapse the issue to only a few articles: "
                "when the candidate pool is below the desired item count, prefer include_as_context for useful "
                "but overlapping background and exclude only stale, off-topic, inaccessible, or clearly promotional items. "
                "Treat provider dates as weak evidence when URL paths, snippets, or article text imply an older date. "
                "When published_at is null/empty, infer the publication date from the dateline, body text, "
                "url_date_hint, metadata_dates, or snippet and report it in resolved_published_date as ISO YYYY-MM-DD. "
                "Also report resolved_published_date when published_at IS set but the article text, dateline, "
                "url_date_hint, or snippet shows a credibly OLDER date that conflicts with it — in that case set "
                "date_conflict to true and explain the evidence in date_conflict_reason. Prefer the older, "
                "evidence-backed date over an optimistic provider/search date. "
                "Never invent or guess dates: only fill resolved_published_date from explicit evidence in the "
                "supplied fields, and leave it an empty string (with date_conflict false) when you are not confident. "
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
                        "resolved_published_date": "ISO YYYY-MM-DD from explicit evidence (dateline/text/url/snippet); empty if unknown",
                        "date_conflict": "true only when published_at is set but evidence shows a credibly older date",
                        "date_conflict_reason": "short note on the date evidence when date_conflict is true; else empty",
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


def _profile_adjacent_terms(profile: TopicProfile | dict[str, Any]) -> list[str]:
    if isinstance(profile, TopicProfile):
        terms = profile.adjacent_terms
    else:
        terms = profile.get("adjacent_terms") or ()
    return [str(term).strip() for term in terms if str(term).strip()]


def _coverage_goal(profile: TopicProfile | dict[str, Any]) -> dict[str, Any]:
    if isinstance(profile, TopicProfile):
        limits = profile.content_limits
    else:
        limits = profile.get("content_limits")
    desired_items = None
    if isinstance(limits, dict):
        try:
            desired_items = int(limits.get("target_items") or limits.get("total_items"))
        except (TypeError, ValueError):
            desired_items = None
    return {
        "desired_items": desired_items,
        "instruction": (
            "The final brief should approach this count when enough relevant candidates exist; "
            "source audit should remove genuine bad fits, not enforce a tiny shortlist."
        ),
    }


def _adapter_of(result: ArticleFetchResult) -> str:
    payload = result.payload
    source_type = payload.source_type
    metadata = payload.metadata if isinstance(payload.metadata, dict) else {}
    if source_type == "gmail_link":
        if metadata.get("search_query") or metadata.get("search_provider"):
            return "web_search"
        return "gmail"
    return {
        "gmail": "gmail",
        "reddit_thread": "reddit",
        "reddit_post": "reddit",
        "podcast_episode": "podcasts",
        "youtube_video": "youtube",
        "collection_chunk": "collections",
        "market_snapshot": "markets",
        "sec_filing": "sec_filings",
        "fred_series": "fred",
    }.get(source_type, "web_search")


def _candidate_indexes(results: list[ArticleFetchResult], *, max_candidates: int | None = None) -> list[int]:
    limit = _candidate_limit(max_candidates, MAX_AUDIT_CANDIDATES)
    eligible = [
        (index, result)
        for index, result in enumerate(results)
        if result.tier != "dropped" and (result.fetched or result.link_score >= 0.55)
    ]
    grouped: dict[str, list[tuple[int, ArticleFetchResult]]] = {}
    for index, result in eligible:
        adapter = _adapter_of(result)
        grouped.setdefault(adapter, []).append((index, result))
    selected_indexes: list[int] = []
    adapters = sorted(grouped.keys())
    pointers = {adapter: 0 for adapter in adapters}
    while len(selected_indexes) < limit:
        added = False
        for adapter in adapters:
            ptr = pointers[adapter]
            if ptr < len(grouped[adapter]):
                selected_indexes.append(grouped[adapter][ptr][0])
                pointers[adapter] += 1
                added = True
                if len(selected_indexes) >= limit:
                    break
        if not added:
            break
    return selected_indexes


def _candidate_limit(value: int | None, maximum: int) -> int:
    if value is None:
        return maximum
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return maximum


def _article_record(index: int, result: ArticleFetchResult, *, compact: bool = False) -> dict[str, Any]:
    metadata = dict(result.payload.metadata or {})
    result_metadata = dict(result.metadata or {})
    url = result.final_url or result.original_url or result.payload.original_url or ""
    summary_limit = 320 if compact else 500
    text_limit = 220 if compact else 420
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
        "summary": (result.editor_summary or result.excerpt or "")[:summary_limit],
        "text_sample": (result.text or "")[:text_limit],
        "translation": _translation_record(result_metadata.get("translation") or metadata.get("translation")),
        "relevance_score": result.relevance_score,
        "link_score": result.link_score,
    }


def _metadata_dates(metadata: dict[str, Any]) -> dict[str, Any]:
    keys = ("published_at", "published", "date", "created_at", "updated_at", "search_result_date", "pub_date")
    return {key: metadata.get(key) for key in keys if metadata.get(key)}


def _translation_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in ("translated", "source_language", "source_language_name", "mode", "error")
        if value.get(key) is not None
    }


def _url_date_hint(url: str) -> str | None:
    match = URL_DATE_RE.search(str(url or ""))
    if not match:
        return None
    year, month, day = match.groups()
    parts = [year]
    if month:
        parts.append(month.zfill(2))
    if day:
        parts.append(day.zfill(2))
    return "-".join(parts)


def _apply_audit_payload(
    results: list[ArticleFetchResult],
    payload: dict[str, Any],
    *,
    model_name: str,
    elapsed_ms: int,
    cutoff: datetime | None = None,
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

        # AI date resolution: backfill a date when the rule-based extractor found
        # none, AND override an existing date when the model reports an explicit
        # conflict (a credibly older date from the supplied text/url/snippet). The
        # window is then enforced on the resolved date.
        result, model_date, model_recency_reason = _apply_model_resolved_date(
            result,
            str(raw.get("resolved_published_date") or "").strip(),
            cutoff,
            date_conflict=_truthy(raw.get("date_conflict")),
            conflict_reason=str(raw.get("date_conflict_reason") or "").strip()[:200],
        )
        if model_recency_reason:
            decision = "exclude"
            confidence = max(confidence, 0.9)
            failures = sorted(set(failures) | {"recency"})
            reason = model_recency_reason

        metadata = {**dict(result.metadata or {}), "source_audit": {
            "decision": decision,
            "confidence": confidence,
            "constraint_failures": failures,
            "reason": reason,
            "resolved_published_date": model_date or "",
        }}

        if decision == "exclude" and confidence >= 0.5 and _is_protected_from_audit_exclusion(result):
            updated[index] = replace(result, metadata=metadata)
            included_count += 1
            action = "preserve_approved_source"
        elif decision == "exclude" and confidence >= 0.5 and result.payload.source_type != "market_snapshot":
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


_AUDIT_DATE_METADATA_KEYS = (
    "published_at",
    "published",
    "publication_date",
    "date",
    "pub_date",
    "search_result_date",
)
_SERVED_ONCE_METADATA_KEYS = ("served_once", "served_once_note", "served_once_key", "date_status")


def _apply_model_resolved_date(
    result: ArticleFetchResult,
    raw_date: str,
    cutoff: datetime | None,
    *,
    date_conflict: bool = False,
    conflict_reason: str = "",
) -> tuple[ArticleFetchResult, str, str]:
    """Resolve a publish date from the model and enforce the recency window on it.

    Two cases:
      * Backfill — no deterministic date exists: trust the model's inferred date.
      * Conflict override — a date already exists, but the model flagged
        date_conflict and supplied a credibly OLDER date from the article
        text/url/snippet. We prefer the older, evidence-backed date.

    A random model guess never overrides an existing date: an override requires
    both the conflict flag AND a date strictly older than the current one.

    Returns the (possibly updated) result, the ISO date applied (or ""), and a
    recency-rejection reason when that date falls outside the requested window.
    """
    if not raw_date:
        return result, "", ""
    parsed = _parse_model_date(raw_date)
    if parsed is None:
        return result, "", ""
    iso = parsed.date().isoformat()

    existing_raw = _existing_date_text(result)
    existing_dt = _existing_published_datetime(result)
    conflict_meta: dict[str, str] = {}
    if existing_raw:
        # Only override an existing date with explicit conflict evidence, and only
        # toward an older date (prefer the more reliable, older page evidence).
        if not date_conflict:
            return result, "", ""
        if existing_dt is not None and parsed >= existing_dt:
            return result, "", ""
        if existing_dt is not None and iso == existing_dt.date().isoformat():
            return result, "", ""
        date_source = "source_audit"
        conflict_meta = {
            "date_conflict_original": existing_dt.date().isoformat() if existing_dt else existing_raw,
            "date_conflict_resolved": iso,
        }
        if conflict_reason:
            conflict_meta["date_conflict_reason"] = conflict_reason
    else:
        date_source = "model"

    payload = replace(result.payload, published_at=iso)
    # A real date supersedes the "shown once" placeholder so it displays the date
    # and is not recorded as an undated item.
    metadata = {
        key: value
        for key, value in dict(result.metadata or {}).items()
        if key not in _SERVED_ONCE_METADATA_KEYS
    }
    metadata["date_source"] = date_source
    metadata.update(conflict_meta)
    updated = replace(result, payload=payload, metadata=metadata)
    reason = ""
    if cutoff is not None and parsed < cutoff:
        if date_source == "source_audit":
            reason = f"Published {iso} based on article text; outside the requested recency window."
        else:
            reason = f"Published {iso}; outside the requested recency window."
    return updated, iso, reason


def _has_existing_date(result: ArticleFetchResult) -> bool:
    return bool(_existing_date_text(result))


def _existing_date_text(result: ArticleFetchResult) -> str:
    value = str(getattr(result.payload, "published_at", "") or "").strip()
    if value:
        return value
    for meta in (result.payload.metadata, result.metadata):
        if isinstance(meta, dict):
            for key in _AUDIT_DATE_METADATA_KEYS:
                candidate = str(meta.get(key) or "").strip()
                if candidate:
                    return candidate
    return ""


def _existing_published_datetime(result: ArticleFetchResult) -> datetime | None:
    return _parse_model_date(_existing_date_text(result))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"true", "yes", "1"}


def _parse_model_date(raw: str) -> datetime | None:
    match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", str(raw or ""))
    if not match:
        return None
    try:
        # End-of-day UTC so a same-day article is not excluded on a time boundary.
        return datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), 23, 59, 59, tzinfo=UTC
        )
    except ValueError:
        return None


def _target_for(result: ArticleFetchResult) -> str:
    return result.final_url or result.original_url or result.title


def _client_model_name(client: ModelClient | None, fallback: str | None) -> str | None:
    if client is None:
        return fallback
    config = getattr(client, "config", None)
    return str(getattr(config, "model", None) or fallback or "")


def _is_protected_from_audit_exclusion(result: ArticleFetchResult) -> bool:
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))
