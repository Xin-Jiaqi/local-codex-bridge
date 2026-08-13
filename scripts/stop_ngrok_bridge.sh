#!/usr/bin/env bash
#
# Stop only the Bridge/ngrok processes started by scripts/start_ngrok_bridge.sh.
# PIDs are read from .runtime/bridge.pid and .runtime/ngrok.pid. Before any
# SIGTERM/SIGKILL the process identity is verified read-only via `ps` against
# this project's managed command shapes (see scripts/pid_guard_lib.sh); a pid
# that is alive but does NOT match is reported as stale/unmanaged and is NEVER
# killed — only the project's own pid file is removed. pkill/killall are never
# used.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
. "$ROOT/scripts/bridge_instance_lib.sh"
. "$ROOT/scripts/pid_guard_lib.sh"

RUNTIME_DIR="$ROOT/.runtime"
BRIDGE_PID_FILE="$RUNTIME_DIR/bridge.pid"
NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"
BRIDGE_PORT="8321"

# Pin to the same instance the start script uses (BRIDGE_INSTANCE, default
# local); absent instance config -> legacy singleton fallback with a warning.
if requested="$(bridge_instance_effective 2>/dev/null)" && bridge_instance_exists "$requested"; then
  if coll="$(bridge_instance_collision "$requested")"; then
    echo "[stop] error: instance '$requested' collides with '${coll#*:}' on ${coll%%:*} (fail closed; concurrent instances need distinct ports and runtime dirs)" >&2
    exit 1
  fi
  RUNTIME_DIR="$(bridge_instance_get "$requested" runtime_dir)"
  BRIDGE_PID_FILE="$RUNTIME_DIR/bridge.pid"
  NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"
  BRIDGE_PORT="$(bridge_instance_get "$requested" port)"
  echo "[stop] instance: $requested (runtime: $RUNTIME_DIR)"
else
  echo "[stop] warning: no instance config; using legacy singleton state (.runtime)" >&2
fi

stop_pid_file() {
  local file="$1" name="$2" pid i
  if [[ ! -f "$file" ]]; then
    echo "[stop] $name: no pid file ($file), nothing to do"
    return 0
  fi
  pid="$(cat "$file" 2>/dev/null || true)"
  if [[ -z "$pid" ]] || ! is_valid_pid "$pid"; then
    echo "[stop] $name: invalid pid '$pid' in $file; removing stale file"
    rm -f "$file"
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[stop] $name: pid $pid is not running; removing stale file"
    rm -f "$file"
    return 0
  fi

  local cmd=""
  if [[ "$name" == "bridge" ]]; then
    if ! managed_bridge_pid "$pid" "$ROOT" "$RUNTIME_DIR"; then
      cmd="$(proc_command "$pid")"
      report_unmanaged "bridge" "$pid" "$cmd"
      echo "[stop] bridge: pid $pid is not this project's managed bridge; removing stale pid file without killing"
      rm -f "$file"
      return 0
    fi
  else
    if ! managed_ngrok_pid "$pid" "$BRIDGE_PORT"; then
      cmd="$(proc_command "$pid")"
      report_unmanaged "ngrok" "$pid" "$cmd"
      echo "[stop] ngrok: pid $pid is not this project's managed ngrok; removing stale pid file without killing"
      rm -f "$file"
      return 0
    fi
  fi

  echo "[stop] $name: sending SIGTERM to pid $pid"
  kill "$pid" 2>/dev/null || true
  for i in 1 2 3 4 5; do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "[stop] $name: pid $pid still alive after SIGTERM, sending SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$file"
  echo "[stop] $name: stopped (pid $pid)"
}

stop_pid_file "$NGROK_PID_FILE" "ngrok"
stop_pid_file "$BRIDGE_PID_FILE" "bridge"
echo "[stop] done"
