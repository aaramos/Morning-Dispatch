from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.agents.discovery.types import AdapterUnavailable, TopicProfile

logger = logging.getLogger(__name__)

# Per-ticker wall-clock cap for the (blocking) yfinance call.
_MARKET_FETCH_TIMEOUT_SECONDS = 15.0
# Dedicated, bounded thread pool for blocking yfinance calls. Isolating them here
# means a yfinance request that wedges on Yahoo (no library-level HTTP timeout)
# can NEVER exhaust the default asyncio thread pool that the rest of the pipeline
# — notably the post-review report compilation (asyncio.to_thread) — depends on.
# Without this, leaked hung yfinance threads accumulate across builds and
# eventually deadlock the brief between the `review` and `done` stages.
_MARKETS_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="markets-yf")

MARKETS_WINDOW_DAYS = 90
YAHOO_QUOTE_URL = "https://finance.yahoo.com/quote/{ticker}"
_SEC_CIK_MAP: dict[str, str] = {}


@dataclass(frozen=True)
class MarketCompany:
    ticker: str
    company_name: str
    tier: str
    rationale: str
    explicit: bool = False


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
    change_3m_pct: float | None
    price_history: tuple[dict[str, Any], ...]
    analyst_rating: str | None
    sector: str | None
    industry: str | None
    recent_news: tuple[dict[str, Any], ...]
    fetched_at: str
    source_url: str

    # Fundamental multiples
    pe_trailing: float | None = None
    pe_forward: float | None = None
    peg_ratio: float | None = None
    price_to_book: float | None = None
    ev_ebitda: float | None = None
    debt_to_equity: float | None = None
    profit_margin: float | None = None
    operating_margin: float | None = None
    beta: float | None = None
    short_percent_of_float: float | None = None
    target_mean_price: float | None = None
    implied_upside_pct: float | None = None
    next_earnings_date: str | None = None
    explicit_ticker: bool = False

    def summary_text(self) -> str:
        movement = _movement_sentence(self)
        news = "; ".join(str(item.get("title") or "") for item in self.recent_news[:3] if item.get("title"))
        multiples = []
        if self.pe_trailing: multiples.append(f"PE (Trailing): {self.pe_trailing:.1f}")
        if self.pe_forward: multiples.append(f"PE (Forward): {self.pe_forward:.1f}")
        if self.peg_ratio: multiples.append(f"PEG: {self.peg_ratio:.2f}")
        if self.price_to_book: multiples.append(f"PB: {self.price_to_book:.2f}")
        if self.ev_ebitda: multiples.append(f"EV/EBITDA: {self.ev_ebitda:.1f}")
        if self.debt_to_equity: multiples.append(f"Debt/Equity: {self.debt_to_equity:.1f}")
        if self.profit_margin: multiples.append(f"Profit Margin: {self.profit_margin * 100:.1f}%")
        if self.operating_margin: multiples.append(f"Operating Margin: {self.operating_margin * 100:.1f}%")
        if self.beta: multiples.append(f"Beta: {self.beta:.2f}")
        if self.short_percent_of_float: multiples.append(f"Short Float: {self.short_percent_of_float * 100:.1f}%")

        multiples_text = " Multiples: " + ", ".join(multiples) + "." if multiples else ""
        upside = f"Target price: {self.target_mean_price:.2f} ({self.implied_upside_pct:+.1f}% upside)." if self.target_mean_price and self.implied_upside_pct else ""
        earnings = f"Next Earnings: {self.next_earnings_date}." if self.next_earnings_date else ""

        parts = [
            f"Public-market snapshot for {self.company_name} ({self.ticker}).",
            movement,
            multiples_text,
            upside,
            earnings,
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
                explicit=True,
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
                    explicit=False,
                )
                if company.ticker not in {item.ticker for item in selections}:
                    selections.append(company)

    return _trim_by_tier(selections, max_core=max_core, max_related=max_related)


