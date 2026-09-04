#!/usr/bin/env bash
# =============================================================================
# cutover_primary_to_wsl.sh  (MVP, one-shot, idempotent)
#
# Move the single ngrok static domain from the Mac bridge to the
# Windows/WSL PRIMARY stack WITHOUT changing the public URL:
#
#   https://diploma-ideology-skier.ngrok-free.dev  (ChatGPT Action URL stays)
#
# PREREQUISITES (verified before anything is touched):
#   - Mac bridge/router healthy at http://127.0.0.1:8321 (public endpoint up)
#   - Windows PRIMARY router healthy via `ssh win` + `wsl.exe -e`
#     (127.0.0.1:8321 on the shared mirrored loopback, dual_host enabled,
#     worker_online=true) and the WSL worker stack is up
#   - WSL supervisor scripts at ~/.local/share/local-codex-bridge/scripts/
#     (start.sh watches $WSL_SD/ngrok.enable and keeps ngrok up)
#   - Mac ngrok is managed by the local instance supervisor
#     (run_local_supervisor.sh, pid in $MAC_STATE/supervisor.pid) whose child
#     would normally re-claim the domain ~7s after a kill; this script
#     transiently SIGSTOPs that supervisor, then resumes it on exit
#
# WHAT IT DOES
#   1. preflight (Windows router worker_online=true, Mac public /health ok,
#      API key file present); abort with exit 2 and touch nothing otherwise
#   2. if the WSL ngrok already holds the domain -> skip to step 5
#   3. SIGSTOP the Mac supervisor (existing safe mechanism, no system config
#      change), SIGTERM the Mac ngrok (identity checked; SIGKILL fallback)
#   4. WSL: touch ngrok.enable + start Linux ngrok on the SAME static domain
#   5. low-frequency public /health poll, max ~60s
#   6. on PASS: harmless read-only /start -> /observe through the PUBLIC URL;
#      requires WSL/Linux evidence (pwd under /mnt/d, hostname 1F-Theory,
#      uname Linux) -> prints "CUTOVER PASS / Mac may go offline", exit 0
#   7. on ANY failure: automatic rollback (stop WSL ngrok, remove the enable
#      flag, resume the Mac supervisor so it restarts the Mac ngrok, verify
#      public health is back) and exit non-zero
#
# SAFETY
#   - set -euo pipefail; never prints API keys/tokens (they are read from
#     local files only); logs (no secrets) to $MAC_STATE/cutover_primary_to_wsl.log
#   - never touches the Mac bridge/router process, only the ngrok child
#   - rollback also runs on Ctrl-C / unexpected exit (trap EXIT)
#
# USAGE
#   ./scripts/cutover_primary_to_wsl.sh
# =============================================================================
set -euo pipefail

DOMAIN="diploma-ideology-skier.ngrok-free.dev"
PUBLIC_URL="https://${DOMAIN}"
MAC_STATE="${HOME}/.local/state/local-codex-bridge/local/runtime"
MAC_SUPERVISOR_DIR="${HOME}/.local/share/local-codex-bridge/current/scripts"
MAC_API_KEY_FILE="${HOME}/.config/local-codex-bridge/api_key"
WSL_SD="/home/comofgroupzou/.local/share/local-codex-bridge"
WSL_FLAG="${WSL_SD}/ngrok.enable"
WSL_ROUTER_URL="http://127.0.0.1:8321"     # shared mirrored loopback -> Windows router
LOG="${MAC_STATE}/cutover_primary_to_wsl.log"

SSH=(ssh -T -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR -o RemoteCommand=none win)

# --------------------------------------------------------------------- state
PASSED=0
SUP_STOPPED=0
MAC_NGROK_KILLED=0
WSL_TOUCHED=0
SUP_PID=""
MAC_NGROK_PID=""

mkdir -p "${MAC_STATE}"
log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "${LOG}"; }

