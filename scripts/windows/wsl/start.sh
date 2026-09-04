#!/usr/bin/env bash
# WSL PRIMARY execution stack supervisor: Linux bridge (127.0.0.1:8322) + outbound worker.
# Router URL: $SD/router.url (one line). Secrets stay under $SD/secrets (0600).
SD=/home/comofgroupzou/.local/share/local-codex-bridge
REPO=/mnt/d/work-of-jiaqi/actions-bridge
PY=/usr/bin/python3
CODEX_BIN=/home/comofgroupzou/.codex/packages/standalone/current/bin/codex
BRIDGE_PORT=8322
RUN=$SD/run
LOG=$SD/logs
mkdir -p "$RUN" "$LOG"
ROUTER_URL=$(tr -d '\n ' < "$SD/router.url" 2>/dev/null)
[ -z "$ROUTER_URL" ] && ROUTER_URL="https://diploma-ideology-skier.ngrok-free.dev"
API_KEY=$(tr -d '\n' < "$SD/secrets/bridge.api.key")
export DEEPSEEK_API_KEY=$(tr -d '\n' < "$SD/secrets/deepseek.key")
export BRIDGE_WORKER_TOKEN=$(tr -d '\n' < "$SD/secrets/worker.token")
export BRIDGE_SANDBOX_MODE=bridge-workspace
export BRIDGE_INSTANCE=local
export WORKER_POSIX_MNT_MAP=1
export PYTHONUNBUFFERED=1

is_alive() { [ -f "$1" ] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null; }

log() { echo "$(date -u +%FT%TZ) $*"; }

ensure_bridge() {
  if is_alive "$RUN/bridge.pid"; then
    if curl -sf --max-time 2 "http://127.0.0.1:$BRIDGE_PORT/health" 2>/dev/null | grep -q '"ready": true'; then
      return 0
    fi
    log "bridge unhealthy; restarting"
    kill "$(cat "$RUN/bridge.pid")" 2>/dev/null; sleep 2
    rm -f "$RUN/bridge.pid"
  fi
  cd "$REPO" || return 1
  BRIDGE_API_KEY=$API_KEY setsid nohup "$PY" -m http_server --host 127.0.0.1 --port "$BRIDGE_PORT" \
    --codex-bin "$CODEX_BIN" --codex-home "$SD/codex-deepseek" \
    --log "$LOG/bridge.log" >> "$LOG/bridge.out.log" 2>&1 &
  echo $! > "$RUN/bridge.pid"
  log "bridge started pid=$(cat "$RUN/bridge.pid")"
  for _ in $(seq 1 45); do
    if curl -sf --max-time 2 "http://127.0.0.1:$BRIDGE_PORT/health" 2>/dev/null | grep -q '"ready": true'; then
      return 0
    fi
    sleep 1
  done
  log "bridge not ready after 45s (will retry)"
  return 1
}

ensure_worker() {
  if is_alive "$RUN/worker.pid"; then return 0; fi
  ROUTER_URL=$(tr -d '\n ' < "$SD/router.url" 2>/dev/null)
  [ -z "$ROUTER_URL" ] && ROUTER_URL="https://diploma-ideology-skier.ngrok-free.dev"
  cd "$REPO" || return 1
  setsid nohup "$PY" -m bridge.worker \
    --router-url "$ROUTER_URL" \
    --local-url "http://127.0.0.1:$BRIDGE_PORT" \
    --local-api-key-file "$SD/secrets/bridge.api.key" \
    --poll-timeout-s 15 \
    --state-file "$RUN/worker.state.json" >> "$LOG/worker.out.log" 2>&1 &
  echo $! > "$RUN/worker.pid"
  log "worker started pid=$(cat "$RUN/worker.pid") router=$ROUTER_URL"
  return 0
}

log "supervisor up (router=$ROUTER_URL)"
while true; do
  ensure_bridge
  ensure_worker
  if [ -f "$SD/ngrok.enable" ] && ! pgrep -f "$SD/bin/ngrok http" >/dev/null 2>&1; then
    bash "$SD/scripts/ngrok.sh" start >> "$LOG/supervisor.log" 2>&1 || true
  fi
  sleep 15
done
