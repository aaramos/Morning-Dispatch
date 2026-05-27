from __future__ import annotations

import asyncio
import importlib.util
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.agents.discovery.types import AdapterUnavailable, TopicProfile

MARKETS_WINDOW_DAYS = 90
YAHOO_QUOTE_URL = "https://finance.yahoo.com/quote/{ticker}"


@dataclass(frozen=True)
class MarketCompany:
    ticker: str
    company_name: str
    tier: str
    rationale: str


@dataclass(frozen=True)
class MarketSnapshot:
    ticker: str
    company_name: str
    tier: str
    rationale: str
    current_price: float | None
    market_cap: int | None
    currency: str | None
    change_1d_pct: float | None
    change_7d_pct: float | None
    change_30d_pct: float | None
    analyst_rating: str | None
    sector: str | None
    industry: str | None
    recent_news: tuple[dict[str, Any], ...]
    fetched_at: str
    source_url: str

    def summary_text(self) -> str:
        movement = _movement_sentence(self)
        news = "; ".join(str(item.get("title") or "") for item in self.recent_news[:3] if item.get("title"))
        parts = [
            f"Public-market snapshot for {self.company_name} ({self.ticker}).",
            movement,
            f"Market cap: {_format_market_cap(self.market_cap)}." if self.market_cap else "",
            f"Analyst rating: {self.analyst_rating}." if self.analyst_rating else "",
            f"Sector: {self.sector}." if self.sector else "",
            f"Recent news: {news}." if news else "",
        ]
        return " ".join(part for part in parts if part).strip()


def markets_available() -> bool:
    return importlib.util.find_spec("yfinance") is not None


def select_market_companies(
    profile: TopicProfile,
    *,
    max_core: int,
    max_related: int,
) -> list[MarketCompany]:
    text = profile.discovery_text().lower()
    explicit = _explicit_tickers(profile)
    selections: list[MarketCompany] = []
    for ticker in explicit:
        selections.append(
            MarketCompany(
                ticker=ticker,
                company_name=ticker,
                tier="core",
                rationale="Explicitly requested or named in the interest.",
            )
        )

    matched_groups = [
        group
        for group in _MARKET_TOPIC_GROUPS
        if any(term in text for term in group["terms"])
    ]
    if not matched_groups:
        matched_groups = [_DEFAULT_MARKET_GROUP]

    for group in matched_groups:
        for tier, max_count in (("core", max_core), ("related", max_related)):
            current_tier_count = sum(1 for item in selections if item.tier == tier)
            remaining = max(0, max_count - current_tier_count)
            if remaining <= 0:
                continue
            for raw in group[tier][:remaining]:
                company = MarketCompany(
                    ticker=raw["ticker"],
                    company_name=raw["name"],
                    tier=tier,
                    rationale=raw["rationale"],
                )
                if company.ticker not in {item.ticker for item in selections}:
                    selections.append(company)

    return _trim_by_tier(selections, max_core=max_core, max_related=max_related)


async def fetch_market_snapshots(companies: list[MarketCompany]) -> list[MarketSnapshot]:
    if not markets_available():
        raise AdapterUnavailable("Markets requires the yfinance package.")
    tasks = [asyncio.to_thread(_fetch_market_snapshot, company) for company in companies]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    snapshots: list[MarketSnapshot] = []
    for result in results:
        if isinstance(result, MarketSnapshot):
            snapshots.append(result)
    return snapshots


def _fetch_market_snapshot(company: MarketCompany) -> MarketSnapshot | None:
    yf = _yfinance_module()
    ticker = yf.Ticker(company.ticker)
    info = _safe_dict(lambda: ticker.info)
    history = _safe_history(lambda: ticker.history(period="3mo", interval="1d"))
    current_price = _number(
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
        or _last_close(history)
    )
    if current_price is None:
        return None
    company_name = str(info.get("shortName") or info.get("longName") or company.company_name or company.ticker).strip()
    snapshot = MarketSnapshot(
        ticker=company.ticker,
        company_name=company_name,
        tier=company.tier,
        rationale=company.rationale,
        current_price=current_price,
        market_cap=_int_or_none(info.get("marketCap")),
        currency=str(info.get("currency") or "").strip() or None,
        change_1d_pct=_change_pct(history, 1),
        change_7d_pct=_change_pct(history, 7),
        change_30d_pct=_change_pct(history, 30),
        analyst_rating=str(info.get("recommendationKey") or info.get("recommendationMean") or "").strip() or None,
        sector=str(info.get("sector") or "").strip() or None,
        industry=str(info.get("industry") or "").strip() or None,
        recent_news=tuple(_recent_news(_safe_list(lambda: ticker.news))),
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        source_url=YAHOO_QUOTE_URL.format(ticker=company.ticker),
    )
    return snapshot


def _yfinance_module() -> Any:
    import yfinance as yf

    return yf


