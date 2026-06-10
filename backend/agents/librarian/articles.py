from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from backend.agents.digestor.base import NormalizedPayload

logger = logging.getLogger(__name__)

# lxml is ~3-5x faster than the pure-Python parser on large article pages and
# produces equivalent extraction (validated for parity on real pages). Falls back
# to the stdlib parser if lxml is unavailable.
try:  # pragma: no cover - import-time capability probe
    import lxml  # noqa: F401

    _HTML_PARSER = "lxml"
except ImportError:  # pragma: no cover
    _HTML_PARSER = "html.parser"

MAX_ARTICLE_FETCHES = 1000
MIN_ARTICLE_TEXT_CHARS = 450
MIN_CONTEXT_FALLBACK_CHARS = 180
REQUEST_TIMEOUT_SECONDS = 12
USER_AGENT = "MorningDispatch/0.1 (+https://tailnet.local)"


@dataclass(frozen=True)
class ArticleFetchResult:
    payload: NormalizedPayload
    original_url: str
    final_url: str | None
    title: str
    text: str
    excerpt: str
    domain: str | None
    status: str
    error: str | None = None
    canonical_url: str | None = None
    link_score: float = 0.0
    relevance_score: float | None = None
    tier: str = "main"
    section: str = "Fetched Articles"
    editor_summary: str = ""
    keywords: tuple[str, ...] = ()
    content_type: str = "article"
    enrichment_source: str = "raw"
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def fetched(self) -> bool:
        return self.status == "fetched"


async def fetch_articles_for_payloads(
    payloads: Iterable[NormalizedPayload],
    *,
    max_articles: int = MAX_ARTICLE_FETCHES,
    concurrency: int = 10,
    force_refresh: bool = False,
) -> list[ArticleFetchResult]:
    payload_list = list(payloads)
    direct_results = direct_article_results(payload_list)
    selected_payloads = select_article_payloads(payload_list, max_articles=max_articles)
    if not selected_payloads:
        return direct_results

    from backend.app.core.config import get_settings

    cache_ttl = max(0, int(get_settings().article_fetch_cache_ttl_seconds))
    global_semaphore = asyncio.Semaphore(concurrency)
    domain_semaphores: dict[str, asyncio.Semaphore] = {}

    def get_domain_semaphore(url: str) -> asyncio.Semaphore | None:
        domain = _domain(url)
        if not domain:
            return None
        domain = domain.lower()
        if domain not in domain_semaphores:
            domain_semaphores[domain] = asyncio.Semaphore(3)
        return domain_semaphores[domain]

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    ) as client:
        tasks = [
            _fetch_one(
                client,
                global_semaphore,
                payload,
                domain_semaphore=get_domain_semaphore(payload.original_url),
                cache_ttl=cache_ttl,
                force_refresh=force_refresh,
            )
            for payload in selected_payloads
        ]
        results = await asyncio.gather(*tasks)

    deduped: list[ArticleFetchResult] = []
    seen_final_urls: set[str] = set()
    for result in direct_results + list(results):
        key = result.canonical_url or canonicalize_url(result.final_url or result.original_url)
        if result.fetched and key in seen_final_urls:
            continue
        if result.fetched:
            seen_final_urls.add(key)
        deduped.append(result)
    return deduped


