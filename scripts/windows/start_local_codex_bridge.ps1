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
    * Verifies local /health AND /ready before touching ngrok.
    * Optionally (default) exposes the bridge through the fixed ngrok domain
      diploma-ideology-skier.ngrok-free.dev as the cutover step for the
      shared ChatGPT Action URL.

    Windows defaults: CODEX_HOME=%USERPROFILE%\.codex (copy your existing
    DeepSeek provider config.toml there; this script never reads auth.json or
    any secret file), codex detection order = $env:CODEX_BIN,
    %APPDATA%\npm\codex.cmd (npm), codex.exe on PATH (native), codex.cmd on
    PATH. The DeepSeek key is read from the session environment, falling back
    to the Windows USER environment variable DEEPSEEK_API_KEY (never printed);
    when only the key is missing and a Codex config exists, the script asks
    for it once with a masked prompt. Instance state lives OUTSIDE the repo under
    %LOCALAPPDATA%\local-codex-bridge\local (mirror of the macOS control
    plane) and only contains non-secret fields.

    A fixed ngrok domain can be served by exactly ONE machine at a time.
    Cutover order: prepare Windows with -NoNgrok -> stop the macOS bridge
    externally (./scripts/stop_ngrok_bridge.sh on the Mac) -> run this script
    again WITHOUT -NoNgrok. API keys and the ngrok authtoken are never
    printed, logged or written to pid/instance files.

.PARAMETER NoNgrok
    Preparation mode: start and verify the LOCAL bridge only
    (127.0.0.1:8321), never start ngrok. Re-run without -NoNgrok to take
    over the fixed domain.

.PARAMETER Stop
    Stop only the bridge/ngrok processes this script started (PID files in
    the instance runtime dir; unmanaged processes are never touched).

.PARAMETER Domain
    Fixed ngrok domain. Default: $env:NGROK_DOMAIN, else
    diploma-ideology-skier.ngrok-free.dev.

.PARAMETER CodexHome
    CODEX_HOME for the spawned app-server. Default: $env:CODEX_HOME, else
    %USERPROFILE%\.codex.

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
    powershell -ExecutionPolicy Bypass -File scripts\windows\start_local_codex_bridge.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\start_local_codex_bridge.ps1 -Stop
