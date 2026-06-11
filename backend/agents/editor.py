from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Iterable

from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian.date_text import parse_iso_datetime
from backend.agents.librarian.text_utils import fallback_text, keyword_set


def prepare_issue_articles(digest: dict[str, Any], results: Iterable[ArticleFetchResult]) -> list[ArticleFetchResult]:
    interest = str(digest.get("interest") or "")
    interest_tokens = _filtered_interest_tokens(interest)
    exclusion_phrases = _exclusion_phrases(interest)
    threshold = float(digest.get("threshold") or 0.45)
    recency_weighting = str(digest.get("recency_weighting") or "recent")
    prepared: list[ArticleFetchResult] = []

    for result in results:
        if result.tier == "dropped":
            continue
        enriched = _prepare_result(result, interest_tokens, threshold, exclusion_phrases, recency_weighting)
        if enriched.tier == "dropped":
            continue
        prepared.append(enriched)

    prepared.sort(key=lambda r: _sort_key(r, recency_weighting), reverse=True)
    for index, result in enumerate(prepared):
        if result.fetched and result.tier != "lower_confidence":
            prepared[index] = replace(result, tier="lead", section=result.section or "Lead Story")
            break
    return prepared


def build_issue_snapshot(
    payload_count: int,
    configured_source_count: int,
    results: list[ArticleFetchResult],
) -> str:
    visible_results = [result for result in results if result.tier != "dropped"]
    fetched = [result for result in visible_results if result.fetched]
    fallback = [result for result in visible_results if not result.fetched]
    if configured_source_count == 0 and not results and not payload_count:
        return "No sources are configured for this digest."
    if not results and not payload_count:
        return f"No matching items found across {configured_source_count} configured source(s)."
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
    exclusion_phrases: tuple[str, ...] = (),
    recency_weighting: str = "recent",
) -> ArticleFetchResult:
    source_text = result.text or result.excerpt or fallback_text(result)
    keywords = list(result.keywords)
    if _is_google_news_result(result):
        section = "News"
    elif result.payload.source_type == "reddit_thread":
        section = "Community Signals"
    elif result.payload.source_type == "podcast_episode":
        section = "Podcast Signals"
    elif result.payload.source_type == "market_snapshot":
        section = "Markets"
    elif result.payload.source_type == "sec_filing":
        section = "SEC Filings"
    elif result.payload.source_type == "fred_series":
        section = "Macro Indicators"
    else:
        section = _section_for(result.title, source_text, keywords, interest_tokens)
    relevance = _relevance_score(result, interest_tokens, keywords, recency_weighting)
    topic_signal = _has_ai_topic_signal(result)
    if topic_signal and result.payload.source_type == "gmail_link":
        relevance = min(1.0, relevance + 0.18)

    if _is_approved_podcast_latest(result):
        tier = "main"
        relevance = max(relevance, 0.55)
    elif _matches_exclusion(result, source_text, exclusion_phrases):
        tier = "dropped"
    elif _translation_unavailable(result):
        tier = "dropped"
    elif (
        _requires_ai_topic_gate(interest_tokens)
        and result.payload.source_type == "gmail_link"
        and not _is_google_news_result(result)
        and not topic_signal
    ):
        tier = "dropped"
    elif result.payload.source_type in {"market_snapshot", "sec_filing", "fred_series"}:
        if (result.payload.metadata or {}).get("explicit_ticker") is True:
            tier = "main"
        elif relevance >= 0.15:
            tier = "main"
        else:
            tier = "dropped"
    elif result.payload.source_type == "reddit_thread":
        if relevance >= max(0.28, threshold - 0.18) and result.link_score >= 0.30:
            tier = "main"
        elif relevance >= 0.22 and result.link_score >= 0.35:
            tier = "lower_confidence"
        else:
            tier = "dropped"
    elif result.payload.source_type == "podcast_episode":
        if relevance >= max(0.30, threshold - 0.16) and result.link_score >= 0.30:
            tier = "main"
        elif relevance >= 0.24 and result.link_score >= 0.34:
            tier = "lower_confidence"
        else:
            tier = "dropped"
    elif _is_google_news_result(result):
        if result.fetched and relevance >= threshold:
            tier = "main"
        elif relevance >= 0.18 and result.link_score >= 0.45:
            tier = "lower_confidence"
        else:
            tier = "dropped"
    elif result.fetched:
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