async def fetch_market_snapshots(companies: list[MarketCompany]) -> list[MarketSnapshot]:
    if not markets_available():
        raise AdapterUnavailable("Markets requires the yfinance package.")
    loop = asyncio.get_running_loop()

    async def _one(company: MarketCompany) -> MarketSnapshot | None:
        # Run on the dedicated markets pool (not the default thread pool) with a hard
        # per-ticker deadline. If yfinance wedges, the lane degrades gracefully and the
        # stuck thread is isolated to this pool — it cannot starve the rest of asyncio.
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(_MARKETS_EXECUTOR, _fetch_market_snapshot, company),
                timeout=_MARKET_FETCH_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.info("Market snapshot for %s timed out after %.0fs", company.ticker, _MARKET_FETCH_TIMEOUT_SECONDS)
            return None
        except Exception as exc:
            logger.info("Market snapshot for %s failed: %s", company.ticker, exc)
            return None

    results = await asyncio.gather(*[_one(company) for company in companies], return_exceptions=True)
    return [result for result in results if isinstance(result, MarketSnapshot)]


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

    pe_trailing = _number(info.get("trailingPE"))
    pe_forward = _number(info.get("forwardPE"))
    peg_ratio = _number(info.get("pegRatio"))
    price_to_book = _number(info.get("priceToBook"))
    ev_ebitda = _number(info.get("enterpriseToEbitda"))
    debt_to_equity = _number(info.get("debtToEquity"))
    profit_margin = _number(info.get("profitMargins"))
    operating_margin = _number(info.get("operatingMargins"))
    beta = _number(info.get("beta"))
    short_percent_of_float = _number(info.get("shortPercentOfFloat"))
    target_mean_price = _number(info.get("targetMeanPrice"))

    implied_upside_pct = None
    if target_mean_price is not None and current_price > 0:
        implied_upside_pct = round(((target_mean_price - current_price) / current_price) * 100, 2)

    next_earnings_date = _safe_earnings_date(ticker)

    recent_news = fetch_google_news_rss(company.ticker, company_name)
    if not recent_news:
        recent_news = _recent_news(_safe_list(lambda: ticker.news))

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
        change_3m_pct=_change_since_first(history),
        price_history=tuple(_history_points(history)),
        analyst_rating=str(info.get("recommendationKey") or info.get("recommendationMean") or "").strip() or None,
        sector=str(info.get("sector") or "").strip() or None,
        industry=str(info.get("industry") or "").strip() or None,
        recent_news=tuple(recent_news),
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        source_url=YAHOO_QUOTE_URL.format(ticker=company.ticker),

        pe_trailing=pe_trailing,
        pe_forward=pe_forward,
        peg_ratio=peg_ratio,
        price_to_book=price_to_book,
        ev_ebitda=ev_ebitda,
        debt_to_equity=debt_to_equity,
        profit_margin=profit_margin,
        operating_margin=operating_margin,
        beta=beta,
        short_percent_of_float=short_percent_of_float,
        target_mean_price=target_mean_price,
        implied_upside_pct=implied_upside_pct,
        next_earnings_date=next_earnings_date,
        explicit_ticker=company.explicit,
    )
    return snapshot


def _safe_earnings_date(ticker: Any) -> str | None:
    try:
        cal = ticker.calendar
        if cal is None:
            return None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date") or cal.get("earningsDate")
            if ed:
                if isinstance(ed, list) and len(ed) > 0:
                    return ed[0].strftime("%Y-%m-%d")
                return str(ed)
        import pandas as pd
        if isinstance(cal, pd.DataFrame):
            for idx in cal.index:
                if "earnings date" in str(idx).lower():
                    val = cal.loc[idx].iloc[0]
                    if isinstance(val, list) and len(val) > 0:
                        return val[0].strftime("%Y-%m-%d")
                    if hasattr(val, "strftime"):
                        return val.strftime("%Y-%m-%d")
                    return str(val)
    except Exception:
        pass
    return None


