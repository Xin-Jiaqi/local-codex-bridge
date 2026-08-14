#!/usr/bin/env bash
#
# Supervisor control helper for the local instance (sourceable AND runnable).
#
# The local bridge/ngrok stack is optionally managed by a foreground
# supervisor (scripts/run_local_supervisor.sh) under launchd
# (com.local.codex-bridge.local, KeepAlive PathState bound to the
# supervisor.enabled sentinel). This helper is the single place that:
#
#   - reads/writes the sentinel + supervisor pid in the LOCAL INSTANCE STATE
#     runtime dir (${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/
#     local/runtime/), never in the repo;
#   - enables: sentinel + launchd kickstart when the runtime agent is
#     installed, else the legacy start_ngrok_bridge.sh flow;
#   - disables: sentinel removal (the supervisor then stops its managed
#     children and exits 0); with --stop also stops children when no
#     supervisor is running;
#   - restores a recorded pre-window state (maintenance activate rollback /
#     deactivate).
#
# Only the explicit local instance is managed; hpc / maintenance / Para /
# Japan and remote jobs are NEVER touched. No pkill/killall: children are
# stopped via scripts/stop_ngrok_bridge.sh (PID identity verified before any
# signal); the sentinel/pid files are removed with guarded `rm -f` on the
# absolute instance-state paths only. No secrets/domain content is printed.
#
# Usage:
#   ./scripts/supervisor_control.sh status
#   ./scripts/supervisor_control.sh enable
#   ./scripts/supervisor_control.sh disable [--stop]
#   ./scripts/supervisor_control.sh restart [--stop]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"
. "$ROOT/scripts/pid_guard_lib.sh"

SUPERVISOR_AGENT_LABEL="${SUPERVISOR_AGENT_LABEL:-$(bridge_supervisor_label)}"
SUPERVISOR_INSTANCE="local"
SUPERVISOR_EXIT_WAIT_SECS="${SUPERVISOR_EXIT_WAIT_SECS:-30}"

log() { printf '[supervisor] %s\n' "$*"; }
die() { printf '[supervisor] error: %s\n' "$*" >&2; exit 1; }

# Prints the absolute sentinel path for the local instance (empty when the
# instance is not configured; supervisor_control is local-only).
supervisor_sentinel() {
  bridge_supervisor_enabled_file "$SUPERVISOR_INSTANCE"
}

supervisor_pid_file() {
  bridge_supervisor_pid_file "$SUPERVISOR_INSTANCE"
}

# Returns 0 when the local supervisor sentinel exists (enabled).
supervisor_enabled() {
  local sentinel
  sentinel="$(supervisor_sentinel)"
  [[ -n "$sentinel" && -f "$sentinel" ]]
}

# Prints the identity-verified supervisor pid (empty when none/stale).
supervisor_pid() {
  local file="" pid=""
  file="$(supervisor_pid_file)"
  [[ -n "$file" && -f "$file" ]] || { printf ''; return 0; }
  pid="$(cat "$file" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && managed_supervisor_pid "$pid"; then
    printf '%s' "$pid"
  else
    printf ''
  fi
}

# Returns 0 when the runtime launchd agent is currently loaded.
supervisor_agent_loaded() {
  command -v launchctl >/dev/null 2>&1 || return 1
  launchctl print "gui/$(id -u)/$SUPERVISOR_AGENT_LABEL" >/dev/null 2>&1
}

# Returns 0 when the runtime launchd agent plist is installed (or loaded).
supervisor_agent_installed() {
  supervisor_agent_loaded && return 0
  [[ -f "$HOME/Library/LaunchAgents/$SUPERVISOR_AGENT_LABEL.plist" ]]
}

