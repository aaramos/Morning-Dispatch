# Handoff: Foreign Media Yield & Quantity

You are improving the foreign-media lane in `/Users/macstudio/Apps/personal_intel`
on branch `main`. This document is the full spec, grounded in a live diagnosis of
exploration `25c73604-30c2-4685-a8a6-f64ee7783e70` (Mexico City solo-travel brief,
rebuilt 2026-06-10): foreign_media adapter status `completed`, 6.4s elapsed,
**0 candidates**, zero foreign entries in the funnel, empty brief section even
after the global broaden retry. Read end to end before writing code.

## Diagnosis (verified live — do not re-derive)

Reproduced with the real profile and real provider keys:

1. **News-vertical trap (the dominant killer).** `SerperBackend.search`
   (`backend/agents/discovery/web_search.py:232`) sets
   `is_news = days is not None` and switches the endpoint to
   `google.serper.dev/news` whenever ANY bounded lookback exists.
   `TavilyBackend.search` (~line 64) likewise sets `topic: "news"` when `days`
   is passed. Foreign queries are mostly evergreen/guide-shaped (native
   "best taco stands in Roma/Condesa" content), which news indexes don't carry.
   Measured with the profile's actual generated Spanish query:
   - news endpoint (`days=180`): **0–2 hits**
   - organic endpoint, no date restrict: **10 hits**
   - organic endpoint + `tbs=qdr:y` date restrict: **8 hits**
   The date restrict is fine; the news vertical is what starves the lane.
2. **One query per language.** `ForeignMediaSourceAdapter.query`
   (`backend/agents/discovery/foreign_media.py:147`) searches only
   `plan[*].native_query` — exactly one query per language. This run: 2 searches
   total (es, pt) while web_search ran 10–20 queries. Meanwhile the refinement
   agent had written **10 excellent native Spanish queries** into
   `profile.source_queries["foreign_media"]` — grep confirms that key is read
   **nowhere** in the discovery code. The lane's best queries are dead data.
3. **The stored language plan was empty.** `profile.foreign_language_plan == []`,
   so `foreign_language_plan_for_profile` (foreign_media.py:227) re-derived
   seeds from `foreign_regions` and called the refinement model **mid-build** to
   write one native query per language — nondeterministic, ~6s of the 40s
   adapter timeout, and silently degrades to `_fallback_plan_entry` (English
   terms) if the model client is unavailable. The plan should be generated and
   persisted at refinement-confirm time, like every other strategy field.
4. **First-provider-empty short-circuit.** `search_web`
   (web_search.py:414-423) returns the first provider's result **even when it
   is empty** — Brave is configured (provider order: serper, brave) but never
   consulted when Serper's news endpoint returns 0.
5. **Locale/relative dates pass through raw.** `_clean_hit_date`
   (web_search.py:269) is a no-op passthrough. Serper returns Spanish locale
   dates ("19 ago 2025") and relative dates ("10 months ago"); if downstream
   parsing misses them, `foreign_web` items become undated and get demoted by
   the strict source-window rules.
6. Minor: Tavily ignores the `language` parameter entirely (no field in its
   payload), and `_looks_like_english_result` (foreign_media.py:546) only
   applies to CJK scripts — the quality gate is NOT the problem for Latin-script
   languages; this is purely a yield problem.

## Design decisions (settled)

- **Foreign media searches Google organic, never the news vertical, unless the
  brief is explicitly breaking-news-shaped** (`recency_weighting == "breaking"`).
  Recency is enforced with the organic `tbs` date restrict plus the existing
  downstream demote-don't-delete window/floors — not by switching index.
- **The lane fans out the refinement-written native queries** instead of one
  model-written query per language.
- **The language plan is built once at refinement confirm and persisted**;
  build-time regeneration remains only as a fallback for legacy profiles.
- **Empty is a retriable signal**: a provider returning 0 hits falls through to
  the next provider.

