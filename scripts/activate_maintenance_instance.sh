#!/usr/bin/env bash
#
# HOST-ADMIN: open a Bridge self-maintenance window.
#
# This is NOT a normal task-facing operation and there is NO task API to
# switch instances. It is an explicit host-admin maintenance workflow that:
#
#   1. fail-closed: verifies every prerequisite before mutating anything;
#   2. never uses pkill/killall - stopping is done by
#      scripts/stop_ngrok_bridge.sh, which verifies PID identity read-only
#      before signalling and never touches unmanaged processes;
#   3. checks the current local instance (config exists + verifies);
#   4. prepares $HOME/.codex-deepseek-maintenance: when absent it copies ONLY
#      $HOME/.codex-deepseek/config.toml (threads/history/cache are never
#      copied); dirs 700, config 600;
#   5. runs the same legacy-sandbox/profile verify+migration that
#      bridge-workspace uses (scripts/migrate_codex_home_permissions.py,
#      explicit --codex-home, no hardcoded paths);
#   6. creates + verifies the maintenance instance (port 8323, own runtime);
#   7. HOST-ADMIN ONLY: writes .bridge_api_key and .ngrok_domain PATH
#      REFERENCES into the maintenance instance config so the current Custom
#      GPT Actions keep using the same fixed endpoint during the window.
#      Only paths are stored; secret/domain CONTENT is never copied or
#      printed. This is NOT the default maintenance template behavior
#      (create leaves both references empty).
#   8. supervisor-aware handover: records the pre-window local supervisor
#      state (enabled|disabled) in a maintenance instance-state marker,
#      creates the local PAUSE MARKER (<state>/local/pause.marker) BEFORE
#      stopping local - the foreground supervisor (when running) sees the
#      marker, stops the local children and WAITS (it stays alive, so launchd
#      never sees an exit and there is no crash-loop during the window), then
#      stops BRIDGE_INSTANCE=local and arms a fail-safe rollback: from the
#      handover on, ANY failure (ERR trap or explicit die) rolls back before
#      exiting - safely stops maintenance if its instance is configured
#      (managed processes only), clears the pause marker, restores the
#      pre-window supervisor enabled/disabled state (sentinel + launchd
#      kickstart when the runtime agent is installed, else the legacy start
#      flow), restarts BRIDGE_INSTANCE=local, verifies local health +
#      identity and public health, and still exits nonzero with the original
#      failure reason preserved. Rollback logs never print API key/domain
#      content.
#   9. starts BRIDGE_INSTANCE=maintenance;
#  10. verifies bridge local/public health, instance=maintenance,
#      mode=bridge-workspace and port=8323; only after the FULL pass
#      (local health + identity + public health) is the rollback disarmed.
#
# LaunchAgent note: the local supervisor agent
# (com.local.codex-bridge.local) is KeepAlive-bound to the
# supervisor.enabled sentinel via PathState. During the window the sentinel
# stays in place and the PAUSE MARKER holds local children stopped: the
# foreground supervisor remains alive (no launchd restart, no crash-loop)
# but refuses to start local children until the marker is removed. Do NOT
# manually remove the pause marker or kickstart-restart the local agent
# while the window is open.
#
# hpc, Para/Japan and remote jobs are NEVER touched.
#
# Usage:
#   ./scripts/activate_maintenance_instance.sh
#
# To leave the maintenance window:
#   ./scripts/deactivate_maintenance_instance.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"
. "$ROOT/scripts/pid_guard_lib.sh"
. "$ROOT/scripts/supervisor_control.sh"
. "$ROOT/scripts/host_ops_lock_lib.sh"

log() { printf '[activate-maintenance] %s\n' "$*"; }
rlog() { printf '[activate-maintenance] rollback: %s\n' "$*" >&2; }

ROLLBACK_ARMED=0
ROLLBACK_DONE=0
ROLLBACK_LOG=""

