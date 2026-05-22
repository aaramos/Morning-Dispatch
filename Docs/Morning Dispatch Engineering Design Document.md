# Morning Dispatch — Engineering Design Document

**Version:** 1.4  
**Author:** Adrian  
**Date:** May 2026  
**Status:** MVP Design with Accepted Product Decisions

-----

## 1. Project Overview

Morning Dispatch is a personal intelligence system that turns approved Gmail newsletters into locally served, newspaper-style HTML digests tailored to the user’s research interests. The newsletters are discovery feeds: Morning Dispatch extracts the links they surface, fetches and summarizes the primary linked articles when possible, and uses the newsletter text only as fallback context when a linked article cannot be fetched.

It is designed as a local-first application running on Apple Silicon (Mac Studio M3 Ultra, 96GB RAM), with a browser-accessible reading surface and a modular agent architecture that can be extended or adapted for broader use.

The MVP supports one local user profile with one or more independent digests. The design reserves room for multiple local profiles later, where each person can have multiple digests, but large-scale cloud multi-tenancy is intentionally out of scope.

### Dual Purpose

1. **Personal use** — a daily intelligence layer surfacing relevant content across AI, investing, technology, and other named interest areas
1. **Educational scaffold** — a practical introduction to agentic workflow design using industry-standard tooling (LangGraph, FastAPI, MLX), suitable for demonstration, adaptation, or productization

### Accepted MVP Product Decisions

1. **Local profile model** — MVP starts with one local profile, but the data model includes profile ownership so multiple local profiles can be added later.
1. **Multiple independent digests** — each digest has its own name, schedule, approved newsletter sources, interest profile, threshold, source weights, and feedback loop.
1. **Shared content, independent judgment** — the same article may be stored once and reused across digests, but each digest scores, ranks, labels, and learns from it independently.
1. **Gmail newsletters as discovery feeds** — newsletters identify promising links; the linked article is the primary content.
1. **Primary and secondary attribution** — primary source is the linked article; secondary source is the newsletter issue that surfaced it.
1. **Strict link filtering** — unsubscribe links, tracking links, ads, account-management links, social-share links, and obvious sponsor/junk links are dropped before fetching.
1. **Clean storage only** — store cleaned article content, article metadata, newsletter attribution, digest output, and feedback. Do not permanently store raw newsletter bodies, raw HTML dumps, or tracking URLs.
1. **Fetch fallback** — if the article cannot be fetched, summarize the newsletter description/snippet, keep the intended article URL as metadata, mark the item as fallback, and rank it lower.
1. **Controlled outbound access** — the app may fetch approved newsletter links and required source APIs, but the user-facing app remains localhost-only.
1. **Portable later, not MVP-critical** — secrets stay outside the project folder. The MVP should not require export/import, but should avoid choices that make future portability hard.
1. **HTML newspaper output** — each digest run produces a locally previewable HTML issue assembled by the Editor agent, not just a list of summaries.

-----

## 2. System Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                       Browser UI                        │
│        Control Panel · HTML Newspaper Preview           │
└─────────────────────┬───────────────────────────────────┘
                      │ HTTP (localhost only)
┌─────────────────────▼───────────────────────────────────┐
│                   FastAPI Server                        │
│ REST API · Static React Build · Scheduler · Issue Server│
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│               Editor Agent (LangGraph)                  │
│  Scores · Groups · Writes HTML newspaper-style issue    │
└──────┬──────────────────────────────┬───────────────────┘
       │                              │
┌──────▼──────┐                ┌──────▼──────┐
│  Digestor   │                │  Librarian  │
│   Agent     │                │    Agent    │
└──────┬──────┘                └──────┬──────┘
       │  MCP tool calls
┌──────▼──────────────────────────────────────────────────┐
│                Local MCP Server Layer                   │
│    mcp-gmail · mcp-article-fetcher                     │
│    future: mcp-rss · mcp-reddit · mcp-podcastindex     │
│            mcp-tavily                                  │
│                                                         │
│    isolated containers · no cross-server calls          │
└──────┬──────────────────────────────────────────────────┘
       │  same interface available to other local agents
┌──────▼──────────────────────────────────────────────────┐
│       Other Local Agents (Hermes, OpenClaw, etc.)       │
└──────┬──────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────┐
│                   SQLite Database                       │
│ profiles · digests · articles · discoveries · issues    │
│ feedback · embeddings · runs · source weights           │
└──────┬──────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────┐
│                  Inference Layer (MLX)                  │
│    Qwen 27B MTP (enrichment)  ·  nomic-embed-text       │
│    ModelClient abstraction · Semaphore concurrency      │
└─────────────────────────────────────────────────────────┘
```

### Design Principle: MCP as the Tool Layer

Data source integrations are not owned by the Digestor. Instead, each source is exposed as a self-hosted MCP server running locally in Docker. The Digestor calls these servers via MCP tool calls — and so can any other agent on your local stack (Hermes, OpenClaw, future projects).

For MVP, Gmail is the only discovery source, but article fetching is also a first-class capability because newsletters point to the primary source material. The system should treat article fetching as controlled outbound access: allowed for approved newsletter links, blocked from exposing the local app to the public internet.

This means you are building a **personal capability library**, not a private pipeline. Every integration built for Morning Dispatch is immediately reusable across your entire local AI infrastructure.

-----

## 3. Security Model & Sandboxing

### 3.1 Guiding Principle

AI agents have no access to personal files, home directories, or host system resources. All boundaries are enforced at the infrastructure level — Docker volume mounts, network policy, and credential isolation — not by convention or agent instruction.

### 3.2 File System Boundaries

Morning Dispatch separates code, runtime data, and secrets so the project can be committed safely and moved later without accidentally publishing credentials.

```
/Users/macstudio/Apps/personal_intel/
├── Docs/                       # Design docs and handoff docs
├── backend/                    # App code
├── frontend/                   # App code
├── mcp-servers/                # Local MCP server code
├── config.example.yaml         # Safe example config
├── .env.example                # Safe example environment file
└── README.md

