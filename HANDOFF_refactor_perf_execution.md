# HANDOFF (execution): finish the refactor/perf batch

**For:** Codex, running in `/Users/macstudio/Apps/personal_intel` (repo `aaramos/Morning-Dispatch`).
**Source spec:** `HANDOFF_refactor_perf_review.md` (the original P1–P9 / M1–M8 plan). This file is the *execution* status — what's already on branches and what's left to finish.

Work was started by parallel agents that were interrupted by a session limit before verifying or opening PRs. Most branches contain substantial, **committed but unverified** work. Your job: per branch, verify it builds and passes tests, fix what's broken or unfinished, then open a PR. One branch (frontend) is a clean restart.

---

## Ground rules (apply to every task)

- **Tests:** `uv run pytest backend/tests -q` — baseline on `main` is **480 passing**. A task is not done until the full suite passes.
- **Frontend:** `npm ci` then `npm run lint` (eslint, `--max-warnings=0`) and `npm run build` (`tsc -b && vite build`).
- **Lint new Python:** `uvx ruff check <files>`.
- **API smoke** (for backend tasks that touch routes/services): start a throwaway server on a scratch port with an isolated home, then curl:
  ```bash
  MORNING_DISPATCH_HOME="$(mktemp -d)" uv run uvicorn backend.app.main:app --port 8077 &
  curl -sf localhost:8077/api/health
  curl -sf localhost:8077/api/explore/source-status
  curl -sf "localhost:8077/api/explore/explorations?limit=5"
  curl -sf localhost:8077/api/admin/status
  kill %1
  ```
  All must return 200 JSON.
- **Render parity** (for tasks that touch brief rendering — database, explore): `scripts/brief_parity.py` snapshots rendered brief HTML into a JSON fingerprint and diffs two snapshots. Render the same synthetic brief before and after your change and confirm a clean diff (the one documented exception is noted in Task 7).
- **Safety:** never touch the launchd service on `:8000` or the live `~/.morning-dispatch` data dir. Use a temp `MORNING_DISPATCH_HOME` for any server/test run.
- **Do not attempt a live brief build.** Model routing currently points at an unreachable endpoint; query-expansion/translation/editorial can't run. The e2e recipes above do not require it.
- **Per task:** review the full diff vs `main` for correctness regressions, run the verification, commit fixes, push, and open a PR with `gh pr create --base main`. Report the PR URL.
- **283ffa0 is sacred:** the foreign-media-yield commit shipped behavior that several tasks must not regress (flagged inline where relevant).

---

## Status of all 10 units

| # | Unit | Branch | State |
|---|------|--------|-------|
| 1 | Settings TTL cache (P1) | `perf/p1-settings-ttl-cache` | ✅ PR #2 open, 480 tests pass |
| — | Podcast/Reddit (P9/M8) | `perf/p9-m8-podcast-reddit` | ✅ PR #3 open |
| 2 | Status memos (P6 backend) | `perf/p6-backend-status-ttl` | committed, **verify + PR** → Task 1 |
| 10 | Refinement split (M7) | `refactor/m7-refinement-split` | committed, **verify + PR** → Task 2 |
| 8 | Table-driven results (M6) | `refactor/m6-direct-results-table` | WIP committed → Task 3 |
| 9 | Date consolidation (M5) | `refactor/m5-date-text-consolidation` | WIP committed → Task 4 |
| 6 | HTTP client pool (P8) | `perf/p8-http-pool` | WIP committed → Task 5 |
| 2 | database perf + split (P2/P3/P7/M2) | `perf/p2-p3-p7-m2-database` | WIP committed, M2 split unfinished → Task 6 |
| 3 | explore perf + split (P4/P5/M3/M4) | `perf/p4-p5-m3-m4-explore` | WIP committed → Task 7 |
| 5 | Frontend split + polling (M1/P6) | — | **restart from main** → Task 8 |

PR #2 and #3 already exist — review and merge them on their own; they are not part of the work below.

---

## Task 1 — P6 backend status memos (verify + PR)

**Branch:** `perf/p6-backend-status-ttl` (commit `1a2e22e`).
**Already committed:** 20s TTL memo around `mcp_status.status()` and `model_catalog.catalog_status()`, keyed by base_url; `reset_*` helpers; tests in `test_mcp_status.py`, `test_model_catalog.py`; autouse reset in `conftest.py`. (5 files, +221/-5.)
**Remaining:** verify only.
**Do:** `git checkout perf/p6-backend-status-ttl`; review the diff; `uv run pytest backend/tests -q` (focus `test_mcp_status.py`, `test_model_catalog.py`); confirm a second call within TTL does not re-hit the network. Push any fixes; `gh pr create --base main --title "perf: TTL-memo MCP and model-catalog status probes (P6)"`.
**Constraint:** only `mcp_status.py`, `model_catalog.py`, and their tests.

