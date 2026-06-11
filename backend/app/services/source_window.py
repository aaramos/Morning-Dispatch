"""Source-window (recency) filtering, date hints, and undated-item handling.

Extracted verbatim from ``explore.py`` (M3 split): the bounded-window filter and
its reserve/revival helpers, pre-filter AI date adjudication, URL/text/locale
date parsing, undated-item once-only bookkeeping, and foreign-language coverage
notes.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import re
from typing import Any

from backend.agents.discovery import TopicProfile
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian.date_text import normalize_date_string
from backend.agents.source_audit import apply_source_audit
from backend.app.core.config import get_settings
from backend.app.db import database
from backend.app.services import model_routing


_STRICT_SOURCE_WINDOW_TYPES = {"gmail_link", "foreign_web", "podcast_episode", "reddit_post"}


_DATE_METADATA_KEYS = (
    "published_at",
    "published",
    "publication_date",
    "date",
    "pub_date",
    "created_at",
    "updated_at",
    "search_result_date",
)
_URL_DATE_RE = re.compile(
    r"(?:^|[^\d])(?P<year>20\d{2})[/-](?P<month>0?[1-9]|1[0-2])"
    r"(?:[/-](?P<day>0?[1-9]|[12]\d|3[01]))?(?:[^\d]|$)"
)
_TEXT_DATE_RE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?"
    r"\s+(?P<day>0?[1-9]|[12]\d|3[01]),?\s+(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
# Locale numeric dates used by Korean/Japanese/Chinese outlets that never emit
# English month text or ISO meta (e.g. 2026年4月25日, 2026년 4월 25일, 2026.04.25).
_CJK_DATE_RE = re.compile(
    r"(20\d{2})\s*[年년]\s*(1[0-2]|0?[1-9])\s*[月월]\s*(3[01]|[12]\d|0?[1-9])\s*[日일]?"
)
_DOTTED_DATE_RE = re.compile(
    r"(?:^|[^\d])(20\d{2})\.(1[0-2]|0?[1-9])\.(3[01]|[12]\d|0?[1-9])(?:[^\d]|$)"
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _adapter_from_payload_type(source_type: str, metadata: dict[str, Any] | None = None) -> str:
    metadata = metadata or {}
    if source_type == "gmail_link":
        if metadata.get("search_query") or metadata.get("search_provider"):
            if metadata.get("search_provider") == "google_news_rss":
                return "google_news"
            return "web_search"
        return "gmail"
    return {
        "gmail": "gmail",
        "podcast_episode": "podcasts",
        "youtube_video": "youtube",
        "foreign_web": "foreign_media",
        "market_snapshot": "markets",
        "collection_chunk": "collections",
        "web_search": "web_search",
        "reddit_post": "reddit",
    }.get(source_type, "")


async def _adjudicate_dates_before_source_window_filter(
    profile: TopicProfile,
    article_results: list[ArticleFetchResult],
    *,
    lookback_hours: int | None,
    inference_run_id: str,
    max_candidates: int | None,
    low_yield: bool = False,
) -> tuple[list[ArticleFetchResult], dict[str, Any]]:
    if lookback_hours is None:
        return article_results, {}
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))
    at_risk_indexes = _source_window_date_adjudication_indexes(profile, article_results, cutoff)
    if not at_risk_indexes:
        return article_results, {
            "status": "skipped",
            "candidate_count": 0,
            "message": "No articles needed AI date review before source-window filtering.",
        }

    limit = max(1, min(int(max_candidates or len(at_risk_indexes)), len(at_risk_indexes)))
    # Round-robin selection by lane/adapter to prevent lane monopolization (P0-3)
    grouped: dict[str, list[int]] = {}
    for index in at_risk_indexes:
        adapter = _adapter_from_payload_type(
            article_results[index].payload.source_type,
            article_results[index].payload.metadata,
        )
        grouped.setdefault(adapter, []).append(index)
    
    selected_indexes: list[int] = []
    adapters = sorted(grouped.keys())
    pointers = {adapter: 0 for adapter in adapters}
    while len(selected_indexes) < limit:
        added = False
        for adapter in adapters:
            ptr = pointers[adapter]
            if ptr < len(grouped[adapter]):
                selected_indexes.append(grouped[adapter][ptr])
                pointers[adapter] += 1
                added = True
                if len(selected_indexes) >= limit:
                    break
        if not added:
            break
    selected_results = [article_results[index] for index in selected_indexes]
    settings = get_settings()
    audit_client = model_routing.client_for_agent(
        "source_audit",
        settings=settings,
        items=selected_results,
        model_override=profile.models.get("brief"),
    ).client
    reviewed_results, _decisions, audit_summary = await apply_source_audit(
        profile,
        selected_results,
        lookback_hours=lookback_hours,
        model_client=audit_client,
        inference_run_id=inference_run_id,
        max_candidates=limit,
        low_yield=low_yield,
    )
    updated = list(article_results)
    resolved_count = 0
    for original_index, reviewed in zip(selected_indexes, reviewed_results, strict=False):
        if _article_published_at(reviewed) is not None and _article_published_at(article_results[original_index]) is None:
            resolved_count += 1
        updated[original_index] = reviewed

    status = str(audit_summary.get("status") or "completed")
    return updated, {
        "status": status,
        "candidate_count": len(selected_indexes),
        "at_risk_count": len(at_risk_indexes),
        "resolved_count": resolved_count,
        "excluded_count": int(audit_summary.get("excluded_count") or 0),
        "message": (
            "AI reviewed ambiguous dates before source-window filtering."
            if status not in {"failed", "fallback"}
            else "AI date review could not fully complete before source-window filtering."
        ),
        "summary": str(audit_summary.get("summary") or "").strip(),
        "issues": list(audit_summary.get("issues") or []),
    }


def _source_window_date_adjudication_indexes(
    profile: TopicProfile,
    article_results: list[ArticleFetchResult],
    cutoff: datetime,
) -> list[int]:
    indexes: list[int] = []
    for index, result in enumerate(article_results):
        if result.tier == "dropped" or not result.fetched:
            continue
        if _article_published_at(result) is not None:
            continue
        reason = _source_window_rejection_reason(profile, result, cutoff)
        if reason:
            indexes.append(index)
    return indexes


def _apply_source_window_filter(
    profile: TopicProfile,
    article_results: list[ArticleFetchResult],
    *,
    lookback_hours: int | None,
) -> tuple[list[ArticleFetchResult], list[dict[str, str]], list[ArticleFetchResult]]:
    """Split fetched items into in-window keepers and an out-of-window reserve.

    Items that fail the recency window are NOT discarded (P0): they are demoted
    into a reserve pool so a selected source that has zero in-window survivors can
    still surface a minimal, clearly-labeled fallback item via _apply_source_floors,
    rather than rendering an empty section.
    """
    if lookback_hours is None:
        kept: list[ArticleFetchResult] = []
        issues: list[dict[str, str]] = []
        for result in article_results:
            if result.tier == "dropped":
                continue
            if _is_strict_undated_result(result):
                item_key = _undated_item_key(result)
                if database.has_served_undated_item(profile.topic_id, item_key):
                    reason = "Undated item was already shown once and is hidden from future editions."
                    issues.append(
                        {
                            "source_name": _source_window_issue_name(result),
                            "source": _source_label_for_result(result),
                            "item": _source_window_issue_name(result),
                            "item_url": str(
                                result.final_url
                                or result.original_url
                                or result.payload.original_url
                                or ""
                            ).strip(),
                            "reason": reason,
                        }
                    )
                    continue
                kept.append(_mark_undated_once(result))
            else:
                kept.append(result)
        return kept, issues, []
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))
    kept: list[ArticleFetchResult] = []
    issues: list[dict[str, str]] = []
    reserve: list[ArticleFetchResult] = []
    for result in article_results:
        reason = _source_window_rejection_reason(profile, result, cutoff)
        if reason:
            issues.append(
                {
                    "source_name": _source_window_issue_name(result),
                    "source": _source_label_for_result(result),
                    "item": _source_window_issue_name(result),
                    "item_url": str(result.final_url or result.original_url or result.payload.original_url or "").strip(),
                    "reason": reason,
                }
            )
            if result.fetched:
                reserve.append(_mark_out_of_window(result, reason))
            continue
        kept.append(_mark_undated_once(result) if _is_strict_undated_result(result) else result)
    return kept, issues, reserve


def _mark_out_of_window(result: ArticleFetchResult, reason: str) -> ArticleFetchResult:
    """Tag a recency-rejected (but fetched) item for last-resort floor revival."""
    metadata = {
        **dict(result.metadata or {}),
        "out_of_window": True,
        "out_of_window_reason": reason,
        "date_status": "out_of_window",
    }
    freshness = _article_published_at(result) or _article_text_or_url_date(result)
    if freshness is not None:
        metadata["out_of_window_published_at"] = freshness.isoformat()
    return replace(result, tier="dropped", metadata=metadata)


def _reserve_sort_key(result: ArticleFetchResult) -> tuple[str, float]:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    published = str(metadata.get("out_of_window_published_at") or "")
    return (published, float(result.link_score or 0.0))


def _revive_out_of_window(result: ArticleFetchResult) -> ArticleFetchResult:
    """Promote a reserve item into the brief with an honest out-of-window note."""
    metadata = {
        **dict(result.metadata or {}),
        "served_once": True,
        "served_once_note": "Outside the requested window — shown as a fallback.",
        "served_once_key": _undated_item_key(result),
    }
    return replace(result, tier="main", metadata=metadata)


def _source_window_rejection_reason(profile: TopicProfile, result: ArticleFetchResult, cutoff: datetime) -> str:
    source_type = str(result.payload.source_type or "")

    # Check URL date hint first. URL dates are highly specific and indicate the original path publication date.
    # If the URL date hint is older than the cutoff, reject it even if metadata says it was updated recently.
    for value in (result.final_url, result.original_url, result.payload.original_url):
        url_date = _date_from_url(value)
        if url_date is not None and url_date < cutoff:
            return f"URL date hint places it outside the requested source window ({_format_window_cutoff(cutoff)} or newer required)."

    published = _article_published_at(result)
    if published is not None:
        if published < cutoff:
            return f"Published outside the requested source window ({_format_window_cutoff(cutoff)} or newer required)."
        return ""

    # If no published date, check general text date hints (which may be less precise, e.g. body text dates)
    hinted_date = _article_text_or_url_date(result)
    if hinted_date is not None:
        if hinted_date < cutoff:
            return f"Date hints place it outside the requested source window ({_format_window_cutoff(cutoff)} or newer required)."
        return ""

    if source_type in _STRICT_SOURCE_WINDOW_TYPES:
        item_key = _undated_item_key(result)
        if database.has_served_undated_item(profile.topic_id, item_key):
            return "Undated item was already shown once and is hidden from future editions."
        return "Date is missing for this strict source, so it is excluded under the bounded window."
    return ""


def _source_label_for_result(result: ArticleFetchResult) -> str:
    source_type = str(result.payload.source_type or "")
    if source_type == "gmail":
        return "Gmail"
    if source_type == "gmail_link":
        metadata = result.payload.metadata or {}
        if metadata.get("search_query") or metadata.get("search_provider"):
            if metadata.get("search_provider") == "google_news_rss":
                return "Google News"
            return "Web Search"
        return "Gmail"
    if source_type == "podcast_episode":
        return "Podcast"
    if source_type == "youtube_video":
        return "YouTube"
    if source_type == "reddit_post":
        return "Reddit"
    if source_type == "market_snapshot":
        return "Markets"
    if source_type == "foreign_web":
        return "Foreign Media"
    return "Web"


def _is_strict_undated_result(result: ArticleFetchResult) -> bool:
    return (
        str(result.payload.source_type or "") in _STRICT_SOURCE_WINDOW_TYPES
        and _article_published_at(result) is None
        and _article_text_or_url_date(result) is None
    )


def _mark_undated_once(result: ArticleFetchResult) -> ArticleFetchResult:
    metadata = {
        **dict(result.metadata or {}),
        "date_status": "unknown",
        "served_once": True,
        "served_once_note": "Date unknown; shown once.",
        "served_once_key": _undated_item_key(result),
    }
    return replace(result, metadata=metadata)


def _served_undated_items_from_results(article_results: list[ArticleFetchResult]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for result in article_results:
        if result.tier == "dropped":
            continue
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        if metadata.get("served_once") is not True:
            continue
        items.append(
            {
                "item_key": str(metadata.get("served_once_key") or _undated_item_key(result)),
                "title": result.title,
                "source_name": result.payload.source_name,
                "url": _result_identity_url(result),
            }
        )
    return items


def _undated_item_key(result: ArticleFetchResult) -> str:
    identity = "|".join(
        part
        for part in (
            _result_identity_url(result),
            result.canonical_url or "",
            result.title,
            result.payload.source_name,
        )
        if part
    )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()


def _result_identity_url(result: ArticleFetchResult) -> str:
    return str(result.canonical_url or result.final_url or result.original_url or result.payload.original_url or "").strip()


def _article_published_at(result: ArticleFetchResult) -> datetime | None:
    values: list[Any] = [result.payload.published_at]
    for metadata in (result.payload.metadata, result.metadata):
        if not isinstance(metadata, dict):
            continue
        for key in _DATE_METADATA_KEYS:
            values.append(metadata.get(key))
    for value in values:
        parsed = _parse_datetime_hint(value)
        if parsed is not None:
            return parsed
    return None


def _article_text_or_url_date(result: ArticleFetchResult) -> datetime | None:
    for value in (result.final_url, result.original_url, result.payload.original_url):
        parsed = _date_from_url(value)
        if parsed is not None:
            return parsed
    text_sample = " ".join(
        part
        for part in (
            result.title,
            result.excerpt,
            result.editor_summary,
            result.payload.raw_text,
        )
        if part
    )
    return _date_from_text(text_sample[:4000])


def _parse_datetime_hint(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = _parse_datetime_string(text)
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_datetime_string(text: str) -> datetime | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    with suppress(ValueError):
        normalized = cleaned.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    with suppress(Exception):
        parsed = parsedate_to_datetime(cleaned)
        if parsed is not None:
            return parsed
    date_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", cleaned)
    if date_match:
        with suppress(ValueError):
            return datetime(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
                23,
                59,
                59,
                tzinfo=UTC,
            )
    for pattern in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        with suppress(ValueError):
            return datetime.strptime(cleaned, pattern).replace(hour=23, minute=59, second=59, tzinfo=UTC)
    return _date_from_text(cleaned)


def _date_from_url(value: str | None) -> datetime | None:
    text = str(value or "")
    match = _URL_DATE_RE.search(text)
    if not match:
        return None
    year = int(match.group("year"))
    month = int(match.group("month"))
    day = int(match.group("day") or 1)
    hour = 23 if match.group("day") else 0
    minute = 59 if match.group("day") else 0
    second = 59 if match.group("day") else 0
    with suppress(ValueError):
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    return None


def _date_from_text(value: str | None) -> datetime | None:
    text = str(value or "")
    match = _TEXT_DATE_RE.search(text)
    if match:
        month = _MONTHS.get(match.group("month").lower())
        if month is not None:
            with suppress(ValueError):
                return datetime(
                    int(match.group("year")),
                    month,
                    int(match.group("day")),
                    23,
                    59,
                    59,
                    tzinfo=UTC,
                )
    for pattern in (_CJK_DATE_RE, _DOTTED_DATE_RE):
        locale_match = pattern.search(text)
        if locale_match:
            with suppress(ValueError):
                return datetime(
                    int(locale_match.group(1)),
                    int(locale_match.group(2)),
                    int(locale_match.group(3)),
                    23,
                    59,
                    59,
                    tzinfo=UTC,
                )
    # Non-English Latin month names that the English _MONTHS map misses. Relative
    # phrasing is disabled here because this scans free body/snippet text where
    # "posted 2 days ago" chrome must not be read as the publish date.
    shared = normalize_date_string(text, allow_relative=False)
    if shared:
        with suppress(ValueError):
            parsed = datetime.fromisoformat(shared)
            if parsed.tzinfo is None:
                parsed = parsed.replace(hour=23, minute=59, second=59, tzinfo=UTC)
            return parsed
    return None


def _foreign_language_coverage_notes(profile: Any, candidates: Any) -> list[dict[str, Any]]:
    """Surface foreign-media languages that returned nothing this build.

    Reads the persisted language plan and the surviving discovery candidates so
    an empty Foreign Media section is explained in the Reporting tab (per
    language) instead of silently rendering blank.
    """
    selection = getattr(profile, "source_selection", None) or {}
    if not (isinstance(selection, dict) and selection.get("foreign_media")):
        return []
    plan = getattr(profile, "foreign_language_plan", None) or ()
    if not plan:
        return []
    covered: set[str] = set()
    for candidate in candidates or ():
        if getattr(candidate, "adapter", "") != "foreign_media":
            continue
        metadata = getattr(candidate.payload, "metadata", None) or {}
        code = str(metadata.get("source_language") or "").strip().lower()
        if code:
            covered.add(code)
    notes: list[dict[str, Any]] = []
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip().lower()
        if not code or code in covered:
            continue
        name = str(entry.get("name") or code).strip() or code
        notes.append(
            {
                "source_name": "Foreign Media",
                "item": name,
                "reason": f"No {name} results survived discovery for this brief.",
            }
        )
    return notes


def _source_window_issue_name(result: ArticleFetchResult) -> str:
    title = (result.title or result.payload.source_name or result.original_url or "Source").strip()
    return title[:120]


def _format_window_cutoff(cutoff: datetime) -> str:
    return cutoff.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
