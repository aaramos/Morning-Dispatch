# Handoff: Fix Must-Have Anchor Pollution (Synonyms Stored as AND-ed Terms)

You are fixing a bug in `/Users/macstudio/Apps/personal_intel` on branch
`main`, in the must-have content rule landed in commit `072c552`
(`feat: enforce must-have anchors in discovery strategy`). Read this document
end to end before writing code.

## The incident (diagnosed — do not re-diagnose)

Exploration `25c73604-30c2-4685-a8a6-f64ee7783e70` (topic
`5251173f-34a9-4bea-a61a-b18984a31c14`, "solo trip to Mexico City in August")
returned a nearly empty brief: Web, Foreign Media, Reddit, and Podcast
sections all empty. The funnel report
(`GET /api/explore/explorations/<id>/report`) shows **144 of 155 candidates
rejected by the `must_have` gate**, and the rejection reasons prove most were
false positives:

- 82 rejected with `Missing required term(s): CDMX, Ciudad de México` —
  i.e. the item **did** say "Mexico City" but was killed anyway.
- 24 rejected missing only `Ciudad de México` (item said Mexico City AND CDMX).
- 1 rejected missing only `CDMX`.
- 37 rejected missing all three — these were the genuinely off-anchor items
  (world-travel drift) the gate exists to remove.

So 107/144 rejections were wrong. The saved profile shows why:

```json
"must_have_terms":   ["Mexico City", "CDMX", "Ciudad de México"],
"must_have_aliases": {"mexico city": ["cdmx", "ciudad de méxico"]}
```

The gate semantics are **correct per spec** (AND across `must_have_terms`,
OR within each term's alias set — see `_apply_must_have` /
`_must_have_alias_sets` in `backend/agents/discovery/runner.py` ~line 546).
The data is wrong: the synonyms were stored **both** as aliases of
"mexico city" (correct) **and** as two additional independent anchors in
`must_have_terms` (wrong), which converts OR-synonyms into AND-requirements.
Almost no article spells out all three names, so the gate emptied the brief.

## Root cause

The capture/normalization layer has no guard against synonyms entering
`must_have_terms`:

1. The refinement-chat profile_patch schema (`config/prompts.yaml` ~lines
   45–46, rule at ~line 69) lets the model emit `must_have_terms` freely and
   never says "each term must be a DISTINCT concept; synonyms/translations
   belong in must_have_aliases." A model asked to anchor "Mexico City"
   naturally lists the synonyms as terms too.
2. `_clean_must_have_aliases(value, terms=...)` and the `_string_list`
   normalization sites in `backend/app/services/refinement.py` (lines ~1406,
   ~1879, ~1981, ~2378, ~4406, ~4482) clean the two fields **independently**
   — nothing cross-checks terms against alias sets, even when the alias dict
   itself (as here) declares term B to be an alias of term A.
3. The chat patch merge (~line 2521, `_merge_string_lists`) accumulates terms
   across turns, so a later turn mentioning "CDMX" appends it as a new anchor.
4. The frontend draft round-trips `must_have_terms` as one comma-joined text
   field (`frontend/src/App.tsx` ~7508 join, ~1800 split) — a user typing
   "Mexico City, CDMX" means OR in their head but creates AND anchors.
5. `_must_have_alias_sets` in the runner trusts the stored terms verbatim, so
   already-saved poisoned profiles (this one is still in the DB) stay broken.

Multi-anchor AND itself remains valid and must keep working (e.g. "Tesla" +
"battery" are two genuinely distinct required concepts). The fix is
canonicalization of synonyms, NOT weakening AND to OR.

## Fix design (settled)

**Core principle: a term that is a known alias of another term is folded into
that term's alias set, never kept as a separate anchor.** Implement this as
one pure helper and apply it at every boundary, with the runner as the last
line of defense so existing poisoned profiles heal at load time without a
data migration.

### Phase 1 — Canonicalization helper (the guarantee)

