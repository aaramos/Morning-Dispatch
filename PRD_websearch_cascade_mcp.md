# PRD: Cascading Free-Tier Web Search Adapter + Shared MCP Service

**Status:** Draft for review
**Date:** 2026-06-23
**Related code:** `backend/agents/discovery/web_search.py`, `mcp-servers/mcp-gmail/server.py`, `scripts/mcp_tavily*.{sh,js}`, `~/.lmstudio/mcp.json`, `~/.omlx/settings.json`

---

## 1. Summary

Build a **quota-aware cascade** over multiple free-tier web-search APIs that drains each provider's **monthly free allotment** before moving to the next, with a **self-hosted SearXNG** instance as an unlimited last resort. Expose the cascade as a **single always-on HTTP MCP service** consumed by both **Morning Dispatch** (digest pipeline) and **oMLX** (local models), backed by **one shared monthly quota ledger** so the two consumers never double-spend a free tier.

## 2. Background & current state

- Dispatch already implements **4 backends** — Serper, Tavily, Brave, SerpAPI — behind the `WebSearchBackend` Protocol in `web_search.py`. `search_web()` already cascades on error/empty results and supports `prefer_provider`.
- oMLX / LM Studio models already web-search **via MCP**: `~/.lmstudio/mcp.json` registers two *separate, single-provider* servers (`tavily`, `brave-search`) built from `scripts/mcp_tavily_secure.js` + `mcp_brave_search.sh`. oMLX also has a native `mcp` block in `~/.omlx/settings.json`.

**Gaps this PRD closes:**
1. No **monthly free-quota accounting** — we exhaust credits/paid tiers unpredictably and have no "drain free, then next" behavior.
2. Providers are **siloed per consumer** (Dispatch has 4 in-process; oMLX has 2 via MCP) with **no shared usage state**.
3. Only a few free tiers are tapped; several generous non-deprecated free APIs are unused.

## 3. Goals / Non-goals

**Goals**
- **G1** — 8 non-deprecated, free-*monthly* sources behind one cascade.
- **G2** — Per-provider monthly quota tracking; drain free tiers in priority order; never exceed a configured free cap.
- **G3** — A single shared quota ledger across Dispatch + oMLX (no double-spend).
- **G4** — Expose the cascade as an **always-on HTTP MCP service** usable by both consumers (and any other MCP client, e.g. Claude Code).
- **G5** — Self-hosted **SearXNG** as the unlimited last-resort tier.
- **G6** — **No change** to the `SearchHit` contract; existing Dispatch consumers keep working unmodified.

**Non-goals**
- Paid-tier / overage billing management — the cap *is* the free tier; the cascade stops at it.
- Deprecated providers (Google Programmable Search / CSE is **excluded** — closed to new users, hard EOL 2027-01-01).
- Rebuilding the discovery `runner`/registry or result ranking/dedup beyond what exists.

## 4. Provider roster (non-deprecated, free monthly)

Cascade tiers, drained top-to-bottom. Quotas are **approximate — verify at signup** and store as configurable caps.

| # | Provider | Free monthly quota | Strength | Key status |
|---|----------|--------------------|----------|------------|
| 1 | **Tavily** | ~1,000 searches/mo | LLM-cleaned content + citations | ✅ have key |
| 2 | **Brave Search** | ~$5/mo credits (~1–2k queries) | Independent index, privacy | ✅ have key |
| 3 | **SerpAPI** | 100 searches/mo | Real Google SERP, multi-engine | ✅ have key |
| 4 | **Linkup** | **4,000 queries/mo** (or €5/mo credit) | LLM-grounded, sourced | ⛔ sign up |
| 5 | **Bright Data SERP API** | **5,000 credits/mo**, no card | Real Google/Bing SERP at volume | ⛔ sign up |
| 6 | **Firecrawl `/search`** | 1,000 credits/mo, no card | Search + scrape-to-markdown | ⛔ sign up |
| 7 | **Jina AI** (DeepSearch/Reader) | 10M free tokens/key + 100 RPM (Reader keyless @20 RPM) | Reader/search, strong extraction | ⛔ sign up |
| 8 | **SearXNG** (self-hosted) | **Unlimited** (local) | Metasearch aggregator, no quota | 🛠 deploy locally |

**Approx. metered budget:** ~12,600 free searches/mo (tiers 1–6) + Jina token bucket + **unlimited** SearXNG.

**Additional fallbacks (not counted in the 8):**
- **Serper** — 2,500 *one-time* credits (already keyed) → keep as a configured fallback, flagged non-recurring.
- **DuckDuckGo** — keyless, rate-limited, ToS-gray → optional final net alongside SearXNG.

