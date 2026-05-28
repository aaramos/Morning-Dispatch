# Morning Dispatch Investment Brief

Date: May 28, 2026

## One-Line Pitch

Morning Dispatch is a local-first AI intelligence product that turns a plain-language interest into a source-aware, audited, editorial-quality briefing across newsletters, web search, forums, podcasts, YouTube, local files, and market data.

## Executive Summary

Professionals do not have an information scarcity problem. They have an attention, trust, and synthesis problem.

Morning Dispatch is built for people who need to track fast-moving topics without living inside feeds: investors, operators, product leaders, researchers, analysts, and domain experts. A user describes what they care about, chooses which sources are allowed, lets an AI refinement agent turn that interest into a search strategy, and receives a readable brief with source provenance, ranking, quality checks, and follow-on actions.

The product is currently an early but working local-first application. It runs as a single-user deployment, connects to real data sources, uses local models through an OpenAI-compatible oMLX endpoint, and produces rendered HTML briefs that can be opened, rebuilt, emailed, or scheduled as recurring digests.

The investment opportunity is to turn a proven local product into a broader personal and team intelligence platform: a trusted briefing layer for recurring knowledge work.

## The Problem

High-value signals are scattered across places that do not naturally work together:

- Gmail newsletters and private email subscriptions
- Web search and public reporting
- Reddit and community discussions
- Podcasts and YouTube transcripts
- Local research folders and private notes
- Market data and company-specific signals

Existing tools usually solve only one slice:

- Search engines find links but do not remember intent or build recurring workflows.
- RSS and newsletter readers collect feeds but do not judge source fit.
- AI chat tools summarize what the user pastes but do not operate as controlled source pipelines.
- Enterprise intelligence tools are expensive, heavy, and often disconnected from personal trusted sources.

Morning Dispatch attacks the workflow gap: "I know what I care about; find the right material, filter it, explain it, and keep doing that over time."

## Product Purpose

Morning Dispatch converts curiosity into a repeatable intelligence workflow.

The core loop is:

1. The user describes an interest in plain English.
2. The refinement agent asks targeted follow-up questions and builds a search strategy.
3. The user selects allowed sources.
4. The app searches, fetches, audits, ranks, and writes a brief.
5. The user can open the brief, rebuild it, email it, or schedule it as a recurring digest.

The product has two closely related modes:

- **Exploration:** a one-off brief built immediately.
- **Digest:** a recurring version of a successful exploration.

This matters commercially because the product can start with a single high-value workflow, prove value quickly, and then become part of the user's daily or weekly operating rhythm.

## Current State

Morning Dispatch is a working prototype / early product build, not a packaged multi-tenant SaaS yet.

Current live deployment:

- Local app served at `http://127.0.0.1:8000`
- Remote access available through Tailscale at `https://ultras-mac-studio-2.tail4aeef0.ts.net`
- Backend service runs under LaunchAgent `com.morning-dispatch`
- Runtime data is stored locally in SQLite and filesystem artifacts
- Secrets are stored outside the repository in owner-only app directories

Current health from the live admin surface:

| Area | Current state |
|---|---|
| App health | Ready for overnight run |
| Gmail | Connected and ready |
| Gmail send | Ready, but default digest email delivery is not enabled |
| Web search | Enabled |
| Reddit MCP | Connected |
| Podcast Index | Configured |
| YouTube | Configured |
| Markets | Enabled in Simple mode |
| Collections | Available; local folder setup still needed |
| Local model | Enabled |
| Active local model | `Gemma4-MTP-26B-BF16` |
| Scheduler | Running |
| Model metrics | Capturing route-level performance |

Known current limitations:

- The product is still single-user and local-first.
- It is not yet packaged as a cloud SaaS.
- Collections are available but need setup before they contribute content.
- Podcast discovery is configured, but local transcription is not yet configured.
- Reddit source review / source scout has not completed.
- Email delivery is ready technically, but default digest delivery is not enabled.

## Why This Is Investable

Morning Dispatch is compelling because it is not another generic chat box. It is an opinionated AI workflow around trusted sources, user intent, and repeatable brief production.

The core investment thesis:

- **Daily pain:** Professionals already spend time scanning fragmented sources.
- **Clear wedge:** Personal intelligence and recurring briefings are easy to understand and easy to try.
- **Expandable use cases:** Investor tracking, competitive intelligence, technical monitoring, professional learning, and market research all share the same pipeline.
- **Privacy-first differentiation:** Gmail and local files can remain local by default.
- **Agentic workflow, not agent theater:** The agents have distinct responsibilities and produce an auditable artifact.
- **SaaS upside:** The local prototype can become a hosted product for individuals, teams, and expert communities.

## Target Customers

### Initial Power Users

- Product managers tracking competitors, platforms, and technical trends
- Investors tracking companies, sectors, supply chains, and catalysts
- Founders tracking markets, customers, tools, and competitor moves
- Engineers and researchers tracking fast-moving technical domains
- Consultants and analysts producing recurring client-ready briefs

