# Handoff: Apple Intelligence as a per-agent model provider

Status: PLAN (not implemented). Drafted 2026-06-10.

## Goal

Let the user toggle each routed agent (refinement, librarian, source_audit, editorial,
critic) between the existing oMLX-hosted model and the Apple Intelligence on-device
foundation model. Additive — oMLX remains the default and is never removed. Must work
on macOS 26.5 (current machine) and macOS 27 (in beta as of June 2026).

## Current state (verified in code)

- `backend/agents/model/client.py` — `ModelClient` speaks OpenAI-compatible HTTP
  (`/chat/completions`, streaming + non-streaming, `response_format: json_object`,
  `chat_template_kwargs`). Already carries `provider` and `api_mode` fields on
  `ModelClientConfig`, but both are effectively hardcoded (`"local"` / `"openai"`).
  Single endpoint from `settings.model_base_url` (oMLX, `:1234/v1`).
- `backend/app/services/model_routing.py` — per-agent routes persisted in
  `model_settings.json` as `{provider, model}`; `provider` is forced to `"local"` in
  `normalized_routes()` and `save_routes()`. `RouteResolution` already has
  `fallback_configured` / `unavailable_reason` fields we can reuse.
- `backend/app/services/model_catalog.py` — lists oMLX models; response shape already
  has a `providers` map, currently only `local`.
- Frontend `frontend/src/App.tsx` — routes UI reads `routing.routes[agent].provider`
  and posts to `/api/admin/model/routes`; provider is currently always `local`.
- Server runs as a **user LaunchAgent** (`~/Library/LaunchAgents/com.morning-dispatch.plist`)
  — i.e. inside a logged-in user session. This is a prerequisite for Apple Intelligence
  access, so the topology is already compatible.
- Route fan-out warning: the `refinement` route is used by far more than the chat —
  `query_refiner`, `reddit_expander`, `foreign_media`, `podcast`/`podcast_agent`, and
  `explore` all resolve `client_for_agent("refinement")`. Toggling refinement to Apple
  switches all of those.
- Output budgets per call site today: librarian enrichment 220, discovery 360–600,
  source_audit 900–1600, critic 1400, editorial 1600, podcast 2000, query_refiner
  expansion 2000, foreign translation 2600.

## External facts that shape the design

