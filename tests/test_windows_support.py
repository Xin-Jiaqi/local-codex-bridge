#!/usr/bin/env python3
"""Offline tests for the Windows bootstrap support (dual-host-router).

Covers, without any live bridge / app-server / network / secret:

PART 1 (runs on every host): bridge/platform_paths.py pure resolution -
Windows profile/defaults (Desktop %USERPROFILE%\\.codex stays OpenAI; the
bridge CODEX_HOME is the dedicated DeepSeek profile
%LOCALAPPDATA%\\local-codex-bridge\\codex-deepseek, state root
%LOCALAPPDATA%\\local-codex-bridge, work root D:\\work-of-jiaqi), codex
detection order (CODEX_BIN > %APPDATA%\\npm\\codex.cmd > codex.exe on PATH
> codex.cmd on PATH), npm codex.cmd shim resolution to node + the real JS
entry (embedded-quote argv survives because cmd.exe is bypassed), and the
macOS spawn argv staying byte-identical.

PART 2 (platform-conditional, runs only when os.name == "nt"): the cwd
guard on a real Windows filesystem - case-insensitive control-plane
rejections, drive-root semantics, and acceptance of the default Windows work
root D:\\work-of-jiaqi.

PART 3 (runs on every host): structural checks of
scripts/windows/start_local_codex_bridge.ps1 - the dual-host default (local
bridge + OUTBOUND worker; Windows opens no public port and never starts ngrok
unless the deprecated -NgrokCutover flag is passed), Desktop OpenAI / Bridge
DeepSeek separation (backup-deepseek restore or minimal OpenAI config,
dedicated %LOCALAPPDATA%\\local-codex-bridge\\codex-deepseek, DPAPI secret
store, OPENAI_BASE_URL scrub only when confirmed DeepSeek, codex-deepseek.cmd
wrapper), the fixed-domain default, local /health + /ready gates before any
outbound phase, that secrets (API key / DeepSeek key / worker token / ngrok
authtoken) are never echoed or written in plain text, and that no path is
ever built at load time from still-empty layout placeholders
(Windows PowerShell aborts such runs with a 'parameter "Path" is an empty
string' binding error) - all env-driven path variables are guarded instead.
External commands resolve to plain string paths (Get-CommandPath:
Source/Definition fallback, no object .Path); the bridge/ngrok
Start-Process -PassThru launches guard the PS 5.1 dead-child "Property Path
not found" shape and never write a pid for an exited child; catch blocks
print the failing script line + PowerShell stack without echoing argument
values (no secret surface).
"""

import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge.platform_paths import (
    CodexSpawnResolutionError,
    codex_argv_head,
    detect_codex_binary,
    resolve_codex_shim,
    windows_codex_candidates,
    windows_defaults,
    windows_profile,
)
from bridge.workspace_guard import TaskCwdError, validate_task_cwd
from http_server.server import build_cwd_guard

WIN_ENV = {
    "USERPROFILE": r"C:\Users\Jiaqi",
    "APPDATA": r"C:\Users\Jiaqi\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\Jiaqi\AppData\Local",
    "PATH": r"C:\Windows\system32;C:\Windows",
}

NPM_SHIM = r"C:\Users\Jiaqi\AppData\Roaming\npm\codex.cmd"
NPM_PKG = r"C:\Users\Jiaqi\AppData\Roaming\npm\node_modules\@openai\codex\package.json"
NPM_ENTRY = r"C:\Users\Jiaqi\AppData\Roaming\npm\node_modules\@openai\codex\bin\codex.js"
NODE = r"C:\Program Files\nodejs\node.exe"
NATIVE_EXE = r"C:\Users\Jiaqi\.local\bin\codex.exe"

# Layout vars that are plain "" at script load time and only derive their
# real value inside Initialize-Layout (never Join-Path'd while still empty).
LAYOUT_PLACEHOLDER_VARS = (
    "StateRootBase", "InstanceDir", "RuntimeDir", "DeepseekCodexHome",
    "SecretsDir", "DeepseekSecretFile", "WorkerTokenFile", "BridgePidFile",
    "NgrokPidFile", "WorkerPidFile", "BridgeLog", "BridgeOutLog",
    "BridgeErrLog", "NgrokLog", "NgrokOutLog", "NgrokErrLog", "WorkerLog",
    "WorkerOutLog", "WorkerErrLog", "WorkerStateFile", "InstanceJson",
)
INSTANCE_RUNTIME_FILE_VARS = (
    "BridgePidFile", "NgrokPidFile", "BridgeLog", "BridgeOutLog",
    "BridgeErrLog", "NgrokLog", "NgrokOutLog", "NgrokErrLog",
    "WorkerPidFile", "WorkerLog", "WorkerOutLog", "WorkerErrLog",
    "WorkerStateFile", "InstanceJson",
)


def fake_which(name, path=None):
    if name == "node.exe":
        return NODE
    if name == "codex.exe":
        return NATIVE_EXE
    return None


