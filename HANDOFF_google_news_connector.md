# Handoff: Google News RSS Source Connector

You are implementing a new discovery source adapter in
`/Users/macstudio/Apps/personal_intel` on branch `main`. Nothing for this
feature exists yet — this document is the full spec. Read it end to end before
writing code.

## Why

The pipeline's news coverage currently depends on paid/keyed web-search
providers (Tavily / Brave / SerpAPI, see
`backend/agents/discovery/web_search.py`). Google News exposes a free,
keyless RSS search endpoint (`https://news.google.com/rss/search?q=...`)
with native date operators (`when:24h`, `after:/before:`) and locale
parameters (`hl`, `gl`, `ceid`). We want it as a first-class source adapter:
breaking-news/headline coverage that works with zero credentials, with the
publisher name supplied by the feed itself.

A minimal proof-of-concept already exists in the codebase:
`fetch_google_news_rss()` in `backend/agents/discovery/markets.py` (~line
292) hits this endpoint for ticker news — synchronous urllib, no retries, no
unfurling. Part of this work is generalizing that into a shared module and
having markets reuse it.

This app is a **continuous daily-brief pipeline, not a backfill tool**. Do
NOT build date-chunked pagination loops; a `when:` filter derived from the
brief's lookback window plus Google's ~100-item-per-query cap is sufficient.

## Architecture context (read these files first)

- `backend/agents/discovery/types.py` — `SourceAdapter` protocol,
  `Candidate`, `TopicProfile`, `SourceAdapterContext`, and the adapter-name
  constants you must extend.
- `backend/agents/discovery/adapters.py` — all existing adapters.
  `WebSearchSourceAdapter` is your closest template (query fan-out, dedupe,
  low-yield refinement, scoring). `RedditSourceAdapter` shows the
  httpx + feedparser + semaphore pattern.
- `backend/agents/discovery/web_search.py` — `lookback_to_days()`,
  `_repair_text_encoding()`, `SearchHit` shape.
