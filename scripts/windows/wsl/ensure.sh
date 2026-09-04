#!/usr/bin/env bash
SD=/home/comofgroupzou/.local/share/local-codex-bridge
if pgrep -f "$SD/scripts/start.sh" >/dev/null 2>&1; then
  exit 0
fi
mkdir -p "$SD/logs"
setsid nohup bash "$SD/scripts/start.sh" >> "$SD/logs/supervisor.out.log" 2>&1 &
echo "ensure: supervisor relaunched pid=$!" >> "$SD/logs/ensure.log"
exit 0