def fetch_google_news_rss(ticker_symbol: str, company_name: str) -> list[dict[str, Any]]:
    sites = ["reuters.com", "cnbc.com", "bloomberg.com", "wsj.com", "ft.com", "marketwatch.com"]
    site_query = " OR ".join(f"site:{site}" for site in sites)
    query = f'({site_query}) AND ("{company_name}" OR "{ticker_symbol}")'
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"

    articles = []
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()
            root = ET.fromstring(xml_data)
            for item in root.findall(".//item")[:10]:
                title = item.find("title")
                link = item.find("link")
                pub_date = item.find("pubDate")
                source = item.find("source")

                title_text = title.text if title is not None else ""
                link_text = link.text if link is not None else ""
                pub_date_text = pub_date.text if pub_date is not None else ""
                source_text = source.text if source is not None else ""

                clean_title = title_text
                if " - " in title_text:
                    clean_title = title_text.rsplit(" - ", 1)[0]

                published_at = None
                if pub_date_text:
                    try:
                        from email.utils import parsedate_to_datetime
                        published_at = parsedate_to_datetime(pub_date_text)
                    except Exception:
                        pass

                articles.append({
                    "title": clean_title,
                    "url": link_text,
                    "published_at": published_at.isoformat(timespec="seconds") if published_at else None,
                    "publisher": source_text or "Google News",
                })
    except Exception:
        pass
    return articles


def _fetch_cik_map() -> dict[str, str]:
    global _SEC_CIK_MAP
    if _SEC_CIK_MAP:
        return _SEC_CIK_MAP
    try:
        url = "https://data.sec.gov/files/company_tickers.json"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Morning Dispatch Admin admin@morning-dispatch.org"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            new_map = {}
            for item in data.values():
                ticker = str(item["ticker"]).upper()
                cik = str(item["cik_str"]).zfill(10)
                new_map[ticker] = cik
            _SEC_CIK_MAP = new_map
    except Exception:
        pass
    return _SEC_CIK_MAP


def fetch_sec_filings(ticker_symbol: str, company_name: str) -> list[dict[str, Any]]:
    ticker = ticker_symbol.upper()
    cik_map = _fetch_cik_map()
    cik = cik_map.get(ticker)
    if not cik:
        return []

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    filings = []
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Morning Dispatch Admin admin@morning-dispatch.org"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            recent = data.get("filings", {}).get("recent", {})
            if not recent:
                return []

            keys = list(recent.keys())
            length = len(recent[keys[0]])
            for i in range(min(length, 40)):
                form = recent["form"][i]
                if form not in {"10-K", "10-Q", "8-K", "4"}:
                    continue

                accession_number = recent["accessionNumber"][i]
                acc_no_dashes = accession_number.replace("-", "")
                primary_doc = recent["primaryDocument"][i]
                filing_date = recent["filingDate"][i]
                description = recent["primaryDocDescription"][i] or f"Form {form} Filing"

                doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/{primary_doc}"

                form_label = {
                    "10-K": "Annual Report (10-K)",
                    "10-Q": "Quarterly Report (10-Q)",
                    "8-K": f"Current Report (8-K) - {description}",
                    "4": "Statement of Changes in Beneficial Ownership (Form 4)",
                }.get(form, f"Form {form}")

                filings.append({
                    "ticker": ticker,
                    "company_name": company_name,
                    "form": form,
                    "form_label": form_label,
                    "filing_date": filing_date,
                    "description": description,
                    "url": doc_url,
                    "accession_number": accession_number,
                })
    except Exception:
        pass
    return filings[:10]


def fetch_fred_series(series_id: str, label: str, api_key: str) -> dict[str, Any] | None:
    url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={api_key}&file_type=json&sort_order=desc&limit=30"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            observations = data.get("observations", [])
            if not observations:
                return None

            history = []
            for obs in observations:
                date_str = obs.get("date")
                val_str = obs.get("value")
                try:
                    val = float(val_str)
                    history.append({"date": date_str, "value": val})
                except (ValueError, TypeError):
                    pass

            if not history:
                return None

            current = history[0]
            latest_val = current["value"]
            prev_val = history[1]["value"] if len(history) > 1 else latest_val
            change_1p = latest_val - prev_val

            return {
                "series_id": series_id,
                "label": label,
                "current_value": latest_val,
                "current_date": current["date"],
                "change_1period": round(change_1p, 4),
                "history": history,
                "url": f"https://fred.stlouisfed.org/series/{series_id}",
            }
    except Exception:
        return None