def _is_approved_podcast_latest(result: ArticleFetchResult) -> bool:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    payload_metadata = result.payload.metadata if isinstance(result.payload.metadata, dict) else {}
    return (
        result.payload.source_type == "podcast_episode"
        and bool(metadata.get("subscribed_show") or payload_metadata.get("subscribed_show"))
    )


def _is_google_news_result(result: ArticleFetchResult) -> bool:
    metadata = result.payload.metadata if isinstance(result.payload.metadata, dict) else {}
    return metadata.get("search_provider") == "google_news_rss"


def _translation_unavailable(result: ArticleFetchResult) -> bool:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    payload_metadata = result.payload.metadata if isinstance(result.payload.metadata, dict) else {}
    translation = metadata.get("translation") or payload_metadata.get("translation")
    if not isinstance(translation, dict):
        return False
    source_language = str(translation.get("source_language") or payload_metadata.get("source_language") or "").strip()
    return bool(source_language and not translation.get("translated"))


def _relevance_score(result: ArticleFetchResult, interest_tokens: set[str], keywords: list[str], recency_weighting: str = "recent") -> float:
    haystack = keyword_set(" ".join([result.title, result.text, result.excerpt, fallback_text(result)]))
    if not haystack:
        return 0.0

    overlap = len(haystack & interest_tokens) / max(1, min(len(interest_tokens), 8)) if interest_tokens else 0.4
    title_tokens = keyword_set(result.title)
    title_overlap = len(title_tokens & interest_tokens) / max(1, len(title_tokens)) if title_tokens and interest_tokens else 0
    keyword_tokens = keyword_set(" ".join(keywords))
    keyword_overlap = len(keyword_tokens & interest_tokens) / max(1, len(keyword_tokens)) if keyword_tokens and interest_tokens else 0
    recency = _recency_score(result.payload.published_at, recency_weighting)
    quality = 0.07 if result.fetched else -0.08
    score = 0.06 + quality + (0.36 * overlap) + (0.16 * title_overlap) + (0.12 * keyword_overlap)
    score += 0.08 * min(max(result.link_score, 0.0), 1.0)
    score += 0.06 * recency
    return max(0.0, min(score, 1.0))


def _filtered_interest_tokens(interest: str) -> set[str]:
    return keyword_set(_positive_interest_text(interest)) - GENERIC_INTEREST_TOKENS


def _positive_interest_text(interest: str) -> str:
    return re.split(r"\bAvoid:\s*", interest, maxsplit=1, flags=re.IGNORECASE)[0]


def _exclusion_phrases(interest: str) -> tuple[str, ...]:
    parts = re.split(r"\bAvoid:\s*", interest, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) < 2:
        return ()
    phrases: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,;\n]+|\bor\b", parts[1], flags=re.IGNORECASE):
        phrase = re.sub(r"\s+", " ", raw.strip().lower())
        if len(phrase) <= 2 or phrase in seen:
            continue
        phrases.append(phrase)
        seen.add(phrase)
    return tuple(phrases)


def _matches_exclusion(
    result: ArticleFetchResult,
    source_text: str,
    exclusion_phrases: tuple[str, ...],
) -> bool:
    if not exclusion_phrases:
        return False
    metadata = result.payload.metadata or {}
    haystack = " ".join(
        str(value or "")
        for value in (
            result.title,
            result.excerpt,
            result.editor_summary,
            source_text[:2000],
            metadata.get("link_text"),
            metadata.get("title"),
        )
    ).lower()
    return any(phrase in haystack for phrase in exclusion_phrases)


def _requires_ai_topic_gate(interest_tokens: set[str]) -> bool:
    return bool(interest_tokens & AI_INTEREST_TOKENS)


def _has_ai_topic_signal(result: ArticleFetchResult) -> bool:
    metadata = result.payload.metadata or {}
    primary_context = " ".join(
        str(value)
        for value in (
            result.title,
            metadata.get("link_text"),
        )
        if value
    )
    if AI_TOPIC_RE.search(primary_context):
        return True

    summary_context = result.editor_summary or result.excerpt
    return len(list(AI_TOPIC_RE.finditer(summary_context))) >= 2


def _section_for(title: str, text: str, keywords: list[str], interest_tokens: set[str]) -> str:
    haystack = " ".join([title, text, " ".join(keywords)]).lower()
    markers_by_section = AI_SECTION_MARKERS if _requires_ai_topic_gate(interest_tokens) else GENERAL_SECTION_MARKERS
    for section, markers in markers_by_section:
        if any(marker in haystack for marker in markers):
            return section
    return "Noteworthy"