~/.morning-dispatch/
├── data/
│   ├── db/                     # SQLite database
│   ├── article-cache/          # Cleaned article text only
│   ├── digest-output/          # Published HTML issues
│   └── podcast-audio/          # Temporary audio downloads
└── secrets/
    ├── gmail/                  # Gmail OAuth credentials
    ├── reddit/                 # Future Reddit credentials
    ├── podcastindex/           # Future Podcastindex API key
    └── tavily/                 # Future Tavily API key
```

The `~/.morning-dispatch/` paths are configurable through environment variables or a local ignored config file. The MVP does not need a full export/import feature, but this layout keeps future portability straightforward and avoids path names with spaces in Docker Compose.

**What agents cannot access:**

- `~` (home directory) or any subdirectory outside the configured Morning Dispatch data and secrets directories
- `/mnt`, `/Volumes`, or any external storage
- Other application data, documents, photos, or personal files
- Host system credentials or keychains

Docker volume mounts are explicit and minimal per container. No container receives a broad mount like `-v ~/:/home`.

### 3.3 Agent Permission Matrix

Each agent is granted only the MCP tools it requires. Enforced at the MCP tool grant level in the LangGraph graph definition.

|MCP Tool                  |Editor|Digestor|Librarian|
|--------------------------|------|--------|---------|
|`gmail_search`            |—     |✓       |—        |
|`gmail_get_message`       |—     |✓       |—        |
|`gmail_extract_links`     |—     |✓       |—        |
|`article_fetch_url`       |—     |✓       |—        |
|`article_extract_text`    |—     |✓       |—        |
|`rss_fetch_feed`          |—     |✓       |—        |
|`rss_get_item`            |—     |✓       |—        |
|`reddit_get_posts`        |—     |✓       |—        |
|`reddit_search`           |—     |✓       |—        |
|`podcast_get_episodes`    |—     |✓       |—        |
|`podcast_download_episode`|—     |✓       |—        |
|`podcast_transcribe`      |—     |✓       |—        |
|`tavily_search`           |✓     |✓       |—        |
|`sqlite_read`             |✓     |—       |✓        |
|`sqlite_write`            |✓     |—       |✓        |
|`mlx_complete`            |—     |—       |✓        |
|`mlx_embed`               |✓     |—       |✓        |

The Editor directs searches but does not fetch raw content. The Librarian enriches but never touches external sources. The Digestor fetches and normalizes content but never writes enriched records. A controlled persistence step writes run state, watermarks, source attribution, and enriched records to SQLite.

### 3.4 Credential Isolation

OAuth tokens and API keys are never stored in the codebase, shared environment variables, or agent prompts. Each MCP server mounts only its own secrets subdirectory as a read-only volume:

```yaml
mcp-gmail:
  volumes:
    - ${MORNING_DISPATCH_SECRETS_DIR}/gmail:/secrets:ro

mcp-reddit:
  volumes:
    - ${MORNING_DISPATCH_SECRETS_DIR}/reddit:/secrets:ro
```

Agents never receive or see raw credentials. They call MCP tools, which handle auth internally.

### 3.5 Network Isolation

The browser UI and API are local only. FastAPI exposes a port bound to `127.0.0.1`, never `0.0.0.0`.

Some services need controlled outbound internet access: Gmail must reach Google APIs, and the article fetcher must fetch links discovered from approved newsletters. These services may use an egress-enabled network, but they expose no host ports. Internal app coordination stays on a private Docker network.

MCP servers cannot call each other directly. All inter-service communication routes through the Digestor agent or the explicit pipeline nodes.

```yaml
networks:
  dispatch-internal:
    internal: true
  dispatch-egress:
    internal: false

services:
  fastapi:
    ports:
      - "127.0.0.1:8000:8000"
    networks: [dispatch-internal]

  mcp-gmail:
    networks: [dispatch-internal, dispatch-egress]

  mcp-article-fetcher:
    networks: [dispatch-internal, dispatch-egress]