def direct_article_results(payloads: Iterable[NormalizedPayload]) -> list[ArticleFetchResult]:
    results: list[ArticleFetchResult] = []
    for payload in payloads:
        if payload.source_type not in {"gmail", "reddit_thread", "reddit_post", "podcast_episode", "youtube_video", "collection_chunk", "market_snapshot", "sec_filing", "fred_series"} or not payload.original_url:
            continue
        canonical_url = canonicalize_url(payload.original_url)
        title = _payload_title(payload) or payload.source_name or "Direct source"
        text = _clean_text(payload.raw_text)
        is_reddit = payload.source_type in ("reddit_thread", "reddit_post")
        is_podcast = payload.source_type == "podcast_episode"
        is_youtube = payload.source_type == "youtube_video"
        is_collection = payload.source_type == "collection_chunk"
        is_market = payload.source_type == "market_snapshot"
        is_sec = payload.source_type == "sec_filing"
        is_fred = payload.source_type == "fred_series"
        is_gmail = payload.source_type == "gmail"
        results.append(
            ArticleFetchResult(
                payload=payload,
                original_url=canonical_url,
                final_url=canonical_url,
                canonical_url=canonical_url,
                title=title,
                text=text,
                excerpt=_truncate(text, 520),
                domain=_domain(canonical_url),
                status="fetched",
                link_score=float(
                    (payload.metadata or {}).get("episode_quality_score")
                    or (payload.metadata or {}).get("thread_quality_score")
                    or (payload.metadata or {}).get("youtube_quality_score")
                    or (payload.metadata or {}).get("collection_quality_score")
                    or (payload.metadata or {}).get("market_quality_score")
                    or (0.80 if is_gmail else 0.85 if is_sec else 0.88 if is_fred else 0.65)
                ),
                section=(
                    "Newsletter Content"
                    if is_gmail
                    else "Podcast Signals"
                    if is_podcast
                    else "YouTube Videos"
                    if is_youtube
                    else "Collections"
                    if is_collection
                    else "Markets"
                    if is_market
                    else "SEC Filings"
                    if is_sec
                    else "Macro Indicators"
                    if is_fred
                    else "Legacy Discussion"
                ),
                content_type=(
                    "newsletter"
                    if is_gmail
                    else "podcast"
                    if is_podcast
                    else "video"
                    if is_youtube
                    else "collection"
                    if is_collection
                    else "market"
                    if is_market
                    else "sec_filing"
                    if is_sec
                    else "fred_series"
                    if is_fred
                    else "reddit_thread"
                ),
                metadata=_direct_result_metadata(payload),
            )
        )
    return results


def select_article_payloads(
    payloads: Iterable[NormalizedPayload],
    *,
    max_articles: int = MAX_ARTICLE_FETCHES,
) -> list[NormalizedPayload]:
    candidates: dict[str, tuple[float, NormalizedPayload]] = {}
    for payload in payloads:
        if payload.source_type not in {"gmail_link", "foreign_web"} or not payload.original_url:
            continue
        canonical_url = canonicalize_url(_resolve_google_news_proxy_url(unwrap_redirect_url(payload.original_url), payload.metadata))
        score = score_link_candidate(canonical_url, _payload_title(payload))
        if score <= 0:
            continue
        metadata = {**(payload.metadata or {}), "canonical_url": canonical_url, "link_quality_score": score}
        cleaned_payload = NormalizedPayload(
            id=payload.id,
            source_type=payload.source_type,
            source_name=payload.source_name,
            raw_text=payload.raw_text,
            original_url=canonical_url,
            published_at=payload.published_at,
            fetched_at=payload.fetched_at,
            metadata=metadata,
        )
        existing = candidates.get(canonical_url)
        if existing is None or score > existing[0]:
            candidates[canonical_url] = (score, cleaned_payload)

    ranked = sorted(candidates.values(), key=lambda item: item[0], reverse=True)
    return [payload for _score, payload in ranked[:max_articles]]


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, value))
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), netloc, path, "", urlencode(query_items, doseq=True), ""))


def unwrap_redirect_url(url: str) -> str:
    current = str(url or "").strip()
    for _attempt in range(3):
        parsed = urlparse(current)
        if parsed.scheme not in {"http", "https"}:
            return current
        query = dict(parse_qsl(parsed.query, keep_blank_values=False))
        next_url = ""
        for key in REDIRECT_QUERY_KEYS:
            raw_value = query.get(key)
            if not raw_value:
                continue
            candidate = unquote(str(raw_value)).strip()
            parsed_candidate = urlparse(candidate)
            if parsed_candidate.scheme in {"http", "https"} and parsed_candidate.netloc:
                next_url = candidate
                break
        if not next_url or next_url == current:
            return current
        current = next_url
    return current


def _resolve_google_news_proxy_url(url: str, metadata: dict[str, object] | None = None) -> str:
    metadata = metadata or {}
    proxy_url = url if _is_google_news_proxy_url(url) else ""
    raw_google_url = metadata.get("google_news_url")
    if not proxy_url and isinstance(raw_google_url, str) and _is_google_news_proxy_url(raw_google_url):
        proxy_url = raw_google_url
    if not proxy_url:
        return url
    try:
        from backend.agents.discovery.google_news import decode_google_news_url_sync

        decoded = decode_google_news_url_sync(proxy_url)
    except Exception as exc:
        logger.info("Google News proxy decode failed for %s: %s", proxy_url, exc)
        return url
    return decoded or url