def _recency_score(published_at: str | None, recency_weighting: str = "recent") -> float:
    if not published_at:
        # Undated content: penalize heavily for breaking news, neutral for all_available
        if recency_weighting == "breaking":
            return 0.1
        if recency_weighting == "all_available":
            return 0.5
        return 0.3
    parsed = parse_iso_datetime(published_at)
    if parsed is None:
        if recency_weighting == "breaking":
            return 0.1
        if recency_weighting == "all_available":
            return 0.5
        return 0.3
    age_hours = max(0.0, (datetime.now(UTC) - parsed).total_seconds() / 3600)
    if recency_weighting == "all_available":
        return 0.5  # Flat — no age penalty when browsing archives
    if recency_weighting == "breaking":
        # Steep cliff: only content from the last 6h is truly fresh
        if age_hours <= 6:
            return 1.0
        if age_hours <= 24:
            return 0.75
        if age_hours <= 48:
            return 0.40
        return 0.1
    # Default / recent / last_year
    if age_hours <= 24:
        return 1.0
    if age_hours <= 72:
        return 0.65
    return 0.3


def _top_sections(results: list[ArticleFetchResult]) -> list[str]:
    counts = Counter(result.section for result in results if result.section)
    return [section for section, _count in counts.most_common(4)] or ["noteworthy stories"]


def _sort_key(result: ArticleFetchResult, recency_weighting: str = "recent") -> tuple[float, float, float]:
    fetched = 1.0 if result.fetched else 0.0
    score = result.relevance_score if result.relevance_score is not None else 0.0
    recency = _recency_score(result.payload.published_at, recency_weighting)
    return (fetched, score, recency)


AI_SECTION_MARKERS = (
    ("Frontier Lab Demand Signals", ("frontier lab", "frontier ai", "scaling law", "model developer capex", "compute scaling")),
    ("Models & Labs", ("model", "gemini", "gpt", "claude", "openai", "anthropic", "llama", "qwen")),
    ("Agents & Developer Tools", ("agent", "codex", "mcp", "sdk", "api", "developer", "workflow", "automation")),
    ("AI Infrastructure", ("gpu", "compute", "nvidia", "capacity", "training", "inference", "mlx", "cluster")),
    ("Business & Markets", ("enterprise", "business", "market", "investor", "revenue", "startup", "acquires")),
    ("Security & Policy", ("security", "privacy", "copyright", "regulation", "provenance", "policy")),
    ("Product & Work", ("product", "search", "workflow", "customer", "operator", "dashboard")),
)


GENERAL_SECTION_MARKERS = (
    ("Food & Drink", ("food", "restaurant", "taco", "market", "culinary", "eat", "dining", "coffee", "cafe")),
    ("Culture & History", ("museum", "history", "historic", "art", "culture", "gallery", "archaeology", "anthropology")),
    ("Neighborhoods & Walking", ("walk", "walking", "neighborhood", "tour", "street", "district", "itinerary")),
    ("Outdoors & Movement", ("bike", "biking", "hike", "hiking", "park", "trail", "outdoor")),
    ("Practical Planning", ("hotel", "stay", "transit", "airport", "safety", "reservation", "planning")),
    ("Shopping", ("shopping", "shop", "boutique", "store")),
)


GENERIC_INTEREST_TOKENS = {
    "daily",
    "latest",
    "local",
    "news",
    "release",
    "releases",
    "update",
    "updates",
}

AI_INTEREST_TOKENS = {
    "agent",
    "agentic",
    "agents",
    "ai",
    "artificial",
    "automation",
    "claude",
    "codex",
    "gemini",
    "gpt",
    "inference",
    "llm",
    "llms",
    "machine",
    "mcp",
    "model",
    "models",
    "openai",
    "qwen",
}

AI_TOPIC_RE = re.compile(
    r"\b(?:"
    r"ai agent(?:s)?|"
    r"artificial intelligence|"
    r"generative ai|"
    r"large language model(?:s)?|"
    r"machine learning|"
    r"fine[- ]?tuning|"
    r"agentic|agents?|ai|anthropic|chatgpt|claude|codex|copilot|cursor|deepseek|gemini|genai|"
    r"gpt(?:[- ]?\d+)?|inference|llama|llm(?:s)?|mcp|mistral|mlx|model(?:s)?|neural|ollama|"
    r"openai|qwen|training|transformer(?:s)?"
    r")\b",
    re.IGNORECASE,
)
