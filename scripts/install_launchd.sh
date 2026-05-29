#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.morning-dispatch.plist"
LABEL="com.morning-dispatch"
USER_ID="$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/runtime/logs"

# Generate the plist from the project directory derived at install time.
cat > "$PLIST_TARGET" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.morning-dispatch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$PROJECT_DIR/scripts/run_morning_dispatch.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>$PROJECT_DIR</string>
  <key>StandardOutPath</key>
  <string>$PROJECT_DIR/runtime/logs/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$PROJECT_DIR/runtime/logs/launchd.err.log</string>
</dict>
</plist>
PLIST

chmod +x "$PROJECT_DIR/scripts/run_morning_dispatch.sh"

launchctl bootout "gui/$USER_ID" "$PLIST_TARGET" 2>/dev/null || true
launchctl bootstrap "gui/$USER_ID" "$PLIST_TARGET"
launchctl enable "gui/$USER_ID/$LABEL"
launchctl kickstart -k "gui/$USER_ID/$LABEL"
launchctl print "gui/$USER_ID/$LABEL"
