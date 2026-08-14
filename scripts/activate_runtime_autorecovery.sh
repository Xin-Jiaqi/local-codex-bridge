#!/usr/bin/env bash
#
# HOST-ADMIN (one-shot): activate the LOCAL runtime auto-recovery stack.
#
# Run ONCE in a normal Terminal while the Bridge maintenance window is ACTIVE
# (./scripts/activate_maintenance_instance.sh succeeded). This script:
#
#   1. preflights the window: local + public /health must report
#      instance=maintenance mode=bridge-workspace port=8323;
#   2. idempotently creates the local supervisor pause marker
#      (<state>/local/pause.marker, existing helper scripts/bridge_instance_lib.sh)
#      so a supervisor started later never launches local children while
#      maintenance owns the fixed endpoint;
#   3. installs the stable non-Desktop runtime
#      (./scripts/install_runtime.sh --instance local) and verifies the
#      managed-release marker, an off-Desktop data root and that the copy
#      contains no secret/.git/tests/docs;
#   4. installs the per-instance launchd agent
#      (./scripts/install_launch_agent.sh --instance local; legacy label is
#      migrated inside that script) and verifies the generated plist: label
#      com.local.codex-bridge.local, ProgramArguments -> the installed runtime
#      supervisor, and that no local managed child holds the endpoint during
#      the window;
#   5. leaves the window (./scripts/deactivate_maintenance_instance.sh) and
#      verifies local + public /health identity = local/bridge-workspace/8321;
#   6. runs ONE safe bridge crash-recovery self-test: the managed bridge pid
#      (identity-verified via scripts/pid_guard_lib.sh) is TERMed, then the
#      supervisor is expected to bring up a DIFFERENT pid and restore
#      local+public health. ngrok is deliberately NOT killed (bridge-only
#      self-test). Set AR_CRASH_RECOVERY=0 to skip this step;
#   7. runs ./scripts/status_launch_agent.sh --instance local (output to the
#      log file) and prints AUTORECOVERY_ACTIVATION_OK YES on full success.
#
# Fail-closed: any failure prints the failing stage and exits non-zero after
# printing AUTORECOVERY_ACTIVATION_OK NO. No pkill/killall/rm -rf anywhere;
# instance state and CODEX_HOME are never deleted; hpc/Para/Japan and remote
# jobs are never touched; no secret/domain content is printed (path
# references and stage names only). launchctl/TCC/permission errors are
# reported with the exact next steps and are never retried in a loop.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"
. "$ROOT/scripts/pid_guard_lib.sh"
. "$ROOT/scripts/supervisor_control.sh"
. "$ROOT/scripts/host_ops_lock_lib.sh"

STAGE=""
AR_LOG=""

log() { printf '[activate-autorecovery] %s\n' "$*"; }
die() {
  printf '[activate-autorecovery] stage=%s error: %s\n' "${STAGE:-<start>}" "$*" >&2
  printf 'AUTORECOVERY_ACTIVATION_OK NO\n' >&2
  exit 1
}
stage() { STAGE="$1"; log "stage: $1"; }

# Resolve the run log under the local instance runtime dir (idempotent).
ensure_ar_log() {
  local runtime
  runtime="$(bridge_instance_get local runtime_dir 2>/dev/null || true)"
  [[ -n "$runtime" ]] || runtime="$ROOT/.runtime"
  AR_LOG="$runtime/activate-autorecovery.log"
  mkdir -p "$(dirname "$AR_LOG")"
}

# Prints the public https base URL for the fixed endpoint (value never
# printed by callers; returns 1 when no domain is configured).
resolve_public_url() {
  local domain="" ref=""
  ref="$(bridge_instance_get maintenance ngrok_domain_file 2>/dev/null || true)"
  if [[ -n "$ref" && -f "$ref" ]]; then
    domain="$(tr -d '[:space:]' < "$ref" 2>/dev/null || true)"
  fi
  [[ -n "$domain" ]] || domain="$(tr -d '[:space:]' < "$ROOT/.ngrok_domain" 2>/dev/null || true)"
  [[ -n "$domain" ]] || return 1
  printf 'https://%s' "$domain"
}

verify_maintenance_preflight() {
  stage preflight
  ensure_ar_log
  if [[ -n "${BRIDGE_INSTANCE:-}" && "$BRIDGE_INSTANCE" != local && "$BRIDGE_INSTANCE" != maintenance ]]; then
    die "BRIDGE_INSTANCE=$BRIDGE_INSTANCE is already exported; this script only manages local/maintenance (hpc and remote jobs are never touched)"
  fi
  bridge_instance_exists maintenance || die \
    "maintenance instance has no config ($(bridge_instance_config maintenance)); open a window with ./scripts/activate_maintenance_instance.sh first"
  bridge_instance_exists local || die \
    "local instance has no config ($(bridge_instance_config local)); run ./scripts/bridge_instance.sh migrate-current --apply first"
  local maint_port maint_url public_url
  maint_port="$(bridge_instance_get maintenance port)"
  [[ "$maint_port" == "8323" ]] || die "maintenance port is $maint_port (expected 8323)"
  maint_url="http://127.0.0.1:$maint_port"
  public_url="$(resolve_public_url)" || die \
    "no ngrok domain configured for the maintenance window; the fixed endpoint is required"
  bridge_health_identity "$maint_url" maintenance bridge-workspace "$maint_port" "$AR_LOG" || die \
    "local /health on port $maint_port is not maintenance/bridge-workspace/8323; the maintenance window must be ACTIVE (run ./scripts/activate_maintenance_instance.sh in a normal Terminal)"
  bridge_health_identity "$public_url" maintenance bridge-workspace "$maint_port" "$AR_LOG" || die \
    "public /health is not served by maintenance (maintenance/bridge-workspace/8323); the maintenance window must be ACTIVE before this script runs"
  log "preflight OK: local+public identity = maintenance/bridge-workspace/8323"
}

