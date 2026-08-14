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
#   - REAL readiness gate: /health or /ready must report ready=true
#     (app-server alive + provider secret ref readable + provider config
#     complete). A bare HTTP 200 is never treated as healthy. Readiness
#     failures restart the managed stack (bounded by the restart lock);
#   - public tunnel health: the fixed public endpoint is checked with the
#     same readiness gate. N consecutive failures (default 2) trigger a
#     managed ngrok/bridge restart, guarded by a restart lock + backoff
#     (>=120s) so a dead tunnel can never cause a restart storm. Transient
#     single failures never kill anything;
#   - sleep/wake and network reconnect need no special handling: every poll
#     round re-checks local + public readiness and recovers as needed;
#   - the supervisor NEVER calls the Codex model API: only local HTTP
#     readiness, managed-process identity and ngrok tunnel health are used;
#   - when the sentinel disappears or TERM/INT arrives: safely stops the
#     managed children (stop_ngrok_bridge.sh, identity-guarded), removes the
#     supervisor.pid file and exits 0 (launchd sees a clean shutdown);
#   - abnormal exit codes are handed back to launchd (which restarts the job
#     while the PathState sentinel exists, with a ThrottleInterval).
#
# No pkill/killall; no unbounded rm -rf (only `rm -f` on the absolute
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
BRIDGE_URL="http://127.0.0.1:$BRIDGE_PORT"
PUBLIC_URL_FILE="$(bridge_public_url_file local)"
SUPERVISOR_LOG="$RUNTIME_DIR/supervisor.log"

# Restart storm guards (instance-state, absolute paths only).
PUBLIC_FAIL_FILE="$RUNTIME_DIR/public_health.fails"
LAST_RESTART_FILE="$RUNTIME_DIR/last_restart.epoch"
RESTART_LOCK="$RUNTIME_DIR/restart.lock"
PUBLIC_FAIL_THRESHOLD="${SUPERVISOR_PUBLIC_FAIL_THRESHOLD:-2}"
RESTART_BACKOFF="${SUPERVISOR_RESTART_BACKOFF_SECS:-120}"

log() {
  local msg="[supervisor] $*"
  printf '%s\n' "$msg"
  printf '%s\n' "$msg" >> "$SUPERVISOR_LOG" 2>/dev/null || true
}
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
  rm -f "$RESTART_LOCK/pid" 2>/dev/null || true
  rmdir "$RESTART_LOCK" 2>/dev/null || true
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

[[ "$PUBLIC_FAIL_THRESHOLD" =~ ^[0-9]+$ && "$PUBLIC_FAIL_THRESHOLD" -ge 1 ]] || PUBLIC_FAIL_THRESHOLD=2
[[ "$RESTART_BACKOFF" =~ ^[0-9]+$ && "$RESTART_BACKOFF" -ge 1 ]] || RESTART_BACKOFF=120

stop_children() {
  BRIDGE_INSTANCE=local "$ROOT/scripts/stop_ngrok_bridge.sh" >>"$SUPERVISOR_LOG" 2>&1 || true
}

# Real local readiness: /ready must report ready=true (HTTP 200 only when
# app-server alive + provider secret ref readable + provider config complete).
local_ready() {
  curl -fsS -m 10 "$BRIDGE_URL/ready" 2>>"$SUPERVISOR_LOG" | python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read())
    sys.exit(0 if d.get("ready") is True else 1)
except Exception:
    sys.exit(1)' 2>/dev/null
}

# Public tunnel readiness: same gate through the fixed public endpoint.
# The recorded public_url state file is read inside; the value is never
# printed by this supervisor.
public_ready() {
  local url=""
  url="$(cat "$PUBLIC_URL_FILE" 2>/dev/null || true)"
  [[ -n "$url" ]] || return 1
  curl -fsS -m 15 "$url/ready" 2>>"$SUPERVISOR_LOG" | python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read())
    sys.exit(0 if d.get("ready") is True else 1)
except Exception:
    sys.exit(1)' 2>/dev/null
}