### Future Team Buyers

- Product strategy teams
- Investment research teams
- Competitive intelligence teams
- Developer relations teams
- Corporate strategy groups
- Specialized newsletters, expert networks, and research boutiques

## Core Use Cases

### 1. Investor and Market Monitoring

A user can ask the product to track companies, tickers, market catalysts, sector shifts, or supply-chain developments. The Markets source can fetch public-market snapshots using free data, including recent price movement and trailing three-month price history. Rendered briefs can surface ticker performance with current price, recent change, and sparkline visuals.

### 2. Technology and Competitive Intelligence

The product is especially strong for AI, developer tooling, infrastructure, and technical ecosystems. It can combine web reporting, newsletters, Reddit discussion, YouTube transcripts, podcasts, and local documents into a single briefing.

### 3. Newsletter Intelligence

Approved newsletters can become discovery feeds. Morning Dispatch reads only approved Gmail senders, extracts linked articles, follows those links, and treats the linked articles as primary content when possible.

### 4. Learning and Research Briefs

Users can ask for a learning-oriented brief on a topic. The refinement agent turns the broad interest into scope, depth, recency, exclusions, source choices, and search queries.

### 5. Private Knowledge Briefing

Collections support lets local documents and saved research become source material. This is the path toward private team intelligence without forcing all data into a third-party cloud.

## Agents In Use

Morning Dispatch uses a multi-agent workflow with a mix of model-backed judgment and deterministic orchestration.

| Agent or service | Type | Role |
|---|---|---|
| Refinement Agent | Model-backed with deterministic guardrails | Turns the user's raw interest into a runnable search strategy. It asks clarifying questions, infers scope, depth, source strategy, exclusions, and source-specific queries. |
| Discovery Runner | Deterministic orchestration | Runs selected source adapters, applies timeouts, merges candidates, removes duplicates, and prepares normalized payloads. |
| Source Adapters | Mostly deterministic | Translate the topic profile into source-specific work for Web, Gmail, Reddit, Podcast, YouTube, Collections, Foreign Media, and Markets. |
| Librarian | Hybrid model + fallback | Cleans and enriches fetched items with titles, summaries, keywords, and content type. Falls back deterministically when the model fails. |
| Source Audit Agent | Model-backed with fallback | Checks candidate freshness, source quality, topic fit, and exclusions before ranking. |
| Editorial Agent | Model-backed with fallback | Ranks the complete candidate set, chooses the lead story, and decides what should be included or demoted. |
| Critic Agent | Model-backed with fallback | Reviews the draft for weak leads, duplicates, promotional material, poor ordering, and low-value items. |
| Brief Quality Checks | Deterministic | Final cleanup layer that removes duplicates, repairs weak display fields, and drops broken or low-value links. |
| Scheduler | Deterministic | Runs recurring topic profiles and turns successful explorations into ongoing digests. |

Current local model routing:

| Route | Provider | Effective model |
|---|---|---|
| Refinement | Local | `Gemma4-MTP-26B-BF16` |
| Librarian | Local | `Gemma4-MTP-26B-BF16` |
| Source Audit | Local | `Gemma4-MTP-26B-BF16` |
| Editorial | Local | `Gemma4-MTP-26B-BF16` |
| Critic | Local | `Gemma4-MTP-26B-BF16` |

## Tech Stack

### Backend

- Python
- FastAPI
- SQLite
- Async source discovery and fetch pipeline
- Local filesystem storage for rendered brief artifacts
- LaunchAgent-backed always-on local service

### Frontend

- React
- TypeScript
- Vite
- Custom CSS UI
- Admin, source setup, library, model, metrics, and brief controls

### Model Runtime

- oMLX
- OpenAI-compatible local endpoint at `http://127.0.0.1:1234/v1`
- Current local model: `Gemma4-MTP-26B-BF16`
- Optional Ollama Cloud route configured, with private-source protections
- Route-level model telemetry and cache metrics

### Integrations

- Gmail OAuth and Gmail send
- Gmail MCP tooling
- Reddit MCP
- Brave Search / Tavily web search support
- YouTube Data API
- Podcast Index
- yfinance-backed Markets Simple mode
- Tailscale for secure remote access to the local app

### Data and Privacy

- SQLite local database
- Runtime files outside the repository
- Secrets stored outside the repository in owner-only app secret folders
- Gmail and Collections treated as private sources
- Private source rule: Gmail and Collections content stays local unless a later explicit override is added

## Notable Product Capabilities

### Strict Gmail Sender Allowlist

Gmail now uses a strict approved-sender model. Discovery can surface candidate newsletter senders, but build-time reading uses only approved senders. This creates a safer trust boundary for private email content.

### AI-Refined Search Strategy

The refinement flow no longer behaves like a static questionnaire. It can ask targeted follow-ups, generate source-specific queries, and fill a plain-language strategy review card before build.

### Source-Aware Brief Controls