json_ok() {  # stdin: /health body; prints OK when status=ok AND ready=true
  python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print("OK" if d.get("status") == "ok" and d.get("ready") is True else "BAD")
except Exception:
    print("BAD")'
}

wsl_run() {  # heredoc body -> WSL bash
  "${SSH[@]}" 'wsl.exe -e bash -s'
}

# ------------------------------------------------------------------ rollback
rollback() {
  log "ROLLBACK: removing WSL ngrok enable flag + stopping WSL ngrok"
  WSL_TOUCHED=0
  wsl_run <<'WSL' >/dev/null 2>&1 || true
SD=/home/comofgroupzou/.local/share/local-codex-bridge
rm -f "$SD/ngrok.enable"
bash "$SD/scripts/ngrok.sh" stop >/dev/null 2>&1 || true
WSL
  if [[ -n "${SUP_PID}" ]] && kill -0 "${SUP_PID}" 2>/dev/null; then
    log "ROLLBACK: resuming Mac supervisor pid ${SUP_PID}"
    kill -CONT "${SUP_PID}" 2>/dev/null || true
    SUP_STOPPED=0
  fi
  log "ROLLBACK: waiting for Mac ngrok to reclaim the domain (up to 45s)"
  ok=""
  for _ in $(seq 1 9); do
    body="$(curl -sS --max-time 8 "${PUBLIC_URL}/health" 2>/dev/null || true)"
    [[ "$(printf '%s' "${body}" | json_ok)" == "OK" ]] && { ok=1; break; }
    sleep 5
  done
  if [[ -z "${ok}" ]]; then
    log "ROLLBACK: supervisor did not restore the tunnel; trying start_ngrok_bridge.sh"
    BRIDGE_INSTANCE=local "${MAC_SUPERVISOR_DIR}/start_ngrok_bridge.sh" >>"${LOG}" 2>&1 || true
    for _ in $(seq 1 6); do
      body="$(curl -sS --max-time 8 "${PUBLIC_URL}/health" 2>/dev/null || true)"
      [[ "$(printf '%s' "${body}" | json_ok)" == "OK" ]] && { ok=1; break; }
      sleep 5
    done
  fi
  if [[ -n "${ok}" ]]; then
    log "ROLLBACK: public health restored (Mac holds the domain again)"
  else
    log "ROLLBACK: CRITICAL - public health NOT restored; run manually:"
    log "  BRIDGE_INSTANCE=local ${MAC_SUPERVISOR_DIR}/start_ngrok_bridge.sh"
  fi
}

cleanup() {
  local rc=$?
  if [[ "${SUP_STOPPED}" == 1 ]] && [[ -n "${SUP_PID}" ]] && kill -0 "${SUP_PID}" 2>/dev/null; then
    kill -CONT "${SUP_PID}" 2>/dev/null || true
    SUP_STOPPED=0
    log "supervisor pid ${SUP_PID} resumed"
  fi
  if [[ "${PASSED}" != 1 ]] && { [[ "${MAC_NGROK_KILLED}" == 1 ]] || [[ "${WSL_TOUCHED}" == 1 ]]; }; then
    log "cutover did not complete -> rollback"
    rollback
    exit 3
  fi
  exit "${rc}"
}
trap cleanup EXIT

# ---------------------------------------------------------------- preflight
log "preflight: Windows router / WSL worker health via ssh win"
wsl_health="$(wsl_run <<'WSL' 2>/dev/null || true
curl -s --max-time 6 http://127.0.0.1:8321/health 2>/dev/null || true
WSL
)"
if ! printf '%s' "${wsl_health}" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    dh = d.get("dual_host") or {}
    sys.exit(0 if (d.get("ready") is True and dh.get("enabled") is True and dh.get("worker_online") is True) else 1)
except Exception:
    sys.exit(1)'; then
  log "FAIL: Windows router not ready / worker_online != true (nothing touched)"
  exit 2