def _safe_dict(factory: Any) -> dict[str, Any]:
    try:
        value = factory()
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _safe_list(factory: Any) -> list[Any]:
    try:
        value = factory()
    except Exception:
        return []
    return list(value) if isinstance(value, list) else []


def _safe_history(factory: Any) -> Any:
    try:
        return factory()
    except Exception:
        return None


def _last_close(history: Any) -> float | None:
    closes = _close_values(history)
    return closes[-1] if closes else None


def _change_pct(history: Any, days_back: int) -> float | None:
    closes = _close_values(history)
    if len(closes) < 2:
        return None
    current = closes[-1]
    previous = closes[max(0, len(closes) - days_back - 1)]
    if previous == 0:
        return None
    return round(((current - previous) / previous) * 100, 2)


def _close_values(history: Any) -> list[float]:
    if history is None:
        return []
    close_column = None
    try:
        close_column = history["Close"]
    except Exception:
        close_column = None
    values = []
    if close_column is not None:
        try:
            values = close_column.dropna().tolist()
        except Exception:
            try:
                values = list(close_column)
            except Exception:
                values = []
    elif isinstance(history, dict):
        values = list(history.get("Close") or history.get("close") or [])
    return [float(value) for value in values if _number(value) is not None]


def _recent_news(items: list[Any]) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(days=MARKETS_WINDOW_DAYS)
    news: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        link = str(item.get("link") or item.get("url") or "").strip()
        published_at = _published_at(item)
        if not title:
            continue
        if published_at and published_at < cutoff:
            continue
        news.append(
            {
                "title": title,
                "url": link or None,
                "published_at": published_at.isoformat(timespec="seconds") if published_at else None,
                "publisher": str(item.get("publisher") or "").strip() or None,
            }
        )
    return news[:5]


def _published_at(item: dict[str, Any]) -> datetime | None:
    raw = item.get("providerPublishTime") or item.get("pubDate") or item.get("published_at")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _explicit_tickers(profile: TopicProfile) -> list[str]:
    raw = " ".join(
        [
            profile.statement,
            profile.scope,
            *profile.keywords,
            *profile.subtopics,
            *[str(source.get("ref") or "") for source in profile.requested_sources if str(source.get("adapter") or "") == "markets"],
        ]
    )
    tickers: list[str] = []
    seen: set[str] = set()
    for match in re.findall(r"\b(?:[A-Z]{1,5}|[0-9]{3,6}[A-Z]?)(?:\.[A-Z]{1,3})?\b", raw):
        normalized = match.upper()
        if normalized in _TICKER_STOPWORDS or normalized in seen:
            continue
        tickers.append(normalized)
        seen.add(normalized)
    return tickers


def _trim_by_tier(companies: list[MarketCompany], *, max_core: int, max_related: int) -> list[MarketCompany]:
    trimmed: list[MarketCompany] = []
    counts = {"core": 0, "related": 0}
    limits = {"core": max_core, "related": max_related}
    for company in companies:
        tier = "related" if company.tier == "related" else "core"
        if counts[tier] >= limits[tier]:
            continue
        trimmed.append(company)
        counts[tier] += 1
    return trimmed


def _movement_sentence(snapshot: MarketSnapshot) -> str:
    price = f"{snapshot.current_price:.2f}" if snapshot.current_price is not None else "n/a"
    currency = f" {snapshot.currency}" if snapshot.currency else ""
    changes = []
    for label, value in (("1d", snapshot.change_1d_pct), ("7d", snapshot.change_7d_pct), ("30d", snapshot.change_30d_pct)):
        if value is not None:
            changes.append(f"{label}: {value:+.2f}%")
    suffix = "; ".join(changes)
    return f"Latest price: {price}{currency}." + (f" Recent movement: {suffix}." if suffix else "")