ensure_local_pause_marker() {
  stage pause-marker
  local marker
  marker="$(bridge_pause_marker)"
  [[ -n "$marker" ]] || die "cannot resolve the local pause marker path"
  mkdir -p "$(dirname "$marker")"
  touch "$marker"
  chmod 600 "$marker"
  log "local supervisor pause marker ensured (idempotent): $marker"
}

install_and_verify_runtime() {
  stage install-runtime
  ensure_ar_log
  if ! bash "$ROOT/scripts/install_runtime.sh" --instance local >>"$AR_LOG" 2>&1; then
    die "install_runtime.sh --instance local failed (see $AR_LOG); re-run ./scripts/status_launch_agent.sh --instance local after fixing"
  fi
  local data_root current bad
  data_root="$(bridge_data_root)"
  current="$data_root/current"
  [[ -L "$current" ]] || die "runtime 'current' symlink missing after install ($current)"
  [[ -f "$current/.runtime-build-info" || -f "$current/runtime.manifest" ]] || die \
    "runtime copy has no managed-release marker (.runtime-build-info/runtime.manifest)"
  grep -q '^release=' "$current/.runtime-build-info" 2>/dev/null || grep -q '^release=' "$current/runtime.manifest" 2>/dev/null || die \
    "runtime build-info lacks the release field"
  [[ "$data_root" != "$HOME/Desktop/"* ]] || die \
    "runtime data root must not live under Desktop (TCC): $data_root"
  for bad in .git tests docs .bridge_api_key .ngrok_domain .bridge_sandbox_mode openapi.ngrok.yaml; do
    if [[ -e "$current/$bad" ]]; then
      die "runtime copy must not contain '$bad' (found $current/$bad)"
    fi
  done
  log "runtime verified: $current (release $(basename "$(readlink "$current")"), marker present, off-Desktop, no secret/.git/tests/docs)"
}

install_and_verify_agent() {
  stage install-launch-agent
  ensure_ar_log
  if ! bash "$ROOT/scripts/install_launch_agent.sh" --instance local >>"$AR_LOG" 2>&1; then
    die "install_launch_agent.sh --instance local failed (see $AR_LOG); run ./scripts/status_launch_agent.sh --instance local"
  fi
  local label plist data_root local_runtime bpid npid
  label="$(bridge_supervisor_label)"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  [[ -f "$plist" ]] || die "agent plist missing: $plist"
  data_root="$(bridge_data_root)"
  if ! python3 - "$plist" "$label" "$data_root" 2>>"$AR_LOG" <<'PY'; then
import plistlib
import sys

plist_path, label, data_root = sys.argv[1:4]
with open(plist_path, "rb") as fh:
    p = plistlib.load(fh)
if p.get("Label") != label:
    raise SystemExit("label mismatch")
args = p.get("ProgramArguments") or []
expected = ["/bin/bash", data_root + "/current/scripts/run_local_supervisor.sh",
            "--instance", "local"]
if args != expected:
    raise SystemExit("ProgramArguments mismatch: %r" % (args,))
PY
    die "generated plist verification failed ($plist; see $AR_LOG)"
  fi
  local_runtime="$(bridge_instance_get local runtime_dir)"
  bpid="$(cat "$local_runtime/bridge.pid" 2>/dev/null || true)"
  npid="$(cat "$local_runtime/ngrok.pid" 2>/dev/null || true)"
  if managed_bridge_pid "$bpid" "$ROOT" "$local_runtime"; then
    die "local bridge is running as a managed child during the maintenance window (pid $bpid); the fixed endpoint must stay with maintenance"
  fi
  if managed_ngrok_pid "$npid" "$(bridge_instance_get local port)"; then
    die "local ngrok is running as a managed child during the maintenance window (pid $npid); the fixed endpoint must stay with maintenance"
  fi
  log "agent verified: $label plist -> $data_root/current/scripts/run_local_supervisor.sh --instance local; no local child holds the endpoint"
}

