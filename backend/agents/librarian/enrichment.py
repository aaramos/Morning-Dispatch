from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import replace
from time import perf_counter
from typing import Iterable
from urllib.parse import urlparse

from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.model import MODEL_CAPACITY_STATUS, ModelClient, ModelClientError
from backend.app.core.config import get_settings
from backend.app.db import database


MAX_SUMMARY_CHARS = 620
MAX_MODEL_TEXT_CHARS = 2500
logger = logging.getLogger(__name__)
MODEL_ENRICHED_SOURCES = {"model", "model_cache"}


async def enrich_articles(
    results: Iterable[ArticleFetchResult],
    *,
    model_client: ModelClient | None = None,
    model_max_items: int | None = None,
) -> list[ArticleFetchResult]:
    """Enrich fetched article results without using digest-specific interests."""
    result_list = list(results)
    settings = get_settings()
    client = model_client if model_client is not None else ModelClient.from_settings(settings)
    max_items = settings.librarian_model_max_items if model_max_items is None else max(0, model_max_items)
    model_result_ids = _model_candidate_ids(result_list, max_items) if client is not None else set()
    enriched: list[ArticleFetchResult] = []
    tasks = [
        enrich_article_with_model(result, model_client=client if id(result) in model_result_ids else None)
        for result in result_list
    ]
    for enriched_result in await asyncio.gather(*tasks):
        if enriched_result.tier == "dropped":
            continue
        enriched.append(enriched_result)
    return enriched


def enrich_article(result: ArticleFetchResult) -> ArticleFetchResult:
    source_text = _clean_article_text(result.title, result.text or result.excerpt or fallback_text(result))
    keywords = extract_keywords(source_text, result.title)
    summary = summarize(result.title, source_text, set(keywords))
    content_type = classify_content_type(result, source_text, keywords)

    if not result.fetched:
        content_type = "fallback_snippet"
        summary = fallback_summary(result)

    quality_tier = "dropped" if should_drop_result(result, source_text) else result.tier
    return replace(
        result,
        text=source_text if result.fetched else result.text,
        excerpt=summary or result.excerpt,
        editor_summary=summary or result.excerpt,
        keywords=tuple(keywords),
        content_type=content_type,
        tier=quality_tier,
        enrichment_source="deterministic",
    )


async def enrich_article_with_model(
    result: ArticleFetchResult,
    *,
    model_client: ModelClient | None = None,
    metrics_context: dict[str, object] | None = None,
) -> ArticleFetchResult:
    if result.enrichment_source in MODEL_ENRICHED_SOURCES:
        return result

    deterministic = enrich_article(result)
    if model_client is None or deterministic.tier == "dropped" or not deterministic.fetched:
        return deterministic

    prompt = _librarian_prompt(deterministic)
    started_at = perf_counter()
    model_response = None
    try:
        if hasattr(model_client, "complete_json_with_metrics"):
            model_response, model_payload = await model_client.complete_json_with_metrics(
                system=LIBRARIAN_SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=220,
            )
        else:
            model_payload = await model_client.complete_json(
                system=LIBRARIAN_SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=220,
            )
    except ModelClientError as exc:
        logger.info("Librarian model enrichment failed for %s: %s", result.original_url, exc)
        fallback_source = "model_capacity_fallback" if exc.status == MODEL_CAPACITY_STATUS else "deterministic"
        _record_model_metric(
            result=deterministic,
            metrics_context=metrics_context,
            model_client=model_client,
            prompt=prompt,
            status=exc.status,
            schema_valid=False,
            fallback_triggered=True,
            total_ms=exc.total_ms if exc.total_ms is not None else _elapsed_ms(started_at),
            queue_wait_ms=exc.queue_wait_ms,
            ttft_ms=exc.ttft_ms,
            generation_ms=exc.generation_ms,
            prompt_tokens=exc.prompt_tokens,
            completion_tokens=exc.completion_tokens,
            tokens_per_sec=exc.tokens_per_sec,
            summary_word_count=_word_count(deterministic.editor_summary or deterministic.excerpt),
            error_detail=str(exc),
        )
        return replace(deterministic, enrichment_source=fallback_source)

    enriched = _apply_model_payload(deterministic, model_payload)
    _record_model_metric(
        result=enriched,
        metrics_context=metrics_context,
        model_client=model_client,
        prompt=prompt,
        status="success",
        schema_valid=True,
        fallback_triggered=False,
        total_ms=getattr(model_response, "total_ms", None) or _elapsed_ms(started_at),
        queue_wait_ms=getattr(model_response, "queue_wait_ms", None),
        ttft_ms=getattr(model_response, "ttft_ms", None),
        generation_ms=getattr(model_response, "generation_ms", None),
        prompt_tokens=getattr(model_response, "prompt_tokens", None),
        completion_tokens=getattr(model_response, "completion_tokens", None),
        tokens_per_sec=getattr(model_response, "tokens_per_sec", None),
        classification_label=enriched.content_type,
        classification_confidence=_classification_confidence(model_payload),
        summary_word_count=_word_count(enriched.editor_summary or enriched.excerpt),
    )
    return enriched


