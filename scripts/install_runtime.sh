#!/usr/bin/env bash
#
# HOST-ADMIN: install the stable non-Desktop runtime for the LOCAL instance
# (launchd auto-recovery target).
#
# Design:
#   - runtime data root: ${XDG_DATA_HOME:-$HOME/.local/share}/
#     local-codex-bridge/ (off Desktop, avoids TCC; BRIDGE_DATA_ROOT overrides
#     for tests);
#   - each install stages into releases/ and atomically flips the `current`
#     symlink (a release is never modified in place);
#   - the runtime copy contains ONLY runtime-required TRACKED allowlisted
#     files (python packages + start/stop/supervisor/control scripts +
#     config/bridge-workspace.example.toml, the workspace profile the
#     supervisor/start flow depends on); no .git, tests, docs, schemas,
#     backups, logs, secrets or domain files;
#   - writes a secret-free manifest (release/head/dirty/time/allowlist count);
#   - keeps the most recent N releases (default 2); pruning is guarded by a
#     strict absolute-path/name check (never touches `current` or anything
#     outside releases/);
#   - when the local instance key/domain PATH REFERENCES still point into the
#     repo/Desktop, copies those files into
#     ${XDG_CONFIG_HOME:-$HOME/.config}/local-codex-bridge/ (dir 700, files
#     600) and safely updates the instance config path refs; file CONTENT is
#     never printed and the repo originals are never deleted;
#   - only the explicit local instance is auto-hosted; hpc / maintenance /
#     Para / Japan and remote jobs are never touched.
#
# This script must run from the git checkout (repo root). Run it BEFORE
# ./scripts/install_launch_agent.sh --instance local.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"

