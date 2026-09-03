"""Windows platform defaults and Codex executable resolution.

macOS behavior never changes: the functions here are consulted only when the
bridge runs with ``os.name == "nt"`` (see ``http_server/server.py`` and
``bridge/client.py``), so the existing bash/launchd control plane keeps its
exact defaults.

Design rules
------------
- Pure functions that take an explicit ``env`` dict; Windows paths are built
  with :mod:`ntpath`, which is deterministic on every host, so unit tests
  can run anywhere (including Linux CI / macOS).
- Desktop vs Bridge isolation (dual-host-router): the Windows Desktop Codex
  keeps ``%USERPROFILE%\\.codex`` (restored to OpenAI by the bootstrap;
  auth.json/state/history are never read or touched), while the Bridge and
  the ``codex-deepseek.cmd`` CLI wrapper use a DEDICATED DeepSeek CODEX_HOME
  = ``%LOCALAPPDATA%\\local-codex-bridge\\codex-deepseek`` (the bootstrap
  migrates any existing DeepSeek config out of the Desktop profile into this
  directory). The bridge never reads ``auth.json``.
- Default Windows state root = ``%LOCALAPPDATA%\\local-codex-bridge\\<instance>``,
  the Windows analog of ``${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge``.
  ``BRIDGE_STATE_ROOT`` / ``XDG_STATE_HOME`` overrides keep working (tests and
  instance isolation rely on them).
- Default task work root = ``D:\\work-of-jiaqi`` (created by the PowerShell
  bootstrap; the task-cwd guard accepts any explicit project directory
  outside the control plane, so the work root needs no allowlist entry).
- Codex resolution order: ``CODEX_BIN`` override > ``%APPDATA%\\npm\\codex.cmd``
  (npm global install) > ``codex.exe`` on PATH (native install) >
  ``codex.cmd`` on PATH > bare ``codex`` on PATH.

``codex.cmd`` (npm shim) cannot be spawned directly with quoted argv: cmd.exe
re-tokenizes the command line and corrupts embedded double quotes, which the
bridge's ``-c`` TOML overrides always contain (e.g. ``model="deepseek-chat"``).
Instead the resolver locates the shim's ``node.exe`` and the real JS entry
from ``node_modules/@openai/codex/package.json`` ("bin"), so the bridge
spawns node directly and argv survives intact. Native ``codex.exe`` is
spawned directly.
"""

import json
import ntpath
import os
import shutil

# Windows-only. macOS keeps its own defaults in the callers (e.g.
# ~/.codex-deepseek), so this module never changes them.
DEFAULT_WINDOWS_WORK_ROOT = r"D:\work-of-jiaqi"
WINDOWS_CODEX_NPM_PACKAGE_DIR = ("node_modules", "@openai", "codex")

_EXECUTABLE_EXTS = (".exe", ".cmd", ".bat")


class CodexSpawnResolutionError(ValueError):
    """Raised when a Windows codex shim cannot be resolved to a spawnable
    executable. The message is actionable and never contains secrets."""


# --------------------------------------------------------------------------- env


def windows_profile(env):
    """Return the Windows per-user profile directories from an env dict.

    Raises ValueError with a clear message when USERPROFILE is unavailable
    (it always is on real Windows; the failure path exists for tests).
    """
    userprofile = env.get("USERPROFILE")
    if not userprofile:
        homedrive = env.get("HOMEDRIVE", "")
        homepath = env.get("HOMEPATH", "")
        userprofile = ntpath.join(homedrive, homepath) if homepath else ""
    if not userprofile:
        raise ValueError(
            "USERPROFILE is not set: cannot determine the Windows home "
            "directory (expected e.g. C:\\Users\\<name>)"
        )
    return {
        "userprofile": userprofile,
        "appdata": env.get("APPDATA") or ntpath.join(userprofile, "AppData", "Roaming"),
        "localappdata": env.get("LOCALAPPDATA") or ntpath.join(userprofile, "AppData", "Local"),
    }


def windows_defaults(env):
    """Windows default paths: codex_home (dedicated DeepSeek bridge profile),
    desktop_codex_home (%USERPROFILE%\\.codex, OpenAI Desktop profile),
    state_root_base and work_root."""
    profile = windows_profile(env)
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        state_root_base = ntpath.join(xdg, "local-codex-bridge")
    else:
        state_root_base = ntpath.join(profile["localappdata"], "local-codex-bridge")
    return {
        "codex_home": ntpath.join(
            profile["localappdata"], "local-codex-bridge", "codex-deepseek"
        ),
        "desktop_codex_home": ntpath.join(profile["userprofile"], ".codex"),
        "state_root_base": state_root_base,
        "work_root": env.get("BRIDGE_WORK_ROOT") or DEFAULT_WINDOWS_WORK_ROOT,
    }


