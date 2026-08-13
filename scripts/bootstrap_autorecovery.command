#!/usr/bin/env bash
#
# One-click (Finder double-click) host-admin bootstrap for the LOCAL
# auto-recovery stack. THIN ORCHESTRATOR ONLY: it calls the existing
# host-admin scripts in order and does not re-implement their logic.
#
# Flow:
#   1. read-only /health determines the current instance:
#      - maintenance already ACTIVE -> skip the initial activate;
#      - local serving -> open the maintenance window first
#        (./scripts/activate_maintenance_instance.sh);
#      - anything else (ambiguous / unhealthy) -> fail closed;
#   2. ./scripts/activate_runtime_autorecovery.sh: preflight maintenance ->
#      install stable runtime + per-instance LaunchAgent -> deactivate ->
#      local active + ONE bridge crash-recovery self-test (ends on local);
#   3. ./scripts/activate_maintenance_instance.sh re-opens the window so
#      ChatGPT can finish the current release work;
#   4. final read-only verification: local + public /health =
#      maintenance/bridge-workspace/8323; prints
#      BOOTSTRAP_AUTORECOVERY_OK YES and "you can close this window".
#
# Fail-closed: no pkill/killall/rm -rf, no retry loops, hpc/Para/Japan and
# remote jobs are never touched, no secret/domain content is printed (path
# references and stage names only). launchctl/TCC/permission errors surface
# the exact next steps and exit non-zero.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"

BOOT_LOG="$ROOT/.runtime/bootstrap.log"

log() { printf '[bootstrap-autorecovery] %s\n' "$*"; }
die() {
  trap - ERR
  printf '[bootstrap-autorecovery] error: %s\n' "$*" >&2
  printf 'BOOTSTRAP_AUTORECOVERY_OK NO\n' >&2
  exit 1
}
on_error() {
  local rc=$?
  trap - ERR
  printf '[bootstrap-autorecovery] error: unexpected failure (exit %s); no retry loop is attempted\n' "$rc" >&2
  printf 'BOOTSTRAP_AUTORECOVERY_OK NO\n' >&2
  exit 1
}
trap on_error ERR

# Prints the public https base URL for the fixed endpoint (value never
# printed by callers; returns 1 when no domain is configured). Glue only:
# the same path-reference resolution the host-admin scripts use.
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

# Read-only /health instance determination (never mutates anything).
current_instance() {
  local mport="" lport="" m=0 l=0
  mport="$(bridge_instance_get maintenance port 2>/dev/null || true)"
  lport="$(bridge_instance_get local port 2>/dev/null || true)"
  if [[ -n "$mport" ]] && bridge_health_identity "http://127.0.0.1:$mport" \
      maintenance bridge-workspace "$mport" "$BOOT_LOG"; then
    m=1
  fi
  if [[ -n "$lport" ]] && bridge_health_identity "http://127.0.0.1:$lport" \
      local bridge-workspace "$lport" "$BOOT_LOG"; then
    l=1
  fi
  if [[ "$m" == 1 && "$l" == 1 ]]; then
    die "both local and maintenance serve /health; refusing to guess (run ./scripts/status_launch_agent.sh --instance local in a normal Terminal)"
  fi
  [[ "$m" == 1 ]] && { printf 'maintenance\n'; return 0; }
  [[ "$l" == 1 ]] && { printf 'local\n'; return 0; }
  die "neither local nor maintenance serves a healthy /health; start the stack or open the window manually, then re-run this file"
}

main() {
  mkdir -p "$(dirname "$BOOT_LOG")"
  local current=""
  current="$(current_instance)"
  log "current instance: $current"
  if [[ "$current" == "local" ]]; then
    log "opening the maintenance window first"
    bash "$ROOT/scripts/activate_maintenance_instance.sh" || die \
      "activate_maintenance_instance.sh failed; no retry loop is attempted (fix and re-run this file)"
  else
    log "maintenance window already ACTIVE; skipping the initial activate"
  fi
  log "activating the local auto-recovery stack (runtime + LaunchAgent + crash recovery)"
  bash "$ROOT/scripts/activate_runtime_autorecovery.sh" || die \
    "activate_runtime_autorecovery.sh failed; no retry loop is attempted (see its stage output above)"
  log "re-opening the maintenance window for the remaining release work"
  bash "$ROOT/scripts/activate_maintenance_instance.sh" || die \
    "activate_maintenance_instance.sh (re-open) failed; no retry loop is attempted (local stays active and auto-recovered)"
  local mport="" murl="" public_url=""
  mport="$(bridge_instance_get maintenance port)"
  [[ "$mport" == "8323" ]] || die "maintenance port is $mport (expected 8323)"
  murl="http://127.0.0.1:$mport"
  public_url="$(resolve_public_url)" || die \
    "cannot resolve the public endpoint after re-entering the maintenance window"
  bridge_health_identity "$murl" maintenance bridge-workspace "$mport" "$BOOT_LOG" || die \
    "local /health is not maintenance/bridge-workspace/8323 after bootstrap (see $BOOT_LOG)"
  bridge_health_identity "$public_url" maintenance bridge-workspace "$mport" "$BOOT_LOG" || die \
    "public /health is not maintenance/bridge-workspace/8323 after bootstrap (see $BOOT_LOG)"
  log "verified: local + public identity = maintenance/bridge-workspace/8323"
  printf 'BOOTSTRAP_AUTORECOVERY_OK YES\n'
  printf 'you can close this window\n'
}

main "$@"