def fetch_fred_macro_data(api_key: str) -> list[dict[str, Any]]:
    series_definitions = [
        ("T10Y2Y", "10-Year Treasury Constant Maturity Minus 2-Year Treasury Constant Maturity"),
        ("FEDFUNDS", "Effective Federal Funds Rate"),
        ("CPIAUCSL", "Consumer Price Index for All Urban Consumers: All Items in U.S. City Average"),
        ("UNRATE", "Civilian Unemployment Rate"),
    ]
    results = []
    for series_id, label in series_definitions:
        res = fetch_fred_series(series_id, label, api_key)
        if res:
            results.append(res)
    return results


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


def _change_since_first(history: Any) -> float | None:
    closes = _close_values(history)
    if len(closes) < 2 or closes[0] == 0:
        return None
    return round(((closes[-1] - closes[0]) / closes[0]) * 100, 2)


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


def _history_points(history: Any) -> list[dict[str, Any]]:
    closes = _close_values(history)
    if not closes:
        return []
    dates = _history_dates(history)
    start = max(0, len(closes) - MARKETS_WINDOW_DAYS)
    points: list[dict[str, Any]] = []
    for offset, close in enumerate(closes[start:]):
        source_index = start + offset
        point: dict[str, Any] = {"close": round(close, 4)}
        if source_index < len(dates):
            point["date"] = dates[source_index]
        points.append(point)
    return points


def _history_dates(history: Any) -> list[str]:
    if history is None:
        return []
    try:
        raw_dates = history.index.tolist()
    except Exception:
        raw_dates = []
    dates: list[str] = []
    for raw_date in raw_dates:
        try:
            dates.append(raw_date.date().isoformat())
        except AttributeError:
            value = str(raw_date).strip()
            if value:
                dates.append(value[:10])
    return dates


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


# A bare run of capital letters in prose ("WWDC", "RAM", "GB", "UI") is NOT a ticker.
# We only accept a token as a ticker when it carries an unambiguous market signal:
#   - a $CASHTAG ("$NVDA")
#   - an exchange-suffixed symbol ("000660.KS", "285A.T")
# Anything else must be resolved through the curated company->ticker map. This is an
# allowlist, replacing the old denylist that played whack-a-mole with acronyms.
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
_EXCHANGE_SUFFIX_RE = re.compile(r"\b([A-Z]{1,5}|[0-9]{3,6}[A-Z]?)\.([A-Z]{1,4})\b")
# A market-lane query entry the model produced is intended to be a ticker, so we accept
# a bare uppercase token there — but still reject obvious non-tickers and digits.
_BARE_TICKER_RE = re.compile(r"^(?:[A-Z]{1,5}|[0-9]{3,6}[A-Z]?)(?:\.[A-Z]{1,4})?$")


def _company_tickers(text: str, seen: set[str]) -> list[str]:
    out: list[str] = []
    for pattern, ticker in _KNOWN_COMPANY_TICKERS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            normalized = ticker.upper()
            if normalized not in seen:
                out.append(normalized)
                seen.add(normalized)
    return out


def _signalled_tickers(text: str, seen: set[str]) -> list[str]:
    """Cashtags and exchange-suffixed symbols only — safe to scan over free prose."""
    out: list[str] = []
    for match in _CASHTAG_RE.findall(text):
        normalized = match.upper()
        if normalized.isdigit() or normalized in _TICKER_STOPWORDS or normalized in seen:
            continue
        out.append(normalized)
        seen.add(normalized)
    for symbol, suffix in _EXCHANGE_SUFFIX_RE.findall(text):
        normalized = f"{symbol.upper()}.{suffix.upper()}"
        if normalized in seen:
            continue
        out.append(normalized)
        seen.add(normalized)
    return out


