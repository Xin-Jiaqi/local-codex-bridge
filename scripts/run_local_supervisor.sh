#!/usr/bin/env bash
#
# Foreground supervisor for the LOCAL instance bridge/ngrok stack, designed
# to run under launchd (com.local.codex-bridge.local, RunAtLoad + KeepAlive
# PathState bound to the supervisor.enabled sentinel) and to be invoked from
# the stable runtime copy (NOT from the repo Desktop path):
#
#   ${XDG_DATA_HOME:-$HOME/.local/share}/local-codex-bridge/current/scripts/\
#     run_local_supervisor.sh --instance local
#
# Behavior:
#   - ONLY the explicit local instance is managed (--instance local required;
#     hpc / maintenance / Para / Japan and remote jobs are never touched);
#   - runs in the foreground; exits 0 immediately when the supervisor.enabled
#     sentinel (instance-state runtime) is absent;
#   - records its own pid in the instance-state supervisor.pid file;
#   - reads the maintenance pause marker under the local instance state
#     (<state>/local/pause.marker): while it exists the managed children are
#     stopped and NOT restarted; the supervisor itself stays alive (sentinel
#     untouched, so launchd does not see an exit and there is no crash-loop).
#     When the marker is removed the stack is resumed;
#   - calls the existing start_ngrok_bridge.sh and then watches the managed
#     bridge/ngrok PIDs (PID identity verified read-only; see
#     scripts/pid_guard_lib.sh). Any child that truly exits is restarted
#     through the idempotent start script with throttled/backed-off retries;
#     transient public-health / network failures never kill a process;
#   - when the sentinel disappears or TERM/INT arrives: safely stops the
#     managed children (stop_ngrok_bridge.sh, identity-guarded), removes the
#     supervisor.pid file and exits 0 (launchd sees a clean shutdown);
#   - abnormal exit codes are handed back to launchd (which restarts the job
#     while the PathState sentinel exists, with a ThrottleInterval).
#
# No pkill/killall; no unbounded rm -rf (only `rm -f` on the two absolute
# instance-state files). No secrets/domain content is printed.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"
. "$ROOT/scripts/pid_guard_lib.sh"