def _is_google_news_proxy_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.netloc.lower() != "news.google.com":
        return False
    path = parsed.path.lower()
    return "/articles/" in path or "/rss/articles/" in path


def score_link_candidate(url: str, link_text: str = "") -> float:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return 0.0

    domain = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.lower()
    text = _clean_text(link_text).lower()
    combined = f"{domain} {path} {text}"

    if domain in BLOCKED_DOMAINS or any(domain.endswith(suffix) for suffix in BLOCKED_DOMAIN_SUFFIXES):
        return 0.0
    if path.endswith(NON_ARTICLE_EXTENSIONS):
        return 0.0
    if any(token in combined for token in BLOCKED_URL_TOKENS):
        return 0.0
    if any(phrase in text for phrase in BLOCKED_LINK_TEXT):
        return 0.0

    score = 0.28
    if len(text) >= 14 and text not in GENERIC_LINK_TEXT:
        score += 0.22
    if any(marker in path for marker in ARTICLE_PATH_MARKERS):
        score += 0.26
    if re.search(r"/20\d{2}/\d{1,2}/", path) or re.search(r"/20\d{2}-\d{2}-", path):
        score += 0.12
    if domain in TRUSTED_CONTENT_DOMAINS or any(domain.endswith(suffix) for suffix in TRUSTED_CONTENT_SUFFIXES):
        score += 0.12
    if any(word in combined for word in TOPIC_HINTS):
        score += 0.10
    if text in GENERIC_LINK_TEXT or len(text) <= 3:
        score -= 0.16

    if domain in REDIRECT_DOMAINS or any(domain.endswith(suffix) for suffix in REDIRECT_DOMAIN_SUFFIXES):
        score = max(score, 0.55)

    return max(0.0, min(score, 1.0))


async def _fetch_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    payload: NormalizedPayload,
    *,
    domain_semaphore: asyncio.Semaphore | None = None,
    cache_ttl: int = 0,
    force_refresh: bool = False,
) -> ArticleFetchResult:
    original_url = canonicalize_url(_resolve_google_news_proxy_url(unwrap_redirect_url(str(payload.original_url)), payload.metadata))
    link_score = float((payload.metadata or {}).get("link_quality_score") or score_link_candidate(original_url, _payload_title(payload)))
    async with semaphore:
        if domain_semaphore is not None:
            await domain_semaphore.acquire()
        try:
            cached = _read_fetch_cache(original_url, cache_ttl) if cache_ttl > 0 and not force_refresh else None
            if cached is not None:
                final_url, html = cached
            else:
                response = await client.get(original_url)
                content_type = response.headers.get("content-type", "")
                final_url = canonicalize_url(str(response.url))
                if response.status_code >= 400:
                    return _failed(
                        payload,
                        original_url,
                        final_url,
                        _http_failure_status(response.status_code),
                        f"HTTP {response.status_code}",
                        link_score,
                    )
                if "html" not in content_type.lower():
                    return _failed(payload, original_url, final_url, "non_html", content_type, link_score)
                html = response.text
                if cache_ttl > 0:
                    _write_fetch_cache(original_url, final_url, html)

            # Extraction always runs on the (possibly cached) HTML, so extraction
            # and date-parsing improvements are never masked by a cache hit.
            article = extract_article(html, final_url, fallback_title=_payload_title(payload))
            payload = _with_published_at(payload, article)
            if len(article.text) < MIN_ARTICLE_TEXT_CHARS:
                context = _newsletter_context(payload)
                if _substantial_newsletter_context(context):
                    return ArticleFetchResult(
                        payload=payload,
                        original_url=original_url,
                        final_url=final_url,
                        canonical_url=canonicalize_url(final_url),
                        title=_contextual_title(article.title, payload, context),
                        text=context,
                        excerpt=_truncate(context, 520),
                        domain=_domain(final_url),
                        status="fetched",
                        link_score=link_score,
                        enrichment_source="newsletter_context",
                        metadata=_article_result_metadata(article),
                    )
                text = article.text or context
                excerpt = _truncate(context or article.excerpt or article.text, 520)
                return ArticleFetchResult(
                    payload=payload,
                    original_url=original_url,
                    final_url=final_url,
                    canonical_url=canonicalize_url(final_url),
                    title=article.title,
                    text=text,
                    excerpt=excerpt,
                    domain=_domain(final_url),
                    status="no_content",
                    error=f"Readable article text was too short ({len(article.text)} chars)",
                    link_score=link_score,
                    metadata=_article_result_metadata(article),
                )
            return ArticleFetchResult(
                payload=payload,
                original_url=original_url,
                final_url=final_url,
                canonical_url=canonicalize_url(final_url),
                title=article.title,
                text=article.text,
                excerpt=article.excerpt,
                domain=_domain(final_url),
                status="fetched",
                link_score=link_score,
                metadata=_article_result_metadata(article),
            )
        except Exception as exc:
            logger.info("Article fetch failed for %s: %s", original_url, exc)
            return _failed(payload, original_url, None, "fetch_error", str(exc), link_score)
        finally:
            if domain_semaphore is not None:
                domain_semaphore.release()


