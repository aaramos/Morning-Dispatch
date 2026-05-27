# Morning Dispatch Backlog

## Recommended Pre-PRD Build Sequence

**Status:** Backlog planning  
**Source:** `/Users/macstudio/Downloads/morning-dispatch-backlog-pre-prd.md`  
**Context:** Personal-scale deployment for one primary user and 1-2 friends, self-hosted through Tailscale, local-first by default. Licensing, multi-tenancy, billing, and SaaS compliance are out of scope for this backlog pass.

1. Refinement Agent: propose-then-correct.
2. Per-agent model routing across local and cloud models.
3. Foreign media and translation.
4. Finance data expansion, starting with OpenDART/EDGAR-style disclosure.
5. Foreign-market data sources, including shared symbology and disclosure registries.

---

## Later: Digest Onboarding and Preference Setup

**Status:** Backlog  
**Priority:** After the single-digest pipeline is stable  
**Goal:** Let a user create and tune one or more digests without manual database/source seeding.

### User Problem

Morning Dispatch currently works from manually configured digest interests, Gmail senders, Reddit sources, and feedback signals. There is no guided setup flow that asks what the user wants to track, which sources to trust, or what should be ignored.

### Desired Outcome

A non-technical user can open the app, create a digest, describe their interests in plain English, connect/select sources, review the proposed configuration, and let feedback refine future ranking.

### Scope

- Guided digest creation flow for interest text, preferred topics, and excluded topics.
- Source setup for Gmail newsletters and Reddit communities.
- Review screen showing what the app thinks the digest should track.
- Support for multiple independent digests with their own interests, sources, thresholds, and feedback.
- Preference profile updates from `Useful` / `Not useful` feedback.

### Not in This Item

- Multi-user cloud accounts.
- Billing, sharing, or public deployment.
- Replacing the current local-first pipeline.

### Acceptance Criteria

- A new digest can be created without editing seed data or SQLite directly.
- The user can see and edit interests and sources before the first run.
- Each digest keeps separate source lists, feedback, and ranking behavior.
- Existing single-digest behavior keeps working.

---

## Backlog: Refinement Agent Propose-Then-Correct

**Status:** Backlog  
**Priority:** First recommended pre-PRD item  
**Goal:** Replace the form-like refinement loop with a draft-first experience where the AI proposes a complete brief strategy, then asks only the questions that materially improve it.

### User Problem

The current Refinement Agent can still feel procedural. It asks questions before showing enough value, which makes the user do too much upfront work and spends model calls before producing a useful strategy.

### Desired Outcome

When the user submits an interest, the app immediately produces a complete draft topic profile: scope, time window, sources, exclusions, search queries, inferred entities, and tickers where relevant. The user reviews and corrects the strawman instead of answering a questionnaire.

### Release Plan

1. Draft-first profile generation
   - Generate a complete topic profile from the raw request in one pass.
   - Include scope, source scope, depth, selected sources, exclusions, requested sources, inferred entities/tickers, search queries, and source-specific queries.
   - Preserve the "just go now" path.

2. Confidence-gated questions
   - Add an internal confidence signal per profile field.
   - Ask questions only for low-confidence fields.
   - Default to surfacing no more than 3 questions, while keeping the hard max of 10 for edge cases.

3. Editable strawman UI
   - Render the draft profile as an editable confirmation card.
   - Let the user accept, edit any field inline, answer surfaced questions, or build immediately.
   - Avoid full re-refinement when a user edits one field.

### Not in This Item

- Downstream digest-core changes.
- Multi-user profile sharing.
- Replacing source adapters.

### Acceptance Criteria

- Given the Micron/Hynix/Kioxia/SanDisk investor prompt, the agent returns a populated profile with resolved tickers, the 3-day window, excluded source types, and zero or one surfaced question.
- A well-specified request can reach build with a single confirmation click.
- Editing a strawman field updates that field without restarting the entire refinement session.
- The confirmation card clearly shows inferred assumptions so wrong inferences can be corrected.

### Open Questions

