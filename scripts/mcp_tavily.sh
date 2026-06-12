#!/bin/zsh
set -euo pipefail

exec node "$(dirname "$0")/mcp_tavily_secure.js" \
  2> >(
    sed -E \
      -e 's/(Authorization":"Bearer )[A-Za-z0-9_+=\/.-]+/\1[REDACTED]/g' \
      -e 's/tvly-[A-Za-z0-9_-]+/[REDACTED_TAVILY_KEY]/g' >&2
  )

