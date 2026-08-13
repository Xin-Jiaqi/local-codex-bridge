#!/usr/bin/env bash
#
# Start the Local Codex Bridge HTTP API (127.0.0.1:8321) and expose it via a
# fixed ngrok tunnel (https://<your-domain>.ngrok-free.dev).
#
# The tunnel domain is NOT hardcoded here. Provide it one of these ways
# (both stay out of git):
#   export NGROK_DOMAIN=my-name.ngrok-free.dev
#   echo 'my-name.ngrok-free.dev' > .ngrok_domain   (project root, gitignored)
#
# Usage:
#   ./scripts/start_ngrok_bridge.sh        # start both in the background
#   ./scripts/stop_ngrok_bridge.sh         # stop only what this script started
#
# Sandbox mode for the spawned app-server (see http_server/server.py):
#   Effective mode: BRIDGE_SANDBOX_MODE env > .bridge_sandbox_mode file >
#   workspace-write default (see scripts/bridge_mode_lib.sh). Use
#   ./scripts/set_bridge_sandbox_mode.sh to persist a mode.
#   BRIDGE_NETWORK_ACCESS=true               (enable network in workspace-write mode)
#
# Pinned instance (local | hpc | maintenance), see scripts/bridge_instance_lib.sh:
#   Selected ONLY by BRIDGE_INSTANCE at process startup (default local); no
#   task-facing switch exists. Instance state lives OUTSIDE this repo under
#   ${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/ and
#   is written only by scripts/bridge_instance.sh (admin). local uses the
#   bridge-workspace profile (git+GitHub coding/release); hpc uses
#   workspace-write + on-request + network (never danger-full-access).
#   If the pinned instance has no config yet, this script falls back to the
#   legacy singleton behavior with a warning until
#   ./scripts/bridge_instance.sh migrate-current --apply has been run.
#
# bridge-workspace is a pure permission profile: the dedicated CODEX_HOME
# config must NOT contain legacy `sandbox_mode` / `[sandbox_workspace_write]`
# keys (they make Codex fall back to the legacy sandbox and ignore
# default_permissions). This script refuses to start in that mode until
# scripts/migrate_codex_home_permissions.py has cleaned the config.
#
# Idempotent: if the recorded PIDs in the effective runtime dir (legacy
# $ROOT/.runtime or the pinned instance runtime) are still alive, they are
# reused instead of starting duplicates. PID files only ever contain PIDs of
# processes started by this script. If the instance port or the ngrok domain
# is occupied by an unmanaged process, the script reports a clear error and
# exits WITHOUT touching it.
#
# Secrets: .bridge_api_key is read into BRIDGE_API_KEY at runtime (env var
# only); it is never written to the script, logs, or pid files.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
. "$ROOT/scripts/bridge_mode_lib.sh"
. "$ROOT/scripts/bridge_instance_lib.sh"
. "$ROOT/scripts/pid_guard_lib.sh"

RUNTIME_DIR="$ROOT/.runtime"

# Derives EVERY runtime file path from the effective RUNTIME_DIR (legacy
# $ROOT/.runtime, or the pinned instance runtime dir once resolve_instance has
# run). Called at startup for the legacy default and again by
# derive_instance_env for instance starts, so explicit local/hpc/maintenance instances
# never share pid files, logs or start.lock with each other or with the repo
# .runtime dir. public_url state lives with the instance dir when an instance
# is active, else in the repo root (legacy).
derive_runtime_paths() {
  BRIDGE_PID_FILE="$RUNTIME_DIR/bridge.pid"
  NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"
  BRIDGE_LOG="$RUNTIME_DIR/bridge.log"
  BRIDGE_OUT_LOG="$RUNTIME_DIR/bridge.out.log"
  NGROK_LOG="$RUNTIME_DIR/ngrok.log"
  if [[ -n "${INSTANCE_DIR:-}" ]]; then
    PUBLIC_URL_FILE="$INSTANCE_DIR/public_url"
  else
    PUBLIC_URL_FILE="$ROOT/.public_url"
  fi
}
derive_runtime_paths

BRIDGE_HOST="127.0.0.1"
BRIDGE_PORT="8321"
BRIDGE_URL="http://$BRIDGE_HOST:$BRIDGE_PORT"

KEY_FILE="$ROOT/.bridge_api_key"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex-deepseek}"

log() { printf '[start] %s\n' "$*"; }
die() { printf '[start] error: %s\n' "$*" >&2; exit 1; }

