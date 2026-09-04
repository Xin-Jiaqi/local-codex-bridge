<#
.SYNOPSIS
    Start/stop the Windows PRIMARY bridge ROUTER (native, 127.0.0.1:8321).

.DESCRIPTION
    Runs the same http_server as the Mac router but on Windows with
    BRIDGE_DUAL_HOST=true: it accepts GPT requests (Bearer BRIDGE_API_KEY)
    and routes D:\... / UNC cwds to the outbound WSL worker through the
    internal worker API. Secrets come from local-only stores:
      - BRIDGE_API_KEY:  <repo>\.bridge_api_key        (0600, gitignored)
      - worker token:    %LOCALAPPDATA%\local-codex-bridge\secrets\worker.token.dpapi (DPAPI)
      - DeepSeek key:    %LOCALAPPDATA%\local-codex-bridge\secrets\deepseek.key.dpapi  (DPAPI)
    Nothing secret is written or logged by this script. The router spawns a
    native codex app-server (idle; execution happens on the WSL worker).
.PARAMETER Stop
    Stop the router process this script started (router.pid).
.PARAMETER Repo
    Bridge repo checkout (default D:\work-of-jiaqi\actions-bridge).
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\start_primary_router.ps1
#>
param(
  [switch]$Stop,
  [string]$Repo = 'D:\work-of-jiaqi\actions-bridge'
)
$ErrorActionPreference = 'Stop'
$layout = Join-Path $env:LOCALAPPDATA 'local-codex-bridge'
$runtime = Join-Path $layout 'local\runtime'
$secrets = Join-Path $layout 'secrets'
$pidFile = Join-Path $runtime 'router.pid'
$outLog = Join-Path $runtime 'router.out.log'
$errLog = Join-Path $runtime 'router.err.log'
$logFile = Join-Path $runtime 'router.log'
$threadMap = Join-Path $layout 'local\thread_map.json'
$python = 'C:\Users\ComofGroupZou\AppData\Local\Programs\Python\Python312\python.exe'
$apiKeyFile = Join-Path $Repo '.bridge_api_key'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

function Get-RunningRouterPid {
  if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
  $pv = 0
  if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$pv) -and $pv -gt 0) {
    $pr = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $pv) -ErrorAction SilentlyContinue
    if ($pr -and $pr.CommandLine -like '*http_server*--port 8321*' -and $pr.CommandLine -like '*router.log*') { return $pv }
  }
  return $null
}

if ($Stop) {
  $pv = Get-RunningRouterPid
  if ($pv) { Stop-Process -Id $pv -Force -ErrorAction SilentlyContinue; Write-Output "router stopped (pid $pv)" }
  else { Write-Output "router not running" }
  exit 0
}

$running = Get-RunningRouterPid
if ($running) { Write-Output "router already running (pid $running)"; exit 0 }

Add-Type -AssemblyName System.Security
function Unprotect-Secret([string]$path) {
  $blob = [System.IO.File]::ReadAllBytes($path)
  return [System.Text.Encoding]::UTF8.GetString([System.Security.Cryptography.ProtectedData]::Unprotect($blob, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser))
}
$workerTokenFile = Join-Path $secrets 'worker.token.dpapi'
$deepseekFile = Join-Path $secrets 'deepseek.key.dpapi'
if (-not (Test-Path -LiteralPath $workerTokenFile)) { throw "missing $workerTokenFile" }
if (-not (Test-Path -LiteralPath $deepseekFile))  { throw "missing $deepseekFile (protect the DeepSeek key first)" }
if (-not (Test-Path -LiteralPath $apiKeyFile))    { throw "missing $apiKeyFile" }

$env:BRIDGE_API_KEY = (Get-Content -LiteralPath $apiKeyFile -Raw).Trim()
$env:BRIDGE_DUAL_HOST = 'true'
$env:BRIDGE_WORKER_TOKEN = (Unprotect-Secret $workerTokenFile).Trim()
$env:DEEPSEEK_API_KEY = (Unprotect-Secret $deepseekFile).Trim()
$env:BRIDGE_SANDBOX_MODE = 'bridge-workspace'
$env:BRIDGE_INSTANCE = 'local'
$env:BRIDGE_NETWORK_ACCESS = 'true'
$env:BRIDGE_THREAD_MAP = $threadMap
$env:PYTHONUTF8 = '1'

# Codex binary: $env:CODEX_BIN wins, else newest native install dir.
$codexBin = $env:CODEX_BIN
if (-not $codexBin) {
  $candidates = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin') -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending
  foreach ($c in $candidates) {
    $probe = Join-Path $c.FullName 'codex.exe'
    if (Test-Path -LiteralPath $probe) { $codexBin = $probe; break }
  }
}
if (-not $codexBin -or -not (Test-Path -LiteralPath $codexBin)) { throw 'codex.exe not found (set CODEX_BIN)' }

$codexHome = Join-Path $layout 'codex-deepseek'
$wargs = @('-m','http_server','--host','127.0.0.1','--port','8321','--codex-bin',$codexBin,'--codex-home',$codexHome,'--log',$logFile)
$p = Start-Process -FilePath $python -ArgumentList $wargs -WorkingDirectory $Repo -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
Start-Sleep -Seconds 2
if ($p.HasExited) { Write-Output ("router exited early: " + (Get-Content $errLog -Raw -ErrorAction SilentlyContinue)); exit $p.ExitCode }
[System.IO.File]::WriteAllText($pidFile, [string]$p.Id)
Write-Output ("router started pid=" + $p.Id)
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Seconds 1
  try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8321/health' -TimeoutSec 3
    if ($h.ready -and $h.'dual_host'.enabled) { Write-Output 'router health: ready + dual_host enabled'; break }
  } catch { }
}
$p.WaitForExit()
exit $p.ExitCode