- **macOS 26.x**: Foundation Models is a Swift-only framework (`LanguageModelSession`,
  `@Generable`, `DynamicGenerationSchema`). No HTTP API, no Python bindings, not
  ObjC-bridged (PyObjC won't work). One fixed on-device model (~3B params).
  **Hard 4,096-token context window — instructions + prompt + response combined.**
  26.4 added APIs to inspect context size and count tokens.
- **macOS 27** (WWDC June 2026, beta): new on-device model with larger context,
  `LanguageModel` protocol (pluggable providers incl. `MLXLanguageModel`), an `fm`
  CLI, a Python SDK, multimodal input, Private Cloud Compute server model (32K ctx).
  None of this exists on 26.5, so it cannot be the common code path.
- Apple Intelligence preconditions: Apple Silicon, Apple Intelligence enabled in
  System Settings, model assets downloaded, logged-in user session.
  `SystemLanguageModel.default.availability` reports the reason when unavailable.
- Per Apple DTS: foreground GUI apps are not rate-limited; background processes /
  CLI tools **are throttled**. Community bridges (e.g. gety-ai/apple-on-device-openai)
  package the server inside a GUI app for this reason.
- Guardrails: the model raises `guardrailViolation` on sensitive content. News
  pipelines (politics, violence, war) trip this in practice.

## Architecture decision

**Build a small vendored Swift sidecar ("apple-model-bridge") that exposes the
on-device model behind the OpenAI-compatible API the app already speaks.** The Python
backend then treats Apple Intelligence as just a second `base_url` + provider id.
This is the only design that works on both 26.5 and 27 (the Python SDK / `fm` CLI are
27-only), and it keeps `ModelClient` changes minimal.

Alternative considered: adopt gety-ai/apple-on-device-openai as-is. Faster to start,
but we want `/health` with availability reasons, guardrail→status mapping, context
reporting, and JSON-schema→`DynamicGenerationSchema` mapping — worth owning ~300–500
lines of Swift. (Borrowing its snapshot→delta streaming approach is fine.)

### Sidecar spec (`native/apple-model-bridge/`, Swift Package + thin app shell)

- Endpoints: `POST /v1/chat/completions` (stream + non-stream), `GET /v1/models`
  (one entry, e.g. `apple-on-device`), `GET /health` →
  `{available, reason, os_version, context_window, languages}`.
  Bind 127.0.0.1 only, fixed port (e.g. `:11535`).
- Map `messages[role=system]` → session `instructions`; concatenate user turns or
  re-create session per request (stateless server; the Python side owns context).
- `temperature`, `max_tokens` → `GenerationOptions(temperature:, maximumResponseTokens:)`.
- `response_format json_object` → strengthen instructions + rely on the existing
  Python-side JSON repair (`_parse_json_object`). Phase 2: accept
  `response_format: {type: json_schema, …}` and map to `DynamicGenerationSchema` for
  guaranteed structure (an upgrade over oMLX behavior).
- Streaming: Foundation Models streams cumulative snapshots — diff against previous
  snapshot and emit OpenAI-style SSE deltas (refinement chat depends on this).
- Error mapping: `exceededContextWindowSize` → HTTP 507 (the app already maps 507 to
  `MODEL_CAPACITY_STATUS`); `guardrailViolation` → 422 with a distinct error code;
  rate limit → 429; model unavailable → 503 with the availability reason.
- Report the **actual** context window from the 26.4+ inspection API (don't hardcode
  4096 — macOS 27's model is larger).
- Package as an `LSUIElement` app bundle (no Dock icon) to mitigate background
  throttling; install as a second user LaunchAgent (`com.morning-dispatch.apple-bridge`).
  Build via `swiftc`/SPM in `scripts/` next to the existing build pipeline; min
  deployment target macOS 26, `#available(macOS 27, *)` guards for 27-only features.

### Backend changes

1. `config.py`: add `apple_model_base_url` (default `http://127.0.0.1:11535/v1`) and
   an `apple_model_enabled` env override. No API key (provider is keyless — don't gate
   on `model_api_key`).
2. `model_routing.py`:
   - `normalized_routes()` / `save_routes()`: accept `provider ∈ {"local", "apple"}`,
     validate, persist. Unknown providers fall back to `local`.
   - `client_for_agent()`: when route provider is `apple`, build a `ModelClient` with
     the apple base_url, `provider="apple"`, fixed model id, no api_key.
   - `routes_status()`: add the `apple` provider entry with live availability (probe
     sidecar `/health`, short timeout, cached ~30s) and the unavailability reason.
   - Optional but recommended: `fallback_to_local` flag per route — on 503/507/guardrail
     from Apple, retry once on the oMLX route and surface `fallback_configured=True`.
3. `client.py`:
   - When `provider == "apple"`: omit `chat_template_kwargs` (oMLX-ism), keep
     `response_format` per sidecar support, and **pre-flight the context budget**:
     `estimate_tokens(system, prompt) + max_tokens` vs the advertised context window;
     raise `ModelClientError(status=MODEL_CAPACITY_STATUS)` before sending so callers
     hit their existing capacity-handling paths.
   - Add `guardrail` to the non-retryable status set.
4. `model_catalog.py`: add `providers.apple` (available flag, single model, reason).
5. Reporting/metrics: tag `route_name` metrics with provider so the inference metrics
   table distinguishes oMLX vs Apple latency/tokens.

### Per-agent context adaptation (the real work)

A 4,096-token total budget on 26.5 means a toggle alone is not enough — several agents
must shrink their prompts when the resolved provider is Apple. Add a
`client.context_window` (None for oMLX = unlimited-ish) and have batch-building call
sites consult it:

- **librarian** (220 out, per-item prompts): fits today. Best first agent to ship.
- **source_audit** (900–1600 out, batched items): reduce batch size so
  prompt + max_tokens fits; reuse the `librarian_model_max_items` pattern.
- **editorial** (1600 out, ranks the complete candidate set): full candidate prompts
  will exceed 4K routinely. Needs chunked map-reduce ranking (rank in groups, then
  rank the group winners) or hard candidate truncation. Largest change; consider
  shipping the toggle for editorial only with a UI warning until this lands.
- **critic** (1400 out, whole brief): trim item summaries to fit; degrade gracefully.
- **refinement** (chat + heavy fan-out): conversation history must be condensed to fit;
  discovery callers (query_refiner 2000-token expansion, podcast 2000) need reduced
  budgets. Consider splitting the route fan-out (e.g. a separate "discovery" route) so
  toggling chat to Apple doesn't drag podcast digestion with it — scope decision.
- **foreign translation** (2600 out): 4K total leaves ~1.4K for input — articles don't
  fit, and Apple's language coverage is limited. Recommend: translation paths ignore
  the Apple toggle and always use oMLX (documented in UI), unless on macOS 27 where
  the larger context may make chunked translation viable.

### Frontend changes (App.tsx models section)

- Per-agent provider segmented control: `oMLX | Apple Intelligence`.
- Apple option disabled with reason chip when unavailable (sidecar down, Apple
  Intelligence off, OS < 26, model downloading).
- When provider=apple: hide the model dropdown (single fixed model), show context
  window, and show a warning badge on editorial/critic/refinement ("small context —
  batching reduced") and on translation ("stays on oMLX").
- Optional per-agent "fall back to oMLX on failure" checkbox wired to the route flag.

### macOS 26.5 vs 27 handling

- One sidecar binary, min target macOS 26; feature-detect at runtime. `/health`
  reports `os_version` + `context_window`, the backend adapts budgets from that —
  no version branching in Python beyond what `/health` reports.
- macOS 27 is beta until ~September 2026: expect API churn; CI/build on this machine
  (26.5) stays the source of truth. Re-test on 27 RC.
- Later options once 27 ships (out of scope now): Apple's Python SDK could replace the
  sidecar; `fm` CLI for quick smoke tests; Private Cloud Compute model (32K ctx,
  keyless) as a third provider — note the app deliberately removed cloud routing for
  privacy, PCC is E2E-attested but still off-device, so that's a user decision.

## Limitations & risks to acknowledge up front

1. **No Apple-supported non-Swift API on 26.5** — the Swift sidecar is a new native
   build artifact (Xcode CLT, signing, second LaunchAgent). Biggest structural change.
2. **4,096-token context on 26.5** dominates the design; editorial/refinement need
   real restructuring, not just routing. Quality will also drop: it's a ~3B model —
   noticeably weaker than oMLX-hosted models at ranking/critique. Per-agent toggling
   is exactly the right mitigation, but expect to keep editorial/critic on oMLX.
3. **Background throttling**: Apple rate-limits Foundation Models for non-foreground
   processes. The LSUIElement app-bundle approach mitigates but is not contractual;
   pipeline-scale librarian enrichment may hit 429s. The existing retry + fallback
   machinery must absorb this.
4. **Guardrail refusals on news content** (politics/violence) will cause item-level
   failures; map to a distinct status and skip-or-fallback rather than failing briefs.
5. **Availability is environmental**: Apple Intelligence toggle, model download state,
   and a logged-in session (headless reboot → unavailable) all gate the provider;
   the UI must show why, and routes must degrade to oMLX cleanly.
6. **Limited language support** → foreign-media lane and translation are poor fits.
7. **macOS 27 churn** while in beta; don't build against 27-only APIs as the base path.

## Suggested milestones

1. Sidecar v1 (non-stream + stream, /health, error mapping) + manual curl tests.
2. Backend provider plumbing + catalog/availability + tests
   (`test_model_routing.py`, `test_model_client.py`, fake sidecar via httpx mock).
3. Frontend toggle + availability UI.
4. Ship toggle for **librarian + source_audit** (fits budget) with fallback enabled.
5. Context adaptation for refinement/critic; editorial map-reduce ranking last.
6. macOS 27 beta validation pass; revisit Python SDK/PCC after 27 GA.

## References

- Foundation Models docs: https://developer.apple.com/documentation/FoundationModels
- WWDC26 "What's new in Foundation Models": https://developer.apple.com/videos/play/wwdc2026/241/
- 4,096-token limit + 26.4 context APIs: https://www.infoq.com/news/2026/03/apple-foundation-models-context/
- OpenAI-compatible bridge prior art: https://github.com/gety-ai/apple-on-device-openai
