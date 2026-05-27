from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery import adapters, markets
from backend.agents.discovery.adapters import MarketsSourceAdapter
from backend.agents.discovery.markets import MarketCompany, MarketSnapshot, select_market_companies
from backend.agents.discovery.types import SourceAdapterContext, TopicProfile
from backend.agents.librarian.articles import direct_article_results
from backend.agents.librarian.enrichment import enrich_article
from backend.app.main import create_app


def _runtime(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))


def test_select_market_companies_for_ai_infrastructure() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "AI infrastructure winners and risks",
            "scope": "AI data center, GPU, and cloud infrastructure",
        }
    )

    selected = select_market_companies(profile, max_core=3, max_related=2)

    assert [company.ticker for company in selected if company.tier == "core"] == ["NVDA", "MSFT", "GOOGL"]
    assert [company.ticker for company in selected if company.tier == "related"] == ["AMD", "AVGO"]


def test_select_market_companies_accepts_inferred_exchange_tickers() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "Track memory companies",
            "scope": "Micron, SK Hynix, Kioxia and SanDisk",
            "requested_sources": [
                {"adapter": "markets", "ref": "MU"},
                {"adapter": "markets", "ref": "000660.KS"},
                {"adapter": "markets", "ref": "285A.T"},
                {"adapter": "markets", "ref": "SNDK"},
            ],
        }
    )

    selected = select_market_companies(profile, max_core=8, max_related=0)

    assert [company.ticker for company in selected[:4]] == ["MU", "000660.KS", "285A.T", "SNDK"]


def test_fetch_market_snapshot_maps_yfinance_payload(monkeypatch, tmp_path) -> None:
    class FakeTicker:
        info = {
            "shortName": "NVIDIA Corporation",
            "currentPrice": 900.0,
            "marketCap": 2_200_000_000_000,
            "currency": "USD",
            "recommendationKey": "buy",
            "sector": "Technology",
            "industry": "Semiconductors",
        }
        news = [
            {
                "title": "NVIDIA announces new AI platform",
                "link": "https://news.example.com/nvda",
                "providerPublishTime": int(datetime.now(UTC).timestamp()),
                "publisher": "Example News",
            }
        ]

        def history(self, *_args: object, **_kwargs: object) -> dict[str, list[float]]:
            return {"Close": [800.0, 840.0, 870.0, 900.0]}

    class FakeYFinance:
        @staticmethod
        def Ticker(_ticker: str) -> FakeTicker:
            return FakeTicker()

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(markets, "markets_available", lambda: True)
    monkeypatch.setattr(markets, "_yfinance_module", lambda: FakeYFinance)

    snapshots = asyncio.run(
        markets.fetch_market_snapshots(
            [MarketCompany(ticker="NVDA", company_name="NVIDIA", tier="core", rationale="AI GPUs")]
        )
    )

    assert len(snapshots) == 1
    assert snapshots[0].ticker == "NVDA"
    assert snapshots[0].company_name == "NVIDIA Corporation"
    assert snapshots[0].change_30d_pct == 12.5
    assert snapshots[0].recent_news[0]["title"] == "NVIDIA announces new AI platform"


def test_markets_adapter_returns_brief_candidates(monkeypatch, tmp_path) -> None:
    async def fake_fetch_market_snapshots(companies: list[MarketCompany]) -> list[MarketSnapshot]:
        return [
            MarketSnapshot(
                ticker=companies[0].ticker,
                company_name=companies[0].company_name,
                tier=companies[0].tier,
                rationale=companies[0].rationale,
                current_price=900.0,
                market_cap=2_200_000_000_000,
                currency="USD",
                change_1d_pct=1.2,
                change_7d_pct=4.1,
                change_30d_pct=12.5,
                analyst_rating="buy",
                sector="Technology",
                industry="Semiconductors",
                recent_news=({"title": "New AI platform", "url": "https://news.example.com/nvda"},),
                fetched_at="2026-05-24T12:00:00+00:00",
                source_url="https://finance.yahoo.com/quote/NVDA",
            )
        ]

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(adapters, "fetch_market_snapshots", fake_fetch_market_snapshots)

    candidates = asyncio.run(
        MarketsSourceAdapter().query(
            TopicProfile.from_dict(
                {
                    "statement": "AI infrastructure winners",
                    "scope": "GPU and cloud AI infrastructure",
                    "source_selection": {"markets": True},
                }
            ),
            SourceAdapterContext(exploration_id="explore-1", candidate_limit=5),
        )
    )

    assert len(candidates) == 1
    assert candidates[0].adapter == "markets"
    assert candidates[0].payload.source_type == "market_snapshot"
    assert candidates[0].payload.metadata["ticker"] == "NVDA"
    assert "Public-market snapshot for" in candidates[0].payload.raw_text
    assert "Latest price" in candidates[0].payload.raw_text
    assert "core company for this topic" not in candidates[0].payload.raw_text


def test_market_payloads_are_direct_brief_inputs() -> None:
    payload = NormalizedPayload(
        source_type="market_snapshot",
        source_name="NVIDIA (NVDA)",
        raw_text="NVIDIA is a core company. Latest price: 900 USD. Recent movement: 30d +12.5%.",
        original_url="https://finance.yahoo.com/quote/NVDA",
        metadata={"market_quality_score": 0.9, "ticker": "NVDA", "tier": "core"},
    )

    results = direct_article_results([payload])

    assert len(results) == 1
    assert results[0].section == "Markets"
    assert results[0].content_type == "market"
    assert results[0].link_score == 0.9


def test_legacy_market_payload_rationale_does_not_render_as_summary() -> None:
    payload = NormalizedPayload(
        source_type="market_snapshot",
        source_name="Micron Technology, Inc. (MU)",
        raw_text="Micron Technology, Inc. (MU) is a core company for this topic: Explicitly requested or named in the interest.",
        original_url="https://finance.yahoo.com/quote/MU",
        metadata={"market_quality_score": 0.9, "ticker": "MU", "company_name": "Micron Technology, Inc.", "tier": "core"},
    )

    result = direct_article_results([payload])[0]
    enriched = enrich_article(result)

    assert "core company for this topic" not in enriched.editor_summary
    assert "Explicitly requested" not in enriched.editor_summary
    assert enriched.editor_summary.startswith("Public-market snapshot for Micron Technology, Inc. (MU).")
    assert "Use the linked quote page" in enriched.editor_summary


def test_source_status_includes_markets(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        status = client.get("/api/explore/source-status")

    assert status.status_code == 200
    assert status.json()["sources"]["markets"]["label"] == "Markets"
    assert status.json()["sources"]["markets"]["mode"] == "simple"
