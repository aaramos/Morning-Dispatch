from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Iterable

from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian.text_utils import fallback_text, keyword_set


def prepare_issue_articles(digest: dict[str, Any], results: Iterable[ArticleFetchResult]) -> list[ArticleFetchResult]:
    interest_tokens = keyword_set(str(digest.get("interest") or ""))
    threshold = float(digest.get("threshold") or 0.45)
    prepared: list[ArticleFetchResult] = []

    for result in results:
        if result.tier == "dropped":
            continue
        enriched = _prepare_result(result, interest_tokens, threshold)
        if enriched.tier == "dropped":
            continue
        prepared.append(enriched)

    prepared.sort(key=_sort_key, reverse=True)
    for index, result in enumerate(prepared):
        if index == 0 and result.fetched:
            prepared[index] = replace(result, tier="lead", section=result.section or "Lead Story")
            break
    return prepared


def build_issue_snapshot(
    payload_count: int,
    configured_source_count: int,
    results: list[ArticleFetchResult],
) -> str:
    body_count = payload_count
    visible_results = [result for result in results if result.tier != "dropped"]
    fetched = [result for result in visible_results if result.fetched]
    fallback = [result for result in visible_results if not result.fetched]
    if configured_source_count == 0:
        return "No Gmail newsletter sources are configured for this digest."
    if not results and not payload_count:
        return f"No matching newsletters found across {configured_source_count} configured Gmail source(s)."
    if not fetched:
        return "No primary article pages were resolved this run. The issue includes lower-confidence newsletter leads only."

    sections = _top_sections(fetched)
    lead = fetched[0].title
    theme_text = ", ".join(sections[:3]).lower()
    lower_count = sum(1 for result in visible_results if result.tier == "lower_confidence")
    return (
        f"The issue is led by {lead}. "
        f"Top coverage clusters around {theme_text}, with {len(fetched)} ranked article(s) "
        f"and {lower_count + len(fallback)} lower-confidence item(s)."
    )


def _prepare_result(
    result: ArticleFetchResult,
    interest_tokens: set[str],
    threshold: float,
) -> ArticleFetchResult:
    source_text = result.text or result.excerpt or fallback_text(result)
    keywords = list(result.keywords)
    section = _section_for(result.title, source_text, keywords)
    relevance = _relevance_score(result, interest_tokens, keywords)

    if result.fetched:
        if relevance >= threshold:
            tier = "main"
        elif relevance >= max(0.35, threshold - 0.10) and result.link_score >= 0.5:
            tier = "lower_confidence"
        else:
            tier = "dropped"
    else:
        tier = "lower_confidence" if relevance >= 0.28 and result.link_score >= 0.55 else "dropped"

    return replace(
        result,
        excerpt=result.editor_summary or result.excerpt,
        editor_summary=result.editor_summary or result.excerpt,
        relevance_score=round(relevance, 3),
        tier=tier,
        section=section,
    )


def _relevance_score(result: ArticleFetchResult, interest_tokens: set[str], keywords: list[str]) -> float:
    haystack = keyword_set(" ".join([result.title, result.text, result.excerpt, fallback_text(result)]))
    if not haystack:
        return 0.0

    overlap = len(haystack & interest_tokens) / max(1, len(interest_tokens)) if interest_tokens else 0.4
    title_tokens = keyword_set(result.title)
    title_overlap = len(title_tokens & interest_tokens) / max(1, len(title_tokens)) if title_tokens and interest_tokens else 0
    keyword_overlap = len(set(keywords) & interest_tokens) / max(1, len(keywords)) if keywords and interest_tokens else 0
    recency = _recency_score(result.payload.published_at)
    quality = 0.07 if result.fetched else -0.08
    score = 0.06 + quality + (0.36 * overlap) + (0.16 * title_overlap) + (0.12 * keyword_overlap)
    score += 0.08 * min(max(result.link_score, 0.0), 1.0)
    score += 0.06 * recency
    return max(0.0, min(score, 1.0))


def _section_for(title: str, text: str, keywords: list[str]) -> str:
    haystack = " ".join([title, text, " ".join(keywords)]).lower()
    for section, markers in SECTION_MARKERS:
        if any(marker in haystack for marker in markers):
            return section
    return "Noteworthy"


def _recency_score(published_at: str | None) -> float:
    if not published_at:
        return 0.3
    try:
        parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        age_hours = max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds() / 3600)
    except ValueError:
        return 0.3
    if age_hours <= 24:
        return 1.0
    if age_hours <= 72:
        return 0.65
    return 0.3


def _top_sections(results: list[ArticleFetchResult]) -> list[str]:
    counts = Counter(result.section for result in results if result.section)
    return [section for section, _count in counts.most_common(4)] or ["noteworthy stories"]


def _sort_key(result: ArticleFetchResult) -> tuple[float, float, float]:
    fetched = 1.0 if result.fetched else 0.0
    score = result.relevance_score if result.relevance_score is not None else 0.0
    recency = _recency_score(result.payload.published_at)
    return (fetched, score, recency)


SECTION_MARKERS = (
    ("Models & Labs", ("model", "gemini", "gpt", "claude", "openai", "anthropic", "google", "llama", "qwen")),
    ("Agents & Developer Tools", ("agent", "codex", "mcp", "sdk", "api", "developer", "workflow", "automation")),
    ("AI Infrastructure", ("gpu", "compute", "nvidia", "capacity", "training", "inference", "mlx", "cluster")),
    ("Business & Markets", ("enterprise", "business", "market", "investor", "revenue", "startup", "acquires")),
    ("Security & Policy", ("security", "privacy", "copyright", "regulation", "provenance", "policy")),
    ("Product & Work", ("product", "search", "workflow", "customer", "operator", "dashboard")),
)