def _fetch_cache_dir() -> Path:
    from backend.app.core.config import get_settings

    return get_settings().data_dir / "article-fetch-cache"


def _fetch_cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _fetch_cache_dir() / f"{key}.json"


def _read_fetch_cache(url: str, ttl_seconds: int) -> tuple[str, str] | None:
    """Return (final_url, html) for a fresh cache entry, else None."""
    path = _fetch_cache_path(url)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cached_at = float(payload.get("cached_at") or 0)
    if time.time() - cached_at > ttl_seconds:
        return None
    html = payload.get("html")
    final_url = payload.get("final_url") or url
    if not isinstance(html, str) or not html:
        return None
    return final_url, html


def _write_fetch_cache(url: str, final_url: str, html: str) -> None:
    path = _fetch_cache_path(url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"cached_at": time.time(), "final_url": final_url, "html": html}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - disk/permission edge cases
        logger.info("Could not write article fetch cache for %s: %s", url, exc)


@dataclass(frozen=True)
class ExtractedArticle:
    title: str
    text: str
    excerpt: str
    image_url: str = ""
    image_source: str = ""
    published_at: str | None = None


def extract_article(html: str, url: str, *, fallback_title: str = "") -> ExtractedArticle:
    soup = BeautifulSoup(html, _HTML_PARSER)
    image_url, image_source = _extract_image(soup, url)
    # Harvest the publish date before scripts/headers are decomposed — many pages
    # only expose it via <meta>, JSON-LD, or <time> tags, not in visible body text.
    published_at = _extract_published_at(soup)
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "form", "iframe"]):
        tag.decompose()
    for selector in ("nav", "header", "footer", "aside", "[role='navigation']", ".sidebar", ".newsletter"):
        for tag in soup.select(selector):
            tag.decompose()

    title = _extract_title(soup, fallback_title=fallback_title, url=url)
    root = _best_content_root(soup)
    text = _extract_text(root)
    if len(text) < MIN_ARTICLE_TEXT_CHARS and root is not soup.body and soup.body:
        text = _extract_text(soup.body)
    excerpt = _truncate(text, 520)
    return ExtractedArticle(
        title=title,
        text=text,
        excerpt=excerpt,
        image_url=image_url,
        image_source=image_source,
        published_at=published_at,
    )


def _best_content_root(soup: BeautifulSoup):
    candidates = []
    for selector in ("article", "main", "[role='main']", ".post", ".entry-content", ".article-content", ".content"):
        candidates.extend(soup.select(selector))
    if not candidates:
        return soup.body or soup
    return max(candidates, key=lambda tag: len(_extract_text(tag)))


def _extract_title(soup: BeautifulSoup, *, fallback_title: str, url: str) -> str:
    for selector, attr in (
        ("meta[property='og:title']", "content"),
        ("meta[name='twitter:title']", "content"),
        ("meta[name='title']", "content"),
    ):
        tag = soup.select_one(selector)
        value = tag.get(attr) if tag else None
        if value:
            return _clean_text(str(value))
    heading = soup.find("h1")
    if heading:
        text = _clean_text(heading.get_text(" ", strip=True))
        if text:
            return text
    if soup.title and soup.title.string:
        return _clean_text(soup.title.string)
    if fallback_title:
        return fallback_title
    parsed = urlparse(url)
    return parsed.netloc.removeprefix("www.") or "Article"