async def refine_ranked_articles_with_model(
    results: Iterable[ArticleFetchResult],
    *,
    model_client: ModelClient | None = None,
    model_max_items: int | None = None,
    inference_run_id: str | None = None,
    metrics_mode: str = "single",
) -> list[ArticleFetchResult]:
    result_list = list(results)
    settings = get_settings()
    client = model_client if model_client is not None else ModelClient.from_settings(settings)
    max_items = settings.librarian_model_max_items if model_max_items is None else max(0, model_max_items)
    tasks = [
        enrich_article_with_model(
            result,
            model_client=client if client is not None and index < max_items else None,
            metrics_context=_metrics_context_for_result(result, inference_run_id, metrics_mode)
            if client is not None and index < max_items and inference_run_id
            else None,
        )
        for index, result in enumerate(result_list)
    ]
    return list(await asyncio.gather(*tasks))


def summarize(title: str, text: str, signal_words: set[str]) -> str:
    sentences = _sentences(text)
    if not sentences:
        return ""
    ranked = sorted(
        enumerate(sentences[:18]),
        key=lambda item: (_sentence_score(item[1], signal_words), -item[0]),
        reverse=True,
    )
    chosen_indexes = sorted(index for index, sentence in ranked[:3] if _sentence_score(sentence, signal_words) > 0)
    if not chosen_indexes:
        chosen_indexes = [0]

    summary_parts: list[str] = []
    for index in chosen_indexes:
        sentence = sentences[index]
        if sentence.lower().strip() == title.lower().strip():
            continue
        summary_parts.append(sentence)
        if len(" ".join(summary_parts)) >= MAX_SUMMARY_CHARS:
            break
    summary = " ".join(summary_parts).strip()
    if not summary:
        summary = sentences[0]
    return _truncate(summary, MAX_SUMMARY_CHARS)


def extract_keywords(text: str, title: str) -> list[str]:
    words = tokens(f"{title} {title} {text}")
    counts = Counter(word for word in words if word not in STOPWORDS and len(word) > 3)
    return [word for word, _count in counts.most_common(8)]


def classify_content_type(result: ArticleFetchResult, text: str, keywords: list[str]) -> str:
    haystack = " ".join([result.title, text, " ".join(keywords)]).lower()
    if not result.fetched:
        return "fallback_snippet"
    if result.payload.source_type == "reddit_thread":
        return "discussion"
    if any(marker in haystack for marker in ("tutorial", "how to", "guide", "walkthrough")):
        return "tutorial"
    if any(marker in haystack for marker in ("opinion", "column", "i think", "we believe")):
        return "opinion"
    return "article"


def should_drop_result(result: ArticleFetchResult, text: str) -> bool:
    url = (result.final_url or result.original_url or "").lower()
    domain = (result.domain or _domain(result.final_url or result.original_url) or "").lower()
    title = (result.title or "").strip().lower()
    haystack = f"{title} {text[:1600].lower()} {url}"

    if any(domain == blocked or domain.endswith(f".{blocked}") for blocked in NOISY_DOMAINS):
        return True
    if any(marker in url for marker in NOISY_URL_MARKERS):
        return True
    if any(marker in title for marker in NOISY_TITLE_MARKERS):
        return True
    if result.fetched and _looks_like_author_bio(result.title, haystack):
        return True
    if result.fetched and any(marker in haystack for marker in NOISY_FETCHED_TEXT_MARKERS):
        return True
    return False


def keyword_set(text: str) -> set[str]:
    return {word for word in tokens(text) if word not in STOPWORDS and len(word) > 1}


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z0-9']+", text.lower())


