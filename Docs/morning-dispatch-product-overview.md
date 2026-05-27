# Morning Dispatch Product Overview

## Executive Summary

Morning Dispatch is a personal intelligence product that turns a user's curiosity into a high-signal briefing. The user describes what they want to understand, the system helps refine the request, searches across selected sources, audits the candidate material for fit and quality, and produces a ranked editorial brief on demand. A completed brief can then be converted into a recurring digest.

The product sits between a search engine, an RSS reader, a research assistant, and an analyst. Its core promise is not simply to collect links. It is to understand what the user is trying to track, find relevant material across heterogeneous sources, and produce a readable briefing with provenance, source controls, and quality checks.

For a potential investor or SaaS buyer, the opportunity is a focused AI workflow for recurring personal or team intelligence: market tracking, technology monitoring, competitive awareness, professional learning, investment research, and domain-specific briefings.

## Product Purpose

Morning Dispatch exists to solve a familiar problem: useful information is scattered across newsletters, websites, forums, podcasts, YouTube, local files, and market data. People either miss important signals or spend too much time skimming low-quality material.

The application gives users one flow:

1. Describe an interest in plain language.
2. Let the AI refine the request into a search strategy.
3. Choose which source types the system may search.
4. Confirm the brief setup.
5. Build an on-demand exploration.
6. Open, rebuild, email, or schedule the result as a recurring digest.

This unifies two product modes:

- **Exploration:** an on-demand brief created now.
- **Digest:** a recurring version of a successful exploration.

Everything starts as an exploration. Scheduling is a follow-on action after the user sees value from the initial brief.

## Target Use Cases

### Investor and Market Monitoring

A user can ask for coverage of company performance, tickers, market catalysts, supply-chain developments, or sector trends. The system can infer likely tickers from company names, search recent news, bring in market snapshots, and avoid low-quality syndicated sources when requested.

Example: "As an investor, track Micron, Hynix, Kioxia, and SanDisk over the previous three days, avoiding MSN or Yahoo-like news."

The app now resolves likely tickers such as `MU`, `000660.KS`, `285A.T`, and `SNDK` without requiring the user to know the exact symbols.

### Technology and Competitive Intelligence

Users can track a technology category, vendor ecosystem, product class, regulatory issue, or competitive landscape. The app is especially well suited to AI, developer tooling, local models, infrastructure, and technical workflows because it can combine web reporting, forums, videos, newsletters, and local knowledge.

### Professional Learning

A user can request a learning-oriented brief on a domain, such as robotics, energy storage, AI agents, cloud infrastructure, history, or policy. The AI refinement step narrows the angle and converts the request into concrete search queries.

### Media and Creator Research

YouTube and podcast sources let the product discover transcript-backed media, not just articles. Media items can be included as first-class cards in the brief, with title, summary, transcript, and playback/review affordances.

### Local Knowledge Briefing

Collections support is designed to let users include local folders as a source. This allows private documents, saved research, notes, or curated files to shape briefs alongside external sources. The current live instance exposes Collections but still needs the local folder setup before it can contribute content.

## Data Sources

Morning Dispatch uses a source registry architecture. Each data source plugs into the same adapter contract, so the system can add or remove sources without changing the core briefing pipeline.

### Current Source Status

As of the current development deployment:

| Source | Current status | Purpose |
|---|---:|---|
| Web Search | Enabled | Finds current articles, reporting, pages, and public web coverage. |
| Gmail | Enabled | Reads configured newsletters and can send completed briefs by email. |
| Reddit | Enabled | Searches forum discussions and community signals through the Reddit MCP connection. |
| Podcasts | Enabled | Uses Podcast Index credentials for podcast discovery; local transcription is not yet configured. |
| YouTube | Enabled | Uses the YouTube Data API and transcript extraction for video-backed brief items. |
| Collections | Available but not enabled | Local folder source; requires creating/configuring the Collections root folder. |
| Markets | Enabled | Simple public-market mode using free market data for public-company context and ticker-aware tracking. |