## Phase 1 — Provider routing: organic vs news (`web_search.py`)

- Add a keyword-only `vertical: Literal["auto", "organic", "news"] = "auto"`
  parameter to `search_web` and thread it to each backend.
  - `SerperBackend`: use the news endpoint only when `vertical == "news"`, or
    when `vertical == "auto"` and `days is not None and days <= 7` (preserves
    current behavior for short-lookback news-shaped web briefs). Otherwise hit
    the organic endpoint and keep applying `tbs = _serpapi_tbs(days)` — already
    supported there (web_search.py:224).
  - `TavilyBackend`: set `topic: "news"` under the same rule; otherwise omit
    `topic` and keep `days` only if Tavily accepts it for general topic (if the
    API rejects it, drop `days` for organic and rely on downstream recency).
  - `BraveBackend`/SerpAPI: respect the same flag where each API distinguishes
    news; otherwise no-op.
- `ForeignMediaSourceAdapter.query` passes
  `vertical=("news" if profile.recency_weighting == "breaking" else "organic")`.
- In `search_web`, when a provider returns an **empty list**, continue to the
  next provider instead of returning; return the first non-empty result (fall
  back to the last empty result if all are empty). Log per-provider hit counts
  at info level.

## Phase 2 — Query fan-out: use the refinement queries (`foreign_media.py`)

- In `ForeignMediaSourceAdapter.query`, build the per-language query list as:
  1. the plan entry's `native_query` (as today), plus
  2. up to `_QUERIES_PER_LANGUAGE = 6` entries from
     `profile.source_queries.get("foreign_media", ())`, assigned to the
     language they're written in.
  For language assignment, don't over-engineer: when the plan has ONE language
  (the common case), all foreign source_queries belong to it. With multiple
  languages, ask the plan-generation model to tag each stored query with its
  language code in one batched call, falling back to attaching untagged queries
  to the first/primary language. Dedupe (casefolded) across the merged list.
- Run searches with a small semaphore (4) so fan-out (≤ 6 queries × languages)
  stays inside the 40s `cost_profile` timeout; keep
  `DEFAULT_RESULTS_PER_LANGUAGE = 20` as the per-language **candidate cap**
  applied after merging+deduping hits across that language's queries (raise the
  per-search `limit` to 10 and dedupe by URL).
- Append must-have native aliases where present: if
  `profile.must_have_aliases` has native-language aliases for the plan's
  language, ensure each outgoing query contains the anchor or an alias
  (reuse `enforce_must_have_on_queries` from
  `backend/agents/discovery/query_refiner.py` with the language-appropriate
  alias preference that already exists there).

## Phase 3 — Persist the language plan at refinement confirm (`refinement.py`)

- In the `astream_refinement` finalize block (where
  `expand_must_have_aliases` is already awaited, ~line 658): when
  `foreign_media` is selected and `foreign_language_plan` is empty, call
  `foreign_language_plan_for_profile` (it already returns the completed plan)
  and store the result on the profile before `save_topic_profile`. This makes
  the plan reviewable in the strategy preview and removes the mid-build model
  call + nondeterminism.
- Keep the build-time derivation in `foreign_language_plan_for_profile` as the
  legacy-profile fallback (unchanged behavior when the stored plan exists —
  it already early-returns).
- Remediate the Mexico City profile by re-saving it after this lands (same
  one-off as the must-have remediation).

## Phase 4 — Locale + relative date parsing

- Extend the date-normalization path used for search hits and foreign articles
  (`_normalize_date_text` in `backend/agents/librarian/articles.py:575` and
  `_date_from_text`/`_MONTHS` in `explore.py` — locations per the earlier
  source-content remediation) with:
  - Spanish/Portuguese/French/German/Italian month names AND abbreviations
    (ene/feb/mar/abr/may/jun/jul/ago/sept|sep/oct/nov/dic; janeiro…dez; etc.),
    keyed by `source_language` when available, tried generically otherwise.
  - English relative forms Serper organic emits: "N hours/days/weeks/months
    ago" → resolve against now.