- Should confidence be self-reported by the model, derived heuristically, or both?
- Is the surfaced-question cap exactly 3 by default?
- Should accepted strawmen become reusable templates for scheduled digests?

### Effort / Risk

Medium effort, low risk. The main uncertainty is how to make confidence useful without trusting the model blindly.

---

## Backlog: Audit, Editorial, and Critic Instrumentation

**Status:** Backlog  
**Priority:** Parallel with refinement improvements  
**Goal:** Measure what Source Audit, Editorial, and Critic each change so we can decide whether to keep all three, merge them, or gate later passes.

### User Problem

The app now has three sequential judgment passes: Source Audit, Editorial, and Critic. Each can drop, demote, or reorder items. This may improve quality, but it also adds latency, model spend, and possible inconsistency.

### Desired Outcome

For any completed brief, Admin can explain which agent changed what. Across multiple briefs, the app can show whether each pass materially affects the final result.

### Release Plan

1. Per-pass delta logging
   - Log the candidate set entering and leaving Source Audit, Editorial, and Critic.
   - Attribute every drop, demote, include-as-context, lead change, and reorder to the responsible agent.

2. Per-brief summary
   - Show how many final-ranking changes each pass caused.
   - Answer questions like: "What did Critic change that Editorial had not already handled?"

3. Aggregate reporting
   - Summarize pass impact across recent briefs.
   - Identify low-impact passes that could be merged or conditionally skipped.

### Not in This Item

- Removing or merging agents.
- Changing model prompts based on the findings.

### Acceptance Criteria

- For a completed brief, Admin can show Source Audit, Editorial, and Critic deltas separately.
- A report can identify whether Critic, Source Audit, or Editorial is consistently low-impact.
- The app captures enough evidence to support a later decision memo on whether to merge Source Audit and Critic or gate Critic behind audit conflict flags.

### Open Questions

- What threshold counts as "low-impact"?
- Should this instrumentation become permanent telemetry or a temporary study?

### Effort / Risk

Low effort, low risk. This is mostly observability.

---

## Backlog: Per-Agent Model Routing Across Local and Cloud

**Status:** Backlog  
**Priority:** After refinement instrumentation, before heavier translation workflows  
**Goal:** Let each model-backed agent use the best model for its job while preserving the app's local-first privacy posture.

### User Problem

Model selection is currently too global. The Refinement Agent, Librarian, Source Audit, Editorial, and Critic do different jobs. High-volume enrichment should usually be cheap/local, while judgment-heavy Editorial or Source Audit may benefit from stronger cloud models.

### Desired Outcome

Admin can configure model routing per agent. Public-source work can use cloud models when configured, while private-source content from Gmail or Collections stays local unless the user explicitly chooses otherwise.

### Release Plan

1. Routing table
   - Add a model route per model-backed agent: Refinement, Librarian, Source Audit, Editorial, and Critic.
   - Keep deterministic components model-free.
   - Add a default local fallback model.

2. Cloud provider configuration
   - Support OpenAI-compatible endpoints and provider-specific keys through the existing owner-only secrets directory.
   - Treat Ollama Cloud as a candidate provider through either local Ollama proxy or direct Ollama API support.
   - Never expose raw keys in UI or API responses.

3. Source-aware privacy rule
   - Content from private sources such as Gmail and Collections must use a local model by default.
   - Public web, Reddit, YouTube, podcast, and market-source items may use cloud routes when configured.
   - Define behavior for mixed-source passes before enabling cloud routing for whole-brief agents.

4. Fallback behavior
   - If a cloud model is unavailable, missing a key, times out, or fails schema validation, fall back to the configured local model.
   - Continue building the brief and show the fallback in telemetry/Admin.

### Not in This Item

- Fine-tuning.
- Full cost accounting beyond basic per-agent token counts.
- A hosted multi-user secrets system.

### Acceptance Criteria

- Admin can set Editorial to a cloud model while Librarian stays local.
- A brief uses the configured route for each model-backed agent.
- A Gmail-sourced item never sends content to a cloud endpoint under default privacy rules, verifiable in logs.
- Removing a cloud key causes graceful local fallback with no brief failure.
- Model metrics are grouped by agent route.