class WindowsDefaultsTest(unittest.TestCase):
    """Part 1a: platform_paths defaults (pure, runs on any host)."""

    def test_profile_uses_windows_env(self):
        profile = windows_profile(WIN_ENV)
        self.assertEqual(profile["userprofile"], r"C:\Users\Jiaqi")
        self.assertEqual(
            profile["appdata"], r"C:\Users\Jiaqi\AppData\Roaming"
        )
        self.assertEqual(
            profile["localappdata"], r"C:\Users\Jiaqi\AppData\Local"
        )

    def test_profile_falls_back_under_userprofile(self):
        profile = windows_profile({"USERPROFILE": r"C:\Users\X"})
        self.assertEqual(
            profile["localappdata"], r"C:\Users\X\AppData\Local"
        )
        self.assertEqual(
            profile["appdata"], r"C:\Users\X\AppData\Roaming"
        )

    def test_profile_requires_userprofile(self):
        with self.assertRaises(ValueError):
            windows_profile({"APPDATA": r"C:\a", "LOCALAPPDATA": r"C:\l"})

    def test_windows_defaults_codex_home_and_state_root(self):
        defaults = windows_defaults(WIN_ENV)
        # Bridge DeepSeek CODEX_HOME is dedicated; Desktop keeps OpenAI in
        # %USERPROFILE%\.codex (Desktop is never touched by the bridge).
        self.assertEqual(
            defaults["codex_home"],
            r"C:\Users\Jiaqi\AppData\Local\local-codex-bridge\codex-deepseek",
        )
        self.assertEqual(
            defaults["desktop_codex_home"], r"C:\Users\Jiaqi\.codex"
        )
        self.assertEqual(
            defaults["state_root_base"],
            r"C:\Users\Jiaqi\AppData\Local\local-codex-bridge",
        )
        self.assertEqual(defaults["work_root"], r"D:\work-of-jiaqi")

    def test_windows_defaults_overrides(self):
        env = dict(WIN_ENV, BRIDGE_WORK_ROOT=r"E:\work", XDG_STATE_HOME=r"D:\state")
        defaults = windows_defaults(env)
        self.assertEqual(defaults["work_root"], r"E:\work")
        self.assertEqual(
            defaults["state_root_base"], r"D:\state\local-codex-bridge"
        )


class CodexDetectionTest(unittest.TestCase):
    """Part 1b: codex executable detection (pure, runs on any host)."""

    def test_env_override_wins(self):
        env = dict(WIN_ENV, CODEX_BIN=r"C:\tools\codex.exe")
        found = detect_codex_binary(env, isfile=lambda p: p == r"C:\tools\codex.exe")
        self.assertEqual(found, r"C:\tools\codex.exe")

    def test_npm_shim_second(self):
        found = detect_codex_binary(
            WIN_ENV, isfile=lambda p: p == NPM_SHIM
        )
        self.assertEqual(found, NPM_SHIM)

    def test_native_exe_on_path_after_npm_shim(self):
        # npm shim absent -> codex.exe on PATH wins over codex.cmd/bare codex
        env = dict(WIN_ENV, PATH=r"C:\Users\Jiaqi\.local\bin")
        calls = []

        def which(name, path=None):
            calls.append(name)
            if name == "codex.exe":
                return NATIVE_EXE
            return None

        found = detect_codex_binary(env, isfile=lambda p: False, which=which)
        self.assertEqual(found, NATIVE_EXE)
        self.assertEqual(calls, ["codex.exe"])

    def test_path_fallback_order(self):
        # No native exe -> codex.cmd on PATH -> bare codex last.
        env = dict(WIN_ENV, PATH=r"C:\tools")
        npm_codex_cmd = r"C:\tools\codex.cmd"

        def which(name, path=None):
            if name == "codex.exe":
                return None
            if name == "codex.cmd":
                return npm_codex_cmd
            if name == "codex":
                return r"C:\tools\codex"
            return None

        found = detect_codex_binary(env, isfile=lambda p: False, which=which)
        self.assertEqual(found, npm_codex_cmd)

    def test_invalid_override_falls_through(self):
        env = dict(WIN_ENV, CODEX_BIN=r"C:\missing\codex.exe")
        found = detect_codex_binary(env, isfile=lambda p: p == NPM_SHIM)
        self.assertEqual(found, NPM_SHIM)

    def test_none_when_everything_missing(self):
        self.assertIsNone(
            detect_codex_binary(WIN_ENV, isfile=lambda p: False, which=lambda n, path=None: None)
        )

    def test_candidates_do_not_touch_filesystem(self):
        candidates = windows_codex_candidates(WIN_ENV)
        self.assertEqual(
            candidates, [r"C:\Users\Jiaqi\AppData\Roaming\npm\codex.cmd"]
        )