def _extract_image(soup: BeautifulSoup, url: str) -> tuple[str, str]:
    for selector, attr, source in (
        ("meta[property='og:image']", "content", "og:image"),
        ("meta[property='og:image:url']", "content", "og:image"),
        ("meta[name='twitter:image']", "content", "twitter:image"),
        ("meta[name='twitter:image:src']", "content", "twitter:image"),
    ):
        tag = soup.select_one(selector)
        value = tag.get(attr) if tag else None
        if not value:
            continue
        resolved = urljoin(url, str(value).strip())
        if resolved.startswith(("http://", "https://")):
            return resolved, source
    return "", ""


# Ordered by trust: explicit publish dates first, "modified/updated" only as a
# last resort so a recently-touched old page does not masquerade as fresh.
_DATE_META_SELECTORS = (
    ("meta[property='article:published_time']", "content"),
    ("meta[property='og:article:published_time']", "content"),
    ("meta[name='article:published_time']", "content"),
    ("meta[itemprop='datePublished']", "content"),
    ("meta[name='datePublished']", "content"),
    ("meta[name='parsely-pub-date']", "content"),
    ("meta[name='sailthru.date']", "content"),
    ("meta[name='publish-date']", "content"),
    ("meta[name='publication_date']", "content"),
    ("meta[name='pubdate']", "content"),
    ("meta[name='dc.date.issued']", "content"),
    ("meta[name='dc.date']", "content"),
    ("meta[name='date']", "content"),
    ("meta[name='cxenseparse:recs:publishtime']", "content"),
    ("meta[property='pagefind:date']", "content"),
)
_JSONLD_DATE_KEYS = ("datePublished", "dateCreated", "uploadDate", "dateModified")
# Visible publish-date bylines, in priority order. Many sites expose the date
# only here (not in <meta>/JSON-LD), e.g. <span class="entry-date">May 04, 2026</span>
# or <div class="date">April 23, 2026</div>. The generic ".date" is last because
# it is the least specific.
_DATE_ELEMENT_SELECTORS = (
    "time[datetime]",
    "[itemprop='datePublished']",
    ".entry-date",
    ".published",
    ".post-date",
    ".posted-on",
    ".article-date",
    ".publish-date",
    "time",
    ".date",
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _extract_published_at(soup: BeautifulSoup) -> str | None:
    for selector, attr in _DATE_META_SELECTORS:
        tag = soup.select_one(selector)
        value = str(tag.get(attr)).strip() if tag and tag.get(attr) else ""
        if value:
            return value
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        found = _jsonld_date(script.string or script.get_text() or "")
        if found:
            return found
    for selector in _DATE_ELEMENT_SELECTORS:
        for element in soup.select(selector):
            candidate = str(
                element.get("datetime") or element.get("content") or element.get_text(" ", strip=True) or ""
            )
            normalized = _normalize_date_text(candidate)
            if normalized:
                return normalized
    body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
    visible_date = _normalize_date_text(body_text[:5000])
    if visible_date:
        return visible_date
    return None


def _normalize_date_text(value: str) -> str | None:
    """Pull a recognizable date out of byline text and return it as ISO `YYYY-MM-DD`.

    Returns None when no valid date is present, so callers can keep scanning
    lower-priority elements rather than latch onto non-date text.
    """
    text = str(value or "").strip()
    if not text:
        return None
    iso = re.search(r"\b20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?", text)
    if iso:
        return iso.group(0)
    iso_date = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso_date:
        return _iso_or_none(iso_date.group(1), iso_date.group(2), iso_date.group(3))
    slash = re.search(r"\b(20\d{2})/(\d{1,2})/(\d{1,2})\b", text)
    if slash:
        return _iso_or_none(slash.group(1), slash.group(2), slash.group(3))
    # Locale numeric dates: CJK 年/月/日, Korean 년/월/일, and dotted (e.g. 2026.04.25)
    # commonly used by Korean/Japanese/Chinese outlets that never emit ISO meta tags.
    cjk = re.search(r"(20\d{2})\s*[年년]\s*(\d{1,2})\s*[月월]\s*(\d{1,2})\s*[日일]?", text)
    if cjk:
        return _iso_or_none(cjk.group(1), cjk.group(2), cjk.group(3))
    dotted = re.search(r"\b(20\d{2})\.(\d{1,2})\.(\d{1,2})\.?", text)
    if dotted:
        return _iso_or_none(dotted.group(1), dotted.group(2), dotted.group(3))
    month_first = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})\b", text)
    if month_first and month_first.group(1).lower() in _MONTHS:
        return _iso_or_none(month_first.group(3), _MONTHS[month_first.group(1).lower()], month_first.group(2))
    day_first = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(20\d{2})\b", text)
    if day_first and day_first.group(2).lower() in _MONTHS:
        return _iso_or_none(day_first.group(3), _MONTHS[day_first.group(2).lower()], day_first.group(1))
    return None


