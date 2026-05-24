#!/bin/zsh
set -euo pipefail

PROJECT_DIR="/Users/macstudio/Apps/personal_intel"
RUNTIME_HOME="/private/tmp/morning-dispatch-dev"

mkdir -p "$RUNTIME_HOME/data/db" "$RUNTIME_HOME/secrets/podcastindex" "$RUNTIME_HOME/secrets/podcasts" "$RUNTIME_HOME/logs"
cd "$PROJECT_DIR"

export MORNING_DISPATCH_HOME="$RUNTIME_HOME"
export MORNING_DISPATCH_DATA_DIR="$RUNTIME_HOME/data"
export MORNING_DISPATCH_SECRETS_DIR="$RUNTIME_HOME/secrets"
export MORNING_DISPATCH_DB_PATH="$RUNTIME_HOME/data/db/morning_dispatch.sqlite3"
export MORNING_DISPATCH_PUBLIC_BASE_URL="https://ultras-mac-studio-2.tail4aeef0.ts.net"
export MORNING_DISPATCH_GMAIL_REMOTE_MCP_ENABLED="false"
export MORNING_DISPATCH_LIBRARIAN_USE_MODEL="auto"
export MORNING_DISPATCH_MODEL_BASE_URL="http://127.0.0.1:1234/v1"
export MORNING_DISPATCH_LIBRARIAN_MODEL="Gemma-4 MTP 6Bit"
export MORNING_DISPATCH_LIBRARIAN_MODEL_MAX_ITEMS="120"
export MORNING_DISPATCH_MODEL_CONCURRENCY="1"
export MORNING_DISPATCH_MODEL_TIMEOUT_SECONDS="90"
export MORNING_DISPATCH_SCHEDULER_ENABLED="true"
export MORNING_DISPATCH_SCHEDULER_INTERVAL_SECONDS="300"
export MORNING_DISPATCH_SCHEDULER_DAILY_RUN_TIME="05:00"
export MORNING_DISPATCH_SCHEDULER_TIMEZONE="America/Los_Angeles"

if [[ -f "$RUNTIME_HOME/secrets/podcastindex/api_key" ]]; then
  export MORNING_DISPATCH_PODCASTINDEX_API_KEY="$(< "$RUNTIME_HOME/secrets/podcastindex/api_key")"
fi

if [[ -f "$RUNTIME_HOME/secrets/podcastindex/api_secret" ]]; then
  export MORNING_DISPATCH_PODCASTINDEX_API_SECRET="$(< "$RUNTIME_HOME/secrets/podcastindex/api_secret")"
fi

if [[ -f "$RUNTIME_HOME/secrets/podcasts/transcribe_command" ]]; then
  export MORNING_DISPATCH_PODCAST_TRANSCRIBE_COMMAND="$(< "$RUNTIME_HOME/secrets/podcasts/transcribe_command")"
fi

if [[ -f "/Users/macstudio/.omlx/settings.json" ]]; then
  export MORNING_DISPATCH_MODEL_API_KEY="$("$PROJECT_DIR/.venv/bin/python" -c 'import json; print(json.load(open("/Users/macstudio/.omlx/settings.json"))["auth"]["api_key"])')"
fi

exec "$PROJECT_DIR/.venv/bin/python" -m uvicorn backend.app.main:create_app --factory --host 127.0.0.1 --port 8000
