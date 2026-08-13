#!/usr/bin/env bash
#
# Read-only shared helpers for the Bridge sandbox-mode file
# (.bridge_sandbox_mode). Sourced by scripts/set_bridge_sandbox_mode.sh,
# scripts/start_ngrok_bridge.sh and scripts/status_launch_agent.sh. The file
# is written ONLY by scripts/set_bridge_sandbox_mode.sh, so ordinary scripts,
# tests and migrations cannot silently overwrite a persisted mode.
# Sourcing has no side effects; no secrets involved.
#
# Effective-mode precedence:
#   1. BRIDGE_SANDBOX_MODE env (non-empty)
#   2. <root>/.bridge_sandbox_mode file (BRIDGE_SANDBOX_MODE_FILE may override
#      the path; used by tests)
#   3. workspace-write (default)
#
# Valid values: workspace-write | bridge-workspace | danger-full-access

# Prints the path of the persistent mode file for a project root.
bridge_mode_file() {
  local root="$1"
  printf '%s' "${BRIDGE_SANDBOX_MODE_FILE:-$root/.bridge_sandbox_mode}"
}

bridge_mode_valid() {
  case "$1" in
    workspace-write|bridge-workspace|danger-full-access) return 0 ;;
    *) return 1 ;;
  esac
}

# Prints the effective mode (env > file > default).
bridge_mode_effective() {
  local root="$1" mode="" f
  mode="${BRIDGE_SANDBOX_MODE:-}"
  if [[ -z "$mode" ]]; then
    f="$(bridge_mode_file "$root")"
    if [[ -f "$f" ]]; then
      mode="$(tr -d '[:space:]' < "$f" || true)"
    fi
  fi
  printf '%s' "${mode:-workspace-write}"
}

# Prints where the effective mode came from: env | file | default.
bridge_mode_source() {
  local root="$1"
  if [[ -n "${BRIDGE_SANDBOX_MODE:-}" ]]; then
    printf 'env'
  elif [[ -f "$(bridge_mode_file "$root")" ]]; then
    printf 'file'
  else
    printf 'default'
  fi
}
