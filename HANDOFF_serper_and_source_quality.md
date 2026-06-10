# Handoff: Serper Integration & P0 Source-Quality Improvements

This document serves as a combined technical handoff summarizing the architecture, modifications, and validation results of the last two major implementations completed in the Morning-Dispatch system:
1. **Serper Web Search Provider Integration** (establishing Serper as the new default web search option).
2. **P0 Source-Quality Improvements** (fixing starvation loops, rate limits, recency-drop leaks, and round-robin pool monopolization).

---

## 1. Serper Web Search Provider Integration

### Configuration & Secret Management
* **[config.py](file:///Users/macstudio/Apps/personal_intel/backend/app/core/config.py):**
  * Added `web_search_serper_api_key` to `Settings`.
  * Loads keys from environment variable `MORNING_DISPATCH_SERPER_API_KEY`, secret file `serper/api_key`, or `.env` files with `SERPER_API_KEY` and `SERPER_SEARCH_API_KEY`.
  * Changed the default `web_search_provider` fallback to `"serper"`.
  * Ensured `settings.secrets_dir / "serper"` directory is generated with strict permissions (`0700`) during initialization.

### Search Backend Client
* **[web_search.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/discovery/web_search.py):**
  * Implemented `SerperBackend` implementing the `WebSearchBackend` protocol. It targets `https://google.serper.dev/search` via a POST request passing the `X-API-KEY` header and parameters `q`, `num`, `hl`, and `tbs`.
  * Integrated `SerperBackend` into the auto-selection provider logic (`_providers_from_config()`).

### API Routes & Status Health
* **[routes.py](file:///Users/macstudio/Apps/personal_intel/backend/app/api/routes.py):** Added `"serper"` as a valid literal option for `SourceSetupPayload.provider` and set it as the default.
* **[explore.py](file:///Users/macstudio/Apps/personal_intel/backend/app/services/explore.py):** Added folder routing mapping `"serper"` provider to `"serper"` secrets folder in `save_web_search_credentials()`.
* **[secret_health.py](file:///Users/macstudio/Apps/personal_intel/backend/app/services/secret_health.py):** Integrated `serper_key` check to status health dashboard reports.

### Frontend UI Components
* **[App.tsx](file:///Users/macstudio/Apps/personal_intel/frontend/src/App.tsx):**
  * Expanded `webProvider` type/state to include `"serper"` and default to `"serper"`.
  * Updated connection wizard wizard credentials submit payload to save key with provider `"serper"`.
  * Configured Serper option to display first in the Admin settings dropdown form.

---

## 2. P0 Source-Quality Improvements

### Librarian (Concurrency Limits)
* **[articles.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/librarian/articles.py):**
  * Introduced a domain-keyed `asyncio.Semaphore(3)` registry to limit concurrent fetches to a maximum of 3 per host, preventing rate-limiting/403 blocks under the new global 35-way concurrency limit.

### Date-Aware Web Discovery
* **[web_search.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/discovery/web_search.py):**
  * Tavily: Appends `"topic": "news"` to the request payload for bounded-window searches (Tavily's news vertical returns `published_date`).
  * Serper: Routes bounded-window searches to `https://google.serper.dev/news` instead of the standard search endpoint, ensuring publication dates are always returned.

### Pipeline Exploration & Budgets
* **[explore.py](file:///Users/macstudio/Apps/personal_intel/backend/app/services/explore.py):**
  * Sorted fetch-budget lane queues so that dated-in-window candidates are prioritized first, followed by undated candidates.
  * Implemented round-robin selection of date adjudication indices to prevent single source lanes (like Gmail) from monopolizing the adjudication window.
  * Refined unlimited lookback filter to execute served-once tracking and deduplication for undated strict items.
* **[source_audit.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/source_audit.py):**
  * Replaced top-N truncation with round-robin index selection across discovery lanes, ensuring diversity in the AI audit pool.

### Adapter Upgrades & Supply Limits
* **[adapters.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/discovery/adapters.py):**
  * Raised `reddit_candidate_cap` from `20` to `100` to utilize the expanded lane capacity.
  * Updated Reddit adapter to concurrently retrieve both `/hot/.rss` and `/new/.rss` when lookback windows are active.
  * Raised web search `per_query_limit` from `20` to `25`.
  * Updated web search query refinement trigger from `total hits < 3` to `in_window_count < target_yield` using a local import of `_parse_datetime_hint` to check candidate freshness.

---

## 3. Verification & Validation Summary

### Automated Unit Tests
* **[test_config.py](file:///Users/macstudio/Apps/personal_intel/backend/tests/test_config.py):** Verified Serper is selected by default and successfully loads key from shared env variables.
* **[test_secret_health.py](file:///Users/macstudio/Apps/personal_intel/backend/tests/test_secret_health.py):** Verified `serper_key` permissions and status reporting.
* **[test_web_search_adapter.py](file:///Users/macstudio/Apps/personal_intel/backend/tests/test_web_search_adapter.py):**
  * Added `test_search_web_uses_serper_aliases` to mock Serper search endpoints and verify results parsing.
  * Added `test_web_search_adapter_refines_on_stale_hits` to verify query refinement fires on stale results.
  * Updated `test_web_search_adapter_allows_twenty_refinement_queries` to assert limits match the new `25` cap.
* **[test_reddit_adapter.py](file:///Users/macstudio/Apps/personal_intel/backend/tests/test_reddit_adapter.py):**
  * Added `test_reddit_adapter_fetches_new_rss_when_bounded` to verify concurrent fetching of hot/new rss feeds.

### Test Execution Results
All tests in the backend suite compile and execute successfully:
```bash
uv run pytest
```
* **Result:** `407 passed`

### Frontend Build
React compilation was validated to ensure no regressions:
```bash
npm run build
```
* **Result:** Compiled cleanly.
