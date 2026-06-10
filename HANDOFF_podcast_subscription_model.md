# Handoff: Curated Podcast-Show Subscription Model

You are reviewing and landing an uncommitted, tested change set in
`/Users/macstudio/Apps/personal_intel` on branch `main`
(remote: origin https://github.com/aaramos/Morning-Dispatch.git).

## Why

The old podcast pipeline was episode-first web search + per-episode recency/topic
gates — it repeatedly produced empty/timed-out podcast sections. This change set
replaces it with the user-specified **curated show-subscription model**:

1. User expresses interest → AI refines/expands → user confirms strategy.
2. When podcasts are selected, the system discovers podcast **shows** whose content
   matches the interest **at any time** (not the brief's lookback).
3. The user is shown candidate shows with a **summary of each show's usual content**
   and picks which to follow (saved subscription + newly-discovered shows surfaced
   each time).
4. Each build summarizes **each subscribed show's latest episode** in the Listen
   lane — **regardless of topic fit or the interest lookback** — with play / read
   transcript / click-through. The only gate is a **show-level staleness cutoff of
   60 days** (configurable): a show whose latest episode is older is suppressed with
   an honest note.
5. A **compelling** podcast episode is eligible for the **Top Stories** section.

## Files changed (uncommitted) — review with `git diff`

Backend:
- `backend/agents/digestor/podcast.py` (+237)
  - `fetch_subscribed_show_latest(shows, *, digest_id, staleness_days=60, inference_run_id, transcription_budget_seconds, deadline)` — per subscribed feed, picks the latest episode with playable audio (`_latest_episode_with_audio`), suppresses stale shows (`_within_staleness`, undated⇒stale) with a `stale_show` decision, summarizes via the existing transcript-feed/show-notes path (bounded transcription).
  - `discover_candidate_shows(queries, *, limit=12, staleness_days=60, enrich=True)` — show-first discovery for the picker; enriches each show with latest-episode title/date + stale flag.
  - `discover_podcasts(...)` now returns a `description` ("usual content").
- `backend/agents/discovery/adapters.py` (+61)
  - `_subscribed_podcast_shows(profile)` — confirmed shows (feed_url) from `requested_sources` + `promoted_sources`.
  - `PodcastSourceAdapter.query` uses subscriptions when present (latest-episode model); raises `AdapterUnavailable` when all stale/empty; legacy episode-first discovery remains the fallback when there are no subscriptions.
- `backend/app/services/explore.py` (+82) — `podcast_show_candidates(topic_id)` and `save_podcast_subscriptions(topic_id, shows)` (persists to `profile.requested_sources`).
- `backend/app/api/routes.py` (+27) — `GET`/`POST /explore/topic-profiles/{topic_id}/podcast-shows` (+ `PodcastShowRef`/`PodcastSubscriptionUpdate`).
- `backend/app/core/config.py` (+4) — `podcast_staleness_days` (env `MORNING_DISPATCH_PODCAST_STALENESS_DAYS`, default 60).
- `backend/app/db/database.py` (+58)
  - Top Stories eligibility: compelling podcasts (relevance/link ≥ `_PODCAST_TOP_STORY_THRESHOLD=0.7`, capped at `_MAX_PODCAST_TOP_STORIES=2`) mix into the top-stories pool and render as **media cards** (`_render_top_story`), removed from the Listen lane to avoid duplication; per-source remainder is story-only.
  - **Latent bug fix**: added module `logger` (`import logging; logger = logging.getLogger(__name__)`). `record_podcast_metric` referenced an undefined `logger` and crashed (NameError) on every exploration run whose `digest_id` wasn't a real digest row.

Frontend:
- `frontend/src/App.tsx` (+173) — `PodcastShowPicker` component + `fetchPodcastShows`/`savePodcastShows` API helpers, rendered in the `RefinementPanel` confirm view when podcasts are selected; `ensurePodcastTopicId()` saves the confirmed profile to obtain a topic_id, then lists/saves shows. (Note: the old `ConfirmationPanel` is dead code — `void ConfirmationPanel;`.)
- `frontend/src/styles.css` (+38) — picker styles.

Tests:
- `backend/tests/test_podcast_subscriptions.py` (NEW, 7 tests) — subscribed-show extraction, latest-within-staleness selection, stale-show suppression, `_within_staleness`, show discovery, subscription persistence round-trip, compelling-podcast Top Stories.
- `backend/tests/test_api.py` — `test_digest_run_can_publish_podcast_episodes` updated: a 0.76-quality podcast now leads Top Stories as a media card (player/transcript still asserted) rather than sitting in a Listen section.

## Review focus

- Confirm the subscription path bypasses topic + interest-lookback gates but enforces ONLY the 60-day staleness cutoff (undated ⇒ stale ⇒ suppressed, not surfaced).
- Confirm a promoted podcast renders once (Top Stories media card) and not also in Listen; non-compelling podcasts stay in Listen.
- Confirm `_subscribed_podcast_shows` dedupes by feed_url and ignores non-podcast/feedless entries.
- Confirm `ensurePodcastTopicId` safely upserts (TopicProfileCreate ignores the extra `candidate_limit`/`refinement_session_id` fields) and that the picker round-trips against `requested_sources`.
- Sanity-check the `logger` addition doesn't shadow any local `logger`.

## Validate (must all pass)

```bash
uv run pytest backend/tests/        # expect 365 passed
npm run build
npm run lint                        # 0 warnings
```

Do NOT commit untracked artifacts `pipeline_flowchart.html` or `scratch/`.

## Commit to main

```bash
git add backend/agents/digestor/podcast.py backend/agents/discovery/adapters.py \
        backend/app/services/explore.py backend/app/api/routes.py \
        backend/app/core/config.py backend/app/db/database.py \
        backend/tests/test_api.py backend/tests/test_podcast_subscriptions.py \
        frontend/src/App.tsx frontend/src/styles.css
git status   # confirm ONLY those are staged
git commit -m "$(cat <<'EOF'
feat: curated podcast-show subscription model (latest-episode, 60d staleness)

Replace episode-first/recency-gated podcast discovery with a show subscription
model. Discover shows matching the interest at any time, let the user confirm a
saved show list, and include each confirmed show's latest episode every build
regardless of topic fit or lookback, gated only by a 60-day show-level staleness
cutoff. Compelling episodes are eligible for Top Stories (rendered as media
cards). Adds show-discovery + subscription APIs, a confirm-step show picker, and
podcast_staleness_days config.

Also fixes a latent NameError in database.record_podcast_metric (undefined
module logger) that crashed podcast metric recording on exploration runs.

Covered by backend/tests/test_podcast_subscriptions.py.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
git push origin main
```

## Restart the app

Production server runs under launchd
(`~/Library/LaunchAgents/com.morning-dispatch.plist`):
`uvicorn backend.app.main:create_app --factory --host 0.0.0.0 --port 8000`.

```bash
launchctl kickstart -k gui/$(id -u)/com.morning-dispatch
# If the label differs, read <key>Label</key> from the plist or: launchctl list | grep -i morning
# Do NOT touch the :8001 --reload dev server.
```

Confirm it is back up (curl the app health/root route) and report status.

## Verify (preferred)

1. Open a topic with podcasts selected → confirm strategy → the confirm step shows
   **"Find & choose shows"**; pick a few shows and Save.
2. Build → the Listen lane summarizes each confirmed show's latest episode (play /
   transcript / link); shows with no episode in 60 days are absent (honest note),
   not stale audio; a compelling episode may appear in Top Stories as a media card.
3. Confirm the podcast lane no longer times out and isn't empty when subscribed
   shows have recent episodes.

Report back: commit SHA, push result, restart method + health check, and the
podcast lane status from a verification build.
```

---

## Addendum: markets/yfinance deadlock fix (same change set)

**Symptom:** briefs hung between the `review` and `done` stages, model server idle for 8+ minutes.

**Root cause:** the markets adapter ran `yfinance` via `asyncio.to_thread` with no library HTTP timeout. A yfinance call wedged on Yahoo (observed: many `CLOSE_WAIT` sockets to `*.yahoo.com` + open `py-yfinance` caches; process sleeping at ~6% CPU; no socket to the model server; DB not locked). `asyncio.wait_for` cancels the coroutine but cannot kill the worker thread, so hung threads leak and accumulate across builds until the **default `ThreadPoolExecutor` is exhausted** — after which the post-review `asyncio.to_thread(_compile_and_save)` (`explore.py`) can never get a worker and the brief deadlocks.

**Fix (`backend/agents/discovery/markets.py`):** `fetch_market_snapshots` now runs each yfinance call on a **dedicated bounded `ThreadPoolExecutor`** (`_MARKETS_EXECUTOR`, 6 workers) with a per-ticker `asyncio.wait_for` (`_MARKET_FETCH_TIMEOUT_SECONDS = 15`). Hung calls are abandoned and isolated to that pool, so they can never starve the default pool the rest of the pipeline depends on. Added a module `logger`. Regression test: `test_fetch_market_snapshots_isolates_hung_yfinance`. Suite: **369 passed**.

**Follow-up (recommended, not blocking):** give yfinance a real HTTP timeout (`yf.Ticker(..., session=...)` is supported in yfinance 1.4) so the dedicated pool itself can't slowly fill; consider the same dedicated-executor isolation for other blocking discovery `to_thread` calls (youtube transcript, sec/fred).

Add `backend/agents/discovery/markets.py` and `backend/tests/test_markets_adapter.py` to the staged commit, and restart the server (the in-flight build is running on the old code and can still hang until restarted).
