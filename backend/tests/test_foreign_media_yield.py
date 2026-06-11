from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from backend.agents.discovery import foreign_media, web_search
from backend.agents.discovery.foreign_media import ForeignMediaSourceAdapter
from backend.agents.discovery.types import AdapterUnavailable, SourceAdapterContext, TopicProfile
from backend.agents.discovery.web_search import SearchHit
from backend.agents.librarian.date_text import normalize_date_string


def _runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_DB_PATH", str(tmp_path / "data" / "db" / "morning_dispatch.sqlite3"))
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(tmp_path / "missing-hermes.env"))


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _capturing_client(captured: dict, payload: dict):
    class _Client:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url, *, json=None, headers=None):  # noqa: A002
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse(payload)

    return _Client


# --- Phase 1: provider routing -------------------------------------------------


def test_serper_uses_organic_endpoint_with_tbs_for_organic_vertical(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        web_search.httpx,
        "AsyncClient",
        _capturing_client(captured, {"organic": [{"title": "T", "link": "https://e.mx/a", "snippet": "s"}]}),
    )
    backend = web_search.SerperBackend(api_key="k")
    hits = asyncio.run(backend.search("tacos cdmx", 10, language="es", days=180, vertical="organic"))

    assert captured["url"] == "https://google.serper.dev/search"
    assert captured["json"]["tbs"] == "qdr:y"
    assert len(hits) == 1
    assert hits[0].url == "https://e.mx/a"


def test_serper_uses_news_endpoint_for_news_vertical(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        web_search.httpx,
        "AsyncClient",
        _capturing_client(captured, {"news": [{"title": "T", "link": "https://e.com/a", "snippet": "s"}]}),
    )
    backend = web_search.SerperBackend(api_key="k")
    asyncio.run(backend.search("breaking", 10, days=1, vertical="news"))

    assert captured["url"] == "https://google.serper.dev/news"


def test_serper_auto_vertical_preserves_news_when_bounded(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        web_search.httpx,
        "AsyncClient",
        _capturing_client(captured, {"news": []}),
    )
    backend = web_search.SerperBackend(api_key="k")
    asyncio.run(backend.search("q", 10, days=30, vertical="auto"))

    # auto preserves the historical main-web-lane behavior: bounded lookback => news.
    assert captured["url"] == "https://google.serper.dev/news"


def test_tavily_omits_news_topic_for_organic_vertical(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        web_search.httpx,
        "AsyncClient",
        _capturing_client(captured, {"results": []}),
    )
    backend = web_search.TavilyBackend(api_key="k")
    asyncio.run(backend.search("q", 5, days=180, vertical="organic"))

    assert "topic" not in captured["json"]
    assert "days" not in captured["json"]


def test_tavily_keeps_news_topic_for_auto_vertical(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        web_search.httpx,
        "AsyncClient",
        _capturing_client(captured, {"results": []}),
    )
    backend = web_search.TavilyBackend(api_key="k")
    asyncio.run(backend.search("q", 5, days=7, vertical="auto"))

    assert captured["json"]["topic"] == "news"
    assert captured["json"]["days"] == 7


# --- Phase 1: empty-result provider fallback -----------------------------------


class _StubBackend:
    def __init__(self, name: str, hits: list[SearchHit] | None, error: Exception | None = None) -> None:
        self.name = name
        self._hits = hits
        self._error = error
        self.calls = 0

    async def search(self, query, limit, *, language=None, days=None, vertical="auto"):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._hits or [])


def test_search_web_falls_through_empty_provider(monkeypatch) -> None:
    empty = _StubBackend("serper", [])
    full = _StubBackend("brave", [SearchHit(title="T", url="https://e.com/a")])
    monkeypatch.setattr(web_search, "_providers_from_config", lambda: [empty, full])

    hits = asyncio.run(web_search.search_web("q", limit=5))

    assert empty.calls == 1
    assert full.calls == 1
    assert [h.url for h in hits] == ["https://e.com/a"]


def test_search_web_returns_empty_when_all_providers_empty(monkeypatch) -> None:
    a = _StubBackend("serper", [])
    b = _StubBackend("brave", [])
    monkeypatch.setattr(web_search, "_providers_from_config", lambda: [a, b])

    hits = asyncio.run(web_search.search_web("q", limit=5))

    assert hits == []
    assert a.calls == 1 and b.calls == 1


def test_search_web_raises_only_when_all_providers_error(monkeypatch) -> None:
    a = _StubBackend("serper", None, error=RuntimeError("boom"))
    b = _StubBackend("brave", None, error=RuntimeError("bang"))
    monkeypatch.setattr(web_search, "_providers_from_config", lambda: [a, b])

    with pytest.raises(AdapterUnavailable):
        asyncio.run(web_search.search_web("q", limit=5))


# --- Phase 2: native-query fan-out ---------------------------------------------