**Explicitly excluded:** Google CSE (deprecating), Exa ($10 one-time, not monthly).

## 5. Functional requirements

- **FR1 — Backends.** Add `LinkupBackend`, `BrightDataBackend`, `FirecrawlBackend`, `JinaBackend`, `SearxngBackend`, each implementing the existing `WebSearchBackend.search(query, limit, *, language, days, vertical) -> list[SearchHit]`. Reuse `SearchHit`, `_normalize_url`, `_clean_query`, `_clean_hit_date`, freshness mappers.
- **FR2 — Quota manager.** A monthly ledger keyed `(provider, period="YYYY-MM")` with a configurable `cap`. Atomic increment per *successful* call. **Proactively skip** any provider at/over cap; **reactively** mark exhausted on credit/`402`/`429` errors. Auto-reset at month rollover (new `period` row).
- **FR3 — Cascade.** `search_web()` orders providers by configurable priority, **skips quota-exhausted** ones, drains each before the next, increments on success, and falls through empty/errored providers (current behavior preserved). SearXNG (then optional DDG) is the always-available tail.
- **FR4 — MCP service.** A FastMCP server exposing `web_search(...)` and `web_search_quota_status()`, served over **streamable-HTTP**, run as an always-on launchd service with bearer-token auth and stderr secret-redaction (per `mcp_tavily.sh`).
- **FR5 — Consumers.** Dispatch's `web_search` source adapter calls the cascade **library in-process** (lowest latency for digest runs); oMLX calls the **HTTP MCP** endpoint. Both update the **same SQLite ledger**.
- **FR6 — SearXNG.** Self-host locally; backend posts to its JSON API; treated as `cap = NULL` (unlimited).
- **FR7 — Config/secrets.** Keys at `secrets_dir/<provider>/api_key` (matches `_read_secret`); caps + priority via env; service port/token in secrets.
- **FR8 — Observability.** `web_search_quota_status()` + an admin surface (extend `mcp_status.py` pattern) showing per-provider used/cap/remaining/exhausted for the current period.

## 6. Architecture

```
                       ┌─────────────────────────────┐
 Dispatch digest  ──►  │  cascading_search() library  │  ◄── oMLX models
 (in-process)          │  (web_search.py backends)    │      (HTTP MCP)
                       └──────────────┬──────────────┘            ▲
                                      │                            │
                        atomic R/W    ▼                  always-on │ HTTP service
                       ┌─────────────────────────┐       ┌─────────┴──────────┐
                       │ search_quota.sqlite3 (WAL)│◄─────│ mcp-websearch (FastMCP)│
                       │  SHARED QUOTA AUTHORITY   │      │ streamable-HTTP + token │
                       └─────────────────────────┘       └────────────────────┘
```

- **Quota authority is the SQLite ledger**, not a process — so an in-process Dispatch call and an oMLX MCP call both decrement the same counters safely (WAL mode handles multi-process concurrency). The HTTP service is the **network face** for oMLX/external clients; Dispatch keeps the in-process path for latency.
- **Dedicated DB** (`runtime/data/db/search_quota.sqlite3`) decouples the MCP service from Dispatch's full schema.
- **Topology (chosen):** always-on launchd service `com.morning-dispatch.websearch-mcp`, FastMCP streamable-HTTP on `127.0.0.1:<port>/mcp`, bearer-token auth.

## 7. Data model

```sql
CREATE TABLE search_quota (
  provider     TEXT NOT NULL,
  period       TEXT NOT NULL,          -- 'YYYY-MM'
  used         INTEGER NOT NULL DEFAULT 0,
  cap          INTEGER,                -- NULL = unlimited (SearXNG / DDG)
  exhausted_at TEXT,                   -- ISO ts; set on reactive 402/429/credit error
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (provider, period)
);
```

## 8. MCP interface

- **`web_search`** — params: `query` (req), `limit` (int, default 8, max 25), `days` (int, optional freshness), `language` (ISO code, optional), `vertical` (`auto|news|organic`, default `auto`), `prefer_provider` (optional). Returns `SearchHit[]` = `{title, url, snippet, score, provider, published_at}`.
- **`web_search_quota_status`** — params: `period` (optional, default current). Returns per-provider `{provider, period, used, cap, remaining, exhausted}`.
- **Auth:** bearer token (`secrets_dir/websearch_mcp/token`). **Transport:** streamable-HTTP. **Registration:** oMLX `mcp` block in `~/.omlx/settings.json` and/or `~/.lmstudio/mcp.json` (replacing the standalone `tavily` + `brave-search` entries).

## 9. Cascade & quota algorithm