## Task 2 — Refinement split M7 (verify + PR)

**Branch:** `refactor/m7-refinement-split` (commit `74d90e3`).
**Already committed:** `refinement.py` (≈5,369 lines) split into `refinement_session.py` (1,319), `refinement_stream.py` (938), `strategy_refinement.py` (979), `profile_patch.py` (2,038); `refinement.py` kept as a re-export façade. Two test files lightly updated.
**Remaining:** verify it's a clean, behavior-preserving move.
**Do:** `git checkout refactor/m7-refinement-split`; review the diff for any logic change (there should be none — pure moves + façade); `uv run pytest backend/tests -q` (focus `test_refinement_strategy.py`, `test_agentic_flow.py`, `test_api.py`); run the API smoke and additionally:
```bash
curl -sf -X POST localhost:8077/api/explore/refinement-sessions -H 'Content-Type: application/json' -d '{"statement":"test"}'   # expect 201
```
Push fixes; `gh pr create --base main --title "refactor: split refinement.py into focused modules (M7)"`.
**Constraint:** do NOT edit `routes.py`; the façade must keep every name `routes.py` and the tests import working. Zero behavior change.

## Task 3 — Table-driven direct results M6 (finish + verify + PR)

**Branch:** `refactor/m6-direct-results-table` (commit `b06caaf`, WIP).
**Already committed:** `direct_article_results` in `backend/agents/librarian/articles.py` converted to a `_DIRECT_SOURCE_META` table; parametrized test added (`test_article_fetcher.py`). (+134/-51.)
**Remaining:** confirm the table transcribes **exact** prior values and behavior; finish if incomplete.
**Verify the table against the pre-change ternaries:** section labels, content_type strings, default scores (e.g. SEC `0.85`, FRED `0.88`, gmail `0.80`, default `0.65`), and the per-type quality-metadata key (`episode_quality_score`, `thread_quality_score`, `youtube_quality_score`, `collection_quality_score`, `market_quality_score`). `reddit_thread`/`reddit_post` share the "Legacy Discussion" fallback. The membership check must equal the prior set literal exactly. If a strict per-type key would change behavior for any payload carrying an unexpected metadata key, preserve the original lookup chain instead and note it in the PR.
**Do:** review diff; `uv run pytest backend/tests -q`; push; `gh pr create --base main --title "refactor: table-drive direct_article_results (M6)"`.
**Constraint:** zero behavior change; only `articles.py` + `test_article_fetcher.py`.

## Task 4 — Date consolidation M5 (finish + verify + PR)

**Branch:** `refactor/m5-date-text-consolidation` (commit `a92a3bd`, WIP).
**Already committed:** `backend/agents/librarian/date_text.py` extended (+113) with the repo-wide superset (English months, RFC 2822, ISO variants, URL-embedded dates); `gmail.py`, `gmail_mcp_client.py`, `markets.py`, `editor.py`, `scheduler.py` migrated to delegate to it; `test_date_text.py` added (232 lines).
**Remaining:** confirm each migrated module preserves prior parsing behavior; finish if incomplete.
**Check:** diff each migrated module's parsing against `main`; keep any intentional quirk as a thin local wrapper rather than dropping it. Ensure `allow_relative=False` is passed wherever free body text is scanned (so page chrome like "posted 2 days ago" is never read as a publish date) and `True` only for dedicated provider-date fields.
**Do:** review diff; `uv run pytest backend/tests -q` (focus `test_markets_adapter.py`, `test_gmail_digestor.py`, `test_gmail_mcp.py`, `test_editor.py`, `test_scheduler.py`, `test_foreign_media_yield.py`); push; `gh pr create --base main --title "refactor: consolidate date parsing onto date_text.py (M5)"`.
**Constraint:** do NOT edit `explore.py`, `database.py`, `podcast.py`, `adapters.py`, `google_news.py`, `articles.py`, `web_search.py` — those are a deferred follow-up.

## Task 5 — Shared HTTP client pool P8 (finish + verify + PR)