# Wait up to MAX (default SUPERVISOR_EXIT_WAIT_SECS) seconds for the
# supervisor process to exit. Returns 0 when gone.
supervisor_wait_exit() {
  local max="${1:-$SUPERVISOR_EXIT_WAIT_SECS}" pid="" i
  pid="$(supervisor_pid)"
  [[ -n "$pid" ]] || return 0
  for i in $(seq 1 "$max"); do
    if ! managed_supervisor_pid "$pid"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# Enable supervision: create the sentinel, then start the stack through the
# launchd agent when installed (kickstart), else the legacy start flow.
supervisor_enable() {
  local sentinel="" runtime=""
  bridge_instance_exists "$SUPERVISOR_INSTANCE" || die \
    "local instance has no config ($(bridge_instance_config "$SUPERVISOR_INSTANCE")); run ./scripts/bridge_instance.sh migrate-current --apply first"
  runtime="$(bridge_instance_get "$SUPERVISOR_INSTANCE" runtime_dir)"
  [[ -n "$runtime" && "$runtime" == /* ]] || die "local runtime_dir missing from instance config"
  sentinel="$(supervisor_sentinel)"
  mkdir -p "$runtime"
  touch "$sentinel"
  log "sentinel created: $sentinel"
  if supervisor_agent_loaded; then
    launchctl kickstart -k "gui/$(id -u)/$SUPERVISOR_AGENT_LABEL" >/dev/null 2>&1 || true
    log "kickstarted launchd agent $SUPERVISOR_AGENT_LABEL"
  elif supervisor_agent_installed; then
    launchctl enable "gui/$(id -u)/$SUPERVISOR_AGENT_LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$SUPERVISOR_AGENT_LABEL.plist" >/dev/null 2>&1 || true
    launchctl kickstart "gui/$(id -u)/$SUPERVISOR_AGENT_LABEL" >/dev/null 2>&1 || true
    log "bootstrapped launchd agent $SUPERVISOR_AGENT_LABEL"
  else
    log "no launchd agent installed ($SUPERVISOR_AGENT_LABEL); falling back to the legacy start flow"
    BRIDGE_INSTANCE=local "$ROOT/scripts/start_ngrok_bridge.sh"
  fi
}

# Disable supervision: remove the sentinel (guarded absolute instance-state
# path). The running supervisor notices and safely stops its managed children
# before exiting 0. With --stop, also stop children when no supervisor is
# running (legacy-managed stack).
supervisor_disable() {
  local sentinel="" stop=0
  for arg in "$@"; do
    [[ "$arg" == "--stop" ]] && stop=1
  done
  sentinel="$(supervisor_sentinel)"
  [[ -n "$sentinel" ]] || die "cannot resolve the local supervisor sentinel (instance config missing?)"
  rm -f "$sentinel"
  log "sentinel removed: $sentinel"
  if ! supervisor_wait_exit; then
    log "warning: supervisor still running after sentinel removal; it will stop its managed children and exit"
  fi
  if [[ "$stop" == 1 ]]; then
    local spid=""
    spid="$(supervisor_pid)"
    if [[ -z "$spid" ]]; then
      BRIDGE_INSTANCE=local "$ROOT/scripts/stop_ngrok_bridge.sh"
    fi
  fi
}

# Restore a recorded pre-window supervisor state (enabled|disabled).
supervisor_restore() {
  local state="$1"
  case "$state" in
    enabled) supervisor_enable ;;
    disabled)
      local sentinel
      sentinel="$(supervisor_sentinel)"
      [[ -n "$sentinel" ]] || die "cannot resolve the local supervisor sentinel"
      rm -f "$sentinel"
      log "supervisor stays disabled (pre-window state was disabled)"
      ;;
    *) die "supervisor_restore: invalid state '$state' (enabled|disabled)" ;;
  esac
}

supervisor_status() {
  local sentinel="" pid="" current="" release="" agent="not installed"
  local lport="" ready="unknown"
  sentinel="$(supervisor_sentinel)"
  if [[ -n "$sentinel" && -f "$sentinel" ]]; then
    printf 'supervisor: enabled (%s)\n' "$sentinel"
  else
    printf 'supervisor: disabled\n'
  fi
  pid="$(supervisor_pid)"
  if [[ -n "$pid" ]]; then
    printf 'supervisor_pid: running (pid %s)\n' "$pid"
  else
    printf 'supervisor_pid: not running\n'
  fi
  lport="$(bridge_instance_get local port 2>/dev/null || true)"
  if [[ -n "$lport" ]]; then
    if bridge_health_ready "http://127.0.0.1:$lport" /dev/null 2>/dev/null; then
      ready="true"
    else
      ready="false"
    fi
  fi
  printf 'readiness: %s\n' "$ready"
  if supervisor_agent_loaded; then
    agent="loaded ($SUPERVISOR_AGENT_LABEL)"
  elif supervisor_agent_installed; then
    agent="installed ($SUPERVISOR_AGENT_LABEL)"
  fi
  printf 'agent: %s\n' "$agent"
  current="$(bridge_data_root)/current"
  if [[ -L "$current" ]]; then
    release="$(basename "$(readlink "$current" 2>/dev/null || true)")"
    printf 'runtime_source: %s\n' "$(readlink "$current")"
    printf 'release: %s\n' "${release:-<unknown>}"
  else
    printf 'runtime_source: none\n'
    printf 'release: none\n'
  fi
}

# ---------------------------------------------------------------------------
# CLI (guarded so sourcing this file has no side effects).
# ---------------------------------------------------------------------------
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  cmd="${1:-}"
  shift || true
  case "$cmd" in
    status) supervisor_status ;;
    enable) supervisor_enable ;;
    disable) supervisor_disable "$@" ;;
    restart)
      supervisor_disable "$@"
      supervisor_enable
      ;;
    *) echo "usage: supervisor_control.sh <status|enable|disable [--stop]|restart [--stop]>" >&2; exit 2 ;;
  esac
fi
