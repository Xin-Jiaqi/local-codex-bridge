#!/usr/bin/env bash
#
# Stop only the Bridge/ngrok processes started by scripts/start_ngrok_bridge.sh.
# PIDs are read from .runtime/bridge.pid and .runtime/ngrok.pid; only those
# exact PIDs are killed. pkill/killall are never used and unmanaged processes
# are never touched. Stale or invalid pid files are removed safely.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUNTIME_DIR="$ROOT/.runtime"
BRIDGE_PID_FILE="$RUNTIME_DIR/bridge.pid"
NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"

is_valid_pid() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 > 1 )); }

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