INSTANCE=""
KEEP="${KEEP_RELEASES:-2}"
DEST_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance)
      [[ $# -ge 2 ]] || { echo "[install-runtime] error: --instance requires a value" >&2; exit 2; }
      INSTANCE="$2"
      shift
      ;;
    --keep-releases)
      [[ $# -ge 2 ]] || { echo "[install-runtime] error: --keep-releases requires a value" >&2; exit 2; }
      KEEP="$2"
      shift
      ;;
    --dest)
      [[ $# -ge 2 ]] || { echo "[install-runtime] error: --dest requires a value" >&2; exit 2; }
      DEST_ARG="$2"
      shift
      ;;
    *) echo "[install-runtime] error: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '[install-runtime] %s\n' "$*"; }
die() { printf '[install-runtime] error: %s\n' "$*" >&2; exit 1; }

[[ "$INSTANCE" == "local" ]] || die \
  "install_runtime.sh only supports the explicit local instance (got '${INSTANCE:-<none>}'); hpc/maintenance are on-demand and never auto-hosted"
[[ "$KEEP" =~ ^[0-9]+$ && "$KEEP" -ge 1 ]] || die "--keep-releases must be a positive integer"
bridge_instance_exists local || die \
  "local instance has no config ($(bridge_instance_config local)); run ./scripts/bridge_instance.sh migrate-current --apply first"
[[ -d "$ROOT/.git" ]] || die "runtime install must run from the git checkout (repo root), not from a runtime copy"
command -v git >/dev/null 2>&1 || die "git not found (required for the manifest and the tracked allowlist)"

DATA_ROOT="$(bridge_data_root)"
if [[ -n "$DEST_ARG" ]]; then
  [[ "$DEST_ARG" == /* ]] || die "--dest must be an absolute path (test/override hook for temp-dir installs)"
  DATA_ROOT="$DEST_ARG"
fi
RELEASES_DIR="$DATA_ROOT/releases"
CURRENT="$DATA_ROOT/current"
mkdir -p "$DATA_ROOT" "$RELEASES_DIR"
chmod 700 "$DATA_ROOT" "$RELEASES_DIR"

# Runtime-required allowlist (top-level entries; every file must be tracked).
# config/ carries config/bridge-workspace.example.toml - the bridge-workspace
# permission profile example the stable runtime start flow depends on.
# Deliberately EXCLUDES .git, tests, docs, schemas, openapi, backups, logs,
# .bridge_* secrets/domain files and the legacy .runtime dir.
ALLOWLIST=(
  bridge
  http_server
  config
  scripts/bridge_instance_lib.sh
  scripts/bridge_mode_lib.sh
  scripts/bridge_secret_lib.sh
  scripts/pid_guard_lib.sh
  scripts/start_ngrok_bridge.sh
  scripts/stop_ngrok_bridge.sh
  scripts/run_local_supervisor.sh
  scripts/supervisor_control.sh
  scripts/migrate_codex_home_permissions.py
  scripts/install_runtime.sh
  scripts/uninstall_runtime.sh
  scripts/status_launch_agent.sh
  scripts/bridge_instance.sh
)

# Guarded recursive removal: ONLY paths under $RELEASES_DIR matching the
# release pattern (or the internal .stage.* dirs) may be removed.
runtime_rm_tree() {
  local target="$1" base=""
  base="$(basename "$target")"
  [[ "$target" == "$RELEASES_DIR"/* ]] || return 1
  if [[ "$base" == .stage.* ]]; then
    [[ -d "$target" && ! -L "$target" ]] || return 1
  elif [[ "$base" =~ ^release-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]]; then
    [[ -d "$target" && ! -L "$target" ]] || return 1
    [[ -f "$target/.runtime-build-info" || -f "$target/runtime.manifest" ]] || return 1
  else
    return 1
  fi
  rm -rf "$target"
}

HEAD="$(git -C "$ROOT" rev-parse HEAD)"
DIRTY="no"
if [[ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null || true)" ]]; then
  DIRTY="yes"
fi

log "installing runtime for instance=local from $ROOT (head=${HEAD:0:12}, dirty=$DIRTY)"

STAGE="$(mktemp -d "$RELEASES_DIR/.stage.XXXXXX")"
trap 'runtime_rm_tree "$STAGE" || true' EXIT

tracked_count=0
for entry in "${ALLOWLIST[@]}"; do
  files="$(git -C "$ROOT" ls-files -- "$entry")"
  if [[ -z "$files" ]]; then
    if [[ -f "$ROOT/$entry" ]]; then
      files="$entry"
    elif [[ -d "$ROOT/$entry" ]]; then
      # pre-commit working tree: allowlisted dir is present but untracked;
      # copy its files, still excluding caches/backups/logs
      files="$(cd "$ROOT" && find "$entry" -type f \
        ! -name '*.pyc' ! -name '*.bak' ! -name '*.bak-*' ! -path '*/__pycache__/*' \
        ! -path '*/.runtime/*' 2>/dev/null || true)"
    fi
    [[ -n "$files" ]] || die "allowlisted path is not tracked or does not exist: $entry"
  fi
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    [[ -f "$ROOT/$f" ]] || die "allowlisted file missing from checkout: $f"
    mkdir -p "$STAGE/$(dirname "$f")"
    cp -p "$ROOT/$f" "$STAGE/$f"
    tracked_count=$((tracked_count + 1))
  done <<< "$files"
done

find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
find "$STAGE" -type f \( -name '*.sh' -o -name '*.py' \) -exec chmod 755 {} +

RELEASE="release-$(date -u +%Y%m%dT%H%M%SZ)-${HEAD:0:12}"
VERSION="$(python3 -c 'import re,sys
m=re.search(r"__version__\s*=\s*\"([^\"]+)\"", open(sys.argv[1], encoding="utf-8").read())
print(m.group(1) if m else "unknown")' "$ROOT/bridge/__init__.py" 2>/dev/null || true)"
{
  printf 'release=%s\n' "$RELEASE"
  printf 'head=%s\n' "$HEAD"
  printf 'dirty=%s\n' "$DIRTY"
  printf 'time=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'version=%s\n' "$VERSION"
  printf 'allowlist_files=%s\n' "$tracked_count"
} > "$STAGE/.runtime-build-info"
chmod 644 "$STAGE/.runtime-build-info"
# Compatibility manifest (superseded by .runtime-build-info but kept for
# older status/check scripts that read runtime.manifest).
{
  printf 'release=%s\n' "$RELEASE"
  printf 'head=%s\n' "$HEAD"
  printf 'dirty=%s\n' "$DIRTY"
  printf 'time=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'allowlist_files=%s\n' "$tracked_count"
} > "$STAGE/runtime.manifest"
chmod 644 "$STAGE/runtime.manifest"

RELEASE_DIR="$RELEASES_DIR/$RELEASE"
if [[ -e "$RELEASE_DIR" ]]; then
  die "release directory already exists: $RELEASE_DIR (refusing to overwrite; remove it with uninstall_runtime.sh --keep-releases if intentional)"
fi
mv "$STAGE" "$RELEASE_DIR"
trap - EXIT

# Atomic `current` symlink flip. BSD mv would treat the destination symlink
# as the directory it resolves to and move the temp link inside it, so use
# os.replace (rename(2)) which atomically replaces the existing symlink.
ln -s "$RELEASE_DIR" "$RELEASES_DIR/.current.tmp.$$"
python3 -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' \
  "$RELEASES_DIR/.current.tmp.$$" "$CURRENT"
chmod -h 755 "$CURRENT" 2>/dev/null || true
log "current -> $RELEASE_DIR"

# Keep the newest $KEEP releases; prune older ones with the strict guard.
pruned=0
kept=0
while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  rel="$(basename "$rel")"
  [[ "$rel" =~ ^release-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]] || continue
  target="$RELEASES_DIR/$rel"
  if [[ -L "$CURRENT" && "$(readlink "$CURRENT")" == "$target" ]]; then
    kept=$((kept + 1))
    continue
  fi
  if [[ "$kept" -lt "$KEEP" ]]; then
    kept=$((kept + 1))
    continue
  fi
  if runtime_rm_tree "$target"; then
    pruned=$((pruned + 1))
  fi
done < <(printf '%s\n' "$RELEASES_DIR"/release-* | sort -r)
log "releases kept: $KEEP (pruned $pruned old release(s))"

# ---------------------------------------------------------------------------
# Stable credential PATH REFERENCES: when the local instance still points at
# the repo/Desktop key/domain files, copy them into the config root
# (dir 700 / files 600) and update the instance config refs. Content is
# never printed; the repo originals are never deleted.
# ---------------------------------------------------------------------------

CONFIG_ROOT="$(bridge_config_root)"
need_update=0

migrate_credential() {
  local kind="$1" repo_file="$2" current_ref="$3" dst=""
  dst="$CONFIG_ROOT/$kind"
  if [[ -n "$current_ref" ]]; then
    case "$current_ref" in
      "$ROOT"/*|"$HOME"/Desktop/*)
        ;;
      *)
        log "credential ref $kind already outside repo/Desktop: $current_ref (left as-is)"
        return 0
        ;;
    esac
  fi
  [[ -f "$repo_file" ]] || die "$repo_file missing (required to stabilize the $kind path reference)"
  mkdir -p "$CONFIG_ROOT"
  chmod 700 "$CONFIG_ROOT"
  cp -p "$repo_file" "$dst"
  chmod 600 "$dst"
  log "stabilized $kind path reference: $dst (content never printed)"
  need_update=1
}

key_ref="$(bridge_instance_get local api_key_file)"
domain_ref="$(bridge_instance_get local ngrok_domain_file)"
if [[ -z "$key_ref" ]]; then
  key_ref="$ROOT/.bridge_api_key"
  migrate_credential api_key "$ROOT/.bridge_api_key" ""
else
  migrate_credential api_key "$ROOT/.bridge_api_key" "$key_ref"
fi
if [[ -z "$domain_ref" ]]; then
  domain_ref="$ROOT/.ngrok_domain"
  migrate_credential ngrok_domain "$ROOT/.ngrok_domain" ""
else
  migrate_credential ngrok_domain "$ROOT/.ngrok_domain" "$domain_ref"
fi

if [[ "$need_update" == 1 ]]; then
  bash "$ROOT/scripts/bridge_instance.sh" update local \
    "api_key_file=$CONFIG_ROOT/api_key" \
    "ngrok_domain_file=$CONFIG_ROOT/ngrok_domain" >/dev/null 2>&1 || die \
    "failed to update local instance credential path refs (run ./scripts/bridge_instance.sh show local to inspect; no secrets are printed)"
  log "local instance credential path refs updated to config root (repo originals untouched)"
fi

log "runtime install complete: $DATA_ROOT (release $RELEASE, $tracked_count allowlisted files)"
log "next: ./scripts/install_launch_agent.sh --instance local"
