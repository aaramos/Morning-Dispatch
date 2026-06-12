# HANDOFF: Refactor & Performance Review

Code review of the application (backend + frontend) with two goals:
1. Refactors for maintainability.
2. Optimizations that make the app faster with **no loss of content or quality** (no caps lowered, no model stages removed, no items dropped).

> **Reconciled against `283ffa0` (foreign-media yield work, merged 2026-06-10).** The original review predated that merge; line references below have been re-anchored to the post-merge tree (`explore.py` is now 3,595 lines; `web_search.py`, `articles.py`, `foreign_media.py`, `refinement.py` changed; `backend/agents/librarian/date_text.py` is new). Item M5 was rewritten to build on `date_text.py` instead of creating a new module, and P8 gained do-not-regress constraints from that commit's self-review fixes.

Items are ordered by impact within each section. Each item is self-contained: problem, evidence, fix, verification. Implement P-items independently; M-items note their dependencies.

Global verification after any batch: `uv run pytest backend/tests` and a manual build of one topic profile against the launchd service (rebuild `frontend/dist` + `launchctl kickstart` per runtime topology — port 8000 serves a prebuilt bundle, not Vite dev).

---

## Part 1 — Performance

### P1. Cache `get_settings()` (highest impact, smallest diff)

**Problem:** `get_settings()` in `backend/app/core/config.py:221` is called **89 times** in non-test code, including inside per-article and per-candidate loops. Every call re-reads the environment AND does multiple disk reads: `_secret_text()` file reads for every API key (config.py:249-330 area), `model-settings.json` JSON parse (`_model_settings_payload`, config.py:174), brief settings, etc. During one build this happens hundreds of times.

**Fix:**
- Add a module-level cache with explicit invalidation:
  ```python
  _SETTINGS_CACHE: tuple[float, Settings] | None = None
  _SETTINGS_TTL_SECONDS = 5.0

  def get_settings() -> Settings:
      global _SETTINGS_CACHE
      now = time.monotonic()
      if _SETTINGS_CACHE is not None and now - _SETTINGS_CACHE[0] < _SETTINGS_TTL_SECONDS:
          return _SETTINGS_CACHE[1]
      settings = _build_settings()   # rename current body
      _SETTINGS_CACHE = (now, settings)
      return settings

  def reset_settings_cache() -> None:
      global _SETTINGS_CACHE
      _SETTINGS_CACHE = None
  ```
- A short TTL (rather than `lru_cache`) keeps "edit secret file / model-settings.json takes effect within seconds" behavior, which the admin UI relies on.
- Call `reset_settings_cache()` explicitly from every code path that writes settings/secrets so changes are instant: `save_web_search_credentials` / `save_youtube_credentials` (`backend/app/services/explore.py:415,444`), the admin endpoints in `backend/app/api/admin.py` that write model settings, brief settings (`backend/app/services/brief_settings.py` writers), and secret-file writers.
- Tests: add an autouse fixture (or call in existing fixtures) that calls `reset_settings_cache()` so env-var monkeypatching keeps working. Grep tests for `MORNING_DISPATCH_` monkeypatch usage and clear cache there.

**Verify:** full test suite; toggle a model route in Admin and confirm it takes effect on the next build.

### P2. Stop re-running `ensure_runtime_dirs()` on every DB connection

**Problem:** `connect()` (`backend/app/db/database.py:105-116`) calls `get_settings()` + `ensure_runtime_dirs()` on **every** connection. `ensure_runtime_dirs` (`backend/app/core/config.py:392`) does ~18 `mkdir` + `chmod` syscalls. Nearly every DB function opens its own connection, so this runs thousands of times per build.

**Fix:**
- Run `ensure_runtime_dirs` once: guard with a module-level flag keyed by `settings.data_dir` (so tests that point at temp dirs still get dirs created):
  ```python
  _RUNTIME_DIRS_READY: set[Path] = set()

  def _ensure_dirs_once(settings: Settings) -> None:
      if settings.data_dir in _RUNTIME_DIRS_READY:
          return
      ensure_runtime_dirs(settings)
      _RUNTIME_DIRS_READY.add(settings.data_dir)
  ```