- Prefer provider-supplied dates over body-text scanning (already the
  intention; verify it holds for the serper organic `date` field).

## Phase 5 — Funnel visibility (`foreign_media.py` + adapter status)

- The adapter's `AdapterStatus` message must state per-language activity
  instead of a silent `completed/0`, e.g.
  `es: 7 queries → 34 hits, kept 21, excluded 13 (english-looking 2, blocked
  domains 11); pt: 7 queries → 0 hits`. Exclusion counts already exist as
  logger.info lines (foreign_media.py:166-176) — aggregate them into the status
  and, when a selected language nets 0 kept candidates, emit a
  `source_filter_notes` entry (same shape as the must-have guardrail note:
  `source_name` "Foreign Media", `reason` naming the language and the dominant
  drop reason) so the Reporting tab shows *why* instead of an empty section.

## Phase 6 — Small quality boosts (optional, last)

- In `_foreign_media_quality`, add a small positive `score_adjustment` for
  country-TLD hosts matching the language/region (`.mx`/`.com.mx` for es +
  latin_america, `.br`/`.com.br` for pt), using the existing `_host_matches`
  helper. Do NOT add new exclusion rules — quantity is the problem here.

## Tests (`backend/tests/test_foreign_media_yield.py`, NEW + extend web_search tests)

1. Serper routes to organic endpoint with `tbs` when `vertical="organic"` and
   `days=180`; routes to news only for `vertical="news"` or auto+days≤7
   (mock httpx, assert endpoint + payload).
2. Tavily omits `topic: "news"` for organic vertical.
3. `search_web` falls through to the next provider on an empty result and
   returns the first non-empty list; still raises `AdapterUnavailable` only
   when all providers error.
4. Adapter fan-out: profile with 10 `source_queries["foreign_media"]` entries
   and a 1-language plan issues plan-query + capped extra queries, dedupes
   merged hits by URL, respects per-language cap.
5. Plan persistence: finalize path stores a non-empty
   `foreign_language_plan` when foreign_media is selected (mock the model
   client); stored plans are not regenerated at build time.
6. Date parsing: "19 ago 2025", "1 mar 2025" (es), "10 months ago", "3 weeks
   ago" all normalize to correct dates; unknown text still yields None.
7. Adapter status message includes per-language query/hit/kept counts; a
   0-kept language emits the source_filter_notes entry.

## Validate (must all pass)

```bash
uv run pytest backend/tests/
npm run build        # only if frontend touched (Phase 5 needs no frontend work)
npm run lint
```

## Commit & restart

Stage only changed files; commit to `main`
(`feat: foreign media organic search, native query fan-out, persisted language plan`),
push, then restart production (launchd serves frontend/dist; an un-restarted
server tests stale code):

```bash
launchctl kickstart -k gui/$(id -u)/com.morning-dispatch
# Do NOT touch the :8001 --reload dev server.
```

## Verify (preferred)

1. Re-save topic profile `5251173f-34a9-4bea-a61a-b18984a31c14` (persists the
   language plan), then rebuild exploration `25c73604-...`.
2. Expect: foreign_media adapter status reports per-language counts with
   double-digit hits for `es`; the funnel report contains foreign_media
   entries; the brief's Foreign Media section renders Spanish items (CDMX
   travel/food content) with working translation affordances.
3. Confirm the must-have gate keeps Spanish items via native aliases (CDMX /
   Ciudad de México) — rejections in the funnel should name genuinely
   off-anchor items only.
4. Regression: a breaking-news-shaped brief (recency_weighting="breaking")
   still uses the news vertical; English web_search behavior unchanged for
   short lookbacks.

Report back: commit SHA, restart + health check, per-language adapter counts,
and the Foreign Media item count in the rebuilt brief.
