#!/usr/bin/env bash
#
# HOST-ADMIN: remove the stable non-Desktop runtime releases for the local
# instance. Default (no options) removes ALL releases and the `current`
# symlink but PRESERVES:
#   - instance state (${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/)
#   - $HOME/.codex-deepseek (local CODEX_HOME)
#   - the stabilized credential files in
#     ${XDG_CONFIG_HOME:-$HOME/.config}/local-codex-bridge/
#   - the launchd agent (use ./scripts/uninstall_launch_agent.sh --instance
#     local to remove the agent as well)
#
# --keep-releases N keeps the newest N releases instead of removing all.
# Every deletion is guarded by a strict absolute-path/name check (only
# release-* dirs under the releases dir that carry the managed-release
# marker .runtime-build-info / runtime.manifest; `current` is a symlink and
# is only unlinked, never followed).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"

KEEP="${KEEP_RELEASES:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-releases)
      [[ $# -ge 2 ]] || { echo "[uninstall-runtime] error: --keep-releases requires a value" >&2; exit 2; }
      KEEP="$2"
      shift
      ;;
    *) echo "[uninstall-runtime] error: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '[uninstall-runtime] %s\n' "$*"; }
die() { printf '[uninstall-runtime] error: %s\n' "$*" >&2; exit 1; }

[[ "$KEEP" =~ ^[0-9]+$ ]] || die "--keep-releases must be a non-negative integer"

DATA_ROOT="$(bridge_data_root)"
RELEASES_DIR="$DATA_ROOT/releases"
CURRENT="$DATA_ROOT/current"

# Managed-release identity guard: only a release-* dir that was created by
# install_runtime.sh (carries the .runtime-build-info / runtime.manifest
# marker) may be removed. Anything else - foreign dirs, symlinks, arbitrary
# paths - is refused.
runtime_rm_tree() {
  local target="$1" base=""
  base="$(basename "$target")"
  [[ "$target" == "$RELEASES_DIR"/* ]] || return 1
  [[ "$base" =~ ^release-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]] || return 1
  [[ -d "$target" && ! -L "$target" ]] || return 1
  [[ -f "$target/.runtime-build-info" || -f "$target/runtime.manifest" ]] || return 1
  rm -rf "$target"
}

[[ -d "$RELEASES_DIR" ]] || log "no runtime releases dir at $RELEASES_DIR (nothing to uninstall)"

if [[ -L "$CURRENT" ]]; then
  rm -f "$CURRENT"
  log "removed current symlink: $CURRENT"
fi

kept=0
removed=0
while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  rel="$(basename "$rel")"
  target="$RELEASES_DIR/$rel"
  if [[ "$kept" -lt "$KEEP" ]]; then
    kept=$((kept + 1))
    continue
  fi
  if runtime_rm_tree "$target"; then
    removed=$((removed + 1))
  fi
done < <(printf '%s\n' "$RELEASES_DIR"/release-* | sort -r)
log "removed $removed release(s); kept $kept"

log "state, CODEX_HOME and config-root credentials were preserved (default)"
log "launchd agent untouched: use ./scripts/uninstall_launch_agent.sh --instance local to remove it"
