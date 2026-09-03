<#
.SYNOPSIS
    Start or stop the Local Codex Bridge on Windows (native PowerShell).

.DESCRIPTION
    Windows bootstrap for the Local Codex Bridge (macOS behavior unchanged).

    * Checks and installs the dependencies without admin: Python 3.8+ is
      installed automatically - winget first (--scope user), then the
      official python.org per-user installer (silent, no UAC); PATH is
      refreshed from the registry and startup continues. Codex CLI via npm
      (user prefix) or the official native installer; ngrok into
      %LOCALAPPDATA%\ngrok. If an automatic install is impossible, the
      script prints the shortest manual fallback and exits.
    * Creates the default work root D:\work-of-jiaqi when missing.
    * Starts the bridge on 127.0.0.1:8321 (python -m http_server) with the
      SAME HTTP API as macOS: /health /ready /start /continue /observe
      /steer /interrupt /threads (no protocol change).
    * Verifies local /health AND /ready before starting anything outbound.

    Desktop OpenAI / Bridge DeepSeek separation (no app-server --profile):
    * Desktop Codex keeps %USERPROFILE%\.codex. When that config is marked as
      DeepSeek, it is restored from the official DeepSeek backup
      %USERPROFILE%\.codex\backup-deepseek\config.toml; without a backup a
      minimal OpenAI config (model "gpt-5.6-sol") is written instead.
      auth.json, state and history are never read or touched. A USER-level
      OPENAI_BASE_URL is removed only when it points at api.deepseek.com
      (normal proxies are kept).
    * The migrated DeepSeek config goes to the dedicated CODEX_HOME
      %LOCALAPPDATA%\local-codex-bridge\codex-deepseek; the bridge app-server
      always uses it and scripts/windows/codex-deepseek.cmd wraps the real
      codex CLI with that CODEX_HOME (ordinary Desktop codex keeps OpenAI).
    * The DeepSeek key is never stored in plain text: after a masked first
      prompt it is DPAPI-encrypted to
      %LOCALAPPDATA%\local-codex-bridge\secrets\deepseek.key.dpapi and is
      only injected into the spawned child process environment (never
      printed; auth.json is never read).

    DEFAULT mode = dual-host (one GPT controls Mac + Windows):
    Windows opens no public port and never takes the ngrok domain. After the
    local bridge is healthy the script starts the OUTBOUND worker
    (python -m bridge.worker), which long-polls the Mac bridge's internal
    worker API at <MacBridgeUrl> and forwards allowlisted jobs to the local
    bridge on http://127.0.0.1:8321. The Mac bridge keeps serving its fixed
    public URL; a Windows worker outage never affects Mac requests.

    -NgrokCutover (legacy, deprecated): the old one-machine-at-a-time flow -
    Windows takes over the fixed ngrok domain after the macOS bridge is
    stopped externally (./scripts/stop_ngrok_bridge.sh on the Mac). Kept for
    rollback only; the dual-host worker does not use ngrok.

    Codex detection order: $env:CODEX_BIN, %APPDATA%\npm\codex.cmd (npm),
    codex.exe on PATH (native), codex.cmd on PATH. Instance state lives
    OUTSIDE the repo under %LOCALAPPDATA%\local-codex-bridge\local (mirror of
    the macOS control plane) and only contains non-secret fields. API keys,
    the DeepSeek key, the worker token and the ngrok authtoken are never
    printed, logged or written to pid/instance files.

.PARAMETER NoNgrok
    Preparation mode: start and verify the LOCAL bridge only
    (127.0.0.1:8321); neither ngrok (legacy cutover) nor the dual-host worker
    is started. Re-run without -NoNgrok to start the worker that connects the
    local bridge to the Mac router.

.PARAMETER Stop
    Stop only the bridge/ngrok processes this script started (PID files in
    the instance runtime dir; unmanaged processes are never touched).

