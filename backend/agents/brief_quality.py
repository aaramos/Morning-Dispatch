from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from backend.agents.agentic import AgentDecision
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult

RAW_URL_RE = re.compile(r"https?://[^\s<>)\"']+", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]{1,180})\]\((https?://[^)\s]+)[^)]*\)", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
IMAGE_PLACEHOLDER_RE = re.compile(r"(?:[-–—]{2,}\s*)?View image:\s*\([^)]*(?:\)|$)\s*(?:Caption:\s*)?", re.IGNORECASE)
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff\u00ad]+")


def apply_brief_quality_checks(
    results: list[ArticleFetchResult],
) -> tuple[list[ArticleFetchResult], list[AgentDecision]]:
    cleaned_results: list[ArticleFetchResult] = []
    decisions: list[AgentDecision] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for result in results:
        url = result.final_url or result.original_url
        target = url or result.title
        if not result.fetched and _low_value_unresolved_link(result):
            decisions.append(
                _decision(
                    target=target,
                    decision="low_value_fallback",
                    action="drop_article",
                    reason="Dropped a blocked newsletter section or utility link before rendering the brief.",
                )
            )
            continue
        if result.fetched and not _is_http_url(url):
            decisions.append(
                _decision(
                    target=target,
                    decision="broken_link",
                    action="drop_article",
                    reason="Dropped a fetched article because its final URL was not a usable web link.",
                )
            )
            continue

        url_key = _url_key(url)
        title = clean_display_text(result.title) or result.title
        title_key = _title_key(title)
        if (url_key and url_key in seen_urls) or (title_key and title_key in seen_titles):
            decisions.append(
                _decision(
                    target=target,
                    decision="duplicate",
                    action="drop_article",
                    reason="Dropped a duplicate story before rendering the brief.",
                )
            )
            continue

        summary = clean_display_text(result.editor_summary or result.excerpt)
        if _weak_summary(summary):
            repaired_summary = clean_display_text(result.excerpt or result.text or result.title)
            if not _weak_summary(repaired_summary):
                summary = repaired_summary
                decisions.append(
                    _decision(
                        target=target,
                        decision="summary_repaired",
                        action="repair_article",
                        reason="Repaired a weak or noisy article summary before rendering the brief.",
                    )
                )
            elif not result.fetched:
                decisions.append(
                    _decision(
                        target=target,
                        decision="weak_fallback",
                        action="drop_article",
                        reason="Dropped an unresolved link because its fallback summary was too weak.",
                    )
                )
                continue

        payload = _payload_with_date(result.payload)
        if payload is not result.payload:
            decisions.append(
                _decision(
                    target=target,
                    decision="missing_date",
                    action="repair_article",
                    reason="Filled a missing article date from the fetch timestamp.",
                )
            )

        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        cleaned_results.append(
            replace(
                result,
                payload=payload,
                title=title,
                excerpt=summary or result.excerpt,
                editor_summary=summary or result.editor_summary,
            )
        )

    return cleaned_results, decisions


def clean_display_text(value: str | None) -> str:
    text = unescape(value or "")
    text = ZERO_WIDTH_RE.sub(" ", text)
    text = IMAGE_PLACEHOLDER_RE.sub(" ", text)
    text = MARKDOWN_LINK_RE.sub(lambda match: f" {match.group(1)} ", text)
    text = RAW_URL_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = text.replace("**", " ").replace("__", " ").replace("`", " ")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -|")


def _payload_with_date(payload: NormalizedPayload) -> NormalizedPayload:
    if payload.published_at:
        return payload
    fallback_date = payload.fetched_at or datetime.now(UTC).isoformat(timespec="seconds")
    return replace(payload, published_at=fallback_date)


def _is_http_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _url_key(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        if not key.lower().startswith("utm_"):
            query_items.append((key, value))
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query_items), ""))


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _weak_summary(summary: str) -> bool:
    if len(summary.split()) < 6:
        return True
    if "http://" in summary.lower() or "https://" in summary.lower():
        return True
    if "<" in summary and ">" in summary:
        return True
    return False


def _low_value_unresolved_link(result: ArticleFetchResult) -> bool:
    url = result.final_url or result.original_url
    domain = (result.domain or urlparse(url or "").netloc).lower().removeprefix("www.")
    title = clean_display_text(result.title).lower()
    return domain == "link.mail.beehiiv.com" and any(
        phrase in title for phrase in LOW_VALUE_NEWSLETTER_LINK_TEXT
    )


def _decision(*, target: str, decision: str, action: str, reason: str) -> AgentDecision:
    return AgentDecision(
        agent="brief_quality",
        target=target,
        decision=decision,
        action=action,
        confidence=0.92,
        reason=reason,
    )


LOW_VALUE_NEWSLETTER_LINK_TEXT = (
    "community ai workflows",
    "highlights: news, guides & events",
    "join the ai university",
    "trending ai tools",
)
