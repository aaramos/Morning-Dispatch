from __future__ import annotations

import asyncio
import logging
import json
import time
import hashlib
import urllib.parse
import email.utils
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, UTC, timedelta

import httpx
import feedparser
from bs4 import BeautifulSoup

from backend.app.core.config import get_settings
from backend.app.core.http_pool import shared_async_client

logger = logging.getLogger(__name__)

GOOGLE_NEWS_DECODE_NEGATIVE_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class GoogleNewsHit:
    title: str            # " - Publisher" suffix stripped (rsplit(" - ", 1))
    url: str              # the news.google.com proxy link as-is
    decoded_url: str | None  # publisher URL after unfurl (Phase 2), else None
    snippet: str          # <description> with HTML stripped (BeautifulSoup)
    publisher: str        # <source> text, fallback "Google News"
    published_at: str | None  # RFC-822 <pubDate> → UTC ISO 8601 (timespec="seconds")


@dataclass
class GoogleNewsDecodeState:
    blocked: bool = False
    reason: str | None = None


def build_search_url(
    query: str,
    *,
    lookback_hours: int | None = None,
    hl: str = "en-US",
    gl: str = "US",
    ceid: str = "US:en",
) -> str:
    encoded_query = urllib.parse.quote(query)
    if lookback_hours is not None:
        if lookback_hours <= 48:
            op = f"when:{lookback_hours}h"
        elif lookback_hours <= 720:
            days = max(1, round(lookback_hours / 24))
            op = f"when:{days}d"
        else:
            dt = datetime.now(UTC) - timedelta(hours=lookback_hours)
            op = f"after:{dt.strftime('%Y-%m-%d')}"
        q_param = f"{encoded_query}+{urllib.parse.quote(op)}"
    else:
        q_param = encoded_query
    return f"https://news.google.com/rss/search?q={q_param}&hl={hl}&gl={gl}&ceid={ceid}"


