#!/usr/bin/env bash
#
# Remove the launchd LaunchAgent for Local Codex Bridge + ngrok autostart.
#
# Default: unload the agent and delete ~/Library/LaunchAgents/com.local.codex-bridge.plist
# WITHOUT stopping anything. The plist uses AbandonProcessGroup, so the
# backgrounded bridge/ngrok processes are not tracked by launchd and keep
# running after bootout (their .runtime pid files stay valid).
#
# --stop   additionally run scripts/stop_ngrok_bridge.sh (only the recorded
#          pids are terminated; unmanaged processes are never touched).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LABEL="com.local.codex-bridge"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
STOP=0
[[ "${1:-}" == "--stop" ]] && STOP=1

log() { printf '[uninstall] %s\n' "$*"; }

if /bin/launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  if /bin/launchctl bootout "gui/$UID_NUM/$LABEL"; then
    log "agent unloaded: $LABEL"
  else
    log "warning: launchctl bootout failed for $LABEL"
  fi
else
  log "agent is not loaded; nothing to unload"
fi

if [[ -f "$DEST" ]]; then
  rm -f "$DEST"
  log "removed $DEST"
else
  log "no plist at $DEST"
fi

if [[ "$STOP" -eq 1 ]]; then
  log "stopping bridge/ngrok via stop_ngrok_bridge.sh (only recorded pids)"
  "$ROOT/scripts/stop_ngrok_bridge.sh"
else
  log "running bridge/ngrok left untouched (re-run with --stop to stop them)"
fi

log "done"
