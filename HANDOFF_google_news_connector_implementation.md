# Handoff: Google News RSS Source Connector Implementation

This document serves as a complete handoff summary for the newly implemented keyless **Google News RSS Source Connector** (`google_news`) in the Morning Dispatch system.

---

## 1. Architecture & Component Summary

We have fully implemented and verified all four phases of the specification. Below is the file-by-file breakdown of changes:

### A. Core Connector Module
*   **[google_news.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/discovery/google_news.py) (NEW):**
    *   **Data Structure:** `GoogleNewsHit` stores news items. Publisher name suffixes are stripped (e.g. `"Headline - Reuters"` → `"Headline"` and `"Reuters"`), HTML formatting tags are removed using `BeautifulSoup` parsing, and `pubDate` strings are normalized to ISO-8601 UTC.
    *   **Search URL Builder:** `build_search_url` formats search requests to `https://news.google.com/rss/search`. It correctly maps lookback windows to native Google News operators (`lookback_hours <= 48` → `when:48h`; `lookback_hours <= 720` → `when:Xd`; `lookback_hours > 720` → `after:YYYY-MM-DD`).
    *   **Sequencing & Retries:** `fetch_google_news_sequential` runs queries sequentially with polite delays to prevent IP bans. If a `429 Too Many Requests` is returned, it retries once with double the configured politeness backoff delay.
    *   **Batch Execute Decoders:** `decode_google_news_url` (async) and `decode_google_news_url_sync` (sync) GET the initial article redirect page, parse the validation parameters (`data-n-a-sg` / `data-n-a-ts`), and make a POST request to Google's internal `/dots/SplashUi/data/batchexecute` RPC service to resolve the target URL.
    *   **Caching:** Resolved URLs are disk-cached for 30 days under `data/google-news-decode-cache` using a SHA-256 hash of the article ID.

### B. Discovery Adapters & Services
*   **[adapters.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/discovery/adapters.py) (MODIFIED):**
    *   Implemented `GoogleNewsSourceAdapter` inheriting `CostProfile("medium", timeout_seconds=45.0)`.
    *   It queries sequential news feeds, performs case/punctuation-agnostic title and URL deduplication, triggers LLM query refinement if query yield is `< 3` unique hits, unfurls candidates using the `batchexecute` decoder, and emits candidates as compatible `"gmail_link"` payloads.
*   **[markets.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/discovery/markets.py) (MODIFIED):**
    *   Refactored `fetch_google_news_rss` to utilize `google_news.build_search_url` for URL construction and resolve proxy redirects using `google_news.decode_google_news_url_sync`.

### C. Backend Configuration & Plumbing
*   **[types.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/discovery/types.py):** Added `"google_news"` to union type literals and default selectors (defaulting to False).
*   **[registry.py](file:///Users/macstudio/Apps/personal_intel/backend/agents/discovery/registry.py):** Registered `GoogleNewsSourceAdapter` in the default `SourceRegistry`.
*   **[explore.py](file:///Users/macstudio/Apps/personal_intel/backend/app/services/explore.py):** Added active-by-default status blocks (`"enabled": True, "setup_required": False, "mode": "public-rss"`), and wired incoming payload metadata mapping.
*   **[refinement.py](file:///Users/macstudio/Apps/personal_intel/backend/app/services/refinement.py) & [reporting.py](file:///Users/macstudio/Apps/personal_intel/backend/app/services/reporting.py):** Added `"google_news"` to valid source filters and display mappings.
*   **[brief_settings.py](file:///Users/macstudio/Apps/personal_intel/backend/app/services/brief_settings.py):** Configured standard cap rates (750 lane cap, 80 source limit) mirroring web search.
*   **[config.py](file:///Users/macstudio/Apps/personal_intel/backend/app/core/config.py):** Exposed environment variables:
    *   `MORNING_DISPATCH_GOOGLE_NEWS_MAX_QUERIES` (default: `5`)
    *   `MORNING_DISPATCH_GOOGLE_NEWS_REQUEST_DELAY_SECONDS` (default: `3.0`)
    *   `MORNING_DISPATCH_GOOGLE_NEWS_REQUEST_TIMEOUT_SECONDS` (default: `10.0`)
    *   `MORNING_DISPATCH_GOOGLE_NEWS_UNFURL_LINKS` (default: `True`)
    *   `MORNING_DISPATCH_GOOGLE_NEWS_LOCALE` (default: `"en-US:US"`)
*   **[prompts.yaml](file:///Users/macstudio/Apps/personal_intel/config/prompts.yaml):** Updated strategy refinement, LLM query broadening schemas, and instruction notes to support target generation for the `"google_news"` adapter.

### D. Frontend Interface
*   **[App.tsx](file:///Users/macstudio/Apps/personal_intel/frontend/src/App.tsx) (MODIFIED):**
    *   Added `google_news` to React types (`SourceKey`).
    *   Registered "Google News" with icon `📰` in selection dropdown list menus.
    *   Wired presets, defaults, enabled-states mapping, and display label formatting.

---

## 2. Automated Test Suite

We created a brand new test module and updated existing tests to ensure full test coverage:
*   **[test_google_news_adapter.py](file:///Users/macstudio/Apps/personal_intel/backend/tests/test_google_news_adapter.py) (NEW):**
    *   `test_build_search_url_*` verifies lookback window parameter mappings and locale options.
    *   `test_fetch_google_news_*` mocks RSS responses to check title-stripping, HTML-stripping, pubDate timezone conversion, and 429 retry loops.
    *   `test_decode_google_news_url_*` verifies `batchexecute` parsing, cache writes/hits, and fallback behavior (fails open to proxy URLs).
    *   `test_google_news_adapter_query` tests the entire adapter pipeline (deduplication, limits, candidate payloads).
*   **[test_explore_discovery.py](file:///Users/macstudio/Apps/personal_intel/backend/tests/test_explore_discovery.py) (MODIFIED):**
    *   Updated default limit dictionary assertions and default refinement session selector lists to match the newly added `google_news` keys.

---

## 3. Verification & Deployment Commands

### A. Run Tests
Ensure all 419 unit tests pass:
```bash
uv run pytest
```
*   **Result:** `419 passed in 14.85s`

### B. Build Frontend
Ensure React and TypeScript build cleanly:
```bash
npm run build
```
*   **Result:** Built bundle cleanly in `78ms`.

### C. Live Deployment
To reload the local launchd service with the fresh build assets:
```bash
launchctl kickstart -k gui/$(id -u)/com.morning-dispatch
```
