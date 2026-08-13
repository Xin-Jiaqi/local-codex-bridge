#!/usr/bin/env bash
#
# Report the state of the Local Codex Bridge launchd LaunchAgent, the
# bridge/ngrok processes recorded in .runtime, and local/public health.
# Exit code 0 = everything healthy; 1 = something needs attention.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_mode_lib.sh"
. "$ROOT/scripts/bridge_instance_lib.sh"
. "$ROOT/scripts/pid_guard_lib.sh"

LABEL="com.local.codex-bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
SELECTED=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance)
      [[ $# -ge 2 ]] || { echo "error: --instance requires a value" >&2; exit 2; }
      SELECTED="$2"; shift
      ;;
    *) echo "error: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ -z "$SELECTED" && -n "${BRIDGE_INSTANCE:-}" ]]; then
  SELECTED="$BRIDGE_INSTANCE"
fi
if [[ -n "$SELECTED" ]]; then
  if ! bridge_instance_valid "$SELECTED"; then
    echo "error: invalid instance '$SELECTED' (use local|hpc|maintenance)" >&2
    exit 2
  fi
  if [[ "$SELECTED" == "local" ]]; then
    LABEL="$(bridge_supervisor_label)"
  else
    LABEL="com.local.codex-bridge.$SELECTED"
  fi
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
fi

RUNTIME_DIR="$ROOT/.runtime"
BRIDGE_PID_FILE="$RUNTIME_DIR/bridge.pid"
NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"
BRIDGE_URL="http://127.0.0.1:8321"

bad=0

INSTANCE=""
INSTANCE_DIR=""
INSTANCE_MODE=""
resolve_instance_status() {
  local requested="${SELECTED:-}"
  if [[ -z "$requested" ]]; then
    requested="$(bridge_instance_effective 2>/dev/null)" || {
      echo "  error: invalid BRIDGE_INSTANCE=$BRIDGE_INSTANCE (use local|hpc|maintenance)" >&2
      bad=1
      return
    }
  fi
  if bridge_instance_exists "$requested"; then
    INSTANCE="$requested"
    INSTANCE_DIR="$(bridge_instance_dir "$INSTANCE")"
    INSTANCE_MODE="$(bridge_instance_get "$INSTANCE" mode)"
    BRIDGE_PORT="$(bridge_instance_get "$INSTANCE" port)"
    RUNTIME_DIR="$(bridge_instance_get "$INSTANCE" runtime_dir)"
    BRIDGE_URL="http://127.0.0.1:$BRIDGE_PORT"
    BRIDGE_PID_FILE="$RUNTIME_DIR/bridge.pid"
    NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"
    if coll="$(bridge_instance_collision "$INSTANCE")"; then
      echo "  FAIL: instance '$INSTANCE' collides with '${coll#*:}' on ${coll%%:*} (concurrent instances need distinct ports and runtime dirs)" >&2
      bad=1
    fi
  else
    echo "  warning: no instance config for '$requested' ($(bridge_instance_config "$requested")); showing legacy singleton state (run ./scripts/bridge_instance.sh migrate-current --apply)" >&2
    bad=1
  fi
}
resolve_instance_status

echo "== instance =="
if [[ -n "$INSTANCE" ]]; then
  echo "  instance: $INSTANCE (config: $(bridge_instance_config "$INSTANCE"))"
  echo "  mode: $INSTANCE_MODE | approval_policy: $(bridge_instance_get "$INSTANCE" approval_policy) | network_access: $(bridge_instance_get "$INSTANCE" network_access)"
  echo "  codex_home: $(bridge_instance_get "$INSTANCE" codex_home) | port: $BRIDGE_PORT"
  echo "  runtime_dir: $RUNTIME_DIR"
else
  echo "  instance: <legacy singleton> (not migrated)"
fi

echo "== sandbox mode =="
if [[ -n "$INSTANCE" ]]; then
  echo "  effective (instance config): $INSTANCE_MODE"
else
  echo "  effective: $(bridge_mode_effective "$ROOT") (source: $(bridge_mode_source "$ROOT"); file: $(bridge_mode_file "$ROOT"))"
fi

echo "== runtime source =="
CURRENT="$(bridge_data_root)/current"
if [[ -L "$CURRENT" ]]; then
  release="$(basename "$(readlink "$CURRENT" 2>/dev/null || true)")"
  if [[ -f "$CURRENT/scripts/run_local_supervisor.sh" ]]; then
    echo "  runtime: INSTALLED"
    echo "  runtime_source: $(readlink "$CURRENT")"
    echo "  release: ${release:-<unknown>}"
  else
    echo "  runtime: INCOMPLETE ($CURRENT exists but supervisor script missing) - re-run ./scripts/install_runtime.sh --instance local"
    bad=1
  fi
else
  echo "  runtime: not installed ($CURRENT missing) - run ./scripts/install_runtime.sh --instance local for auto-recovery"
fi

if [[ -n "$INSTANCE" && "$INSTANCE" == "local" ]]; then
  echo "== supervisor (local) =="
  sentinel="$RUNTIME_DIR/supervisor.enabled"
  if [[ -f "$sentinel" ]]; then
    echo "  supervisor: ENABLED (sentinel: $sentinel)"
  else
    echo "  supervisor: disabled"
  fi
  pause_file="$(bridge_instance_dir local)/pause.marker"
  if [[ -f "$pause_file" ]]; then
    echo "  supervisor: PAUSED for maintenance window ($pause_file present; children stay stopped)"
  fi
  spid=""
  if [[ -f "$RUNTIME_DIR/supervisor.pid" ]]; then
    spid="$(cat "$RUNTIME_DIR/supervisor.pid" 2>/dev/null || true)"
  fi
  if [[ -n "$spid" ]] && managed_supervisor_pid "$spid"; then
    echo "  supervisor_pid: running (pid $spid)"
  else
    echo "  supervisor_pid: not running"
  fi
fi

pid_alive() { kill -0 "$1" 2>/dev/null; }
is_valid_pid() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 > 1 )); }