- `backend/agents/librarian/articles.py` — downstream article fetching.
  Payloads with `source_type="gmail_link"` (legacy name; means "external web
  link") get full-article fetch + extraction via `select_article_payloads`.
- `backend/agents/discovery/query_refiner.py` —
  `refine_queries_for_adapter(...)`, the standard low-yield second pass.

## Phase 1 — Core connector module: `backend/agents/discovery/google_news.py` (NEW)

```python
@dataclass(frozen=True)
class GoogleNewsHit:
    title: str            # " - Publisher" suffix stripped (rsplit(" - ", 1))
    url: str              # the news.google.com proxy link as-is
    decoded_url: str | None  # publisher URL after unfurl (Phase 2), else None
    snippet: str          # <description> with HTML stripped (BeautifulSoup)
    publisher: str        # <source> text, fallback "Google News"
    published_at: str | None  # RFC-822 <pubDate> → UTC ISO 8601 (timespec="seconds")
```

- `build_search_url(query, *, lookback_hours=None, hl="en-US", gl="US",
  ceid="US:en") -> str`
  - URL-encode the query with `urllib.parse.quote`.
  - Lookback mapping: `None` → no operator; ≤ 48h → `when:{h}h`; ≤ 30 days →
    `when:{d}d`; > 30 days → `after:YYYY-MM-DD` (computed from now − lookback,
    UTC). Append the operator inside `q=` separated by `+`.
  - Locale params are **per-call arguments, not a global** — a later iteration
    will reuse this module for `ForeignMediaSourceAdapter` with non-US locales.
- `async fetch_google_news(query, *, lookback_hours, limit, hl, gl, ceid)
  -> list[GoogleNewsHit]`
  - `httpx.AsyncClient` with a browser User-Agent (copy the UA string used in
    `markets.fetch_google_news_rss`), `follow_redirects=True`, explicit
    timeout from settings.
  - Parse with `feedparser` (already a dependency; reddit uses it).
  - Dates: `email.utils.parsedate_to_datetime` on `<pubDate>`, convert to UTC,
    `.isoformat(timespec="seconds")`. Fail open to `None` per item.
  - On HTTP 429: one retry after a backoff (use settings delay × 2). On any
    other failure raise — the adapter layer aggregates errors.
- **Rate limiting is the adapter's job but the primitive lives here**: export
  an async helper that runs a list of queries SEQUENTIALLY with
  `google_news_request_delay_seconds` sleep between requests. Do NOT
  `asyncio.gather` Google News queries — a single IP gets rate-limited fast.
- Refactor `markets.fetch_google_news_rss()` to delegate to this module
  (it can stay sync via `asyncio.run` is NOT acceptable inside the running
  loop — instead make markets call the async function; markets already runs
  adapter code in async context, and the SEC/FRED helpers use
  `asyncio.to_thread`, so follow whatever is least invasive: acceptable
  fallback is keeping a thin sync wrapper in markets that shares only
  `build_search_url` + the item-normalization function).

## Phase 2 — Link unfurling (`decode_google_news_url`) — the risky part

RSS `<link>` values are `https://news.google.com/rss/articles/CBMi...`
encoded proxy URLs. **Since mid-2024 these are NOT plain HTTP redirects** —
a GET returns an HTML/JS page, so `follow_redirects` and the article
fetcher's `final_url` capture will NOT land on the publisher page, and
full-text extraction would silently fail for every item.

- **Validate the decoding technique against live URLs BEFORE building the
  rest on top of it.** The known approach (used by the `googlenewsdecoder`
  PyPI package — read its source for reference; implement ourselves, do not
  add the dependency without checking license/quality): GET the article page
  to extract `signature` + `timestamp` data attributes, then POST to
  `https://news.google.com/_/DotsSplashUi/data/batchexecute` and parse the
  decoded URL from the response.
- Cache decode results on disk keyed by the encoded article ID (mirror the
  fetch-cache pattern in `backend/agents/librarian/articles.py`
  `_read_fetch_cache`/`_write_fetch_cache`).
- Apply decoding only to the top-N candidates that survive adapter scoring
  (N = the adapter's candidate cap), sequentially, with the same politeness
  delay.
- **Fail open**: if decode fails, keep the Google proxy URL and the RSS
  snippet as content. The brief still gets headline + publisher + date; it
  just won't get full article text for that item.
- Gate the whole step behind settings flag `google_news_unfurl_links`
  (default `true`).
- Bonus fix included in scope: markets currently passes proxy URLs straight
  through to brief links; once decoding exists, markets' news items should
  use decoded URLs too.

## Phase 3 — `GoogleNewsSourceAdapter` in `backend/agents/discovery/adapters.py`

Model directly on `WebSearchSourceAdapter`:

- `name = "google_news"`
- `cost_profile = CostProfile(label="medium", timeout_seconds=45.0)` —
  sequential fetching needs more headroom than web_search's 20s.
- `good_for = ("breaking_news", "headlines", "broad_discovery",
  "mainstream_coverage")`
- `query()`:
  1. Build queries with the existing `_web_search_queries(profile,
     _requested_refs(profile, "google_news"), adapter=self.name)`, capped at
     `settings.google_news_max_queries` (default 5).
  2. Fetch sequentially via the Phase-1 helper, passing
     `context.lookback_hours`.
  3. Dedupe by decoded-or-proxy URL **and by normalized title** (casefold,
     strip punctuation/whitespace). Title dedupe matters more here than in
     any other adapter: Google News returns the same story from many outlets.
  4. Low-yield second pass: if `< 3` unique hits, call
     `refine_queries_for_adapter(...)` exactly like web_search does (cap
     refined queries at 3, tag results `metadata["is_refined_query"] = True`).
  5. Unfurl top candidates (Phase 2).
  6. Emit `Candidate`s with `NormalizedPayload(source_type="gmail_link", ...)`
     — reusing this legacy type gives full-article fetch, the strict recency
     window (`_STRICT_SOURCE_WINDOW_TYPES` in explore.py), and brief-section
     mapping for free. `original_url` = decoded URL when available, else
     proxy URL. Metadata:
     `{"link_quality_score": score, "search_query": q, "search_query_rank":
     i+1, "search_provider": "google_news_rss", "publisher": hit.publisher,
     "google_news_url": hit.url}`.
  7. Scoring: no relevance score comes from the feed; use feed rank —
     `score = max(0.55, 0.90 - position * 0.02)` per query, then
     `_web_query_boosted_score`-style boost for earlier queries. Keep within
     the 0–0.98 convention.
  8. If every query errored and zero hits: raise the first error (web_search
     convention). If queries succeeded but zero hits: return `[]`.
- `fetch()` returns `candidate.payload` (standard).

## Phase 4 — Registration plumbing (mechanical; this is the complete list, modeled on how `reddit` is wired)

- `backend/agents/discovery/types.py`: add `"google_news"` to `AdapterName`
  Literal, `VALID_SOURCE_ADAPTERS`, `DEFAULT_SOURCE_SELECTION` (False) and
  `DEFAULT_EXPLORE_SOURCE_SELECTION` (False).
- `backend/agents/discovery/registry.py`: instantiate in
  `default_source_registry()`.
- `backend/app/services/explore.py`:
  - Source-status block (~line 388 area, next to the `"reddit"` entry):
    `{"label": "Google News", "enabled": True, "setup_required": False,
    "reason": None, "mode": "public-rss"}` — keyless, always enabled.
  - Adapter/source-type label mappings near lines 1507, 1602, 1673, 2194:
    because we reuse `source_type="gmail_link"`, most mappings need no
    change, but verify per-source attribution uses
    `metadata["search_provider"]` where web_search does, and that
    "google_news" appears wherever adapters are enumerated for the
    per-source brief sections / funnel reporting.
- `backend/app/services/refinement.py`: `VALID_SOURCE_ADAPTERS` (~line 64),
  and the per-source funnel enumerations (~lines 2480, 2583).
- `backend/app/services/reporting.py`: adapter map (~line 43).
- `backend/app/services/brief_settings.py`: per-source caps dicts (~lines
  35, 47) — start with the same values as `web_search`.
- `backend/app/core/config.py` (+ matching env-override wiring, follow the
  reddit settings block ~lines 74–78 / 359–363):
  - `google_news_max_queries: int = 5` (`MORNING_DISPATCH_GOOGLE_NEWS_MAX_QUERIES`)
  - `google_news_request_delay_seconds: float = 3.0` (`..._REQUEST_DELAY_SECONDS`)
  - `google_news_request_timeout_seconds: float = 10.0` (`..._REQUEST_TIMEOUT_SECONDS`)
  - `google_news_unfurl_links: bool = True` (`..._UNFURL_LINKS`)
  - `google_news_locale: str = "en-US:US"` (`..._LOCALE`) — parsed into
    hl/gl/ceid; per-call override stays possible.
- `config/prompts.yaml`: extend the source-specific repair guidance line
  (~line 396, the one listing web_search/foreign_media/.../reddit) with:
  `google_news should use headline-style keyword phrasing without operators`.
- `frontend/src/App.tsx`:
  - `SourceKey` union (line 4).
  - Source list entry next to reddit (~line 503):
    `{ key: "google_news", label: "Google News", icon: "📰" }`.
  - Both default-selection objects (~lines 526, 536) and both per-source
    percent preset blocks (~lines 551, 567) — copy web_search's values.
  - The enabled-gating map (~line 7519): mirror the reddit line.

## Phase 5 — Tests: `backend/tests/test_google_news_adapter.py` (NEW)

Model on `backend/tests/test_web_search_adapter.py` (fixtures + monkeypatched
HTTP). Cover at minimum:

1. `build_search_url`: keyword encoding, quoted phrases, `when:24h` /
   `when:7d` mapping from lookback_hours, `after:` for >30d, `None` lookback
   omits operators, locale params present.
2. XML parsing from a canned RSS fixture: title suffix strip
   (`"Headline - Reuters"` → `"Headline"` + publisher `"Reuters"`),
   RFC-822 → UTC ISO date, missing pubDate → None, description HTML stripped.
3. Dedupe: same story URL across two queries kept once; near-identical
   titles from different outlets deduped.
4. 429 → one retry → success path; persistent failure raises.
5. Decode: success maps `original_url` to publisher URL; failure falls back
   to proxy URL with snippet (no exception); `google_news_unfurl_links=False`
   skips decoding entirely.
6. Adapter wiring: registry contains `google_news`; candidates carry
   `source_type="gmail_link"` and `search_provider="google_news_rss"`.
7. Low-yield path triggers `refine_queries_for_adapter` (monkeypatch it).

No test may hit the network.

## Validate (must all pass)

```bash
uv run pytest backend/tests/
npm run build
npm run lint        # 0 warnings
```

Plus one manual end-to-end check: create a topic with only Google News
selected, run an exploration, confirm the brief renders items with publisher
attribution and working publisher links (not news.google.com proxy links,
when unfurling succeeds).

## Runtime topology (do not skip)

Port 8000 is a launchd service (`com.morning-dispatch`) serving
`frontend/dist` with NO auto-reload. After changes:

```bash
npm run build
launchctl kickstart -k gui/$(id -u)/com.morning-dispatch
```

Otherwise you are testing a stale build.

## Known risks / decisions already made

- **Decode technique may break**: Google changes the batchexecute internals
  occasionally. That's why decoding is fail-open and behind a settings flag.
  Validate against live URLs first; if it doesn't work, ship Phases 1/3/4/5
  with proxy links + snippets and leave Phase 2 as a follow-up.
- **Overlap with web_search**: heavy URL overlap is expected when both lanes
  are on. Per-adapter dedupe only for v1; cross-adapter dedupe by decoded URL
  is a noted follow-up, enabled by Phase 2.
- **No pagination/backfill**: out of scope by design (daily pipeline).
- **Foreign locales**: out of scope, but keep locale per-call (Phase 1) so
  foreign_media can adopt this module later.
- Do NOT commit untracked artifacts `pipeline_flowchart.html` or `scratch/`,
  and there are unrelated uncommitted changes in the worktree — stage only
  your own files.
