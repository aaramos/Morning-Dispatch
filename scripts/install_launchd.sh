#!/bin/zsh
set -euo pipefail

PLIST_SOURCE="/Users/macstudio/Apps/personal_intel/deploy/com.morning-dispatch.plist"
PLIST_TARGET="/Users/macstudio/Library/LaunchAgents/com.morning-dispatch.plist"
LABEL="com.morning-dispatch"
USER_ID="$(id -u)"
PROJECT_DIR="/Users/macstudio/Apps/personal_intel"

mkdir -p "/Users/macstudio/Library/LaunchAgents" "$PROJECT_DIR/runtime/logs"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
chmod +x "$PROJECT_DIR/scripts/run_morning_dispatch.sh"

launchctl bootout "gui/$USER_ID" "$PLIST_TARGET" 2>/dev/null || true
launchctl bootstrap "gui/$USER_ID" "$PLIST_TARGET"
launchctl enable "gui/$USER_ID/$LABEL"
launchctl kickstart -k "gui/$USER_ID/$LABEL"
launchctl print "gui/$USER_ID/$LABEL"
