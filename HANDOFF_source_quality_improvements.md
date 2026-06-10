# Handoff: Source-Quality Improvements (companion to the capacity/recency/refinement release)

Audience: the developer landing the current uncommitted release in
`/Users/macstudio/Apps/personal_intel` (capacity raises, lane caps, randomized
screening, date-adjudication split, unlimited recency, foreign regions, Reddit
comment dates, refinement intents). This document does NOT re-specify that
release. It lists quality work that should follow it — plus a few spots where
the release's new caps are silently defeated by older clamps, which are cheap
to fix while you're in the code.

## Why quality is the next bottleneck

Evidence from the per-candidate lifecycle logs
(`runtime/data/digest-output/exploration-<id>-reporting.json`, aggregated over
the 8 most recent builds, 2026-06-05 → 2026-06-09, pre-release):

- Discovery already over-delivers: 1,100–2,800 candidates per run; only ~30–55
  rendered. Of ~3,030 fetch-stage drops, ~2,980 were "never fetched" (budget),
  only ~50 were real fetch failures (403/short-text).
- web_search: 150–250 discovered → 24–48 fetched (recorded
  `source_fetch_budgets`) → **~20–40 of the fetched died POST-fetch at the
  recency window** → 1–3 included. The fetch budget was mostly spent on items
  that were stale but undated at fetch time.
- gmail: 900–2,400 candidates; ~70–88% dropped at discovery (dedupe/screening);
  2–13 included.
- markets: every snapshot included, never reviewed by any model gate.

The release raises every capacity number (lane caps 250–750, inclusion caps
×2, fetches 1,000, audit 150, editorial 500, critic 250). That fixes
starvation, but it means **far more content now flows through the same quality
gates** — and the gates' selection behavior (first-N truncation, score-only
fetch ordering, date-blind search) becomes the dominant quality factor.

Caveat for all before/after comparisons: audit/editorial/critic are
model-driven, so per-run survivor counts are noisy. Judge changes by the
structural funnel numbers (fetch budgets, recency-drop counts, audited share),
not one run's totals.

---

## P0 — closes gaps that will be exposed immediately by the new caps

### 1. Date-aware web discovery (stop spending fetch budget on stale pages)
**Problem:** `_select_fetch_payloads_for_budget`
([explore.py:1378](backend/app/services/explore.py:1378)) pre-screens only
*known*-stale candidates. Tavily is called with `search_depth: "basic"` and no
`topic` ([web_search.py:42](backend/agents/discovery/web_search.py:42)), which
rarely returns `published_date`, so most hits arrive undated, pass the
pre-screen, consume fetch slots, and then die at the post-fetch window. This
survives the release unchanged — bigger budgets just fetch more stale pages.
**Suggestion:**
- When a bounded window is active, request Tavily with `topic: "news"` (its
  news vertical reliably returns `published_date`); consider Brave's
  `/res/v1/news/search` endpoint as the bounded-window variant of
  [BraveBackend](backend/agents/discovery/web_search.py:84) and SerpAPI's
  `google_news` engine.
- In `_select_fetch_payloads_for_budget`, order each lane's fetch queue
  *dated-in-window first, undated second* instead of score-only, so dated
  fresh items can't be crowded out by confident-but-undated ones.
**Acceptance:** web_search post-fetch recency drops fall from ~80% of fetched
to <25%; web items included rises accordingly with no inclusion-cap change.

### 2. Per-host fetch concurrency (the 35-way semaphore will trip rate limits)
**Problem:** `fetch_articles_for_payloads` uses one global
`asyncio.Semaphore(concurrency)`
([articles.py:82](backend/agents/librarian/articles.py:82)). The release
raises default concurrency 15→35. Lanes are homogeneous by domain (reddit.com,
medium.com, big publishers), so 35 in-flight can mean 10+ concurrent hits on
one host → 403/429 → *lower* successful-fetch counts than before. The old
comment in brief_settings warned about exactly this.
**Suggestion:** add a per-host semaphore (2–3 per host) inside the fetch loop,
keeping the global cap at 35. Cheap and makes the higher global limit safe.
**Acceptance:** fetch failure rate (non-"never fetched" fetch-stage drops)
stays at or below the pre-release ~1.6% at concurrency 35.