async def fetch_google_news(
    query: str,
    *,
    lookback_hours: int | None = None,
    limit: int = 10,
    hl: str = "en-US",
    gl: str = "US",
    ceid: str = "US:en",
) -> list[GoogleNewsHit]:
    settings = get_settings()
    timeout = getattr(settings, "google_news_request_timeout_seconds", 10.0)
    delay = getattr(settings, "google_news_request_delay_seconds", 3.0)
    url = build_search_url(query, lookback_hours=lookback_hours, hl=hl, gl=gl, ceid=ceid)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    async def make_request(client: httpx.AsyncClient) -> httpx.Response:
        response = await client.get(url, headers=headers, timeout=timeout)
        if response.status_code == 429:
            logger.warning("Google News returned 429 for query %s. Retrying after backoff...", query)
            await asyncio.sleep(delay * 2)
            response = await client.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response
    
    client = shared_async_client(purpose="google_news", timeout=timeout, follow_redirects=True)
    response = await make_request(client)

    feed = feedparser.parse(response.text)
    hits = []
    
    for entry in feed.entries[:limit]:
        title_text = entry.get("title", "")
        clean_title = title_text
        if " - " in title_text:
            clean_title = title_text.rsplit(" - ", 1)[0]
            
        link_text = entry.get("link", "")
        pub_date_text = entry.get("published", "") or entry.get("pubDate", "")
        
        published_at = None
        if pub_date_text:
            try:
                dt = email.utils.parsedate_to_datetime(pub_date_text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                published_at = dt.astimezone(UTC).isoformat(timespec="seconds")
            except Exception:
                pass
                
        summary_text = entry.get("summary", "") or entry.get("description", "")
        snippet = ""
        if summary_text:
            try:
                soup = BeautifulSoup(summary_text, "html.parser")
                snippet = soup.get_text()
            except Exception:
                snippet = summary_text
                
        publisher = "Google News"
        if "source" in entry:
            publisher = entry["source"].get("title", "Google News")
        elif hasattr(entry, "source"):
            publisher = getattr(entry.source, "title", "Google News")
            
        hits.append(GoogleNewsHit(
            title=clean_title,
            url=link_text,
            decoded_url=None,
            snippet=snippet,
            publisher=publisher,
            published_at=published_at,
        ))
        
    return hits


async def fetch_google_news_sequential(
    queries: list[str],
    *,
    lookback_hours: int | None = None,
    limit: int = 10,
    hl: str = "en-US",
    gl: str = "US",
    ceid: str = "US:en",
) -> list[GoogleNewsHit]:
    results = []
    settings = get_settings()
    delay = getattr(settings, "google_news_request_delay_seconds", 3.0)
    
    for i, query in enumerate(queries):
        if i > 0:
            await asyncio.sleep(delay)
        try:
            hits = await fetch_google_news(
                query,
                lookback_hours=lookback_hours,
                limit=limit,
                hl=hl,
                gl=gl,
                ceid=ceid,
            )
            results.extend(hits)
        except Exception as e:
            logger.warning("Error fetching Google News for query %s: %s", query, e)
            if len(queries) == 1:
                raise
    return results


def extract_google_news_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if path_parts:
        return path_parts[-1]
    return url


def _decode_cache_dir() -> Path:
    return get_settings().data_dir / "google-news-decode-cache"


def _decode_negative_cache_dir() -> Path:
    return get_settings().data_dir / "google-news-decode-cache-negative"


def _read_decode_cache(guid: str) -> str | None:
    path = _decode_cache_dir() / f"{hashlib.sha256(guid.encode('utf-8')).hexdigest()}.json"
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached_at = float(payload.get("cached_at") or 0)
            if time.time() - cached_at <= 2592000:  # 30 days TTL
                return payload.get("decoded_url")
    except Exception:
        pass
    return None


def cached_decoded_google_news_url(proxy_url: str) -> str | None:
    return _read_decode_cache(extract_google_news_id(proxy_url))


def cached_google_news_decode_failure(proxy_url: str) -> str | None:
    return _read_decode_negative_cache(extract_google_news_id(proxy_url))


def _write_decode_cache(guid: str, decoded_url: str) -> None:
    path = _decode_cache_dir() / f"{hashlib.sha256(guid.encode('utf-8')).hexdigest()}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"cached_at": time.time(), "guid": guid, "decoded_url": decoded_url}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.info("Could not write Google News decode cache for %s: %s", guid, exc)


def _read_decode_negative_cache(guid: str) -> str | None:
    path = _decode_negative_cache_dir() / f"{hashlib.sha256(guid.encode('utf-8')).hexdigest()}.json"
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached_at = float(payload.get("cached_at") or 0)
            ttl = float(payload.get("ttl_seconds") or GOOGLE_NEWS_DECODE_NEGATIVE_TTL_SECONDS)
            if time.time() - cached_at <= ttl:
                return str(payload.get("reason") or "decode_failed")
    except Exception:
        pass
    return None


def _write_decode_negative_cache(
    guid: str,
    reason: str,
    *,
    ttl_seconds: int = GOOGLE_NEWS_DECODE_NEGATIVE_TTL_SECONDS,
) -> None:
    path = _decode_negative_cache_dir() / f"{hashlib.sha256(guid.encode('utf-8')).hexdigest()}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "cached_at": time.time(),
                    "guid": guid,
                    "reason": reason,
                    "ttl_seconds": ttl_seconds,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.info("Could not write Google News negative decode cache for %s: %s", guid, exc)


async def decode_google_news_url(
    proxy_url: str,
    client: httpx.AsyncClient | None = None,
    state: GoogleNewsDecodeState | None = None,
) -> str | None:
    guid = extract_google_news_id(proxy_url)
    
    cached = _read_decode_cache(guid)
    if cached:
        return cached
    if state is not None and state.blocked:
        return None
    cached_failure = _read_decode_negative_cache(guid)
    if cached_failure:
        return None

    settings = get_settings()
    timeout = getattr(settings, "google_news_request_timeout_seconds", 10.0)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async def _do_decode(async_client: httpx.AsyncClient) -> str | None:
        art_url = f"https://news.google.com/articles/{guid}"
        try:
            _set_google_consent_cookie(async_client)
            resp = await async_client.get(art_url, headers=headers, timeout=timeout)
            if _google_news_decode_blocked(resp):
                _write_decode_negative_cache(guid, "decode_blocked")
                if state is not None:
                    state.blocked = True
                    state.reason = "decode_blocked"
                logger.warning("Google News decode appears blocked for %s", guid)
                return None
            if resp.status_code != 200:
                logger.warning("GET article redirect returned %d for %s", resp.status_code, guid)
                _write_decode_negative_cache(guid, f"http_{resp.status_code}")
                return None
            
            soup = BeautifulSoup(resp.text, "html.parser")
            elem = soup.find(attrs={"data-n-a-sg": True, "data-n-a-ts": True})
            if not elem:
                logger.warning("Could not find data-n-a-sg / data-n-a-ts in page for %s", guid)
                _write_decode_negative_cache(guid, "signature_missing")
                return None
                
            sig = elem.get("data-n-a-sg")
            ts = elem.get("data-n-a-ts")
            
            rpc_id = "Fbv4je"
            params = [
                rpc_id,
                f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{guid}",{ts},"{sig}"]',
                None,
                "generic"
            ]
            
            post_data = {
                "f.req": json.dumps([[params]])
            }
            
            post_url = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
            resp_post = await async_client.post(
                post_url,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", **headers},
                data=post_data,
                timeout=timeout
            )
            
            if resp_post.status_code != 200:
                logger.warning("POST batchexecute returned %d for %s", resp_post.status_code, guid)
                if _google_news_decode_blocked(resp_post):
                    if state is not None:
                        state.blocked = True
                        state.reason = "decode_blocked"
                    _write_decode_negative_cache(guid, "decode_blocked")
                else:
                    _write_decode_negative_cache(guid, f"http_{resp_post.status_code}")
                return None
                
            parts = resp_post.text.split("\n\n")
            if len(parts) < 2:
                _write_decode_negative_cache(guid, "decode_response_malformed")
                return None
            data = json.loads(parts[1])
            
            for item in data:
                if isinstance(item, list):
                    for subitem in item:
                        if isinstance(subitem, list) and len(subitem) >= 3 and subitem[1] == rpc_id:
                            res_str = subitem[2]
                            res_data = json.loads(res_str)
                            if isinstance(res_data, list) and len(res_data) >= 2:
                                return res_data[1]
                                
        except Exception as e:
            logger.warning("Error decoding Google News URL for %s: %s", guid, e)
            _write_decode_negative_cache(guid, "decode_exception")
        return None

    if client is None:
        client = shared_async_client(purpose="google_news", timeout=timeout, follow_redirects=True)
    decoded_url = await _do_decode(client)

    if decoded_url:
        _write_decode_cache(guid, decoded_url)
        return decoded_url

    return None


def decode_google_news_url_sync(proxy_url: str) -> str | None:
    guid = extract_google_news_id(proxy_url)
    
    cached = _read_decode_cache(guid)
    if cached:
        return cached
    cached_failure = _read_decode_negative_cache(guid)
    if cached_failure:
        return None

    settings = get_settings()
    timeout = getattr(settings, "google_news_request_timeout_seconds", 10.0)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        with httpx.Client(follow_redirects=True) as client:
            _set_google_consent_cookie(client)
            art_url = f"https://news.google.com/articles/{guid}"
            resp = client.get(art_url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                _write_decode_negative_cache(guid, "decode_blocked" if _google_news_decode_blocked(resp) else f"http_{resp.status_code}")
                return None
                
            soup = BeautifulSoup(resp.text, "html.parser")
            elem = soup.find(attrs={"data-n-a-sg": True, "data-n-a-ts": True})
            if not elem:
                _write_decode_negative_cache(guid, "signature_missing")
                return None
                
            sig = elem.get("data-n-a-sg")
            ts = elem.get("data-n-a-ts")
            
            rpc_id = "Fbv4je"
            params = [
                rpc_id,
                f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{guid}",{ts},"{sig}"]',
                None,
                "generic"
            ]
            
            post_data = {
                "f.req": json.dumps([[params]])
            }
            
            post_url = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
            resp_post = client.post(
                post_url,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", **headers},
                data=post_data,
                timeout=timeout
            )
            
            if resp_post.status_code != 200:
                _write_decode_negative_cache(guid, "decode_blocked" if _google_news_decode_blocked(resp_post) else f"http_{resp_post.status_code}")
                return None
                
            parts = resp_post.text.split("\n\n")
            if len(parts) < 2:
                _write_decode_negative_cache(guid, "decode_response_malformed")
                return None
            data = json.loads(parts[1])
            
            for item in data:
                if isinstance(item, list):
                    for subitem in item:
                        if isinstance(subitem, list) and len(subitem) >= 3 and subitem[1] == rpc_id:
                            res_str = subitem[2]
                            res_data = json.loads(res_str)
                            if isinstance(res_data, list) and len(res_data) >= 2:
                                decoded_url = res_data[1]
                                _write_decode_cache(guid, decoded_url)
                                return decoded_url
    except Exception as e:
        logger.warning("Error decoding sync Google News URL for %s: %s", guid, e)
        _write_decode_negative_cache(guid, "decode_exception")
    return None


def _set_google_consent_cookie(client: object) -> None:
    try:
        client.cookies.set("CONSENT", "YES+cb", domain=".google.com", path="/")
    except Exception:
        pass


def _google_news_decode_blocked(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    try:
        parsed = urllib.parse.urlparse(str(response.url))
    except Exception:
        return False
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host.endswith("google.com") and "/sorry" in path
