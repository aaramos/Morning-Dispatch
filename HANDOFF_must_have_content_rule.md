# Handoff: Optional "Must-Have" Content Rule in Interest Refinement

You are implementing a new feature in `/Users/macstudio/Apps/personal_intel` on
branch `main`. Nothing for this feature exists yet — this document is the full
spec. Read it end to end before writing code.

## Why

Proactive query expansion (`expand_search_strategy` in
`backend/agents/discovery/query_refiner.py`, folded into every selected
source's queries by `_expand_profile_queries` in
`backend/agents/discovery/runner.py`) deliberately widens the net with
"affiliated, adjacent, or synonymous angles." That works for broad interests
but breaks anchored ones: a "potential holiday in Mexico City" brief produced
travel content from around the world, because expanded queries kept the theme
(holiday travel) and dropped the anchor (Mexico City). The existing downstream
gates don't catch this:

- `_apply_topic_relevance` is a token-overlap gate — "travel"/"holiday" tokens
  overlap fine on a Lisbon travel-deals article, so it passes.
- `_apply_topic_relevance` **fully exempts foreign media** (English tokens
  can't judge native-language text), so foreign drift isn't gated at all.
- `screen_candidates` only screens Gmail and podcast candidates.

The fix: let the user optionally designate one or more **must-have anchor
terms** during interest refinement. The refinement agent expands each anchor
into a synonym/alias set (English + every language in the brief's foreign
plan — e.g. Mexico City → CDMX, Ciudad de México, México DF), and the pipeline
then **hard-requires** every candidate to mention each anchor (or one of its
aliases) or be excluded, with the rejection visible in the funnel reporting.
When no anchor is set, behavior is byte-for-byte unchanged.

## Architecture context (read these files first)

- `backend/agents/discovery/types.py` — `TopicProfile` dataclass,
  `from_payload` (~line 125) / `to_payload` (~line 156). `exclusions` and
  `priority_terms` are the precedent for flat profile list fields.
- `backend/agents/discovery/runner.py` — `DiscoveryRunner.run` pipeline order:
  `_expand_profile_queries` → adapters → `_apply_exclusions` →
  `_apply_topic_relevance` → `screen_candidates` → lane limits. Study
  `_apply_exclusions` + `_matched_exclusion_terms` (~line 488): the must-have
  gate is its mirror image. Note the funnel-exclusion record shape
  (`excluded_by`, `reason`, candidate identity fields) — reporting depends on
  it. Note also `_candidate_relevance_text` / `_candidate_has_judgeable_topic_text`
  and the requested/promoted-source exemption in `_apply_topic_relevance`.
- `backend/agents/discovery/query_refiner.py` — `expand_search_strategy`
  (proactive expansion, the drift source) and `refine_queries_for_adapter`
  (low-yield fallback queries; same drift risk).
- `backend/app/services/refinement.py` — refinement session state machine.
  `FIELD_ORDER` (line 34), `QUESTIONS` (line 55), the profile
  normalization/serialization sites (the `_string_list(profile.get(...))`
  clusters around lines 1350, 1782–1800, 1890, 2204, 2286, 4232, 4305, 4502),
  `_strategy_fingerprint` (~line 1777), the profile_patch merge key list
  (~line 1742), and the chat-instruction prompts (~lines 1380, 1903).
- `backend/app/services/explore.py` — builds `TopicProfile` payloads from
  stored profiles (~lines 1357, 2911, 2933 for the `priority_terms` example).
- `backend/app/api/routes.py` — Pydantic profile models (`priority_terms` at
  line 71 is the template).
- `backend/app/core/prompt_loader.py` — `load_prompt(key)`; prompts live in
  `config/prompts.yaml` with `FALLBACK_PROMPTS` in the module.
- `frontend/src/App.tsx` — profile types (~lines 46–161), plan preview chips
  for exclusions (~3434, ~3574), draft override editor (~3740,
  `splitList`/`uniqueCleanList` at ~1796–1807).

## Design decisions (settled — do not re-litigate)

1. **Two flat profile fields, not a nested object** (matches codebase
   convention):
   - `must_have_terms: tuple[str, ...]` — user-specified anchors. Empty tuple
     = feature off, zero behavior change.
   - `must_have_aliases: dict[str, tuple[str, ...]]` — agent-generated synonym
     sets keyed by the anchor term (casefolded key). Aliases include English
     synonyms/abbreviations AND native-language variants for every language in
     `foreign_language_plan` / selected foreign-media languages. The anchor
     itself always counts as its own alias; a missing dict entry means "anchor
     only" — alias generation failing open never disables the gate.
2. **Match semantics:** a candidate passes if, for **every** anchor term, the
   candidate's text contains the anchor **or any of its aliases** (AND across
   anchors, OR within an alias set). Matching is case-insensitive substring
   over the candidate's relevance text (title + subject/link_text + raw_text +
   source_name — reuse `_candidate_relevance_text`), with **accent folding**
   (NFKD-strip combining marks on both sides) so "Ciudad de Mexico" matches
   "Ciudad de México". Do NOT tokenize — "mexico city" is a phrase and must
   match as one.
