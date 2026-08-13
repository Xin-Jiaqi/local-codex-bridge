#!/usr/bin/env bash
#
# Install the launchd LaunchAgent for Local Codex Bridge.
#
# New model (per-instance supervisor agent, LOCAL only):
#   ./scripts/install_launch_agent.sh --instance local
#   - installs com.local.codex-bridge.local whose ProgramArguments run the
#     foreground supervisor from the stable NON-Desktop runtime copy
#     (${XDG_DATA_HOME:-$HOME/.local/share}/local-codex-bridge/current/
#     scripts/run_local_supervisor.sh --instance local);
#   - RunAtLoad=true; KeepAlive is bound to the ABSOLUTE instance-state
#     supervisor.enabled sentinel via PathState (never unconditional true);
#     ThrottleInterval=10; the plist contains no secret/domain values;
#   - requires the runtime to be installed first
#     (./scripts/install_runtime.sh --instance local);
#   - creates the supervisor.enabled sentinel (enables auto-recovery);
#   - safely migrates/backs up the legacy agent (com.local.codex-bridge) and
#     any old Desktop-pointing plist: loaded agents are booted out (no
#     pkill/killall; legacy backgrounded children keep running because the
#     old plist used AbandonProcessGroup), files are moved to a timestamped
#     .bak-* instead of being deleted;
#   - refuses hpc/maintenance: those instances are on-demand and are NEVER
#     auto-managed.
#
# Legacy model (unchanged, for non-instance setups):
#   ./scripts/install_launch_agent.sh
#   - installs com.local.codex-bridge running scripts/start_ngrok_bridge.sh;
#   - --sandbox-mode/--network-access apply only to these legacy installs.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"