# ------------------------------------------------------------------ codex find


def _which(name, env, which):
    path = env.get("PATH")
    if which is None:
        which = shutil.which
    return which(name, path=path)


def windows_codex_candidates(env):
    """Ordered list of codex locations probed on Windows (env-aware)."""
    profile = windows_profile(env)
    candidates = []
    override = env.get("CODEX_BIN")
    if override:
        candidates.append(override)
    candidates.append(ntpath.join(profile["appdata"], "npm", "codex.cmd"))
    return candidates


def detect_codex_binary(env, isfile=None, which=None):
    """Detect the Windows codex executable (absolute path or None).

    Order: CODEX_BIN override > %APPDATA%\\npm\\codex.cmd > codex.exe on PATH
    > codex.cmd on PATH > bare codex on PATH. ``isfile``/``which`` are
    injectable for tests.
    """
    if isfile is None:
        isfile = os.path.isfile
    for candidate in windows_codex_candidates(env):
        if candidate and isfile(candidate):
            return candidate
    for name in ("codex.exe", "codex.cmd", "codex"):
        found = _which(name, env, which)
        if found:
            return found
    return None


# --------------------------------------------------------------- npm shim path


def resolve_codex_shim(codex_bin, env=None, isfile=None, which=None, read_text=None):
    """Resolve an npm ``codex.cmd`` shim to a direct spawnable command head.

    Returns None when the shim is not an npm @openai/codex shim or its node
    runtime cannot be found (callers then fail with an actionable error).
    The returned head is one of:
      {"kind": "node", "node": <abs node.exe>, "entry": <abs codex.js>}
      {"kind": "exe", "path": <abs .exe>}   (package bin points at a binary)
    """
    if env is None:
        env = os.environ
    if isfile is None:
        isfile = os.path.isfile
    if read_text is None:
        read_text = _read_text
    shim_dir = ntpath.dirname(codex_bin)
    if not shim_dir:
        return None
    package_dir = ntpath.join(shim_dir, *WINDOWS_CODEX_NPM_PACKAGE_DIR)
    package_json = ntpath.join(package_dir, "package.json")
    if not isfile(package_json):
        return None
    try:
        meta = json.loads(read_text(package_json) or "{}")
    except ValueError:
        return None
    bin_value = meta.get("bin") or {}
    if isinstance(bin_value, dict):
        bin_value = bin_value.get("codex")
    if not isinstance(bin_value, str) or not bin_value:
        return None
    entry = ntpath.normpath(ntpath.join(package_dir, bin_value))
    if not isfile(entry):
        return None
    if entry.lower().endswith(".exe"):
        return {"kind": "exe", "path": entry}
    node = ntpath.join(shim_dir, "node.exe")
    if not isfile(node):
        node = _which("node.exe", env, which)
        if node is None:
            return None
    return {"kind": "node", "node": node, "entry": entry}


def _read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------- argv head


def codex_argv_head(codex_bin, extra_args, env=None, os_name=None,
                    isfile=None, which=None, read_text=None):
    """Return the argv list used to spawn the codex CLI on this host.

    ``os_name`` defaults to ``os.name``; pass ``"nt"`` explicitly in tests to
    exercise the Windows branch on any host. macOS/POSIX keeps the historic
    argv exactly: ``[codex_bin] + extra_args``.
    """
    if os_name is None:
        os_name = os.name
    env = os.environ if env is None else env
    if os_name != "nt" or not codex_bin.lower().endswith((".cmd", ".bat")):
        return [codex_bin] + list(extra_args)
    head = resolve_codex_shim(codex_bin, env=env, isfile=isfile, which=which,
                              read_text=read_text)
    if head is None:
        raise CodexSpawnResolutionError(
            "cannot spawn %s: it is a Windows cmd shim whose node runtime or "
            "npm package entry could not be resolved. Install the native "
            "Windows Codex CLI (powershell -ExecutionPolicy ByPass -c \"irm "
            "https://chatgpt.com/codex/install.ps1 | iex\"), or reinstall "
            "with npm (npm install -g @openai/codex) with Node.js on PATH, "
            "then restart the bridge" % codex_bin
        )
    if head["kind"] == "exe":
        argv_head = [head["path"]]
    else:
        argv_head = [head["node"], head["entry"]]
    return argv_head + list(extra_args)