.PARAMETER MacBridgeUrl
    Public URL of the Mac bridge router the outbound worker long-polls
    (default: $env:MAC_BRIDGE_URL, else
    https://diploma-ideology-skier.ngrok-free.dev). Windows only ever makes
    OUTBOUND https connections to this URL - it never exposes a public port.

.PARAMETER WorkerToken
    Independent worker token of the Mac router (must match the
    BRIDGE_WORKER_TOKEN configured on the Mac bridge). When omitted the
    script reuses the DPAPI-protected copy and asks once (masked) on first
    use.

.PARAMETER NgrokCutover
    Legacy one-machine-at-a-time mode (deprecated): start ngrok so Windows
    owns the fixed domain. Requires the macOS bridge to be stopped first.

.PARAMETER Domain
    Fixed ngrok domain (legacy -NgrokCutover only). Default:
    $env:NGROK_DOMAIN, else diploma-ideology-skier.ngrok-free.dev.

.PARAMETER CodexHome
    CODEX_HOME for the spawned app-server. Default: $env:CODEX_HOME, else
    %LOCALAPPDATA%\local-codex-bridge\codex-deepseek (the dedicated DeepSeek
    profile; Desktop %USERPROFILE%\.codex stays OpenAI).

.PARAMETER WorkRoot
    Default task work root, created when missing. Default:
    $env:BRIDGE_WORK_ROOT, else D:\work-of-jiaqi. /start may use any
    explicit absolute project directory outside the bridge control plane.

.PARAMETER SkipAutoInstall
    Never auto-install missing tools (Python, npm/native codex, ngrok
    download); only report what is missing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\start_local_codex_bridge.ps1 -NoNgrok

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\start_local_codex_bridge.ps1 `
        -MacBridgeUrl https://diploma-ideology-skier.ngrok-free.dev -WorkerToken <token>

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\start_local_codex_bridge.ps1 -Stop

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\start_local_codex_bridge.ps1 -NgrokCutover
#>
[CmdletBinding()]
param(
    [switch]$NoNgrok,
    [switch]$Stop,
    [switch]$NgrokCutover,
    [string]$MacBridgeUrl = "",
    [string]$WorkerToken = "",
    [string]$Domain = "",
    [string]$CodexHome = "",
    [string]$WorkRoot = "",
    [switch]$SkipAutoInstall
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

# ------------------------------------------------------------------ layout
$script:Version = "dual-host-router-1.0.0"
$script:FixedDomainDefault = "diploma-ideology-skier.ngrok-free.dev"
$script:MacBridgeUrlDefault = "https://diploma-ideology-skier.ngrok-free.dev"
$script:Instance = "local"
$script:HostAddr = "127.0.0.1"
$script:Port = 8321
$script:BaseUrl = "http://$($script:HostAddr):$($script:Port)"

# RepoRoot is needed by every mode (bridge working dir + .bridge_api_key).
# Resolve it from $PSScriptRoot right away, but only after guarding against an
# empty script root: when the script is pasted/run via -Command, $PSScriptRoot
# is empty and Join-Path/Resolve-Path would fail with a cryptic 'parameter
# "Path" is an empty string' binding error instead of a diagnosable message.
if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) {
    throw "this script must be run from a file (powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\windows\start_local_codex_bridge.ps1): the script root is empty"
}
try {
    $script:RepoRoot = (Resolve-Path -LiteralPath (Join-Path -Path $PSScriptRoot -ChildPath "..\..") -ErrorAction Stop).Path
} catch {
    throw "cannot resolve the bridge repo root from '$PSScriptRoot' (expected scripts\windows\start_local_codex_bridge.ps1 inside the repo): $($_.Exception.Message)"
}
if ([string]::IsNullOrWhiteSpace($script:RepoRoot)) {
    throw "the bridge repo root resolved to an empty path from '$PSScriptRoot'"
}
$script:KeyFile = Join-Path $script:RepoRoot ".bridge_api_key"

# Instance layout is derived in Initialize-Layout (after the environment
# sanity check at the top of main). The placeholders below are intentionally
# plain "" and are never Join-Path'd at load time: InstanceDir/RuntimeDir are
# still empty here and Windows PowerShell would abort EVERY run (any flag,
# even -Stop) before main starts with 'Cannot bind argument to parameter
# "Path" because it is an empty string'.
$script:StateRootBase = ""
$script:InstanceDir = ""
$script:RuntimeDir = ""
$script:DeepseekCodexHome = ""
$script:SecretsDir = ""
$script:DeepseekSecretFile = ""
$script:WorkerTokenFile = ""
$script:BridgePidFile = ""
$script:NgrokPidFile = ""
$script:WorkerPidFile = ""
$script:BridgeLog = ""
$script:BridgeOutLog = ""
$script:BridgeErrLog = ""
$script:NgrokLog = ""
$script:NgrokOutLog = ""
$script:NgrokErrLog = ""
$script:WorkerLog = ""
$script:WorkerOutLog = ""
$script:WorkerErrLog = ""
$script:WorkerStateFile = ""
$script:InstanceJson = ""
$script:CodexHome = ""
$script:CodexBin = ""
$script:WorkRoot = ""
$script:MacBridgeUrl = ""
$script:WorkerTokenValue = ""

$script:StartedBridgeNow = $false
$script:StartedNgrokNow = $false
$script:StartedWorkerNow = $false
$script:BridgePid = 0
$script:NgrokPid = 0
$script:WorkerPid = 0
$script:EffectiveMode = "workspace-write"
$script:EffectivePolicy = "on-request"
$script:EffectiveNetwork = "false"
$script:EnvBackup = @{}

# PS 5.1 defaults to TLS 1.0; downloads and https health checks need TLS 1.2.
try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

function Write-Info {
    Write-Host "[start] $args"
}

function Write-Fail {
    Write-Host "[start] error: $args" -ForegroundColor Red
}

function Exit-With([int]$Code, [string]$Message) {
    if ($Message) { Write-Fail $Message }
    Stop-StartedChildren
    exit $Code
}

function Write-FailDetail {
    # Diagnostic printer for catch blocks: exception message plus the script
    # position / PowerShell stack of the failure. Only positions and stack
    # text are echoed - never InvocationInfo.Line or argument values - so no
    # secret can leak through this output.
    param([System.Management.Automation.ErrorRecord]$Record)
    if (-not $Record) { return }
    if ($Record.Exception -and $Record.Exception.Message) {
        Write-Fail $Record.Exception.Message
    } else {
        Write-Fail $Record.ToString()
    }
    $invocation = $Record.InvocationInfo
    if ($invocation -and $invocation.ScriptLineNumber -gt 0) {
        $commandName = ""
        if ($invocation.MyCommand) { $commandName = [string]$invocation.MyCommand.Name }
        Write-Host "[start]   raised by '$commandName' at $($invocation.ScriptName) line $($invocation.ScriptLineNumber)" -ForegroundColor Yellow
    }
    $stackTrace = $Record.ScriptStackTrace
    if (-not [string]::IsNullOrWhiteSpace($stackTrace)) {
        Write-Host "[start]   PowerShell stack:" -ForegroundColor Yellow
        foreach ($stackLine in ($stackTrace -split "`r?`n")) {
            Write-Host "[start]     $stackLine" -ForegroundColor Yellow
        }
    }
}

function Require-EnvPath([string]$Name) {
    # Returns the trimmed value of a required Windows path env var, or throws
    # a diagnosable error. Every Join-Path/New-Item input that originates from
    # the environment is validated here so an unset/empty variable can never
    # surface as PowerShell's cryptic 'Cannot bind argument to parameter
    # "Path" because it is an empty string' binding error.
    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "the required Windows path environment variable '$Name' is empty or unset; run this script from a normal Windows desktop session (set '$Name' and open a new terminal)"
    }
    return $value.Trim()
}

function Get-CommandPath([string]$Name) {
    # Resolve $Name (an exe/cmd on PATH) to an executable file path as a
    # PLAIN STRING, or $null. Windows PowerShell 5.1 and PowerShell 7 return
    # different CommandInfo shapes, so only the always-string members Source /
    # Definition are read (each guarded); callers never touch object-only
    # members such as .Path that do not exist on every CommandInfo. Every
    # external command launch in this script goes through this function.
    $command = Get-Command -Name $Name -ErrorAction SilentlyContinue
    if (-not $command) { return $null }
    $commandPath = $null
    try { $commandPath = [string]$command.Source } catch { $commandPath = $null }
    if ([string]::IsNullOrWhiteSpace($commandPath)) {
        try { $commandPath = [string]$command.Definition } catch { $commandPath = $null }
    }
    if ([string]::IsNullOrWhiteSpace($commandPath)) { return $null }
    return $commandPath.Trim()
}

# ------------------------------------------------------------------ python
function Refresh-PathFromRegistry {
    # Rebuild $env:PATH from the registry (machine + user + current session)
    # so freshly installed tools are visible without opening a new terminal.
    $parts = @()
    foreach ($source in @($env:PATH,
                          [Environment]::GetEnvironmentVariable("Path", "Machine"),
                          [Environment]::GetEnvironmentVariable("Path", "User"))) {
        if ($source) { $parts += $source -split ';' }
    }
    $env:PATH = (($parts | Where-Object { $_ } | Select-Object -Unique) -join ';')
}

function Test-PythonProbe([string]$Exe, [string[]]$Extra) {
    # $true when $Exe runs and reports Python >= 3.8 (rejects store stubs).
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    $probe = "import sys; print('%d.%d' % sys.version_info[:2])"
    try {
        $out = & $Exe @Extra -c $probe 2>$null
        return [bool]($out -match "^\s*3\.([89]|[1-9][0-9])")
    } catch {
        return $false
    }
}

function Get-PythonCommand {
    # Returns @{ Path; Extra = @() | @("-3") } for Python >= 3.8, or $null.
    # Path is always a plain string path (see Get-CommandPath) so callers can
    # hand it straight to Start-Process -FilePath on PS 5.1 and PS 7.
    $pythonCmd = Get-CommandPath "python.exe"
    if ($pythonCmd -and $pythonCmd -notlike "*WindowsApps*" -and
        (Test-PythonProbe $pythonCmd @())) {
        return @{ Path = $pythonCmd; Extra = @() }
    }
    $pyCmd = Get-CommandPath "py.exe"
    if ($pyCmd -and (Test-PythonProbe $pyCmd @("-3"))) {
        return @{ Path = $pyCmd; Extra = @("-3") }
    }
    # per-user python.org installs may not be on PATH yet; probe them directly
    # (optional probe: skipped when LOCALAPPDATA is missing, never Join-Path'd
    # with an empty value)
    $localAppData = [Environment]::GetEnvironmentVariable("LOCALAPPDATA", "Process")
    if (-not [string]::IsNullOrWhiteSpace($localAppData)) {
        $localAppData = $localAppData.Trim()
        $userPython = Join-Path $localAppData "Programs\Python\Python312\python.exe"
        if (Test-PythonProbe $userPython @()) {
            return @{ Path = $userPython; Extra = @() }
        }
        $userLauncher = Join-Path $localAppData "Programs\Python\Launcher\py.exe"
        if (Test-PythonProbe $userLauncher @("-3")) {
            return @{ Path = $userLauncher; Extra = @("-3") }
        }
    }
    return $null
}

function Get-LatestPython312 {
    # Newest published 3.12.x from the official directory listing; falls back
    # to a known-good pinned patch when the listing is unreachable.
    try {
        $page = (Invoke-WebRequest -Uri "https://www.python.org/ftp/python/" `
            -UseBasicParsing -TimeoutSec 20).Content
        $latest = [regex]::Matches($page, 'href="(3\.12\.\d+)/"') |
            ForEach-Object { $_.Groups[1].Value } |
            Sort-Object { [version]$_ } | Select-Object -Last 1
        if ($latest) { return $latest }
    } catch { }
    return "3.12.10"
}

