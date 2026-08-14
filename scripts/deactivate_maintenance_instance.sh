#!/usr/bin/env bash
#
# HOST-ADMIN: leave the Bridge self-maintenance window and restore local.
#
#   - stops BRIDGE_INSTANCE=maintenance (managed processes only; the same
#     PID-identity-verified stop path as stop_ngrok_bridge.sh; never
#     pkill/killall);
#   - clears the local PAUSE MARKER (<state>/local/pause.marker) and restores
#     the pre-window local supervisor state recorded by
#     activate_maintenance_instance.sh (marker in the maintenance instance
#     state):
#       enabled  -> the launchd supervisor is preferred to resume (when it is
#                   already running it restarts the local children by itself;
#                   otherwise the supervisor.enabled sentinel is recreated
#                   and the runtime launchd agent is kickstarted); when no
#                   agent is installed/unavailable this falls back to the
#                   legacy start flow;
#       disabled -> local stays stopped (sentinel stays removed);
#     a missing marker (pre-runtime window) is treated as enabled so legacy
#     windows still restore local;
#   - verifies local/public health when local was restored;
#   - fail-safe rollback: the maintenance stop is the point of no return, so
#     from that moment ANY failure (local restore / local health / identity /
#     public health) automatically re-opens the maintenance window and
#     verifies maintenance/bridge-workspace/8323 + public health; only when
#     that rollback ALSO fails is an explicit DOUBLE FAILURE reported (the
#     original failure reason is always preserved);
#   - NEVER deletes the maintenance state or $HOME/.codex-deepseek-maintenance
#     (they are reused by the next maintenance window);
#   - never touches hpc, Para/Japan or remote jobs.
#
# Usage:
#   ./scripts/deactivate_maintenance_instance.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"
. "$ROOT/scripts/supervisor_control.sh"
. "$ROOT/scripts/host_ops_lock_lib.sh"

log() { printf '[deactivate-maintenance] %s\n' "$*"; }

ROLLBACK_ARMED=0
ROLLBACK_DONE=0
ROLLBACK_LOG=""

# Fail-safe rollback to the maintenance window. Armed BEFORE the maintenance
# stop: once maintenance is down, ANY failure in the local restore or the
# local/public health verification must bring the fixed public endpoint back
# to maintenance instead of leaving the control plane dark. Brings
# maintenance back up through the sanctioned start path (managed processes
# only), re-raises the local pause hold + activation marker so the window
# state stays consistent, and verifies maintenance identity
# (maintenance/bridge-workspace/8323) + public health. Only when this
# rollback itself fails is an explicit DOUBLE FAILURE reported. The original
# failure reason always remains the exit reason; no secret/domain content is
# printed.
rollback() {
  [[ "${ROLLBACK_ARMED:-0}" == 1 && "${ROLLBACK_DONE:-0}" == 0 ]] || return 0
  ROLLBACK_DONE=1
  local mport="" murl="" domain="" ref="" ok=0 rlog=""
  rlog="$(bridge_instance_get "$LOCAL" runtime_dir 2>/dev/null || true)/deactivate-rollback.log"
  [[ -n "$rlog" ]] || rlog="$ROOT/.runtime/deactivate-rollback.log"
  mkdir -p "$(dirname "$rlog")" 2>/dev/null || true
  printf '[deactivate-maintenance] rollback: local restore/health failed; fail-safe restoring the maintenance window (managed processes only; hpc/Para/Japan and remote jobs are never touched)\n' >&2
  # 1. Re-raise the local pause hold + activation marker so the window state
  #    stays consistent for the next deactivate (local stays stopped).
  if [[ -n "${PAUSE_MARKER:-}" ]]; then
    mkdir -p "$(dirname "$PAUSE_MARKER")" 2>/dev/null || true
    touch "$PAUSE_MARKER" 2>/dev/null || true
    chmod 600 "$PAUSE_MARKER" 2>/dev/null || true
  fi
  if [[ -n "${MARKER:-}" && ! -f "$MARKER" ]]; then
    printf 'instance=%s\nsupervisor_state=%s\n' "$INSTANCE" "${PRE_SUPERVISOR:-enabled}" > "$MARKER" 2>/dev/null || true
    chmod 600 "$MARKER" 2>/dev/null || true
  fi
  # 2. Bring maintenance back up (best-effort: it may already be up when the
  #    stop itself failed).
  BRIDGE_INSTANCE="$INSTANCE" "$ROOT/scripts/start_ngrok_bridge.sh" >>"$rlog" 2>&1 || true
  # 3. Verify maintenance identity + public health.
  mport="$(bridge_instance_get "$INSTANCE" port 2>/dev/null || true)"
  [[ -n "$mport" ]] || mport="8323"
  murl="http://127.0.0.1:$mport"
  if bridge_health_ok "$murl" "$rlog" \
     && bridge_health_identity "$murl" "$INSTANCE" "bridge-workspace" "$mport" "$rlog"; then
    ok=1
  fi
  ref="$(bridge_instance_get "$INSTANCE" ngrok_domain_file 2>/dev/null || true)"
  if [[ -n "$ref" && -f "$ref" ]]; then
    domain="$(tr -d '[:space:]' < "$ref" 2>/dev/null || true)"
  fi
  if [[ -z "$domain" && -f "$ROOT/.ngrok_domain" ]]; then
    domain="$(tr -d '[:space:]' < "$ROOT/.ngrok_domain" 2>/dev/null || true)"
  fi
  if [[ "$ok" == 1 && -n "$domain" ]]; then
    bridge_health_ok "https://$domain" "$rlog" || ok=0
  fi
  if [[ "$ok" == 1 ]]; then
    printf '[deactivate-maintenance] rollback: maintenance window restored and verified (instance=%s mode=bridge-workspace port=%s, local+public health OK); the original failure remains the exit reason (rollback log: %s)\n' \
      "$INSTANCE" "$mport" "$rlog" >&2
    return 0
  fi
  printf '[deactivate-maintenance] DOUBLE FAILURE: local restore/health failed AND the maintenance rollback could not be verified (instance=%s port=%s, local+public health); the public control plane may be DOWN - manual host intervention required (rollback log: %s)\n' \
    "$INSTANCE" "$mport" "$rlog" >&2
  return 1
}

