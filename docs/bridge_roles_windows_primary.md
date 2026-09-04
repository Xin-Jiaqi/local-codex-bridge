# Bridge roles: Windows+WSL PRIMARY / Mac SECONDARY (MVP, 2026-09-04)

Dual-host router branch (`dual-host-router`). Execution happens in WSL Linux
(avoids the broken Windows native sandbox runner). No secrets in this repo:
keys/tokens live only in local secret stores (DPAPI on Windows,
`~/.local/share/local-codex-bridge/secrets/` 0600 in WSL).

## Topology (MVP)
```
ChatGPT (Custom GPT Actions)
  -> public endpoint (ngrok, one static domain per ngrok account)
  -> Windows PRIMARY router: python -m http_server 127.0.0.1:8321
     (BRIDGE_DUAL_HOST=true, BRIDGE_WORKER_TOKEN set; native bridge-workspace)
  -> internal worker API (Bearer worker token)
  -> WSL outbound worker -> WSL Linux bridge 127.0.0.1:8322
     (Linux codex standalone, CODEX_HOME=codex-deepseek, DeepSeek provider)
  -> Linux sandbox execution under /mnt/d/... (D:\ cwd mapped via WORKER_POSIX_MNT_MAP)
```
- Windows native = router/gateway only (no execution; native sandbox runner is
  broken on this box: pipe-in 15s timeout across codex 0.151-0.153).
- WSL = execution worker (host `1F-Theory`, Ubuntu-24.04, systemd enabled).
- Router URL for the worker is one line in `$SD/router.url`
  (now `http://127.0.0.1:8321`; mirrored WSL networking shares the loopback).

## Services / autostart (Windows Task Scheduler, user logon)
- `LCB-Primary-Router` (every 3 min + at logon):
  powershell -File D:\work-of-jiaqi\actions-bridge\scripts\windows\start_primary_router.ps1
  -> starts/sticks the native router on 127.0.0.1:8321 (pid
  %LOCALAPPDATA%\local-codex-bridge\local\runtime\router.pid; logs router*.log).
- `LCB-Primary-Stack` (every 3 min + at logon):
  wsl.exe -d Ubuntu-24.04 -u comofgroupzou -- bash -lc "exec .../keepalive.sh"
  -> keeps the WSL VM attached/alive; keepalive runs `ensure.sh`, which starts
  the supervisor `start.sh` (Linux bridge 8322 + outbound worker) if missing.
- Old native worker task `LCB-Worker` is Disabled (it resurrected the broken
  native worker).
- WSL supervisor: $SD/scripts/start.sh; logs $SD/logs/*.log; stop: stop.sh.
- ngrok (WSL Linux, $SD/bin/ngrok, config $HOME/.config/ngrok/ngrok.yml 0600):
  `bash $SD/scripts/ngrok.sh start|stop|status`. Supervisor auto-starts it only
  when flag file $SD/ngrok.enable exists. ngrok account owns ONE static domain
  (diploma-ideology-skier.ngrok-free.dev): while the Mac agent holds it, WSL
  start fails with ERR_NGROK_334 (expected). Cutover = stop Mac ngrok, then
  `touch $SD/ngrok.enable` (supervisor brings the tunnel up within ~15 s;
  public URL stays the same domain, so the ChatGPT Action URL is unchanged).

## One-shot cutover script
 (Mac terminal, run once): moves the static
ngrok domain from Mac to the WSL ngrok on the SAME public URL with preflight,
~60s public-health poll, harmless public /start->/observe WSL-evidence check,
and automatic rollback on any failure. See its header + README section.
## Verification (2026-09-04 MVP)
- Windows router /health: ready, dual_host worker_online=true.
- Harmless end-to-end (public-URL test pending domain cutover; private loop PASS):
  POST /start {"cwd":"D:\\work-of-jiaqi\\projects"} -> worker maps to
  /mnt/d/work-of-jiaqi/projects; assistant_text = "/mnt/d/work-of-jiaqi/projects
  1F-Theory Linux" (executed by WSL Linux codex).

## Roles
- PRIMARY (24h): Windows machine 1f-theory - native router + WSL worker. Must
  stay the ONLY worker target (single outbound worker; no dual-primary).
- SECONDARY: Mac (jiaqimacbook-air). Mac bridge/router stays reachable for
  POSIX tasks while online; D:\-routed jobs on the Mac URL fail cleanly while
  the worker points at Windows. To fall back: point $SD/router.url back at the
  Mac public URL, restart the WSL worker, and (if the domain moved) stop the
  WSL ngrok + start the Mac ngrok.