function Ensure-Python {
    $python = Get-PythonCommand
    if ($python) {
        Write-Info "python: $($python.Path)"
        return $python
    }
    if ($SkipAutoInstall) {
        Write-Fail "Python 3.8+ was not found and -SkipAutoInstall is set; install it (winget install -e --id Python.Python.3.12, or https://www.python.org/downloads/) and re-run."
        exit 2
    }
    Write-Info "Python 3.8+ not found; attempting an automatic per-user install (no admin/UAC)"

    # 1) winget, user scope only - never asks for machine-wide elevation
    $winget = Get-CommandPath "winget.exe"
    if ($winget) {
        Write-Info "trying winget: install -e --id Python.Python.3.12 --scope user (silent)"
        & $winget install -e --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) { Refresh-PathFromRegistry }
        $python = Get-PythonCommand
    }

    # 2) official python.org per-user installer (silent, no UAC)
    if (-not $python) {
        $arch = "amd64"
        if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { $arch = "arm64" }
        $version = Get-LatestPython312
        $installerUrl = "https://www.python.org/ftp/python/$version/python-$version-$arch.exe"
        $installer = Join-Path (Require-EnvPath "TEMP") "python-$version-$arch.exe"
        Write-Info "trying the official per-user installer: $installerUrl"
        try {
            Invoke-WebRequest -Uri $installerUrl -OutFile $installer -UseBasicParsing
            $installProc = Start-Process -FilePath $installer `
                -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", `
                    "Include_launcher=1", "Include_test=0" `
                -Wait -PassThru -WindowStyle Hidden
            if ($installProc.ExitCode -ne 0) {
                Write-Fail "python.org installer exited with code $($installProc.ExitCode)"
            }
        } catch {
            Write-Fail "automatic Python install failed; details:"
            Write-FailDetail $_
        } finally {
            Refresh-PathFromRegistry
        }
        $python = Get-PythonCommand
    }

    if ($python) {
        Write-Info "python ready: $($python.Path)"
        return $python
    }
    Write-Fail "Python 3.8+ could not be installed automatically."
    Write-Host "Manual fallback (one time, still no admin):"
    Write-Host "  winget install -e --id Python.Python.3.12"
    Write-Host "or run the installer from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'."
    Write-Host "Then re-run this script."
    exit 2
}

# ------------------------------------------------------------------- codex
function Find-Codex {
    # CODEX_BIN override > %APPDATA%\npm\codex.cmd > codex.exe / codex.cmd
    # on PATH. Returns an absolute path or $null. Env vars are validated
    # before use so an empty value can never reach Join-Path/Test-Path.
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_BIN)) {
        if (Test-Path -LiteralPath $env:CODEX_BIN) { return $env:CODEX_BIN }
        Write-Fail "CODEX_BIN is set but not found: $env:CODEX_BIN"
        exit 3
    }
    $appData = [Environment]::GetEnvironmentVariable("APPDATA", "Process")
    if (-not [string]::IsNullOrWhiteSpace($appData)) {
        $npmShim = Join-Path $appData.Trim() "npm\codex.cmd"
        if (Test-Path -LiteralPath $npmShim) { return $npmShim }
    }
    $native = Get-CommandPath "codex.exe"
    if ($native) { return $native }
    # official native installer default location (may not be on PATH yet)
    $userProfile = [Environment]::GetEnvironmentVariable("USERPROFILE", "Process")
    if (-not [string]::IsNullOrWhiteSpace($userProfile)) {
        $nativeLocal = Join-Path $userProfile.Trim() ".local\bin\codex.exe"
        if (Test-Path -LiteralPath $nativeLocal) { return $nativeLocal }
    }
    $shim = Get-CommandPath "codex.cmd"
    if ($shim) { return $shim }
    return $null
}

function Ensure-Codex {
    $codexBin = Find-Codex
    if ($codexBin) {
        Write-Info "codex: $codexBin"
        return $codexBin
    }
    if (-not $SkipAutoInstall) {
        # 1) npm global install (user prefix %APPDATA%\npm, no admin)
        $npm = Get-CommandPath "npm.cmd"
        if ($npm) {
            Write-Info "codex not found; installing @openai/codex via npm (user prefix, no admin needed)"
            & $npm install -g "@openai/codex"
            if ($LASTEXITCODE -eq 0) {
                $codexBin = Find-Codex
                if ($codexBin) {
                    Write-Info "codex installed: $codexBin"
                    return $codexBin
                }
            }
        }
        # 2) official native Windows installer (user scope, no admin)
        if (-not $codexBin) {
            Write-Info "codex not found via npm; trying the official native Windows installer"
            try {
                powershell.exe -NoProfile -ExecutionPolicy Bypass `
                    -Command "irm https://chatgpt.com/codex/install.ps1 | iex"
                Refresh-PathFromRegistry
            } catch {
                Write-Fail "native codex installer failed: $_"
            }
            $codexBin = Find-Codex
            if ($codexBin) {
                Write-Info "codex installed: $codexBin"
                return $codexBin
            }
        }
    }
    Write-Fail "Codex CLI was not found (set CODEX_BIN or add codex to PATH)."
    Write-Host ""
    Write-Host "Manual fallback:"
    Write-Host "  powershell -ExecutionPolicy ByPass -c `"irm https://chatgpt.com/codex/install.ps1 | iex`""
    Write-Host "or:  npm install -g @openai/codex     (when Node.js is installed)"
    Write-Host "Then re-run this script."
    Write-Host ""
    exit 3
}

# ------------------------------------------------------------------- ngrok
function Get-NgrokExe {
    if (-not [string]::IsNullOrWhiteSpace($env:NGROK_BIN)) {
        if (Test-Path -LiteralPath $env:NGROK_BIN) { return $env:NGROK_BIN }
        Write-Fail "NGROK_BIN is set but not found: $env:NGROK_BIN"
        return $null
    }
    $cmd = Get-CommandPath "ngrok.exe"
    if ($cmd) { return $cmd }
    $localAppData = [Environment]::GetEnvironmentVariable("LOCALAPPDATA", "Process")
    if (-not [string]::IsNullOrWhiteSpace($localAppData)) {
        $local = Join-Path $localAppData.Trim() "ngrok\ngrok.exe"
        if (Test-Path -LiteralPath $local) { return $local }
    }
    return $null
}

