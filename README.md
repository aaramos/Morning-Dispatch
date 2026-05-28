# Morning Dispatch

Morning Dispatch is a local-first personal intelligence app. The MVP turns approved Gmail newsletters into AI-curated, newspaper-style HTML digests served on localhost.

## Current Slice

This scaffold includes:

- FastAPI backend app
- SQLite schema for profiles, digests, shared articles, issues, and feedback
- Basic digest CRUD and manual run endpoints
- Admin Gmail OAuth screen at `/admin`
- Admin status screen for Gmail, scheduler, latest runs, and model-cache health
- Gmail newsletter ingestion from configured sender allowlists
- Strict newsletter-link filtering and tracking-parameter cleanup
- Linked-article fetching and readable-text extraction
- Librarian enrichment with local-model summaries, keywords, content type, and deterministic fallback
- Persistent cache for local-model article enrichment records
- Digest-specific article scoring, sectioning, and lead-story selection
- Optional in-process scheduler for active digests
- Newspaper-style HTML issue rendering with lead story, topic sections, lower-confidence items, and source notes
- React/Vite management UI shell
- Safe config examples with runtime data and secrets outside the project folder
- Exploration flow for ad hoc topic discovery and on-demand briefs (show-now or schedule)

The MVP is usable end to end: connect Gmail, run a digest, open the generated issue, and read the ranked article digest over localhost or the configured Tailscale HTTPS URL.

## Runtime Layout

The project folder is safe for GitHub. Runtime data and secrets are outside the repo:

```text
/Users/macstudio/Apps/personal_intel/
~/.morning-dispatch/data/
~/.morning-dispatch/secrets/
```

Use absolute paths in `.env`; Docker Compose does not reliably expand `~`.

## Backend