# Resolve a binary by PATH first, then fall back to the usual user-local and
# Homebrew locations. This keeps the script working under launchd, which runs
# jobs with a very short PATH (e.g. /usr/bin:/bin:/usr/sbin:/sbin).
locate_bin() {
  local name="$1" found="" d
  found="$(command -v "$name" 2>/dev/null || true)"
  if [[ -z "$found" ]]; then
    for d in "$HOME/.local/bin" "/opt/homebrew/bin" "/usr/local/bin"; do
      if [[ -x "$d/$name" ]]; then
        found="$d/$name"
        break
      fi
    done
  fi
  printf '%s' "$found"
}

CODEX_BIN="${CODEX_BIN:-$(locate_bin codex)}"
NGROK_BIN="${NGROK_BIN:-$(locate_bin ngrok)}"

START_LOCK_DIR=""

# Single-instance guard: only one start_ngrok_bridge.sh may run at a time.
# Uses an atomic mkdir(2) lock (macOS has no flock(1)); a stale lock whose
# owner pid is gone is removed and retried.
acquire_start_lock() {
  local lock_dir="$RUNTIME_DIR/start.lock" owner i
  for i in 1 2 3; do
    if mkdir "$lock_dir" 2>/dev/null; then
      echo "$$" > "$lock_dir/pid"
      START_LOCK_DIR="$lock_dir"
      return 0
    fi
    owner="$(cat "$lock_dir/pid" 2>/dev/null || true)"
    if [[ -n "$owner" ]] && is_valid_pid "$owner" && pid_alive "$owner"; then
      die "another start_ngrok_bridge.sh is already running (pid $owner); not starting duplicates"
    fi
    rm -rf "$lock_dir" 2>/dev/null || true
    sleep 1
  done
  die "could not acquire start lock at $lock_dir"
}

release_start_lock() {
  if [[ -n "$START_LOCK_DIR" ]]; then
    rm -rf "$START_LOCK_DIR"
    START_LOCK_DIR=""
  fi
}

INSTANCE=""
INSTANCE_DIR=""

# Resolve the pinned instance (BRIDGE_INSTANCE env, default local). Absent
# instance config -> legacy singleton fallback with a warning.
resolve_instance() {
  local requested
  requested="$(bridge_instance_effective)" || die "invalid BRIDGE_INSTANCE=$BRIDGE_INSTANCE (use local|hpc|maintenance)"
  if bridge_instance_exists "$requested"; then
    INSTANCE="$requested"
    INSTANCE_DIR="$(bridge_instance_dir "$INSTANCE")"
    derive_instance_env
    if coll="$(bridge_instance_collision "$INSTANCE")"; then
      die "instance collision: instance '${coll#*:}' is configured with the same ${coll%%:*} as '$INSTANCE' (fail closed; concurrent instances need distinct ports and runtime dirs)"
    fi
    export BRIDGE_INSTANCE
    log "instance: $INSTANCE (config: $(bridge_instance_config "$INSTANCE"))"
  else
    INSTANCE=""
    INSTANCE_DIR=""
    unset BRIDGE_INSTANCE BRIDGE_APPROVAL_POLICY
    log "warning: no instance config for '$requested' ($(bridge_instance_config "$requested")); falling back to legacy singleton behavior (run ./scripts/bridge_instance.sh migrate-current --apply)"
  fi
}