# Fail-safe rollback. Only runs once, and only when armed (i.e. after the
# local bridge was successfully stopped). Order: stop maintenance (managed
# processes only, via the sanctioned stop script), restart local, verify
# local health + identity, then public health when a fixed domain is
# configured. Every step is best-effort (`|| true`) so the rollback itself
# can never mask the original failure; the caller still exits nonzero.
# Nothing here prints API key or ngrok domain content.
rollback() {
  [[ "${ROLLBACK_ARMED:-0}" == 1 && "${ROLLBACK_DONE:-0}" == 0 ]] || return 0
  ROLLBACK_DONE=1
  local local_runtime="" local_port="" domain=""
  local_runtime="$(bridge_instance_get local runtime_dir 2>/dev/null || true)"
  [[ -n "$local_runtime" ]] || local_runtime="$ROOT/.runtime"
  mkdir -p "$local_runtime" 2>/dev/null || true
  ROLLBACK_LOG="$local_runtime/activate-rollback.log"
  rlog "activation failed; restoring the pre-window local state (hpc, Para/Japan and remote jobs are never touched)"
  # 1. Safely stop maintenance when its instance is configured. PID identity
  #    verification happens inside stop_ngrok_bridge.sh; managed processes
  #    only, never pkill/killall. Nothing to stop when the instance config
  #    does not exist yet (create/migrate failures).
  if bridge_instance_exists "$INSTANCE"; then
    BRIDGE_INSTANCE="$INSTANCE" "$ROOT/scripts/stop_ngrok_bridge.sh" >>"$ROLLBACK_LOG" 2>&1 || true
  fi
  # 1b. Clear the pause marker first so the local supervisor may resume.
  if [[ -n "${PAUSE_MARKER:-}" ]]; then
    rm -f "$PAUSE_MARKER" 2>/dev/null || true
    rlog "pause marker cleared: $PAUSE_MARKER"
  fi
  # 2. Restore the pre-window supervisor state: enabled -> sentinel + launchd
  #    kickstart when the runtime agent is installed, else the legacy start
  #    flow; disabled -> sentinel stays removed and local stays stopped.
  if [[ "${PRE_SUPERVISOR:-enabled}" == "enabled" ]]; then
    if [[ -n "${SUPERVISOR_SENTINEL:-}" ]]; then
      mkdir -p "$(dirname "$SUPERVISOR_SENTINEL")" 2>/dev/null || true
      touch "$SUPERVISOR_SENTINEL" 2>/dev/null || true
    fi
    if supervisor_agent_loaded; then
      launchctl kickstart -k "gui/$(id -u)/$SUPERVISOR_AGENT_LABEL" >>"$ROLLBACK_LOG" 2>&1 || true
    else
      BRIDGE_INSTANCE=local "$ROOT/scripts/start_ngrok_bridge.sh" >>"$ROLLBACK_LOG" 2>&1 || true
    fi
    # 3. Verify local health + identity when the local instance is configured.
    if bridge_instance_exists local; then
      local_port="$(bridge_instance_get local port 2>/dev/null || true)"
      if [[ -n "$local_port" ]]; then
        bridge_health_ok "http://127.0.0.1:$local_port" "$ROLLBACK_LOG" || true
        bridge_health_identity "http://127.0.0.1:$local_port" local \
          "$(bridge_instance_mode local 2>/dev/null || true)" "$local_port" "$ROLLBACK_LOG" || true
      fi
    fi
  else
    if [[ -n "${SUPERVISOR_SENTINEL:-}" ]]; then
      rm -f "$SUPERVISOR_SENTINEL" 2>/dev/null || true
    fi
    rlog "pre-window supervisor state was disabled; local left stopped"
  fi
  # 4. Public health when a fixed domain is configured (value never printed).
  if [[ -f "$ROOT/.ngrok_domain" ]]; then
    domain="$(tr -d '[:space:]' < "$ROOT/.ngrok_domain" 2>/dev/null || true)"
    if [[ -n "$domain" ]]; then
      bridge_health_ok "https://$domain" "$ROLLBACK_LOG" || true
    fi
  fi
  if [[ -n "${MARKER:-}" ]]; then
    rm -f "$MARKER" 2>/dev/null || true
  fi
  rlog "local state restore attempted; the original failure above remains the exit reason (rollback log: $ROLLBACK_LOG)"
}

# Explicit failure: print the reason first (never overwritten by rollback),
# then roll back when armed, then exit nonzero.
die() {
  trap - ERR 2>/dev/null || true
  printf '[activate-maintenance] error: %s\n' "$*" >&2
  rollback
  exit 1
}

# ERR trap (armed only after the local stop): roll back then exit with the
# failing command's status, so the failure is never masked.
on_error() {
  local rc=$?
  trap - ERR 2>/dev/null || true
  printf '[activate-maintenance] error: command failed (exit %s) at line %s (see %s)\n' \
    "$rc" "${BASH_LINENO[0]}" "${ROLLBACK_LOG:-$ROOT/.runtime/activate-rollback.log}" >&2
  rollback
  exit "${rc:-1}"
}