class NpmShimResolutionTest(unittest.TestCase):
    """Part 1c: codex.cmd -> node + JS entry resolution (pure)."""

    FILES = {
        NPM_SHIM: True,
        NPM_PKG: True,
        NPM_ENTRY: True,
        NODE: True,
    }
    TEXTS = {NPM_PKG: json.dumps({"bin": {"codex": "bin/codex.js"}})}

    def isfile(self, path):
        return path in self.FILES

    def read_text(self, path):
        if path not in self.TEXTS:
            raise AssertionError("unexpected file read: %s" % path)
        return self.TEXTS[path]

    def test_shim_resolves_to_node_plus_entry(self):
        argv = codex_argv_head(
            NPM_SHIM,
            ["app-server", "--listen", "stdio://", "-c", 'model="deepseek-chat"'],
            env=WIN_ENV,
            os_name="nt",
            isfile=self.isfile,
            which=fake_which,
            read_text=self.read_text,
        )
        self.assertEqual(
            argv,
            [NODE, NPM_ENTRY, "app-server", "--listen", "stdio://",
             "-c", 'model="deepseek-chat"'],
        )

    def test_shim_prefers_adjacent_node_exe(self):
        adjacent = r"C:\Users\Jiaqi\AppData\Roaming\npm\node.exe"
        self.FILES[adjacent] = True
        try:
            argv = codex_argv_head(
                NPM_SHIM, ["app-server"], env=WIN_ENV, os_name="nt",
                isfile=self.isfile, which=fake_which, read_text=self.read_text,
            )
            self.assertEqual(argv, [adjacent, NPM_ENTRY, "app-server"])
        finally:
            del self.FILES[adjacent]

    def test_packaged_native_exe_bin(self):
        pkg_exe = r"C:\Users\Jiaqi\AppData\Roaming\npm\node_modules\@openai\codex\codex.exe"
        files = dict(self.FILES, **{pkg_exe: True})
        texts = dict(self.TEXTS, **{NPM_PKG: json.dumps({"bin": "codex.exe"})})
        argv = codex_argv_head(
            NPM_SHIM, ["app-server"], env=WIN_ENV, os_name="nt",
            isfile=files.__contains__, which=fake_which,
            read_text=texts.__getitem__,
        )
        self.assertEqual(argv, [pkg_exe, "app-server"])

    def test_unresolvable_shim_raises_actionable_error(self):
        with self.assertRaises(CodexSpawnResolutionError) as ctx:
            codex_argv_head(
                NPM_SHIM, ["app-server"], env=WIN_ENV, os_name="nt",
                isfile=lambda p: p == NPM_SHIM, which=fake_which,
                read_text=lambda p: "",
            )
        message = str(ctx.exception)
        self.assertIn("npm", message)
        self.assertIn("chatgpt.com/codex/install.ps1", message)

    def test_resolve_shim_none_without_package(self):
        self.assertIsNone(
            resolve_codex_shim(
                r"C:\x\codex.cmd", env=WIN_ENV,
                isfile=lambda p: False, which=fake_which,
            )
        )


class SpawnArgvParityTest(unittest.TestCase):
    """Part 1d: macOS spawn argv stays byte-identical."""

    def test_posix_branch_is_unchanged(self):
        extra = ["app-server", "--listen", "stdio://", "-c", 'model="deepseek-chat"']
        self.assertEqual(
            codex_argv_head("/opt/homebrew/bin/codex", extra),
            ["/opt/homebrew/bin/codex"] + extra,
        )

    def test_windows_exe_is_spawned_directly(self):
        argv = codex_argv_head(
            r"C:\tools\codex.exe", ["app-server", "-c", 'model="deepseek-chat"'],
            os_name="nt",
        )
        self.assertEqual(
            argv,
            [r"C:\tools\codex.exe", "app-server", "-c", 'model="deepseek-chat"'],
        )


@unittest.skipUnless(os.name == "nt", "runs only on a real Windows host")
class WindowsCwdGuardTest(unittest.TestCase):
    """Part 2: cwd guard semantics on Windows (case-insensitive FS)."""

    def _layout(self):
        import tempfile
        base = tempfile.mkdtemp(prefix="lcb-win-")
        layout = {}
        for key in ("home", "repo", "state", "codex"):
            layout[key] = os.path.join(base, key)
            os.makedirs(layout[key])
        layout["work"] = os.path.join(base, "work-of-jiaqi")
        os.makedirs(layout["work"])
        layout["base"] = base
        return layout

    def _guard(self, layout):
        return {
            "home": layout["home"],
            "repo_root": layout["repo"],
            "state_root": layout["state"],
            "codex_home": layout["codex"],
        }

    def test_case_variant_of_home_is_rejected(self):
        layout = self._layout()
        try:
            cwd = os.path.join(layout["home"].upper(), "Desktop", "proj")
            os.makedirs(cwd)
            with self.assertRaises(TaskCwdError) as ctx:
                validate_task_cwd(cwd, **self._guard(layout))
            self.assertEqual(ctx.exception.category, "home")
        finally:
            import shutil
            shutil.rmtree(layout["base"], ignore_errors=True)

    def test_drive_root_is_ancestor_of_home_on_same_drive(self):
        layout = self._layout()
        try:
            home_drive = os.path.splitdrive(layout["home"])[0] + os.sep
            with self.assertRaises(TaskCwdError) as ctx:
                validate_task_cwd(home_drive, **self._guard(layout))
            self.assertEqual(ctx.exception.category, "home")
        finally:
            import shutil
            shutil.rmtree(layout["base"], ignore_errors=True)

    def test_case_variant_of_codex_home_is_rejected(self):
        layout = self._layout()
        try:
            cwd = layout["codex"].upper()
            if not os.path.exists(cwd):
                cwd = layout["codex"]
            with self.assertRaises(TaskCwdError) as ctx:
                validate_task_cwd(cwd, **self._guard(layout))
            self.assertEqual(ctx.exception.category, "codex_home")
        finally:
            import shutil
            shutil.rmtree(layout["base"], ignore_errors=True)

    def test_default_windows_work_root_subdir_is_accepted(self):
        # D:\work-of-jiaqi is an ordinary project volume outside the control
        # plane; acceptance must not depend on the drive letter spelling.
        cwd = os.path.join(r"D:\work-of-jiaqi", "some-project")
        canonical = validate_task_cwd(cwd, home=r"C:\Users\bridge-user",
                                      repo_root=r"C:\Users\bridge-user\repo",
                                      state_root=r"C:\Users\bridge-user\repo-state",
                                      codex_home=r"C:\Users\bridge-user\AppData\Local\local-codex-bridge\codex-deepseek")
        self.assertTrue(canonical.lower().startswith(r"d:\work-of-jiaqi"))

    def test_build_cwd_guard_windows_defaults(self):
        env = {
            "USERPROFILE": r"C:\Users\Jiaqi",
            "LOCALAPPDATA": r"C:\Users\Jiaqi\AppData\Local",
            "BRIDGE_INSTANCE": "local",
            "BRIDGE_SANDBOX_MODE": "workspace-write",
        }
        guard = build_cwd_guard(env)
        self.assertEqual(guard["home"], r"C:\Users\Jiaqi")
        self.assertEqual(
            guard["codex_home"],
            r"C:\Users\Jiaqi\AppData\Local\local-codex-bridge\codex-deepseek",
        )
        self.assertEqual(
            guard["state_root"],
            r"C:\Users\Jiaqi\AppData\Local\local-codex-bridge\local",
        )