def _iso_or_none(year: int | str, month: int | str, day: int | str) -> str | None:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _jsonld_date(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return _find_jsonld_date(data)


def _find_jsonld_date(obj) -> str | None:
    if isinstance(obj, dict):
        for key in _JSONLD_DATE_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = _find_jsonld_date(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_jsonld_date(item)
            if found:
                return found
    return None


def _extract_text(root) -> str:
    chunks: list[str] = []
    for tag in root.find_all(["h1", "h2", "h3", "p", "li", "blockquote"]):
        text = _clean_text(tag.get_text(" ", strip=True))
        if not _useful_chunk(text):
            continue
        if chunks and chunks[-1] == text:
            continue
        chunks.append(text)
    if not chunks:
        text = _clean_text(root.get_text(" ", strip=True))
        return text if _useful_chunk(text) else ""
    return "\n\n".join(chunks)


def _useful_chunk(text: str) -> bool:
    if len(text) < 35:
        return False
    lowered = text.lower()
    boilerplate = (
        "accept cookies",
        "all rights reserved",
        "cookie policy",
        "privacy policy",
        "sign up for our newsletter",
        "subscribe to our newsletter",
        "terms of service",
    )
    return not any(phrase in lowered for phrase in boilerplate)


def _payload_title(payload: NormalizedPayload) -> str:
    metadata = payload.metadata or {}
    return str(
        metadata.get("link_text")
        or metadata.get("title")
        or metadata.get("parent_subject")
        or metadata.get("subject")
        or ""
    )


def _with_published_at(payload: NormalizedPayload, article: ExtractedArticle) -> NormalizedPayload:
    """Prefer a publish date harvested from the article page over provider metadata."""
    if not article.published_at:
        return payload
    return replace(payload, published_at=article.published_at)


def _article_result_metadata(article: ExtractedArticle) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if article.image_url:
        metadata["image_url"] = article.image_url
        metadata["image_source"] = article.image_source
    return metadata


def _direct_result_metadata(payload: NormalizedPayload) -> dict[str, object]:
    metadata = dict(payload.metadata or {})
    if payload.source_type == "youtube_video":
        thumbnail_url = str(metadata.get("thumbnail_url") or "").strip()
        if thumbnail_url:
            metadata.setdefault("image_url", thumbnail_url)
            metadata.setdefault("image_source", "youtube")
    elif payload.source_type == "podcast_episode":
        image_url = str(metadata.get("image_url") or "").strip()
        if image_url:
            metadata.setdefault("image_source", "podcast")
    return metadata


def _failed(
    payload: NormalizedPayload,
    original_url: str,
    final_url: str | None,
    status: str,
    error: str,
    link_score: float = 0.0,
) -> ArticleFetchResult:
    context = _newsletter_context(payload)
    return ArticleFetchResult(
        payload=payload,
        original_url=original_url,
        final_url=final_url,
        canonical_url=canonicalize_url(final_url or original_url),
        title=_payload_title(payload) or _domain(original_url) or "Article",
        text=context,
        excerpt=_truncate(context, 520),
        domain=_domain(final_url or original_url),
        status=status,
        error=error,
        link_score=link_score,
    )


def _http_failure_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "blocked"
    if status_code in {404, 410}:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "site_error"
    return "http_error"


def _newsletter_context(payload: NormalizedPayload) -> str:
    metadata = payload.metadata or {}
    parts = [
        metadata.get("link_text"),
        metadata.get("title"),
        metadata.get("parent_subject"),
        metadata.get("subject"),
        payload.raw_text,
    ]
    text = " ".join(str(part) for part in parts if part)
    text = re.sub(r"https?://[^\s<>)\"']+", " ", text)
    text = _clean_text(text)
    return _truncate(text, 900)


def _substantial_newsletter_context(context: str) -> bool:
    if len(context) < MIN_CONTEXT_FALLBACK_CHARS:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+", context)
    if len(words) < 18:
        return False
    lowered = context.lower()
    low_value = (
        "unsubscribe",
        "manage preferences",
        "privacy policy",
        "sign up",
        "sponsor",
        "terms of service",
        "view in browser",
    )
    return not any(phrase in lowered for phrase in low_value)


def _contextual_title(article_title: str, payload: NormalizedPayload, context: str) -> str:
    payload_title = _payload_title(payload)
    if payload_title and len(payload_title) >= max(16, len(article_title) + 8):
        return payload_title
    first_sentence = re.split(r"(?<=[.!?])\s+", context, maxsplit=1)[0].strip()
    if 20 <= len(first_sentence) <= 140 and len(first_sentence) >= len(article_title) + 8:
        return first_sentence
    return article_title or payload_title or _domain(str(payload.original_url or "")) or "Article"


def _clean_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _truncate(value: str, max_chars: int) -> str:
    value = " ".join(value.split())
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 1].rstrip()}..."


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).netloc.removeprefix("www.") or None