def _format_market_cap(value: int | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    return f"${value:,}"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _int_or_none(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


_TICKER_STOPWORDS = {
    "AI",
    "API",
    "CEO",
    "CFO",
    "GPU",
    "LLM",
    "MCP",
    "SEC",
    "SK",
    "USA",
}


_AI_INFRASTRUCTURE_GROUP = {
    "terms": (
        "ai",
        "artificial intelligence",
        "llm",
        "gpu",
        "accelerator",
        "inference",
        "training",
        "data center",
        "datacenter",
        "cloud",
        "agentic",
    ),
    "core": (
        {"ticker": "NVDA", "name": "NVIDIA", "rationale": "Dominant GPU and accelerator supplier for AI training and inference."},
        {"ticker": "MSFT", "name": "Microsoft", "rationale": "Azure AI platform and major commercial AI distribution."},
        {"ticker": "GOOGL", "name": "Alphabet", "rationale": "Gemini, TPU hardware, and Google Cloud AI services."},
        {"ticker": "AMZN", "name": "Amazon", "rationale": "AWS AI services, Bedrock, Trainium, and Inferentia."},
        {"ticker": "META", "name": "Meta Platforms", "rationale": "Large AI infrastructure buyer and open-model publisher."},
    ),
    "related": (
        {"ticker": "AMD", "name": "Advanced Micro Devices", "rationale": "GPU and accelerator competitor in AI infrastructure."},
        {"ticker": "AVGO", "name": "Broadcom", "rationale": "Custom silicon and networking exposure for AI data centers."},
        {"ticker": "TSM", "name": "Taiwan Semiconductor", "rationale": "Foundry manufacturer for advanced AI chips."},
        {"ticker": "MU", "name": "Micron Technology", "rationale": "High-bandwidth memory supplier for AI accelerators."},
        {"ticker": "ANET", "name": "Arista Networks", "rationale": "Data-center networking supplier for AI clusters."},
    ),
}

_SEMICONDUCTOR_GROUP = {
    "terms": ("semiconductor", "chip", "chips", "foundry", "wafer", "memory", "hbm"),
    "core": (
        {"ticker": "NVDA", "name": "NVIDIA", "rationale": "Leading AI and graphics semiconductor designer."},
        {"ticker": "AMD", "name": "Advanced Micro Devices", "rationale": "CPU and accelerator designer."},
        {"ticker": "TSM", "name": "Taiwan Semiconductor", "rationale": "Leading advanced-node foundry."},
        {"ticker": "AVGO", "name": "Broadcom", "rationale": "Semiconductor and networking supplier."},
        {"ticker": "MU", "name": "Micron Technology", "rationale": "Memory and HBM exposure."},
    ),
    "related": (
        {"ticker": "AMAT", "name": "Applied Materials", "rationale": "Semiconductor equipment supplier."},
        {"ticker": "ASML", "name": "ASML", "rationale": "EUV lithography supplier."},
        {"ticker": "LRCX", "name": "Lam Research", "rationale": "Wafer fabrication equipment supplier."},
        {"ticker": "KLAC", "name": "KLA", "rationale": "Process control and inspection equipment."},
        {"ticker": "INTC", "name": "Intel", "rationale": "Integrated chipmaker and foundry operator."},
    ),
}

_EV_GROUP = {
    "terms": ("ev", "electric vehicle", "battery", "autonomous vehicle", "charging"),
    "core": (
        {"ticker": "TSLA", "name": "Tesla", "rationale": "Largest pure-play EV and autonomy company."},
        {"ticker": "GM", "name": "General Motors", "rationale": "Major automaker investing in EV platforms."},
        {"ticker": "F", "name": "Ford", "rationale": "Major automaker with EV and commercial vehicle exposure."},
        {"ticker": "RIVN", "name": "Rivian", "rationale": "EV truck and delivery-vehicle maker."},
        {"ticker": "LI", "name": "Li Auto", "rationale": "Large Chinese EV maker."},
    ),
    "related": (
        {"ticker": "ALB", "name": "Albemarle", "rationale": "Lithium supplier for EV batteries."},
        {"ticker": "ON", "name": "ON Semiconductor", "rationale": "Power semiconductors for vehicles."},
        {"ticker": "NXPI", "name": "NXP Semiconductors", "rationale": "Automotive semiconductor exposure."},
        {"ticker": "CHPT", "name": "ChargePoint", "rationale": "EV charging network exposure."},
        {"ticker": "APTV", "name": "Aptiv", "rationale": "Vehicle electronics and autonomy supplier."},
    ),
}

_DEFAULT_MARKET_GROUP = {
    "terms": (),
    "core": (
        {"ticker": "MSFT", "name": "Microsoft", "rationale": "Large-cap technology bellwether relevant to many business themes."},
        {"ticker": "NVDA", "name": "NVIDIA", "rationale": "Major AI and semiconductor bellwether."},
        {"ticker": "GOOGL", "name": "Alphabet", "rationale": "Large-cap platform and cloud company."},
        {"ticker": "AMZN", "name": "Amazon", "rationale": "Large-cap cloud, commerce, and infrastructure company."},
        {"ticker": "AAPL", "name": "Apple", "rationale": "Large-cap consumer technology bellwether."},
    ),
    "related": (
        {"ticker": "META", "name": "Meta Platforms", "rationale": "Large-cap platform and AI infrastructure buyer."},
        {"ticker": "AVGO", "name": "Broadcom", "rationale": "Semiconductor and infrastructure software exposure."},
        {"ticker": "TSM", "name": "Taiwan Semiconductor", "rationale": "Advanced chip manufacturing exposure."},
        {"ticker": "ORCL", "name": "Oracle", "rationale": "Enterprise software and cloud infrastructure exposure."},
        {"ticker": "CRM", "name": "Salesforce", "rationale": "Enterprise software and automation exposure."},
    ),
}

_MARKET_TOPIC_GROUPS = (
    _AI_INFRASTRUCTURE_GROUP,
    _SEMICONDUCTOR_GROUP,
    _EV_GROUP,
)