1. Build provider list (configured order, keyed providers + SearXNG tail).
2. For each provider: if `used >= cap` for current period → **skip**.
3. Call `backend.search(...)`. On success with hits → **increment** `used`, return hits.
4. On empty → fall through (current behavior). On `402/429`/credit error → set `exhausted_at`, fall through.
5. If all metered providers skipped/exhausted → SearXNG (unlimited), then optional DDG.
6. Month rollover ⇒ new `period` row, counters reset to 0.

**Default priority:** quality-first for brief relevance (Tavily → Linkup → Brave → Bright Data → Firecrawl → Jina → SerpAPI → SearXNG), configurable. *(Open question: switch default to drain-largest-pool-first?)*

## 10. Configuration & secrets

- `secrets_dir/<provider>/api_key` for Linkup, Bright Data, Firecrawl, Jina (Tavily/Brave/SerpAPI/Serper already present).
- `MORNING_DISPATCH_WEBSEARCH_<PROVIDER>_CAP` per-provider monthly cap; `MORNING_DISPATCH_WEBSEARCH_ORDER` priority list.
- `secrets_dir/websearch_mcp/token` (service auth); `MORNING_DISPATCH_WEBSEARCH_MCP_PORT`.
- SearXNG base URL via env; backend uses `format=json`.

## 11. Rollout / milestones

- **M1 — Backends + quota lib.** New backends + `search_quota` ledger + cascade upgrade. *Dispatch benefits immediately* via the existing in-process path. Unit-tested.
- **M2 — MCP HTTP service.** Build `mcp-servers/mcp-websearch/server.py` (FastMCP) + `scripts/mcp_websearch.sh` + launchd plist. Health + redaction.
- **M3 — oMLX cutover.** Register `web-search` in oMLX's MCP registry; retire standalone `tavily`/`brave-search` entries.
- **M4 — SearXNG.** Deploy locally (Docker or native), wire `SearxngBackend`, set as unlimited tail.
- **M5 — Observability.** Admin/quota status surface.

## 12. Testing & acceptance criteria

- **Unit:** each backend parser against captured JSON fixtures; quota manager (cap → skip, reactive exhaustion, month reset); cascade ordering & skip logic.
- **Integration:** two callers (simulated Dispatch lib + MCP client) share one ledger; provider exhaustion advances to the next; SearXNG serves when all metered tiers are capped; month rollover resets.
- **Acceptance:** (a) digest run never exceeds any provider's configured monthly cap; (b) `web_search_quota_status` reflects live usage for both consumers; (c) oMLX model can call the unified `web_search` tool and gets cascaded results; (d) existing `web_search.py` consumers unchanged.

## 13. Risks & mitigations

- **Free-tier drift / ToS** — caps configurable; quotas verified at signup; Google-scraping providers (SerpAPI/Bright Data/Serper) kept as *fallbacks*, not the lead.
- **Rate limits** (esp. Jina 100 RPM, SearXNG self-throttle) — respect per-provider concurrency; reuse `shared_async_client`.
- **SearXNG maintenance** — pin a known-good image; restrict to a curated engine set; localhost-only.
- **Single-service availability** — Dispatch's in-process path is independent of the MCP service; SQLite ledger persists across restarts; launchd `KeepAlive`.
- **Secret handling** — keys only in `secrets_dir` (0600), stderr redaction, bearer-token on the HTTP endpoint, localhost bind.

## 14. Open questions

1. Default cascade order: **quality-first** (proposed) vs **drain-largest-pool-first** (maximizes total monthly free volume)?
2. Should Dispatch *also* consume via the HTTP endpoint for uniformity, or keep the in-process path for latency (proposed)?
3. SearXNG deployment: Docker vs native, and which engines to enable?
4. Bind the HTTP service to the Tailscale interface too (if oMLX ever runs off-box), or localhost-only?

## 15. Out of scope / future

Result caching layer; cross-provider dedup/merge; paid-tier overflow once free is exhausted; per-topic provider routing (e.g. academic → specialized index).

---

### Sources (free-tier research, Jun 2026)
- Firecrawl — Best Web Search APIs (2026): https://www.firecrawl.dev/blog/best-web-search-apis
- KDnuggets — 7 Free Web Search APIs for AI Agents: https://www.kdnuggets.com/7-free-web-search-apis-for-ai-agents
- Linkup vs Exa (free tier): https://www.linkup.so/blog/exa-vs-linkup
- Exa pricing: https://exa.ai/pricing
- Jina review (free tokens / rate limits): https://www.linkstartai.com/en/agents/jina
- Google CSE pricing & deprecation: https://blog.expertrec.com/google-custom-search-json-api-simplified/
- Bright Data — best SERP APIs: https://brightdata.com/blog/web-data/best-serp-apis
