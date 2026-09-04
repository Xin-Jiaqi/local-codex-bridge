#!/usr/bin/env bash
# Attached WSL keepalive: (1) ensure bridge+worker supervisor is up,
# (2) stay attached forever so the WSL VM never idles out.
SD=/home/comofgroupzou/.local/share/local-codex-bridge
bash "$SD/scripts/ensure.sh" >> "$SD/logs/ensure.log" 2>&1 || true
echo "$(date -u +%FT%TZ) keepalive attached (pid $$)" >> "$SD/logs/keepalive.log"
while true; do
  sleep 60
done
