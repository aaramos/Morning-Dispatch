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
MORNING_DISPATCH_HOME=/private/tmp/morning-dispatch-dev \
MORNING_DISPATCH_DATA_DIR=/private/tmp/morning-dispatch-dev/data \
MORNING_DISPATCH_SECRETS_DIR=/private/tmp/morning-dispatch-dev/secrets \
MORNING_DISPATCH_DB_PATH=/private/tmp/morning-dispatch-dev/data/db/morning_dispatch.sqlite3 \
uv run uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

## Always-On Local Service

The local service uses launchd and keeps the backend available behind the existing Tailscale Serve mapping:

```bash
bash scripts/install_launchd.sh
```

It runs `scripts/run_morning_dispatch.sh`, enables scheduled digest checks, runs daily digests at `05:00` Pacific by default, and keeps runtime data under:

```text
/private/tmp/morning-dispatch-dev/
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
MORNING_DISPATCH_PUBLIC_BASE_URL=https://ultras-mac-studio-2.tail4aeef0.ts.net:8000 \
MORNING_DISPATCH_HOST=100.113.204.75 \
uv run python -m backend.app.server
```

The admin API accepts requests from loopback and Tailscale client addresses only.

## Local Librarian Model

The Librarian can call an OpenAI-compatible local model endpoint, such as oMLX, for article title cleanup, summaries, keywords, and content type. If the model is unavailable, the digest run falls back to deterministic enrichment instead of failing.

```bash
MORNING_DISPATCH_LIBRARIAN_USE_MODEL=auto \
MORNING_DISPATCH_MODEL_BASE_URL=http://127.0.0.1:1234/v1 \
MORNING_DISPATCH_LIBRARIAN_MODEL="Gemma-4 MTP 6Bit" \
MORNING_DISPATCH_LIBRARIAN_MODEL_MAX_ITEMS=120 \
MORNING_DISPATCH_MODEL_CONCURRENCY=1 \
MORNING_DISPATCH_MODEL_TIMEOUT_SECONDS=90 \
MORNING_DISPATCH_MODEL_API_KEY=... \
uv run python -m backend.app.server
```

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