fi
log "OK: Windows router ready + worker_online=true"

log "preflight: Mac public /health"
mac_public="$(curl -sS --max-time 10 "${PUBLIC_URL}/health" 2>/dev/null || true)"
if [[ "$(printf '%s' "${mac_public}" | json_ok)" != "OK" ]]; then
  log "FAIL: Mac public /health not OK (nothing touched)"
  exit 2
fi
log "OK: Mac public /health OK (domain currently served)"

if [[ ! -r "${MAC_API_KEY_FILE}" ]] || [[ -z "$(tr -d '\n\r' < "${MAC_API_KEY_FILE}")" ]]; then
  log "FAIL: API key file ${MAC_API_KEY_FILE} missing/empty (nothing touched)"
  exit 2
fi
log "OK: API key present (never printed)"

# ------------------------------------------- already-cut-over? (idempotent)
wsl_tunnel="$(wsl_run <<'WSL' 2>/dev/null || true
curl -s --max-time 4 http://127.0.0.1:4040/api/tunnels 2>/dev/null || true
WSL
)"
if printf '%s' "${wsl_tunnel}" | grep -q "${DOMAIN}"; then
  log "WSL ngrok already holds ${DOMAIN}; skipping cutover, running verification"
else
  # --------------------------------------------------------- cutover actions
  log "reading Mac supervisor + ngrok pids"
  if [[ -f "${MAC_STATE}/supervisor.pid" ]]; then
    SUP_PID="$(cat "${MAC_STATE}/supervisor.pid" 2>/dev/null || true)"
    if [[ -n "${SUP_PID}" ]] && kill -0 "${SUP_PID}" 2>/dev/null; then
      sup_cmd="$(ps -p "${SUP_PID}" -o command= 2>/dev/null || true)"
      if [[ "${sup_cmd}" != *"run_local_supervisor.sh"* ]]; then
        log "WARN: supervisor pid ${SUP_PID} is not run_local_supervisor.sh; proceeding without suppression"
        SUP_PID=""
      fi
    else
      SUP_PID=""
    fi
  fi

  if [[ -f "${MAC_STATE}/ngrok.pid" ]]; then
    MAC_NGROK_PID="$(cat "${MAC_STATE}/ngrok.pid" 2>/dev/null || true)"
  fi
  if [[ -z "${MAC_NGROK_PID}" ]] || ! kill -0 "${MAC_NGROK_PID}" 2>/dev/null; then
    log "FAIL: Mac ngrok pid missing/not running (nothing touched)"
    exit 2
  fi
  ngrok_cmd="$(ps -p "${MAC_NGROK_PID}" -o command= 2>/dev/null || true)"
  if [[ "${ngrok_cmd}" != *"ngrok"* ]]; then
    log "FAIL: pid ${MAC_NGROK_PID} is not a managed ngrok process (never kill unmanaged; nothing touched)"
    exit 3
  fi
  log "OK: Mac ngrok pid ${MAC_NGROK_PID} verified managed"

  if [[ -n "${SUP_PID}" ]]; then
    log "suppressing Mac supervisor pid ${SUP_PID} (SIGSTOP) so it cannot re-claim the domain"
    kill -STOP "${SUP_PID}"
    SUP_STOPPED=1
  fi

  log "stopping Mac ngrok pid ${MAC_NGROK_PID} (SIGTERM)"
  kill -TERM "${MAC_NGROK_PID}" 2>/dev/null || true
  MAC_NGROK_KILLED=1
  for _ in $(seq 1 6); do
    kill -0 "${MAC_NGROK_PID}" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "${MAC_NGROK_PID}" 2>/dev/null; then
    log "ngrok still alive after SIGTERM; SIGKILL"
    kill -KILL "${MAC_NGROK_PID}" 2>/dev/null || true
  fi
  log "Mac ngrok stopped"

  log "activating WSL ngrok (touch flag + start)"
  WSL_TOUCHED=1
  wsl_run >/dev/null 2>&1 <<'WSL' || true