TRACKING_QUERY_KEYS = {
    "_bhlid",
    "amp",
    "ck_subscriber_id",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "msclkid",
    "oly_enc_id",
    "ref",
    "ref_src",
    "s",
    "spm",
}

REDIRECT_QUERY_KEYS = (
    "url",
    "u",
    "target",
    "redirect",
    "redirect_url",
    "destination",
    "dest",
    "link",
)

NON_ARTICLE_EXTENSIONS = (
    ".avif",
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
)

BLOCKED_DOMAINS = {
    "apply.careers.microsoft.com",
    "calendar.google.com",
    "danelfin.com",
    "facebook.com",
    "forms.gle",
    "instagram.com",
    "jobs.ashbyhq.com",
    "linkedin.com",
    "media.beehiiv.com",
    "metacareers.com",
    "passionfroot.me",
    "threads.net",
    "tiktok.com",
    "twitter.com",
    "useomnia.com",
    "x.com",
}

BLOCKED_DOMAIN_SUFFIXES = (
    ".extforms.netsuite.com",
    ".list-manage.com",
    ".typeform.com",
)

BLOCKED_URL_TOKENS = (
    "/account",
    "/advertise",
    "/affiliate",
    "/author/",
    "/authors/",
    "/careers",
    "/crm/externalleadpage",
    "/externalleadpage",
    "/jobs/",
    "/login",
    "/preferences",
    "/privacy",
    "/profile/job_details",
    "/polls/",
    "/referral",
    "/register",
    "/response?",
    "/signin",
    "/signup",
    "/sponsor",
    "/subscribe",
    "/terms",
    "/unsubscribe",
    "email-preferences",
    "manage-preferences",
    "unsubscribe=",
)

BLOCKED_LINK_TEXT = (
    "advertise",
    "apply now",
    "become a sponsor",
    "email preferences",
    "follow on",
    "follow us",
    "community ai workflows",
    "highlights: news, guides & events",
    "join the ai university",
    "manage preferences",
    "option a",
    "option b",
    "privacy policy",
    "share on",
    "sign up",
    "subscribe",
    "terms of service",
    "trending ai tools",
    "unsubscribe",
    "update preferences",
    "view in browser",
    "work with us",
)

GENERIC_LINK_TEXT = {
    "",
    "click here",
    "here",
    "learn more",
    "more",
    "read more",
    "read online",
    "source",
    "view",
    "view online",
}

ARTICLE_PATH_MARKERS = (
    "/article",
    "/blog/",
    "/news/",
    "/p/",
    "/posts/",
    "/story/",
)

TRUSTED_CONTENT_DOMAINS = {
    "anthropic.com",
    "blog.google",
    "businessinsider.com",
    "forbes.com",
    "openai.com",
    "thedeepview.com",
}

TRUSTED_CONTENT_SUFFIXES = (
    ".substack.com",
    ".thedeepview.com",
)

TOPIC_HINTS = (
    "agent",
    "ai",
    "anthropic",
    "claude",
    "codex",
    "compute",
    "gemini",
    "gpt",
    "inference",
    "model",
    "openai",
)


REDIRECT_DOMAINS = {
    "bit.ly",
    "buff.ly",
    "clicks.beehiv.com",
    "clicks.substack.com",
    "dest.link",
    "lnkd.in",
    "link.beehiiv.com",
    "ow.ly",
    "t.co",
    "tinyurl.com",
}

REDIRECT_DOMAIN_SUFFIXES = (".beehiiv.com", ".substack.com")