function Ensure-Ngrok {
    $ngrok = Get-NgrokExe
    if ($ngrok) {
        Write-Info "ngrok: $ngrok"
        return $ngrok
    }
    if ($SkipAutoInstall) {
        Write-Fail "ngrok not found (set NGROK_BIN or install it from https://ngrok.com/download; -NoNgrok keeps the bridge local-only)"
        exit 6
    }
    Write-Info "ngrok not found; downloading the stable Windows build into %LOCALAPPDATA%\ngrok (no admin needed)"
    $destDir = Join-Path (Require-EnvPath "LOCALAPPDATA") "ngrok"
    $zip = Join-Path (Require-EnvPath "TEMP") "ngrok-v3-stable-windows-amd64.zip"
    try {
        Invoke-WebRequest -Uri "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip" `
            -OutFile $zip -UseBasicParsing
        if (Test-Path -LiteralPath $destDir) {
            Remove-Item -LiteralPath $destDir -Recurse -Force
        }
        Expand-Archive -LiteralPath $zip -DestinationPath $destDir -Force
    } catch {
        Write-Fail "ngrok download/install failed: $_"
        Write-Host "Install ngrok manually (https://ngrok.com/download), then re-run."
        exit 6
    }
    $ngrok = Join-Path $destDir "ngrok.exe"
    if (-not (Test-Path -LiteralPath $ngrok)) {
        Write-Fail "ngrok install finished but ngrok.exe is missing under $destDir"
        exit 6
    }
    Write-Info "ngrok installed: $ngrok"
    return $ngrok
}

# --------------------------------------------------- secrets (DPAPI store)
# All secret files under SecretsDir are DPAPI-protected (per-user
# CryptProtectData), so no plaintext secret ever lands on disk. The DeepSeek
# key and the worker token are decrypted in-memory only for the short window
# in which a child process env is being prepared.
function Initialize-SecretsDir {
    if (-not (Test-Path -LiteralPath $script:SecretsDir)) {
        New-Item -ItemType Directory -Path $script:SecretsDir -Force | Out-Null
    }
}

function Protect-BridgeSecretText([string]$Path, [string]$PlainText) {
    Add-Type -AssemblyName System.Security -ErrorAction Stop
    $scope = [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($PlainText)
    $protected = [System.Security.Cryptography.ProtectedData]::Protect($bytes, $null, $scope)
    [System.IO.File]::WriteAllBytes($Path, $protected)
    $PlainText = $null
}

function Unprotect-BridgeSecretText([string]$Path) {
    # Returns the plain text (callers must null it) or $null when the store
    # is missing / corrupt / belongs to another Windows user.
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        Add-Type -AssemblyName System.Security -ErrorAction Stop
        $scope = [System.Security.Cryptography.DataProtectionScope]::CurrentUser
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        $plainBytes = [System.Security.Cryptography.ProtectedData]::Unprotect($bytes, $null, $scope)
        return [System.Text.Encoding]::UTF8.GetString($plainBytes)
    } catch {
        return $null
    }
}

function Ensure-DeepseekKey {
    # 1) session env (manual override for one run)
    if (-not [string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
        Write-Info "DEEPSEEK_API_KEY: using the session environment (value never printed)"
        return
    }
    # 2) DPAPI store (the normal path after the first masked entry)
    $plain = Unprotect-BridgeSecretText $script:DeepseekSecretFile
    if (-not [string]::IsNullOrWhiteSpace($plain)) {
        $env:DEEPSEEK_API_KEY = $plain
        $plain = $null
        Write-Info "DEEPSEEK_API_KEY: loaded from the DPAPI store (value never printed)"
        return
    }
    # 3) legacy plaintext USER env (pre-dual-host bootstrap): migrate it into
    #    DPAPI once and tell the user how to delete the plaintext copy
    $userKey = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
    if (-not [string]::IsNullOrWhiteSpace($userKey)) {
        Initialize-SecretsDir
        Protect-BridgeSecretText $script:DeepseekSecretFile $userKey
        $env:DEEPSEEK_API_KEY = $userKey
        $userKey = $null
        Write-Info "DEEPSEEK_API_KEY: migrated the legacy user-environment copy into the DPAPI store (value never printed)"
        Write-Host "Remove the old plaintext copy yourself (never auto-deleted):  setx DEEPSEEK_API_KEY `"`""
        return
    }
    # 4) masked one-time prompt -> DPAPI (interactive terminals only)
    $canPrompt = -not [Console]::IsInputRedirected
    if ($canPrompt) {
        Write-Info "DEEPSEEK_API_KEY is not set. Paste the DeepSeek key once (masked); it is encrypted with"
        Write-Info "Windows DPAPI into $($script:DeepseekSecretFile) and only injected into the bridge child process:"
        $secure = Read-Host "DeepSeek API key" -AsSecureString
        if ($secure -and $secure.Length -gt 0) {
            $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            $plain = $null
            try {
                $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            } finally {
                [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            }
            if (-not [string]::IsNullOrWhiteSpace($plain)) {
                Initialize-SecretsDir
                Protect-BridgeSecretText $script:DeepseekSecretFile $plain
                $env:DEEPSEEK_API_KEY = $plain
                $plain = $null
                Write-Info "DEEPSEEK_API_KEY accepted and stored with Windows DPAPI (value never printed)"
                return
            }
        }
    }
    Write-Fail "DEEPSEEK_API_KEY is not set (no session env, no DPAPI store and no interactive prompt available)."
    Write-Host "The key value is never printed and auth.json is never read; re-run in an interactive"
    Write-Host "terminal, or provide it for this session only:  `$env:DEEPSEEK_API_KEY='<your-deepseek-key>'"
    Exit-With 4
}

function Ensure-SecretEnvironment {
    # API key for the LOCAL HTTP endpoints (.bridge_api_key, gitignored).
    # In dual-host mode each machine may keep its own key: the worker calls
    # the LOCAL bridge and the GPT never talks to Windows directly, so the
    # Windows key does not need to match the Mac's (it may, if you prefer).
    if (-not (Test-Path -LiteralPath $script:KeyFile)) {
        $bytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $hex = -join ($bytes | ForEach-Object { $_.ToString("x2") })
        [System.IO.File]::WriteAllText($script:KeyFile, $hex)
        Write-Info "generated a new local API key file: $($script:KeyFile) (gitignored; content never printed)"
        Write-Host "Dual-host mode: the GPT talks to the Mac router only, so this local key stays on Windows."
        Write-Host "Legacy -NgrokCutover mode: replace it with the Mac's .bridge_api_key content so both share one key."
    } else {
        $content = [System.IO.File]::ReadAllText($script:KeyFile)
        if ([string]::IsNullOrWhiteSpace($content)) {
            Exit-With 4 "the API key file exists but is empty: $($script:KeyFile) (delete it and re-run)"
        }
    }
    Ensure-DeepseekKey
}

# Env for the spawned bridge/worker children: set before Start-Process,
# restored after so the caller's session is not left with any of these set.
$script:EnvKeys = @("BRIDGE_API_KEY", "BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT",
    "BRIDGE_PORT", "BRIDGE_SANDBOX_MODE", "BRIDGE_APPROVAL_POLICY",
    "BRIDGE_NETWORK_ACCESS", "CODEX_HOME", "CODEX_BIN", "PYTHONUTF8",
    "DEEPSEEK_API_KEY", "BRIDGE_WORKER_TOKEN", "MAC_BRIDGE_URL")

function Save-EnvSnapshot {
    $script:EnvBackup = @{}
    foreach ($key in $script:EnvKeys) {
        $script:EnvBackup[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
    }
}

function Restore-Env {
    if (-not $script:EnvBackup) { return }
    foreach ($key in $script:EnvKeys) {
        $value = $script:EnvBackup[$key]
        if ($null -eq $value) {
            [Environment]::SetEnvironmentVariable($key, $null, "Process")
        } else {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

# ------------------------------------------------------- pid / process guard
function Get-PidIfManaged([string]$PidFile, [string]$CommandLineMatch) {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $pidText = [System.IO.File]::ReadAllText($PidFile).Trim()
    $pidValue = 0
    if (-not [int]::TryParse($pidText, [ref]$pidValue)) { return $null }
    if ($pidValue -le 0) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    if ($proc.CommandLine -and $proc.CommandLine -like "*$CommandLineMatch*") {
        return $pidValue
    }
    return $null
}

function Remove-PidFile([string]$PidFile) {
    if (Test-Path -LiteralPath $PidFile) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
}

function Stop-PidIfManaged([string]$PidFile, [string]$CommandLineMatch, [string]$Label) {
    $managed = Get-PidIfManaged $PidFile $CommandLineMatch
    if ($managed) {
        Write-Info "stopping $Label (pid $managed)"
        Stop-Process -Id $managed -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
        if (Get-Process -Id $managed -ErrorAction SilentlyContinue) {
            Start-Sleep -Seconds 2
        }
    }
    Remove-PidFile $PidFile
}

function Stop-StartedChildren {
    if ($script:StartedWorkerNow) {
        Stop-Process -Id $script:WorkerPid -Force -ErrorAction SilentlyContinue
        Remove-PidFile $script:WorkerPidFile
        $script:StartedWorkerNow = $false
    }
    if ($script:StartedNgrokNow) {
        Stop-Process -Id $script:NgrokPid -Force -ErrorAction SilentlyContinue
        Remove-PidFile $script:NgrokPidFile
        $script:StartedNgrokNow = $false
    }
    if ($script:StartedBridgeNow) {
        Stop-Process -Id $script:BridgePid -Force -ErrorAction SilentlyContinue
        Remove-PidFile $script:BridgePidFile
        $script:StartedBridgeNow = $false
    }
    Restore-Env
}

# ------------------------------------------------------------------- layout
function Initialize-Layout {
    # All layout paths are derived HERE - after the environment sanity check
    # at the top of main - and never at load time. Every input is validated
    # (optional overrides are trimmed only when non-empty; the %LOCALAPPDATA%
    # fallback is required) so an empty string can never reach Join-Path.
    $stateOverride = [Environment]::GetEnvironmentVariable("BRIDGE_STATE_ROOT", "Process")
    $xdgStateHome = [Environment]::GetEnvironmentVariable("XDG_STATE_HOME", "Process")
    if (-not [string]::IsNullOrWhiteSpace($stateOverride)) {
        $script:StateRootBase = $stateOverride.Trim()
    } elseif (-not [string]::IsNullOrWhiteSpace($xdgStateHome)) {
        $script:StateRootBase = Join-Path $xdgStateHome.Trim() "local-codex-bridge"
    } else {
        $script:StateRootBase = Join-Path (Require-EnvPath "LOCALAPPDATA") "local-codex-bridge"
    }
    if ([string]::IsNullOrWhiteSpace($script:StateRootBase)) {
        throw "the bridge state root resolved to an empty path (BRIDGE_STATE_ROOT/XDG_STATE_HOME/LOCALAPPDATA are all unset or empty)"
    }
    $script:InstanceDir = Join-Path $script:StateRootBase $script:Instance
    $script:RuntimeDir = Join-Path $script:InstanceDir "runtime"
    # Dedicated DeepSeek profile for the Bridge app-server + codex-deepseek.cmd
    # (the Desktop %USERPROFILE%\.codex stays OpenAI; see platform_paths.py).
    $script:DeepseekCodexHome = Join-Path $script:StateRootBase "codex-deepseek"
    # DPAPI-protected secrets (never plaintext): DeepSeek key + worker token.
    $script:SecretsDir = Join-Path $script:StateRootBase "secrets"
    $script:DeepseekSecretFile = Join-Path $script:SecretsDir "deepseek.key.dpapi"
    $script:WorkerTokenFile = Join-Path $script:SecretsDir "worker.token.dpapi"

    # pid/log/instance files: built from the dirs above, never from the ""
    # load-time placeholders (joining those used to abort every run with a
    # 'parameter "Path" is an empty string' binding error).
    $script:BridgePidFile = Join-Path $script:RuntimeDir "bridge.pid"
    $script:NgrokPidFile = Join-Path $script:RuntimeDir "ngrok.pid"
    $script:WorkerPidFile = Join-Path $script:RuntimeDir "worker.pid"
    $script:BridgeLog = Join-Path $script:RuntimeDir "bridge.log"
    $script:BridgeOutLog = Join-Path $script:RuntimeDir "bridge.out.log"
    $script:BridgeErrLog = Join-Path $script:RuntimeDir "bridge.err.log"
    $script:NgrokLog = Join-Path $script:RuntimeDir "ngrok.log"
    $script:NgrokOutLog = Join-Path $script:RuntimeDir "ngrok.out.log"
    $script:NgrokErrLog = Join-Path $script:RuntimeDir "ngrok.err.log"
    $script:WorkerLog = Join-Path $script:RuntimeDir "worker.log"
    $script:WorkerOutLog = Join-Path $script:RuntimeDir "worker.out.log"
    $script:WorkerErrLog = Join-Path $script:RuntimeDir "worker.err.log"
    $script:WorkerStateFile = Join-Path $script:RuntimeDir "worker.state.json"
    $script:InstanceJson = Join-Path $script:InstanceDir "instance.json"
}

# ------------------------------------------------------------------- bridge
function Test-PortBusy {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $client.Connect($script:HostAddr, $script:Port)
            return $true
        } finally {
            $client.Close()
        }
    } catch {
        return $false
    }
}

function Get-HealthJson([string]$Path) {
    try {
        return Invoke-RestMethod -Uri "$($script:BaseUrl)$Path" -Method Get -TimeoutSec 5
    } catch {
        return $null
    }
}

function Set-BridgeProcessEnvironment {
    $env:BRIDGE_API_KEY = [System.IO.File]::ReadAllText($script:KeyFile)
    $env:BRIDGE_INSTANCE = $script:Instance
    $env:BRIDGE_STATE_ROOT = $script:StateRootBase
    $env:BRIDGE_PORT = "$($script:Port)"
    $env:BRIDGE_SANDBOX_MODE = $script:EffectiveMode
    $env:BRIDGE_APPROVAL_POLICY = $script:EffectivePolicy
    $env:BRIDGE_NETWORK_ACCESS = $script:EffectiveNetwork
    $env:CODEX_HOME = $script:CodexHome
    $env:CODEX_BIN = $script:CodexBin
    $env:PYTHONUTF8 = "1"
}

function Ensure-Bridge {
    $managed = Get-PidIfManaged $script:BridgePidFile "http_server"
    if ($managed) {
        Write-Info "bridge already running (pid $managed); reusing it (sandbox env changes need a stop/start)"
        return
    }
    Remove-PidFile $script:BridgePidFile
    if (Test-PortBusy) {
        Exit-With 7 "port $($script:Port) on $($script:HostAddr) is already in use by an unmanaged process; not touching it (stop it manually)"
    }
    Save-EnvSnapshot
    Set-BridgeProcessEnvironment
    $bridgeArgs = @("-m", "http_server", "--host", $script:HostAddr,
        "--port", "$($script:Port)",
        "--codex-bin", "`"$script:CodexBin`"",
        "--codex-home", "`"$script:CodexHome`"",
        "--log", "`"$script:BridgeLog`"")
    Write-Info "starting bridge: python -m http_server on $($script:BaseUrl) (codex home: $script:CodexHome)"
    # $python.Path is our own plain-string descriptor (built by
    # Get-CommandPath); Start-Process receives a [string] and no .Path is ever
    # read back off the returned Process object. On Windows PowerShell 5.1 a
    # child that exits before -PassThru returns its wrapper surfaces as a
    # spurious "Property 'Path' cannot be found on this object" error (PS
    # 7.2.8+ instead returns an exited Process); both shapes are handled the
    # same way below - never write a pid file for a process that is gone.
    $pythonPath = [string]$python.Path
    $proc = $null
    try {
        $proc = Start-Process -FilePath $pythonPath -ArgumentList (@($python.Extra) + $bridgeArgs) `
            -WorkingDirectory $script:RepoRoot -WindowStyle Hidden `
            -RedirectStandardOutput $script:BridgeOutLog -RedirectStandardError $script:BridgeErrLog `
            -PassThru
    } catch {
        $startError = $_
        Write-Fail "the bridge process failed to start (a child that exits immediately surfaces on Windows PowerShell 5.1 as a spurious 'property Path not found' error from Start-Process)"
        Show-BridgeLogTail
        Write-FailDetail $startError
        Exit-With 5
    }
    if ($proc.HasExited) {
        Show-BridgeLogTail
        Exit-With 5 "the bridge process exited immediately (exit code: $($proc.ExitCode)); see the log tails above"
    }
    Restore-Env
    [System.IO.File]::WriteAllText($script:BridgePidFile, "$($proc.Id)")
    $script:StartedBridgeNow = $true
    $script:BridgePid = $proc.Id
}

function Wait-LocalHealth {
    # Requirement: verify BOTH /health and /ready locally before ngrok.
    $deadline = (Get-Date).AddSeconds(120)
    $health = $null
    while ((Get-Date) -lt $deadline) {
        $health = Get-HealthJson "/health"
        if ($health -and $health.ready -eq $true -and $health.status -eq "ok") { break }
        if ($script:StartedBridgeNow) {
            $bridgeProc = Get-Process -Id $script:BridgePid -ErrorAction SilentlyContinue
            if (-not $bridgeProc -and -not ([string]$python.Path -like "*py.exe")) { break }
        }
        Start-Sleep -Seconds 1
    }
    if (-not ($health -and $health.ready -eq $true)) {
        Write-Fail "local /health did not become ready on $($script:BaseUrl)/health within 120s"
        Show-BridgeLogTail
        Exit-With 5
    }
    Write-Info "local health OK: $($script:BaseUrl)/health (instance=$($health.instance) mode=$($health.mode))"

    $deadline = (Get-Date).AddSeconds(30)
    $ready = $null
    while ((Get-Date) -lt $deadline) {
        $ready = Get-HealthJson "/ready"
        if ($ready -and $ready.ready -eq $true) { break }
        Start-Sleep -Seconds 1
    }
    if (-not ($ready -and $ready.ready -eq $true)) {
        Write-Fail "local /ready did not become ready on $($script:BaseUrl)/ready"
        Show-BridgeLogTail
        Exit-With 5
    }
    Write-Info "local ready OK: $($script:BaseUrl)/ready"
}

function Show-BridgeLogTail {
    foreach ($log in @($script:BridgeOutLog, $script:BridgeErrLog, $script:BridgeLog)) {
        if (Test-Path -LiteralPath $log) {
            Write-Host "---- tail of $log ----"
            Get-Content -LiteralPath $log -Tail 10 -ErrorAction SilentlyContinue
        }
    }
    Write-Host ""
    Write-Host "Readiness needs: DEEPSEEK_API_KEY (DPAPI store or session env, never printed) and a"
    Write-Host "DeepSeek provider config under $script:CodexHome (auto-created/migrated by this script)."
    Write-Host "In bridge-workspace mode the config must not carry legacy sandbox keys; the"
    Write-Host "default workspace-write mode needs no migration."
}

function Write-InstanceJson {
    $info = [ordered]@{
        name            = $script:Instance
        mode            = $script:EffectiveMode
        approval_policy = $script:EffectivePolicy
        network_access  = $script:EffectiveNetwork
        host            = $script:HostAddr
        port            = $script:Port
        codex_home      = $script:CodexHome
        codex_bin       = $script:CodexBin
        work_root       = $script:WorkRoot
        state_root      = $script:StateRootBase
        secrets_dir     = $script:SecretsDir
        deepseek_codex_home = $script:DeepseekCodexHome
        desktop_codex_home  = (Join-Path (Require-EnvPath "USERPROFILE") ".codex")
        runtime_dir     = $script:RuntimeDir
        bridge_log      = $script:BridgeLog
        ngrok_log       = $script:NgrokLog
        worker_log      = $script:WorkerLog
        mac_bridge_url  = $script:MacBridgeUrl
        created_at      = (Get-Date).ToString("o")
        script          = $script:Version
    }
    $info | ConvertTo-Json | Set-Content -LiteralPath $script:InstanceJson -Encoding UTF8
}

# --------------------------------------------- Desktop/Bridge separation
function Test-ConfigMarkedDeepseek([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $text = [System.IO.File]::ReadAllText($Path)
        return ($text -match "(?i)deepseek")
    } catch {
        return $false
    }
}

function Write-MinimalOpenAiDesktopConfig([string]$Path) {
    # Minimal OpenAI Desktop config, written ONLY when the Desktop config was
    # DeepSeek-marked (and no safe backup exists) or absent. auth.json, state
    # and history are never read or touched.
    $text = @"
# Restored by the Local Codex Bridge bootstrap (dual-host-router).
# Desktop Codex = OpenAI. DeepSeek lives in the dedicated bridge CODEX_HOME
# (%LOCALAPPDATA%\local-codex-bridge\codex-deepseek).
model = "gpt-5.6-sol"
model_provider = "openai"
approval_policy = "on-request"
"@
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    [System.IO.File]::WriteAllText($Path, $text)
    Write-Info "wrote a minimal OpenAI Desktop config to $Path (auth.json/state/history untouched)"
}

function Write-DeepseekBridgeConfig([string]$Path) {
    $text = @"
# Dedicated DeepSeek profile for the Bridge app-server and codex-deepseek.cmd.
# Desktop %USERPROFILE%\.codex stays OpenAI. The key is env_key-referenced
# only (DEEPSEEK_API_KEY); the value lives in the DPAPI secret store.
model = "deepseek-chat"
model_provider = "deepseek"
model_reasoning_effort = "max"
approval_policy = "on-request"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
env_key = "DEEPSEEK_API_KEY"
wire_api = "responses"
"@
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    [System.IO.File]::WriteAllText($Path, $text)
}

function Invoke-HomeSeparationPhase {
    # Desktop OpenAI / Bridge DeepSeek: restore %USERPROFILE%\.codex to OpenAI
    # and move any DeepSeek profile into the dedicated bridge CODEX_HOME.
    $desktopHome = Join-Path (Require-EnvPath "USERPROFILE") ".codex"
    $desktopCfg = Join-Path $desktopHome "config.toml"
    $backupCfg = Join-Path $desktopHome "backup-deepseek\config.toml"
    $bridgeHome = $script:DeepseekCodexHome
    $bridgeCfg = Join-Path $bridgeHome "config.toml"

    $desktopDeepseek = Test-ConfigMarkedDeepseek $desktopCfg
    if ($desktopDeepseek) {
        # 1) migrate the DeepSeek profile (config files only; auth.json,
        #    state/ and history/ are never read or copied) into the bridge home
        if (-not (Test-Path -LiteralPath $bridgeCfg)) {
            New-Item -ItemType Directory -Force -Path $bridgeHome | Out-Null
            Copy-Item -LiteralPath $desktopCfg -Destination $bridgeCfg -Force
            foreach ($sub in @("config", "providers")) {
                $src = Join-Path $desktopHome $sub
                if (Test-Path -LiteralPath $src) {
                    $dst = Join-Path $bridgeHome $sub
                    New-Item -ItemType Directory -Force -Path $dst | Out-Null
                    Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
                }
            }
            Write-Info "migrated the DeepSeek profile from $desktopCfg into the dedicated bridge CODEX_HOME $bridgeHome"
        }
        # 2) restore the Desktop OpenAI config from the official DeepSeek backup
        if ((Test-Path -LiteralPath $backupCfg) -and -not (Test-ConfigMarkedDeepseek $backupCfg)) {
            Copy-Item -LiteralPath $backupCfg -Destination $desktopCfg -Force
            Write-Info "Desktop restored to OpenAI from $backupCfg (auth.json/state/history untouched)"
        } else {
            Write-MinimalOpenAiDesktopConfig $desktopCfg
        }
    } elseif (-not (Test-Path -LiteralPath $desktopCfg)) {
        if ((Test-Path -LiteralPath $backupCfg) -and -not (Test-ConfigMarkedDeepseek $backupCfg)) {
            New-Item -ItemType Directory -Force -Path $desktopHome | Out-Null
            Copy-Item -LiteralPath $backupCfg -Destination $desktopCfg -Force
            Write-Info "Desktop config restored to OpenAI from $backupCfg (auth.json/state/history untouched)"
        } else {
            Write-MinimalOpenAiDesktopConfig $desktopCfg
        }
    } else {
        Write-Info "Desktop config %USERPROFILE%\.codex is present and not DeepSeek-marked; left untouched (Desktop stays OpenAI/its own setup)"
    }
    # 3) the dedicated DeepSeek bridge home must always exist for the app-server
    if (-not (Test-Path -LiteralPath $bridgeCfg)) {
        Write-DeepseekBridgeConfig $bridgeCfg
        Write-Info "created the dedicated DeepSeek CODEX_HOME config: $bridgeCfg"
    }
}

function Invoke-OpenaiBaseUrlGuard {
    # A USER-level OPENAI_BASE_URL that points at DeepSeek would silently
    # redirect the Desktop's OpenAI provider. Remove it ONLY when it is
    # confirmed DeepSeek; normal proxy/base-url setups are kept untouched.
    $userValue = [Environment]::GetEnvironmentVariable("OPENAI_BASE_URL", "User")
    if ([string]::IsNullOrWhiteSpace($userValue)) { return }
    if ($userValue -match "(?i)deepseek") {
        [Environment]::SetEnvironmentVariable("OPENAI_BASE_URL", $null, "User")
        Write-Info "removed the USER-level OPENAI_BASE_URL that pointed at DeepSeek (Desktop must use OpenAI's own endpoint; the value is never printed)"
    } else {
        Write-Info "note: USER-level OPENAI_BASE_URL is set but does not point at DeepSeek; kept untouched (normal proxy/base-url setup)"
    }
}

function Install-DeepseekCliWrapper {
    $wrapperSrc = Join-Path $script:RepoRoot "scripts\windows\codex-deepseek.cmd"
    if (-not (Test-Path -LiteralPath $wrapperSrc)) {
        Write-Info "note: scripts\windows\codex-deepseek.cmd not found in this repo; skipping wrapper install"
        return
    }
    $cmd = Get-CommandPath "codex.cmd"
    if (-not $cmd) { $cmd = Get-CommandPath "codex.exe" }
    if (-not $cmd) {
        $userProfile = Require-EnvPath "USERPROFILE"
        $native = Join-Path $userProfile ".local\bin\codex.exe"
        if (Test-Path -LiteralPath $native) { $cmd = $native }
    }
    if (-not $cmd) {
        Write-Info "note: codex CLI not found yet; codex-deepseek.cmd stays available in the repo (scripts\windows\)"
        return
    }
    $destDir = Split-Path -Parent $cmd
    $dest = Join-Path $destDir "codex-deepseek.cmd"
    try {
        Copy-Item -LiteralPath $wrapperSrc -Destination $dest -Force
        Write-Info "codex-deepseek.cmd installed next to codex: $dest (temporarily sets CODEX_HOME to the DeepSeek profile; Desktop codex unchanged)"
    } catch {
        Write-Info "note: cannot write codex-deepseek.cmd next to codex; use the repo copy scripts\windows\codex-deepseek.cmd instead"
    }
}

# ------------------------------------------------------- outbound worker
function Resolve-MacBridgeUrl {
    if (-not [string]::IsNullOrWhiteSpace($MacBridgeUrl)) { return $MacBridgeUrl.Trim() }
    $envUrl = [Environment]::GetEnvironmentVariable("MAC_BRIDGE_URL", "Process")
    if (-not [string]::IsNullOrWhiteSpace($envUrl)) { return $envUrl.Trim() }
    $userUrl = [Environment]::GetEnvironmentVariable("MAC_BRIDGE_URL", "User")
    if (-not [string]::IsNullOrWhiteSpace($userUrl)) { return $userUrl.Trim() }
    return $script:MacBridgeUrlDefault
}

function Ensure-WorkerToken {
    # Worker token = the Mac router's BRIDGE_WORKER_TOKEN (independent of the
    # bridge API keys). Resolution: -WorkerToken > session env > DPAPI store >
    # masked one-time prompt -> DPAPI. Returns the token or "" (skip worker).
    if (-not [string]::IsNullOrWhiteSpace($script:WorkerTokenValue)) {
        return $script:WorkerTokenValue
    }
    if (-not [string]::IsNullOrWhiteSpace($WorkerToken)) {
        $script:WorkerTokenValue = $WorkerToken.Trim()
        return $script:WorkerTokenValue
    }
    $envToken = [Environment]::GetEnvironmentVariable("BRIDGE_WORKER_TOKEN", "Process")
    if (-not [string]::IsNullOrWhiteSpace($envToken)) {
        $script:WorkerTokenValue = $envToken.Trim()
        return $script:WorkerTokenValue
    }
    $plain = Unprotect-BridgeSecretText $script:WorkerTokenFile
    if (-not [string]::IsNullOrWhiteSpace($plain)) {
        $script:WorkerTokenValue = $plain
        $plain = $null
        Write-Info "worker token: loaded from the DPAPI store (value never printed)"
        return $script:WorkerTokenValue
    }
    if (-not [Console]::IsInputRedirected) {
        Write-Info "worker token is not set. Paste the Mac router's BRIDGE_WORKER_TOKEN once (masked); it is"
        Write-Info "DPAPI-encrypted into $($script:WorkerTokenFile) and only sent to the Mac router:"
        $secure = Read-Host "Mac router worker token" -AsSecureString
        if ($secure -and $secure.Length -gt 0) {
            $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            $plain = $null
            try {
                $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            } finally {
                [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            }
            if (-not [string]::IsNullOrWhiteSpace($plain)) {
                Initialize-SecretsDir
                Protect-BridgeSecretText $script:WorkerTokenFile $plain
                $script:WorkerTokenValue = $plain
                $plain = $null
                Write-Info "worker token accepted and stored with Windows DPAPI (value never printed)"
                return $script:WorkerTokenValue
            }
        }
    }
    return ""
}

function Set-WorkerProcessEnvironment {
    $env:BRIDGE_WORKER_TOKEN = $script:WorkerTokenValue
    $env:MAC_BRIDGE_URL = $script:MacBridgeUrl
    $env:PYTHONUTF8 = "1"
}

function Ensure-Worker {
    $managed = Get-PidIfManaged $script:WorkerPidFile "bridge.worker"
    if ($managed) {
        Write-Info "worker already running (pid $managed); reusing it (router: $($script:MacBridgeUrl))"
        return
    }
    Remove-PidFile $script:WorkerPidFile
    Save-EnvSnapshot
    Set-WorkerProcessEnvironment
    $workerArgs = @("-m", "bridge.worker",
        "--router-url", "`"$script:MacBridgeUrl`"",
        "--local-url", "`"$script:BaseUrl`"",
        "--local-api-key-file", "`"$script:KeyFile`"",
        "--poll-timeout-s", "15",
        "--state-file", "`"$script:WorkerStateFile`"")
    Write-Info "starting worker: python -m bridge.worker -> $($script:MacBridgeUrl) (outbound only; local bridge $($script:BaseUrl))"
    $pythonPath = [string]$python.Path
    $proc = $null
    try {
        $proc = Start-Process -FilePath $pythonPath -ArgumentList (@($python.Extra) + $workerArgs) `
            -WorkingDirectory $script:RepoRoot -WindowStyle Hidden `
            -RedirectStandardOutput $script:WorkerOutLog -RedirectStandardError $script:WorkerErrLog `
            -PassThru
    } catch {
        $startError = $_
        Write-Fail "the worker process failed to start (a child that exits immediately surfaces on Windows PowerShell 5.1 as a spurious 'property Path not found' error from Start-Process)"
        Show-WorkerLogTail
        Write-FailDetail $startError
        Exit-With 5
    }
    if ($proc.HasExited) {
        Show-WorkerLogTail
        Exit-With 5 "the worker process exited immediately (exit code: $($proc.ExitCode)); see the log tails above"
    }
    Restore-Env
    [System.IO.File]::WriteAllText($script:WorkerPidFile, "$($proc.Id)")
    $script:StartedWorkerNow = $true
    $script:WorkerPid = $proc.Id
}

function Show-WorkerLogTail {
    foreach ($log in @($script:WorkerOutLog, $script:WorkerErrLog, $script:WorkerLog)) {
        if (Test-Path -LiteralPath $log) {
            Write-Host "---- tail of $log ----"
            Get-Content -LiteralPath $log -Tail 12 -ErrorAction SilentlyContinue
        }
    }
    Write-Host ""
    Write-Host "Common causes:"
    Write-Host "  * the Mac bridge does not run with BRIDGE_DUAL_HOST=true + BRIDGE_WORKER_TOKEN set"
    Write-Host "  * the worker token does not match the Mac router's BRIDGE_WORKER_TOKEN"
    Write-Host "  * the Mac public URL is unreachable from this network (outbound https only)"
}

function Wait-WorkerConnected {
    # First successful long-poll writes worker.state.json. The local bridge
    # stays up even when the Mac router is unreachable (worker retries in the
    # background), so a timeout here is a warning, not a failure.
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $script:WorkerStateFile) { return $true }
        if ($script:StartedWorkerNow) {
            $proc = Get-Process -Id $script:WorkerPid -ErrorAction SilentlyContinue
            if (-not $proc) { break }
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Invoke-WorkerPhase {
    $script:MacBridgeUrl = Resolve-MacBridgeUrl
    $token = Ensure-WorkerToken
    if ([string]::IsNullOrWhiteSpace($token)) {
        Write-Info "worker token unavailable (non-interactive run): dual-host worker NOT started; the local bridge stays up"
        Write-Host "Provide it next time with  -WorkerToken <token>  (or paste it once in an interactive terminal;"
        Write-Host "it is stored DPAPI-encrypted, never in plain text)"
        return
    }
    Ensure-Worker
    if (Wait-WorkerConnected) {
        Write-Info "worker connected to the Mac router: $($script:MacBridgeUrl) (pid $((Get-Content -LiteralPath $script:WorkerPidFile).Trim()))"
    } else {
        Write-Info "worker still connecting to $($script:MacBridgeUrl) (Mac router offline or token mismatch?); the local bridge stays up and the worker retries in the background"
        Show-WorkerLogTail
    }
    Write-InstanceJson
}

function Invoke-BridgePhase {
    New-Item -ItemType Directory -Force -Path $script:RuntimeDir | Out-Null
    New-Item -ItemType Directory -Force -Path $script:InstanceDir | Out-Null
    try {
        New-Item -ItemType Directory -Force -Path $script:WorkRoot | Out-Null
    } catch {
        Exit-With 1 "cannot create the work root $script:WorkRoot ($_); pass -WorkRoot <existing-dir> or set BRIDGE_WORK_ROOT if this drive does not exist"
    }
    Write-Info "work root ready: $($script:WorkRoot)"

    $mode = $env:BRIDGE_SANDBOX_MODE
    if (-not $mode) { $mode = "workspace-write" }
    if ($mode -notin @("workspace-write", "bridge-workspace", "danger-full-access")) {
        Exit-With 1 "BRIDGE_SANDBOX_MODE=$mode is invalid (use workspace-write|bridge-workspace|danger-full-access)"
    }
    $script:EffectiveMode = $mode
    if ($env:BRIDGE_APPROVAL_POLICY) { $script:EffectivePolicy = $env:BRIDGE_APPROVAL_POLICY }
    if ($script:EffectivePolicy -notin @("on-request", "never")) {
        Exit-With 1 "BRIDGE_APPROVAL_POLICY=$script:EffectivePolicy is invalid (use on-request|never)"
    }
    if ($env:BRIDGE_NETWORK_ACCESS) {
        $script:EffectiveNetwork = $env:BRIDGE_NETWORK_ACCESS
        if ($script:EffectiveNetwork -notin @("true", "false", "1", "0", "yes", "no", "on", "off")) {
            Exit-With 1 "BRIDGE_NETWORK_ACCESS=$script:EffectiveNetwork is invalid (use true|false)"
        }
    }

    # CODEX_HOME is resolved before the secret check so its hints can name
    # the exact config path: param -CodexHome > $env:CODEX_HOME > the
    # dedicated DeepSeek profile %LOCALAPPDATA%\local-codex-bridge\
    # codex-deepseek (Desktop %USERPROFILE%\.codex always stays OpenAI).
    if ([string]::IsNullOrWhiteSpace($script:CodexHome)) {
        $codexHomeEnv = [Environment]::GetEnvironmentVariable("CODEX_HOME", "Process")
        if (-not [string]::IsNullOrWhiteSpace($codexHomeEnv)) {
            $script:CodexHome = $codexHomeEnv.Trim()
        } else {
            $script:CodexHome = $script:DeepseekCodexHome
        }
    }
    if ([string]::IsNullOrWhiteSpace($script:CodexHome)) {
        throw "CODEX_HOME is empty; pass -CodexHome <dir> or set the CODEX_HOME environment variable and re-run"
    }
    # Desktop OpenAI / Bridge DeepSeek separation (idempotent; pure config
    # file ops - auth.json/state/history are never read or touched).
    Invoke-HomeSeparationPhase
    Ensure-SecretEnvironment
    $script:CodexBin = Ensure-Codex
    Install-DeepseekCliWrapper
    $configCheck = Join-Path $script:CodexHome "config.toml"
    if (-not (Test-Path -LiteralPath $configCheck)) {
        Write-Info "note: $configCheck does not exist; the app-server cannot start until a DeepSeek provider config exists under $script:CodexHome"
    }
    Invoke-OpenaiBaseUrlGuard
    Ensure-Bridge
    Wait-LocalHealth
    Write-InstanceJson
}

# ------------------------------------------------------------------- ngrok
function Resolve-TunnelDomain {
    if (-not [string]::IsNullOrWhiteSpace($Domain)) { return $Domain.Trim() }
    if (-not [string]::IsNullOrWhiteSpace($env:NGROK_DOMAIN)) { return $env:NGROK_DOMAIN.Trim() }
    return $script:FixedDomainDefault
}

function Test-NgrokConfigOk([string]$NgrokExe) {
    try {
        & $NgrokExe config check *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Get-TunnelsJson {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -Method Get -TimeoutSec 3
    } catch {
        return $null
    }
}

function Test-LocalNgrokServesDomain([string]$Domain) {
    $tunnels = Get-TunnelsJson
    if (-not $tunnels) { return $false }
    foreach ($tunnel in $tunnels.tunnels) {
        if ($tunnel.public_url -and $tunnel.public_url -like "*$Domain*") { return $true }
    }
    return $false
}

function Show-NgrokLogTail {
    foreach ($log in @($script:NgrokOutLog, $script:NgrokErrLog, $script:NgrokLog)) {
        if (Test-Path -LiteralPath $log) {
            Write-Host "---- tail of $log ----"
            Get-Content -LiteralPath $log -Tail 12 -ErrorAction SilentlyContinue
        }
    }
    Write-Host ""
    Write-Host "Common causes:"
    Write-Host "  * a fixed ngrok domain can only be served by ONE machine at a time; if the"
    Write-Host "    macOS bridge still runs, stop it first (./scripts/stop_ngrok_bridge.sh on"
    Write-Host "    the Mac), then re-run this script."
    Write-Host "  * the ngrok account does not own this fixed domain: run"
    Write-Host "    'ngrok config add-authtoken <token>' with the account that reserved it."
    Write-Host "  * outbound network is blocked (proxy/firewall)."
}

function Invoke-NgrokPhase {
    $tunnelDomain = Resolve-TunnelDomain
    Write-Info "tunnel domain: https://$tunnelDomain (one machine at a time; see README cutover steps)"

    $ngrokExe = Ensure-Ngrok
    if (-not [string]::IsNullOrWhiteSpace($env:NGROK_AUTHTOKEN)) {
        Write-Info "ngrok authtoken: NGROK_AUTHTOKEN present (never printed)"
    } elseif (Test-NgrokConfigOk $ngrokExe) {
        Write-Info "ngrok authtoken: existing ngrok config is valid (checked via 'ngrok config check', never read/printed)"
    } else {
        Write-Fail "no ngrok authtoken available (neither NGROK_AUTHTOKEN nor a valid ngrok config)."
        Write-Host ""
        Write-Host "Minimal fix - run once yourself (the token is never handled by this script):"
        Write-Host "  ngrok config add-authtoken <your-ngrok-authtoken>"
        Write-Host "or set the NGROK_AUTHTOKEN user environment variable, then re-run."
        Write-Host ""
        exit 6
    }

    $managedNgrok = Get-PidIfManaged $script:NgrokPidFile "ngrok"
    if ($managedNgrok) {
        Write-Info "ngrok already running (pid $managedNgrok); reusing it"
        return
    }
    Remove-PidFile $script:NgrokPidFile
    if (Test-LocalNgrokServesDomain $tunnelDomain) {
        Exit-With 7 "an unmanaged local ngrok instance already serves https://$tunnelDomain on this machine; not touching it"
    }

    Write-Info "starting ngrok: $ngrokExe http $($script:Port) --url https://$tunnelDomain"
    $ngrokProc = $null
    try {
        $ngrokProc = Start-Process -FilePath $ngrokExe `
            -ArgumentList @("http", "$($script:Port)", "--url", "https://$tunnelDomain") `
            -WindowStyle Hidden `
            -RedirectStandardOutput $script:NgrokOutLog -RedirectStandardError $script:NgrokErrLog `
            -PassThru
    } catch {
        $startError = $_
        Write-Fail "ngrok failed to start (a child that exits immediately surfaces on Windows PowerShell 5.1 as a spurious 'property Path not found' error from Start-Process)"
        Show-NgrokLogTail
        Write-FailDetail $startError
        Exit-With 6
    }
    if ($ngrokProc.HasExited) {
        Show-NgrokLogTail
        Exit-With 6 "ngrok exited immediately (exit code: $($ngrokProc.ExitCode)); see the log tails above"
    }
    [System.IO.File]::WriteAllText($script:NgrokPidFile, "$($ngrokProc.Id)")
    $script:StartedNgrokNow = $true
    $script:NgrokPid = $ngrokProc.Id

    $deadline = (Get-Date).AddSeconds(90)
    $tunnelUp = $false
    while ((Get-Date) -lt $deadline) {
        if (Test-LocalNgrokServesDomain $tunnelDomain) { $tunnelUp = $true; break }
        if ($ngrokProc.HasExited) { break }
        Start-Sleep -Seconds 2
    }
    if (-not $tunnelUp) {
        Write-Fail "ngrok did not bring up https://$tunnelDomain within 90s"
        Show-NgrokLogTail
        Exit-With 6
    }
    Write-Info "tunnel up: https://$tunnelDomain"

    $deadline = (Get-Date).AddSeconds(60)
    $publicHealth = $null
    while ((Get-Date) -lt $deadline) {
        try {
            $publicHealth = Invoke-RestMethod -Uri "https://$tunnelDomain/health" -Method Get -TimeoutSec 10
            if ($publicHealth -and $publicHealth.status -eq "ok") { break }
        } catch {
            $publicHealth = $null
        }
        Start-Sleep -Seconds 2
    }
    if (-not ($publicHealth -and $publicHealth.status -eq "ok")) {
        Write-Fail "public /health did not become OK on https://$tunnelDomain/health"
        Show-NgrokLogTail
        Exit-With 6
    }
    Write-Info "public health OK: https://$tunnelDomain/health"
}

# --------------------------------------------------------------------- stop
function Invoke-Stop {
    if (-not (Test-Path -LiteralPath $script:RuntimeDir)) {
        Write-Info "nothing to stop (no runtime dir at $($script:RuntimeDir))"
        return
    }
    Stop-PidIfManaged $script:WorkerPidFile "bridge.worker" "worker"
    Stop-PidIfManaged $script:NgrokPidFile "ngrok" "ngrok"
    Stop-PidIfManaged $script:BridgePidFile "http_server" "bridge"
    Write-Info "stopped (logs kept at $($script:RuntimeDir))"
}

# ------------------------------------------------------------------- main
$python = $null
try {
    foreach ($required in @("USERPROFILE", "APPDATA", "LOCALAPPDATA")) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($required, "Process"))) {
            Write-Fail "$required is not set; run this from a normal Windows user session"
            exit 1
        }
    }
    Initialize-Layout
    New-Item -ItemType Directory -Force -Path $script:InstanceDir | Out-Null
    if ($Stop) {
        Invoke-Stop
        exit 0
    }
    if (-not [string]::IsNullOrWhiteSpace($CodexHome)) {
        $script:CodexHome = $CodexHome.Trim()
    }
    if ([string]::IsNullOrWhiteSpace($WorkRoot)) {
        if (-not [string]::IsNullOrWhiteSpace($env:BRIDGE_WORK_ROOT)) { $WorkRoot = $env:BRIDGE_WORK_ROOT.Trim() }
        else { $WorkRoot = "D:\work-of-jiaqi" }
    }
    $script:WorkRoot = $WorkRoot.Trim()
    $python = Ensure-Python
    Invoke-BridgePhase
    if ($NgrokCutover) {
        # Legacy one-machine-at-a-time mode (deprecated): Windows owns the
        # fixed ngrok domain; the dual-host worker is not used in this mode.
        if (-not $NoNgrok) {
            Invoke-NgrokPhase
            Write-Host ""
            Write-Host "============================================================"
            Write-Host "  READY (legacy cutover) - public bridge is live"
            Write-Host "  local:   $($script:BaseUrl)/health"
            Write-Host "  public:  https://$(Resolve-TunnelDomain)/health"
            Write-Host "  bridge pid: $((Get-Content -LiteralPath $script:BridgePidFile).Trim())  (log: $script:BridgeLog)"
            Write-Host "  ngrok  pid: $((Get-Content -LiteralPath $script:NgrokPidFile).Trim())  (log: $script:NgrokLog)"
            Write-Host "  stop both:   powershell -ExecutionPolicy Bypass -File $PSCommandPath -Stop"
            Write-Host ""
            Write-Host "  Reminder: a fixed ngrok domain is served by ONE machine at a time; keep"
            Write-Host "  the macOS bridge stopped while Windows owns the domain."
            Write-Host "  (dual-host-router default: NO ngrok - the outbound worker replaces cutover)"
            Write-Host "============================================================"
        } else {
            Write-Host ""
            Write-Host "============================================================"
            Write-Host "  READY (legacy preparation - local only)"
            Write-Host "  local health:  $($script:BaseUrl)/health   (bridge pid: $((Get-Content -LiteralPath $script:BridgePidFile).Trim()))"
            Write-Host "  stop:          powershell -ExecutionPolicy Bypass -File $PSCommandPath -Stop"
            Write-Host "============================================================"
        }
    } elseif ($NoNgrok) {
        Write-Host ""
        Write-Host "============================================================"
        Write-Host "  READY (local-only preparation mode)"
        Write-Host "  local health:  $($script:BaseUrl)/health"
        Write-Host "  local ready:   $($script:BaseUrl)/ready"
        Write-Host "  work root:     $script:WorkRoot"
        Write-Host "  bridge pid:    $((Get-Content -LiteralPath $script:BridgePidFile).Trim())  (log: $script:BridgeLog)"
        Write-Host "  Desktop = OpenAI  /  Bridge CODEX_HOME = $script:CodexHome (DeepSeek)"
        Write-Host "  stop:          powershell -ExecutionPolicy Bypass -File $PSCommandPath -Stop"
        Write-Host ""
        Write-Host "  Dual-host worker skipped (-NoNgrok). Re-run WITHOUT -NoNgrok to connect this"
        Write-Host "  bridge to the Mac router (Windows stays private; one GPT controls Mac + Windows)."
        Write-Host "============================================================"
    } else {
        Invoke-WorkerPhase
        Write-Host ""
        Write-Host "============================================================"
        Write-Host "  READY - dual-host mode (one GPT controls Mac + Windows)"
        Write-Host "  local:   $($script:BaseUrl)/health"
        Write-Host "  worker ->  $script:MacBridgeUrl (outbound only, no public port, no ngrok)"
        Write-Host "  bridge pid: $((Get-Content -LiteralPath $script:BridgePidFile).Trim())  (log: $script:BridgeLog)"
        if (Test-Path -LiteralPath $script:WorkerPidFile) {
            Write-Host "  worker pid: $((Get-Content -LiteralPath $script:WorkerPidFile).Trim())  (log: $script:WorkerLog)"
        }
        Write-Host "  Desktop = OpenAI  /  Bridge CODEX_HOME = $script:CodexHome (DeepSeek)"
        Write-Host "  stop:          powershell -ExecutionPolicy Bypass -File $PSCommandPath -Stop"
        Write-Host ""
        Write-Host "  The Mac router keeps serving the shared ChatGPT Action URL; route there with a"
        Write-Host "  Windows path (C:\..., \\\\wsl$\...) in /start and the Mac handles the rest."
        Write-Host "============================================================"
    }
} catch {
    Write-FailDetail $_
    Stop-StartedChildren
    exit 1
}