# Global single-writer host-ops lock: only one control-plane mutation runs
# at a time (humans / ChatGPT / unattended automation). BUSY when another
# host op holds the lock; sub-scripts called from this parent re-enter via
# the exported token; released automatically on EXIT.
host_ops_lock_acquire "activate-maintenance" || die \
  "another host operation holds the host-ops lock; concurrent control-plane writes are refused (retry after it exits; run ./scripts/status_launch_agent.sh --instance local to inspect)"

INSTANCE="maintenance"
SOURCE_HOME="$HOME/.codex-deepseek"
MAINT_HOME="$HOME/.codex-deepseek-maintenance"

# ---------------------------------------------------------------------------
# 1. Fail-closed prerequisite checks (nothing is mutated before this passes).
# ---------------------------------------------------------------------------

bridge_instance_valid "$INSTANCE" || die "internal error: invalid maintenance instance name"
bridge_instance_exists local || die \
  "local instance has no config ($(bridge_instance_config local)); run ./scripts/bridge_instance.sh migrate-current --apply first"
bash "$ROOT/scripts/bridge_instance.sh" verify local >/dev/null 2>&1 || die \
  "local instance config failed verification (run ./scripts/bridge_instance.sh verify local)"

if [[ -n "${BRIDGE_INSTANCE:-}" && "$BRIDGE_INSTANCE" != "local" && "$BRIDGE_INSTANCE" != "$INSTANCE" ]]; then
  die "BRIDGE_INSTANCE=$BRIDGE_INSTANCE is already exported; this host-admin window only manages local/maintenance (hpc and remote jobs are never touched)"
fi

# Refuse to run when a maintenance bridge is already active (fail closed).
if bridge_instance_exists "$INSTANCE"; then
  maint_runtime="$(bridge_instance_get "$INSTANCE" runtime_dir)"
  if [[ -n "$maint_runtime" && -f "$maint_runtime/bridge.pid" ]]; then
    maint_pid="$(cat "$maint_runtime/bridge.pid" 2>/dev/null || true)"
    if managed_bridge_pid "$maint_pid" "$ROOT" "$maint_runtime"; then
      die "maintenance bridge is already running (pid $maint_pid); run ./scripts/deactivate_maintenance_instance.sh first"
    fi
  fi
fi

[[ -d "$SOURCE_HOME" ]] || die "source CODEX_HOME not found: $SOURCE_HOME"
[[ -f "$SOURCE_HOME/config.toml" ]] || die "source config not found: $SOURCE_HOME/config.toml"
[[ -f "$ROOT/.bridge_api_key" ]] || die "API key file not found: $ROOT/.bridge_api_key (path reference only; content is never read here)"
[[ -f "$ROOT/.ngrok_domain" ]] || die "ngrok domain file not found: $ROOT/.ngrok_domain (path reference only; content is never read here)"

# ---------------------------------------------------------------------------
# 2. Prepare the maintenance CODEX_HOME (config only, never history).
# ---------------------------------------------------------------------------

if [[ ! -d "$MAINT_HOME" ]]; then
  mkdir -p "$MAINT_HOME"
  chmod 700 "$MAINT_HOME"
  cp -p "$SOURCE_HOME/config.toml" "$MAINT_HOME/config.toml"
  chmod 600 "$MAINT_HOME/config.toml"
  log "created maintenance CODEX_HOME: $MAINT_HOME (config copied from $SOURCE_HOME/config.toml; threads/history/cache never copied)"
else
  log "maintenance CODEX_HOME already exists: $MAINT_HOME (left untouched)"
fi
chmod 700 "$MAINT_HOME" 2>/dev/null || true
[[ -f "$MAINT_HOME/config.toml" ]] || die "maintenance config missing: $MAINT_HOME/config.toml"
chmod 600 "$MAINT_HOME/config.toml" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. Same legacy-sandbox/profile migration as bridge-workspace, but for the
#    explicit maintenance CODEX_HOME (--codex-home compatibility parameter).
# ---------------------------------------------------------------------------

if ! python3 "$ROOT/scripts/migrate_codex_home_permissions.py" \
    --codex-home "$MAINT_HOME" --project-root "$ROOT" --apply >/dev/null 2>&1; then
  die "failed to migrate maintenance config for bridge-workspace (see: python3 $ROOT/scripts/migrate_codex_home_permissions.py --codex-home $MAINT_HOME --dry-run)"
