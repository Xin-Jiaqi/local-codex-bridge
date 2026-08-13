#!/usr/bin/env bash
#
# Verify, with real `codex sandbox` runs (no API key, no model calls), that
# the Bridge sandbox modes behave as documented:
#
#   - bridge-workspace (permission profile, injected via `-c` exactly like the
#     bridge does): `git init` + `git add` + `git commit` succeed inside a
#     throwaway repo, proving the app-server can do local commits without
#     user help and without any $CODEX_HOME/config.toml edit;
#   - workspace-write (default): `git add` is denied, reproducing the Codex
#     0.147.0 protected-`.git` limitation;
#   - --network: read-only GitHub connectivity (git ls-remote of the public
#     repo; never pushes).
#
# Run this in a normal Terminal: it needs macOS Seatbelt, so it cannot run
# inside a sandboxed Codex session (sandbox_apply: Operation not permitted).
# Prints RESULT lines; exits nonzero on unexpected results. No secrets.
#
# Usage:
#   ./scripts/verify_bridge_git_automation.sh           # git-only checks
#   ./scripts/verify_bridge_git_automation.sh --network # + read-only GitHub check
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT/config/bridge-workspace.example.toml"
CODEX_BIN="${CODEX_BIN:-$(command -v codex || true)}"
NETWORK=0
[[ "${1:-}" == "--network" ]] && NETWORK=1

die() { printf '[verify] error: %s\n' "$*" >&2; exit 1; }
log() { printf '[verify] %s\n' "$*"; }

[[ -n "$CODEX_BIN" && -x "$CODEX_BIN" ]] || die "codex binary not found (set CODEX_BIN)"
[[ -f "$TEMPLATE" ]] || die "profile template not found: $TEMPLATE"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/bridge-git-verify.XXXXXX")"
HOME_TMP="$(mktemp -d "${TMPDIR:-/tmp}/bridge-codex-home.XXXXXX")"
cleanup() { rm -rf "$WORK" "$HOME_TMP"; }
trap cleanup EXIT

# Hermetic CODEX_HOME: trusted workdir only, no secrets, no profile block —
# the profile comes from the same -c overrides the bridge emits (see
# http_server/server.py). Sandbox command execution does not call the model.
cat > "$HOME_TMP/config.toml" <<EOF
approval_policy = "never"

[projects."$WORK/repo-profile"]
trust_level = "trusted"

[projects."$WORK/repo-workspace"]
trust_level = "trusted"
EOF

GIT_SCRIPT='git init -b main && git config user.email bridge-test@example.invalid && git config user.name "Bridge Test" && echo hi > a.txt && git add a.txt && git commit -m bridge-test >/dev/null'

# The profile flags come from http_server/server.py (single source of truth,
# exactly what the Bridge itself injects). python3 is a project prerequisite.
PROFILE_FLAGS=()
FLAGS_TEXT="$(python3 - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from http_server.server import (
    BRIDGE_PERMISSION_PROFILE,
    BRIDGE_WORKSPACE_EXTENDS_OVERRIDE,
    BRIDGE_WORKSPACE_FILESYSTEM_OVERRIDE,
    BRIDGE_WORKSPACE_NETWORK_OVERRIDE,
)
print('default_permissions="%s"' % BRIDGE_PERMISSION_PROFILE)
print(BRIDGE_WORKSPACE_EXTENDS_OVERRIDE)
print(BRIDGE_WORKSPACE_FILESYSTEM_OVERRIDE)
print(BRIDGE_WORKSPACE_NETWORK_OVERRIDE)
PY
)"
if [[ "$FLAGS_TEXT" == *sandbox_mode* || "$FLAGS_TEXT" == *sandbox_workspace_write* ]]; then
  die "bridge-workspace -c injection must never contain legacy sandbox keys (sandbox_mode / sandbox_workspace_write)"
fi
while IFS= read -r override; do
  PROFILE_FLAGS+=(-c "$override")
done <<< "$FLAGS_TEXT"

# Probe: confirm we can create a sandbox at all (must run in a real Terminal).
# The repo dirs must exist before `codex sandbox -C` chdirs into them.
mkdir -p "$WORK/repo-profile" "$WORK/repo-workspace"
if ! CODEX_HOME="$HOME_TMP" "$CODEX_BIN" sandbox -C "$WORK/repo-profile" true >/dev/null 2>&1; then
  die "codex sandbox is unavailable here (nested sandboxing is not permitted); run this script in a normal Terminal"
fi

# 1) bridge-workspace profile: full local commit must succeed.
if CODEX_HOME="$HOME_TMP" "$CODEX_BIN" sandbox -C "$WORK/repo-profile" "${PROFILE_FLAGS[@]}" \
    bash -c "$GIT_SCRIPT" >/dev/null 2>&1 \
    && git -C "$WORK/repo-profile" rev-parse --verify HEAD >/dev/null 2>&1; then
  log "bridge-workspace: git init/add/commit OK (commit $(git -C "$WORK/repo-profile" rev-parse --short HEAD))"
  printf 'RESULT profile_git_commit=OK\n'
else
  printf 'RESULT profile_git_commit=FAILED\n'
  exit 1
fi

# 2) workspace-write control: git metadata write must be denied (protected .git).
if CODEX_HOME="$HOME_TMP" "$CODEX_BIN" sandbox -C "$WORK/repo-workspace" \
    -c 'sandbox_mode="workspace-write"' \
    bash -c "$GIT_SCRIPT" >/dev/null 2>&1; then
  printf 'RESULT workspace_write_git_denied=UNEXPECTED_ALLOWED\n'
  exit 1
fi
log "workspace-write: git metadata write denied as expected (Codex 0.147.0 protected .git)"
printf 'RESULT workspace_write_git_denied=OK\n'

# 3) optional read-only GitHub connectivity under the profile.
if [[ "$NETWORK" -eq 1 ]]; then
  if CODEX_HOME="$HOME_TMP" "$CODEX_BIN" sandbox -C "$WORK/repo-profile" "${PROFILE_FLAGS[@]}" \
      git ls-remote https://github.com/Xin-Jiaqi/local-codex-bridge.git HEAD >/dev/null 2>&1; then
    log "bridge-workspace: read-only GitHub connectivity OK (git ls-remote)"
    printf 'RESULT network_readonly=OK\n'
  else
    printf 'RESULT network_readonly=FAILED\n'
    log "bridge-workspace: read-only GitHub connectivity FAILED (network proxy/SSH may need attention; try git/gh over HTTPS vs SSH)"
  fi
fi

log "all checks passed (exit 0)"