# Explicit failure: print the reason, attempt the fail-safe maintenance
# rollback, then exit nonzero (the rollback never masks the original error).
die() {
  trap - ERR 2>/dev/null || true
  printf '[deactivate-maintenance] error: %s\n' "$*" >&2
  rollback
  exit 1
}

# ERR trap (armed only from the maintenance stop on): fail-safe rollback to
# the maintenance window, then exit with the failing command's status.
on_error() {
  local rc=$?
  trap - ERR 2>/dev/null || true
  printf '[deactivate-maintenance] error: command failed (exit %s) at line %s (see %s)\n' \
    "$rc" "${BASH_LINENO[0]}" "${ROLLBACK_LOG:-$ROOT/.runtime/deactivate-rollback.log}" >&2
  rollback
  exit "${rc:-1}"
}

# Global single-writer host-ops lock: only one control-plane mutation runs
# at a time (humans / ChatGPT / unattended automation). BUSY when another
# host op holds the lock; sub-scripts called from this parent re-enter via
# the exported token; released automatically on EXIT.
host_ops_lock_acquire "deactivate-maintenance" || die \
  "another host operation holds the host-ops lock; concurrent control-plane writes are refused (retry after it exits; run ./scripts/status_launch_agent.sh --instance local to inspect)"

INSTANCE="maintenance"
LOCAL="local"

if [[ -n "${BRIDGE_INSTANCE:-}" && "$BRIDGE_INSTANCE" != "$LOCAL" && "$BRIDGE_INSTANCE" != "$INSTANCE" ]]; then
  die "BRIDGE_INSTANCE=$BRIDGE_INSTANCE is already exported; this host-admin script only manages local/maintenance (hpc and remote jobs are never touched)"
fi

# Fail closed: the maintenance window must actually be configured.
bridge_instance_exists "$INSTANCE" || die \
  "maintenance instance has no config ($(bridge_instance_config "$INSTANCE")); nothing to deactivate"
bridge_instance_exists "$LOCAL" || die \
  "local instance has no config ($(bridge_instance_config "$LOCAL")); run ./scripts/bridge_instance.sh migrate-current --apply first"

# 1. Stop maintenance (managed processes only; maintenance state/CODEX_HOME
#    are deliberately left in place for the next window).
# Fail-safe: armed BEFORE the maintenance stop (the point of no return).
# From here on, ANY failure (ERR trap or explicit die) fail-safe restores
# the maintenance window (see rollback()). Disarmed only after the FULL
# local restore + identity + public health verification below.
ROLLBACK_ARMED=1
trap on_error ERR
log "rollback armed: any subsequent failure will fail-safe restore the maintenance window"
log "stopping BRIDGE_INSTANCE=maintenance (managed processes only)"
BRIDGE_INSTANCE="$INSTANCE" "$ROOT/scripts/stop_ngrok_bridge.sh" || die "failed to stop the maintenance instance"

# 2. Read the pre-window supervisor state from the activation marker
#    (instance state; missing marker = legacy window -> restore local).
MARKER="$(bridge_instance_dir "$INSTANCE")/activate.marker"
PRE_SUPERVISOR="enabled"
if [[ -f "$MARKER" ]]; then
  pre="$(sed -nE 's/^supervisor_state=(.*)$/\1/p' "$MARKER" | head -1)"
  [[ -n "$pre" ]] && PRE_SUPERVISOR="$pre"
fi
log "pre-window local supervisor state: $PRE_SUPERVISOR"