**Branch:** `perf/p8-http-pool` (commit `39f6ac6`, WIP).
**Already committed:** new `backend/app/core/http_pool.py` (58 lines — `shared_async_client` + `aclose_shared_clients`, generalized from `agents/model/client.py`); `web_search.py`, `youtube.py`, `google_news.py` converted to the pool; `aclose_shared_clients()` wired into `main.py` lifespan; adapter tests updated.
**Remaining:** verify; finish anything incomplete.
**Do:** review diff; `uv run pytest backend/tests -q` (focus `test_web_search_adapter.py`, `test_youtube_adapter.py`, `test_google_news_adapter.py`); start/stop the server and confirm no "unclosed client" warnings on shutdown; push; `gh pr create --base main --title "perf: shared pooled HTTP clients for discovery adapters (P8)"`.
**HARD CONSTRAINTS (must not regress 283ffa0):**
- `web_search._run_search` must catch `Exception`, **not** `BaseException` (so `CancelledError` propagates and the foreign-media 40s `wait_for` works).
- `search_web` must still fall through to the next provider on an **empty** result, not only on error.
- The `vertical="auto"` routing contract must hit byte-for-byte the same URL/params as before (news when a lookback is set; only foreign-media uses `organic`, breaking-recency uses `news`).
- Google News consent-cookie flow must still decode correctly with a shared (cookie-persistent) client.
- Do NOT edit `podcast.py`, `podcast_agent.py`, `adapters.py`, or `model/client.py`.

## Task 6 — database perf + split P2/P3/P7/M2 (finish + verify + PR)

