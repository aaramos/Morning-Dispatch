from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from backend.agents.digestor.base import NormalizedPayload

logger = logging.getLogger(__name__)

MAX_ARTICLE_FETCHES = 120
MIN_ARTICLE_TEXT_CHARS = 450
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

    @property
    def fetched(self) -> bool:
        return self.status == "fetched"


async def fetch_articles_for_payloads(
    payloads: Iterable[NormalizedPayload],
    *,
    max_articles: int = MAX_ARTICLE_FETCHES,
    concurrency: int = 8,
) -> list[ArticleFetchResult]:
    payload_list = list(payloads)
    direct_results = direct_article_results(payload_list)
    selected_payloads = select_article_payloads(payload_list, max_articles=max_articles)
    if not selected_payloads:
        return direct_results

    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    ) as client:
        tasks = [_fetch_one(client, semaphore, payload) for payload in selected_payloads]
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
        if payload.source_type != "reddit_thread" or not payload.original_url:
            continue
        canonical_url = canonicalize_url(payload.original_url)
        title = _payload_title(payload) or payload.source_name or "Reddit thread"
        text = _clean_text(payload.raw_text)
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
                link_score=float((payload.metadata or {}).get("thread_quality_score") or 0.65),
                section="Reddit Threads",
                content_type="reddit_thread",
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
        if payload.source_type != "gmail_link" or not payload.original_url:
            continue
        canonical_url = canonicalize_url(unwrap_redirect_url(payload.original_url))
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

    return max(0.0, min(score, 1.0))


async def _fetch_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    payload: NormalizedPayload,
) -> ArticleFetchResult:
    original_url = canonicalize_url(unwrap_redirect_url(str(payload.original_url)))
    link_score = float((payload.metadata or {}).get("link_quality_score") or score_link_candidate(original_url, _payload_title(payload)))
    async with semaphore:
        try:
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

            article = extract_article(response.text, final_url, fallback_title=_payload_title(payload))
            if len(article.text) < MIN_ARTICLE_TEXT_CHARS:
                context = _newsletter_context(payload)
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
            )
        except Exception as exc:
            logger.info("Article fetch failed for %s: %s", original_url, exc)
            return _failed(payload, original_url, None, "fetch_error", str(exc), link_score)


@dataclass(frozen=True)
class ExtractedArticle:
    title: str
    text: str
    excerpt: str


def extract_article(html: str, url: str, *, fallback_title: str = "") -> ExtractedArticle:
    soup = BeautifulSoup(html, "html.parser")
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
    return ExtractedArticle(title=title, text=text, excerpt=excerpt)


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
    "manage preferences",
    "option a",
    "option b",
    "privacy policy",
    "share on",
    "sign up",
    "subscribe",
    "terms of service",
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
