#!/bin/zsh
set -euo pipefail
umask 077

PROJECT_DIR="/Users/macstudio/Apps/personal_intel"
RUNTIME_HOME="$PROJECT_DIR/runtime"
SECRETS_HOME="${MORNING_DISPATCH_SECRETS_DIR:-/Users/macstudio/.morning-dispatch/secrets}"

mkdir -p \
  "$RUNTIME_HOME/data/db" \
  "$RUNTIME_HOME/logs" \
  "$SECRETS_HOME/gmail" \
  "$SECRETS_HOME/podcastindex" \
  "$SECRETS_HOME/podcasts" \
  "$SECRETS_HOME/tavily" \
  "$SECRETS_HOME/brave" \
  "$SECRETS_HOME/serpapi" \
  "$SECRETS_HOME/youtube" \
  "$SECRETS_HOME/ollama"
chmod 700 \
  "$SECRETS_HOME" \
  "$SECRETS_HOME/gmail" \
  "$SECRETS_HOME/podcastindex" \
  "$SECRETS_HOME/podcasts" \
  "$SECRETS_HOME/tavily" \
  "$SECRETS_HOME/brave" \
  "$SECRETS_HOME/serpapi" \
  "$SECRETS_HOME/youtube" \
  "$SECRETS_HOME/ollama"
cd "$PROJECT_DIR"

export MORNING_DISPATCH_HOME="$RUNTIME_HOME"
export MORNING_DISPATCH_DATA_DIR="$RUNTIME_HOME/data"
export MORNING_DISPATCH_SECRETS_DIR="$SECRETS_HOME"
export MORNING_DISPATCH_DB_PATH="$RUNTIME_HOME/data/db/morning_dispatch.sqlite3"
export MORNING_DISPATCH_GMAIL_CLIENT_SECRET_PATH="${MORNING_DISPATCH_GMAIL_CLIENT_SECRET_PATH:-$SECRETS_HOME/gmail/gmail_client_secret.json}"
export MORNING_DISPATCH_GMAIL_CREDENTIALS_PATH="${MORNING_DISPATCH_GMAIL_CREDENTIALS_PATH:-$SECRETS_HOME/gmail/gmail_credentials.json}"
export MORNING_DISPATCH_PUBLIC_BASE_URL="https://ultras-mac-studio-3.tail4aeef0.ts.net"
export MORNING_DISPATCH_GMAIL_REMOTE_MCP_ENABLED="false"
export MORNING_DISPATCH_LIBRARIAN_USE_MODEL="auto"
export MORNING_DISPATCH_MODEL_BASE_URL="http://127.0.0.1:1234/v1"
export MORNING_DISPATCH_LIBRARIAN_MODEL="Gemma4-MTP-26B-BF16"
export MORNING_DISPATCH_LIBRARIAN_MODEL_MAX_ITEMS="150"
export MORNING_DISPATCH_YOUTUBE_MAX_RESULTS="40"
export MORNING_DISPATCH_COLLECTIONS_MAX_RESULTS="50"
export MORNING_DISPATCH_MARKETS_MAX_CORE_COMPANIES="10"
export MORNING_DISPATCH_MARKETS_MAX_RELATED_COMPANIES="10"
export MORNING_DISPATCH_MODEL_CONCURRENCY="1"
export MORNING_DISPATCH_MODEL_TIMEOUT_SECONDS="90"
export MORNING_DISPATCH_SCHEDULER_ENABLED="true"
export MORNING_DISPATCH_SCHEDULER_INTERVAL_SECONDS="300"
export MORNING_DISPATCH_SCHEDULER_DAILY_RUN_TIME="05:00"
export MORNING_DISPATCH_SCHEDULER_TIMEZONE="America/Los_Angeles"
export MORNING_DISPATCH_HOST="${MORNING_DISPATCH_HOST:-0.0.0.0}"
export MORNING_DISPATCH_PORT="${MORNING_DISPATCH_PORT:-8000}"

if [[ -z "${MORNING_DISPATCH_PODCASTINDEX_API_KEY:-}" && -f "$SECRETS_HOME/podcastindex/api_key" ]]; then
  export MORNING_DISPATCH_PODCASTINDEX_API_KEY="$(< "$SECRETS_HOME/podcastindex/api_key")"
fi

if [[ -z "${MORNING_DISPATCH_PODCASTINDEX_API_SECRET:-}" && -f "$SECRETS_HOME/podcastindex/api_secret" ]]; then
  export MORNING_DISPATCH_PODCASTINDEX_API_SECRET="$(< "$SECRETS_HOME/podcastindex/api_secret")"
fi

if [[ -z "${MORNING_DISPATCH_PODCAST_TRANSCRIBE_COMMAND:-}" && -f "$SECRETS_HOME/podcasts/transcribe_command" ]]; then
  export MORNING_DISPATCH_PODCAST_TRANSCRIBE_COMMAND="$(< "$SECRETS_HOME/podcasts/transcribe_command")"
fi

if [[ -z "${MORNING_DISPATCH_YOUTUBE_API_KEY:-}" && -f "$SECRETS_HOME/youtube/api_key" ]]; then
  export MORNING_DISPATCH_YOUTUBE_API_KEY="$(< "$SECRETS_HOME/youtube/api_key")"
fi

if [[ -z "${MORNING_DISPATCH_OLLAMA_API_KEY:-}" && -f "$SECRETS_HOME/ollama/api_key" ]]; then
  export MORNING_DISPATCH_OLLAMA_API_KEY="$(< "$SECRETS_HOME/ollama/api_key")"
fi

if [[ -f "/Users/macstudio/.omlx/settings.json" ]]; then
  export MORNING_DISPATCH_MODEL_API_KEY="$("$PROJECT_DIR/.venv/bin/python" -c 'import json; print(json.load(open("/Users/macstudio/.omlx/settings.json"))["auth"]["api_key"])')"
fi

exec "$PROJECT_DIR/.venv/bin/python" -m uvicorn backend.app.main:create_app --factory --host "$MORNING_DISPATCH_HOST" --port "$MORNING_DISPATCH_PORT"