def test_foreign_media_fans_out_source_queries(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    seen_queries: list[str] = []
    counter = {"n": 0}

    async def fake_search_web(query, *, limit, language=None, days=None, vertical="auto"):
        seen_queries.append(query)
        counter["n"] += 1
        # unique URL per query so dedupe keeps them all
        return [SearchHit(title=f"T{counter['n']}", url=f"https://medio.mx/{counter['n']}", snippet="nota local")]

    monkeypatch.setattr(foreign_media, "search_web", fake_search_web)
    profile = TopicProfile.from_dict(
        {
            "statement": "Solo trip to Mexico City",
            "scope": "CDMX travel",
            "source_selection": {"foreign_media": True},
            "source_queries": {"foreign_media": [f"consulta {i}" for i in range(10)]},
            "foreign_language_plan": [
                {"code": "es", "name": "Spanish", "native_query": "guia cdmx", "native_entity_terms": []}
            ],
        }
    )

    candidates = asyncio.run(
        ForeignMediaSourceAdapter().query(profile, SourceAdapterContext(exploration_id="fanout"))
    )

    # native_query + 6 capped source_queries = 7 distinct searches.
    assert len(seen_queries) == 7
    assert "guia cdmx" in seen_queries
    assert all(c.payload.metadata["source_language"] == "es" for c in candidates)
    assert len(candidates) == 7


def test_foreign_media_dedupes_and_caps_per_language(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    async def fake_search_web(query, *, limit, language=None, days=None, vertical="auto"):
        # every query returns the SAME url -> must collapse to one candidate
        return [SearchHit(title="dup", url="https://medio.mx/same", snippet="x")]

    monkeypatch.setattr(foreign_media, "search_web", fake_search_web)
    profile = TopicProfile.from_dict(
        {
            "statement": "x",
            "scope": "x",
            "source_selection": {"foreign_media": True},
            "source_queries": {"foreign_media": [f"q{i}" for i in range(6)]},
            "foreign_language_plan": [
                {"code": "es", "name": "Spanish", "native_query": "n", "native_entity_terms": []}
            ],
        }
    )

    candidates = asyncio.run(
        ForeignMediaSourceAdapter().query(profile, SourceAdapterContext(exploration_id="dedupe"))
    )

    assert len(candidates) == 1


# --- Phase 3: language plan persistence ----------------------------------------


def test_ensure_foreign_language_plan_persists_when_selected(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    profile = {
        "statement": "Track AI policy in Latin America",
        "scope": "regional coverage",
        "source_selection": {"foreign_media": True},
        "foreign_regions": ["latin_america"],
        "foreign_language_plan": [],
    }

    plan = asyncio.run(refinement_ensure_plan(profile))

    codes = [entry.get("code") for entry in plan]
    assert "es" in codes


def test_ensure_foreign_language_plan_noop_when_present(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    existing = [{"code": "es", "name": "Spanish", "native_query": "hola"}]
    profile = {
        "source_selection": {"foreign_media": True},
        "foreign_language_plan": existing,
    }

    plan = asyncio.run(refinement_ensure_plan(profile))

    assert plan == existing


def test_foreign_language_plan_for_profile_returns_stored_plan(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    stored = [{"code": "es", "name": "Spanish", "native_query": "guia cdmx"}]
    profile = TopicProfile.from_dict(
        {
            "statement": "x",
            "scope": "x",
            "source_selection": {"foreign_media": True},
            "foreign_language_plan": stored,
        }
    )

    plan = asyncio.run(foreign_media.foreign_language_plan_for_profile(profile))

    assert [entry["code"] for entry in plan] == ["es"]
    assert plan[0]["native_query"] == "guia cdmx"


def refinement_ensure_plan(profile):
    from backend.app.services.refinement import _ensure_foreign_language_plan

    return _ensure_foreign_language_plan(profile)


# --- Phase 4: locale + relative dates ------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("19 ago 2025", "2025-08-19"),
        ("1 mar 2025", "2025-03-01"),
        ("3 de marzo de 2025", "2025-03-03"),
        ("15 settembre 2025", "2025-09-15"),
        ("2025-08-19T10:00:00Z", "2025-08-19T10:00:00Z"),
    ],
)
def test_normalize_date_string_locale(raw: str, expected: str) -> None:
    assert normalize_date_string(raw) == expected


def test_normalize_date_string_relative() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    from backend.agents.librarian.date_text import parse_relative_date

    # months approximate to 30 days; 10 * 30 = 300 days before 2026-06-10.
    assert parse_relative_date("10 months ago", now=now).isoformat() == "2025-08-14"
    assert parse_relative_date("3 weeks ago", now=now).isoformat() == "2026-05-20"


def test_normalize_date_string_rejects_garbage() -> None:
    assert normalize_date_string("not a date") is None
    assert normalize_date_string("") is None


# --- Phase 5: foreign-language coverage notes ----------------------------------


def test_foreign_language_coverage_notes_flags_empty_language() -> None:
    from backend.app.services.explore import _foreign_language_coverage_notes

    profile = TopicProfile.from_dict(
        {
            "statement": "x",
            "scope": "x",
            "source_selection": {"foreign_media": True},
            "foreign_language_plan": [
                {"code": "es", "name": "Spanish", "native_query": "a"},
                {"code": "pt", "name": "Portuguese", "native_query": "b"},
            ],
        }
    )

    class _Cand:
        def __init__(self, adapter, language):
            self.adapter = adapter
            self.payload = type("P", (), {"metadata": {"source_language": language}})()

    notes = _foreign_language_coverage_notes(profile, [_Cand("foreign_media", "es")])

    assert len(notes) == 1
    assert notes[0]["source_name"] == "Foreign Media"
    assert notes[0]["item"] == "Portuguese"
    assert "Portuguese" in notes[0]["reason"]


def test_foreign_language_coverage_notes_silent_when_all_covered() -> None:
    from backend.app.services.explore import _foreign_language_coverage_notes

    profile = TopicProfile.from_dict(
        {
            "statement": "x",
            "scope": "x",
            "source_selection": {"foreign_media": True},
            "foreign_language_plan": [{"code": "es", "name": "Spanish", "native_query": "a"}],
        }
    )

    class _Cand:
        def __init__(self, adapter, language):
            self.adapter = adapter
            self.payload = type("P", (), {"metadata": {"source_language": language}})()

    notes = _foreign_language_coverage_notes(profile, [_Cand("foreign_media", "es")])

    assert notes == []