3. **Enforcement is layered — both at query construction and as a hard
   post-fetch gate:**
   - Expansion queries must carry the anchor (cheap, keeps fetch budget spent
     on-theme).
   - The deterministic gate in the runner is the guarantee (catches drift from
     original queries, adapters' own fan-out, and foreign media).
4. **Foreign media is NOT exempt** — that's the point of multilingual aliases.
   This gate is the first deterministic relevance check foreign candidates get.
5. **Exemptions mirror `_apply_topic_relevance`:** candidates from
   requested/promoted sources (`_is_candidate_from_requested_source`) are
   exempt — a user-subscribed podcast show's latest episode must keep its
   "always included" contract (see HANDOFF_podcast_subscription_model.md).
   Candidates without judgeable text (`_candidate_has_judgeable_topic_text`
   false) and `markets` candidates are exempt. Everything else — including
   gmail, reddit, google_news, youtube, foreign_media — is gated.
6. **No low-yield loosening.** Unlike `_apply_topic_relevance`, the must-have
   gate does NOT relax when retrieval is sparse. A sparse on-anchor brief
   beats a full off-anchor brief; the funnel report tells the user why it's
   thin. (This is the user's explicit intent: "ensure ALL content contains
   mexico city.")
7. **Gate placement:** immediately after `_apply_exclusions`, before
   `_apply_topic_relevance` — rejected items must never consume lane slots or
   screening budget. Funnel tag: `excluded_by: ["must_have"]` with reason
   `"Missing required term(s): <anchors that failed>"`.

## Phase 1 — Profile schema plumbing

`backend/agents/discovery/types.py`:
- Add `must_have_terms: tuple[str, ...] = ()` and
  `must_have_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)`
  to `TopicProfile`; thread through `from_payload` / `to_payload` (coerce
  aliases values via the existing `_string_list` helper; casefold dict keys).

`backend/app/services/refinement.py`:
- Add both fields to every profile normalization/serialization cluster listed
  above (limits: terms 6, aliases 12 per term), to `_coerce_profile`, to the
  profile_patch merge key list (~1742), to `_strategy_fingerprint`, and to the
  patch-change formatter (~1659–1667) so chat surfaces "Must-have terms"
  as a changed field. Reuse `_string_list`; add a small
  `_clean_must_have_aliases` normalizer modeled on `_clean_source_queries`.

`backend/app/api/routes.py`:
- Add `must_have_terms: list[str] = Field(default_factory=list)` and
  `must_have_aliases: dict[str, list[str]] = Field(default_factory=dict)` to
  the same Pydantic models that carry `priority_terms`/`exclusions`.

`backend/app/services/explore.py`:
- Thread both fields through every site that builds/serializes profile
  payloads (follow the `priority_terms` occurrences at ~1357, ~2911, ~2933).

## Phase 2 — Refinement capture + alias generation

- `FIELD_ORDER`: insert `"must_have"` after `"exclusions"`. `QUESTIONS`
  entry: `"Is there a term every single item must mention — like a place,
  company, or product? (Optional. I'll also match common synonyms and
  translations, e.g. Mexico City → CDMX, Ciudad de México.)"` Skipping/empty
  answer leaves the feature off; wire the `*_answered` bookkeeping the same
  way `exclusions` does (~line 3256).
- The AI-led chat instructions (~1380 source-fit guidance, ~1903 patch rules)
  get one new rule: when the user's interest is anchored to a specific
  entity/place and they confirm it's a hard requirement, set
  `must_have_terms`; never invent anchors the user didn't confirm.
- **Alias expansion** — new async helper in `refinement.py` (or a small new
  module) `expand_must_have_aliases(profile) -> dict[str, list[str]]`:
  - Called at strategy-confirm time (wherever the confirmed profile is
    finalized before save — same hook where gmail rules are merged) and
    whenever `must_have_terms` or the foreign language plan changes.
  - Routes via `model_routing.client_for_agent("refinement", ...)`,
    `complete_json`, new prompt key `must_have_alias_expansion` (add to
    `config/prompts.yaml` AND `FALLBACK_PROMPTS` in `prompt_loader.py`).
    Prompt inputs: the anchor terms, statement/scope, and the language codes
    from `foreign_language_plan` + selected foreign-media languages. Output:
    `{"aliases": {"<term>": ["...", ...]}}` — official name variants,
    abbreviations, demonyms only where unambiguous, and native-language
    renderings per requested language. Explicitly forbid broader-category
    terms ("Mexico", "Latin America" are NOT aliases of "Mexico City").
  - Fails open to `{}` (gate then matches anchors verbatim only). Cache the
    result on the profile; don't re-call per build if terms/languages are
    unchanged.

## Phase 3 — Query-construction enforcement

`backend/agents/discovery/query_refiner.py`:
- `expand_search_strategy` and `refine_queries_for_adapter`: when
  `profile.must_have_terms` is non-empty, add the anchors to the prompt data
  with an instruction that every suggested query must contain one of the
  anchor terms or listed aliases. Then enforce deterministically: for each
  returned query, if no anchor/alias appears (accent-folded substring), append
  the first anchor to the query rather than discarding it. For
  `refine_queries_for_adapter` on `foreign_media`, prefer appending a
  native-language alias for that adapter's language when one exists.

## Phase 4 — Hard gate in the discovery runner

`backend/agents/discovery/runner.py`:
- New `_apply_must_have(profile, candidates) -> tuple[list[Candidate], list[dict]]`
  inserted at line ~207, right after `_apply_exclusions`:
  - No-op (`candidates, []`) when `must_have_terms` is empty.
  - Build the folded alias sets once: for each anchor, `{anchor} ∪ aliases`,
    each accent-folded + casefolded.
  - Apply exemptions from Design decision 5, then require every anchor set to
    hit the candidate's folded relevance text.
  - Rejection record matches the existing funnel shape (copy the
    `low_topic_overlap` record fields) with
    `excluded_by: ["must_have"]` and
    `reason: f"Missing required term(s): {', '.join(missed)}"`.
  - Fold its records into the `DiscoveryResult.exclusions` tuple (line ~285).
- Add a shared `_fold_text(value: str) -> str` helper (casefold + NFKD strip
  combining marks) and use it on both sides of the match.
- `screen_candidates` prompt data already carries `exclusions`; also pass
  `must_have_terms` so the LLM screen reinforces (but never replaces) the
  deterministic gate.

## Phase 5 — Frontend

`frontend/src/App.tsx` + `frontend/src/styles.css`:
- Add `must_have_terms?: string[]` and
  `must_have_aliases?: Record<string, string[]>` to the profile TS types
  (~lines 46–161) and the draft state (`must_have: string` text field, comma
  split via `splitList`, mirroring `exclusions` at ~410/1796/7364/7377).
- Plan preview + confirm view: render a "Must include" row exactly like the
  exclusions rows (~3434, ~3574), showing each anchor with its generated
  aliases in muted text (e.g. `Mexico City — also matching: CDMX, Ciudad de
  México, México DF`). Aliases are read-only display plus a per-alias remove
  affordance only if trivially cheap; otherwise editing the anchor list and
  regenerating is sufficient for v1.
- Funnel/diagnostics view: wherever `excluded_by` reasons are surfaced, no new
  work should be needed if it's generic; verify `must_have` rejections render
  with their reason string. If the brief's reporting summarizes exclusion
  counts by tag, add the new tag label ("Missing must-have term").

## Phase 6 — Tests (`backend/tests/test_must_have_gate.py`, NEW)

Cover at minimum:
1. Gate no-op when `must_have_terms` empty (object identity / zero exclusions).
2. AND-across-anchors, OR-within-aliases semantics.
3. Accent folding both directions ("México" text vs "mexico" alias and
   vice versa); phrase matching (no token-level false positives).
4. Foreign-media candidate kept via native-language alias, dropped without it.
5. Exemptions: requested/promoted source kept, markets kept, no-judgeable-text
   kept.
6. Funnel record shape: `excluded_by == ["must_have"]`, reason names the
   missed anchors, records land in `DiscoveryResult.exclusions`.
7. No low-yield loosening (gate result identical with `low_yield=True`).
8. `expand_search_strategy` post-check: anchor appended to a drifting
   expansion query; alias-bearing query left untouched.
9. `expand_must_have_aliases` fails open to `{}` when no client; result cached
   (no second model call when terms/languages unchanged).
10. Profile round-trip: `from_payload`/`to_payload`, `_coerce_profile`,
    fingerprint changes when terms change, API model accepts the new fields.

Also update any profile-fixture-asserting tests that enumerate profile keys
(grep tests for `priority_terms` to find them).

## Validate (must all pass)

```bash
uv run pytest backend/tests/
npm run build
npm run lint        # 0 warnings
```

## Commit & restart

Stage only the files you changed (no untracked artifacts), commit to `main`
with a `feat: optional must-have content rule with multilingual aliases`
message, push, then restart the production server — it runs under launchd and
serves `frontend/dist`, so an un-restarted server means you're testing stale
code:

```bash
launchctl kickstart -k gui/$(id -u)/com.morning-dispatch
# Do NOT touch the :8001 --reload dev server.
```

## Verify (preferred)

1. Refine a "potential holiday in Mexico City" interest with web search +
   foreign media (Spanish) selected. Confirm the chat offers the must-have
   option; set "Mexico City". The confirm view shows the anchor with
   generated aliases including CDMX / Ciudad de México.
2. Build the brief. Every included item mentions Mexico City or an alias;
   Spanish-language items match via native aliases.
3. Open the funnel/diagnostics for the run: off-anchor items (generic world
   travel) appear as exclusions with "Missing required term(s): mexico city".
4. Re-run the same interest with the must-have left empty and confirm
   discovery output is unchanged from current behavior.

Report back: commit SHA, push result, restart + health check, and the
verification-brief funnel numbers (kept vs `must_have` rejections per source).
