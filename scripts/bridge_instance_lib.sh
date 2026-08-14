#!/usr/bin/env bash
#
# Read-only shared helpers for the pinned Bridge instances (local | hpc | maintenance).
# Sourced by scripts/bridge_instance.sh (admin), scripts/start_ngrok_bridge.sh,
# scripts/status_launch_agent.sh, scripts/stop_ngrok_bridge.sh and the
# maintenance host-admin activate/deactivate scripts.
#
# Instance state lives OUTSIDE task workspaces and outside this repo, under:
#   <state_root>/<instance>/
# where state_root is ${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge
# (BRIDGE_STATE_ROOT may override the whole root; used by tests).
#
# Each instance has one NON-SECRET config file <instance>/instance.conf
# (chmod 600; dirs chmod 700) with exactly these fields:
#   name            = local | hpc | maintenance
#   mode            = local: bridge-workspace | hpc: workspace-write |
#                     maintenance: bridge-workspace
#   approval_policy = on-request | never
#   network_access  = true | false
#   codex_home      = absolute CODEX_HOME path for this instance
#   port            = HTTP port (local 8321, hpc 8322, maintenance 8323)
#   runtime_dir     = absolute dir for pid/log files (under the instance dir)
#   api_key_file    = optional absolute path reference to the existing secret
#                     key file (never copied; empty = repo .bridge_api_key)
#   ngrok_domain_file = optional absolute path reference to a file holding the
#                     ngrok domain (never copied; hpc never falls back to the
#                     local domain)
#
# The config is written ONLY by scripts/bridge_instance.sh (admin). Ordinary
# scripts, tests and migrations are read-only. Secrets are never stored here:
# they remain in existing external files/env (e.g. .bridge_api_key).
#
# The instance is pinned at process startup via BRIDGE_INSTANCE; when unset it
# defaults to "local". There is no task-facing switch and no active-profile
# file. An instance whose config file is absent falls back to legacy singleton
# behavior (with a warning) until scripts/bridge_instance.sh migrate-current
# --apply has been run.
#
# Sourcing has no side effects.

# Prints the instance state root (no trailing slash).
bridge_instance_state_root() {
  printf '%s' "${BRIDGE_STATE_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge}"
}

# Prints the stable runtime data root (launchd-managed copies live here, off
# Desktop to avoid TCC prompts). BRIDGE_DATA_ROOT may override for tests.
bridge_data_root() {
  printf '%s' "${BRIDGE_DATA_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/local-codex-bridge}"
}

# Prints the stable config root holding non-repo copies of local credential
# PATH REFERENCES targets (api_key / ngrok_domain files; dir 700, files 600;
# content is never printed). BRIDGE_CONFIG_ROOT may override for tests.
bridge_config_root() {
  printf '%s' "${BRIDGE_CONFIG_ROOT:-${XDG_CONFIG_HOME:-$HOME/.config}/local-codex-bridge}"
}

# Prints the launchd supervisor agent label (local only).
bridge_supervisor_label() {
  printf 'com.local.codex-bridge.local'
}

# Prints the supervisor enabled-sentinel path for an instance (lives in the
# instance state runtime dir; kept when the repo is not present).
bridge_supervisor_enabled_file() {
  local runtime=""
  runtime="$(bridge_instance_get "$1" runtime_dir 2>/dev/null || true)"
  if [[ -n "$runtime" ]]; then
    printf '%s/supervisor.enabled' "$runtime"
  fi
}

# Prints the supervisor pid path for an instance.
bridge_supervisor_pid_file() {
  local runtime=""
  runtime="$(bridge_instance_get "$1" runtime_dir 2>/dev/null || true)"
  if [[ -n "$runtime" ]]; then
    printf '%s/supervisor.pid' "$runtime"
  fi
}

# Prints the maintenance pause-marker path for the LOCAL instance (lives in
# the local instance state dir, next to instance.conf, NOT in the repo).
# While this file exists the local supervisor keeps its managed children
# stopped (maintenance window hold); the supervisor itself stays alive.
bridge_pause_marker() {
  printf '%s/pause.marker' "$(bridge_instance_dir local)"
}

bridge_instance_valid() {
  case "$1" in
    local|hpc|maintenance) return 0 ;;
    *) return 1 ;;
  esac
}

# Prints the instance directory for a name.
bridge_instance_dir() {
  printf '%s/%s' "$(bridge_instance_state_root)" "$1"
}

# Prints the instance config path.
bridge_instance_config() {
  printf '%s/instance.conf' "$(bridge_instance_dir "$1")"
}

# Returns 0 when the instance config file exists.
bridge_instance_exists() {
  [[ -f "$(bridge_instance_config "$1")" ]]
}

# Prints the pinned instance name: BRIDGE_INSTANCE when set (must be valid),
# else "local". Returns 1 for an explicitly invalid BRIDGE_INSTANCE.
bridge_instance_effective() {
  if [[ -n "${BRIDGE_INSTANCE:-}" ]]; then
    bridge_instance_valid "$BRIDGE_INSTANCE" || return 1
    printf '%s' "$BRIDGE_INSTANCE"
  else
    printf 'local'
  fi
}