fi
python3 "$ROOT/scripts/migrate_codex_home_permissions.py" \
  --codex-home "$MAINT_HOME" --project-root "$ROOT" --verify >/dev/null 2>&1 || die \
  "maintenance config is not clean for bridge-workspace (legacy sandbox keys remain; clean manually, do not weaken the profile)"

# ---------------------------------------------------------------------------
# 4. Create (if needed) and verify the maintenance instance. The default
#    template has NO api_key/domain references; the HOST-ADMIN path refs are
#    written explicitly in step 5.
# ---------------------------------------------------------------------------

if ! bridge_instance_exists "$INSTANCE"; then
  bash "$ROOT/scripts/bridge_instance.sh" create "$INSTANCE" >/dev/null 2>&1 || die \
    "failed to create maintenance instance (run ./scripts/bridge_instance.sh create maintenance to see the error)"
  log "created maintenance instance (port 8323, dedicated runtime and CODEX_HOME)"
fi

# ---------------------------------------------------------------------------
# 5. HOST-ADMIN ONLY: pin the maintenance contract (port 8323 + dedicated
#    CODEX_HOME) and point the instance at the existing fixed endpoint via
#    PATH REFERENCES (never contents). Not the template default. When the
#    stable config-root copies exist (runtime install), they are preferred;
#    the repo path references remain the fallback.
# ---------------------------------------------------------------------------

KEY_REF="$ROOT/.bridge_api_key"
DOMAIN_REF="$ROOT/.ngrok_domain"
if [[ -f "$(bridge_config_root)/api_key" ]]; then
  KEY_REF="$(bridge_config_root)/api_key"
fi
if [[ -f "$(bridge_config_root)/ngrok_domain" ]]; then
  DOMAIN_REF="$(bridge_config_root)/ngrok_domain"
fi

bash "$ROOT/scripts/bridge_instance.sh" update "$INSTANCE" \
  "port=8323" \
  "codex_home=$MAINT_HOME" \
  "api_key_file=$KEY_REF" \
  "ngrok_domain_file=$DOMAIN_REF" >/dev/null 2>&1 || die \
  "failed to pin maintenance config (run ./scripts/bridge_instance.sh show maintenance to inspect; no secrets are printed)"
bash "$ROOT/scripts/bridge_instance.sh" verify "$INSTANCE" >/dev/null 2>&1 || die \
  "maintenance instance config failed verification (run ./scripts/bridge_instance.sh verify maintenance)"
log "maintenance instance verified: mode=bridge-workspace port=8323 codex_home=$MAINT_HOME (path references only, secret/domain content untouched)"

# ---------------------------------------------------------------------------
# 6. Supervisor-aware handover: record the pre-window local supervisor state
#    in the maintenance instance state, create the PAUSE MARKER before
#    stopping local (the foreground supervisor stays alive, holds local
#    children stopped and never restarts them while the marker exists; the
#    sentinel is left in place so launchd has no crash-loop), then stop
#    local, and start maintenance. hpc and remote jobs are never touched; no
#    pkill/killall.
# ---------------------------------------------------------------------------

PRE_SUPERVISOR="disabled"
if supervisor_enabled; then
  PRE_SUPERVISOR="enabled"
fi
log "local supervisor pre-window state: $PRE_SUPERVISOR"
MARKER="$(bridge_instance_dir "$INSTANCE")/activate.marker"
printf 'instance=%s\nsupervisor_state=%s\n' "$INSTANCE" "$PRE_SUPERVISOR" > "$MARKER"
chmod 600 "$MARKER"
SUPERVISOR_SENTINEL="$(bridge_supervisor_enabled_file local)"
PAUSE_MARKER="$(bridge_pause_marker)"
LOCAL_RUNTIME="$(bridge_instance_get local runtime_dir)"
LOCAL_PORT="$(bridge_instance_get local port)"

# Fail-safe: from the supervisor handover on, ANY failure (ERR trap or
# explicit die) rolls back to the pre-window local state before exiting
# nonzero (see rollback()). Disarmed only after the FULL verification pass
# below (local health + identity + public health).
ROLLBACK_ARMED=1
trap on_error ERR
log "rollback armed: any subsequent failure will restore the pre-window local state"

