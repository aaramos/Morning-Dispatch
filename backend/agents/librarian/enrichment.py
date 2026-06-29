from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import replace
from time import perf_counter
from typing import Iterable
from urllib.parse import urlparse

from backend.agents.discovery.language_support import trusted_script_language_patterns
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.model import MODEL_CAPACITY_STATUS, ModelClient, ModelClientConfig, ModelClientError
from backend.app.core.config import get_settings
from backend.app.core.prompt_loader import load_prompt
from backend.app.db import database


MAX_SUMMARY_CHARS = 620
# Per-article enrichment input clip: how much of the article body the librarian model
# sees when writing the title/summary/keywords. Larger windows improve summary
# faithfulness and entity coverage; sized for 32k+ context models (Qwen3.6-35B, Gemma-4).
MAX_MODEL_TEXT_CHARS = 12500
# Foreign full-article translation: translate the whole body, not a clipped lead,
# so a selected article is delivered in full English rather than a partial pass.
FOREIGN_TRANSLATION_BODY_CHARS = 16000
FOREIGN_TRANSLATION_MAX_TOKENS = 4000
# Cap how many foreign-language articles get a full (body-in, up to 4000-token-out)
# translation per build. Translation is the dominant per-article model cost and is
# serialized by the model server's concurrency=1, so an uncapped foreign-heavy run
# can stretch the rank stage to an hour+. The cap keeps the top-ranked foreign items;
# lower-ranked foreign articles are kept untranslated (demoted) rather than blocking
# the build. The rank stage also has an overall wall-clock budget as a final backstop.
MAX_FOREIGN_TRANSLATIONS_PER_RUN = 12
logger = logging.getLogger(__name__)
MODEL_ENRICHED_SOURCES = {"model", "model_cache", "model_fallback"}


async def enrich_articles(
    results: Iterable[ArticleFetchResult],
    *,
    model_client: ModelClient | None = None,
    translation_client: ModelClient | None = None,
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
        enrich_article_with_model(
            result,
            model_client=client if id(result) in model_result_ids or _needs_metadata_translation(result) else None,
            translation_client=translation_client,
        )
        for result in result_list
    ]
    for enriched_result in await asyncio.gather(*tasks):
        if enriched_result.tier == "dropped":
            continue
        enriched.append(enriched_result)
    return enriched


def enrich_article(result: ArticleFetchResult) -> ArticleFetchResult:
    if result.payload.source_type == "market_snapshot":
        source_text = _market_snapshot_text(result)
        keywords = extract_keywords(source_text, result.title)
        return replace(
            result,
            text=source_text,
            excerpt=source_text,
            editor_summary=source_text,
            keywords=tuple(keywords),
            content_type="market",
            enrichment_source="deterministic",
        )

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
    translation_client: ModelClient | None = None,
    metrics_context: dict[str, object] | None = None,
) -> ArticleFetchResult:
    if result.enrichment_source in MODEL_ENRICHED_SOURCES:
        return result

    deterministic = enrich_article(result)
    if _needs_metadata_translation(deterministic):
        # Foreign translation runs on the dedicated translation route when one is
        # supplied, falling back to the librarian/default client otherwise.
        return await _assess_and_translate_foreign(
            deterministic,
            model_client=translation_client if translation_client is not None else model_client,
        )

    if model_client is None or deterministic.tier == "dropped" or not deterministic.fetched:
        return deterministic

    prompt = _librarian_prompt(deterministic)
    started_at = perf_counter()
    model_response = None
    try:
        if hasattr(model_client, "complete_json_with_metrics"):
            model_response, model_payload = await model_client.complete_json_with_metrics(
                system=load_prompt("librarian"),
                prompt=prompt,
                max_tokens=220,
            )
        else:
            model_payload = await model_client.complete_json(
                system=load_prompt("librarian"),
                prompt=prompt,
                max_tokens=220,
            )
    except ModelClientError as exc:
        logger.info("Librarian model enrichment failed for %s: %s", result.original_url, exc)
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
        if exc.status == MODEL_CAPACITY_STATUS:
            fallback = _fallback_model_client(model_client)
            if fallback is not None:
                return await _retry_with_fallback_model(
                    deterministic,
                    prompt=prompt,
                    fallback_client=fallback,
                    metrics_context=metrics_context,
                )
            return replace(deterministic, enrichment_source="model_capacity_fallback")
        return replace(deterministic, enrichment_source="deterministic")

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