### 3. Stratify the audit and date-adjudication windows by source
**Problem:** both gates select **first-N from a single ranked list**:
`results[:limit]` in [source_audit.py:592](backend/agents/source_audit.py:592)
and `at_risk_indexes[:limit]` in
[explore.py:2021](backend/app/services/explore.py:2021). With ~500+ fetched
items post-release and windows of 150/100, whichever lane sorts first (usually
gmail by volume) monopolizes the audit; other lanes pass unaudited, and markets
(direct payloads) are never audited at all — the funnel shows markets at 100%
inclusion with zero review.
**Suggestion:** allocate each window per-source proportionally (or round-robin
by lane, like the fetch-budget floor/fill passes), and include direct-source
payloads (markets, collections) in the audit pool.
**Acceptance:** every selected source shows a non-zero audited share in the
reporting funnel; markets items can be dropped by audit.

### 4. Raise the Reddit adapter's internal cap to match its new 500 lane
**Problem:** `reddit_candidate_cap = min(max(1, context.candidate_limit), 20)`
([adapters.py:1007](backend/agents/discovery/adapters.py:1007)). The release
gives Reddit a 500 lane cap, but the adapter still emits ≤20 candidates — the
lane cap is dead code for this source.
**Suggestion:** raise the hard cap (e.g. 100–150, it also bounds comment
fetches at lines 1272/1392/1450) and add `/new/.rss` alongside `/hot/.rss` for
bounded-window briefs — hot ranking is popularity, not recency, which is why
the lane oscillates between "17 included" and "19 recency-dropped" runs.

### 5. Web lane cannot physically fill its 750 cap — fix the supply math
**Problem:** per-query limit is clamped to 20
([adapters.py:235](backend/agents/discovery/adapters.py:235)), queries to 20
([adapters.py:963](backend/agents/discovery/adapters.py:963)), providers to
25/request — so the theoretical max is ~400 pre-dedupe hits against a 750
lane. Also the second-wave query refinement only triggers when total hits < 3
([adapters.py:255](backend/agents/discovery/adapters.py:255)) — with 20
queries it never fires, even when every hit is stale.
**Suggestion:** raise the per-query clamp toward the provider max (25), and
change the refinement trigger from "hits < 3" to "**in-window dated** hits <
lane target" so the refiner reacts to staleness, not just emptiness.

### 6. Unlimited recency skips served-once dedupe (regression risk in the release)
**Problem:** `_apply_source_window_filter` returns early when
`lookback_hours is None`
([explore.py:2094](backend/app/services/explore.py:2094)), so
`_mark_undated_once` / served-once tracking
([explore.py:2115](backend/app/services/explore.py:2115)) never runs in
unlimited mode. Scheduled briefs with Unlimited will re-surface the same
undated evergreen items every edition with no suppression.
**Suggestion:** in unlimited mode, keep the early return for *window*
rejection but still apply the served-once marking/lookup for undated strict
sources (the `served_undated_items` table already exists). Also decide whether
score-only ranking is right when no window exists — consider weighting dated
items above undated ones so unlimited briefs don't fill with SEO evergreens.

---

## P1 — quality systems worth building next