class BootstrapScriptStructuralTest(unittest.TestCase):
    """Part 3: PowerShell bootstrap structural/secret-surface checks."""

    @classmethod
    def setUpClass(cls):
        script = os.path.join(ROOT, "scripts", "windows",
                              "start_local_codex_bridge.ps1")
        with open(script, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.lines = cls.source.splitlines()

    def test_script_exists_and_has_param_switches(self):
        self.assertGreater(len(self.source), 3000)
        self.assertIn("[switch]$NoNgrok", self.source)
        self.assertIn("[switch]$Stop", self.source)
        self.assertIn("[switch]$NgrokCutover", self.source)
        self.assertIn("[string]$MacBridgeUrl", self.source)
        self.assertIn("[string]$WorkerToken", self.source)
        self.assertIn("-NoNgrok", self.source)
        self.assertIn("-ExecutionPolicy Bypass", self.source)

    def test_fixed_domain_default_and_overrides(self):
        self.assertIn(
            '$script:FixedDomainDefault = "diploma-ideology-skier.ngrok-free.dev"',
            self.source,
        )
        self.assertIn(
            '$script:MacBridgeUrlDefault = "https://diploma-ideology-skier.ngrok-free.dev"',
            self.source,
        )
        self.assertIn("$env:NGROK_DOMAIN", self.source)
        self.assertIn("[string]$Domain", self.source)

    def test_windows_defaults_present(self):
        self.assertIn('$WorkRoot = "D:\\work-of-jiaqi"', self.source)
        # Dedicated DeepSeek bridge profile (Desktop %USERPROFILE%\.codex
        # stays OpenAI; the bridge never uses it as CODEX_HOME).
        self.assertIn('$script:DeepseekCodexHome = Join-Path $script:StateRootBase "codex-deepseek"', self.source)
        self.assertIn("backup-deepseek\\config.toml", self.source)
        self.assertIn('model = "gpt-5.6-sol"', self.source)
        self.assertIn('Join-Path $appData.Trim() "npm\\codex.cmd"', self.source)

    def test_local_health_and_ready_verified_before_ngrok_phase(self):
        wait_health = self.source.index("function Wait-LocalHealth")
        ready_check = self.source.index('Get-HealthJson "/ready"')
        bridge_phase = self.source.index("function Invoke-BridgePhase")
        ngrok_phase = self.source.index("function Invoke-NgrokPhase")
        start_ngrok = self.source.index("starting ngrok:")
        self.assertLess(wait_health, ready_check)
        self.assertLess(ready_check, bridge_phase)
        self.assertLess(bridge_phase, ngrok_phase)
        self.assertLess(ngrok_phase, start_ngrok)
        # the local verification must run before ngrok can start in main too
        main_ngrok = self.source.index("Invoke-NgrokPhase", bridge_phase)
        self.assertLess(
            self.source.index("Wait-LocalHealth", bridge_phase), main_ngrok
        )
        self.assertIn("Get-HealthJson \"/health\"", self.source)
        self.assertIn("Get-HealthJson \"/ready\"", self.source)

    def test_secret_values_are_never_echoed_or_written(self):
        # Echoing cmdlets must never interpolate a secret variable. Backtick-
        # escaped $env:DEEPSEEK_API_KEY inside double quotes is instructional
        # text for the user ("$env:DEEPSEEK_API_KEY='<key>'"), not a value.
        echoing = re.compile(
            r"^\s*(Write-Host|Write-Info|Write-Fail|Write-Output|Write-Error|"
            r"Out-File|Set-Content|Add-Content|ConvertTo-Json).*", re.I
        )
        value_echo = re.compile(
            r"(?<!`)\$env:(BRIDGE_API_KEY|NGROK_AUTHTOKEN|DEEPSEEK_API_KEY|BRIDGE_WORKER_TOKEN)"
        )
        forbidden = []
        for lineno, line in enumerate(self.lines, 1):
            if not echoing.match(line):
                continue
            if value_echo.search(line):
                forbidden.append((lineno, line.strip()))
        self.assertEqual(forbidden, [])

    def test_python_auto_install_paths_are_automated(self):
        # Requirement: missing Python must be installed automatically, first
        # via winget user scope, then the python.org per-user installer, then
        # PATH is refreshed and startup continues (no admin/UAC).
        self.assertNotIn("never auto-installs", self.source)
        self.assertIn("Python.Python.3.12", self.source)
        self.assertIn("--scope user", self.source)
        self.assertIn("--accept-package-agreements", self.source)
        self.assertIn("InstallAllUsers=0", self.source)
        self.assertIn("PrependPath=1", self.source)
        self.assertIn("Include_launcher=1", self.source)
        self.assertIn("https://www.python.org/ftp/python/", self.source)
        self.assertIn("function Refresh-PathFromRegistry", self.source)
        # after an automatic install the script must re-detect and continue,
        # not exit with a manual-python message
        python_section = self.source[self.source.index("function Ensure-Python"):
                                     self.source.index("# ------------------------------------------------------------------- codex")]
        self.assertIn("$python = Get-PythonCommand", python_section)
        self.assertIn("return $python", python_section)

    def test_deepseek_key_dpapi_store_and_masked_prompt(self):
        # The DeepSeek key is never stored in plain text: session env first,
        # then the DPAPI-protected store (deepseek.key.dpapi), then a legacy
        # plaintext USER env var is migrated into DPAPI (never auto-deleted
        # but never relied on), and only interactive terminals get a masked
        # prompt that writes DPAPI. auth.json is never read.
        self.assertIn("deepseek.key.dpapi", self.source)
        self.assertIn('[System.Security.Cryptography.DataProtectionScope]::CurrentUser', self.source)
        self.assertIn("[System.Security.Cryptography.ProtectedData]::Protect", self.source)
        self.assertIn("[System.Security.Cryptography.ProtectedData]::Unprotect", self.source)
        self.assertIn("WriteAllBytes($Path, $protected)", self.source)
        self.assertIn('Read-Host "DeepSeek API key" -AsSecureString', self.source)
        self.assertIn("[Console]::IsInputRedirected", self.source)
        self.assertIn("SecureStringToBSTR", self.source)
        # the key is only ever injected into the child process env
        self.assertIn("function Ensure-DeepseekKey", self.source)
        self.assertNotIn("setx DEEPSEEK_API_KEY <", self.source)
        for lineno, line in enumerate(self.lines, 1):
            if "auth.json" not in line:
                continue
            low = line.lower()
            for reader in ("readalltext", "get-content", "invoke-restmethod",
                           "invoke-webrequest"):
                if reader in low:
                    self.fail("auth.json read attempt at line %d: %s"
                              % (lineno, line.strip()))
            for writer in ("out-file", "set-content", "add-content"):
                if writer in low:
                    self.fail("auth.json write attempt at line %d: %s"
                              % (lineno, line.strip()))

    def test_codex_install_fallbacks_stay_automatic(self):
        # npm global install first, then the official native installer, then
        # PATH refresh and re-detection; instructions only as a last resort.
        self.assertIn('& $npm install -g "@openai/codex"', self.source)
        self.assertGreaterEqual(self.source.count("chatgpt.com/codex/install.ps1"), 2)
        auto_run = self.source.index('irm https://chatgpt.com/codex/install.ps1 | iex')
        segment = self.source[auto_run:auto_run + 400]
        self.assertIn("Refresh-PathFromRegistry", segment)
        self.assertIn("Find-Codex", segment)

    def test_secrets_never_written_to_logs_or_pid_files(self):
        for lineno, line in enumerate(self.lines, 1):
            low = line.lower()
            if ("bridgeapikey" in low or "ngrokauthtoken" in low
                    or "deepseekapikey" in low) and (
                    "out-file" in low or "add-content" in low or "set-content" in low):
                self.fail("secret written to a file at line %d: %s"
                          % (lineno, line.strip()))

    def test_ngrok_token_never_passed_on_command_line(self):
        # The script must rely on NGROK_AUTHTOKEN env / existing config only.
        ngrok_start = self.source.index("starting ngrok:")
        segment = self.source[ngrok_start:ngrok_start + 600]
        self.assertNotIn("--authtoken", segment)
        self.assertNotIn("add-authtoken", segment)
        self.assertIn("--url", segment)

    def test_api_key_read_into_env_only(self):
        # .bridge_api_key is loaded with ReadAllText into BRIDGE_API_KEY env
        # for the child process; never Get-Content'd into console output.
        self.assertIn("[System.IO.File]::ReadAllText($script:KeyFile)", self.source)
        self.assertNotIn("Get-Content", self.source.split("function Ensure-SecretEnvironment")[0])
        for lineno, line in enumerate(self.lines, 1):
            if "Get-Content" in line and "bridge_api_key" not in line.lower():
                # Get-Content is only used for pid-file summaries, never keys
                continue
            if "Get-Content" in line and "bridge_api_key" in line.lower():
                self.fail("key file read via Get-Content at line %d" % lineno)

    def test_env_keys_defined_before_snapshot_helpers(self):
        # Save-EnvSnapshot/Restore-Env iterate $script:EnvKeys; under
        # Set-StrictMode an undefined script variable aborts the whole run,
        # so the definition must exist and precede its first use.
        self.assertIn("function Save-EnvSnapshot", self.source)
        define = self.source.index("$script:EnvKeys = @(")
        snapshot = self.source.index("function Save-EnvSnapshot")
        self.assertLess(define, snapshot)
        match = re.search(r"\$script:EnvKeys = @\((.*?)\)", self.source, re.S)
        self.assertIsNotNone(match)
        keys = re.findall(r'"([A-Z0-9_]+)"', match.group(1))
        self.assertEqual(keys, [
            "BRIDGE_API_KEY", "BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT",
            "BRIDGE_PORT", "BRIDGE_SANDBOX_MODE", "BRIDGE_APPROVAL_POLICY",
            "BRIDGE_NETWORK_ACCESS", "CODEX_HOME", "CODEX_BIN", "PYTHONUTF8",
            "DEEPSEEK_API_KEY", "BRIDGE_WORKER_TOKEN", "MAC_BRIDGE_URL",
        ])

    # --------------------- empty-path regression (PS 5.1 Path binding) ----
    def test_load_time_placeholders_are_empty_and_never_join_path(self):
        # Reported Windows PS 5.1 failure: pid/log/instance paths were
        # Join-Path'd at load time from InstanceDir/RuntimeDir which are still
        # "" there, aborting EVERY run (any flag, even -Stop) before main with
        # 'Cannot bind argument to parameter "Path" because it is an empty
        # string'. Load time may only Join-Path bases that are already
        # guaranteed non-empty: $PSScriptRoot (after its guard) and
        # $script:RepoRoot (after Resolve-Path).
        load_region = self.source[:self.source.index("function Write-Info")]
        for var in LAYOUT_PLACEHOLDER_VARS:
            self.assertIn('$script:%s = ""' % var, load_region,
                          "%s must start as a plain empty placeholder at load time" % var)
        guarded_bases = ("$PSScriptRoot", "$script:RepoRoot")
        for lineno, line in enumerate(load_region.splitlines(), 1):
            if "Join-Path" not in line or line.lstrip().startswith("#"):
                continue
            if not any(base in line for base in guarded_bases):
                self.fail("load-time Join-Path at line %d uses an unguarded "
                          "base: %s" % (lineno, line.strip()))

    def test_runtime_files_derived_after_dirs_inside_initialize_layout(self):
        # All pid/log/instance file paths must be derived inside
        # Initialize-Layout, strictly after InstanceDir/RuntimeDir exist, so
        # Join-Path never receives an empty base on Windows PS 5.1.
        layout = self.source[self.source.index("function Initialize-Layout"):
                             self.source.index("function Test-PortBusy")]
        self.assertIn('(Require-EnvPath "LOCALAPPDATA")', layout)
        instance_join = layout.index("$script:InstanceDir = Join-Path")
        runtime_join = layout.index("$script:RuntimeDir = Join-Path")
        self.assertLess(instance_join, runtime_join)
        for var in INSTANCE_RUNTIME_FILE_VARS:
            marker = "$script:%s = Join-Path" % var
            self.assertIn(marker, layout,
                          "%s must be derived inside Initialize-Layout" % var)
            self.assertGreater(layout.index(marker), runtime_join,
                               "%s must be derived after RuntimeDir exists" % var)

    def test_script_root_and_repo_root_guards_are_diagnosable(self):
        # An empty $PSScriptRoot (pasted / -Command execution) or an
        # unresolvable repo root must produce a clear error, not PowerShell's
        # cryptic 'parameter "Path" is an empty string' binding failure.
        load_region = self.source[:self.source.index("function Write-Info")]
        self.assertIn("IsNullOrWhiteSpace($PSScriptRoot)", load_region)
        self.assertIn("throw \"this script must be run from a file", load_region)
        self.assertIn('Join-Path -Path $PSScriptRoot -ChildPath "..\\.."', load_region)
        self.assertIn("-ErrorAction Stop", load_region)
        self.assertIn("throw \"cannot resolve the bridge repo root", load_region)
        self.assertIn(
            '$script:KeyFile = Join-Path $script:RepoRoot ".bridge_api_key"',
            load_region,
        )

    def test_env_paths_never_reach_join_path_unguarded(self):
        # No path env var may feed Join-Path directly: required vars go
        # through Require-EnvPath (diagnosable throw with the variable name),
        # optional probes read a validated local variable first. A direct
        # 'Join-Path $env:X' would pass an empty string straight to the Path
        # parameter on Windows PowerShell 5.1.
        self.assertNotIn("Join-Path $env:", self.source)
        for name in ("LOCALAPPDATA", "TEMP", "USERPROFILE"):
            self.assertIn('Require-EnvPath "%s"' % name, self.source,
                          "required path env var %s must go through Require-EnvPath" % name)
        for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE",
                     "BRIDGE_STATE_ROOT", "XDG_STATE_HOME", "CODEX_HOME"):
            self.assertIn('GetEnvironmentVariable("%s", "Process")' % name, self.source)

    def test_codex_home_placeholder_and_default_resolution_guarded(self):
        # -CodexHome is optional: the load-time "" placeholder prevents
        # Set-StrictMode from aborting Invoke-BridgePhase on the unset
        # $script:CodexHome, and the dedicated DeepSeek profile
        # (%LOCALAPPDATA%\local-codex-bridge\codex-deepseek) is the default;
        # Desktop %USERPROFILE%\.codex is never used as bridge CODEX_HOME.
        load_region = self.source[:self.source.index("function Write-Info")]
        for var in ("CodexHome", "CodexBin", "WorkRoot"):
            self.assertIn('$script:%s = ""' % var, load_region)
        bridge_phase = self.source[self.source.index("function Invoke-BridgePhase"):
                                   self.source.index("function Resolve-TunnelDomain")]
        self.assertIn("$script:CodexHome = $script:DeepseekCodexHome", bridge_phase)
        self.assertIn('GetEnvironmentVariable("CODEX_HOME", "Process")', bridge_phase)
        self.assertIn('throw "CODEX_HOME is empty', bridge_phase)

    def test_dual_host_worker_phase_structural(self):
        # Default mode = local bridge + outbound worker; Windows never opens
        # a public port and never starts ngrok unless -NgrokCutover is passed.
        self.assertIn("function Invoke-WorkerPhase", self.source)
        self.assertIn("function Ensure-Worker", self.source)
        self.assertIn("function Ensure-WorkerToken", self.source)
        self.assertIn("function Wait-WorkerConnected", self.source)
        self.assertIn("python -m bridge.worker", self.source)
        self.assertIn("--router-url", self.source)
        self.assertIn("--local-api-key-file", self.source)
        self.assertIn("--state-file", self.source)
        self.assertIn("worker.state.json", self.source)
        main_region = self.source[self.source.index("# ------------------------------------------------------------------- main"):]
        worker_call = main_region.index("Invoke-WorkerPhase")
        ngrok_call = main_region.index("Invoke-NgrokPhase")
        cutover = main_region.index("$NgrokCutover")
        self.assertLess(cutover, ngrok_call)
        self.assertLess(cutover, worker_call)
        # local /health and /ready gates run before any outbound phase
        self.assertLess(
            self.source.index("function Wait-LocalHealth"),
            self.source.index("function Invoke-WorkerPhase"),
        )
        # the worker only ever talks to the local bridge + the Mac router
        self.assertNotIn("ngrok http ", self.source.split("function Invoke-WorkerPhase")[1])

    def test_desktop_openai_bridge_deepseek_separation_structural(self):
        # Desktop restore: backup-deepseek config preferred, minimal OpenAI
        # config as the no-backup fallback; never touches auth.json/state.
        self.assertIn("function Invoke-HomeSeparationPhase", self.source)
        self.assertIn("function Test-ConfigMarkedDeepseek", self.source)
        self.assertIn("function Write-MinimalOpenAiDesktopConfig", self.source)
        self.assertIn("function Write-DeepseekBridgeConfig", self.source)
        self.assertIn("backup-deepseek\\config.toml", self.source)
        self.assertIn('model = "gpt-5.6-sol"', self.source)
        self.assertIn('model_provider = "openai"', self.source)
        self.assertIn("api.deepseek.com", self.source)
        # bridge config with an env_key reference only (no key value)
        self.assertIn('env_key = "DEEPSEEK_API_KEY"', self.source)
        # auth.json is never read or written (full-file scan also below)
        for lineno, line in enumerate(self.lines, 1):
            if "auth.json" not in line.lower():
                continue
            low = line.lower()
            for op in ("readalltext", "get-content", "copy-item",
                       "out-file", "set-content", "add-content",
                       "invoke-restmethod", "invoke-webrequest"):
                if op in low:
                    self.fail("auth.json %s attempt at line %d: %s"
                              % (op, lineno, line.strip()))

    def test_openai_base_url_scrub_is_deepseek_confirmed_only(self):
        guard = self.source[self.source.index("function Invoke-OpenaiBaseUrlGuard"):
                            self.source.index("function Install-DeepseekCliWrapper")]
        self.assertIn('GetEnvironmentVariable("OPENAI_BASE_URL", "User")', guard)
        self.assertIn('$userValue -match "(?i)deepseek"', guard)
        self.assertIn(
            '[Environment]::SetEnvironmentVariable("OPENAI_BASE_URL", $null, "User")',
            guard,
        )
        # a non-DeepSeek OPENAI_BASE_URL (normal proxy) is kept untouched
        self.assertIn("kept untouched", guard)
        self.assertNotIn('SetEnvironmentVariable("OPENAI_BASE_URL", $null',
                         guard.split('$userValue -match')[0])

    def test_codex_deepseek_wrapper_exists_and_is_installed(self):
        wrapper = os.path.join(ROOT, "scripts", "windows", "codex-deepseek.cmd")
        with open(wrapper, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("CODEX_HOME=%LOCALAPPDATA%\\local-codex-bridge\\codex-deepseek", text)
        self.assertIn("setlocal", text.lower())
        self.assertIn("codex %*", text)
        # the wrapper only ever sets the dedicated DeepSeek CODEX_HOME; the
        # Desktop %USERPROFILE%\.codex appears in comments only, never as a
        # set/read target
        self.assertNotIn('set "CODEX_HOME=%USERPROFILE%', text)
        self.assertIn("%LOCALAPPDATA%\\local-codex-bridge\\codex-deepseek", text)
        # and the bootstrap installs it next to the detected codex binary
        self.assertIn("function Install-DeepseekCliWrapper", self.source)
        self.assertIn('$destDir = Split-Path -Parent $cmd', self.source)
        self.assertIn('Copy-Item -LiteralPath $wrapperSrc -Destination $dest -Force', self.source)

    def test_secrets_store_is_dpapi_never_plaintext(self):
        # deepseek.key.dpapi and worker.token.dpapi are only written through
        # Protect-BridgeSecretText (CryptProtectData); no plaintext writer may
        # target the secrets dir, and no Get-Content/ReadAllText may be used
        # to print them.
        self.assertIn("deepseek.key.dpapi", self.source)
        self.assertIn("worker.token.dpapi", self.source)
        self.assertIn("function Protect-BridgeSecretText", self.source)
        self.assertIn("function Unprotect-BridgeSecretText", self.source)
        for lineno, line in enumerate(self.lines, 1):
            if ".dpapi" not in line:
                continue
            low = line.lower()
            for op in ("get-content", "out-file", "set-content", "add-content"):
                if op in low:
                    self.fail("plaintext secret writer on dpapi file at line %d: %s"
                              % (lineno, line.strip()))

    def test_work_root_whitespace_falls_back_to_default(self):
        main_region = self.source[self.source.index("# ------------------------------------------------------------------- main"):]
        default = '$WorkRoot = "D:\\work-of-jiaqi"'
        self.assertIn(default, main_region)
        self.assertLess(main_region.index("IsNullOrWhiteSpace($WorkRoot)"),
                        main_region.index(default))
        self.assertIn("IsNullOrWhiteSpace($env:BRIDGE_WORK_ROOT)", main_region)
        self.assertIn("$script:WorkRoot = $WorkRoot.Trim()", main_region)

    # -------- PS 5.1 object-shape regression (command/.Path + dead child) --
    def test_external_commands_resolve_to_plain_string_paths(self):
        # Get-Command returns different CommandInfo shapes on Windows
        # PowerShell 5.1 vs PowerShell 7. Every external command (python, py,
        # winget, codex, npm, ngrok) is resolved through Get-CommandPath,
        # which returns a plain string from the always-string Source /
        # Definition members with a guarded fallback; object-only members
        # such as .Path are never read from a command/process result.
        helper = self.source[self.source.index("function Get-CommandPath"):
                             self.source.index("function Refresh-PathFromRegistry")]
        self.assertIn("[string]$command.Source", helper)
        self.assertIn("[string]$command.Definition", helper)
        self.assertNotIn("$command.Path", helper)
        for name in ('"python.exe"', '"py.exe"', '"winget.exe"',
                     '"codex.exe"', '"codex.cmd"', '"npm.cmd"', '"ngrok.exe"'):
            self.assertIn("Get-CommandPath %s" % name, self.source,
                          "%s must be resolved via Get-CommandPath" % name)
        # the raw Get-Command ... .Source pattern is gone from call sites
        for name in ("python.exe", "py.exe", "winget.exe",
                     "codex.exe", "codex.cmd", "npm.cmd", "ngrok.exe"):
            self.assertNotIn("Get-Command %s" % name, self.source)

    def test_bridge_launch_guards_ps51_dead_child_path_error(self):
        # Windows PowerShell 5.1 Start-Process -PassThru throws "Property
        # 'Path' cannot be found on this object" when the child exits before
        # the wrapper is returned (PS 7.2.8+ instead returns an exited
        # Process). The bridge launch must catch that shape, never write a
        # pid file for a dead child, and surface logs + the failure site.
        ensure = self.source[self.source.index("function Ensure-Bridge"):
                             self.source.index("function Wait-LocalHealth")]
        start = ensure.index("Start-Process -FilePath")
        pid_write = ensure.index(
            '[System.IO.File]::WriteAllText($script:BridgePidFile, "$($proc.Id)")'
        )
        self.assertLess(ensure.index("try {"), start)
        self.assertLess(start, ensure.index("} catch {"))
        has_exited = ensure.index("$proc.HasExited")
        self.assertLess(has_exited, pid_write)
        catch_region = ensure[ensure.index("} catch {"):]
        self.assertIn("Write-FailDetail $startError", catch_region)
        self.assertIn("Show-BridgeLogTail", catch_region)
        self.assertIn("Exit-With 5", catch_region)
        # between the HasExited probe and the pid write: logs, then bail out
        immediate = ensure[has_exited:pid_write]
        self.assertIn("Show-BridgeLogTail", immediate)
        self.assertIn("Exit-With 5", immediate)
        # never read .Path off the Start-Process/Process result; the
        # launcher receives a plain [string] path
        self.assertNotIn("$proc.Path", ensure)
        self.assertIn("$pythonPath = [string]$python.Path", ensure)

    def test_ngrok_launch_guards_ps51_dead_child_path_error(self):
        # Same PS 5.1 dead-child guard for the ngrok launch.
        ngrok_phase = self.source[self.source.index("function Invoke-NgrokPhase"):
                                  self.source.index("function Invoke-Stop")]
        launch = ngrok_phase[ngrok_phase.index("starting ngrok:"):]
        self.assertIn("try {", launch)
        self.assertIn("} catch {", launch)
        self.assertLess(launch.index("try {"), launch.index("Start-Process -FilePath"))
        self.assertLess(launch.index("Start-Process -FilePath"),
                        launch.index("} catch {"))
        pid_write = ngrok_phase.index(
            '[System.IO.File]::WriteAllText($script:NgrokPidFile, "$($ngrokProc.Id)")'
        )
        has_exited = ngrok_phase.index("$ngrokProc.HasExited")
        self.assertLess(has_exited, pid_write)
        self.assertIn("Write-FailDetail $startError", ngrok_phase)
        self.assertNotIn("$ngrokProc.Path", ngrok_phase)
        immediate = ngrok_phase[has_exited:pid_write]
        self.assertIn("Show-NgrokLogTail", immediate)
        self.assertIn("Exit-With 6", immediate)

    def test_catch_diagnostics_report_line_and_stack_without_secrets(self):
        # Failure output must include the script line + PowerShell stack so
        # Windows PowerShell 5.1 errors are diagnosable, but only positions /
        # stack text are echoed: InvocationInfo.Line (raw argument text) is
        # deliberately never printed and no secret env value is interpolated.
        detail = self.source[self.source.index("function Write-FailDetail"):
                             self.source.index("function Require-EnvPath")]
        self.assertIn("ScriptLineNumber", detail)
        self.assertIn("ScriptName", detail)
        self.assertIn("ScriptStackTrace", detail)
        # no actual Line-text access may ever be echoed (only positions)
        self.assertNotIn("$Record.InvocationInfo.Line", detail)
        self.assertNotIn("$invocation.Line", detail)
        self.assertNotIn("$env:BRIDGE_API_KEY", detail)
        self.assertNotIn("$env:DEEPSEEK_API_KEY", detail)
        self.assertNotIn("$env:NGROK_AUTHTOKEN", detail)
        # every top-level failure path routes through the detail printer
        self.assertIn("Write-FailDetail $_", self.source)
        main_catch = self.source[self.source.rindex("} catch {"):]
        self.assertIn("Write-FailDetail $_", main_catch)
        self.assertIn("exit 1", main_catch)


if __name__ == "__main__":
    unittest.main()