echo "== LaunchAgent ($LABEL) =="
if [[ -f "$PLIST" ]]; then
  echo "  plist: present ($PLIST)"
else
  echo "  plist: MISSING ($PLIST) - run ./scripts/install_launch_agent.sh"
  bad=1
fi
if [[ -z "$SELECTED" && -n "$INSTANCE" ]]; then
  echo "  note: instance '$INSTANCE' has a config; check its agent with: ./scripts/status_launch_agent.sh --instance $INSTANCE"
fi

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  state="$(launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | sed -nE 's/^[[:space:]]*state = (.*)/\1/p' | head -1)"
  echo "  agent: loaded (state: ${state:-unknown})"
  if [[ "${state:-}" == "exited" ]]; then
    echo "  note: state 'exited' is expected - the start script backgrounds the processes and exits"
  fi
else
  echo "  agent: NOT loaded - run ./scripts/install_launch_agent.sh"
  bad=1
fi

echo "== processes (.runtime pid files) =="
check_pid_file() {
  local file="$1" name="$2" pid
  if [[ ! -f "$file" ]]; then
    echo "  $name: no pid file (not started by this repo's scripts)"
    bad=1
    return
  fi
  pid="$(cat "$file" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && is_valid_pid "$pid" && pid_alive "$pid"; then
    echo "  $name: running (pid $pid)"
  else
    echo "  $name: pid '$pid' is not running (stale; next start will clean it up)"
    bad=1
  fi
}
check_pid_file "$BRIDGE_PID_FILE" "bridge"
check_pid_file "$NGROK_PID_FILE" "ngrok"

echo "== health =="
if curl -fsS -m 3 "$BRIDGE_URL/health" >/dev/null 2>&1; then
  echo "  local: OK ($BRIDGE_URL/health)"
else
  echo "  local: DOWN ($BRIDGE_URL/health)"
  bad=1
fi

public_url_file="$ROOT/.public_url"
if [[ -n "$INSTANCE" ]]; then
  public_url_file="$INSTANCE_DIR/public_url"
fi
if [[ -f "$public_url_file" ]]; then
  public_url="$(tr -d '[:space:]' < "$public_url_file")"
  if curl -fsS -m 5 "$public_url/health" >/dev/null 2>&1; then
    echo "  public: OK ($public_url/health)"
  else
    echo "  public: DOWN ($public_url/health)"
    bad=1
  fi
else
  echo "  public: skipped ($public_url_file not found - run start_ngrok_bridge.sh once)"
fi

exit "$bad"
