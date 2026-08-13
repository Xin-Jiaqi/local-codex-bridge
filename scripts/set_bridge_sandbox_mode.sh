#!/usr/bin/env bash
#
# Set / read / clear the persistent Bridge sandbox mode.
#
# The mode is stored in the project-local, gitignored file
# .bridge_sandbox_mode (no secrets). scripts/start_ngrok_bridge.sh reads it
# when BRIDGE_SANDBOX_MODE env is not set; it takes effect on the next Bridge
# start. Switch modes in a normal Terminal (stop/start or re-run the
# LaunchAgent); see SECURITY.md for why not from inside the Bridge sandbox.
#
# This script is the ONLY sanctioned writer of .bridge_sandbox_mode. The
# shared lib (scripts/bridge_mode_lib.sh) is read-only; other scripts, tests
# and migrations must not write the mode file directly (tests use
# BRIDGE_SANDBOX_MODE_FILE pointing at a temp path).
#
# Usage:
#   ./scripts/set_bridge_sandbox_mode.sh                # show effective mode
#   ./scripts/set_bridge_sandbox_mode.sh <mode>         # workspace-write | bridge-workspace | danger-full-access
#   ./scripts/set_bridge_sandbox_mode.sh --unset        # remove the file (back to default)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_mode_lib.sh"

case "${1:-}" in
  ""|--show)
    printf 'effective sandbox mode: %s (source: %s; file: %s)\n' \
      "$(bridge_mode_effective "$ROOT")" \
      "$(bridge_mode_source "$ROOT")" \
      "$(bridge_mode_file "$ROOT")"
    ;;
  --unset)
    f="$(bridge_mode_file "$ROOT")"
    rm -f "$f"
    echo "[set] removed $f; next Bridge start will use workspace-write"
    ;;
  *)
    mode="$1"
    if ! bridge_mode_valid "$mode"; then
      printf 'error: invalid sandbox mode %q (use workspace-write|bridge-workspace|danger-full-access)\n' "$mode" >&2
      exit 1
    fi
    f="$(bridge_mode_file "$ROOT")"
    mkdir -p "$(dirname "$f")"
    printf '%s\n' "$mode" > "$f"
    chmod 600 "$f"
    echo "[set] wrote $f = $mode"
    echo "[set] takes effect on the next Bridge start:"
    echo "      ./scripts/stop_ngrok_bridge.sh && ./scripts/start_ngrok_bridge.sh"
    ;;
esac