# Restart lock: only one recovery may run at a time (mkdir-based, owner pid
# recorded; a stale lock whose owner is gone is removed first).
acquire_restart_lock() {
  local owner=""
  if mkdir "$RESTART_LOCK" 2>/dev/null; then
    echo "$$" > "$RESTART_LOCK/pid"
    return 0
  fi
  owner="$(cat "$RESTART_LOCK/pid" 2>/dev/null || true)"
  if [[ -n "$owner" ]] && is_valid_pid "$owner" && pid_alive "$owner"; then
    return 1
  fi
  rm -f "$RESTART_LOCK/pid" 2>/dev/null || true
  rmdir "$RESTART_LOCK" 2>/dev/null || true
  if mkdir "$RESTART_LOCK" 2>/dev/null; then
    echo "$$" > "$RESTART_LOCK/pid"
    return 0
  fi
  return 1
}

release_restart_lock() {
  rm -f "$RESTART_LOCK/pid" 2>/dev/null || true
  rmdir "$RESTART_LOCK" 2>/dev/null || true
}

# Returns 0 when a full recovery is allowed (>= RESTART_BACKOFF since the
# last recovery; never storms on a persistently broken tunnel).
restart_allowed() {
  local now="" last=0
  now="$(date +%s)"
  [[ -f "$LAST_RESTART_FILE" ]] && last="$(cat "$LAST_RESTART_FILE" 2>/dev/null || true)"
  [[ "$last" =~ ^[0-9]+$ ]] || last=0
  (( now - last >= RESTART_BACKOFF ))
}

# Full managed recovery (stop + idempotent start) used for readiness failures
# and degraded public tunnel health. Bounded by the restart lock + backoff.
recover_stack() {
  local reason="$1"
  if ! restart_allowed; then
    log "recovery suppressed: restart backoff active (>=${RESTART_BACKOFF}s since last full recovery); reason=$reason"
    return 1
  fi
  if ! acquire_restart_lock; then
    log "recovery skipped: another recovery is already in progress; reason=$reason"
    return 1
  fi
  date +%s > "$LAST_RESTART_FILE"
  log "recovery start: $reason"
  stop_children
  if BRIDGE_INSTANCE=local "$ROOT/scripts/start_ngrok_bridge.sh" >>"$SUPERVISOR_LOG" 2>&1; then
    FAILS=0
    printf '0\n' > "$PUBLIC_FAIL_FILE"
    log "recovery complete: $reason"
    release_restart_lock
    return 0
  fi
  log "recovery start failed: $reason (see $SUPERVISOR_LOG); readiness stays false"
  release_restart_lock
  return 1
}

# Managed-child crash recovery: pid missing -> start again (idempotent).
# Uses the existing exponential backoff for repeated failures. Never subject
# to the 120s full-recovery lock, so a single crash heals quickly.
restart_children() {
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
    restart_children
    sleep "$POLL"
    continue
  fi

  # Real readiness gate (app-server + provider secret ref + config).
  if ! local_ready; then
    log "local readiness failed: $BRIDGE_URL/ready is not ready (app-server alive / provider secret ref / provider config); attempting full recovery"
    recover_stack "local readiness failure" || true
    sleep "$POLL"
    continue
  fi

  # Public tunnel readiness gate with a consecutive-failure threshold.
  if ! public_ready; then
    count="$(cat "$PUBLIC_FAIL_FILE" 2>/dev/null || echo 0)"
    [[ "$count" =~ ^[0-9]+$ ]] || count=0
    count=$((count + 1))
    printf '%s\n' "$count" > "$PUBLIC_FAIL_FILE"
    log "public tunnel health failed ($count/$PUBLIC_FAIL_THRESHOLD)"
    if (( count >= PUBLIC_FAIL_THRESHOLD )); then
      recover_stack "public tunnel health degraded" || true
    fi
  else
    if [[ -f "$PUBLIC_FAIL_FILE" ]]; then
      prev="$(cat "$PUBLIC_FAIL_FILE" 2>/dev/null || echo 0)"
      if [[ "$prev" != "0" ]]; then
        log "public tunnel health recovered"
      fi
    fi
    printf '0\n' > "$PUBLIC_FAIL_FILE"
    FAILS=0
  fi

  sleep "$POLL"
done

log "supervisor sentinel removed; stopping the managed stack"
STOPPING=1
exit 0