### Open Questions

- Should one private item force a whole mixed-source Editorial pass local, or should public/private items be split?
- Which cloud providers/models are supported first?
- Do we need a per-brief "stay fully local" toggle?
- How should translated public items be routed?

### Effort / Risk

Medium effort, medium risk. The privacy rule is the hard part and should be treated as product behavior, not just configuration.

---

## Backlog: Foreign Media and Translation

**Status:** Backlog  
**Priority:** After per-agent routing decisions  
**Goal:** Discover and use foreign-language coverage as first-class brief material, especially for semiconductor, market, and international-company monitoring.

### User Problem

English-only discovery misses differentiated coverage from sources such as Korean business press, Japanese market coverage, Chinese/Taiwanese semiconductor reporting, Nikkei, and DigiTimes. For some investor briefs, the best signal is not in English.

### Desired Outcome

The app can discover foreign-language candidates, translate the title and summary for ranking, preserve the original text, and render translated items with clear provenance.

### Release Plan

1. Language detection
   - Detect language during enrichment.
   - Flag non-English candidates.
   - Pass language metadata to Source Audit and Editorial.

2. Translation pipeline
   - Translate title and summary eagerly for ranking.
   - Defer full-body translation until an item survives into the brief.
   - Retain original-language text alongside translated text.

3. Brief rendering
   - Show a language badge such as "Translated from Korean."
   - Expose both translated and original text.
   - Include translator/model provenance.

4. Audit awareness
   - Let Source Audit know when a candidate was translated.
   - Include translation confidence or warning flags where available.

### Not in This Item

- Licensed-news adapters.
- Full-document translation for every candidate.
- Cloud translation of private Gmail or Collections content unless explicitly allowed by routing policy.

### Acceptance Criteria

- A Korean-language Hynix article can be discovered, ranked on translated metadata, and rendered with both original and translated text.
- Dropped candidates do not incur full-body translation cost.
- Source Audit can see translated status and factor it into fit scoring.
- Translation failures soft-fail without breaking the brief.

### Open Questions

- Should translation use the routed local/cloud model or a dedicated translation service?
- Initial target languages: Korean, Japanese, Chinese?
- How should low translation confidence be shown?
- How should paywalls and fetch failures from foreign outlets degrade?

### Effort / Risk

Medium effort, medium risk. High product upside for investor and competitive intelligence use cases.

---

## Backlog: Finance Data Expansion

**Status:** Backlog  
**Priority:** After Markets Simple Mode hardens  
**Goal:** Add disclosure and event data as first-class finance sources so investor briefs can surface actual catalysts, not just articles and price snapshots.

### User Problem

The current Markets source provides useful simple snapshots, but investor workflows need filings, disclosure events, earnings timing, and company-specific catalysts.

### Desired Outcome

Investor briefs can include recent filings and market events as normalized candidates. A filing-driven catalyst can become the lead story when it is the highest-signal item.

### Release Plan

1. SEC EDGAR filings adapter
   - Resolve tracked US companies to CIK identifiers.
   - Fetch recent 8-K, 10-Q, and 10-K filings inside the brief time window.
   - Prioritize catalyst-type filings such as 8-K and earnings-related filings.
   - Emit filing candidates with form type, filing date, link, source, and summary.

2. Filing card schema
   - Define one shared filing-card type for EDGAR and future foreign disclosure adapters.
   - Include company, identifier, form type, date, link, summary, language, and source registry.

3. Earnings/event expansion
   - Add an earnings calendar source.
   - Add consensus estimate or event flags when a free source is chosen.
   - Add corporate-actions/events feed later if a reliable source is selected.

### Not in This Item

- Paid market-data vendors.
- Deep fundamentals modeling.
- Buy/sell recommendations.

### Acceptance Criteria

- A US-company brief surfaces a recent 8-K or 10-Q as a distinct filing card.
- Filings respect the same time-window constraints as web/news candidates.
- A filing can become the lead item when Source Audit and Editorial judge it to be the strongest catalyst.
- EDGAR failures degrade gracefully and are visible in Admin/source issues.