INSTANCE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance)
      [[ $# -ge 2 ]] || { echo "[supervisor] error: --instance requires a value" >&2; exit 2; }
      INSTANCE="$2"
      shift
      ;;
    *) echo "[supervisor] error: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

[[ "$INSTANCE" == "local" ]] || {
  echo "[supervisor] error: run_local_supervisor.sh only manages the explicit local instance (got '${INSTANCE:-<none>}'); hpc/maintenance/legacy are never auto-managed" >&2
  exit 2
}
export BRIDGE_INSTANCE=local

bridge_instance_exists local || {
  echo "[supervisor] error: local instance has no config ($(bridge_instance_config local)); run ./scripts/bridge_instance.sh migrate-current --apply first" >&2
  exit 2
}

RUNTIME_DIR="$(bridge_instance_get local runtime_dir)"
[[ -n "$RUNTIME_DIR" && "$RUNTIME_DIR" == /* ]] || {
  echo "[supervisor] error: local runtime_dir missing from instance config" >&2
  exit 2
}
SENTINEL="$RUNTIME_DIR/supervisor.enabled"
PID_FILE="$RUNTIME_DIR/supervisor.pid"
BRIDGE_PID_FILE="$RUNTIME_DIR/bridge.pid"
NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"
PAUSE_FILE="$(bridge_instance_dir local)/pause.marker"
BRIDGE_PORT="$(bridge_instance_get local port)"
SUPERVISOR_LOG="$RUNTIME_DIR/supervisor.log"

log() { printf '[supervisor] %s\n' "$*"; }
die() { printf '[supervisor] error: %s\n' "$*" >&2; exit 1; }

mkdir -p "$RUNTIME_DIR"

[[ -f "$SENTINEL" ]] || {
  log "supervisor disabled ($SENTINEL absent); exiting 0"
  exit 0
}

# Single-supervisor guard: never run two supervisors for the same instance.
if [[ -f "$PID_FILE" ]]; then
  other="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$other" ]] && is_valid_pid "$other" && managed_supervisor_pid "$other"; then
    die "another supervisor is already running for local (pid $other)"
  fi
fi

STOPPING=0

cleanup() {
  local rc=$?
  rm -f "$PID_FILE" 2>/dev/null || true
  BRIDGE_INSTANCE=local "$ROOT/scripts/stop_ngrok_bridge.sh" >/dev/null 2>&1 || true
  if [[ "$STOPPING" == 1 ]]; then
    exit 0
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'STOPPING=1; exit 0' TERM INT

echo "$$" > "$PID_FILE"
log "supervisor pid $$ recorded in $PID_FILE"

POLL="${SUPERVISOR_POLL_SECS:-2}"
BACKOFF="${SUPERVISOR_BACKOFF_SECS:-5}"
MAX_BACKOFF="${SUPERVISOR_MAX_BACKOFF_SECS:-60}"

stop_children() {
  BRIDGE_INSTANCE=local "$ROOT/scripts/stop_ngrok_bridge.sh" >>"$SUPERVISOR_LOG" 2>&1 || true
}

PAUSED=0
FAILS=0
if [[ -f "$PAUSE_FILE" ]]; then
  PAUSED=1
  log "pause marker present at startup ($PAUSE_FILE); managed children stay stopped until it is removed"
else
  log "supervisor enabled; starting the managed stack (instance=local)"
  if ! BRIDGE_INSTANCE=local "$ROOT/scripts/start_ngrok_bridge.sh" >>"$SUPERVISOR_LOG" 2>&1; then
    log "initial start failed (see $SUPERVISOR_LOG); the monitor will retry with backoff"
    FAILS=1
  fi
fi

while [[ -f "$SENTINEL" ]]; do
  # Maintenance window hold: stop the managed children and WAIT (never
  # restart while the marker exists). The supervisor stays alive so launchd
  # does not see a crash-loop; resuming happens as soon as the marker is gone.
  if [[ -f "$PAUSE_FILE" ]]; then
    if [[ "$PAUSED" != 1 ]]; then
      PAUSED=1
      FAILS=0
      log "pause marker detected ($PAUSE_FILE); stopping the managed children and waiting"
      stop_children
    fi
    sleep "$POLL"
    continue
  fi
  if [[ "$PAUSED" == 1 ]]; then
    PAUSED=0
    FAILS=0
    log "pause marker removed; resuming the managed stack"
    if BRIDGE_INSTANCE=local "$ROOT/scripts/start_ngrok_bridge.sh" >>"$SUPERVISOR_LOG" 2>&1; then
      log "managed stack resumed"
    else
      FAILS=1
      log "resume start failed (see $SUPERVISOR_LOG); the monitor will retry with backoff"
    fi
    sleep "$POLL"
    continue
  fi
  need_restart=0
  bpid="$(cat "$BRIDGE_PID_FILE" 2>/dev/null || true)"
  if ! managed_bridge_pid "$bpid" "$ROOT" "$RUNTIME_DIR"; then
    need_restart=1
  fi
  npid="$(cat "$NGROK_PID_FILE" 2>/dev/null || true)"
  if ! managed_ngrok_pid "$npid" "$BRIDGE_PORT"; then
    need_restart=1
  fi
  if [[ "$need_restart" == 1 ]]; then
    FAILS=$((FAILS + 1))
    wait="$BACKOFF"
    if [[ "$FAILS" -gt 1 ]]; then
      mult=1
      for _ in $(seq 2 "$FAILS"); do
        mult=$((mult * 2))
        [[ $((wait * mult)) -ge $MAX_BACKOFF ]] && break
      done
      wait=$((wait * mult))
      [[ "$wait" -gt "$MAX_BACKOFF" ]] && wait="$MAX_BACKOFF"
    fi
    log "managed child missing (attempt #$FAILS); retrying start in ${wait}s"
    sleep "$wait"
    if BRIDGE_INSTANCE=local "$ROOT/scripts/start_ngrok_bridge.sh" >>"$SUPERVISOR_LOG" 2>&1; then
      FAILS=0
      log "managed stack restarted"
    fi
  else
    FAILS=0
    sleep "$POLL"
  fi
done

log "supervisor sentinel removed; stopping the managed stack"
STOPPING=1
exit 0
