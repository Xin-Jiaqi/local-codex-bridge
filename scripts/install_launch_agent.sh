#!/usr/bin/env bash
#
# Install the launchd LaunchAgent that autostarts Local Codex Bridge + ngrok
# at login (RunAtLoad; reuse of scripts/start_ngrok_bridge.sh).
#
# - Generates ~/Library/LaunchAgents/com.local.codex-bridge.plist from the
#   template in scripts/launch_agent/ (placeholders __PROJECT_ROOT__ and
#   __HOME__ are replaced with absolute paths; no secrets in the repo).
# - Loads the agent for the current user. RunAtLoad starts the bridge script,
#   which is idempotent and reuses already-running Bridge/ngrok processes,
#   so an install never stops or restarts anything.
# - If the agent is already loaded: prints a notice and exits 0. Pass
#   --force to boot it out and reload (e.g. after editing the template).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LABEL="com.local.codex-bridge"
TEMPLATE="$ROOT/scripts/launch_agent/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] error: %s\n' "$*" >&2; exit 1; }

[[ -f "$TEMPLATE" ]] || die "template not found: $TEMPLATE"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/.runtime"

# Exact string replacement via python3 (a project prerequisite); avoids sed
# delimiter/escaping issues with unusual paths.
python3 - "$TEMPLATE" "$DEST" "$ROOT" "$HOME" <<'PY'
import sys
src, dst, root, home = sys.argv[1:5]
data = open(src, encoding="utf-8").read()
data = data.replace("__PROJECT_ROOT__", root).replace("__HOME__", home)
with open(dst, "w", encoding="utf-8") as f:
    f.write(data)
PY
chmod 644 "$DEST"

/usr/bin/plutil -lint "$DEST" >/dev/null || die "generated plist failed lint: $DEST"
log "plist written to $DEST"

if /bin/launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  if [[ "$FORCE" -eq 1 ]]; then
    log "agent already loaded; booting it out first (--force)"
    /bin/launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
  else
    log "agent already loaded; nothing to do (use --force to reload)"
    exit 0
  fi
fi

/bin/launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
if ! /bin/launchctl bootstrap "gui/$UID_NUM" "$DEST"; then
  die "launchctl bootstrap failed (see $ROOT/.runtime/launchagent.err.log); run ./scripts/status_launch_agent.sh"
fi

log "LaunchAgent installed and loaded: $LABEL"
log "started now (RunAtLoad) and will start automatically at next login"
log "check status with: ./scripts/status_launch_agent.sh"