### Open Questions

- Should company-to-CIK resolution use a maintained map, EDGAR search, or both?
- Should long filings go directly to Librarian, or should we extract sections first?
- Which earnings-calendar data source should be used first?
- How should this share infrastructure with foreign-market symbology?

### Effort / Risk

Phase A is medium effort and low risk because EDGAR is free and stable. Later phases depend on data-source choice.

---

## Backlog: Foreign-Market Data Sources and Symbology Resolver

**Status:** Backlog  
**Priority:** Build resolver early because it unblocks finance expansion and foreign disclosures  
**Goal:** Support foreign equities through a shared name-to-identifier resolver, price fallbacks, and foreign disclosure adapters.

### User Problem

Tracking foreign equities requires exchange-aware symbols, price data across vendors, and access to foreign primary disclosure. Today the app can infer some Yahoo Finance tickers, but it does not have a shared resolver or foreign filing adapters.

### Desired Outcome

The app can resolve a company such as Hynix once, then use the correct symbol or identifier for each downstream system: Yahoo Finance, Stooq, OpenDART, EDINET, TDnet, MOPS, or EDGAR.

### Release Plan

1. Shared symbology resolver
   - Resolve company name to canonical identifier where available.
   - Store per-vendor symbols such as Yahoo, Stooq, OpenDART, EDGAR CIK, EDINET, TDnet, and MOPS.
   - Let Markets, EDGAR, and foreign disclosure adapters consume the same resolver.

2. Foreign price data
   - Use yfinance as the primary free source.
   - Add Stooq as a fallback for thin or failing exchange coverage.
   - Soft-fail when both sources miss.

3. Foreign disclosure adapters
   - Add OpenDART first for Korean companies.
   - Use the OpenDART adapter as the contract model for EDINET/TDnet in Japan and MOPS in Taiwan.
   - Emit foreign filings as normalized filing candidates.
   - Route foreign-language filings through the translation pipeline.

### Not in This Item

- Paid price vendors.
- SaaS-safe licensing review.
- Full global exchange coverage.

### Acceptance Criteria

- "Hynix" resolves to both its Yahoo price symbol and its OpenDART identifier through one resolver call.
- A foreign ticker returns a price snapshot through yfinance, and a deliberately thin case falls back to Stooq without failing the brief.
- An OpenDART filing appears as a translated filing card with original Korean text preserved.
- The OpenDART adapter contract is documented clearly enough to spec EDINET against it.

### Open Questions

- Is the symbology source of truth a maintained file, an ISIN lookup service, or accreted on demand?
- Initial exchange scope: KRX, TSE, TWSE, and US?
- How much OpenDART metadata should be ingested versus linked?
- How should US and foreign filings share one card schema?

### Effort / Risk

Resolver is medium effort and foundational. yfinance/Stooq fallback is low effort. OpenDART is medium effort because of auth and format handling.

---

## Cross-Cutting Backlog Decisions

**Status:** Backlog / requirements gathering  
**Goal:** Capture decisions that affect multiple backlog items before implementation starts.

### Decisions Needed

- Shared symbology service: one resolver for EDGAR, OpenDART, prices, and tickers, or separate systems?
- Unified filing card: define once for EDGAR and foreign disclosure.
- Translator selection: routed local/cloud model or dedicated translation service?
- Mixed-source routing: how to handle a single brief containing private, public, and translated content.
- Telemetry scope: one-off study for Audit/Editorial/Critic or permanent product telemetry.

### Suggested First PRD

The pre-PRD recommends starting with either:

- Propose-then-correct refinement as the fastest user-experience win.
- Symbology resolver plus OpenDART as the most dependency-unblocking finance/data-source work.

---

## In Progress: YouTube Source Adapter

