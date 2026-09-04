#!/usr/bin/env bash
# WSL Linux ngrok control: public HTTPS entry -> Windows PRIMARY router (127.0.0.1:8321).
# Usage: ngrok.sh start|stop|status
# Note: the ngrok account owns ONE static domain (diploma-ideology-skier.ngrok-free.dev).
# While the Mac agent holds it, start() fails with ERR_NGROK_334 - expected until the
# documented domain cutover (stop Mac ngrok first). Proxy env is cleared because the
# WSL autoProxy env makes ngrok demand a paid plan (ERR_NGROK_9009).
SD=/home/comofgroupzou/.local/share/local-codex-bridge
BIN=$SD/bin/ngrok
LOG=$SD/logs/ngrok.out.log
URLFILE=$SD/logs/ngrok.url
mkdir -p $SD/logs
is_running() { pgrep -f "$BIN http" >/dev/null 2>&1; }
write_url() {
  for _ in $(seq 1 15); do
    sleep 2
    URL=$(curl -s --max-time 3 http://127.0.0.1:4040/api/tunnels 2>/dev/null | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)
  ts=[t for t in d.get("tunnels",[]) if t.get("public_url")]
  print(ts[0]["public_url"] if ts else "")
except Exception: print("")' 2>/dev/null)
    [ -n "$URL" ] && { echo "$URL" > "$URLFILE"; chmod 600 "$URLFILE"; return 0; }
  done
  return 1
}
start() {
  if is_running; then status; return 0; fi
  env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    setsid nohup "$BIN" http 8321 --log-level=info > "$LOG" 2>&1 &
  sleep 3
  write_url && status || { echo "ngrok start failed (see $LOG; expected ERR_NGROK_334 while the Mac holds the domain)"; tail -4 "$LOG"; return 1; }
}
stop() { pkill -f "$BIN http" 2>/dev/null; rm -f "$URLFILE"; echo "ngrok stopped"; }
status() {
  if is_running; then
    echo "ngrok running; public_url=$(cat "$URLFILE" 2>/dev/null || echo retrieving...)"
  else
    echo "ngrok not running"
  fi
}
case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: ngrok.sh start|stop|status"; exit 2 ;;
esac