```bash
uv sync
MORNING_DISPATCH_HOME=/Users/macstudio/Apps/personal_intel/runtime \
MORNING_DISPATCH_DATA_DIR=/Users/macstudio/Apps/personal_intel/runtime/data \
MORNING_DISPATCH_SECRETS_DIR=/Users/macstudio/.morning-dispatch/secrets \
MORNING_DISPATCH_DB_PATH=/Users/macstudio/Apps/personal_intel/runtime/data/db/morning_dispatch.sqlite3 \
uv run uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

## Always-On Local Service

The local service uses launchd and keeps the backend available behind the existing Tailscale Serve mapping:

```bash
bash scripts/install_launchd.sh
```

It runs `scripts/run_morning_dispatch.sh`, enables scheduled digest checks, runs daily digests at `05:00` Pacific by default, and keeps runtime data under:

```text
/Users/macstudio/Apps/personal_intel/runtime/
```

Useful checks:

```bash
launchctl print gui/$(id -u)/com.morning-dispatch
curl http://127.0.0.1:8000/api/health
curl https://ultras-mac-studio-2.tail4aeef0.ts.net/api/health
```

## Admin Gmail Login

Open the admin screen at:

```text
http://127.0.0.1:8000/admin
```

From there you can upload a Google OAuth client secret JSON file and start the Gmail login. The app stores the resulting Gmail token outside the project folder:

```text
~/.morning-dispatch/secrets/gmail/gmail_credentials.json
```

Google OAuth redirect URLs generally need HTTPS unless they are localhost. For Tailscale use, set `MORNING_DISPATCH_PUBLIC_BASE_URL` to the HTTPS MagicDNS URL you registered in Google Cloud, for example:

```bash
MORNING_DISPATCH_PUBLIC_BASE_URL=https://ultras-mac-studio-2.tail4aeef0.ts.net \
MORNING_DISPATCH_HOST=0.0.0.0 \
uv run python -m backend.app.server
```

The admin API accepts requests from loopback and Tailscale client addresses only.

## Local Librarian Model

The Librarian can call an OpenAI-compatible local model endpoint, such as oMLX, for article title cleanup, summaries, keywords, and content type. If the model is unavailable, the digest run falls back to deterministic enrichment instead of failing.

```bash
MORNING_DISPATCH_LIBRARIAN_USE_MODEL=auto \
MORNING_DISPATCH_MODEL_BASE_URL=http://127.0.0.1:1234/v1 \
MORNING_DISPATCH_LIBRARIAN_MODEL="Gemma-4 MTP 6Bit" \
MORNING_DISPATCH_LIBRARIAN_MODEL_MAX_ITEMS=250 \
MORNING_DISPATCH_MODEL_CONCURRENCY=1 \
MORNING_DISPATCH_MODEL_TIMEOUT_SECONDS=90 \
MORNING_DISPATCH_MODEL_API_KEY=... \
uv run python -m backend.app.server
```

## Explore an Interest

Morning Dispatch now supports a second flow for one-off exploration:

- Start with a plain-English statement in the frontend Explore panel.
- The refinement chat gathers a minimal topic profile (`scope`, `depth`, `recency_weighting`, `exclusions`).
- Run immediately (“show now”) for a rendered brief, then optionally save and mail it.
- Or save a `topic profile` and schedule it for recurring exploration.

The same digest core does the candidate ranking and brief quality checks, so scheduled and ad-hoc flows stay consistent.

## Web Search Adapter

Discovery can use pluggable web search providers for discovery-only candidates:

- `tavily`
- `brave`
- `serpapi`

Configure with environment variables (or equivalent secret files in `${MORNING_DISPATCH_SECRETS_DIR}`):

```bash
MORNING_DISPATCH_WEB_SEARCH_PROVIDER=auto \
MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH=/Users/macstudio/.hermes/.env \
MORNING_DISPATCH_TAVILY_API_KEY=... \
MORNING_DISPATCH_BRAVE_API_KEY=... \
MORNING_DISPATCH_SERPAPI_API_KEY=...
```

`auto` selects the first configured key in this order: `tavily`, `brave`, `serpapi`.
Setting a specific provider name forces that adapter.
If no Morning Dispatch key is saved, the app also checks the shared search env file for names like
`TAVILY_API_KEY`, `BRAVE_SEARCH_API_KEY`, and `SERPAPI_API_KEY`.

## YouTube Adapter

YouTube can be enabled as an optional source for on-demand briefs. It uses the YouTube Data API for discovery, then only includes videos when a native transcript is available.

```bash
MORNING_DISPATCH_YOUTUBE_API_KEY=... \
MORNING_DISPATCH_YOUTUBE_MAX_RESULTS=15 \
MORNING_DISPATCH_YOUTUBE_DURATION_FILTER=medium
```

You can also paste the API key in Admin -> Sources. YouTube is off by default and only runs when selected for a brief.

## Collections Adapter

Collections is an optional local source for briefs. First slice support indexes text-like files from top-level folders under the Collections root and sends relevant chunks into the shared brief pipeline.

```bash
MORNING_DISPATCH_COLLECTIONS_ROOT=/Users/macstudio/Documents/Collections \
MORNING_DISPATCH_COLLECTIONS_MAX_RESULTS=12 \
MORNING_DISPATCH_COLLECTIONS_MAX_FILE_BYTES=1000000
```

Create the folder from Admin -> Sources or from the inline source setup card. Add top-level folders inside it, then place `.txt`, `.md`, `.csv`, `.json`, `.yaml`, or `.html` files inside those folders.

## Markets Adapter

Markets is an optional source for public-company context. First slice support runs in Simple mode with free Yahoo Finance data through `yfinance`; no API key is required.

```bash
MORNING_DISPATCH_MARKETS_MODE=simple \
MORNING_DISPATCH_MARKETS_MAX_CORE_COMPANIES=5 \
MORNING_DISPATCH_MARKETS_MAX_RELATED_COMPANIES=5
```

The source selects relevant public companies from the topic, fetches recent price movement, market cap, analyst rating, sector, and recent news, then sends company snapshots into the shared brief pipeline.

## Frontend

```bash
npm --cache .npm-cache install
npm --cache .npm-cache run dev
```

The Vite dev server proxies API requests to `http://127.0.0.1:8000`.

## Tests

```bash
uv run pytest
npm --cache .npm-cache run build
```