```

### 3.6 PII Filter

Before any payload is passed from the Digestor to the Librarian, a lightweight PII filter runs in the Digestor. If the raw text of an item contains signals of personal or sensitive content — keywords such as `password`, `invoice`, `ssn`, `account number`, `routing number` — the item is dropped with a `quality_flag: pii_detected` and never reaches the inference layer. This protects against misconfigured sender allowlists accidentally ingesting personal email.

### 3.7 Podcast Audio Housekeeping

`mcp-podcastindex` writes audio files to `podcast-audio/` during transcription. A housekeeping job — implemented as an APScheduler task in the FastAPI service — deletes files in that directory older than 24 hours. Audio is transient; only the transcript is retained.

-----

## 4. Local MCP Server Catalog

Each MCP server runs as its own Docker container with a minimal footprint.

### 4.1 mcp-gmail

**Purpose:** Read and search approved Gmail newsletters on behalf of the Digestor. In MVP, Gmail is used as a discovery feed, not as the final source of truth.

|Tool                 |Description                                            |
|---------------------|-------------------------------------------------------|
|`gmail_search`       |Search threads by query, sender, label, or date range  |
|`gmail_get_thread`   |Fetch full thread by ID                                |
|`gmail_get_message`  |Fetch single message — returns plain text and HTML body|
|`gmail_extract_links`|Extract all href links from a message HTML body        |
|`gmail_list_labels`  |List available labels for filter configuration         |

**Auth:** OAuth 2.0, `gmail.readonly` scope. Token refresh handled internally — if the access token is expired, mcp-gmail refreshes using the stored refresh token before retrying. Returns a structured `401` error to the Digestor if refresh fails, allowing the Digestor to log the failure and continue with remaining sources rather than crashing the run.

**Secrets mount:** `${MORNING_DISPATCH_SECRETS_DIR}/gmail:/secrets:ro`

-----

### 4.2 mcp-article-fetcher

**Purpose:** Fetch and extract readable content from links discovered in approved newsletters. This is not broad web search. It only fetches URLs that came from approved discovery sources and survived strict link filtering.

|Tool                       |Description                                                                  |
|---------------------------|-----------------------------------------------------------------------------|
|`article_filter_links`     |Remove tracking, unsubscribe, social-share, account, ad, and obvious junk URLs|
|`article_resolve_url`      |Follow redirects and strip tracking parameters                               |
|`article_fetch_url`        |Fetch a primary article URL                                                  |
|`article_extract_text`     |Extract readable title, text, author, publisher, and publish date when present|
|`article_fetch_status`     |Return fetch status such as `fetched`, `blocked`, `paywalled`, or `error`     |

**Auth:** None for public pages.  
**Secrets mount:** None.  
**Network:** Controlled outbound access only. No exposed host port.  
**Fallback behavior:** If an article cannot be fetched or parsed, the pipeline keeps the intended primary URL as metadata and summarizes the newsletter snippet instead.

-----

### 4.3 mcp-rss

**Purpose:** Poll and parse RSS/Atom feeds. Stateless — watermarks managed by the caller.

|Tool               |Description                                            |
|-------------------|-------------------------------------------------------|
|`rss_fetch_feed`   |Fetch all items from a feed URL since a given timestamp|
|`rss_validate_feed`|Confirm a URL is a valid RSS/Atom feed                 |
|`rss_get_item`     |Fetch full content of a single item by URL             |

**Auth:** None. Public feeds only.  
**Secrets mount:** None.

-----

### 4.4 mcp-reddit

**Purpose:** Query Reddit posts and discussions via PRAW.

|Tool                 |Description                                                 |
|---------------------|------------------------------------------------------------|
|`reddit_get_posts`   |Fetch top/new posts from a subreddit since a given timestamp|
|`reddit_search`      |Search Reddit by keyword, optionally scoped to subreddits   |
|`reddit_get_comments`|Fetch top-level comments for a post                         |

**Auth:** Reddit OAuth app credentials (read-only scope).  
**Secrets mount:** `${MORNING_DISPATCH_SECRETS_DIR}/reddit:/secrets:ro`

-----

### 4.5 mcp-podcastindex

**Purpose:** Discover podcasts, fetch episode feeds, download audio, and transcribe.

|Tool                      |Description                                                            |
|--------------------------|-----------------------------------------------------------------------|
|`podcast_search`          |Search Podcastindex by show name or keyword                            |
|`podcast_get_feed`        |Get RSS feed URL for a show by Podcastindex ID                         |
|`podcast_get_episodes`    |List recent episodes from a feed since a given timestamp               |
|`podcast_download_episode`|Download audio via yt-dlp to `/podcast-audio/`                         |
|`podcast_transcribe`      |Transcribe a downloaded audio file via Parakeet (fallback: whisper.cpp)|

**Known podcast:** AI Daily Brief — Podcastindex ID `6280366`  
**Auth:** Podcastindex API key (free tier).  
**Secrets mount:** `${MORNING_DISPATCH_SECRETS_DIR}/podcastindex:/secrets:ro`  
**Audio volume:** `${MORNING_DISPATCH_DATA_DIR}/podcast-audio:/podcast-audio` (scoped to this container only)  
**Note:** Transcription is async — queued separately, does not block the main batch pipeline. Audio files deleted after 24 hours by the FastAPI housekeeping job.

-----

### 4.6 mcp-tavily

**Purpose:** Web search for on-demand research queries issued by the Editor.

|Tool               |Description                                                    |
|-------------------|---------------------------------------------------------------|
|`tavily_search`    |Execute a web search query, return ranked results with snippets|
|`tavily_fetch_page`|Fetch full text content of a URL                               |

**Auth:** Tavily API key (free tier to start).  
**Secrets mount:** `${MORNING_DISPATCH_SECRETS_DIR}/tavily:/secrets:ro`

-----

## 5. Agent Definitions

### 5.1 The Digestor

**Role:** Raw data acquisition. Calls MCP tools to read approved newsletters, extract links, strictly filter those links, fetch primary article content when possible, and normalize source attribution. Has no knowledge of user interests and no access to enriched records.

**Sources (MVP → Full):**

|Phase|Discovery Source       |Primary Content Source                 |MCP Server(s)                   |
|-----|-----------------------|---------------------------------------|--------------------------------|
|MVP  |Approved Gmail newsletters|Linked public articles              |mcp-gmail, mcp-article-fetcher  |
|v1.1 |RSS/Atom feeds         |Feed item articles                     |mcp-rss, mcp-article-fetcher    |
|v1.2 |Reddit                 |Linked posts/articles and discussions  |mcp-reddit, mcp-article-fetcher |
|v1.3 |Web search             |Search result pages                    |mcp-tavily, mcp-article-fetcher |
|v1.4 |Podcasts               |Episode transcripts                    |mcp-podcastindex                |

**Output per item — normalized payload:**

```json
{
  "id": "uuid",
  "primary_source_type": "article | podcast | discussion | fallback_snippet",
  "primary_source_name": "string",
  "primary_url": "string | null",
  "primary_domain": "string | null",
  "secondary_source_type": "gmail_newsletter | rss | reddit | web_search | podcast_feed",
  "secondary_source_name": "string",
  "secondary_message_id": "string | null",
  "secondary_url": "string | null",
  "link_text": "string | null",
  "content_text": "cleaned article text or newsletter fallback snippet",
  "fetch_status": "fetched | blocked | paywalled | error | fallback_snippet",
  "published_at": "ISO8601 timestamp",
  "fetched_at": "ISO8601 timestamp",
  "metadata": {}
}
```

**Key behaviors:**

- **Newsletter allowlist** — MVP only reads newsletters/senders selected by the user for a given digest
- **Strict link filtering** — removes unsubscribe, tracking, social-share, sponsor, ad, account-management, login, and non-content links before fetching
- **Primary/secondary source tracking** — primary source is the linked article; secondary source is the newsletter issue and message that surfaced it
- **Article fetching** — fetches linked articles that survive strict filtering and extracts readable title, text, publisher, author, and publish date when available
- **Fallback handling** — if the article cannot be fetched, uses the newsletter title/snippet/description, marks `fetch_status: fallback_snippet`, keeps the intended primary URL in metadata, and lowers ranking downstream
- **Deduplication** — URL-primary: if two newsletters surface the same primary URL, the article is stored once and both discoveries are preserved. For items without a URL, fuzzy deduplication uses embedding similarity on the title — items with cosine similarity above 0.92 are treated as duplicates
- **Watermarks** — stored per source in the `source_watermarks` table using both timestamp and source-native ID (e.g., Gmail `message_id`, RSS `entry_id`) to handle timezone edge cases and clock skew
- **PII filter** — runs before passing to Librarian; drops items containing sensitive keywords with `quality_flag: pii_detected`
- **Text truncation** — raw text is truncated to 4,000 tokens before passing to the Librarian to keep VRAM usage predictable
- **Gmail** — extracts body text and embedded links; each accepted content link becomes a normalized payload. The newsletter body is not submitted as an equal item unless needed as fallback context
- **Podcasts** — async queue, does not block main batch
- **Partial failure** — if an MCP source fails (e.g., mcp-gmail returns 401), the Digestor logs the failure, marks the run as `partial`, and continues with remaining sources; the Editor assembles a digest from whatever is available

-----

### 5.2 The Librarian

**Role:** Enrichment and classification. Receives normalized Digestor payloads and produces structured article records. Has no knowledge of user interests and no access to external sources.

**Processing pipeline per item:**

1. **Title normalization** — generates a clean canonical title via Qwen 27B
1. **Summary** — 2–4 sentence distillation via Qwen 27B
1. **Keywords** — topical, entity-based, and domain tags via Qwen 27B
1. **Content type** — classifies as `article | opinion | tutorial | podcast | newsletter | discussion`
1. **Embedding** — 768-dimension semantic vector via nomic-embed-text
1. **Quality signal** — flags low-quality or near-duplicate content

**Qwen 27B prompt pattern:**

```
You are a content librarian. Given the following text, return only a JSON object
with these fields:
  - title: canonical clean title for the primary article or fallback snippet
  - summary: 2-4 sentence summary of the content, not the source
  - keywords: array of 5-10 topical and entity tags
  - content_type: one of [article, opinion, tutorial, podcast, newsletter_fallback, discussion]
  - confidence_note: short note if the text came from a newsletter fallback

