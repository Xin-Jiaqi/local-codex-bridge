#!/usr/bin/env bash
#
# Report the state of the Local Codex Bridge launchd LaunchAgent, the
# bridge/ngrok processes recorded in .runtime, and local/public health.
# Exit code 0 = everything healthy; 1 = something needs attention.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LABEL="com.local.codex-bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
RUNTIME_DIR="$ROOT/.runtime"
BRIDGE_PID_FILE="$RUNTIME_DIR/bridge.pid"
NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"
BRIDGE_URL="http://127.0.0.1:8321"

bad=0

pid_alive() { kill -0 "$1" 2>/dev/null; }
is_valid_pid() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 > 1 )); }

echo "== LaunchAgent ($LABEL) =="
if [[ -f "$PLIST" ]]; then
  echo "  plist: present ($PLIST)"
else
  echo "  plist: MISSING ($PLIST) - run ./scripts/install_launch_agent.sh"
  bad=1
fi

if /bin/launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  state="$(/bin/launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | sed -nE 's/^[[:space:]]*state = (.*)/\1/p' | head -1)"
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

if [[ -f "$ROOT/.public_url" ]]; then
  public_url="$(tr -d '[:space:]' < "$ROOT/.public_url")"
  if curl -fsS -m 5 "$public_url/health" >/dev/null 2>&1; then
    echo "  public: OK ($public_url/health)"
  else
    echo "  public: DOWN ($public_url/health)"
    bad=1
  fi
else
  echo "  public: skipped (.public_url not found - run start_ngrok_bridge.sh once)"
fi

exit "$bad"
