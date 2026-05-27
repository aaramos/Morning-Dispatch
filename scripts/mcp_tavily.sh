#!/bin/zsh
set -euo pipefail

SECRETS_HOME="${MORNING_DISPATCH_SECRETS_DIR:-/Users/macstudio/.morning-dispatch/secrets}"

if [[ -z "${TAVILY_API_KEY:-}" ]]; then
  if [[ -f "$SECRETS_HOME/tavily/api_key" ]]; then
    export TAVILY_API_KEY="$(< "$SECRETS_HOME/tavily/api_key")"
  fi
fi

if [[ -z "${TAVILY_API_KEY:-}" ]]; then
  echo "TAVILY_API_KEY is not configured." >&2
  exit 1
fi

exec npx -y mcp-remote https://mcp.tavily.com/mcp/ --header "Authorization: Bearer $TAVILY_API_KEY"