In `backend/app/services/refinement.py`, add:

```python
def _canonicalize_must_have(terms, aliases) -> tuple[list[str], dict[str, list[str]]]
```

Behavior (operate on accent-folded + casefolded forms for comparison, but
preserve original display casing of survivors):

1. Drop empty/duplicate terms (folded comparison).
2. For each term T (in list order), if folded(T) appears in the alias set of
   any **earlier surviving** term A (per the aliases dict, folded), remove T
   from terms and merge T plus T's own alias list (if it has a dict entry)
   into A's alias set.
3. Symmetric pollution: if a **later** term's alias set contains an earlier
   term, same merge — first-listed term wins as the canonical anchor.
4. Re-key the aliases dict so every key is the casefolded form of a surviving
   term; drop orphan keys (alias entries for terms that no longer exist) by
   merging them into the term that absorbed them.
5. Pure, deterministic, no model calls. With the incident profile as input it
   must return exactly
   `(["Mexico City"], {"mexico city": ["cdmx", "ciudad de méxico"]})`.

Note: the accent-folding helper already exists in `runner.py` (`_fold_text`).
Either import it from a shared location or move it somewhere import-safe for
both modules (`backend/agents/discovery/types.py` or a small util) — do not
duplicate the implementation.

Apply the helper at:
- `_coerce_profile` (~line 4406) — this covers the finalize path and
  `explore.save_topic_profile` callers that pass through it.
- `_fill_defaults` output (~line 4482).
- The chat patch merge (~lines 2521–2528) — canonicalize after merging both
  fields, so a later-turn "CDMX" patch folds into the existing anchor.
- The must_have question-answer apply paths (~lines 3390–3404 and
  3552–3567).
- `expand_must_have_aliases` (~line 705): canonicalize terms+aliases **after**
  the model returns (the freshly generated aliases are exactly the synonym
  knowledge needed to detect that two input terms are the same concept), and
  return/store both canonical terms and aliases — adjust the call site at
  ~line 661 (`patched["must_have_aliases"] = await expand_must_have_aliases(patched)`)
  so it can also update `patched["must_have_terms"]`. Keep fail-open: if the
  model is unavailable, canonicalize with whatever aliases already exist.

### Phase 2 — Runner defense-in-depth (heals stored profiles)

`backend/agents/discovery/runner.py`, `_must_have_alias_sets` (~line 580):
while building alias sets in term order, if a term's folded form is already
present in a previously built set's aliases, merge its aliases into that set
instead of appending a new `(anchor, aliases)` tuple. This makes the
already-saved Mexico City profile produce ONE anchor set
`{mexico city, cdmx, ciudad de mexico}` at the next build with **no DB
migration and no profile edit required**. Keep genuine multi-anchor profiles
(disjoint sets) AND-ed exactly as today.

### Phase 3 — Capture hardening (prompts)

`config/prompts.yaml`:
- Chat profile_patch schema (~lines 45–46) and rule (~line 69): add — every
  entry in `must_have_terms` is a DISTINCT required concept (logical AND);
  synonyms, abbreviations, translations, and alternate spellings must go in
  `must_have_aliases` under the canonical term, never as additional terms.
  Include the wrong/right Mexico City example verbatim:
  wrong `["Mexico City", "CDMX"]`, right
  `["Mexico City"]` + `{"mexico city": ["CDMX", "Ciudad de México"]}`.
- `must_have_alias_expansion` prompt (~line 333): add an instruction that if
  any input terms are synonyms of each other, group them — return aliases
  keyed only by the canonical term and list the synonym terms as its aliases.
  (Phase 1's post-call canonicalization consumes this.)
- The single-question parse prompt in `refinement.py` (~lines 3653–3656)
  already shows a good example; add the same explicit DISTINCT-concepts rule.

Mirror any of these prompt blocks that exist in
`backend/app/core/prompt_loader.py` `FALLBACK_PROMPTS` — keep yaml and
fallback in sync.

### Phase 4 — Frontend clarity

`frontend/src/App.tsx`:
- The must-have draft field (~line 3685): set helper text — "Every item must
  mention ALL of these. Add one entry per concept; synonyms and translations
  are matched automatically." (The comma-split semantics stay, but the user
  now knows commas mean AND.)