### Source Strategy

The product defaults to a simple entry experience. Users choose which source types are allowed for a brief. The selected sources guide discovery and retrieval; disabled or unselected sources do not run.

The source system supports:

- per-brief source selection
- source enablement flows
- per-source progress
- soft failures when a source times out or cannot return useful material
- future adapters through the same registry pattern

## Agent System

Morning Dispatch is built around a multi-agent workflow. Some agents are model-backed and judgment-oriented; others are deterministic service components that normalize, fetch, and route data.

### Refinement Agent

**Type:** Model-backed, with deterministic guardrails.

The Refinement Agent takes the user's raw interest and turns it into a runnable topic profile. It asks clarifying questions, infers constraints, creates search queries, identifies source needs, and prepares the confirmation card.

Recent improvements include:

- up to 10 refinement questions when needed
- stronger inference from the user's original request
- ticker inference for finance prompts
- explicit handling of source scope, exclusions, depth, and search strategy
- visible progress while the model is working

### Discovery Runner

**Type:** Deterministic orchestration.

The Discovery Runner queries selected source adapters in parallel, applies source timeouts, merges candidates, removes duplicates, and prepares normalized candidate payloads.

It is not an LLM agent. Its role is orchestration and reliability.

### Source Adapters

**Type:** Mostly deterministic services with source-specific logic.

Adapters translate a topic profile into source-specific search or fetch behavior. Current adapters include Web, Gmail, Reddit, Podcast, YouTube, Collections, and Markets.

### Librarian

**Type:** Hybrid: model-backed enrichment with deterministic fallback.

The Librarian cleans and enriches candidate items. It can generate better titles, summaries, keywords, and content classifications using the local model. If the model fails, the system falls back instead of failing the brief.

### Source Audit Agent

**Type:** Model-backed.

The Source Audit Agent is a new quality checkpoint before ranking. It reviews candidate items for:

- fit against the user's request
- freshness against the requested time window
- source quality
- syndicated or aggregator-like behavior
- whether an item should be excluded, included, or included only as context

This gives the system a smarter quality gate than simple keyword filtering.

### Editorial Agent

**Type:** Model-backed, with deterministic fallback.

The Editorial Agent ranks the approved candidate set as a whole. It chooses the lead story, decides what to include or demote, and assigns editorial sections. Ranking happens after discovery, fetch, enrichment, and source audit so the agent can compare the complete candidate set.

### Critic Agent

**Type:** Model-backed, with deterministic fallback.

The Critic Agent reviews the draft for problems such as weak leads, duplicates, promotional material, low-value links, or poor ordering. It can recommend dropping, demoting, or replacing items.

### Brief Quality Checks

**Type:** Deterministic quality layer.

Brief Quality applies final cleanup before rendering. It repairs weak summaries where possible, removes duplicates, drops broken or low-value links, and normalizes display fields.

### Scheduler

**Type:** Deterministic service.

The Scheduler runs recurring topic profiles. A completed exploration can become a digest with a chosen schedule. The current development deployment has the scheduler running.

## User Experience

The current product experience has three major surfaces.

### Entry Page

The entry page is the main user experience. It shows recent explorations, a composer, and source chips. The user describes what they are interested in, selects sources, and starts refinement.

Source chips communicate state:

- enabled and selected
- enabled but unselected
- disabled and requiring setup

### Refinement and Confirmation

After submission, the AI asks questions to refine the request. The user can continue answering or choose "just go now." The app then shows a confirmation card before building.

The confirmation step exposes the practical brief setup: scope, source scope, depth, sources, exclusions, and search plan.

### Admin

Admin is a separate management surface with tabs for:

- Status
- Sources
- Library
- Delivery
- Models

Admin supports source setup, library review, email delivery configuration, model selection, secret health, and digest management.

## Output Experience

The generated brief uses an editorial layout with:

- masthead and brief header
- lead story treatment
- ranked story list
- media cards for YouTube and podcast items
- provenance sidebar
- source and processing stats
- feedback controls
- warning links when a brief was built with source issues