Return only valid JSON. No preamble, no markdown fences.
```

**Concurrency control:** The Librarian runs as a parallel map in LangGraph, but inference calls are gated by an `asyncio.Semaphore(value=4)` in the `ModelClient`. This prevents more than 4 simultaneous Qwen 27B inference calls, avoiding OOM conditions on the M3 Ultra.

**Output record written to SQLite:**

```json
{
  "id": "uuid",
  "article_id": "uuid",
  "run_id": "uuid",
  "digest_id": "uuid",
  "primary_source_name": "string",
  "primary_url": "string",
  "primary_domain": "string",
  "secondary_source_name": "string",
  "secondary_message_id": "string",
  "fetch_status": "fetched | blocked | paywalled | error | fallback_snippet",
  "published_at": "ISO8601",
  "title": "string",
  "summary": "string",
  "keywords": ["string"],
  "content_type": "string",
  "confidence_note": "string | null",
  "embedding": [0.0, "...768 floats"],
  "quality_flag": "ok | low | duplicate | pii_detected | error",
  "relevance_score": null,
  "tier": "main | lower_confidence | dropped",
  "created_at": "ISO8601"
}
```

**Partial failure handling:** If enrichment fails on an individual item, it is marked `quality_flag: error` and dropped from scoring. The run continues with the remaining items. A failed item count is recorded in `digest_runs` for observability.

-----

### 5.3 The Editor

**Role:** Orchestration, judgment, and publishing. The only agent with knowledge of the digest’s interest profile. It acts like the editorial desk for each digest run and produces the final HTML newspaper-style issue.

**Orchestration**

- Receives digest configuration from FastAPI (interest profile, source list, schedule)
- Translates interest description into Digestor source directives and approved newsletter queries
- Triggers and monitors batch pipeline via LangGraph graph execution

**Scoring**

- Embeds the user’s interest description using nomic-embed-text
- Computes cosine similarity between interest embedding and each item embedding
- At MVP scale (under ~500 items per run), similarity is computed in Python from SQLite BLOBs — fast enough with the candidate set bounded per run
- Combines similarity score with keyword overlap for a final relevance score (0.0–1.0)
- Applies relevance threshold (default: 0.45, configurable per digest)
- Items below 0.35 are dropped; items 0.35–0.45 go to the lower confidence section
- Keeps scoring independent per digest even when two digests reuse the same stored article

**Deduplication**

- Story-level deduplication across sources after scoring
- Selects the highest quality item per story cluster; others noted as alternate sources

**Assembly**

- Generates a 3–5 sentence snapshot summary of dominant themes via Qwen 27B
- Ranks surviving items by: relevance score (60%), recency (25%), source weight (15%)
- Groups articles into newspaper-style sections based on topic/theme
- Chooses lead stories and supporting stories for the issue
- Marks fallback items clearly when the linked article could not be fetched
- Structures output: masthead → snapshot → lead stories → topic sections → lower confidence section → source notes

**Publishing**

- Writes assembled digest issue metadata and rendered HTML to SQLite and/or the configured digest-output directory
- FastAPI serves the issue to the browser reading surface on localhost
- Partial run flag surfaced in UI if any sources failed

**Feedback loop**

- Accepts thumbs up/down per item from the UI
- Updates source weights and interest profile refinement signals in SQLite
- Signals compound over time, adjusting future ranking behavior for that digest only

-----

## 6. LangGraph Pipeline Topology

```
START
  │
  ▼