**Branch:** `perf/p2-p3-p7-m2-database` (commit `cdfb4ba`, WIP).
**Already committed:** the brief renderer is extracted — `database.py` shrunk by ~2,649 lines and `backend/app/services/brief_renderer.py` created (2,676 lines); `routes.py` 1-line change (P7 wiring); `test_database_perf.py` added. This commit bundles **P2** (run-once `ensure_runtime_dirs` guard + `busy_timeout=5000` / `synchronous=NORMAL` pragmas in `connect()`), **P3** (single-query `update_exploration_progress` returning bool), **P7** (`summary_only` mode on `list_explorations`, wired into the list route), and the **renderer half of M2**.
**Remaining — the M2 domain split is NOT done.** Only the renderer was extracted. Finish M2: split the remaining data-access code out of `database.py` into modules under `backend/app/db/` — suggested `core.py` (connect/init/`_ensure_*` migrations/row helpers/`utc_now`/`new_id`), `explorations.py`, `topics.py`, `digests.py`, `podcasts.py`, `gmail.py`, `feedback.py`, `enrichment_cache.py`, `metrics.py` — keeping `database.py` as an **explicit re-export façade** (list each name; not `import *`) so all `database.xyz()` call sites and test monkeypatches keep working. First verify `brief_renderer.py` re-exports `render_ingested_issue`/`render_placeholder_issue` through `database` so existing callers (e.g. `explore.py`'s `database.render_ingested_issue`) are unaffected.
**Verify:**
- `uv run pytest backend/tests -q` (must pass).
- **Render parity:** render a synthetic brief via `database.render_ingested_issue` before and after, compare with `scripts/brief_parity.py` — must be clean.
- API smoke (all four endpoints above).
**Do:** push incrementally (renderer/perf first if not already, then the split); `gh pr create --base main --title "perf+refactor: database connect/progress/list perf + module split (P2/P3/P7/M2)"`.
**Constraint:** do NOT edit `explore.py`, `config.py`, `admin.py`, `main.py`. The façade must keep behavior identical.

## Task 7 — explore perf + split P4/P5/M3/M4 (finish + verify + PR)

**Branch:** `perf/p4-p5-m3-m4-explore` (commit `8a826f9`, WIP).
**Already committed:** new `build_queue.py` (98), `exploration_progress.py` (179), `source_window.py` (599); `explore.py` shrunk by ~976 lines. This bundles **M4** (delete `_accepts_param`, call functions directly), **P5** (single brief render with a publishing-seconds placeholder patched in after measuring), **P4** (debounced + `asyncio.to_thread` progress persistence), and **M3** (the module split above).
**Remaining:** verify each sub-item landed correctly; finish anything incomplete.
**Verify:**
- `uv run pytest backend/tests -q` (focus `test_explore_retry.py`, `test_explore_discovery.py`).
- **Render parity for P5:** render the final-brief path before/after with synthetic data and diff via `scripts/brief_parity.py` — the **only** allowed difference is the publishing-seconds value in the stats sidebar.
- Confirm M4: `_accepts_param` is gone and the previously-guarded calls pass all params directly.
- Confirm P4: `_persist_progress` debounces (>300 ms) and flushes at stage boundaries / before terminal status / in except handlers; async flushes go through `asyncio.to_thread`.
- API smoke.
**Do:** push; `gh pr create --base main --title "perf+refactor: explore pipeline perf + module split (P4/P5/M3/M4)"`.
**Constraints:** `explore.py` must re-export anything `main.py`/`routes.py`/tests import (e.g. `start_build_queue`, `stop_build_queue`, `cleanup_expired_exploration_briefs`, `purge_expired_deleted_explorations`). Do NOT collapse explore's own date helpers into `date_text.py` (deferred follow-up). Do NOT edit `database.py`, `config.py`, `agents/**`, `api/**`. `update_exploration_progress` may return a bool (Task 6) — do not rely on its return value.

## Task 8 — Frontend split + polling M1/P6 (restart from main)

**Branch:** none yet — start fresh: `git checkout main && git checkout -b refactor/m1-p6-frontend-split`. The prior attempt produced nothing usable (`App.tsx` is still 8,158 lines; `frontend/src/components/` and `frontend/src/lib/` exist but are empty).
**Do, as four commits, running `npm run lint && npm run build` after each (start with `npm ci`):**
1. **lib:** move the `api<T>()` helper, shared types/interfaces, constants (`defaultSourceSelection`, presets), and pure functions (`scaleContentLimits`, `loadInterestDraft`/`saveInterestDraft`/`clearInterestDraft`, `loadSessionValue`) into `lib/api.ts`, `lib/types.ts`, `lib/drafts.ts`.
2. **leaf components** (one file each under `components/`): `PodcastShowPicker`, `GmailApprovalCard`, `RecencyControl`, `ForeignRegionPicker`, `EditablePlanQuery`, `ChatMessageContent`, `StrategyReviewCard`, `NumberStepper`, `SourceChips`, `DisclosureButton`, `SystemLimitsPanel`, `SettingsErrorList`, `BuildStartingPanel`, `LibraryBuildProgress`, `ScheduledDeliveryAlert`, `QuickRecencyEditor`.
3. **panels/apps:** `RefinementPanel`, `ConfirmationPanel`, `StrategyRefinementModal`, `StrategyModalPlanPreview`, `ContentLimitsPanel`, `BriefControlsPanel`, `PipelineLimitsPanel`, `ProgressPanel`, `ReportingTabContent`, `BriefReadyPanel`, `SchedulePanel`, `EnableSourceModal`, `GmailAllowlistGroup`, `AdminApp` (+ its private subcomponents under `components/admin/`), `SecretHealthPanel`, `LibrarySection`, `DigestScheduleEditor`.
4. **P6 polling split:** split `loadHome` into `loadStatics` (source-status, admin/status, brief-settings, topic-profiles — on mount and after explicit user actions like saving credentials) and `loadBuildState` (explorations + scheduled-topic-profiles only — the 2.5s `setInterval` calls **only** this). When a background build finishes (status → complete/failed), call `loadStatics` once. Preserve all existing state-setting behavior (e.g. `deliveryConfigured`/`emailSendReady` derivation stays with the admin/status fetch).
Stages 1–3 are mechanical moves with zero behavior change; the type-checked `vite build` is the gate. `gh pr create --base main --title "refactor: split App.tsx + split home polling (M1/P6)"`.
**Constraint:** `frontend/src/**` only; no backend edits.

---

## Merge order

1. Land the small, non-overlapping ones first: Task 1 (P6), 2 (M7), 3 (M6), 4 (M5), 5 (P8), 8 (frontend) — plus the already-open PR #2 (P1) and PR #3 (P9/M8). These touch disjoint files and won't conflict.
2. Then the two large splits: Task 6 (database) and Task 7 (explore) — biggest blast radius, land last and one at a time, rebasing on `main` first.

## Deferred follow-ups (not in this batch)

- Collapse the remaining date parsers in `explore.py`, `database.py`, `podcast.py`, `google_news.py`, `adapters.py` onto the extended `date_text.py`.
- Unify the podcast HTTP helper (in PR #3) with `backend/app/core/http_pool.py`.
- Optional: extract `DispatchApp` state clusters into hooks (`useHomeData`, `useRefinementStream`, `useBuildProgress`) after M1 lands.
