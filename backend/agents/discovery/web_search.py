from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any, Protocol

import httpx

from backend.agents.discovery.types import AdapterUnavailable
from backend.app.core.config import get_settings


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    score: float = 0.5
    provider: str = "unknown"
    published_at: str | None = None


class WebSearchBackend(Protocol):
    name: str

    async def search(self, query: str, limit: int, *, language: str | None = None, days: int | None = None) -> list[SearchHit]:
        ...


@dataclass(frozen=True)
class TavilyBackend:
    api_key: str
    timeout_seconds: float = 8.0
    name: str = "tavily"
    endpoint: str = "https://api.tavily.com/search"

    async def search(self, query: str, limit: int, *, language: str | None = None, days: int | None = None) -> list[SearchHit]:
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
        if days is not None:
            payload["days"] = max(1, int(days))
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
                    title=str(result.get("title") or parsed_url).strip()[:220],
                    url=parsed_url,
                    snippet=str(result.get("content") or result.get("snippet") or "").strip()[:600],
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

    async def search(self, query: str, limit: int, *, language: str | None = None, days: int | None = None) -> list[SearchHit]:
        clean_query = _clean_query(query)
        if not clean_query:
            return []

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
                    title=str(result.get("title") or parsed_url).strip()[:220],
                    url=parsed_url,
                    snippet=str(result.get("description") or result.get("extra_snippets") or "").strip()[:600],
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

    async def search(self, query: str, limit: int, *, language: str | None = None, days: int | None = None) -> list[SearchHit]:
        clean_query = _clean_query(query)
        if not clean_query:
            return []

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
                    title=str(result.get("title") or parsed_url).strip()[:220],
                    url=parsed_url,
                    snippet=str(result.get("snippet") or "").strip()[:600],
                    score=score,
                    published_at=_clean_hit_date(result.get("date")),
                )
            )
        return parsed


def _clean_hit_date(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


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


def _provider_from_config() -> WebSearchBackend:
    settings = get_settings()
    preferred = str(settings.web_search_provider or "auto").strip().lower()
    if preferred in {"", "auto", "tavily", "tavily_search", "tavily-search"}:
        key = str(settings.web_search_tavily_api_key or _read_secret(settings, "tavily", "api_key") or "").strip()
        if key:
            return TavilyBackend(api_key=key)
    if preferred in {"", "auto", "brave", "brave_search", "brave-search"}:
        key = str(settings.web_search_brave_api_key or _read_secret(settings, "brave", "api_key") or "").strip()
        if key:
            return BraveBackend(api_key=key)
    if preferred in {"", "auto", "serpapi", "serp_api", "serp-api"}:
        key = str(settings.web_search_serpapi_api_key or _read_secret(settings, "serpapi", "api_key") or "").strip()
        if key:
            return SerpAPIBackend(api_key=key)
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


async def search_web(query: str, *, limit: int, language: str | None = None, days: int | None = None) -> list[SearchHit]:
    backend = _provider_from_config()
    return await backend.search(query=query, limit=limit, language=language, days=days)