[load_digest_config]           # Pull digest definition and interest profile from SQLite
  │
  ▼
[check_corpus_threshold]       # Determine lookback window: standard or cold start
  │
  ▼
[run_digestor]                 # Fetch newsletters and normalize via MCP tool calls
  │                            # Partial failure: log and continue if a source fails
  ├── gmail_node               # MVP
  ├── rss_node                 # v1.1
  ├── reddit_node              # v1.2
  ├── search_node              # v1.3
  └── podcast_node             # v1.4 — async, non-blocking
  │
  ▼
[extract_newsletter_links]     # Pull links and newsletter snippets from approved senders
  │
  ▼
[strict_link_filter]           # Drop tracking, unsubscribe, ads, social, account links
  │
  ▼
[fetch_primary_articles]       # Fetch linked articles; fallback to newsletter snippet on failure
  │
  ▼
[build_source_attribution]     # Primary article source + secondary newsletter source
  │
  ▼
[pii_filter]                   # Drop items with sensitive content before enrichment
  │
  ▼
[deduplicate]                  # Store shared articles once; preserve all discoveries
  │
  ▼
[run_librarian]                # Enrich each item — parallel map, semaphore(4)
  │                            # Checkpointed: state persisted after each item
  ▼
[embed_interest_profile]       # Embed user interest description via nomic-embed-text
  │
  ▼
[score_items]                  # Cosine similarity + keyword overlap per item
  │
  ▼
[filter_and_cluster]           # Apply threshold, tier items, deduplicate stories
  │
  ▼
[generate_snapshot]            # Editor summary of dominant themes via Qwen 27B
  │
  ▼
[assemble_issue]               # Rank, group, choose leads, write HTML newspaper issue
  │
  ▼