**Status:** Slice 1 implemented: API-key setup, source chip/Admin exposure, YouTube Data API discovery, native transcript extraction, quota tracking, and transcript-backed brief candidates. Remaining work: richer YouTube brief cards, transcript modal/player, and Whisper fallback.
**Priority:** After unified brief flow and Admin redesign are verified
**Spec:** `/Users/macstudio/Desktop/youtube-source-spec.html`
**Goal:** Add YouTube as a selectable source that discovers relevant videos, extracts transcripts, and feeds transcript content into the shared brief pipeline.

### User Problem

Useful topic context increasingly lives in videos, but the app cannot currently discover or summarize YouTube content alongside web, Gmail, Reddit, and podcasts.

### Desired Outcome

A user can select YouTube while creating a brief. The system searches YouTube for relevant videos, extracts transcripts when available, falls back to local transcription only for high-value videos, and includes useful videos in the same ranked brief as other sources.

### Release Plan

1. Backend adapter scaffold
   - Add a YouTube adapter to the source registry.
   - Use YouTube Data API v3 for discovery.
   - Use topic-profile keywords and recency preference to build search queries.
   - Filter out Shorts by default with the medium-duration setting.

2. Transcript extraction
   - Try native captions first with `youtube-transcript-api`.
   - Use `yt-dlp` plus local Whisper only when native captions are unavailable and the video appears high relevance.
   - Group transcript chunks into roughly 60-second timestamped blocks.
   - Drop videos cleanly when transcript extraction fails.

3. Schema, progress, and quota
   - Add YouTube-specific candidate metadata: `video_id`, `channel_name`, `youtube_title`, `thumbnail_url`, `duration_seconds`, `transcript_segments`, and `transcript_source`.
   - Add a `youtube` progress block with search status, transcript counts, failures, and quota units used.
   - Persist daily API quota usage and reset at midnight Pacific.
   - Surface amber status when usage exceeds 8,000 of 10,000 daily units.

4. Admin and source setup
   - Add YouTube as the fifth source row below Podcasts in Admin -> Sources.
   - Add API key storage, quota usage, max results, and duration filter controls.
   - Add disabled/no-key state to the source chip and inline connect card.

5. Brief rendering
   - Add YouTube brief cards with AI-generated title, summary, channel, thumbnail, duration, and "Watch & read" action.
   - Add transcript modal with embedded YouTube player and timestamped transcript.
   - Defer transcript auto-scroll polish.

### Not in This Item

- YouTube account authentication.
- Playlist or subscription monitoring.
- Comments, community posts, or Shorts-first behavior.
- Offline video caching or downloading beyond temporary audio for transcription fallback.

### Acceptance Criteria

- YouTube can be enabled from Admin with an API key.
- The YouTube chip appears in the input bar in fifth position.
- When selected, YouTube contributes transcript-backed candidates to the shared digest core.
- No-key, quota-exceeded, zero-result, private-video, and transcript-failure paths degrade gracefully.
- Daily quota usage is visible in Admin and reflected in source status.
- Included YouTube cards open a transcript modal with a working embedded player and timestamp seeking.
- Existing non-YouTube brief flows continue working.

### Test Plan

- Backend tests for discovery success, no-result handling, no-key handling, quota tracking, quota warning threshold, native transcript success, Whisper fallback, transcript timeout, and candidate metadata mapping.
- Pipeline tests proving YouTube candidates pass through Librarian, Editorial, and Critic.
- Frontend checks for chip states, inline setup, Admin row/drawer, progress row, brief card, transcript modal, timestamp seeking, and clean modal close.

---

## In Progress: Collections Source Adapter

**Status:** Slice 1 implemented: folder setup, top-level collection detection, text-like file indexing, simple local lexical retrieval, source chip/Admin exposure, and brief-pipeline candidates. Remaining work: sqlite-vec embeddings, richer file types, folder watching, oMLX transcription/OCR, collection picker, and citation polish.
**Priority:** After YouTube source foundations or whenever local knowledge-base support becomes the next product focus
**Spec:** `/Users/macstudio/Desktop/collections-source-spec.html`
**Goal:** Add a fully local "Collections" source that lets users index personal files and use them to sharpen refinement, discovery, ranking, and citations.

### User Problem