Completed briefs can be:

- opened in the browser
- rebuilt in place
- emailed to a specified address when Gmail send is configured
- scheduled as recurring digests

## Current Technical State

The current deployment is a local development instance exposed through Tailscale:

- Entry page: `https://ultras-mac-studio-2.tail4aeef0.ts.net/`
- Admin: `https://ultras-mac-studio-2.tail4aeef0.ts.net/admin`
- Health: `https://ultras-mac-studio-2.tail4aeef0.ts.net/api/health`

Current live health:

- App status: ready
- Gmail: connected
- Reddit MCP: connected
- Local model: enabled
- Active model: `Gemma4-MTP-26B-BF16`
- Scheduler: running
- Gmail send: ready
- Secret storage: owner-only app secrets folder
- Web, Gmail, Reddit, Podcast, YouTube, and Markets: enabled
- Collections: available, pending local folder setup

Known current warnings:

- No legacy scheduled digest has run in the current reset state.
- Reddit source review / source scout has not completed.
- Default digest email delivery is not enabled, though Gmail send is ready.

## Tech Stack

Morning Dispatch is currently implemented as a local-first web application.

### Backend

- Python
- FastAPI
- SQLite
- LangGraph-style pipeline orchestration for digest flow
- Async source discovery and fetch pipeline
- Local filesystem artifact storage for rendered briefs

### Frontend

- React
- TypeScript
- Vite
- CSS-based custom UI

### Model Runtime

- oMLX / OpenAI-compatible local model endpoint
- Current model: `Gemma4-MTP-26B-BF16`
- Admin-selectable model configuration
- Model metrics and cache tracking

### Integrations

- Gmail OAuth and Gmail send
- Reddit MCP
- Brave Search / Tavily search support
- YouTube Data API
- Podcast Index
- yfinance-backed Markets simple mode
- Tailscale for secure local access

### Secret Storage

Secrets live outside the repository in an owner-only app secrets directory. The app reports whether secrets are configured but does not expose secret values in the UI or API responses.

## Differentiation

Morning Dispatch is differentiated from a generic AI chat product because it combines:

- source-aware retrieval
- explicit user-controlled source selection
- recurring digest conversion
- multi-agent editorial workflow
- source audit before ranking
- local model support
- media and transcript-aware sources
- private-source extensibility through Collections and Gmail

It is differentiated from a traditional newsletter or RSS product because the unit of value is not a feed. The unit of value is a refined user intent converted into a briefing workflow.

## Product Maturity

Morning Dispatch is currently best described as a working prototype / early product build. The core user flow exists, multiple data sources are integrated, and the app can generate briefs through a shared pipeline. It also has Admin controls, source setup, email delivery, model selection, secret health, and a developing quality layer.

The product is not yet a packaged multi-tenant SaaS. It currently runs as a local-first single-user deployment. To become a SaaS product, the next major work would be:

- account and workspace model
- hosted credential management
- multi-tenant data isolation
- billing and plan limits
- production observability
- hosted queue/workers
- durable cloud storage
- stronger source compliance controls
- onboarding designed for non-technical users

## Near-Term Roadmap

The most valuable next steps are:

1. Improve source audit and brief adherence for strict constraints such as time windows and source exclusions.
2. Make refinement more consistently strategic and less form-like.
3. Strengthen finance workflows with richer company resolution, filings, and market context.
4. Complete Collections setup and local knowledge indexing.
5. Improve podcast transcription support.
6. Add clearer live progress and model telemetry throughout the brief build.
7. Package the product experience around repeatable use cases: investor briefs, competitive intelligence, technology monitoring, and research digests.

## Positioning Statement

Morning Dispatch is an AI-powered briefing system for people who need to stay current without living inside feeds. It turns a plain-language interest into a source-aware, audited, editorial-quality brief, and lets the user convert successful explorations into recurring digests.

The product's long-term potential is a personal or team intelligence layer: one place to express what matters, connect trusted sources, and receive concise briefings that improve over time.