def fallback_text(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    return " ".join(
        str(value)
        for value in (
            metadata.get("link_text"),
            metadata.get("title"),
            metadata.get("parent_subject"),
            metadata.get("subject"),
            result.original_url,
        )
        if value
    )


def fallback_summary(result: ArticleFetchResult) -> str:
    domain = result.domain or _domain(result.original_url) or "the linked source"
    reason = result.error or result.status
    title = result.title or str((result.payload.metadata or {}).get("link_text") or domain)
    source = result.payload.source_name or "an approved newsletter"
    return f"{title} was surfaced by {source}, but the article page could not be fully read ({reason})."


def _librarian_prompt(result: ArticleFetchResult) -> str:
    text = (result.text or result.excerpt or fallback_text(result))[:MAX_MODEL_TEXT_CHARS]
    source_label = "Source" if result.payload.source_type == "reddit_thread" else "Source newsletter"
    return f"""Title: {result.title}
URL: {result.final_url or result.original_url}
{source_label}: {result.payload.source_name}

Text:
{text}

Keep the summary under 70 words. Keep keyword labels short. Return compact JSON only.
"""


def _apply_model_payload(result: ArticleFetchResult, payload: dict[str, object]) -> ArticleFetchResult:
    title = _clean_model_text(str(payload.get("title") or result.title), max_chars=180) or result.title
    summary = _clean_model_text(str(payload.get("summary") or result.editor_summary or result.excerpt), max_chars=900)
    keywords = _model_keywords(payload.get("keywords"), fallback=list(result.keywords))
    content_type = _model_content_type(str(payload.get("content_type") or result.content_type))

    if not summary:
        summary = result.editor_summary or result.excerpt
    return replace(
        result,
        title=title,
        excerpt=summary,
        editor_summary=summary,
        keywords=tuple(keywords),
        content_type=content_type,
        enrichment_source="model",
    )


def _model_keywords(value: object, *, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        keyword = _clean_model_text(str(item), max_chars=48)
        key = keyword.lower()
        if not keyword or key in seen:
            continue
        cleaned.append(keyword)
        seen.add(key)
        if len(cleaned) >= 10:
            break
    return cleaned or fallback


def _model_content_type(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized == "newsletter_fallback":
        return "fallback_snippet"
    allowed = {"article", "opinion", "tutorial", "podcast", "discussion", "fallback_snippet"}
    return normalized if normalized in allowed else "article"


def _model_candidate_ids(results: list[ArticleFetchResult], max_items: int) -> set[int]:
    if max_items <= 0:
        return set()
    candidates = [result for result in results if result.fetched and result.tier != "dropped"]
    ranked = sorted(candidates, key=lambda result: result.link_score, reverse=True)
    return {id(result) for result in ranked[:max_items]}


def _clean_model_text(value: str, *, max_chars: int) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip().strip('"')
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[: max_chars - 1].rstrip()}..."


def _clean_article_text(title: str, text: str) -> str:
    cleaned = " ".join(text.split())
    normalized_title = " ".join(title.split())
    if normalized_title and cleaned.lower().startswith(normalized_title.lower()):
        cleaned = cleaned[len(normalized_title) :].lstrip(" :-")
    return cleaned


def _sentences(text: str) -> list[str]:
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", cleaned)
    return [part.strip() for part in parts if len(part.strip()) >= 35]


def _sentence_score(sentence: str, signal_words: set[str]) -> int:
    sentence_tokens = keyword_set(sentence)
    return len(sentence_tokens & signal_words)


def _looks_like_author_bio(title: str, haystack: str) -> bool:
    words = [word for word in re.findall(r"[A-Z][a-z]+", title) if word]
    if len(words) not in {2, 3}:
        return False
    author_markers = (
        "is a senior reporter",
        "is the chief content officer",
        "is an award-winning journalist",
        "previously led",
        "reach out to",
    )
    return any(marker in haystack for marker in author_markers)


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 1].rstrip()}..."


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).netloc.removeprefix("www.") or None


def _metrics_context_for_result(
    result: ArticleFetchResult,
    inference_run_id: str | None,
    metrics_mode: str,
) -> dict[str, object] | None:
    if not inference_run_id:
        return None
    return {
        "run_id": inference_run_id,
        "article_id": _article_metric_id(result),
        "mode": metrics_mode,
    }