SD=/home/comofgroupzou/.local/share/local-codex-bridge
touch "$SD/ngrok.enable"
bash "$SD/scripts/ngrok.sh" start >/dev/null 2>&1 || true   # supervisor retries if this is too early
WSL
  log "WSL ngrok activation issued"
fi

# ------------------------------------------------- public health poll (<=60s)
log "polling ${PUBLIC_URL}/health (low frequency, max ~60s)"
ok=""
for _ in $(seq 1 12); do
  body="$(curl -sS --max-time 8 "${PUBLIC_URL}/health" 2>/dev/null || true)"
  if [[ "$(printf '%s' "${body}" | json_ok)" == "OK" ]]; then
    ok=1
    break
  fi
  sleep 5
done
if [[ -z "${ok}" ]]; then
  log "FAIL: public /health did not PASS within 60s (rollback follows)"
  exit 3
fi
log "OK: public /health PASS"

# ------------------------------------------- end-to-end read-only codex turn
KEY="$(tr -d '\n\r' < "${MAC_API_KEY_FILE}")"
log "E2E: harmless /start via public URL (cwd D:\\work-of-jiaqi\\projects)"
start_body="$(curl -sS --max-time 60 -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' \
  -d '{"prompt":"Run exactly one read-only shell command and report its output verbatim in a code block:  pwd && hostname && uname -s . Do NOT run any other tool, do NOT read or write files, do NOT use the network.","cwd":"D:\\work-of-jiaqi\\projects"}' \
  "${PUBLIC_URL}/start")"
TID="$(printf '%s' "${start_body}" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("thread_id",""))
except Exception: print("")')"
TURN="$(printf '%s' "${start_body}" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("turn_id",""))
except Exception: print("")')"
if [[ -z "${TID}" ]] || [[ -z "${TURN}" ]]; then
  log "FAIL: /start did not return a thread (rollback follows)"
  exit 3
fi
log "thread ${TID} started; observing..."
assistant=""
for _ in $(seq 1 8); do
  ob="$(curl -sS --max-time 25 -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' \
    -d "{\"thread_id\":\"${TID}\",\"turn_id\":\"${TURN}\",\"wait_ms\":15000}" "${PUBLIC_URL}/observe" 2>/dev/null || true)"
  st="$(printf '%s' "${ob}" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("status",""))
except Exception: print("")')"
  if [[ "${st}" == "completed" ]]; then
    assistant="$(printf '%s' "${ob}" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("assistant_text","") or "")
except Exception: print("")')"
    break
  fi
  if [[ "${st}" == "failed" ]] || [[ -z "${st}" ]]; then
    log "FAIL: /observe status=${st:-empty}"
    break
  fi
  sleep 2
done
if [[ "${assistant}" == *"/mnt/d/"* && "${assistant}" == *"1F-Theory"* && "${assistant}" == *"Linux"* ]]; then
  PASSED=1
  log "E2E OK: execution evidence on WSL (pwd under /mnt/d, hostname 1F-Theory, uname Linux)"
  log "CUTOVER PASS / Mac may go offline"
  echo "------------------------------------------------------------"
  echo "CUTOVER PASS / Mac may go offline"
  echo "public URL (unchanged): ${PUBLIC_URL}"
  echo "domain holder: Windows/WSL PRIMARY (Mac ngrok kept stopped)"
  echo "Mac bridge/router: SECONDARY (local http://127.0.0.1:8321 still up)"
  echo "rollback later: stop WSL ngrok + remove ~/.local/share/local-codex-bridge/ngrok.enable,"
  echo "  then on Mac: BRIDGE_INSTANCE=local ${MAC_SUPERVISOR_DIR}/start_ngrok_bridge.sh"
  exit 0
fi
log "FAIL: E2E did not show WSL/Linux evidence (rollback follows)"
exit 3