### 7. Fetch refill loop
Budget lost to post-fetch recency/audit drops is never reallocated — one wave,
then the pipeline moves on. After P0-1 reduces waste, a refill loop in the
fetch stage ("while lane survivors < inclusion target and candidates remain,
fetch the next ranked batch, bounded by the global 1,000") converts the
remaining waste into coverage. `_FETCH_OVERSAMPLE = 2` / `_RESERVED_FETCH_FLOOR
= 10` ([explore.py:69](backend/app/services/explore.py:69)) become tuning
knobs instead of guesses.

### 8. Pre-fetch near-duplicate clustering
Duplicates are currently caught **after** they've consumed fetch + enrichment +
critic slots (the critic's drop reason "redundant, duplicate, or low-value" is
a recurring funnel entry). At 1,000 candidates, cluster pre-fetch on normalized
title + registered domain (cheap; no model) and fetch only the best-scored
member per cluster. This also keeps the doubled per-source sections from
reading repetitively at `target_items: 50`.

### 9. Learned source reputation (turn the dead tables on)
`source_weights`, `feedback`, and `exploration_feedback` tables exist with 0
rows; `source_scout_*` tables have no code references; `promoted_sources` has
313 rows that only boost scores. Suggest: per-domain and per-gmail-sender
rolling stats (fetch success rate, audit relevance, inclusion rate) computed
from the reporting JSONs already on disk, used as a score prior at discovery
time — replacing the hardcoded `LOW_QUALITY_DOMAINS` /
`SYNDICATED_AGGREGATOR_DOMAINS` lists in
[source_audit.py](backend/agents/source_audit.py:29). A thumbs-up/down control
per brief item can feed the same tables later.

### 10. RSS/Atom adapter
The one missing source type, and the cheapest high-quality one: dated, free,
no bot-blocking, exactly what the strict window wants. Seed feeds from
`promoted_sources` domains and the domains Gmail newsletters link to most. It
slots into the existing adapter registry + lane/inclusion-cap machinery with
`DEFAULT_LANE_CAP`/`DEFAULT_PER_SOURCE_MAX` defaults.

### 11. Search-provider federation
`search_web` is failover-only — Brave/SerpAPI never run while Tavily succeeds
([web_search.py:328](backend/agents/discovery/web_search.py:328)). Querying
two providers in parallel and merging (URL dedupe already exists in the
adapter) roughly doubles result diversity per query and softens any one
provider's ranking bias. Pairs well with P0-5.

---

## P2 — guardrails for the new scale

### 12. Per-run quality scorecard
Aggregate each run's reporting JSON into one persisted summary row per source:
discovered, fetched, fetch-failures, in-window %, audited %, included,
build-time per stage. The data already exists; this makes regressions (like
the ones this release is meant to fix) visible in one query instead of a
session of forensics.

### 13. Local-model capacity at the new window sizes
All five gates route to the local model (`model-settings.json`: everything
`"provider": "local"`, Gemma4-MTP-26B). The release multiplies gate windows
(editorial 150→500 records, critic 50→250) — watch for: context-window
truncation producing silently arbitrary selections, per-stage latency pushing
builds past 30–40 min, and timeout fallbacks to deterministic paths (which
skip quality review entirely). Check `inference_metrics` error/latency rates
after the first live builds; consider batching editorial like screening
already does (15-item batches, concurrency 8), and optionally allow cloud
routing for critic on large runs.

### 14. Small consistency nits in the release diff
- Queue rebuild path still defaults `candidate_limit` to 250
  ([explore.py:196](backend/app/services/explore.py:196)) while interactive
  paths now resolve against `MAX_CANDIDATE_BUDGET = 1000`.
- `ExplorationCreate.candidate_limit` default stays 150
  ([routes.py:77](backend/app/api/routes.py:77)) — fine if profiles always
  carry `content_limits.total_items`, otherwise ad-hoc builds quietly run at
  15% of the new budget.
- `SourceAdapterContext.candidate_limit` default is still 150
  ([types.py:51](backend/agents/discovery/types.py:51)).

---

## Suggested order

1. P0-2 (per-host fetch limit) and P0-4 (Reddit cap) — small, de-risk the release itself.
2. P0-1 + P0-5 (date-aware search + supply math) — the single biggest quality lever.
3. P0-3 (stratified audit) and P0-6 (unlimited served-once) — correctness of the new gates/modes.
4. P1-7/8 (refill + dedupe), then P1-9/10/11 as standalone projects.
5. P2-12 first if you want before/after numbers for everything above — it is
   read-only over files that already exist.