#>
[CmdletBinding()]
param(
    [switch]$NoNgrok,
    [switch]$Stop,
    [string]$Domain = "",
    [string]$CodexHome = "",
    [string]$WorkRoot = "",
    [switch]$SkipAutoInstall
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

# ------------------------------------------------------------------ layout
$script:Version = "windows-bootstrap-1.0.1"
$script:FixedDomainDefault = "diploma-ideology-skier.ngrok-free.dev"
$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script:Instance = "local"
$script:HostAddr = "127.0.0.1"
$script:Port = 8321
$script:BaseUrl = "http://$($script:HostAddr):$($script:Port)"
$script:KeyFile = Join-Path $script:RepoRoot ".bridge_api_key"

# Instance layout is derived in Initialize-Layout (after the environment
# sanity check at the top of main), so missing env vars fail cleanly.
$script:StateRootBase = ""
$script:InstanceDir = ""
$script:RuntimeDir = ""
$script:BridgePidFile = Join-Path $script:RuntimeDir "bridge.pid"
$script:NgrokPidFile = Join-Path $script:RuntimeDir "ngrok.pid"
$script:BridgeLog = Join-Path $script:RuntimeDir "bridge.log"
$script:BridgeOutLog = Join-Path $script:RuntimeDir "bridge.out.log"
$script:BridgeErrLog = Join-Path $script:RuntimeDir "bridge.err.log"
$script:NgrokLog = Join-Path $script:RuntimeDir "ngrok.log"
$script:NgrokOutLog = Join-Path $script:RuntimeDir "ngrok.out.log"
$script:NgrokErrLog = Join-Path $script:RuntimeDir "ngrok.err.log"
$script:InstanceJson = Join-Path $script:InstanceDir "instance.json"

$script:StartedBridgeNow = $false
$script:StartedNgrokNow = $false
$script:BridgePid = 0
$script:NgrokPid = 0
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
    $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source -notlike "*WindowsApps*" -and
        (Test-PythonProbe $pythonCmd.Source @())) {
        return @{ Path = $pythonCmd.Source; Extra = @() }
    }
    $pyCmd = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyCmd -and (Test-PythonProbe $pyCmd.Source @("-3"))) {
        return @{ Path = $pyCmd.Source; Extra = @("-3") }
    }
    # per-user python.org installs may not be on PATH yet; probe them directly
    $userPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-PythonProbe $userPython @()) {
        return @{ Path = $userPython; Extra = @() }
    }
    $userLauncher = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"
    if (Test-PythonProbe $userLauncher @("-3")) {
        return @{ Path = $userLauncher; Extra = @("-3") }
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
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Info "trying winget: install -e --id Python.Python.3.12 --scope user (silent)"
        & $winget.Source install -e --id Python.Python.3.12 --scope user --silent `
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
        $installer = Join-Path $env:TEMP "python-$version-$arch.exe"
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
            Write-Fail "automatic Python install failed: $_"
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
    # on PATH. Returns an absolute path or $null.
    if ($env:CODEX_BIN) {
        if (Test-Path -LiteralPath $env:CODEX_BIN) { return $env:CODEX_BIN }
        Write-Fail "CODEX_BIN is set but not found: $env:CODEX_BIN"
        exit 3
    }
    if ($env:APPDATA) {
        $npmShim = Join-Path $env:APPDATA "npm\codex.cmd"
        if (Test-Path -LiteralPath $npmShim) { return $npmShim }
    }
    $native = Get-Command codex.exe -ErrorAction SilentlyContinue
    if ($native) { return $native.Source }
    # official native installer default location (may not be on PATH yet)
    $nativeLocal = Join-Path $env:USERPROFILE ".local\bin\codex.exe"
    if (Test-Path -LiteralPath $nativeLocal) { return $nativeLocal }
    $shim = Get-Command codex.cmd -ErrorAction SilentlyContinue
    if ($shim) { return $shim.Source }
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
        $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if ($npm) {
            Write-Info "codex not found; installing @openai/codex via npm (user prefix, no admin needed)"
            & $npm.Source install -g "@openai/codex"
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
    if ($env:NGROK_BIN) {
        if (Test-Path -LiteralPath $env:NGROK_BIN) { return $env:NGROK_BIN }
        Write-Fail "NGROK_BIN is set but not found: $env:NGROK_BIN"
        return $null
    }
    $cmd = Get-Command ngrok.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $local = Join-Path $env:LOCALAPPDATA "ngrok\ngrok.exe"
    if (Test-Path -LiteralPath $local) { return $local }
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
    $destDir = Join-Path $env:LOCALAPPDATA "ngrok"
    $zip = Join-Path $env:TEMP "ngrok-v3-stable-windows-amd64.zip"
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

# --------------------------------------------------- secrets (presence only)
function Ensure-SecretEnvironment {
    # API key for the HTTP endpoints (.bridge_api_key, gitignored). The key
    # content is only ever loaded into an env var for the child process.
    if (-not (Test-Path -LiteralPath $script:KeyFile)) {
        $bytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $hex = -join ($bytes | ForEach-Object { $_.ToString("x2") })
        [System.IO.File]::WriteAllText($script:KeyFile, $hex)
        Write-Info "generated a new API key file: $($script:KeyFile) (gitignored; content never printed)"
        Write-Host "If the Custom GPT Action already uses your Mac key, replace the content of"
        Write-Host "$($script:KeyFile) with the Mac's .bridge_api_key so both machines share one key."
    } else {
        $content = [System.IO.File]::ReadAllText($script:KeyFile)
        if ([string]::IsNullOrWhiteSpace($content)) {
            Exit-With 4 "the API key file exists but is empty: $($script:KeyFile) (delete it and re-run, or copy the Mac key)"
        }
    }

    # DeepSeek provider key: session env -> Windows USER env var -> masked
    # one-time prompt. The value is never printed and auth.json is never read.
    if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
        $userKey = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
        if (-not [string]::IsNullOrWhiteSpace($userKey)) {
            $env:DEEPSEEK_API_KEY = $userKey
            $userKey = $null
            Write-Info "DEEPSEEK_API_KEY: imported from the Windows user environment (value never printed)"
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
        $configExists = Test-Path -LiteralPath (Join-Path $script:CodexHome "config.toml")
        $canPrompt = -not [Console]::IsInputRedirected
        if ($configExists -and $canPrompt) {
            Write-Info "DEEPSEEK_API_KEY is not set; a Codex config exists under $script:CodexHome. Paste the DeepSeek key once (masked, session-only) or press Enter to skip:"
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
                    $env:DEEPSEEK_API_KEY = $plain
                    $plain = $null
                    Write-Info "DEEPSEEK_API_KEY accepted for this session (never written to disk by this script)"
                    Write-Host "To skip this prompt next time: setx DEEPSEEK_API_KEY <your-deepseek-key>  (then open a new terminal)"
                }
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
        # Only the user-typed secret can block the bootstrap; everything else
        # is automated. auth.json is never read and no value is printed.
        if (Test-Path -LiteralPath (Join-Path $script:CodexHome "config.toml")) {
            Write-Fail "DEEPSEEK_API_KEY is not set (Codex config found under $script:CodexHome)."
            Write-Host "Set it and re-run:  `$env:DEEPSEEK_API_KEY='<your-deepseek-key>'"
            Write-Host "or persist once:    setx DEEPSEEK_API_KEY <your-deepseek-key>   (then open a new terminal)"
        } else {
            Write-Fail "DEEPSEEK_API_KEY is not set and no Codex config exists under $script:CodexHome."
            Write-Host "Copy config.toml + providers/ from your Mac profile into $script:CodexHome, then set:"
            Write-Host "  `$env:DEEPSEEK_API_KEY='<your-deepseek-key>'   (or: setx DEEPSEEK_API_KEY <your-deepseek-key>)"
        }
        Write-Host "The key value is never printed by this script; auth.json is never read."
        Exit-With 4
    }
}

