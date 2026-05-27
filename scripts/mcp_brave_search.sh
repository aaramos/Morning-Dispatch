#!/bin/zsh
set -euo pipefail

SECRETS_HOME="${MORNING_DISPATCH_SECRETS_DIR:-/Users/macstudio/.morning-dispatch/secrets}"
SHARED_ENV_PATH="${MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH:-/Users/macstudio/.hermes/.env}"

read_env_value() {
  local key="$1"
  local file="$2"
  [[ -f "$file" ]] || return 1
  awk -F= -v wanted="$key" '
    $0 ~ /^[[:space:]]*#/ { next }
    {
      line=$0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      split(line, parts, "=")
      name=parts[1]
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
      if (name == wanted) {
        value=substr(line, index(line, "=") + 1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
        sub(/^"/, "", value)
        sub(/"$/, "", value)
        sub(/^\047/, "", value)
        sub(/\047$/, "", value)
        print value
        exit 0
      }
    }
  ' "$file"
}

if [[ -z "${BRAVE_API_KEY:-}" ]]; then
  if [[ -f "$SECRETS_HOME/brave/api_key" ]]; then
    export BRAVE_API_KEY="$(< "$SECRETS_HOME/brave/api_key")"
  else
    BRAVE_API_KEY="$(read_env_value BRAVE_API_KEY "$SHARED_ENV_PATH" || read_env_value BRAVE_SEARCH_API_KEY "$SHARED_ENV_PATH" || true)"
    export BRAVE_API_KEY
  fi
fi

if [[ -z "${BRAVE_API_KEY:-}" ]]; then
  echo "BRAVE_API_KEY is not configured." >&2
  exit 1
fi

exec npx -y @modelcontextprotocol/server-brave-search
