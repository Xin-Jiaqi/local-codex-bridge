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

log() { printf '[deactivate-maintenance] %s\n' "$*"; }
die() { printf '[deactivate-maintenance] error: %s\n' "$*" >&2; exit 1; }

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
for i in $(seq 1 30); do
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

rm -f "$MARKER"
log "maintenance window CLOSED: BRIDGE_INSTANCE=local active (pause marker cleared; supervisor state restored: $PRE_SUPERVISOR)"
log "maintenance instance state and $HOME/.codex-deepseek-maintenance were left in place for the next window"