The app currently treats every brief as if the user's prior notes, course materials, PDFs, recordings, and research files do not exist. That misses a major local-first advantage: connecting new source discovery to the user's own knowledge base.

### Desired Outcome

A user can create named collections from folders under `~/Documents/Collections/`, select one or more collections for a brief, and have the AI use that material to refine the topic, improve external search queries, boost relevant candidates, and cite local source files when used.

### Release Plan

1. Local collection foundation
   - Create the root Collections folder on first use.
   - Treat only top-level folders as collections.
   - Ignore files placed directly in the root folder and log an Admin warning.
   - Add tables for `collection_files`, `collection_chunks`, and `collection_transcripts`.

2. Indexing pipeline
   - Add a folder watcher using `watchdog` inside the existing launchd service.
   - Add a pre-action sync before any brief run that uses Collections.
   - Index only new or modified files using modification timestamps.
   - Extract text from documents, spreadsheets, presentations, images, audio, and video.
   - Mark unsupported or failed files with status and reason.

3. Transcription and embeddings
   - Transcribe audio and video eagerly in the background.
   - Use local Whisper through oMLX with a max of 2 concurrent transcription jobs.
   - Chunk extracted text into roughly 400-token chunks with 50-token overlap.
   - Generate embeddings through the configured local oMLX embedding model.
   - Store vectors in SQLite using `sqlite-vec`.

4. Retrieval and refinement
   - Retrieve top-K chunks from selected collections using cosine similarity.
   - Inject summarized collection context into the refinement prompt.
   - Add enriched keywords from collection content to the topic profile.
   - Never select all collections by default; the user must explicitly choose collections.

5. Pipeline and citations
   - Retrieve collection context again after confirmation, before source discovery.
   - Use collection context to enrich external source queries.
   - Add a configurable relevance boost during Editorial ranking.
   - Prompt Librarian to cite collection material when summaries use it.
   - Render citation footnotes and open cited files with the system default app.

6. Admin and UI
   - Add Collections as the sixth source row after YouTube.
   - Add drawer sections for each collection: path, counts, index status, re-index, skipped files, and delete-from-index.
   - Add global settings for root path, chunk size, overlap, retrieval K, relevance boost, Whisper model, and transcription concurrency.
   - Add Collections chip with first-use onboarding, picker dropdown, add-collection flow, and selected-count label.
   - Add progress row showing retrieval status, chunks retrieved, enriched keywords, and citation count.

### Not in This Item

- Cloud sync.
- Multi-user collection permissions.
- Deleting folders or files from disk when a collection is removed from the index.
- Auto-selecting collections based on the topic.

### Acceptance Criteria

- First-use onboarding creates a root folder and first collection without leaving the brief flow.
- Top-level folders are collections; nested folders belong to their parent collection.
- Supported files are indexed incrementally and re-indexed when modified.
- Unsupported or failed files are visible in Admin with reasons.
- Collections retrieval is scoped only to the selected collections.
- Refinement questions can use collection terminology and context.
- External source discovery receives enriched keywords from selected collections.
- Brief cards that use collection context can cite the collection name and source file.
- If oMLX, sqlite-vec, or an individual file fails, the brief still builds with clear status.

### Test Plan

- Backend tests for folder creation, collection registration, root-file ignore warnings, incremental indexing, modified/deleted file handling, supported file extraction, unsupported file logging, transcription timeout, transcription concurrency, vector retrieval ordering, selected-collection scoping, refinement context injection, keyword enrichment, citation generation, relevance boost, and graceful zero-context builds.
- Frontend checks for first-use onboarding, collection picker, add-collection flow, selected-count chip label, Admin drawer counts/status/skipped files, re-index, delete-from-index, progress row, citation rendering, and citation click behavior.

---

## In Progress: Markets Source Adapter

