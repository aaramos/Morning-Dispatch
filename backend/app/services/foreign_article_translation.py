from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import fetch_articles_for_payloads
from backend.agents.model import ModelClient, ModelClientError
from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)


async def translate_foreign_article(payload: dict[str, Any]) -> dict[str, Any]:
    url = _safe_url(payload.get("url"))
    if not url:
        raise ValueError("A valid article URL is required")

    source_language = _clean(payload.get("source_language")).lower()
    source_language_name = _clean(payload.get("source_language_name")) or source_language.upper()
    cache_key = _cache_key(url=url, source_language=source_language)
    cached = _read_cache(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    title = _clean(payload.get("title")) or _clean(payload.get("original_title")) or url
    summary = _clean(payload.get("summary")) or _clean(payload.get("original_summary"))
    original_title = _clean(payload.get("original_title")) or title
    original_summary = _clean(payload.get("original_summary")) or summary

    fetched = await fetch_articles_for_payloads(
        [
            NormalizedPayload(
                source_type="foreign_web",
                source_name=_domain(url) or "Foreign media",
                raw_text=original_summary or summary,
                original_url=url,
                metadata={
                    "source_language": source_language,
                    "source_language_name": source_language_name,
                    "needs_translation": True,
                    "original_search_title": original_title,
                    "original_search_summary": original_summary,
                },
            )
        ],
        max_articles=1,
        concurrency=1,
    )
    result = fetched[0] if fetched else None
    original_body = _clean(getattr(result, "text", "") or original_summary or summary)
    fetch_status = str(getattr(result, "status", "") or "unavailable")
    original_unavailable = result is None or not getattr(result, "fetched", False) or len(original_body) < 180

    settings = get_settings()
    client = ModelClient.from_settings(settings)
    if client is None:
        response = {
            "status": "translation_unavailable",
            "url": url,
            "title": title,
            "source_language": source_language,
            "source_language_name": source_language_name,
            "translated_title": title,
            "translated_body": summary,
            "original_title": original_title,
            "original_body": original_body or original_summary,
            "original_unavailable": original_unavailable,
            "fetch_status": fetch_status,
            "notice": "Translation model is not available, so the original text is shown.",
            "cached": False,
        }
        _write_cache(cache_key, response)
        return response

    text_to_translate = original_body or original_summary or summary
    if not text_to_translate:
        response = {
            "status": "original_unavailable",
            "url": url,
            "title": title,
            "source_language": source_language,
            "source_language_name": source_language_name,
            "translated_title": title,
            "translated_body": summary,
            "original_title": original_title,
            "original_body": "",
            "original_unavailable": True,
            "fetch_status": fetch_status,
            "notice": "The original article body could not be fetched.",
            "cached": False,
        }
        _write_cache(cache_key, response)
        return response

    try:
        model_payload = await client.complete_json(
            system="You translate public foreign-language articles into clear English. Return strict JSON only.",
            prompt=_translation_prompt(
                url=url,
                source_language_name=source_language_name,
                title=original_title,
                body=text_to_translate,
            ),
            max_tokens=2600,
        )
    except ModelClientError as exc:
        logger.info("Foreign full-article translation failed for %s: %s", url, exc)
        fallback = await _translate_with_text_fallback(
            client,
            url=url,
            source_language_name=source_language_name,
            title=original_title,
            body=text_to_translate,
        )
        if fallback is not None:
            translated_title = _clean(fallback.get("title")) or title
            translated_body = _clean(fallback.get("body")) or summary or title
            response = {
                "status": "translated" if not original_unavailable else "translated_summary_only",
                "url": url,
                "title": translated_title,
                "source_language": source_language,
                "source_language_name": source_language_name,
                "translated_title": translated_title,
                "translated_body": translated_body,
                "original_title": original_title,
                "original_body": original_body or original_summary,
                "original_unavailable": original_unavailable,
                "fetch_status": fetch_status,
                "notice": (
                    "Original body could not be fully fetched; translated available text is shown."
                    if original_unavailable
                    else ""
                ),
                "cached": False,
            }
            _write_cache(cache_key, response)
            return response
        return {
            "status": "translation_failed",
            "url": url,
            "title": title,
            "source_language": source_language,
            "source_language_name": source_language_name,
            "translated_title": title,
            "translated_body": summary,
            "original_title": original_title,
            "original_body": original_body or original_summary,
            "original_unavailable": original_unavailable,
            "fetch_status": fetch_status,
            "notice": f"Translation failed: {exc.status}",
            "cached": False,
        }

    translated_title = _clean(model_payload.get("title_en")) if isinstance(model_payload, dict) else ""
    translated_body = _clean(model_payload.get("body_en")) if isinstance(model_payload, dict) else ""
    if not translated_body:
        translated_body = summary or title

    response = {
        "status": "translated" if not original_unavailable else "translated_summary_only",
        "url": url,
        "title": translated_title or title,
        "source_language": source_language,
        "source_language_name": source_language_name,
        "translated_title": translated_title or title,
        "translated_body": translated_body,
        "original_title": original_title,
        "original_body": original_body or original_summary,
        "original_unavailable": original_unavailable,
        "fetch_status": fetch_status,
        "notice": "Original body could not be fully fetched; translated available text is shown." if original_unavailable else "",
        "cached": False,
    }
    _write_cache(cache_key, response)
    return response


def _translation_prompt(*, url: str, source_language_name: str, title: str, body: str) -> str:
    clipped_body = _clip(body, 80_000)
    return json.dumps(
        {
            "task": "Translate this public article to English for a Morning Dispatch brief reader.",
            "source_language": source_language_name,
            "url": url,
            "title": title,
            "body": clipped_body,
            "rules": [
                "Translate faithfully; do not summarize unless the source text is repetitive boilerplate.",
                "Preserve company names, tickers, product names, numbers, dates, and quoted claims.",
                "Keep paragraph breaks readable.",
                "If the body contains navigation or cookie boilerplate, omit that boilerplate.",
                "Return strict JSON only.",
            ],
            "schema": {
                "title_en": "English title",
                "body_en": "Full English translation with paragraphs",
            },
        },
        ensure_ascii=False,
    )


async def _translate_with_text_fallback(
    client: ModelClient,
    *,
    url: str,
    source_language_name: str,
    title: str,
    body: str,
) -> dict[str, str] | None:
    try:
        response = await client.complete(
            system="You translate public foreign-language articles into clear English.",
            prompt=_translation_text_prompt(
                url=url,
                source_language_name=source_language_name,
                title=title,
                body=body,
            ),
            max_tokens=2600,
        )
    except ModelClientError as exc:
        logger.info("Foreign full-article fallback translation failed for %s: %s", url, exc)
        return None
    parsed = _parse_text_translation(response)
    if not parsed.get("title") and not parsed.get("body"):
        return None
    return parsed


def _translation_text_prompt(*, url: str, source_language_name: str, title: str, body: str) -> str:
    clipped_body = _clip(body, 80_000)
    return (
        f"Translate this public {source_language_name} article to English for a Morning Dispatch brief reader.\n"
        "Preserve company names, tickers, product names, numbers, dates, and quoted claims.\n"
        "Omit navigation, cookie banners, and repeated boilerplate.\n"
        "Return this plain text shape exactly:\n"
        "Title: <English title>\n"
        "Body:\n"
        "<English translation with readable paragraphs>\n\n"
        f"URL: {url}\n"
        f"Title: {title}\n\n"
        f"Body:\n{clipped_body}"
    )


def _parse_text_translation(value: str) -> dict[str, str]:
    text = str(value or "").strip()
    if not text:
        return {"title": "", "body": ""}
    title = ""
    body = ""
    title_match = re.search(r"(?im)^title:\s*(.+)$", text)
    if title_match:
        title = title_match.group(1).strip()
    body_match = re.search(r"(?ims)^body:\s*(.+)$", text)
    if body_match:
        body = body_match.group(1).strip()
    else:
        body = re.sub(r"(?im)^title:\s*.+$", "", text).strip()
    return {"title": _clean(title), "body": body.strip()}


def _read_cache(cache_key: str) -> dict[str, Any] | None:
    path = _cache_path(cache_key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_cache(cache_key: str, payload: dict[str, Any]) -> None:
    path = _cache_path(cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_path(cache_key: str) -> Path:
    return get_settings().data_dir / "foreign-article-cache" / f"{cache_key}.json"


def _cache_key(*, url: str, source_language: str) -> str:
    raw = "\n".join(("foreign-article-v1", source_language, url))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_url(value: Any) -> str:
    url = _clean(value)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clip(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rsplit(" ", 1)[0].strip() or value[:max_chars]