# Env for the spawned bridge child: set before Start-Process, restored after
# so the caller's session is not left with BRIDGE_API_KEY / CODEX_* set.
$script:EnvKeys = @("BRIDGE_API_KEY", "BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT",
    "BRIDGE_PORT", "BRIDGE_SANDBOX_MODE", "BRIDGE_APPROVAL_POLICY",
    "BRIDGE_NETWORK_ACCESS", "CODEX_HOME", "CODEX_BIN", "PYTHONUTF8")

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
    $script:StateRootBase = ""
    if ($env:BRIDGE_STATE_ROOT) {
        $script:StateRootBase = $env:BRIDGE_STATE_ROOT
    } elseif ($env:XDG_STATE_HOME) {
        $script:StateRootBase = Join-Path $env:XDG_STATE_HOME "local-codex-bridge"
    } else {
        $script:StateRootBase = Join-Path $env:LOCALAPPDATA "local-codex-bridge"
    }
    $script:InstanceDir = Join-Path $script:StateRootBase $script:Instance
    $script:RuntimeDir = Join-Path $script:InstanceDir "runtime"
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
    $proc = Start-Process -FilePath $python.Path -ArgumentList (@($python.Extra) + $bridgeArgs) `
        -WorkingDirectory $script:RepoRoot -WindowStyle Hidden `
        -RedirectStandardOutput $script:BridgeOutLog -RedirectStandardError $script:BridgeErrLog `
        -PassThru
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
            if (-not $bridgeProc -and -not ($python.Path -like "*py.exe")) { break }
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
    Write-Host "Readiness needs: DEEPSEEK_API_KEY in the bridge environment and a DeepSeek"
    Write-Host "provider config under $script:CodexHome (config.toml + providers/deepseek.toml)."
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
        runtime_dir     = $script:RuntimeDir
        bridge_log      = $script:BridgeLog
        ngrok_log       = $script:NgrokLog
        created_at      = (Get-Date).ToString("o")
        script          = $script:Version
    }
    $info | ConvertTo-Json | Set-Content -LiteralPath $script:InstanceJson -Encoding UTF8
}

function Invoke-BridgePhase {
    New-Item -ItemType Directory -Force -Path $script:RuntimeDir | Out-Null
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
    # the exact config path (param > env > %USERPROFILE%\.codex default).
    if (-not $script:CodexHome) {
        if ($env:CODEX_HOME) { $script:CodexHome = $env:CODEX_HOME }
        else { $script:CodexHome = Join-Path $env:USERPROFILE ".codex" }
    }
    Ensure-SecretEnvironment
    $script:CodexBin = Ensure-Codex
    $configCheck = Join-Path $script:CodexHome "config.toml"
    if (-not (Test-Path -LiteralPath $configCheck)) {
        Write-Info "note: $configCheck does not exist yet; copy your DeepSeek provider config (config.toml + providers/) from the Mac profile into $script:CodexHome before the first real /start"
    }
    Ensure-Bridge
    Wait-LocalHealth
    Write-InstanceJson
}

# ------------------------------------------------------------------- ngrok
function Resolve-TunnelDomain {
    if ($Domain) { return $Domain.Trim() }
    if ($env:NGROK_DOMAIN) { return $env:NGROK_DOMAIN.Trim() }
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
    $ngrokProc = Start-Process -FilePath $ngrokExe `
        -ArgumentList @("http", "$($script:Port)", "--url", "https://$tunnelDomain") `
        -WindowStyle Hidden `
        -RedirectStandardOutput $script:NgrokOutLog -RedirectStandardError $script:NgrokErrLog `
        -PassThru
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
    if ($CodexHome) { $script:CodexHome = $CodexHome }
    if (-not $WorkRoot) {
        if ($env:BRIDGE_WORK_ROOT) { $WorkRoot = $env:BRIDGE_WORK_ROOT }
        else { $WorkRoot = "D:\work-of-jiaqi" }
    }
    $script:WorkRoot = $WorkRoot
    $python = Ensure-Python
    Invoke-BridgePhase
    if ($NoNgrok) {
        Write-Host ""
        Write-Host "============================================================"
        Write-Host "  READY (preparation mode - local only, no public tunnel)"
        Write-Host "  local health:  $($script:BaseUrl)/health"
        Write-Host "  local ready:   $($script:BaseUrl)/ready"
        Write-Host "  work root:     $script:WorkRoot"
        Write-Host "  bridge pid:    $((Get-Content -LiteralPath $script:BridgePidFile).Trim())  (log: $script:BridgeLog)"
        Write-Host "  stop:          powershell -ExecutionPolicy Bypass -File $PSCommandPath -Stop"
        Write-Host ""
        Write-Host "  Cutover to the shared Action URL: stop the macOS bridge externally, then"
        Write-Host "  re-run THIS script WITHOUT -NoNgrok."
        Write-Host "============================================================"
    } else {
        Invoke-NgrokPhase
        Write-Host ""
        Write-Host "============================================================"
        Write-Host "  READY - public bridge is live"
        Write-Host "  local:   $($script:BaseUrl)/health"
        Write-Host "  public:  https://$(Resolve-TunnelDomain)/health"
        Write-Host "  bridge pid: $((Get-Content -LiteralPath $script:BridgePidFile).Trim())  (log: $script:BridgeLog)"
        Write-Host "  ngrok  pid: $((Get-Content -LiteralPath $script:NgrokPidFile).Trim())  (log: $script:NgrokLog)"
        Write-Host "  stop both:   powershell -ExecutionPolicy Bypass -File $PSCommandPath -Stop"
        Write-Host ""
        Write-Host "  Reminder: a fixed ngrok domain is served by ONE machine at a time; keep"
        Write-Host "  the macOS bridge stopped while Windows owns the domain."
        Write-Host "============================================================"
    }
} catch {
    Write-Fail $_
    Stop-StartedChildren
    exit 1
}