def normalize_market_query_tickers(queries: Any, seen: set[str] | None = None) -> list[str]:
    """Validate the model's markets query lane, where each entry should already be a ticker.

    Bare uppercase tokens are accepted here (the lane is explicitly tickers) but obvious
    acronyms/digits are rejected. Company names and cashtags are still resolved.
    """
    seen = seen if seen is not None else set()
    out: list[str] = []
    items: list[str]
    if isinstance(queries, (list, tuple)):
        items = [str(item) for item in queries]
    elif queries:
        items = [str(queries)]
    else:
        items = []
    for raw in items:
        token = raw.strip()
        if not token:
            continue
        # Resolve any embedded company names / signalled symbols first. If the token
        # already resolved that way (e.g. "APPLE" -> AAPL), don't also keep it as a bare
        # token — otherwise the company name leaks in alongside its ticker.
        resolved = _company_tickers(token, seen)
        resolved.extend(_signalled_tickers(token, seen))
        if resolved:
            out.extend(resolved)
            continue
        candidate = token.lstrip("$").upper()
        if (
            _BARE_TICKER_RE.match(candidate)
            and not candidate.isdigit()
            and candidate not in _TICKER_STOPWORDS
            and candidate not in seen
        ):
            out.append(candidate)
            seen.add(candidate)
    return out


def resolve_tickers_from_text(text: str) -> list[str]:
    """Return ticker symbols found in *text* — known companies plus signalled symbols only.

    Used by the refinement layer to populate strategy previews. It deliberately does NOT
    treat bare uppercase prose tokens as tickers, so acronyms like WWDC/RAM/GB/UI never
    leak into the markets plan.
    """
    seen: set[str] = set()
    tickers = _company_tickers(text, seen)
    tickers.extend(_signalled_tickers(text, seen))
    return tickers


def _explicit_tickers(profile: TopicProfile) -> list[str]:
    prose = " ".join(
        [
            profile.statement,
            profile.scope,
            *profile.keywords,
            *profile.subtopics,
        ]
    )
    seen: set[str] = set()
    tickers = _company_tickers(prose, seen)
    tickers.extend(_signalled_tickers(prose, seen))
    # Explicit market lanes: requested_sources refs + the markets source_queries lane are
    # intended to name tickers directly, so accept validated bare tokens there.
    market_refs = [
        str(source.get("ref") or "")
        for source in profile.requested_sources
        if str(source.get("adapter") or "") == "markets"
    ]
    tickers.extend(normalize_market_query_tickers(market_refs, seen))
    tickers.extend(normalize_market_query_tickers(profile.source_queries.get("markets"), seen))
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
    for label, value in (
        ("1d", snapshot.change_1d_pct),
        ("7d", snapshot.change_7d_pct),
        ("30d", snapshot.change_30d_pct),
        ("3mo", snapshot.change_3m_pct),
    ):
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
    "ASP",
    "CAPEX",
    "CEO",
    "CFO",
    "DRAM",
    "GPU",
    "HBM",
    "GB",
    "GUI",
    "I",
    "LLM",
    "MLX",
    "MCP",
    "NAND",
    "OS",
    "RAM",
    "SEC",
    "SK",
    "UI",
    "USA",
    "UX",
    "WWDC",
}
_KNOWN_COMPANY_TICKERS = (
    (r"\bapple\b|\bapple inc\.?\b", "AAPL"),
    (r"\bmicron\b|\bmicron technology\b", "MU"),
    (r"\b(?:sk\s+)?hynix\b", "000660.KS"),
    (r"\bsamsung(?: electronics)?\b", "005930.KS"),
    (r"\bnvidia\b", "NVDA"),
    (r"\bamd\b|\badvanced micro devices\b", "AMD"),
    (r"\bkioxia\b", "285A.T"),
    (r"\bsandisk\b", "SNDK"),
)


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