# Prints the mode a config value may have for an instance, or returns 1.
# danger-full-access is NEVER valid for any instance.
bridge_instance_mode() {
  case "$1" in
    local) printf 'bridge-workspace' ;;
    hpc) printf 'workspace-write' ;;
    maintenance) printf 'bridge-workspace' ;;
    *) return 1 ;;
  esac
}

# Reads key = "value" from the instance config; prints default when absent.
bridge_instance_get() {
  local name="$1" key="$2" default="${3:-}" f value=""
  f="$(bridge_instance_config "$name")"
  if [[ ! -f "$f" ]]; then
    printf '%s' "$default"
    return 1
  fi
  value="$(sed -nE "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$f" | head -1 || true)"
  printf '%s' "${value:-$default}"
}

bridge_instance_default_codex_home() {
  case "$1" in
    local) printf '%s/.codex-deepseek' "$HOME" ;;
    hpc) printf '%s/.codex-deepseek-hpc' "$HOME" ;;
    maintenance) printf '%s/.codex-deepseek-maintenance' "$HOME" ;;
    *) return 1 ;;
  esac
}

bridge_instance_default_port() {
  case "$1" in
    local) printf '8321' ;;
    hpc) printf '8322' ;;
    maintenance) printf '8323' ;;
    *) return 1 ;;
  esac
}

bridge_instance_default_network() {
  case "$1" in
    local|hpc|maintenance) printf 'true' ;;
    *) return 1 ;;
  esac
}

bridge_instance_default_approval() {
  printf 'on-request'
}

bridge_instance_default_runtime() {
  printf '%s/runtime' "$(bridge_instance_dir "$1")"
}

# Prints the allowed config keys (schema); secrets are never among them.
bridge_instance_keys() {
  printf 'name mode approval_policy network_access codex_home port runtime_dir api_key_file ngrok_domain_file'
}

# Returns 0 (prints "port:<name>" or "runtime:<name>") when another instance
# config collides with NAME on port or runtime_dir; returns 1 when safe.
bridge_instance_collision() {
  local name="$1" port runtime other other_port other_runtime
  port="$(bridge_instance_get "$name" port)"
  runtime="$(bridge_instance_get "$name" runtime_dir)"
  for other in local hpc maintenance; do
    [[ "$other" == "$name" ]] && continue
    bridge_instance_exists "$other" || continue
    other_port="$(bridge_instance_get "$other" port)"
    other_runtime="$(bridge_instance_get "$other" runtime_dir)"
    if [[ -n "$port" && "$port" == "$other_port" ]]; then
      printf 'port:%s' "$other"
      return 0
    fi
    if [[ -n "$runtime" && "$runtime" == "$other_runtime" ]]; then
      printf 'runtime:%s' "$other"
      return 0
    fi
  done
  return 1
}

# Returns 0 only for the local instance: local may keep using the legacy
# repo .ngrok_domain file as a fallback; hpc and maintenance must NEVER
# reuse the local public domain automatically (maintenance only reuses it
# when a HOST-ADMIN activate script writes an explicit ngrok_domain_file
# path reference into the instance config).
bridge_instance_may_use_legacy_domain() {
  [[ "$1" == "local" ]]
}

# ---------------------------------------------------------------------------
# Read-only health helpers (shared by the maintenance activate/deactivate
# host-admin scripts and the activate fail-safe rollback). They append curl
# chatter to LOGF, never print the JSON body, and never touch secrets: the
# URL/domain value stays inside the function and is never echoed.
# ---------------------------------------------------------------------------

# Returns 0 when GET $url/health returns JSON with status == "ok".
bridge_health_ok() {
  local url="$1" logf="$2" body rc
  body="$(curl -fsS -m 15 "$url/health" 2>>"$logf")" || return 1
  rc=0
  python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read())
    sys.exit(0 if d.get("status") == "ok" else 1)
except Exception:
    sys.exit(1)' <<<"$body" || rc=1
  return "$rc"
}

# Returns 0 when /health reports the pinned instance identity (instance,
# mode and port all match). No body/domain content is printed.
bridge_health_identity() {
  local url="$1" expect_instance="$2" expect_mode="$3" expect_port="$4" logf="$5"
  local body
  body="$(curl -fsS -m 15 "$url/health" 2>>"$logf")" || return 1
  python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(1)
ok = d.get("status") == "ok" and d.get("instance") == sys.argv[1] \
     and d.get("mode") == sys.argv[2] and d.get("port") == int(sys.argv[3])
sys.exit(0 if ok else 1)' "$expect_instance" "$expect_mode" "$expect_port" <<<"$body"
}

# Returns 0 when GET $url/ready reports readiness == true (the HTTP server
# answers 200 only when the app-server is alive AND the provider secret
# reference is readable AND the model/provider config is complete). This is
# the supervisor's real readiness gate (a bare HTTP 200 is never enough).
# No body/domain content is printed.
bridge_health_ready() {
  local url="$1" logf="$2" body rc
  body="$(curl -fsS -m 15 "$url/ready" 2>>"$logf")" || return 1
  rc=0
  python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read())
    sys.exit(0 if d.get("ready") is True else 1)
except Exception:
    sys.exit(1)' <<<"$body" || rc=1
  return "$rc"
}

# Prints the recorded public tunnel URL file for an instance (written by
# start_ngrok_bridge.sh; value is never printed by callers).
bridge_public_url_file() {
  printf '%s/public_url' "$(bridge_instance_dir "$1")"
}
