from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any, Protocol

import httpx

from backend.agents.discovery.types import AdapterUnavailable
from backend.agents.librarian.date_text import normalize_date_string
from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)


def _use_news_vertical(vertical: str | None, days: int | None) -> bool:
    """Decide whether a search should target a news index.

    ``organic`` never uses news; ``news`` always does. ``auto`` preserves the
    historical behavior of treating any bounded lookback as news-shaped — the
    foreign-media lane opts into ``organic`` explicitly because its native
    queries are evergreen and absent from news indexes.
    """
    value = str(vertical or "auto").strip().lower()
    if value == "news":
        return True
    if value == "organic":
        return False
    return days is not None


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    score: float = 0.5
    provider: str = "unknown"
    published_at: str | None = None


def _repair_text_encoding(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for encoding in ("latin1", "cp1252"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != text:
            return repaired
    return text


class WebSearchBackend(Protocol):
    name: str

    async def search(
        self,
        query: str,
        limit: int,
        *,
        language: str | None = None,
        days: int | None = None,
        vertical: str = "auto",
    ) -> list[SearchHit]:
        ...


@dataclass(frozen=True)
class TavilyBackend:
    api_key: str
    timeout_seconds: float = 8.0
    name: str = "tavily"
    endpoint: str = "https://api.tavily.com/search"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        language: str | None = None,
        days: int | None = None,
        vertical: str = "auto",
    ) -> list[SearchHit]:
        clean_query = _clean_query(query)
        if not clean_query:
            return []

        payload = {
            "api_key": self.api_key,
            "query": clean_query,
            "max_results": max(1, min(limit, 25)),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        # Tavily only honors `days` under the news topic; for organic searches we
        # omit both and rely on the downstream recency window instead.
        if days is not None and _use_news_vertical(vertical, days):
            payload["days"] = max(1, int(days))
            payload["topic"] = "news"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(self.endpoint, json=payload)
            response.raise_for_status()
            data = response.json()

        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        parsed: list[SearchHit] = []
        for result in results[: max(1, min(limit, 25))]:
            if not isinstance(result, dict):
                continue
            raw_url = str(result.get("url") or result.get("link") or "").strip()
            if not raw_url:
                continue
            parsed_url = _normalize_url(raw_url)
            if not parsed_url:
                continue
            parsed.append(
                SearchHit(
                    provider=self.name,
                    title=_repair_text_encoding(result.get("title") or parsed_url)[:220],
                    url=parsed_url,
                    snippet=_repair_text_encoding(result.get("content") or result.get("snippet") or "").strip()[:600],
                    score=float(result.get("score") if isinstance(result.get("score"), (int, float)) else 0.58),
                    published_at=_clean_hit_date(result.get("published_date")),
                )
            )
        return parsed


@dataclass(frozen=True)
class BraveBackend:
    api_key: str
    timeout_seconds: float = 8.0
    name: str = "brave"
    endpoint: str = "https://api.search.brave.com/res/v1/web/search"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        language: str | None = None,
        days: int | None = None,
        vertical: str = "auto",
    ) -> list[SearchHit]:
        clean_query = _clean_query(query)
        if not clean_query:
            return []

        # Brave's web endpoint has no separate news vertical; the freshness filter
        # applies identically regardless of `vertical`.
        params = {
            "q": clean_query,
            "count": max(1, min(limit, 25)),
            "search_lang": _brave_search_language(language),
        }
        freshness = _brave_freshness(days)
        if freshness:
            params["freshness"] = freshness
        headers = {"X-Subscription-Token": self.api_key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(self.endpoint, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

        results = data.get("web", {}).get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        parsed: list[SearchHit] = []
        for result in results[: max(1, min(limit, 25))]:
            if not isinstance(result, dict):
                continue
            raw_url = str(result.get("url") or "").strip()
            parsed_url = _normalize_url(raw_url)
            if not parsed_url:
                continue
            parsed.append(
                SearchHit(
                    provider=self.name,
                    title=_repair_text_encoding(result.get("title") or parsed_url)[:220],
                    url=parsed_url,
                    snippet=_repair_text_encoding(result.get("description") or result.get("extra_snippets") or "").strip()[:600],
                    score=float(result.get("score") if isinstance(result.get("score"), (int, float)) else 0.58),
                    published_at=_clean_hit_date(result.get("page_age") or result.get("age")),
                )
            )
        return parsed


@dataclass(frozen=True)
class SerpAPIBackend:
    api_key: str
    timeout_seconds: float = 8.0
    name: str = "serpapi"
    endpoint: str = "https://serpapi.com/search.json"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        language: str | None = None,
        days: int | None = None,
        vertical: str = "auto",
    ) -> list[SearchHit]:
        clean_query = _clean_query(query)
        if not clean_query:
            return []

        # SerpAPI's google engine returns organic results with a tbs date filter;
        # `vertical` is accepted for interface parity but does not switch indexes.
        params = {
            "q": clean_query,
            "engine": "google",
            "api_key": self.api_key,
            "num": max(1, min(limit, 25)),
        }
        serp_language = _serpapi_language(language)
        if serp_language:
            params.update(serp_language)
        tbs = _serpapi_tbs(days)
        if tbs:
            params["tbs"] = tbs
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(self.endpoint, params=params)
            response.raise_for_status()
            data = response.json()

        results = data.get("organic_results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        parsed: list[SearchHit] = []
        for result in results[: max(1, min(limit, 25))]:
            if not isinstance(result, dict):
                continue
            raw_url = str(result.get("link") or result.get("url") or "").strip()
            parsed_url = _normalize_url(raw_url)
            if not parsed_url:
                continue
            raw_position = result.get("position")
            if isinstance(raw_position, (int, float)) and raw_position > 0:
                score = min(1.0, max(0.0, 1.0 / float(raw_position)))
            else:
                score = 0.58
            parsed.append(
                SearchHit(
                    provider=self.name,
                    title=_repair_text_encoding(result.get("title") or parsed_url)[:220],
                    url=parsed_url,
                    snippet=_repair_text_encoding(result.get("snippet") or "").strip()[:600],
                    score=score,
                    published_at=_clean_hit_date(result.get("date")),
                )
            )
        return parsed


@dataclass(frozen=True)
class SerperBackend:
    api_key: str
    timeout_seconds: float = 8.0
    name: str = "serper"
    endpoint: str = "https://google.serper.dev/search"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        language: str | None = None,
        days: int | None = None,
        vertical: str = "auto",
    ) -> list[SearchHit]:
        clean_query = _clean_query(query)
        if not clean_query:
            return []

        payload = {
            "q": clean_query,
            "num": max(1, min(limit, 25)),
        }
        if language:
            payload["hl"] = language
        # The organic endpoint honors the same tbs date restrict as news, so we
        # keep recency bounded even when serving evergreen organic results.
        tbs = _serpapi_tbs(days)
        if tbs:
            payload["tbs"] = tbs

        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }
        is_news = _use_news_vertical(vertical, days)
        endpoint = "https://google.serper.dev/news" if is_news else self.endpoint
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        results_key = "news" if is_news else "organic"
        results = data.get(results_key) if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        parsed: list[SearchHit] = []
        for result in results[: max(1, min(limit, 25))]:
            if not isinstance(result, dict):
                continue
            raw_url = str(result.get("link") or result.get("url") or "").strip()
            parsed_url = _normalize_url(raw_url)
            if not parsed_url:
                continue
            raw_position = result.get("position")
            if isinstance(raw_position, (int, float)) and raw_position > 0:
                score = min(1.0, max(0.0, 1.0 / float(raw_position)))
            else:
                score = 0.58
            parsed.append(
                SearchHit(
                    provider=self.name,
                    title=_repair_text_encoding(result.get("title") or parsed_url)[:220],
                    url=parsed_url,
                    snippet=_repair_text_encoding(result.get("snippet") or "").strip()[:600],
                    score=score,
                    published_at=_clean_hit_date(result.get("date")),
                )
            )
        return parsed


def _clean_hit_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    # Providers emit locale ("19 ago 2025") and relative ("10 months ago") dates,
    # especially for foreign-language and organic results. Normalize to ISO when
    # possible so downstream recency filtering can read the date; otherwise keep
    # the raw string so a later body-text scan can still attempt to parse it.
    return normalize_date_string(text) or text


def _normalize_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return ""


def _clean_query(value: str, *, max_length: int = 380) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    if len(cleaned) <= max_length:
        return cleaned
    clipped = cleaned[:max_length].rsplit(" ", 1)[0].strip()
    return clipped or cleaned[:max_length].strip()


def _providers_from_config() -> list[WebSearchBackend]:
    settings = get_settings()
    preferred = str(settings.web_search_provider or "auto").strip().lower()
    providers: list[WebSearchBackend] = []
    seen: set[str] = set()

    def add(provider: WebSearchBackend) -> None:
        if provider.name in seen:
            return
        providers.append(provider)
        seen.add(provider.name)

    if preferred in {"", "auto", "serper", "serper_search", "serper-search"}:
        key = str(settings.web_search_serper_api_key or _read_secret(settings, "serper", "api_key") or "").strip()
        if key:
            add(SerperBackend(api_key=key))
    if preferred in {"", "auto", "tavily", "tavily_search", "tavily-search"}:
        key = str(settings.web_search_tavily_api_key or _read_secret(settings, "tavily", "api_key") or "").strip()
        if key:
            add(TavilyBackend(api_key=key))
    if preferred in {"", "auto", "brave", "brave_search", "brave-search"}:
        key = str(settings.web_search_brave_api_key or _read_secret(settings, "brave", "api_key") or "").strip()
        if key:
            add(BraveBackend(api_key=key))
    if preferred in {"", "auto", "serpapi", "serp_api", "serp-api"}:
        key = str(settings.web_search_serpapi_api_key or _read_secret(settings, "serpapi", "api_key") or "").strip()
        if key:
            add(SerpAPIBackend(api_key=key))
    if preferred not in {"", "auto"}:
        # A named primary provider should still have configured backups. This is
        # especially useful for transient Tavily failures such as HTTP 432.
        if "serper" not in seen:
            key = str(settings.web_search_serper_api_key or _read_secret(settings, "serper", "api_key") or "").strip()
            if key:
                add(SerperBackend(api_key=key))
        if "brave" not in seen:
            key = str(settings.web_search_brave_api_key or _read_secret(settings, "brave", "api_key") or "").strip()
            if key:
                add(BraveBackend(api_key=key))
        if "serpapi" not in seen:
            key = str(settings.web_search_serpapi_api_key or _read_secret(settings, "serpapi", "api_key") or "").strip()
            if key:
                add(SerpAPIBackend(api_key=key))
    if providers:
        return providers
    if preferred not in {"auto", ""}:
        raise AdapterUnavailable(f"Web-search provider '{preferred}' is not configured.")
    raise AdapterUnavailable("Web-search provider is not configured yet.")


def _read_secret(settings: Any, *parts: str) -> str:
    path = settings.secrets_dir
    for part in parts:
        path = path / part
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _brave_freshness(days: int | None) -> str:
    if days is None:
        return ""
    d = int(days)
    if d <= 1:
        return "pd"
    if d <= 7:
        return "pw"
    if d <= 31:
        return "pm"
    if d <= 365:
        return "py"
    return ""


def _serpapi_tbs(days: int | None) -> str:
    if days is None:
        return ""
    d = int(days)
    if d <= 1:
        return "qdr:d"
    if d <= 7:
        return "qdr:w"
    if d <= 31:
        return "qdr:m"
    if d <= 365:
        return "qdr:y"
    return ""


def _brave_search_language(language: str | None) -> str:
    code = str(language or "en").strip().lower()
    return {
        "ko": "ko",
        "ja": "ja",
        "zh": "zh-hans",
        "de": "de",
        "fr": "fr",
        "nl": "nl",
        "es": "es",
    }.get(code, "en")


def _serpapi_language(language: str | None) -> dict[str, str]:
    code = str(language or "").strip().lower()
    if code not in {"ko", "ja", "zh", "de", "fr", "nl", "es"}:
        return {}
    params = {"hl": code}
    if code != "zh":
        params["lr"] = f"lang_{code}"
    return params


def lookback_to_days(lookback_hours: int | None) -> int | None:
    """Convert lookback_hours to a days integer for web search freshness filters.

    Returns None when lookback_hours is None (all_available / no constraint) to omit date
    filtering entirely, or when the window is wider than 365 days.
    """
    if lookback_hours is None:
        return None
    days = max(1, int(lookback_hours) // 24)
    return days if days <= 365 else None


async def search_web(
    query: str,
    *,
    limit: int,
    language: str | None = None,
    days: int | None = None,
    vertical: str = "auto",
) -> list[SearchHit]:
    providers = _providers_from_config()
    errors: list[str] = []
    any_success = False
    for backend in providers:
        try:
            hits = await backend.search(
                query=query, limit=limit, language=language, days=days, vertical=vertical
            )
        except Exception as exc:
            errors.append(f"{backend.name}: {exc}")
            continue
        any_success = True
        logger.info(
            "web search provider %s returned %d hits (vertical=%s, days=%s) for %r",
            backend.name,
            len(hits),
            vertical,
            days,
            query[:80],
        )
        # An empty result is a retriable signal, not an answer — fall through to
        # the next configured provider before giving up on the query.
        if hits:
            return hits
    if any_success:
        return []
    raise AdapterUnavailable("Web search failed across configured providers: " + "; ".join(errors))
