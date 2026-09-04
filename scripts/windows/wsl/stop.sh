#!/usr/bin/env bash
SD=/home/comofgroupzou/.local/share/local-codex-bridge
for name in worker bridge; do
  pf="$SD/run/$name.pid"
  if [ -f "$pf" ]; then
    pid=$(cat "$pf" 2>/dev/null)
    kill "$pid" 2>/dev/null && echo "stopped $name ($pid)"
    rm -f "$pf"
  fi
done
pkill -f 'bridge.worker' 2>/dev/null && echo "pkill worker fallback"
pkill -f 'http_server --host 127.0.0.1 --port 8322' 2>/dev/null && echo "pkill bridge fallback"
pkill -f "$SD/scripts/start.sh" 2>/dev/null && echo "pkill supervisor fallback"
exit 0