def _record_model_metric(
    *,
    result: ArticleFetchResult,
    metrics_context: dict[str, object] | None,
    model_client: object,
    prompt: str,
    status: str,
    schema_valid: bool,
    fallback_triggered: bool,
    total_ms: int,
    queue_wait_ms: int | None = None,
    ttft_ms: int | None = None,
    generation_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    tokens_per_sec: float | None = None,
    classification_label: str | None = None,
    classification_confidence: float | None = None,
    summary_word_count: int | None = None,
    error_detail: str | None = None,
) -> None:
    if metrics_context is None:
        return

    model_name = _model_name(model_client)
    database.record_inference_metric(
        {
            "run_id": metrics_context.get("run_id"),
            "article_id": metrics_context.get("article_id") or _article_metric_id(result),
            "model": model_name,
            "model_tag": _model_tag(model_name),
            "quantization": _quantization(model_name),
            "backend": metrics_context.get("backend") or _backend_name(model_client),
            "mode": metrics_context.get("mode") or "single",
            "queue_wait_ms": queue_wait_ms,
            "ttft_ms": ttft_ms,
            "generation_ms": generation_ms,
            "total_ms": total_ms,
            "prompt_tokens": prompt_tokens if prompt_tokens is not None else _estimate_tokens(LIBRARIAN_SYSTEM_PROMPT, prompt),
            "completion_tokens": completion_tokens,
            "tokens_per_sec": tokens_per_sec,
            "classification_label": classification_label,
            "classification_confidence": classification_confidence,
            "schema_valid": schema_valid,
            "summary_word_count": summary_word_count,
            "fallback_triggered": fallback_triggered,
            "status": status,
            "error_detail": _truncate(error_detail, 600) if error_detail else None,
        }
    )


def _model_name(model_client: object) -> str:
    config = getattr(model_client, "config", None)
    model = getattr(config, "model", None)
    return str(model or get_settings().librarian_model or "unknown")


def _backend_name(model_client: object) -> str:
    config = getattr(model_client, "config", None)
    base_url = str(getattr(config, "base_url", None) or get_settings().model_base_url or "").lower()
    if "ollama" in base_url or ":11434" in base_url:
        return "ollama"
    if "llama" in base_url:
        return "llamacpp"
    if ":1234" in base_url or "omlx" in base_url or "127.0.0.1" in base_url:
        return "omlx"
    return "openai-compatible"


def _model_tag(model_name: str) -> str | None:
    lowered = model_name.lower()
    match = re.search(r"(oq\d+|q\d(?:_[a-z0-9]+)*|[468]bit|fp16|bf16)", lowered)
    return match.group(1) if match else None


def _quantization(model_name: str) -> str | None:
    lowered = model_name.lower()
    if "fp16" in lowered:
        return "fp16"
    if "bf16" in lowered:
        return "bf16"
    match = re.search(r"(?:oq|q)(\d+)", lowered)
    if match:
        return f"Q{match.group(1)}"
    match = re.search(r"([468])bit", lowered)
    if match:
        return f"Q{match.group(1)}"
    return None


def _article_metric_id(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    return str(
        metadata.get("article_id")
        or result.canonical_url
        or result.final_url
        or result.original_url
        or result.payload.id
    )


def _classification_confidence(payload: dict[str, object]) -> float | None:
    value = payload.get("classification_confidence") or payload.get("confidence")
    if not isinstance(value, (int, float)):
        return None
    return float(value)


def _word_count(value: str | None) -> int:
    if not value:
        return 0
    return len(re.findall(r"\b\w+\b", value))


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


def _estimate_tokens(*parts: str) -> int:
    text = " ".join(part for part in parts if part)
    if not text:
        return 0
    return max(1, round(len(text) / 4))


NOISY_DOMAINS = {
    "chatgpt.com",
    "email.beehiivstatus.com",
    "facebook.com",
    "instagram.com",
    "jobs.ashbyhq.com",
    "metacareers.com",
    "passionfroot.me",
    "tiktok.com",
    "unsplash.com",
    "x.com",
}

NOISY_URL_MARKERS = (
    "/author/",
    "/authors/",
    "/careers/",
    "/jobs/",
    "/polls/",
    "/profile/job_details",
    "/response?",
    "externalleadpage",
)

NOISY_TITLE_MARKERS = (
    "check out this image",
    "download this free",
    "instagram photos and videos",
    "make your day",
    "option a",
    "option b",
    "photo by ",
    "yes,",
)

NOISY_FETCHED_TEXT_MARKERS = (
    "download this free hd photo",
    "logo treatment at masthead",
    "we're one of the fastest-growing ai newsletters",
)

LIBRARIAN_SYSTEM_PROMPT = """You are a content librarian for a personal newspaper.
Return only valid JSON with these fields:
- title: canonical clean title for the primary article
- summary: 2-4 concise sentences about the content, not the source newsletter
- keywords: array of 5-10 topical and entity tags
- content_type: one of [article, opinion, tutorial, podcast, newsletter_fallback, discussion]
- confidence_note: short note only if the source text is weak or partial
No preamble, no markdown fences. Keep the whole response under 220 tokens."""

STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "because",
    "been",
    "being",
    "but",
    "can",
    "could",
    "from",
    "had",
    "has",
    "have",
    "how",
    "into",
    "its",
    "more",
    "new",
    "not",
    "now",
    "only",
    "our",
    "out",
    "over",
    "said",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "through",
    "today",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
    "you",
    "your",
}