# Pause the local supervisor BEFORE stopping local: the marker tells the
# foreground supervisor (when running) to stop its managed children and wait,
# never to restart them while the window is open. The sentinel is left in
# place, so the supervisor stays alive and launchd has no crash-loop.
mkdir -p "$(dirname "$PAUSE_MARKER")"
touch "$PAUSE_MARKER"
chmod 600 "$PAUSE_MARKER"
log "pause marker created BEFORE stopping local: $PAUSE_MARKER (local supervisor holds local children stopped for the window)"

log "stopping BRIDGE_INSTANCE=local (managed processes only)"
BRIDGE_INSTANCE=local "$ROOT/scripts/stop_ngrok_bridge.sh" || die "failed to stop the local instance"

# Settle: a running supervisor may be mid-restart (it checks the pause marker
# before every start, so this converges); wait until no managed local child
# remains before starting maintenance, so the fixed endpoint is never
# contended. Bounded, best-effort - the maintenance start itself still
# fail-closes on a conflict.
for _ in $(seq 1 10); do
  bpid="$(cat "$LOCAL_RUNTIME/bridge.pid" 2>/dev/null || true)"
  npid="$(cat "$LOCAL_RUNTIME/ngrok.pid" 2>/dev/null || true)"
  if ! managed_bridge_pid "$bpid" "$ROOT" "$LOCAL_RUNTIME" \
     && ! managed_ngrok_pid "$npid" "$LOCAL_PORT"; then
    break
  fi
  log "waiting for the local supervisor to honor the pause marker (local child still up)"
  sleep 1
done

maint_runtime="$(bridge_instance_get "$INSTANCE" runtime_dir)"
[[ -n "$maint_runtime" ]] || die "maintenance runtime_dir missing from instance config"
mkdir -p "$maint_runtime"
ACTIVATE_LOG="$maint_runtime/activate.log"

log "starting BRIDGE_INSTANCE=maintenance (port 8323, dedicated runtime)"
if ! BRIDGE_INSTANCE="$INSTANCE" "$ROOT/scripts/start_ngrok_bridge.sh" >"$ACTIVATE_LOG" 2>&1; then
  die "maintenance start failed; see $ACTIVATE_LOG (domain/secret content is not printed)"
fi

# ---------------------------------------------------------------------------
# 7. Verify: bridge local health, instance=maintenance, mode=bridge-workspace,
#    port=8323, and the fixed public endpoint health.
# ---------------------------------------------------------------------------

MAINT_PORT="$(bridge_instance_get "$INSTANCE" port)"
MAINT_URL="http://127.0.0.1:$MAINT_PORT"

if ! bridge_health_ok "$MAINT_URL" "$ACTIVATE_LOG"; then
  die "bridge local health did not become OK on $MAINT_URL/health (see $ACTIVATE_LOG)"
fi

# Verify the RUNNING instance identity through /health (strongest check).
if ! bridge_health_identity "$MAINT_URL" "maintenance" "bridge-workspace" "$MAINT_PORT" "$ACTIVATE_LOG"; then
  die "health identity check failed: expected instance=maintenance mode=bridge-workspace port=$MAINT_PORT on $MAINT_URL/health (see $ACTIVATE_LOG)"
fi
log "local health OK + identity verified: instance=maintenance mode=bridge-workspace port=$MAINT_PORT"

# Public endpoint: same fixed domain, now served by maintenance. The domain
# value is read but NEVER printed; curl chatter goes to the log file.
DOMAIN="$(tr -d '[:space:]' < "$ROOT/.ngrok_domain" || true)"
[[ -n "$DOMAIN" ]] || die "ngrok domain file is empty: $ROOT/.ngrok_domain"
PUBLIC_URL="https://$DOMAIN"
if ! bridge_health_ok "$PUBLIC_URL" "$ACTIVATE_LOG"; then
  die "public health did not become OK for the fixed endpoint (see $ACTIVATE_LOG; the domain value is not printed)"
fi
log "public health OK for the fixed endpoint (same Custom GPT Actions endpoint, now served by maintenance)"

# Full pass complete: local health + identity + public health all verified.
# Disarm the fail-safe rollback before declaring the window active.
trap - ERR
ROLLBACK_ARMED=0
ROLLBACK_DONE=0
log "rollback disarmed: activation completed successfully"

log "maintenance window ACTIVE: BRIDGE_INSTANCE=maintenance port=$MAINT_PORT mode=bridge-workspace"
log "local supervisor PAUSED for the window (pause marker kept: $PAUSE_MARKER; pre-window state: $PRE_SUPERVISOR; activation marker: $MARKER)"
log "to leave the window: ./scripts/deactivate_maintenance_instance.sh"