PAUSE_MARKER="$(bridge_pause_marker)"
if [[ "$PRE_SUPERVISOR" == "disabled" ]]; then
  sentinel="$(bridge_supervisor_enabled_file "$LOCAL")"
  if [[ -n "$sentinel" ]]; then
    rm -f "$sentinel"
  fi
  rm -f "$PAUSE_MARKER"
  rm -f "$MARKER"
  log "pre-window supervisor state was disabled; local stays stopped (pause marker cleared; maintenance state left in place for the next window)"
  exit 0
fi

# 3. Clear the pause marker so the local supervisor may resume its managed
#    children (a running supervisor notices it on the next poll).
rm -f "$PAUSE_MARKER"
log "pause marker cleared: $PAUSE_MARKER"

# 4. Restore local: prefer the launchd supervisor (a running supervisor
#    resumes by itself; otherwise sentinel + kickstart when the runtime agent
#    is installed). When no agent is installed/unavailable, fall back to the
#    legacy start flow.
local_runtime="$(bridge_instance_get "$LOCAL" runtime_dir)"
[[ -n "$local_runtime" ]] || die "local runtime_dir missing from instance config"
mkdir -p "$local_runtime"
DEACTIVATE_LOG="$local_runtime/deactivate.log"

# Pre-window state was enabled: recreate the sentinel first so supervision is
# re-enabled regardless of which start path is used below.
sentinel="$(bridge_supervisor_enabled_file "$LOCAL")"
if [[ -n "$sentinel" ]]; then
  touch "$sentinel"
  log "supervisor sentinel restored: $sentinel"
fi

if supervisor_agent_installed; then
  spid="$(supervisor_pid)"
  if [[ -n "$spid" ]]; then
    log "launchd supervisor already running (pid $spid); pause marker cleared - it will resume the local children"
  else
    log "restoring local supervisor (sentinel + kickstart of $SUPERVISOR_AGENT_LABEL)"
    if ! supervisor_restore enabled >>"$DEACTIVATE_LOG" 2>&1; then
      die "supervisor restore failed; see $DEACTIVATE_LOG (domain/secret content is not printed)"
    fi
  fi
else
  log "no launchd agent installed; using the legacy start flow"
  log "starting BRIDGE_INSTANCE=local"
  if ! BRIDGE_INSTANCE="$LOCAL" "$ROOT/scripts/start_ngrok_bridge.sh" >"$DEACTIVATE_LOG" 2>&1; then
    die "local start failed; see $DEACTIVATE_LOG (domain/secret content is not printed)"
  fi
fi

# 4. Verify local health + identity (shared read-only helpers from
#    scripts/bridge_instance_lib.sh; no body/domain content is printed).
LOCAL_PORT="$(bridge_instance_get "$LOCAL" port)"
LOCAL_URL="http://127.0.0.1:$LOCAL_PORT"
ok=0
for i in $(seq 1 "${DEACTIVATE_HEALTH_WAIT:-30}"); do
  if bridge_health_ok "$LOCAL_URL" "$DEACTIVATE_LOG"; then
    ok=1
    break
  fi
  sleep 1
done
if [[ "$ok" != 1 ]]; then
  die "local health did not become OK on $LOCAL_URL/health (see $DEACTIVATE_LOG)"
fi
if ! bridge_health_identity "$LOCAL_URL" "local" "bridge-workspace" "$LOCAL_PORT" "$DEACTIVATE_LOG"; then
  die "health identity check failed: expected instance=local mode=bridge-workspace port=$LOCAL_PORT on $LOCAL_URL/health (see $DEACTIVATE_LOG)"
fi
log "local health OK + identity verified: instance=local mode=bridge-workspace port=$LOCAL_PORT"

# Public endpoint: the same fixed domain served by local again. The domain
# value is read but NEVER printed.
DOMAIN=""
if [[ -f "$ROOT/.ngrok_domain" ]]; then
  DOMAIN="$(tr -d '[:space:]' < "$ROOT/.ngrok_domain" || true)"
elif [[ -n "$(bridge_instance_get "$LOCAL" ngrok_domain_file)" ]]; then
  DOMAIN="$(tr -d '[:space:]' < "$(bridge_instance_get "$LOCAL" ngrok_domain_file)" 2>/dev/null || true)"
fi
if [[ -n "$DOMAIN" ]]; then
  if ! bridge_health_ok "https://$DOMAIN" "$DEACTIVATE_LOG"; then
    die "public health did not become OK for the fixed endpoint (see $DEACTIVATE_LOG; the domain value is not printed)"
  fi
  log "public health OK for the fixed endpoint (served by local again)"
else
  log "public endpoint skipped: no ngrok domain configured for local (local-only mode)"
fi

# Full pass complete: local health + identity + public health all verified.
# Disarm the fail-safe rollback before finalizing (marker removal + logs).
trap - ERR
ROLLBACK_ARMED=0
ROLLBACK_DONE=0
log "rollback disarmed: deactivation completed successfully"

rm -f "$MARKER"
log "maintenance window CLOSED: BRIDGE_INSTANCE=local active (pause marker cleared; supervisor state restored: $PRE_SUPERVISOR)"
log "maintenance instance state and $HOME/.codex-deepseek-maintenance were left in place for the next window"