leave_window_and_verify_health() {
  stage deactivate-maintenance
  ensure_ar_log
  if ! bash "$ROOT/scripts/deactivate_maintenance_instance.sh" >>"$AR_LOG" 2>&1; then
    die "deactivate_maintenance_instance.sh failed (see $AR_LOG); run ./scripts/status_launch_agent.sh --instance local"
  fi
  local local_port local_url public_url ok i
  local_port="$(bridge_instance_get local port)"
  [[ "$local_port" == "8321" ]] || die "local port is $local_port (expected 8321)"
  local_url="http://127.0.0.1:$local_port"
  public_url="$(resolve_public_url)" || die "cannot resolve the public endpoint after deactivate"
  ok=0
  for i in $(seq 1 "${AR_HEALTH_WAIT:-30}"); do
    if bridge_health_identity "$local_url" local bridge-workspace "$local_port" "$AR_LOG"; then
      ok=1
      break
    fi
    sleep 1
  done
  [[ "$ok" == 1 ]] || die "local health did not recover to local/bridge-workspace/8321 (see $AR_LOG)"
  bridge_health_identity "$public_url" local bridge-workspace "$local_port" "$AR_LOG" || die \
    "public endpoint did not return to local/bridge-workspace/8321 after deactivate (see $AR_LOG)"
  log "health verified after deactivate: local+public identity = local/bridge-workspace/8321"
}

# Bridge-only crash-recovery self-test. The bridge pid is read from the local
# runtime pid file and must pass the identity guard before the TERM; then the
# supervisor is expected to start a DIFFERENT pid and restore local+public
# health. ngrok is never killed here.
bridge_crash_recovery() {
  stage bridge-crash-recovery
  ensure_ar_log
  if [[ "${AR_CRASH_RECOVERY:-1}" == "0" ]]; then
    log "bridge crash-recovery self-test skipped (AR_CRASH_RECOVERY=0); ngrok is never killed by this script"
    return 0
  fi
  local spid local_runtime bridge_pid_file bpid new recovered local_port local_url public_url i
  spid="$(supervisor_pid)"
  if [[ -z "$spid" ]]; then
    die "no managed supervisor running after deactivate (supervisor.pid missing or identity not verified); auto-recovery is not active"
  fi
  log "supervisor verified running (pid $spid)"
  local_runtime="$(bridge_instance_get local runtime_dir)"
  [[ -n "$local_runtime" ]] || die "local runtime_dir missing from instance config"
  bridge_pid_file="$local_runtime/bridge.pid"
  bpid="$(cat "$bridge_pid_file" 2>/dev/null || true)"
  if [[ -z "$bpid" ]] || ! managed_bridge_pid "$bpid" "$ROOT" "$local_runtime"; then
    die "bridge pid from $bridge_pid_file is not a managed child (pid guard rejected it); refusing to TERM"
  fi
  local_port="$(bridge_instance_get local port)"
  local_url="http://127.0.0.1:$local_port"
  public_url="$(resolve_public_url)" || die "cannot resolve the public endpoint for the recovery check"
  log "TERM managed bridge pid $bpid (identity verified via pid_guard_lib.sh); waiting for the supervisor to start a different pid"
  kill "$bpid" 2>>"$AR_LOG" || true
  recovered=0
  new=""
  for i in $(seq 1 "${AR_RECOVERY_WAIT:-45}"); do
    new="$(cat "$bridge_pid_file" 2>/dev/null || true)"
    if [[ -n "$new" && "$new" != "$bpid" ]] && managed_bridge_pid "$new" "$ROOT" "$local_runtime"; then
      if bridge_health_identity "$local_url" local bridge-workspace "$local_port" "$AR_LOG" \
         && bridge_health_identity "$public_url" local bridge-workspace "$local_port" "$AR_LOG"; then
        recovered=1
        break
      fi
    fi
    sleep 1
  done
  [[ "$recovered" == 1 ]] || die \
    "bridge crash recovery did not converge (new pid + local/public health) within ${AR_RECOVERY_WAIT:-45}s (see $AR_LOG)"
  log "bridge crash recovery OK: pid $bpid -> $new, local+public health restored (ngrok not killed)"
}

main() {
  # Global single-writer host-ops lock: only one control-plane mutation runs
  # at a time. When this script is called by the bootstrap orchestration the
  # exported token makes it reentrant; released automatically on EXIT.
  host_ops_lock_acquire "activate-runtime-autorecovery" || die \
    "another host operation holds the host-ops lock; concurrent control-plane writes are refused (retry after it exits)"
  verify_maintenance_preflight
  ensure_local_pause_marker
  install_and_verify_runtime
  install_and_verify_agent
  leave_window_and_verify_health
  bridge_crash_recovery
  stage final-status
  ensure_ar_log
  local status_rc=0
  set +e
  bash "$ROOT/scripts/status_launch_agent.sh" --instance local >>"$AR_LOG" 2>&1
  status_rc=$?
  set -e
  if [[ "$status_rc" != 0 ]]; then
    die "status_launch_agent.sh --instance local reported problems (exit $status_rc); full output: $AR_LOG"
  fi
  log "status_launch_agent.sh --instance local: OK (output in $AR_LOG)"
  log "done: local runtime auto-recovery installed and verified (launchd supervisor + stable runtime + per-instance agent)"
  printf 'AUTORECOVERY_ACTIVATION_OK YES\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