**Status:** Slice 1 implemented: Markets chip/Admin exposure, deterministic topic-to-company selection, Simple mode yfinance snapshots, Core/Related company metadata, and brief-pipeline candidates. Remaining work: model-backed company selection, SEC EDGAR filings, FRED macro context, Detailed mode, richer company cards, and Admin audit details.
**Priority:** After source registry and brief rendering can support specialized source cards
**Spec:** `/Users/macstudio/Desktop/markets-source-spec.html`
**Goal:** Add Markets as a financial intelligence source that selects relevant public companies from the user's topic, fetches recent market and filing data, and renders Core and Related company sections.

### User Problem

For topics with public-market implications, the app can find articles but cannot directly answer "which public companies matter here and what changed recently?"

### Desired Outcome

A user can select Markets for a brief. The AI selects relevant publicly traded companies, separates them into Core and Related tiers, fetches free financial data, applies a 90-day recency rule, and includes market context in the brief without requiring the user to pick tickers manually.

### Release Plan

1. Dependencies and configuration
   - Add `yfinance` for prices, earnings, news, financial statements, and analyst data.
   - Add FRED support for Detailed mode macro context with a free API key.
   - Add SEC EDGAR access with required descriptive User-Agent.
   - Add Admin config for default mode, FRED key, max Core companies, and max Related companies.

2. AI company selection
   - Add a Librarian prompt that returns up to 10 Core and 10 Related public companies.
   - Keep Core and Related tiers separate throughout the pipeline.
   - Validate tickers through yfinance before fetching.
   - Store selection rationale for Admin audit, not brief display.

3. Simple mode data
   - Fetch price, 1-day/7-day/30-day movement, market cap, recent earnings, company news, analyst rating, and recent 8-K filings.
   - Apply the 90-day recency rule before inclusion.
   - Retry yfinance failures once after 5 seconds.

4. SEC and Detailed mode data
   - Cache ticker-to-CIK mappings in SQLite.
   - Add SEC EDGAR rate limiting at max 10 requests per second.
   - Fetch 10-Q and 10-K filing text when available within 90 days.
   - Truncate filing text at 50,000 characters before summarization.
   - Add financial statements, EPS/revenue trends, and optional FRED macro context.

5. Enrichment and ranking
   - Enrich each company with a narrative headline, summary, headline data point, sentiment, and Detailed-mode filing summary.
   - Rank within Core and Related separately by topic relevance, recency, and movement/earnings surprise magnitude.
   - Include all companies with valid 90-day data; ranking controls order, not inclusion.

6. Progress, Admin, and UI
   - Add a Markets progress block with mode, selected companies, per-company status, exclusions, filings count, and macro-data status.
   - Add Markets as the seventh source row after Collections in Admin -> Sources.
   - Add Markets chip in seventh position with Simple/Detailed mode picker.
   - Persist last-used mode with source setup for future briefs.
   - Add Core companies and Related companies brief sections with data-age labels.

### Not in This Item

- User-entered ticker picking.
- Paid market data providers.
- Alpha Vantage integration.
- ETFs, indices, private companies, or crypto assets unless a future spec explicitly adds them.
- Investment advice or buy/sell recommendations.

### Acceptance Criteria

- Markets can run in Simple mode without any paid API key.
- Detailed mode works without FRED by skipping macro context and showing a clear warning.
- AI-selected tickers are validated before data fetch.
- Invalid tickers, stale companies, EDGAR failures, yfinance timeouts, missing FRED key, and all-company-failed paths degrade gracefully.
- Core and Related tiers stay separate in data, progress, Admin details, ranking, and brief rendering.
- All included data points are within 90 days or explicitly current price data.
- Each displayed data point has an age label.
- Existing brief flows continue working when Markets is off.

### Test Plan

- Backend tests for Core/Related selection, invalid ticker discard, CIK lookup and caching, Simple mode fetch coverage, 90-day filtering, stale company exclusion, Detailed mode statements and filings, filing truncation, FRED success and missing-key paths, EDGAR rate limiting, yfinance retry, separate tier ranking, data-age calculation, progress JSON, and all-fail graceful build.
- Frontend checks for Markets chip position, Simple/Detailed picker, persisted mode, Admin row/drawer, FRED warning badge, progress sub-status, Detailed-mode "Fetching filings" state, Core/Related sections, age labels, and absence of empty company cards.