LABEL="com.local.codex-bridge"
TEMPLATE="$ROOT/scripts/launch_agent/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
FORCE=0
INSTANCE=""
SANDBOX_MODE=""
NETWORK_ACCESS=""

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] error: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1 ;;
    --sandbox-mode)
      [[ $# -ge 2 ]] || die "--sandbox-mode requires a value"
      SANDBOX_MODE="$2"; shift
      ;;
    --network-access)
      [[ $# -ge 2 ]] || die "--network-access requires a value"
      NETWORK_ACCESS="$2"; shift
      ;;
    --instance)
      [[ $# -ge 2 ]] || die "--instance requires a value"
      INSTANCE="$2"; shift
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

LOG_DIR="$ROOT/.runtime"
if [[ -n "$INSTANCE" ]]; then
  bridge_instance_valid "$INSTANCE" || die "--instance=$INSTANCE is invalid (use local|hpc|maintenance)"
  if [[ "$INSTANCE" != "local" ]]; then
    die "--instance=$INSTANCE is not supported by the supervisor agent: only the explicit local instance is auto-managed (hpc/maintenance are on-demand; run start/stop_ngrok_bridge.sh with BRIDGE_INSTANCE=$INSTANCE instead)"
  fi
  [[ -z "$SANDBOX_MODE" && -z "$NETWORK_ACCESS" ]] || die "--sandbox-mode/--network-access only apply to legacy installs; the local supervisor agent derives everything from the instance config and the runtime"
  bridge_instance_exists local || die "no config for the local instance ($(bridge_instance_config local)) - run ./scripts/bridge_instance.sh migrate-current --apply first"
  if coll="$(bridge_instance_collision local)"; then
    die "local instance collides with '${coll#*:}' on ${coll%%:*} (fail closed; concurrent instances need distinct ports and runtime dirs)"
  fi

  LABEL="$(bridge_supervisor_label)"
  TEMPLATE="$ROOT/scripts/launch_agent/$LABEL.plist"
  DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
  DATA_ROOT="$(bridge_data_root)"
  CURRENT="$DATA_ROOT/current"
  RUNTIME_DIR="$(bridge_instance_get local runtime_dir)"
  [[ -n "$RUNTIME_DIR" && "$RUNTIME_DIR" == /* ]] || die "local runtime_dir missing from instance config"
  LOG_DIR="$RUNTIME_DIR"
  [[ -f "$TEMPLATE" ]] || die "template not found: $TEMPLATE"
  [[ -f "$CURRENT/scripts/run_local_supervisor.sh" ]] || die \
    "runtime not installed: $CURRENT/scripts/run_local_supervisor.sh not found (run ./scripts/install_runtime.sh --instance local first)"
  [[ -f "$CURRENT/.runtime-build-info" || -f "$CURRENT/runtime.manifest" ]] || die \
    "runtime not installed: $CURRENT has no managed-release marker (.runtime-build-info/runtime.manifest); refusing to install the agent over an unmanaged directory (run ./scripts/install_runtime.sh --instance local first)"
  mkdir -p "$HOME/Library/LaunchAgents" "$RUNTIME_DIR"

  # --- legacy migration: bootout if loaded, then BACK UP (never delete). ---
  LEGACY_LABEL="com.local.codex-bridge"
  LEGACY_DEST="$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
  if launchctl print "gui/$UID_NUM/$LEGACY_LABEL" >/dev/null 2>&1; then
    if launchctl bootout "gui/$UID_NUM/$LEGACY_LABEL" >/dev/null 2>&1; then
      log "legacy agent $LEGACY_LABEL unloaded (backgrounded children untouched)"
    else
      log "warning: could not boot out legacy agent $LEGACY_LABEL"
    fi
  fi
  if [[ -f "$LEGACY_DEST" ]]; then
    mv "$LEGACY_DEST" "$LEGACY_DEST.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    log "legacy plist backed up (not deleted): $LEGACY_DEST.bak-*"
  fi
  # Backup any previous local-agent plist (e.g. an old Desktop-pointing one).
  if [[ -f "$DEST" ]]; then
    mv "$DEST" "$DEST.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    log "previous $LABEL plist backed up: $DEST.bak-*"
  fi

  python3 - "$TEMPLATE" "$DEST" "$DATA_ROOT" "$RUNTIME_DIR" "$HOME" "$LABEL" "$LOG_DIR" <<'PY'
import sys
src, dst, data_root, state_runtime, home, label, log_dir = sys.argv[1:8]
data = open(src, encoding="utf-8").read()
data = (data.replace("__DATA_ROOT__", data_root)
            .replace("__STATE_RUNTIME__", state_runtime)
            .replace("__HOME__", home)
            .replace("__LABEL__", label)
            .replace("__LOG_DIR__", log_dir))
with open(dst, "w", encoding="utf-8") as f:
    f.write(data)
PY
  chmod 644 "$DEST"
  /usr/bin/plutil -lint "$DEST" >/dev/null || die "generated plist failed lint: $DEST"
  log "plist written to $DEST (ProgramArguments: $CURRENT/scripts/run_local_supervisor.sh --instance local)"

  SENTINEL="$RUNTIME_DIR/supervisor.enabled"
  if [[ ! -f "$SENTINEL" ]]; then
    touch "$SENTINEL"
    log "supervisor enabled: $SENTINEL"
  fi

  if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
    if [[ "$FORCE" -eq 1 ]]; then
      log "agent already loaded; booting it out first (--force)"
      launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
    else
      log "agent already loaded; nothing to do (use --force to reload)"
      exit 0
    fi
  fi

  launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  if ! launchctl bootstrap "gui/$UID_NUM" "$DEST"; then
    die "launchctl bootstrap failed; run ./scripts/status_launch_agent.sh"
  fi
  launchctl kickstart "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
  log "LaunchAgent installed and loaded: $LABEL"
  log "instance: local (BRIDGE_INSTANCE pinned via --instance local; state derived from $(bridge_instance_config local))"
  log "runtime: $CURRENT (release: $(basename "$(readlink "$CURRENT")"))"
  log "supervisor auto-recovery enabled (sentinel: $SENTINEL); bridge/ngrok restarts are handled by the supervisor, launchd restores the supervisor after crash/login/reboot"
  log "check status with: ./scripts/status_launch_agent.sh --instance local"
  exit 0
fi

# ---------------------------------------------------------------------------
# Legacy branch (unchanged behavior).
# ---------------------------------------------------------------------------
[[ -f "$TEMPLATE" ]] || die "template not found: $TEMPLATE"
mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

python3 - "$TEMPLATE" "$DEST" "$ROOT" "$HOME" "$LABEL" "$LOG_DIR" "$INSTANCE" "$SANDBOX_MODE" "$NETWORK_ACCESS" <<'PY'
import sys
src, dst, root, home, label, log_dir, instance, sandbox_mode, network_access = sys.argv[1:10]
data = open(src, encoding="utf-8").read()
data = (data.replace("__PROJECT_ROOT__", root)
            .replace("__HOME__", home)
            .replace("__LABEL__", label)
            .replace("__LOG_DIR__", log_dir))
extra = ""
if instance:
    extra += "\t\t<key>BRIDGE_INSTANCE</key>\n\t\t<string>%s</string>\n" % instance
if sandbox_mode:
    extra += "\t\t<key>BRIDGE_SANDBOX_MODE</key>\n\t\t<string>%s</string>\n" % sandbox_mode
if network_access:
    extra += "\t\t<key>BRIDGE_NETWORK_ACCESS</key>\n\t\t<string>%s</string>\n" % network_access
data = data.replace("<!--__ENVIRONMENT_EXTRA__-->", extra)
with open(dst, "w", encoding="utf-8") as f:
    f.write(data)
PY
chmod 644 "$DEST"

/usr/bin/plutil -lint "$DEST" >/dev/null || die "generated plist failed lint: $DEST"
log "plist written to $DEST"

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  if [[ "$FORCE" -eq 1 ]]; then
    log "agent already loaded; booting it out first (--force)"
    launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
  else
    log "agent already loaded; nothing to do (use --force to reload)"
    exit 0
  fi
fi

launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
if ! launchctl bootstrap "gui/$UID_NUM" "$DEST"; then
  die "launchctl bootstrap failed (see $ROOT/.runtime/launchagent.err.log); run ./scripts/status_launch_agent.sh"
fi

log "LaunchAgent installed and loaded: $LABEL"
if [[ -n "$INSTANCE" ]]; then
  log "instance: $INSTANCE (BRIDGE_INSTANCE pinned in plist; state derived from $(bridge_instance_config "$INSTANCE"))"
elif [[ -n "$SANDBOX_MODE" || -n "$NETWORK_ACCESS" ]]; then
  log "sandbox env: BRIDGE_SANDBOX_MODE=${SANDBOX_MODE:-<unset>} BRIDGE_NETWORK_ACCESS=${NETWORK_ACCESS:-<unset>}"
fi
log "started now (RunAtLoad) and will start automatically at next login"
log "check status with: ./scripts/status_launch_agent.sh"