- The confirm/preview chips (~3355, ~3506) already render aliases per term;
  after the backend fix they will show one anchor with its aliases. No logic
  change expected — verify rendering with the canonicalized shape.

### Phase 5 — Funnel guardrail (visibility, small)

In `DiscoveryRunner.run`, after `_apply_must_have`: if the gate rejected more
than 60% of non-exempt gated candidates AND rejected ≥ 20 items, emit a
`logger.warning` with per-anchor miss counts, and surface a one-line note
into the run's diagnostics/reporting (follow how other discovery-stage
notes reach the funnel report) so the next over-aggressive gate is visible in
the UI instead of silently emptying a brief.

### Phase 6 — Remediate the incident profile

After the code fix, normalize the stored topic profile
`5251173f-34a9-4bea-a61a-b18984a31c14` (DB:
`runtime/data/db/morning_dispatch.sqlite3`, table `topic_profiles`,
`profile_json`): re-save it through `explore.save_topic_profile` (which now
canonicalizes) — a tiny one-off script or a `uv run python -c` is fine.
Phase 2 makes this non-blocking, but the stored data should still be clean.

## Tests

Extend `backend/tests/test_must_have_gate.py` (and refinement tests where the
helpers live):

1. `_canonicalize_must_have` golden case: the exact incident data → one term,
   merged aliases (assert display casing preserved).
2. Distinct multi-anchor preserved: `["Tesla", "battery"]` with disjoint
   aliases stays two AND-ed anchors.
3. Accent/case folding: term `"ciudad de mexico"` vs alias
   `"Ciudad de México"` recognized as the same.
4. Later-term-absorbs-earlier symmetry (alias entry keyed by the later term).
5. Chat patch merge: profile with anchor "Mexico City" + later patch adding
   term "CDMX" → still one anchor after merge.
6. `expand_must_have_aliases` result canonicalizes terms (mock client
   returning synonym groupings); fail-open path leaves terms canonicalized
   against existing aliases only.
7. Runner regression: build a `TopicProfile` from the **verbatim poisoned
   payload** (terms = three names, aliases keyed "mexico city") and assert a
   candidate whose text contains only "Mexico City" is KEPT and a
   world-travel candidate with none of the names is still dropped with
   `excluded_by == ["must_have"]`.
8. Funnel guardrail fires (warning + diagnostics note) on a synthetic
   80%-rejection run and stays silent on a normal run.

## Validate (must all pass)

```bash
uv run pytest backend/tests/
npm run build
npm run lint        # 0 warnings
```

## Commit & restart

Stage only files you changed; commit to `main`
(`fix: canonicalize must-have anchors so synonyms OR instead of AND`), push,
then restart production — launchd serves `frontend/dist`, an un-restarted
server tests stale code:

```bash
launchctl kickstart -k gui/$(id -u)/com.morning-dispatch
# Do NOT touch the :8001 --reload dev server.
```

## Verify (preferred)

1. Rebuild the Mexico City exploration (POST
   `/api/explore/explorations/<id>/rebuild` or via the UI). The brief's Web /
   Foreign / Reddit sections should populate again; funnel report should show
   `must_have` rejections only for genuinely off-anchor items (the ~37
   world-travel class), with reasons naming a single anchor.
2. In the strategy preview, the must-have row shows one anchor ("Mexico
   City") with CDMX / Ciudad de México listed as matched aliases.
3. Regression: a brief with must-have left empty is unchanged, and a true
   two-concept profile (e.g. "Tesla" + "battery") still ANDs.

Report back: commit SHA, restart + health check, and before/after funnel
numbers for the rebuilt Mexico City brief (kept vs `must_have` rejections per
source).