- While here, add pragmas to `connect()` after the existing `foreign_keys` line:
  ```python
  connection.execute("PRAGMA busy_timeout = 5000")
  connection.execute("PRAGMA synchronous = NORMAL")
  ```
  The DB is WAL (set at init); `synchronous=NORMAL` is the recommended WAL pairing and removes an fsync per commit. `busy_timeout` protects the queue worker / scheduler / API threadpool from "database is locked" errors. No data-content change.

**Verify:** test suite; run two concurrent builds (queue) and watch for lock errors in logs.

### P3. `update_exploration_progress` does 3 connections per call; callers discard the result

**Problem:** `backend/app/db/database.py:1422-1438`: each call runs `get_exploration()` (connection #1, existence check), the UPDATE (connection #2), then `get_exploration()` again (connection #3) to build a return value. `_persist_progress` (`backend/app/services/explore.py:3364`) — the dominant caller, invoked ~18 static call sites plus per-adapter-status callbacks during discovery — **discards the return value**. So 2 of 3 queries are pure waste.

**Fix:**
- Rewrite as a single connection; use `cursor.rowcount` instead of the pre-read; return a bool:
  ```python
  def update_exploration_progress(exploration_id: str, *, progress: dict[str, Any]) -> bool:
      with connect() as connection:
          cursor = connection.execute(
              "UPDATE explorations SET progress_json = ? WHERE exploration_id = ?",
              (json.dumps(progress, sort_keys=True), exploration_id),
          )
          return cursor.rowcount > 0
  ```
- Grep all callers (`grep -rn "update_exploration_progress" backend/`) — if any caller actually uses the returned record, have that caller call `get_exploration()` itself.

**Verify:** test suite (test_explore_discovery.py exercises progress persistence); watch the UI progress panel during a build.

### P4. Debounce progress persistence and move it off the event loop

**Problem:** `_persist_progress` runs synchronous sqlite + `json.dumps` of the full progress dict directly on the event loop, dozens of times per build (including inside adapter-status callbacks that fire mid-`gather`). Every call stalls the loop, which also serves SSE refinement streams and API requests.

**Fix (both halves are simple and content-neutral):**
1. Coalesce: make `_persist_progress` a small debouncer — persist immediately if >300 ms since last write, otherwise mark dirty and let the next call (or a `flush=True` call) write. Add `flush=True` at stage boundaries (`_set_pipeline_stage` transitions, before `update_exploration_status`, in `except` handlers) so the UI never misses a terminal state. The UI polls at 2.5 s, so sub-300 ms granularity is invisible.
2. In async contexts (`_run_exploration`, `_run_digest_core` via its `persist` lambda), wrap the actual write in `await asyncio.to_thread(...)`. Keep a sync path for sync callers. Easiest shape: `_persist_progress` stays sync (used by callbacks), and the debounce means it rarely touches the DB; stage-boundary flushes from async code go through a tiny `async def _persist_progress_async(...)` wrapper using `asyncio.to_thread`.

**Verify:** build a brief while a refinement chat stream is open; the stream should not stutter. Confirm progress panel still updates through all stages and on failure.

### P5. Brief is rendered and written **twice** at the end of every build

**Problem:** `backend/app/services/explore.py:1007-1037`: the pipeline calls `build_stats()` + `database.render_ingested_issue(...)` + `_write_exploration_brief(...)`, measures publishing duration, then calls all three **again** solely so the stats sidebar shows the just-measured publishing time. `render_ingested_issue` (`backend/app/db/database.py:2831-3229`) is a ~400-line renderer that re-cleans newsletter bodies (regex + BeautifulSoup paths) per payload — this doubles the most expensive non-LLM publishing step.

**Fix (exact output preserved):** render once with a placeholder token for the publishing-duration value, then patch by string replacement:
1. In `build_stats()`, set `stage_seconds["publishing"]` to a sentinel value the renderer prints (e.g. pass `publishing_seconds=None` and have the stats sidebar emit `__PUBLISHING_SECONDS__`).
2. Render once, measure elapsed, `html = html.replace("__PUBLISHING_SECONDS__", f"{elapsed:.1f}")` (match the current formatting in the stats sidebar exactly — check how `stage_seconds` is formatted in `render_ingested_issue` / `build_digest_stats` before choosing the format string), write once.
3. `_apply_model_health_to_progress(progress, digest_stats)` only reads model-call counters, which don't change between the two renders — call it after the single `build_stats()`.
4. Update `progress["brief"]["stats"]` with the patched publishing number so UI stats match the HTML.

**Verify:** `scripts/brief_parity.py` (already in repo) comparing a brief built before/after the change; the only allowed diff is the publishing-seconds value itself.

### P6. Polling storm: 6 endpoints every 2.5 s during builds, two of which make outbound HTTP calls

**Problem:** `loadHome` (`frontend/src/App.tsx:1202`) fetches `source-status`, `explorations?limit=25`, `scheduled-topic-profiles`, `topic-profiles`, `admin/status`, and `admin/brief-settings` — and a `setInterval` re-runs **all six** every 2.5 s while a background build runs (App.tsx:1229-1235). Per tick the backend then:
- `/api/admin/status` (`backend/app/api/admin.py:189`): `model_catalog.catalog_status` (HTTP to oMLX) + `mcp_status.status` (2 HTTP calls, `backend/app/services/mcp_status.py:14`) + ~8 DB summary queries + secret-health file checks.
- `/api/explore/source-status` (`backend/app/services/explore.py:310`): another `mcp_status.status` (2 more HTTP calls), Gmail credential file reads, collections filesystem scan, DB queries.

That's ~6 outbound HTTP requests and dozens of file/DB reads per 2.5 s, competing with the build itself.

**Fix (two independent layers; do both):**
1. **Frontend:** split `loadHome` into `loadStatics` (source-status, admin/status, brief-settings, topic-profiles — load on mount and after explicit user actions like saving credentials) and `loadBuildState` (explorations + scheduled-topic-profiles only). The 2.5 s interval calls only `loadBuildState`. When the build completes (status flips to complete/failed), call `loadStatics` once.
2. **Backend:** add a tiny TTL memo (15–30 s) around `mcp_status.status(settings)` and `model_catalog.catalog_status(settings)` keyed by base_url, in their own modules. These report external-service health; sub-30-second staleness is fine and protects any future caller, not just this poll loop.

**Verify:** Network tab during a build — per tick should be 2 requests, both cheap; admin page still reflects credential changes immediately after save (because saves explicitly refetch).

### P7. Exploration list endpoint ships full `progress_json` for 25 rows per poll

**Problem:** `list_explorations` (`backend/app/db/database.py:1508-1539`) does `SELECT *`, and `_exploration_row_to_dict` parses the full progress JSON — which contains reasoning buckets, exclusion lists, per-source notes — for all 25 rows on every 2.5 s poll. Only the active build's progress is actually used at full fidelity by the home screen; library rows need status/title/timestamps and a few summary fields.

**Fix:**
- Add a `summary_only: bool = True` mode to `list_explorations` used by the `/api/explore/explorations` route: select explicit columns, and reduce progress to the small subset the list UI reads. **First grep the frontend** for which fields of `progress` are read from `recentExplorations` (vs. the single `exploration` fetched by id) — expect things like `queue`, `pipeline`, `brief.title`, `brief.stats`, `built_with_issues`, `requested_source_issues` — and keep exactly those. The detail route `/api/explore/explorations/{id}` keeps full progress.
- Cheap alternative if frontend surface is larger than expected: keep full parse but cap the JSON sent per row to the keys actually used. The win is payload + parse time, not the SQL.

**Verify:** library cards render identically (title, status chips, issue badges, stats line); active build progress panel unaffected (it uses the per-id endpoint).

### P8. Reuse pooled HTTP clients in discovery adapters

**Problem:** The model client already pools connections (`backend/agents/model/client.py:22-48`), but discovery creates a fresh `httpx.AsyncClient` (new TLS handshake pool) per call: `web_search.py:105,167,230,302` (one per provider request), `youtube.py:79`, `google_news.py:94,369`, several in `podcast.py` and `gmail_mcp_client.py`. A build issues dozens of these. The foreign-media fan-out added in `283ffa0` multiplies per-language web-search calls, making the per-call client churn worse than at original review time.

**Fix:** generalize the `_SHARED_HTTP_CLIENTS` pattern from `backend/agents/model/client.py` into a small shared helper, e.g. `backend/app/core/http_pool.py`:
```python
def shared_async_client(*, purpose: str, timeout: float | None, follow_redirects: bool = False, http2: bool = False, headers: dict | None = None) -> httpx.AsyncClient
async def aclose_shared_clients() -> None
```
keyed by `(purpose, loop)` with the same loop-binding logic already proven in client.py. Replace the per-call `async with httpx.AsyncClient(...)` blocks in the adapters listed above with the shared client (drop the `async with` — lifecycle is owned by the pool; per-request `timeout=` arguments still apply). Wire `aclose_shared_clients()` into the FastAPI shutdown hook next to `aclose_shared_model_clients()`.

**Scope discipline:** convert `web_search.py`, `youtube.py`, `google_news.py` first (highest call counts); `podcast.py` in a second pass since several of its clients carry per-call header/timeout variations.

**Do not regress `283ffa0` behavior while refactoring `web_search.py`:**
- `_run_search` deliberately catches `Exception`, **not** `BaseException`, so `asyncio.CancelledError` propagates and the foreign-media adapter's 40 s `wait_for` timeout works. Keep that exact semantics.
- `search_web` falls through to the next provider when a provider returns an **empty** result (not just on error). Pooling clients must not short-circuit that fall-through.
- The `vertical` routing contract: `vertical="auto"` is byte-for-byte the pre-`283ffa0` behavior (news whenever a lookback is set); only the foreign-media lane passes `organic` and breaking-recency briefs pass `news`. A client refactor must not alter which URL/params each vertical hits.

**Verify:** adapter tests (`test_web_search_adapter.py`, `test_youtube_adapter.py`, `test_google_news_adapter.py`) — they likely use `respx` or transport mocks; if they patch `httpx.AsyncClient`, add an injection seam (optional `client` param, as `google_news.py:269` already does).

### P9. Unbounded `timeout=None` HTTP clients can hang an entire stage

**Problem:** `backend/agents/discovery/adapters.py:1274,1405` (reddit, `timeout=None`) and `backend/agents/digestor/podcast.py:1248` (`timeout=None`). One stuck connection holds the stage's `asyncio.gather` open indefinitely — this is a tail-latency bug, not just style.

**Fix:** give each an explicit `httpx.Timeout`, e.g. `httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)` for reddit JSON/RSS; for the podcast audio-download client at podcast.py:1248 use a long read timeout (e.g. read=120) rather than none. Check call sites for an existing per-request timeout before choosing values; keep whichever is stricter.

**Verify:** reddit + podcast adapter tests; build a podcast-heavy topic.

---

## Part 2 — Maintainability refactors

### M1. Split `frontend/src/App.tsx` (8,158 lines) into modules

**Problem:** one file holds ~35 components (list below from `grep "^function [A-Z]"`), all shared types, the API helper, localStorage helpers, and `DispatchApp` with **70+ `useState` hooks**. The empty `frontend/src/components/` and `frontend/src/lib/` directories already exist for this purpose.

**Fix — mechanical file moves, zero behavior change, do in 3 PRs:**
1. **PR 1 — `lib/`:** move the `api<T>()` helper, all shared TypeScript types/interfaces, constants (`defaultSourceSelection`, presets), and pure functions (`scaleContentLimits` App.tsx:623, `loadInterestDraft`/`saveInterestDraft`/`clearInterestDraft` App.tsx:1007-1043, `loadSessionValue` App.tsx:1045) into `lib/api.ts`, `lib/types.ts`, `lib/drafts.ts`. Export everything; App.tsx imports.
2. **PR 2 — leaf components:** move self-contained components that only take props: `PodcastShowPicker` (769), `GmailApprovalCard` (2648), `RecencyControl` (2804), `ForeignRegionPicker` (2851), `EditablePlanQuery` (3357), `ChatMessageContent` (3395), `StrategyReviewCard` (3460), `NumberStepper` (4510), `SourceChips` (5200), `DisclosureButton` (7463), `SystemLimitsPanel` (4426), `SettingsErrorList` (4447), `BuildStartingPanel` (4655), `LibraryBuildProgress` (5051), `ScheduledDeliveryAlert` (7291), `QuickRecencyEditor` (7439). One file each under `components/`.
3. **PR 3 — panels and apps:** `RefinementPanel` (2883), `ConfirmationPanel` (3542), `StrategyRefinementModal` (3883), `ContentLimitsPanel` (4137), `BriefControlsPanel` (4251), `PipelineLimitsPanel` (4461), `ProgressPanel` (4542), `ReportingTabContent` (4696), `BriefReadyPanel` (5083), `SchedulePanel` (5146), `EnableSourceModal` (5232), `GmailAllowlistGroup` (5365), `AdminApp` (5452, large — give it `components/admin/`), `SecretHealthPanel` (7235), `LibrarySection` (7344), `DigestScheduleEditor` (7371).
4. Optionally afterwards: extract `DispatchApp` state clusters into hooks (`useHomeData` wrapping `loadHome`/polling, `useRefinementStream`, `useBuildProgress`). Do this only after P6 lands to avoid churn.

**Verify per PR:** `npm run build` (tsc catches missed imports), eslint, and a manual click-through (start refinement, build, open library, open admin).

### M2. Break up `backend/app/db/database.py` (7,218 lines) — it is a god module

**Problem:** one module contains: connection management + schema migrations; CRUD for explorations, topic profiles, digests, podcasts, gmail senders, collections, feedback, metrics; the model-enrichment cache; **a ~400-line HTML brief renderer** (`render_ingested_issue`, 2831-3229) plus newsletter text-cleaning regexes and BeautifulSoup logic (lines 44-90 and helpers). Rendering does not belong in the DB layer, and the file's import block alone pulls `bs4` into every DB consumer.

**Fix — extract by responsibility, keep `database.py` as a façade:**
1. **First and most valuable:** move `render_ingested_issue`, `render_placeholder_issue` (3229), `build_issue_snapshot` consumers, the newsletter cleaning regexes/constants (lines 44-90), and all `_render*`/`_clean*` helpers they use into `backend/app/services/brief_renderer.py`. They take plain data (payloads, ArticleFetchResults, stats dict) and return strings — no DB access. Where they *do* call DB functions, pass the data in from the caller instead.
2. Then split data modules under `backend/app/db/`: `core.py` (connect, init, `_ensure_*` migrations, row helpers), `explorations.py`, `topics.py` (topic profiles + refinement sessions + promoted sources), `digests.py` (digests, runs, items, issues, stats), `podcasts.py` (sources + caches + metrics), `gmail.py` (senders/allowlist), `feedback.py` (feedback + source weights + served-undated), `enrichment_cache.py`, `metrics.py` (inference/agent-decision summaries).
3. **Façade:** `database.py` becomes `from backend.app.db.explorations import *` etc. (explicit re-export list, not `*`, to keep linters honest). All existing `database.xyz()` call sites and test monkeypatches keep working. Migrate call sites to direct imports opportunistically later; do not do a big-bang import rewrite.

**Verify:** full test suite; `scripts/brief_parity.py` before/after the renderer extraction (output must be byte-identical).

### M3. Split `backend/app/services/explore.py` (3,595 lines)

**Problem:** one module owns the build queue worker, credential-saving endpoints' logic, the exploration pipeline, fetch budgeting, source-window/date logic, progress bookkeeping, strategy repair, and brief assembly.

**Fix — extract in dependency order (each step is a move + import update, no logic change):**
1. `backend/app/services/build_queue.py`: `start/stop_build_queue`, `_signal_build_queue`, `_build_queue_worker`, `cancel_exploration`, `_raise_if_cancelled`, `BuildCancelled` (explore.py:132-205).
2. Date helpers `_parse_datetime_hint`, `_parse_datetime_string`, `_date_from_url`, `_date_from_text`, `_article_published_at`, `_article_text_or_url_date` (explore.py:2510-2645) → collapse into `date_text.py` delegation per M5 (do not move them to a new module first; that's churn).
3. `backend/app/services/exploration_progress.py`: `_initial_progress`, `_set_pipeline_stage`, `_set_source_status`, `_init_reasoning_bucket`, `_reasoning_flusher`, `_set_candidate_count`, `_set_exclusion_reasons`, `_persist_progress`, `_persistable_progress` (~2755-3380). This is also where P4's debouncer lives.
4. `backend/app/services/source_window.py`: the `_apply_source_window_filter` family, `_adjudicate_dates_before_source_window_filter`, undated-item helpers, and `_foreign_language_coverage_notes` (new in `283ffa0`, explore.py:2648 — it reads the persisted language plan + surviving candidates to emit per-language 0-survivor notes into `progress.source_filter_notes`; keep it next to the other source-issue builders).
5. What remains in explore.py: `_run_exploration`, `_run_digest_core`, fetch budgeting, strategy repair, and the public entry points — still big, but single-purpose.

**Dependency note:** do M3 after P3/P4/P5 so the perf changes don't have to chase moved code.

### M4. Delete the `_accepts_param` inspect-signature dance

**Problem:** `explore.py:662` defines `_accepts_param(func, name)` and 6 call sites use `inspect.signature` to check whether *functions in the same module / same repo* accept `low_yield` / `threshold` / `recency_reserve` / `starved_sources` before passing them (explore.py:744, 805, 850-854, 916). The signatures are statically known; this is dead flexibility that hides real signature errors (a typo'd param silently isn't passed instead of raising).

**Fix:** call the functions directly with all parameters; delete `_accepts_param`. If any of these guards exist because tests monkeypatch the functions with narrower fakes, fix the fakes to accept `**kwargs`.

**Verify:** test suite — particularly `test_explore_retry.py` / `test_explore_discovery.py` which exercise the low-yield retry path.

### M5. Finish consolidating date parsing onto `backend/agents/librarian/date_text.py`

**Status update (`283ffa0`):** the shared leaf module now exists — `backend/agents/librarian/date_text.py` ships `normalize_date_string(value, *, allow_relative=True, now=None)`, `parse_relative_date`, and `month_from_token` with es/pt/fr/de/it month names + abbreviations and English relative phrasing. `web_search.py` and `articles.py` already delegate to it; `explore.py::_date_from_text` (explore.py:2606-2645) keeps its own English `_MONTHS`/regex/CJK/dotted-numeric parsing and only **falls through** to `normalize_date_string(..., allow_relative=False)` as a last resort. **Do not create a parallel `backend/app/core/dates.py`** — extend `date_text.py` instead; it is already the designated leaf module with three importers.

**Remaining problem:** date parsing is still re-implemented in `podcast.py` (11 `fromisoformat`/`parsedate_to_datetime` hits), `database.py` (4), `markets.py` (4), `gmail.py` (4), `gmail_mcp_client.py` (3), `editor.py`, `adapters.py`, `google_news.py`, `scheduler.py` — plus the explore.py helpers above that only partially delegate.

**Fix:**
1. Extend `date_text.py` to the superset: English month map, ISO 8601 with/without Z, RFC 2822 via `parsedate_to_datetime`, URL-embedded dates, and the CJK/dotted-numeric patterns currently private to explore.py. Return timezone-aware UTC datetimes (or ISO strings, matching the existing `normalize_date_string` contract — pick one and keep both shapes available via thin helpers).
2. Collapse explore.py's `_parse_datetime_hint` / `_parse_datetime_string` / `_date_from_url` / `_date_from_text` (explore.py:2542-2645) into delegation, keeping the end-of-day (`23:59:59`) defaulting they apply.
3. Migrate the other modules one at a time, **diffing behavior with each module's existing parser first** — some have intentional quirks (Reddit comments-RSS dates, Google News locale handling) that must become explicit parameters, not get lost.
4. **Preserve the `allow_relative` semantics from `283ffa0`:** `allow_relative=False` for broad body-text scans (so "posted 2 days ago" page chrome isn't read as a publish date), `True` only for dedicated provider-date fields. Any migrated call site scanning free text must pass `False`.

**Verify:** the date-heavy test files: `test_foreign_media_yield.py` (22 new tests covering date_text), `test_source_content_remediation.py`, `test_podcast_digestor.py`, `test_google_news_adapter.py`, `test_reddit_adapter.py`, `test_markets_adapter.py`.

### M6. Table-drive `direct_article_results`

**Problem:** `backend/agents/librarian/articles.py:124-190`: eight `is_*` booleans feeding two long chained-ternary expressions and a quality-score chain. Adding a source type means touching four spots in one function.

**Fix:** one dict:
```python
_DIRECT_SOURCE_META: dict[str, tuple[str, str, float, str]] = {
    # source_type: (section, content_type, default_score, quality_metadata_key)
    "gmail": ("Newsletter Content", "newsletter", 0.80, ""),
    "podcast_episode": ("Podcast Signals", "podcast", 0.65, "episode_quality_score"),
    "youtube_video": ("YouTube Videos", "video", 0.65, "youtube_quality_score"),
    ...
}
```
Read the current ternaries carefully to transcribe exact values (e.g. SEC=0.85, FRED=0.88), then replace the conditionals with dict lookups. The membership check at line 127 becomes `payload.source_type in _DIRECT_SOURCE_META`.

**Verify:** `test_article_fetcher.py` and a brief parity run.

### M7. Split `backend/app/services/refinement.py` (4,999 lines)

**Problem:** one module holds the legacy turn-based refinement, the SSE streaming chat (`astream_refinement`), strategy refinement + review (both sync and streaming variants), gmail-specific refinement, profile patch/merge utilities, and ~30 prose-formatting helpers.

**Fix:** split along the seams already visible in the function list:
- `refinement_session.py` — `start_session`, `advance_session`, `_initial_profile`, `_apply_models`, session CRUD glue.
- `refinement_stream.py` — `astream_refinement`, `_astream_gmail_discovery`, `_astream_gmail_approval`, `_astream_fallback`, `_visible_prose`, `_parse_chat_payload`, chat prompt builders.
- `strategy_refinement.py` — `astream_refine_strategy`, `astream_review_strategy`, `refine_strategy`, `review_strategy`, `confirm_strategy_refinement`, fingerprint/pending helpers.
- `profile_patch.py` — `_merge_agent_profile_patch`, `_apply_agent_update`, `_merge_string_lists`, `_source_selection_dict`, `_string_list`, etc. (pure functions, easiest to test).
- Keep `refinement.py` as a façade re-exporting the public names used by `routes.py` and tests.

**Dependency note:** lowest urgency of the M-items; do last. The streaming-chat redesign work is active in this area — coordinate so a split doesn't land mid-feature. `283ffa0` added `_ensure_foreign_language_plan` to the confirm path; an open follow-up from that work is that `save_topic_profile` does **not** run it (existing profiles keep an empty `foreign_language_plan` until a fresh refinement-confirm). If a backfill lands, it will touch the same confirm/save seam this split moves — sequence accordingly.

### M8. `backend/agents/digestor/podcast.py` (2,297 lines): extract the client layer

**Problem:** feed fetching, Podcast Index search, episode resolution, audio download, transcription orchestration, caching, and digesting live in one module with ~10 separately-constructed HTTP clients.

**Fix (lighter touch than the others):** extract `podcast_http.py` (feed/audio/index HTTP + the shared client from P8) and `podcast_resolution.py` (episode→feed resolution + its caches, which already have DB-backed cache functions in database.py). Keep digesting/transcription in place. Do together with the P8 podcast pass to avoid touching the same lines twice.

---

## Suggested implementation order

| Batch | Items | Why together |
|-------|-------|--------------|
| 1 | P1, P2, P3, P12-style pragmas (in P2) | Tiny diffs, biggest wall-clock wins, no API changes |
| 2 | P6, P7 | Both halves of the polling cost; one frontend PR + one backend PR |
| 3 | P4, P5 | Pipeline-internal; verify with brief_parity |
| 4 | P8, P9, M8 | All touch adapter HTTP clients |
| 5 | M1 (3 PRs) | Frontend-only, mechanical |
| 6 | M2 (renderer first), then M3, M4 | Backend structure; renderer extraction unlocks parity-checked refactors |
| 7 | M5, M6, M7 | Opportunistic cleanups |

Notes for the implementer:
- Port 8000 is the launchd service serving `frontend/dist`; rebuild and `launchctl kickstart -k gui/$UID/com.morning-dispatch` before manual verification, or you're testing a stale build.
- **Manual build verification is currently blocked**: model routing points at an unreachable cloud endpoint (`api.ollama.com`) while local Ollama (:11434) and LM Studio (:1234) are up. Query expansion, translation, and editorial all need the model, so no brief can build until routing is repointed. Repoint first (Admin model settings), or rely on the test suite + `brief_parity.py` for batches that don't strictly need a live build.
- `scripts/brief_parity.py` is the safety net for anything touching rendering or article selection: outputs must be identical except where an item explicitly says otherwise (P5's publishing-seconds value).
- None of the P-items change candidate counts, caps, model stages, prompts, or filtering thresholds — if an implementation forces such a change, stop and reconsider; that's out of contract.