# Derives every runtime value from the pinned instance config (non-secret).
derive_instance_env() {
  local mode="" allowed="" policy="" network="" ch="" port="" runtime=""
  mode="$(bridge_instance_get "$INSTANCE" mode)"
  allowed="$(bridge_instance_mode "$INSTANCE")"
  [[ "$mode" == "$allowed" ]] || die "instance '$INSTANCE' has invalid mode '$mode' (must be '$allowed'; danger-full-access is never an instance mode)"
  policy="$(bridge_instance_get "$INSTANCE" approval_policy)"
  case "$policy" in on-request|never) ;; *) die "instance '$INSTANCE' has invalid approval_policy '$policy'" ;; esac
  network="$(bridge_instance_get "$INSTANCE" network_access)"
  case "$network" in true|false) ;; *) die "instance '$INSTANCE' has invalid network_access '$network'" ;; esac
  ch="$(bridge_instance_get "$INSTANCE" codex_home)"
  [[ "$ch" == /* ]] || die "instance '$INSTANCE' has invalid codex_home '$ch'"
  port="$(bridge_instance_get "$INSTANCE" port)"
  [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1 && "$port" -le 65535 ]] || die "instance '$INSTANCE' has invalid port '$port'"
  runtime="$(bridge_instance_get "$INSTANCE" runtime_dir)"
  [[ "$runtime" == /* ]] || die "instance '$INSTANCE' has invalid runtime_dir '$runtime'"
  if [[ -n "$(bridge_instance_get "$INSTANCE" api_key_file)" ]]; then
    KEY_FILE="$(bridge_instance_get "$INSTANCE" api_key_file)"
    [[ -f "$KEY_FILE" ]] || die "instance '$INSTANCE' api_key_file not found: $KEY_FILE"
  fi
  SANDBOX_MODE="$mode"
  BRIDGE_NETWORK_ACCESS="$network"
  BRIDGE_APPROVAL_POLICY="$policy"
  CODEX_HOME="$ch"
  BRIDGE_PORT="$port"
  RUNTIME_DIR="$runtime"
  BRIDGE_URL="http://$BRIDGE_HOST:$BRIDGE_PORT"
  derive_runtime_paths
  BRIDGE_SANDBOX_MODE="$SANDBOX_MODE"
  export BRIDGE_SANDBOX_MODE BRIDGE_NETWORK_ACCESS BRIDGE_APPROVAL_POLICY CODEX_HOME
}

# Resolve the public ngrok domain: NGROK_DOMAIN env > instance
# ngrok_domain_file > legacy repo .ngrok_domain (local/legacy only; hpc and
# maintenance never
# reuse the local domain). A missing hpc/maintenance domain fails loudly instead of
# exposing the hpc instance through the local endpoint.
resolve_public_url() {
  if [[ -z "${NGROK_DOMAIN:-}" && -n "$INSTANCE" ]]; then
    local df=""
    df="$(bridge_instance_get "$INSTANCE" ngrok_domain_file)"
    if [[ -n "$df" ]]; then
      [[ -f "$df" ]] || die "instance '$INSTANCE' ngrok_domain_file not found: $df"
      NGROK_DOMAIN="$(tr -d '[:space:]' < "$df")"
    elif bridge_instance_may_use_legacy_domain "$INSTANCE" && [[ -f "$ROOT/.ngrok_domain" ]]; then
      NGROK_DOMAIN="$(tr -d '[:space:]' < "$ROOT/.ngrok_domain")"
    fi
  elif [[ -z "${NGROK_DOMAIN:-}" && -f "$ROOT/.ngrok_domain" ]]; then
    NGROK_DOMAIN="$(tr -d '[:space:]' < "$ROOT/.ngrok_domain")"
  fi
  if [[ -z "${NGROK_DOMAIN:-}" ]]; then
    if [[ -n "$INSTANCE" ]]; then
      die "no public tunnel domain for instance '$INSTANCE': set NGROK_DOMAIN or ngrok_domain_file in the instance config. hpc never reuses the local domain; local-only mode is unaffected"
    fi
    die "NGROK_DOMAIN is required: export NGROK_DOMAIN=<your-domain> or write it to $ROOT/.ngrok_domain (gitignored)"
  fi
  PUBLIC_URL="https://$NGROK_DOMAIN"
}

STARTED_BRIDGE=""
STARTED_NGROK=""

# Stop only processes created by THIS run; pre-existing processes are never
# touched. Called before every failure exit after startup has begun.
cleanup_started() {
  local pid
  if [[ -n "$STARTED_BRIDGE" ]]; then
    pid="$STARTED_BRIDGE"
    if pid_alive "$pid" && managed_bridge_pid "$pid" "$ROOT" "$RUNTIME_DIR"; then
      kill "$pid" 2>/dev/null || true
    elif pid_alive "$pid"; then
      report_unmanaged "bridge" "$pid" "$(proc_command "$pid")"
    fi
    rm -f "$BRIDGE_PID_FILE"
    STARTED_BRIDGE=""
  fi
  if [[ -n "$STARTED_NGROK" ]]; then
    pid="$STARTED_NGROK"
    if pid_alive "$pid" && managed_ngrok_pid "$pid" "$BRIDGE_PORT"; then
      kill "$pid" 2>/dev/null || true
    elif pid_alive "$pid"; then
      report_unmanaged "ngrok" "$pid" "$(proc_command "$pid")"
    fi
    rm -f "$NGROK_PID_FILE"
    STARTED_NGROK=""
  fi
}

fail() {
  cleanup_started
  die "$@"
}

# Returns 0 when something is listening on 127.0.0.1:$1.
port_busy() {
  if command -v nc >/dev/null 2>&1; then
    nc -z -G 2 "$BRIDGE_HOST" "$1" >/dev/null 2>&1
  else
    curl -sS -m 2 "$BRIDGE_URL/health" >/dev/null 2>&1
  fi
}

# Returns 0 when a running ngrok instance already serves our fixed domain.
ngrok_serving_domain() {
  curl -fsS -m 3 "http://127.0.0.1:4040/api/tunnels" 2>/dev/null | grep -q "$NGROK_DOMAIN"
}

check_prereqs() {
  [[ -f "$KEY_FILE" ]] || die "$KEY_FILE not found (create it, chmod 600, and keep it out of git)"
  [[ -x "$CODEX_BIN" ]] || die "codex binary not found (set CODEX_BIN or add codex to PATH)"
  command -v python3 >/dev/null 2>&1 || die "python3 not found"
  command -v "$NGROK_BIN" >/dev/null 2>&1 || die "ngrok not found: $NGROK_BIN"
  mkdir -p "$RUNTIME_DIR"
}

validate_sandbox_env() {
  local source_note=""
  if [[ -z "$INSTANCE" ]]; then
    SANDBOX_MODE="$(bridge_mode_effective "$ROOT")"
    source_note="(source: $(bridge_mode_source "$ROOT"))"
  fi
  case "$SANDBOX_MODE" in
    workspace-write|bridge-workspace|danger-full-access) ;;
    *) die "BRIDGE_SANDBOX_MODE=$SANDBOX_MODE is invalid (use workspace-write|bridge-workspace|danger-full-access)" ;;
  esac
  case "${BRIDGE_NETWORK_ACCESS:-false}" in
    true|1|yes|on|false|0|no|off|"") ;;
    *) die "BRIDGE_NETWORK_ACCESS=${BRIDGE_NETWORK_ACCESS} is invalid (use true|false)" ;;
  esac
  BRIDGE_SANDBOX_MODE="$SANDBOX_MODE"
  export BRIDGE_SANDBOX_MODE BRIDGE_NETWORK_ACCESS
  if [[ "$SANDBOX_MODE" == "bridge-workspace" ]]; then
    if ! python3 "$ROOT/scripts/migrate_codex_home_permissions.py" \
        --config "$CODEX_HOME/config.toml" --config-dir "$CODEX_HOME/config" \
        --project-root "$ROOT" --verify >/dev/null 2>&1; then
      die "bridge-workspace requires clean configs: $CODEX_HOME/config.toml, $CODEX_HOME/config/*.toml or $ROOT/.codex/config.toml still has legacy sandbox_mode / [sandbox_workspace_write] keys (they disable permission profiles). Run: ./scripts/migrate_codex_home_permissions.py --dry-run && ./scripts/migrate_codex_home_permissions.py --apply (config dir / project config must be cleaned manually)"
    fi
  fi
  if [[ -n "$INSTANCE" ]]; then
    log "instance: $INSTANCE; sandbox mode: $SANDBOX_MODE; approval_policy: $BRIDGE_APPROVAL_POLICY; network_access: $BRIDGE_NETWORK_ACCESS"
  else
    log "instance: <legacy singleton>; sandbox mode: $SANDBOX_MODE $source_note${BRIDGE_NETWORK_ACCESS:+; BRIDGE_NETWORK_ACCESS=$BRIDGE_NETWORK_ACCESS}"
  fi
}

ensure_bridge() {
  local pid=""
  if [[ -f "$BRIDGE_PID_FILE" ]]; then
    pid="$(cat "$BRIDGE_PID_FILE" 2>/dev/null || true)"
  fi
  if managed_bridge_pid "$pid" "$ROOT" "$RUNTIME_DIR"; then
    log "bridge already running (pid $(cat "$BRIDGE_PID_FILE")); reusing it (sandbox env changes need a stop/start)"
    return 0
  fi
  if is_valid_pid "$pid" && pid_alive "$pid"; then
    report_unmanaged "bridge" "$pid" "$(proc_command "$pid")"
    log "bridge pid file points at an unmanaged process; ignoring it (not killed)"
  fi
  rm -f "$BRIDGE_PID_FILE"
  if port_busy "$BRIDGE_PORT"; then
    fail "port $BRIDGE_PORT is in use by an unmanaged process; not touching it (stop it manually if needed)"
  fi

  log "starting bridge: python3 -m http_server --host $BRIDGE_HOST --port $BRIDGE_PORT"
  export BRIDGE_API_KEY="$(cat "$KEY_FILE")"
  export CODEX_BIN
  export CODEX_HOME
  nohup python3 -m http_server --host "$BRIDGE_HOST" --port "$BRIDGE_PORT" --log "$BRIDGE_LOG" \
    > "$BRIDGE_OUT_LOG" 2>&1 &
  STARTED_BRIDGE=$!
  echo "$STARTED_BRIDGE" > "$BRIDGE_PID_FILE"
}

wait_for_local_health() {
  local i
  for i in $(seq 1 60); do
    if curl -fsS -m 5 "$BRIDGE_URL/health" >/dev/null 2>&1; then
      log "local health OK: $BRIDGE_URL/health"
      return 0
    fi
    if [[ -n "$STARTED_BRIDGE" ]] && ! pid_alive "$STARTED_BRIDGE"; then
      log "bridge process exited; tail of $BRIDGE_OUT_LOG:"
      tail -n 5 "$BRIDGE_OUT_LOG" >&2 2>/dev/null || true
      break
    fi
    sleep 1
  done
  return 1
}

ensure_ngrok() {
  local pid=""
  if [[ -f "$NGROK_PID_FILE" ]]; then
    pid="$(cat "$NGROK_PID_FILE" 2>/dev/null || true)"
  fi
  if managed_ngrok_pid "$pid" "$BRIDGE_PORT"; then
    log "ngrok already running (pid $(cat "$NGROK_PID_FILE")); reusing it"
    return 0
  fi
  if is_valid_pid "$pid" && pid_alive "$pid"; then
    report_unmanaged "ngrok" "$pid" "$(proc_command "$pid")"
    log "ngrok pid file points at an unmanaged process; ignoring it (not killed)"
  fi
  rm -f "$NGROK_PID_FILE"
  if ngrok_serving_domain; then
    fail "an unmanaged ngrok instance already serves $PUBLIC_URL; not touching it"
  fi

  log "starting ngrok: $NGROK_BIN http $BRIDGE_PORT --url $PUBLIC_URL"
  nohup "$NGROK_BIN" http "$BRIDGE_PORT" --url "$PUBLIC_URL" > "$NGROK_LOG" 2>&1 &
  STARTED_NGROK=$!
  echo "$STARTED_NGROK" > "$NGROK_PID_FILE"
}

wait_for_tunnel() {
  local i
  for i in $(seq 1 60); do
    if ngrok_serving_domain; then
      log "tunnel up: $PUBLIC_URL"
      return 0
    fi
    if [[ -n "$STARTED_NGROK" ]] && ! pid_alive "$STARTED_NGROK"; then
      log "ngrok process exited; tail of $NGROK_LOG:"
      tail -n 5 "$NGROK_LOG" >&2 2>/dev/null || true
      break
    fi
    sleep 1
  done
  return 1
}

check_public_health() {
  local i
  for i in $(seq 1 30); do
    if curl -fsS -m 15 "$PUBLIC_URL/health" 2>/dev/null | grep -q '"status": "ok"'; then
      log "public health OK: $PUBLIC_URL/health"
      return 0
    fi
    sleep 2
  done
  return 1
}

# Main flow. Guarded so offline tests can source this file (functions and the
# runtime-path derivation) without starting any process or touching the
# network.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  resolve_instance
  resolve_public_url
  check_prereqs
  validate_sandbox_env
  acquire_start_lock
  trap release_start_lock EXIT
  ensure_bridge
  wait_for_local_health || fail "local /health did not become OK on $BRIDGE_URL (see $BRIDGE_OUT_LOG)"
  ensure_ngrok
  wait_for_tunnel || fail "ngrok tunnel for $PUBLIC_URL did not come up (see $NGROK_LOG)"
  check_public_health || fail "public /health did not become OK on $PUBLIC_URL/health (see $NGROK_LOG)"

  echo "$PUBLIC_URL" > "$PUBLIC_URL_FILE"
  echo
  echo "============================================================"
  echo "  READY — public bridge is live"
  echo "  local:   $BRIDGE_URL/health"
  echo "  public:  $PUBLIC_URL/health"
  echo "  bridge pid: $(cat "$BRIDGE_PID_FILE")  (log: $BRIDGE_LOG)"
  echo "  ngrok  pid: $(cat "$NGROK_PID_FILE")  (log: $NGROK_LOG)"
  echo "  stop both:   ./scripts/stop_ngrok_bridge.sh"
  echo "============================================================"
fi