Users can tune brief size, lead story count, quality thresholds, per-source maximums, and system-level pipeline limits. This is important because the product is moving toward configurable, repeatable intelligence workflows rather than one-off summaries.

### Ticker Performance Rendering

Markets support can surface current public-company performance and trailing three-month price history in the brief, including sparkline rendering for market snapshots.

### Local Model First

The active product uses local model inference through oMLX. This supports the privacy story, reduces dependency on external AI APIs for private content, and gives the product a differentiated local-first posture.

### Multi-Source Expansion

The product already has adapters or setup surfaces for Web, Gmail, Reddit, Podcasts, YouTube, Collections, Foreign Media, and Markets. The source registry design makes additional adapters realistic.

### Observable AI Workflow

The admin surface exposes model health, route performance, cache behavior, source status, secret health, and scheduler state. This is the start of the observability layer needed for a reliable AI workflow product.

## Competitive Positioning

Morning Dispatch should be positioned as:

> The AI briefing system for people who need trusted recurring intelligence, not another stream of links.

It is not primarily:

- a chatbot
- an RSS reader
- a newsletter client
- a generic search wrapper
- a one-off summarizer

It is a source-controlled briefing workflow. The user defines what matters, the system searches only allowed sources, agents judge and rank the material, and the output is a readable artifact that can become recurring.

## Proof Points

Current product proof:

- End-to-end local application exists.
- Gmail, web, Reddit, YouTube, Podcasts, and Markets are integrated or configured.
- Local model routing is live.
- Refinement agent is live and source-aware.
- Gmail allowlist is implemented.
- Rendered briefs include provenance and source stats.
- Admin can configure sources, models, defaults, and limits.
- Scheduler is running.
- Model metrics and cache metrics are visible.
- The app is reachable locally and over Tailscale.

This is still pre-commercial, but it is beyond a slideware concept. The product has a real architecture, live integrations, and a working user loop.

## What Funding Would Unlock

### 1. Package the Product

Convert the local single-user prototype into a productized experience with onboarding, templates, default workflows, and clearer setup.

### 2. Add Team and SaaS Foundations

Build accounts, workspaces, multi-tenant storage, hosted credential management, billing, and production observability.

### 3. Build Repeatable Use-Case Packages

Launch opinionated workflows:

- investor morning brief
- competitor watch
- AI infrastructure monitor
- product category tracker
- customer / market signal tracker
- private research digest

### 4. Improve Source Compliance and Governance

Add stronger policy controls around email, private documents, paid content, source attribution, retention, and export.

### 5. Harden the Agent Pipeline

Improve evaluation, traceability, source quality scoring, failure recovery, and benchmarked performance for agent routes.

### 6. Expand Distribution

Package as:

- local desktop / personal server app
- team SaaS
- self-hosted enterprise deployment
- premium research workflow for specific verticals

## Main Risks

| Risk | Mitigation path |
|---|---|
| Source reliability varies by provider | Keep adapter architecture, source timeouts, graceful degradation, and source-specific health checks. |
| AI quality can drift | Add evals, route metrics, human feedback loops, deterministic safeguards, and prompt/version tracking. |
| Gmail and private data require trust | Keep strict allowlists, local-first processing, explicit source controls, and clear privacy boundaries. |
| Local-first setup can be complex | Productize onboarding, bundle model/runtime setup, and offer hosted or managed options. |
| SaaS conversion is non-trivial | Treat local product as proof of workflow, then build account/workspace/billing infrastructure deliberately. |

## Investment Narrative

The market is moving from "AI that answers questions" to "AI that runs repeatable knowledge workflows." Morning Dispatch is aimed directly at that shift.

The early product shows the shape of a durable platform:

- user intent becomes structured strategy
- trusted sources become reusable inputs
- agents perform bounded jobs
- outputs are readable, attributable, and repeatable
- successful explorations become recurring intelligence

The initial wedge is personal and team briefings. The larger opportunity is a controllable intelligence layer for knowledge workers: one place to say what matters, connect trusted sources, and receive high-signal briefings that improve over time.

## Suggested Fundraising Position

Morning Dispatch should be pitched as a seed-stage opportunity to productize a working local-first AI intelligence engine.

Recommended positioning:

> Morning Dispatch is building the trusted briefing layer for recurring knowledge work. It combines source-aware retrieval, private-source controls, local model execution, and a multi-agent editorial pipeline to turn scattered information into repeatable intelligence.

The ask should be tied to a concrete milestone:

> Fund the transition from working local prototype to polished personal/team product: onboarding, hosted option, workspace model, source governance, packaged use cases, and quality evaluation.

## Bottom Line

Morning Dispatch has the right early ingredients: a clear pain point, a real working product, defensible source-control behavior, local AI infrastructure, and a product loop that can become habitual.

The opportunity is to turn it from an impressive local intelligence tool into a trusted AI briefing platform for individuals and teams who need to stay current without drowning in feeds.
