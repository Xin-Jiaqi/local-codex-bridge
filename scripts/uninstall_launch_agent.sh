#!/usr/bin/env bash
#
# Remove the launchd LaunchAgent for Local Codex Bridge.
#
# New model: --instance local removes the per-instance supervisor agent
# com.local.codex-bridge.local: the agent is booted out (launchd TERMs the
# foreground supervisor, which safely stops its managed bridge/ngrok children
# and exits 0), the plist is deleted and the supervisor.enabled sentinel is
# removed (guarded instance-state path). Instance state, CODEX_HOME, the
# runtime releases and config-root credentials are preserved.
#
# Legacy: without --instance (or with --instance hpc|maintenance) the
# matching com.local.codex-bridge[.<name>] agent is unloaded and its plist
# removed WITHOUT stopping anything unless --stop is given.
#
# --stop additionally runs scripts/stop_ngrok_bridge.sh (only the recorded
# pids are terminated; unmanaged processes are never touched). No
# pkill/killall.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"

LABEL="com.local.codex-bridge"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
STOP=0
INSTANCE=""

log() { printf '[uninstall] %s\n' "$*"; }
die() { printf '[uninstall] error: %s\n' "$*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stop) STOP=1 ;;
    --instance)
      [[ $# -ge 2 ]] || die "--instance requires a value"
      INSTANCE="$2"; shift
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

RUNTIME_DIR=""
if [[ -n "$INSTANCE" ]]; then
  bridge_instance_valid "$INSTANCE" || die "--instance=$INSTANCE is invalid (use local|hpc|maintenance)"
  if [[ "$INSTANCE" == "local" ]]; then
    LABEL="$(bridge_supervisor_label)"
    RUNTIME_DIR="$(bridge_instance_get local runtime_dir 2>/dev/null || true)"
  else
    LABEL="com.local.codex-bridge.$INSTANCE"
  fi
  DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
fi

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  if launchctl bootout "gui/$UID_NUM/$LABEL"; then
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

# Local supervisor agent: remove the enabled sentinel so launchd never
# restarts the supervisor (guarded absolute instance-state path only), and
# clear any stale maintenance pause marker so a leftover window hold can
# never pause a future supervisor.
if [[ -n "$RUNTIME_DIR" && -f "$RUNTIME_DIR/supervisor.enabled" ]]; then
  rm -f "$RUNTIME_DIR/supervisor.enabled"
  log "removed supervisor enabled sentinel: $RUNTIME_DIR/supervisor.enabled"
fi
PAUSE_MARKER="$(bridge_pause_marker 2>/dev/null || true)"
if [[ -n "$PAUSE_MARKER" && -f "$PAUSE_MARKER" ]]; then
  rm -f "$PAUSE_MARKER"
  log "removed stale maintenance pause marker: $PAUSE_MARKER"
fi

if [[ "$STOP" -eq 1 ]]; then
  log "stopping bridge/ngrok via stop_ngrok_bridge.sh (only recorded pids)"
  if [[ -n "$INSTANCE" ]]; then
    BRIDGE_INSTANCE="$INSTANCE" "$ROOT/scripts/stop_ngrok_bridge.sh"
  else
    "$ROOT/scripts/stop_ngrok_bridge.sh"
  fi
else
  log "running bridge/ngrok left untouched (re-run with --stop to stop them)"
fi

log "done"
