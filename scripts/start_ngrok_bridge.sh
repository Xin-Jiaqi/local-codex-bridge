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
# Idempotent: if the recorded PIDs in .runtime/ are still alive, they are
# reused instead of starting duplicates. PID files only ever contain PIDs of
# processes started by this script. If port 8321 or the ngrok domain is
# occupied by an unmanaged process, the script reports a clear error and
# exits WITHOUT touching it.
#
# Secrets: .bridge_api_key is read into BRIDGE_API_KEY at runtime (env var
# only); it is never written to the script, logs, or pid files.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUNTIME_DIR="$ROOT/.runtime"
BRIDGE_PID_FILE="$RUNTIME_DIR/bridge.pid"
NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"
BRIDGE_LOG="$RUNTIME_DIR/bridge.log"
BRIDGE_OUT_LOG="$RUNTIME_DIR/bridge.out.log"
NGROK_LOG="$RUNTIME_DIR/ngrok.log"
PUBLIC_URL_FILE="$ROOT/.public_url"

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

if [[ -z "${NGROK_DOMAIN:-}" && -f "$ROOT/.ngrok_domain" ]]; then
  NGROK_DOMAIN="$(tr -d '[:space:]' < "$ROOT/.ngrok_domain")"
fi
if [[ -z "${NGROK_DOMAIN:-}" ]]; then
  die "NGROK_DOMAIN is required: export NGROK_DOMAIN=<your-domain> or write it to $ROOT/.ngrok_domain (gitignored)"
fi
PUBLIC_URL="https://$NGROK_DOMAIN"

STARTED_BRIDGE=""
STARTED_NGROK=""

pid_alive() { kill -0 "$1" 2>/dev/null; }
is_valid_pid() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 > 1 )); }

# Stop only processes created by THIS run; pre-existing processes are never
# touched. Called before every failure exit after startup has begun.
cleanup_started() {
  local pid
  if [[ -n "$STARTED_BRIDGE" ]]; then
    pid="$STARTED_BRIDGE"
    if pid_alive "$pid"; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$BRIDGE_PID_FILE"
    STARTED_BRIDGE=""
  fi
  if [[ -n "$STARTED_NGROK" ]]; then
    pid="$STARTED_NGROK"
    if pid_alive "$pid"; then
      kill "$pid" 2>/dev/null || true
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

ensure_bridge() {
  if [[ -f "$BRIDGE_PID_FILE" ]] && is_valid_pid "$(cat "$BRIDGE_PID_FILE" 2>/dev/null || true)" \
      && pid_alive "$(cat "$BRIDGE_PID_FILE" 2>/dev/null || true)"; then
    log "bridge already running (pid $(cat "$BRIDGE_PID_FILE")); reusing it"
    return 0
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
  if [[ -f "$NGROK_PID_FILE" ]] && is_valid_pid "$(cat "$NGROK_PID_FILE" 2>/dev/null || true)" \
      && pid_alive "$(cat "$NGROK_PID_FILE" 2>/dev/null || true)"; then
    log "ngrok already running (pid $(cat "$NGROK_PID_FILE")); reusing it"
    return 0
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

check_prereqs
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
