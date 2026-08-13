#!/usr/bin/env bash
#
# PID identity guard shared by start_ngrok_bridge.sh / stop_ngrok_bridge.sh.
#
# Rule: never signal or reuse a process based on a bare numeric PID. PID
# numbers can be recycled; before SIGTERM/SIGKILL or PID-file reuse the
# process identity must be verified read-only via `ps -p PID -o command=`:
#
#   - bridge: command line contains `http_server` AND `--log <runtime>/`
#     where <runtime> is the legacy <root>/.runtime/ or an instance runtime
#     dir under ${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/
#     (the bridge is spawned as `python3 -m http_server ... --log $RUNTIME_DIR/bridge.log`);
#   - ngrok:   command line contains `ngrok` AND `http <port>` (spawned as
#     `<ngrok-bin> http <port> --url <domain>`).
#
# When verification fails the pid is reported as stale/unmanaged and is NEVER
# killed; only the project's own bookkeeping pid file may be removed.
# pkill/killall are never used.
#
# Sourcing has no side effects; no secrets involved.
#
# Functions:
#   is_valid_pid PID
#   pid_alive PID
#   proc_command PID                 # command line string (empty when unknown)
#   is_bridge_command CMD ROOT [RUNTIME_DIR]   # pure string match, no process access
#   is_ngrok_command CMD PORT        # pure string match, no process access
#   managed_bridge_pid PID ROOT [RUNTIME_DIR]  # alive AND identity verified
#   managed_ngrok_pid PID PORT       # alive AND identity verified
#   managed_supervisor_pid PID       # alive AND identity verified
#   report_unmanaged ROLE PID CMD    # stderr report, no kill

is_valid_pid() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 > 1 )); }

pid_alive() { kill -0 "$1" 2>/dev/null; }

proc_command() {
  local pid="$1"
  if ! is_valid_pid "$pid"; then
    printf ''
    return 1
  fi
  ps -p "$pid" -o command= 2>/dev/null || true
}

is_bridge_command() {
  local cmd="$1" root="$2" runtime="${3:-}"
  [[ -n "$cmd" && "$cmd" == *"http_server"* ]] || return 1
  if [[ -n "$runtime" ]]; then
    [[ "$cmd" == *"$runtime/"* ]]
  else
    [[ "$cmd" == *"$root/.runtime/"* ]]
  fi
}

is_ngrok_command() {
  local cmd="$1" port="$2"
  [[ -n "$cmd" && "$cmd" == *"ngrok"* && "$cmd" == *"http $port"* ]]
}

# The launchd supervisor runs in the foreground as
#   run_local_supervisor.sh --instance local
# (from the repo or from the stable runtime copy).
is_supervisor_command() {
  local cmd="$1"
  [[ -n "$cmd" && "$cmd" == *"run_local_supervisor.sh"* && "$cmd" == *"--instance local"* ]]
}

managed_bridge_pid() {
  local pid="$1" root="$2" runtime="${3:-}" cmd
  if ! is_valid_pid "$pid" || ! pid_alive "$pid"; then
    return 1
  fi
  cmd="$(proc_command "$pid")"
  is_bridge_command "$cmd" "$root" "$runtime"
}

managed_ngrok_pid() {
  local pid="$1" port="$2" cmd
  if ! is_valid_pid "$pid" || ! pid_alive "$pid"; then
    return 1
  fi
  cmd="$(proc_command "$pid")"
  is_ngrok_command "$cmd" "$port"
}

managed_supervisor_pid() {
  local pid="$1" cmd
  if ! is_valid_pid "$pid" || ! pid_alive "$pid"; then
    return 1
  fi
  cmd="$(proc_command "$pid")"
  is_supervisor_command "$cmd"
}

report_unmanaged() {
  local role="$1" pid="$2" cmd="${3:-<unknown>}"
  printf "[pid-guard] %s: pid %s is not this project's managed %s (command: %s); stale/unmanaged, NOT killing it\n" \
    "$role" "$pid" "$role" "$cmd" >&2
}