async def _retry_with_fallback_model(
    result: ArticleFetchResult,
    *,
    prompt: str,
    fallback_client: ModelClient,
    metrics_context: dict[str, object] | None,
) -> ArticleFetchResult:
    started_at = perf_counter()
    model_response = None
    try:
        if hasattr(fallback_client, "complete_json_with_metrics"):
            model_response, model_payload = await fallback_client.complete_json_with_metrics(
                system=load_prompt("librarian"),
                prompt=prompt,
                max_tokens=220,
            )
        else:
            model_payload = await fallback_client.complete_json(
                system=load_prompt("librarian"),
                prompt=prompt,
                max_tokens=220,
            )
    except ModelClientError as exc:
        _record_model_metric(
            result=result,
            metrics_context=metrics_context,
            model_client=fallback_client,
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
            summary_word_count=_word_count(result.editor_summary or result.excerpt),
            error_detail=str(exc),
        )
        return replace(result, enrichment_source="model_capacity_fallback")

    enriched = _apply_model_payload(result, model_payload)
    enriched = replace(enriched, enrichment_source="model_fallback")
    _record_model_metric(
        result=enriched,
        metrics_context=metrics_context,
        model_client=fallback_client,
        prompt=prompt,
        status="success",
        schema_valid=True,
        fallback_triggered=True,
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


def _fallback_model_client(model_client: object) -> ModelClient | None:
    settings = get_settings()
    config = getattr(model_client, "config", None)
    current_model = str(getattr(config, "model", "") or settings.librarian_model or "")
    # Fall back to the configured default model (settings.librarian_model, currently
    # Qwen3.6-35B-A3B-oQ6-mtp) rather than a hard-coded id, so the retry target follows
    # config and can't go stale. When the librarian route already runs the default,
    # current_model == fallback_model and the guard below returns None (no point retrying
    # the same model — capacity errors then degrade to deterministic enrichment).
    fallback_model = settings.librarian_model
    if not fallback_model or current_model == fallback_model:
        return None
    base_url = str(getattr(config, "base_url", None) or settings.model_base_url or "")
    if not base_url:
        return None
    return ModelClient(
        ModelClientConfig(
            base_url=base_url,
            model=fallback_model,
            api_key=getattr(config, "api_key", None) or settings.model_api_key,
            timeout_seconds=float(getattr(config, "timeout_seconds", None) or settings.model_timeout_seconds),
            concurrency=int(getattr(config, "concurrency", None) or settings.model_concurrency),
        )
    )


async def refine_ranked_articles_with_model(
    results: Iterable[ArticleFetchResult],
    *,
    model_client: ModelClient | None = None,
    translation_client: ModelClient | None = None,
    model_max_items: int | None = None,
    inference_run_id: str | None = None,
    metrics_mode: str = "single",
) -> list[ArticleFetchResult]:
    result_list = list(results)
    settings = get_settings()
    client = model_client if model_client is not None else ModelClient.from_settings(settings)
    # Foreign translation runs on its own route so it can be pointed at a
    # translation-tuned model independently of the librarian. Fall back to the
    # librarian/default client when no dedicated translation client is supplied.
    translator = translation_client if translation_client is not None else client
    max_items = settings.librarian_model_max_items if model_max_items is None else max(0, model_max_items)
    translations_used = 0
    tasks = []
    for index, result in enumerate(result_list):
        needs_translation = _needs_metadata_translation(result)
        if needs_translation:
            # Foreign translation is the dominant per-article cost and is serialized by
            # the model server. Cap how many we run per build (top-ranked first, since
            # result_list is already ranked) so one foreign-heavy run can't grind the
            # rank stage for an hour and jam the queue. Over-budget foreign items are
            # kept untranslated rather than translated.
            use_client = translator is not None and translations_used < MAX_FOREIGN_TRANSLATIONS_PER_RUN
            if use_client:
                translations_used += 1
            tasks.append(
                enrich_article_with_model(
                    result,
                    model_client=client if use_client else None,
                    translation_client=translator if use_client else None,
                    metrics_context=_metrics_context_for_result(result, inference_run_id, metrics_mode)
                    if use_client and inference_run_id
                    else None,
                )
            )
        else:
            use_client = client is not None and index < max_items
            tasks.append(
                enrich_article_with_model(
                    result,
                    model_client=client if use_client else None,
                    metrics_context=_metrics_context_for_result(result, inference_run_id, metrics_mode)
                    if use_client and inference_run_id
                    else None,
                )
            )
    return list(await asyncio.gather(*tasks))


async def _assess_and_translate_foreign(
    result: ArticleFetchResult,
    *,
    model_client: ModelClient | None,
) -> ArticleFetchResult:
    """Assess translatability of a foreign article and fully translate it if confident.

    Drops the article (tier='dropped') when the model reports low confidence or
    cannot parse the content.  Fully translates title + body when confidence is
    medium or high, replacing the article text so it enters the brief in English.
    """
    source_language = _source_language(result)
    source_language_name = _source_language_name(result)
    original_title = _original_title(result)
    original_summary = _original_summary(result)

    # Best available body — prefer a fully fetched body over snippet
    body = result.text or ""
    if len(body) < 200:
        body = result.excerpt or result.editor_summary or original_summary or body
    clipped_body = body[:FOREIGN_TRANSLATION_BODY_CHARS]

    if model_client is None:
        # No model — cannot assess; demote rather than silently drop
        return _mark_translation_unavailable(
            result,
            {
                "translated": False,
                "source_language": source_language,
                "source_language_name": source_language_name,
                "translator": None,
                "mode": "unavailable",
                "stage": "assess_and_translate",
                "original_title": original_title,
                "original_summary": original_summary,
                "error": "translation model unavailable",
            },
        )

    prompt = json.dumps(
        {
            "task": (
                "Assess whether you can translate this foreign-language article to English "
                "with sufficient confidence. If yes, translate it fully."
            ),
            "source_language_hint": source_language_name or source_language or "unknown",
            "url": result.final_url or result.original_url,
            "title": original_title,
            "body": clipped_body,
            "rules": [
                "Set can_translate to true only if you can produce an accurate, fluent English translation.",
                "Set confidence to 'low' if the text is under 30 words, mostly garbled, "
                "or in a script you cannot reliably parse.",
                "If can_translate is true: translate the full body faithfully — do not summarize "
                "unless the source is repetitive boilerplate or navigation text.",
                "Preserve company names, tickers, product names, numbers, dates, and quoted claims.",
                "If can_translate is false or confidence is low, leave title_en and body_en empty "
                "and set drop_reason to a short explanation.",
            ],
            "schema": {
                "can_translate": "boolean",
                "confidence": "high|medium|low",
                "detected_language": "ISO 639-1 code if identifiable, else empty string",
                "title_en": "English title (empty string if cannot translate)",
                "body_en": "full English translation (empty string if cannot translate)",
                "drop_reason": "brief reason if dropping, else empty string",
            },
        },
        ensure_ascii=False,
    )

    try:
        payload = await model_client.complete_json(
            system=(
                "You evaluate whether foreign-language content can be reliably translated to English, "
                "then translate it in full when possible. Return strict JSON only."
            ),
            prompt=prompt,
            max_tokens=FOREIGN_TRANSLATION_MAX_TOKENS,
        )
    except Exception as exc:
        logger.info("Foreign translation assessment failed for %s: %s", result.original_url, exc)
        return _mark_translation_unavailable(
            result,
            {
                "translated": False,
                "source_language": source_language,
                "source_language_name": source_language_name,
                "translator": _model_name(model_client),
                "mode": "assess_failed",
                "stage": "assess_and_translate",
                "original_title": original_title,
                "original_summary": original_summary,
                "error": str(exc)[:220],
            },
        )

    if not isinstance(payload, dict):
        payload = {}

    can_translate = bool(payload.get("can_translate"))
    confidence = str(payload.get("confidence") or "").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium" if can_translate else "low"

    # Drop when model signals it cannot translate or confidence is low
    if not can_translate or confidence == "low":
        drop_reason = (
            str(payload.get("drop_reason") or "").strip()
            or "Content could not be translated with sufficient confidence."
        )
        logger.info("Foreign article dropped (untranslatable): %s — %s", result.original_url, drop_reason)
        return replace(
            result,
            tier="dropped",
            metadata={
                **dict(result.metadata or {}),
                "translation": {
                    "translated": False,
                    "source_language": source_language,
                    "source_language_name": source_language_name,
                    "translator": _model_name(model_client),
                    "confidence": confidence,
                    "drop_reason": drop_reason,
                    "mode": "dropped",
                },
            },
        )

    # Translation succeeded — build fully English article
    translated_title = _clean_model_text(str(payload.get("title_en") or ""), max_chars=220)
    translated_body = str(payload.get("body_en") or "").strip()
    detected_language = str(payload.get("detected_language") or source_language or "").strip()

    if not translated_title and not translated_body:
        # Model claimed it could translate but returned nothing — demote rather than drop
        return _mark_translation_unavailable(
            result,
            {
                "translated": False,
                "source_language": source_language,
                "source_language_name": source_language_name,
                "translator": _model_name(model_client),
                "mode": "empty_output",
                "stage": "assess_and_translate",
                "original_title": original_title,
                "original_summary": original_summary,
            },
        )

    title_for_enrichment = translated_title or original_title or result.title
    cleaned_body = _clean_article_text(title_for_enrichment, translated_body)
    kw_text = f"{title_for_enrichment} {cleaned_body}"
    keywords = extract_keywords(kw_text, title_for_enrichment)
    summary = summarize(title_for_enrichment, cleaned_body, set(keywords))

    return replace(
        result,
        title=translated_title or result.title,
        text=cleaned_body or result.text,
        excerpt=summary or _truncate(translated_body, MAX_SUMMARY_CHARS) or result.excerpt,
        editor_summary=summary or _truncate(translated_body, MAX_SUMMARY_CHARS) or result.editor_summary,
        keywords=tuple(keywords),
        content_type="article" if result.content_type == "fallback_snippet" else result.content_type,
        metadata={
            **dict(result.metadata or {}),
            "translation": {
                "translated": True,
                "source_language": detected_language or source_language,
                "source_language_name": source_language_name,
                "translator": _model_name(model_client),
                "confidence": confidence,
                "mode": "assess_and_translate",
                "stage": "full",
                "original_title": original_title,
                "original_summary": original_summary,
                "original_body": clipped_body,
            },
        },
        enrichment_source="model",
    )


def _mark_translation_unavailable(
    result: ArticleFetchResult,
    translation: dict[str, object],
) -> ArticleFetchResult:
    return replace(
        result,
        tier="dropped",
        metadata={**dict(result.metadata or {}), "translation": translation},
    )


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
    if result.payload.source_type == "podcast_episode":
        return "podcast"
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


def _market_snapshot_text(result: ArticleFetchResult) -> str:
    metadata = {**dict(result.payload.metadata or {}), **dict(result.metadata or {})}
    ticker = str(metadata.get("ticker") or "").strip()
    company = str(metadata.get("company_name") or result.title or result.payload.source_name or ticker or "this company").strip()
    if ticker and ticker not in company:
        company = f"{company} ({ticker})"
    parts = [f"Public-market snapshot for {company}."]
    price = _market_number(metadata.get("current_price"))
    currency = str(metadata.get("currency") or "").strip()
    if price is not None:
        parts.append(f"Latest price: {price:.2f}{f' {currency}' if currency else ''}.")
    changes = []
    for label, key in (
        ("1d", "change_1d_pct"),
        ("7d", "change_7d_pct"),
        ("30d", "change_30d_pct"),
        ("3mo", "change_3m_pct"),
    ):
        value = _market_number(metadata.get(key))
        if value is not None:
            changes.append(f"{label}: {value:+.2f}%")
    if changes:
        parts.append(f"Recent movement: {'; '.join(changes)}.")
    market_cap = _market_number(metadata.get("market_cap"))
    if market_cap is not None:
        parts.append(f"Market cap: {_format_market_cap(market_cap)}.")
    analyst_rating = str(metadata.get("analyst_rating") or "").strip()
    if analyst_rating:
        parts.append(f"Analyst rating: {analyst_rating}.")
    sector = str(metadata.get("sector") or "").strip()
    if sector:
        parts.append(f"Sector: {sector}.")
    news = metadata.get("recent_news")
    if isinstance(news, list):
        titles = [
            str(item.get("title") or "").strip()
            for item in news
            if isinstance(item, dict) and str(item.get("title") or "").strip()
        ]
        if titles:
            parts.append(f"Recent news: {'; '.join(titles[:3])}.")
    if len(parts) == 1:
        parts.append("Use the linked quote page for live price, valuation, and recent market news.")
    return " ".join(parts)


def _market_number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _format_market_cap(value: float) -> str:
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    return f"${value:,.0f}"


def _needs_metadata_translation(result: ArticleFetchResult) -> bool:
    metadata = _translation_source_metadata(result)
    if result.payload.source_type == "foreign_web":
        return True
    return bool(metadata.get("needs_translation") and metadata.get("source_language"))


def _source_language(result: ArticleFetchResult) -> str:
    metadata = _translation_source_metadata(result)
    return str(metadata.get("source_language") or "").strip().lower()


def _source_language_name(result: ArticleFetchResult) -> str:
    metadata = _translation_source_metadata(result)
    return str(metadata.get("source_language_name") or _source_language(result)).strip()


def _original_title(result: ArticleFetchResult) -> str:
    metadata = _translation_source_metadata(result)
    return str(metadata.get("original_search_title") or result.title or result.payload.source_name or "").strip()


def _original_summary(result: ArticleFetchResult) -> str:
    metadata = _translation_source_metadata(result)
    return str(
        metadata.get("original_search_summary")
        or result.editor_summary
        or result.excerpt
        or result.text
        or result.payload.raw_text
        or ""
    ).strip()[:1200]


def _translation_source_metadata(result: ArticleFetchResult) -> dict[str, object]:
    metadata = {
        **dict(result.payload.metadata or {}),
        **dict(result.metadata or {}),
    }
    if metadata.get("needs_translation") and metadata.get("source_language"):
        return metadata
    detected = _detect_non_english_script(result)
    if detected is None:
        return metadata
    language_code, language_name = detected
    return {
        **metadata,
        "needs_translation": True,
        "source_language": language_code,
        "source_language_name": language_name,
        "original_search_title": metadata.get("original_search_title") or result.title,
        "original_search_summary": metadata.get("original_search_summary")
        or result.editor_summary
        or result.excerpt
        or result.payload.raw_text
        or result.text,
    }


def _detect_non_english_script(result: ArticleFetchResult) -> tuple[str, str] | None:
    if result.payload.source_type in {"podcast_episode", "youtube_video", "collection_chunk", "market_snapshot", "reddit_post", "reddit_thread"}:
        return None
    text = " ".join(
        str(part or "")
        for part in (
            result.title,
            result.editor_summary,
            result.excerpt,
            result.payload.raw_text,
            result.text[:1200],
        )
    )
    if not text.strip():
        return None
    for code, name, _script, pattern in trusted_script_language_patterns():
        matches = pattern.findall(text)
        if len(matches) >= 4:
            return code, name
    return None


def _librarian_prompt(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    text = (result.text or result.excerpt or fallback_text(result))[:MAX_MODEL_TEXT_CHARS]

    if metadata.get("content_basis") == "youtube_metadata":
        title = metadata.get("youtube_title") or result.title
        channel = metadata.get("channel_name") or result.payload.source_name or "YouTube"
        desc = metadata.get("description") or ""
        return f"""You are summarizing a YouTube video based ONLY on its metadata (title and description) because the transcript is unavailable.
Generate a summary of the video content.
Title: {title}
Channel: {channel}
Metadata Description:
{desc}

Rules:
1. Do NOT imply the video was watched, transcribed, or analyzed directly.
2. Use phrasing that makes it clear the summary is based on metadata/description (e.g. "A video by {channel} titled '{title}' describes...", "According to the description, this video discusses...").
3. Keep the summary under 70 words. Keep keyword labels short. Return compact JSON only.
"""

    if result.payload.source_type == "podcast_episode":
        show_name = metadata.get("podcast_title") or result.payload.source_name or "Podcast"
        episode_title = metadata.get("title") or result.title or "Podcast episode"
        source_basis = str(metadata.get("transcript_source") or "show_notes").strip()
        basis_label = "transcript" if source_basis in {"transcript", "transcript_cache"} else "show notes"
        existing_summary = str(result.excerpt or result.editor_summary or "").strip()
        summary_seed = (
            f"\nExisting summary:\n{existing_summary}\n"
            if existing_summary
            else ""
        )

        return f"""You are summarizing a podcast episode.
Use the most useful source material available and keep the summary concise.
Title: {episode_title}
Podcast: {show_name}
Basis: {basis_label}
{summary_seed}
Text:
{text}

        Rules:
1. Prefer the existing summary when it is accurate and useful.
2. If the provided summary is missing or weak, generate one that is close and practical.
3. Keep the summary under 70 words. Keep keyword labels short. Return compact JSON only.
"""

    if result.payload.source_type in ("reddit_thread", "reddit_post"):
        source_label = "Reddit Post"
    elif result.payload.source_type == "podcast_episode":
        source_label = "Podcast"
    elif result.payload.source_type == "youtube_video":
        source_label = "YouTube Video"
    else:
        source_label = "Source newsletter"
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
            "route_name": getattr(getattr(model_client, "config", None), "route_name", None),
            "mode": metrics_context.get("mode") or "single",
            "queue_wait_ms": queue_wait_ms,
            "ttft_ms": ttft_ms,
            "generation_ms": generation_ms,
            "total_ms": total_ms,
            "prompt_tokens": prompt_tokens if prompt_tokens is not None else _estimate_tokens(load_prompt("librarian"), prompt),
            "completion_tokens": completion_tokens,
            "tokens_per_sec": tokens_per_sec,
            "classification_label": classification_label,
            "classification_confidence": classification_confidence,
            "schema_valid": schema_valid,
            "summary_word_count": summary_word_count,
            "fallback_triggered": fallback_triggered or bool(getattr(model_client, "fallback_triggered", False)),
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
    provider = str(getattr(config, "provider", "") or "")
    if provider:
        return provider
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

# LIBRARIAN_SYSTEM_PROMPT is loaded dynamically via load_prompt("librarian")

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