END
```

**Checkpointing:** LangGraph’s checkpointer is enabled between `run_digestor` and `run_librarian`, and between `run_librarian` and `score_items`. If the pipeline fails mid-enrichment, it can resume from the last checkpoint rather than restarting the entire run.

-----

## 7. SQLite Schema

The schema separates shared article records from digest-specific judgment. This allows the same article to be stored once while each digest independently scores, ranks, labels, and learns from it.

### `profiles`

```sql
CREATE TABLE profiles (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  is_default  INTEGER DEFAULT 0,  -- boolean
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
```

### `digests`

```sql
CREATE TABLE digests (
  id           TEXT PRIMARY KEY,
  profile_id   TEXT NOT NULL REFERENCES profiles(id),
  name         TEXT NOT NULL,
  interest     TEXT NOT NULL,
  schedule     TEXT NOT NULL CHECK(schedule IN ('hourly','daily','weekly','monthly')),
  sources      TEXT NOT NULL,       -- JSON array of approved newsletter/source configs
  status       TEXT DEFAULT 'active',
  threshold    REAL DEFAULT 0.45,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
```

### `digest_runs`

```sql
CREATE TABLE digest_runs (
  id              TEXT PRIMARY KEY,
  digest_id       TEXT NOT NULL REFERENCES digests(id),
  run_at          TEXT NOT NULL,
  lookback_days   INTEGER NOT NULL,
  item_count      INTEGER DEFAULT 0,
  failed_count    INTEGER DEFAULT 0,  -- items that errored during enrichment
  fallback_count  INTEGER DEFAULT 0,  -- items based on newsletter snippet fallback
  cold_start      INTEGER DEFAULT 0,  -- boolean
  partial         INTEGER DEFAULT 0,  -- boolean: at least one source failed
  status          TEXT DEFAULT 'pending',
  snapshot        TEXT,
  completed_at    TEXT
);
```

### `articles`

Shared canonical article/content records. One article may appear in many digest runs.

```sql
CREATE TABLE articles (
  id              TEXT PRIMARY KEY,
  canonical_url   TEXT UNIQUE,
  original_url    TEXT,
  domain          TEXT,
  publisher       TEXT,
  author          TEXT,
  published_at    TEXT,
  title           TEXT,
  cleaned_text    TEXT,
  summary         TEXT,
  keywords        TEXT,               -- JSON array
  content_type    TEXT,
  embedding       BLOB,               -- serialized float32 array (768 dims)
  fetch_status    TEXT NOT NULL DEFAULT 'fetched',
  quality_flag    TEXT DEFAULT 'ok',  -- ok | low | duplicate | pii_detected | error
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
```

### `article_discoveries`

Secondary-source attribution. This records where an article was discovered, such as a specific Gmail newsletter issue.

```sql
CREATE TABLE article_discoveries (
  id                    TEXT PRIMARY KEY,
  article_id            TEXT NOT NULL REFERENCES articles(id),
  discovery_source_type TEXT NOT NULL,  -- gmail_newsletter | rss | reddit | web_search | podcast_feed
  discovery_source_name TEXT NOT NULL,  -- e.g. TLDR AI
  sender_email          TEXT,
  message_id            TEXT,
  thread_id             TEXT,
  issue_date            TEXT,
  link_text             TEXT,
  newsletter_snippet    TEXT,
  discovered_at         TEXT NOT NULL
);
```

### `digest_items`

Digest-specific judgment for a shared article. Feedback and ranking live here so each digest can learn independently.

```sql
CREATE TABLE digest_items (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES digest_runs(id),
  digest_id       TEXT NOT NULL REFERENCES digests(id),
  article_id      TEXT NOT NULL REFERENCES articles(id),
  discovery_id    TEXT REFERENCES article_discoveries(id),
  relevance_score REAL,
  tier            TEXT DEFAULT 'main', -- lead | main | lower_confidence | dropped
  section         TEXT,
  editor_summary  TEXT,
  editor_note     TEXT,
  created_at      TEXT NOT NULL
);
```

### `digest_issues`

Rendered newspaper-style issue for browser preview.

```sql
CREATE TABLE digest_issues (
  id           TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES digest_runs(id),
  digest_id    TEXT NOT NULL REFERENCES digests(id),
  title        TEXT NOT NULL,
  snapshot     TEXT,
  html_path    TEXT,
  html_content TEXT,        -- acceptable for MVP; can move to file-only later
  created_at   TEXT NOT NULL
);
```

### `feedback`

```sql
CREATE TABLE feedback (
  id             TEXT PRIMARY KEY,
  digest_item_id TEXT NOT NULL REFERENCES digest_items(id),
  article_id     TEXT NOT NULL REFERENCES articles(id),
  digest_id      TEXT NOT NULL REFERENCES digests(id),
  signal         TEXT NOT NULL CHECK(signal IN ('up','down')),
  created_at     TEXT NOT NULL
);
```

### `source_watermarks`

```sql
CREATE TABLE source_watermarks (
  digest_id      TEXT NOT NULL,
  source_key     TEXT NOT NULL,    -- e.g. gmail:newsletter@example.com
  last_fetched   TEXT NOT NULL,    -- ISO8601 timestamp
  last_id        TEXT,             -- source-native ID: Gmail message_id, RSS entry_id
  PRIMARY KEY (digest_id, source_key)
);
```

### `source_weights`

```sql
CREATE TABLE source_weights (
  digest_id   TEXT NOT NULL,
  source_name TEXT NOT NULL,
  weight      REAL DEFAULT 1.0,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (digest_id, source_name)
);
```

-----

## 8. API Contract (FastAPI)

### Profiles

|Method|Endpoint           |Description                                      |
|------|-------------------|-------------------------------------------------|
|GET   |`/api/profiles`    |List local profiles                              |
|POST  |`/api/profiles`    |Create a local profile; post-MVP unless needed   |
|GET   |`/api/profiles/{id}`|Get profile details                             |

### Digest Management

|Method|Endpoint               |Description         |
|------|-----------------------|--------------------|
|GET   |`/api/digests`         |List all digests    |
|POST  |`/api/digests`         |Create new digest   |
|GET   |`/api/digests/{id}`    |Get digest config   |
|PATCH |`/api/digests/{id}`    |Update digest config|
|DELETE|`/api/digests/{id}`    |Delete digest       |
|POST  |`/api/digests/{id}/run`|Trigger manual run  |

### Digest Runs & Reading

|Method|Endpoint                       |Description                                   |
|------|-------------------------------|----------------------------------------------|
|GET   |`/api/digests/{id}/runs`       |List past runs                                |
|GET   |`/api/digests/{id}/runs/latest`|Get latest published digest                   |
|GET   |`/api/runs/{run_id}`           |Get specific run with partial/cold-start flags|
|GET   |`/api/runs/{run_id}/items`     |Get all scored digest items for a run         |
|GET   |`/api/digests/{id}/issues/latest`|Get latest HTML newspaper issue metadata    |
|GET   |`/api/issues/{issue_id}`       |Get issue metadata and item structure         |
|GET   |`/api/issues/{issue_id}/html`  |Return rendered HTML newspaper issue          |

### Feedback

|Method|Endpoint                  |Description           |
|------|--------------------------|----------------------|
|POST  |`/api/items/{id}/feedback`|Submit up/down signal for a digest item |
|DELETE|`/api/items/{id}/feedback`|Remove feedback signal for a digest item|

### Sources

|Method|Endpoint               |Description                     |
|------|-----------------------|--------------------------------|
|GET   |`/api/sources/validate`|Test a Gmail sender, feed URL, Reddit source, or article URL|
|POST  |`/api/articles/fetch-preview`|Preview how a newsletter link would be cleaned and fetched|

-----

## 9. Scheduling

APScheduler manages all digest schedules. Each digest has its own independent job.

**Schedule cadences and lookback windows:**

|Schedule|Lookback (normal)|Lookback (cold start)|
|--------|-----------------|---------------------|
|Hourly  |1 hour           |24 hours             |
|Daily   |24 hours         |14 days              |
|Weekly  |7 days           |14 days              |
|Monthly |30 days          |14 days              |

**Cold start conditions:**

- First-ever run of a digest
- Corpus below minimum threshold (default: 10 scored items above relevance cutoff) after normal lookback
- Any run following a source outage that produced an empty corpus

**Cold start behavior:**

- Lookback window extended automatically
- Digest output flagged with “Warming Up” indicator in UI
- Normal cadence resumes once two consecutive runs exceed threshold

-----

## 10. Inference Layer

### Models

|Role                 |Model                |Backend|Notes                                                                                   |
|---------------------|---------------------|-------|----------------------------------------------------------------------------------------|
|Enrichment & assembly|Qwen 27B MTP         |MLX    |Confirmed running on M3 Ultra. Fallback: Qwen 2.5-32B-Instruct (well-tested MLX variant)|
|Embeddings           |nomic-embed-text-v1.5|MLX    |768-dim vectors, semantic similarity scoring                                            |
|Podcast transcription|Parakeet (primary)   |Local  |Benchmark against whisper.cpp before implementing mcp-podcastindex transcribe tool      |

### Model Client Abstraction

A `ModelClient` abstraction wraps all inference calls. Concurrency is controlled via `asyncio.Semaphore(value=4)` — a maximum of 4 simultaneous Qwen 27B calls at any time, preventing OOM on the M3 Ultra. The model is loaded once as a singleton session and held in memory for the duration of the pipeline run — never loaded and unloaded per request.

```python
class ModelClient:
    _semaphore = asyncio.Semaphore(4)  # max concurrent inference calls

    async def complete(self, prompt: str, system: str = None, model: str = None) -> str:
        async with self._semaphore:
            # call MLX backend
            ...

    async def embed(self, text: str) -> list[float]: ...
```

Backends: `MLXBackend` (primary), `CloudBackend` (fallback — Llama API or OpenRouter).

### Vector Search Scaling Path

|Corpus size         |Strategy                                                                                |
|--------------------|----------------------------------------------------------------------------------------|
|< 10,000 items      |Python cosine similarity on SQLite BLOBs — bounded per-run candidate set (max 500 items)|
|10,000–100,000 items|Migrate to `sqlite-vec` extension for indexed ANN search                                |
|> 100,000 items     |Evaluate dedicated vector store (Chroma, Qdrant local)                                  |

`sqlite-vec` is preferred over `sqlite-vss` for this use case — lighter, faster, and better Python bindings.

-----

## 11. Project Structure

```
morning-dispatch/
├── backend/
│   ├── main.py                    # FastAPI app entry point
│   ├── scheduler.py               # APScheduler — digest jobs + audio housekeeping
│   ├── agents/
│   │   ├── editor.py              # LangGraph graph definition
│   │   ├── digestor/
│   │   │   ├── base.py            # Normalized payload model + PII filter
│   │   │   ├── gmail.py           # MVP — calls mcp-gmail
│   │   │   ├── article_fetcher.py # MVP — calls mcp-article-fetcher
│   │   │   ├── rss.py             # v1.1 — calls mcp-rss
│   │   │   ├── reddit.py          # v1.2 — calls mcp-reddit
│   │   │   ├── search.py          # v1.3 — calls mcp-tavily
│   │   │   └── podcast.py         # v1.4 — calls mcp-podcastindex
│   │   └── librarian.py
│   ├── inference/
│   │   ├── client.py              # ModelClient abstraction + semaphore
│   │   ├── mlx_backend.py         # Singleton model session
│   │   └── cloud_backend.py       # Fallback
│   ├── db/
│   │   ├── schema.py
│   │   └── queries.py
│   └── api/
│       └── routes.py
├── mcp-servers/
│   ├── mcp-gmail/
│   │   ├── Dockerfile
│   │   └── server.py              # OAuth with token refresh handling
│   ├── mcp-article-fetcher/
│   │   ├── Dockerfile
│   │   └── server.py              # Strict link filtering + article extraction
│   ├── mcp-rss/
│   │   ├── Dockerfile
│   │   └── server.py
│   ├── mcp-reddit/
│   │   ├── Dockerfile
│   │   └── server.py
│   ├── mcp-podcastindex/
│   │   ├── Dockerfile
│   │   └── server.py              # Async transcription queue
│   └── mcp-tavily/
│       ├── Dockerfile
│       └── server.py
├── frontend/
│   └── src/                       # React app
├── config.example.yaml            # Safe config template
├── .env.example                   # Safe env template; real .env is gitignored
├── docker-compose.yml
├── .gitignore                     # Excludes .env, local config, caches, build output
└── README.md
```

Runtime data and secrets live outside the project folder under the configured data and secrets directories, defaulting to `~/.morning-dispatch/`.

Implementation note: the real `.env` file should use absolute paths such as `/Users/macstudio/.morning-dispatch/data` because Docker Compose does not reliably expand `~` in volume paths.

-----

## 12. Docker Compose Overview

```yaml
version: "3.9"

networks:
  dispatch-internal:
    internal: true
  dispatch-egress:
    internal: false

services:

  fastapi:
    build: ./backend
    ports:
      - "127.0.0.1:8000:8000"     # localhost only — never 0.0.0.0
    volumes:
      - ${MORNING_DISPATCH_DATA_DIR}/db:/data/db
      - ${MORNING_DISPATCH_DATA_DIR}/digest-output:/data/output
    networks: [dispatch-internal]
    depends_on: [mcp-gmail, mcp-article-fetcher]

  mcp-gmail:
    build: ./mcp-servers/mcp-gmail
    volumes:
      - ${MORNING_DISPATCH_SECRETS_DIR}/gmail:/secrets:ro
    networks: [dispatch-internal, dispatch-egress]

  mcp-article-fetcher:
    build: ./mcp-servers/mcp-article-fetcher
    volumes:
      - ${MORNING_DISPATCH_DATA_DIR}/article-cache:/article-cache
    networks: [dispatch-internal, dispatch-egress]

  mcp-rss:
    build: ./mcp-servers/mcp-rss
    networks: [dispatch-internal, dispatch-egress]

  mcp-reddit:
    build: ./mcp-servers/mcp-reddit
    volumes:
      - ${MORNING_DISPATCH_SECRETS_DIR}/reddit:/secrets:ro
    networks: [dispatch-internal, dispatch-egress]

  mcp-podcastindex:
    build: ./mcp-servers/mcp-podcastindex
    volumes:
      - ${MORNING_DISPATCH_SECRETS_DIR}/podcastindex:/secrets:ro
      - ${MORNING_DISPATCH_DATA_DIR}/podcast-audio:/podcast-audio
    networks: [dispatch-internal, dispatch-egress]

  mcp-tavily:
    build: ./mcp-servers/mcp-tavily
    volumes:
      - ${MORNING_DISPATCH_SECRETS_DIR}/tavily:/secrets:ro
    networks: [dispatch-internal, dispatch-egress]
```

-----

## 13. MVP Scope

The MVP implements the complete newspaper-style digest pipeline with approved Gmail newsletters as the only discovery source and linked articles as the primary content source.

**MVP delivers:**

- One local profile with one or more named digests
- Independent settings and feedback loop per digest
- Approved Gmail newsletter ingestion via mcp-gmail
- Strict newsletter link filtering
- Controlled article fetching for approved newsletter links
- Primary source metadata for the linked article
- Secondary source metadata for the newsletter issue that surfaced the article
- Fallback summaries from newsletter title/snippet/description when the article cannot be fetched
- Clear fallback labeling and lower ranking for not-fully-fetched items
- Cleaned article content and metadata storage
- No long-term storage of raw newsletter bodies, raw HTML, or tracking URLs
- Shared article storage across digests, with independent per-digest scoring and feedback
- PII filter before Librarian enrichment
- Librarian enrichment via Qwen 27B MLX (semaphore-controlled, singleton session)
- Semantic relevance scoring via nomic-embed-text
- Editor assembly into a locally served HTML newspaper-style issue
- Browser preview of the rendered issue on localhost
- Feedback buttons on digest items
- Scheduled and manual runs via APScheduler
- Partial-run indicator when a source or fetch step fails
- Cold start fallback logic
- LangGraph checkpointing for pipeline resilience
- Docker sandbox with localhost-only UI, controlled outbound fetching, and credential separation
- SQLite persistence with `last_id` watermarks
- Runtime data and secrets stored outside the project folder

**Post-MVP source additions (in order):**

1. RSS/Atom feeds (mcp-rss)
1. Reddit (mcp-reddit)
1. Web search (mcp-tavily)
1. Podcast transcription pipeline (mcp-podcastindex + Parakeet)
1. Multiple local profiles
1. Export/import flow for moving data between machines

-----

## 14. Open Questions

- **Article extraction strategy** — choose the MVP extraction library or service approach and define how to classify fetch failures such as blocked, paywalled, parse error, or timeout
- **HTML issue persistence** — decide whether MVP stores rendered HTML directly in SQLite, writes HTML files to `digest-output/`, or does both for easier debugging
- **Exact MLX model identifiers** — pin the model names and versions used for Qwen and embeddings before implementation so the build is reproducible
- **Parakeet vs whisper.cpp** — benchmark transcription speed and quality on M3 Ultra before implementing mcp-podcastindex transcribe tool; use the winner
- **Tavily API tier** — evaluate free tier query limits against expected daily volume before committing to a paid plan
- **sqlite-vec migration trigger** — monitor items table row count; migrate from Python cosine similarity to sqlite-vec when corpus exceeds 10,000 items
- **Source weight decay** — should weights drift back toward 1.0 over time in the absence of feedback signals? Prevents stale negative signals from permanently suppressing a source
- **Future profile support** — MVP includes a profile table, but profile switching UI and per-profile secret setup can wait until there is a real second local user
