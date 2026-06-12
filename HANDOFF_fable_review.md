# HANDOFF: Fable Review of Merged Refactor/Performance Batch

Audience: Fable reviewer  
Repo: `/Users/macstudio/Apps/personal_intel`  
Review target: `main` at `5ea7c68` (`fix: re-export renderer duration helper`)  
Prepared: 2026-06-11

## Current State

The refactor/performance batch has been merged to `main`, pushed to `origin/main`, and the live app has been restarted. This review should focus on integration risk in the merged tree, not on re-reviewing isolated branch diffs.

Open PRs at handoff time: none.

Live app access:
- Local: `http://127.0.0.1:8000/`
- Tailnet: `https://ultras-mac-studio-2.tail4aeef0.ts.net/`
- Health endpoints:
  - `http://127.0.0.1:8000/api/health`
  - `https://ultras-mac-studio-2.tail4aeef0.ts.net/api/health`

Live admin status reports:
- Release revision: `5ea7c68`
- Release timestamp: `2026-06-11T16:46:37-07:00`
- Public base URL: `https://ultras-mac-studio-2.tail4aeef0.ts.net`
- Health: `ready`
- Safe for overnight: `true`
- Scheduler: enabled and running, daily run time `05:00` `America/Los_Angeles`
- Gmail: connected and send-ready
- Local model and MCP: available
- Expected warnings: latest run has not run yet; digest email delivery is not enabled.

## What Landed

Merged sequence:

1. PR #2: cache `get_settings()` with a short TTL.
2. PR #3: bound podcast/reddit HTTP clients and extract podcast HTTP/resolution modules.
3. PR #4: add backend status TTL caching.
4. PR #5: split refinement handling.
5. PR #6: convert direct article results to a table-driven shape.
6. PR #7: consolidate date text handling.
7. PR #8: introduce shared async HTTP client pooling and shutdown cleanup.
8. PR #9: split frontend `App.tsx` and reduce home polling work.
9. PR #10: split database responsibilities and apply DB performance changes.
10. PR #11: split explore responsibilities and apply single-render publishing optimization.
11. Integration fix: `5ea7c68`, re-export `_format_stage_duration` from `backend/app/db/database.py` after the database split so the explore single-render path can keep using the database facade.

## Verification Already Run

Full verification after merge:

```bash
uv run pytest backend/tests -q
```

Result: `557 passed in 14.26s`

```bash
npm run lint
```

Result: passed.

```bash
npm run build
```

Result: passed.

Live checks at this handoff:

```bash
curl -sf http://127.0.0.1:8000/api/health
curl -sf https://ultras-mac-studio-2.tail4aeef0.ts.net/api/health
curl -sf http://127.0.0.1:8000/api/admin/status
```

Result: local and tailnet health returned `ok`; admin status returned revision `5ea7c68` and health `ready`.

## Highest-Value Review Focus

Please review the merged integration surface first:

1. `backend/app/db/database.py`
   - Confirm the facade intentionally re-exports every symbol still used by callers and tests.
   - In particular, verify the `5ea7c68` fix for `_format_stage_duration` is the only missing export created by the PR #10 + PR #11 interaction.

2. `backend/app/services/explore.py` and extracted explore modules
   - Check that the single-render publishing optimization still records publishing duration correctly in both HTML output and progress stats.
   - Confirm stage progress persistence still flushes terminal/failure states.
   - Confirm source-window, date, and candidate filtering semantics did not drift during extraction.

3. `backend/tests/conftest.py`
   - PR #2 and PR #4 both touched cache reset behavior.
   - The final merged shape should reset `get_settings`, MCP status, and model catalog caches before and after tests.

4. `backend/app/core/http_pool.py`
   - Verify shared clients are keyed safely, do not leak across event loops, and are closed during app shutdown.
   - Check migrated discovery adapters still preserve provider fallthrough, timeout, and cancellation behavior.

5. Frontend polling split
   - The home build interval should refresh only build-state endpoints during active work.
   - Static/admin/source status refreshes should happen on mount, explicit user actions, and completion transitions.
   - Confirm no UI panel now depends on stale static state during normal admin actions.

6. Database split and renderer extraction
   - Confirm renderer code no longer drags DB-only concerns into rendering.
   - Confirm old `database.xyz()` callers and monkeypatches still work through the facade.
   - Check that query/connection reductions preserved behavior, not just tests.

## Suggested Fable Commands

Start with current state:

```bash
git status --short --branch
git log --oneline --decorate -14
gh pr list --state open --json number,title,headRefName
```

Re-run the verification set:

```bash
uv run pytest backend/tests -q
npm run lint
npm run build
curl -sf http://127.0.0.1:8000/api/health
curl -sf https://ultras-mac-studio-2.tail4aeef0.ts.net/api/health
curl -sf http://127.0.0.1:8000/api/admin/status
```

Optional targeted checks:

```bash
rg "_format_stage_duration|render_ingested_issue|shared_async_client|reset_settings_cache" backend
rg "setInterval|loadBuildState|loadStatics" frontend/src
```

## Known Non-Blockers

- The working tree contains existing untracked local handoff/material files, currently including `.claude/`, `HANDOFF_apple_intelligence_provider.md`, `HANDOFF_refactor_perf_execution.md`, and `HANDOFF_refactor_perf_review.md`. Treat those as local review artifacts unless the owner says otherwise.
- This new file, `HANDOFF_fable_review.md`, is also a local handoff artifact unless intentionally staged later.
- Admin health warnings for "latest run has not run yet" and "digest email delivery is not enabled" are expected in the current setup.
- No live full brief build was run as part of this handoff document creation. The batch was validated by tests/build/lint and live service health.

## Acceptance Criteria for Fable Signoff

Fable can sign off when:

1. `main` remains at or beyond `5ea7c68` with no unmerged required PRs.
2. Full backend tests, frontend lint, and frontend build pass.
3. Local and tailnet health endpoints return `ok`.
4. Admin status reports the deployed revision and health `ready`.
5. No merge interaction issues are found across the DB facade, explore single-render flow, shared HTTP pool, cache reset behavior, or frontend polling split.

