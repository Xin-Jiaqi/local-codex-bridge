#!/usr/bin/env python3
"""Offline tests for the stable non-Desktop runtime + launchd supervisor.

Covers (1.1.0 milestone):
  - install_runtime.sh / uninstall_runtime.sh: tracked allowlist only (no
    .git/tests/docs/backups/logs/secrets/domain), atomic `current` symlink,
    secret-free `.runtime-build-info` (HEAD/dirty/UTC time/version; manifest
    kept for compatibility), --dest test override, data root 700,
    keep-last-2 pruning with strict path+marker guard,
    repo->config-root credential PATH REFERENCE migration (dir 700 / files
    600, content never printed, repo originals untouched), uninstall
    refusing non-marker paths and preserving state + config credentials;
  - the per-instance launchd plist (com.local.codex-bridge.local): non-Desktop
    ProgramArguments (runtime current/scripts/run_local_supervisor.sh),
    RunAtLoad, KeepAlive PathState bound to the absolute supervisor.enabled
    sentinel, ThrottleInterval, no secrets/domain, no AbandonProcessGroup;
  - legacy LaunchAgent migration: the old com.local.codex-bridge plist is
    backed up (never deleted) and every launchctl call is label-precise
    (no wildcards / broad unload);
  - supervisor_control.sh: enable/disable/status, sentinel lifecycle,
    legacy fallback start, no launchd agent required;
  - run_local_supervisor.sh: explicit-local only, enabled/disabled, pause
    marker (maintenance window) stops children and waits without exiting,
    resume after marker removal, bridge crash restart, ngrok crash restart,
    network failure never kills, backoff retry, TERM + sentinel-removal
    graceful stop (exit 0, children stopped, pid file removed), runs from
    the installed runtime copy without the repo (no Desktop dependency);
  - maintenance cooperation: activation creates the pause marker BEFORE
    stopping local (sentinel stays; no launchd crash-loop), rollback clears
    the marker and restores the pre-window state, deactivation clears the
    marker and restores local for enabled/legacy windows and leaves local
    stopped for disabled windows;
  - invariants: no pkill/killall, no unbounded rm -rf, no hpc touch.
All writes go to temp dirs; no real secrets/domains; no live launchd/bridge.
"""

import json
import os
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "scripts", "bridge_instance_lib.sh")
ADMIN = os.path.join(ROOT, "scripts", "bridge_instance.sh")
INSTALL_RT = os.path.join(ROOT, "scripts", "install_runtime.sh")
UNINSTALL_RT = os.path.join(ROOT, "scripts", "uninstall_runtime.sh")
SUPERVISOR = os.path.join(ROOT, "scripts", "run_local_supervisor.sh")
CONTROL = os.path.join(ROOT, "scripts", "supervisor_control.sh")
INSTALL_AGENT = os.path.join(ROOT, "scripts", "install_launch_agent.sh")
UNINSTALL_AGENT = os.path.join(ROOT, "scripts", "uninstall_launch_agent.sh")
LOCAL_PLIST = os.path.join(ROOT, "scripts", "launch_agent",
                           "com.local.codex-bridge.local.plist")

DUMMY_KEY = "dummy-api-key-not-a-real-secret"
DUMMY_DOMAIN = "runtime-test.invalid"


def run_bash(script, env_extra=None, cwd=None, args=()):
    env = dict(os.environ)
    for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT", "BRIDGE_DATA_ROOT",
                "BRIDGE_CONFIG_ROOT", "XDG_STATE_HOME", "XDG_DATA_HOME",
                "XDG_CONFIG_HOME", "BRIDGE_SANDBOX_MODE",
                "BRIDGE_SANDBOX_MODE_FILE", "NGROK_DOMAIN",
                "SUPERVISOR_AGENT_LABEL", "KEEP_RELEASES"):
        env.pop(key, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", script, "test-sh", *args],
        capture_output=True, text=True, env=env, cwd=cwd,
    )


def run_admin(args, state_root, env_extra=None):
    env = dict(os.environ)
    for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT", "XDG_STATE_HOME",
                "BRIDGE_SANDBOX_MODE", "BRIDGE_SANDBOX_MODE_FILE"):
        env.pop(key, None)
    env["BRIDGE_STATE_ROOT"] = state_root
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", ADMIN] + list(args),
        capture_output=True, text=True, env=env, cwd=ROOT,
    )


def env_for(state, data=None, config=None, home=None, **extra):
    env = dict(os.environ)
    for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT", "BRIDGE_DATA_ROOT",
                "BRIDGE_CONFIG_ROOT", "XDG_STATE_HOME", "XDG_DATA_HOME",
                "XDG_CONFIG_HOME", "BRIDGE_SANDBOX_MODE",
                "BRIDGE_SANDBOX_MODE_FILE", "NGROK_DOMAIN",
                "SUPERVISOR_AGENT_LABEL"):
        env.pop(key, None)
    env.update({
        "BRIDGE_STATE_ROOT": state,
        "SUPERVISOR_AGENT_LABEL": "com.local.codex-bridge.testonly",
        "SUPERVISOR_POLL_SECS": "1",
        "SUPERVISOR_BACKOFF_SECS": "0",
    })
    if data:
        env["BRIDGE_DATA_ROOT"] = data
    if config:
        env["BRIDGE_CONFIG_ROOT"] = config
    if home:
        env["HOME"] = home
    env.update(extra)
    return env


def make_fake_repo(d, seed_secrets=True):
    """Mini git repo with the real runtime files + functional fake
    start/stop/curl helpers (committed), plus dummy secret/domain files."""
    repo = os.path.join(d, "repo")
    os.makedirs(os.path.join(repo, "scripts"))
    for name in ("bridge", "http_server", "config"):
        shutil.copytree(os.path.join(ROOT, name), os.path.join(repo, name))
    for f in os.listdir(os.path.join(ROOT, "scripts")):
        if f.endswith(".bak") or f == "__pycache__":
            continue
        src = os.path.join(ROOT, "scripts", f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(repo, "scripts", f))
    shutil.copytree(os.path.join(ROOT, "scripts", "launch_agent"),
                    os.path.join(repo, "scripts", "launch_agent"))
    for root, dirs, files in os.walk(repo):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            if f.endswith((".pyc", ".bak")):
                os.remove(os.path.join(root, f))
    if seed_secrets:
        with open(os.path.join(repo, ".bridge_api_key"), "w") as fh:
            fh.write(DUMMY_KEY + "\n")
        with open(os.path.join(repo, ".ngrok_domain"), "w") as fh:
            fh.write(DUMMY_DOMAIN + "\n")
    subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-q", "-m", "seed"], check=True)
    return repo


def install_fakes(repo, call_log, fail_start=False):
    """Functional fake start/stop/curl used by supervisor/maintenance tests."""
    stop = os.path.join(repo, "scripts", "stop_ngrok_bridge.sh")
    start = os.path.join(repo, "scripts", "start_ngrok_bridge.sh")
    with open(stop, "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'echo "${BRIDGE_INSTANCE:-<unset>} stop" >> "${FAKE_CALL_LOG:?}"\n'
                 'RUNTIME_DIR="${BRIDGE_STATE_ROOT}/local/runtime"\n'
                 'for f in bridge.pid ngrok.pid; do\n'
                 '  pid="$(cat "$RUNTIME_DIR/$f" 2>/dev/null || true)"\n'
                 '  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then\n'
                 '    kill "$pid" 2>/dev/null || true\n'
                 '  fi\n'
                 '  rm -f "$RUNTIME_DIR/$f"\n'
                 'done\n'
                 'exit 0\n')
    with open(start, "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'echo "${BRIDGE_INSTANCE:-<unset>} start" >> "${FAKE_CALL_LOG:?}"\n'
                 'if [[ -n "${FAKE_FAIL_START:-}" ]]; then\n'
                 '  exit 1\n'
                 'fi\n'
                 'RUNTIME_DIR="${BRIDGE_STATE_ROOT}/local/runtime"\n'
                 'mkdir -p "$RUNTIME_DIR"\n'
                 'bpid="$(cat "$RUNTIME_DIR/bridge.pid" 2>/dev/null || true)"\n'
                 'if [[ -z "$bpid" ]] || ! kill -0 "$bpid" 2>/dev/null; then\n'
                 '  nohup python3 -c \'import time; time.sleep(600)\' http_server \\\n'
                 '    --log "$RUNTIME_DIR/bridge.log" >/dev/null 2>&1 &\n'
                 '  echo $! > "$RUNTIME_DIR/bridge.pid"\n'
                 'fi\n'
                 'npid="$(cat "$RUNTIME_DIR/ngrok.pid" 2>/dev/null || true)"\n'
                 'if [[ -z "$npid" ]] || ! kill -0 "$npid" 2>/dev/null; then\n'
                 '  nohup python3 -c \'import time; time.sleep(600)\' ngrok \\\n'
                 '    http 8321 >/dev/null 2>&1 &\n'
                 '  echo $! > "$RUNTIME_DIR/ngrok.pid"\n'
                 'fi\n'
                 'exit 0\n')
    bindir = os.path.join(repo, "bin")
    os.makedirs(bindir, exist_ok=True)
    with open(os.path.join(bindir, "launchctl"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'case "$1" in\n'
                 '  print) exit 1 ;;\n'
                 '  *) echo "launchctl $*" >> "${FAKE_CALL_LOG:?}"; exit 0 ;;\n'
                 'esac\n')
    with open(os.path.join(bindir, "curl"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'port="8321"\n'
                 'for arg in "$@"; do\n'
                 '  case "$arg" in\n'
                 '    http://127.0.0.1:*) port="${arg#http://127.0.0.1:}"; port="${port%%/*}" ;;\n'
                 '  esac\n'
                 'done\n'
                 'if [[ "$port" == "8323" ]]; then instance="maintenance"; else instance="local"; fi\n'
                 'printf \'{"status":"ok","instance":"%s","mode":"bridge-workspace","port":%s}\\n\' \\\n'
                 '  "$instance" "$port"\n'
                 'exit 0\n')
    for f in (stop, start, os.path.join(bindir, "curl"),
              os.path.join(bindir, "launchctl")):
        os.chmod(f, 0o755)
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-q", "-m", "fakes"], check=True)


def local_runtime(state):
    return os.path.join(state, "local", "runtime")


def sentinel(state):
    return os.path.join(local_runtime(state), "supervisor.enabled")


def wait_for(cond, timeout=15, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def calls(call_log):
    if not os.path.exists(call_log):
        return []
    return [ln for ln in open(call_log, encoding="utf-8").read().splitlines() if ln]


def read_pid(state, name):
    path = os.path.join(local_runtime(state), name + ".pid")
    if not os.path.exists(path):
        return ""
    return open(path, encoding="utf-8").read().strip()


def pid_alive(pid):
    if not pid or not re.match(r"^\d+$", str(pid)):
        return False


def ps_available():
    try:
        proc = subprocess.run(["ps", "-p", "1", "-o", "command="],
                              capture_output=True, text=True, timeout=5)
        return proc.returncode == 0
    except Exception:
        return False


def repo_script(repo, name):
    return os.path.join(repo, "scripts", name)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class RuntimeInstallTest(unittest.TestCase):

    def _install(self, repo, state, data, config, home, args=(), extra=None):
        env = env_for(state, data, config, home)
        if extra:
            env.update(extra)
        env["PATH"] = os.path.join(repo, "bin") + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            ["bash", repo_script(repo, "install_runtime.sh"), "--instance", "local", *args],
            capture_output=True, text=True, env=env, cwd=repo,
        )

    def _make(self, keep=2):
        d = tempfile.mkdtemp(prefix="lcb-runtime-")
        self.addCleanup(shutil.rmtree, d, True)
        repo = make_fake_repo(d)
        install_fakes(repo, os.path.join(d, "calls.log"))
        state = os.path.join(d, "state")
        run_admin(["create", "local"], state)
        home = os.path.join(d, "home")
        os.makedirs(home)
        data = os.path.join(d, "data")
        config = os.path.join(d, "config")
        return d, repo, state, data, config, home

    def test_install_allowlist_and_atomic_current(self):
        d, repo, state, data, config, home = self._make()
        proc = self._install(repo, state, data, config, home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        current = os.path.join(data, "current")
        self.assertTrue(os.path.islink(current), current)
        release_dir = os.readlink(current)
        self.assertTrue(release_dir.startswith(os.path.join(data, "releases", "release-")),
                        release_dir)
        # allowlisted runtime files present
        for rel in ("bridge/__init__.py", "http_server/server.py",
                    "scripts/start_ngrok_bridge.sh",
                    "scripts/run_local_supervisor.sh",
                    "scripts/supervisor_control.sh",
                    "scripts/bridge_instance_lib.sh",
                    ".runtime-build-info", "runtime.manifest"):
            self.assertTrue(os.path.exists(os.path.join(current, rel)), rel)
        # data root + releases dir are 700 (off-Desktop runtime tree)
        for d in (data, os.path.join(data, "releases")):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)
        # excluded: no .git / tests / docs / backups / logs / secrets / domain
        for rel in (".git", "tests", "docs", "CHANGELOG.md", "README.md",
                    ".bridge_api_key", ".ngrok_domain", ".public_url",
                    "openapi.yaml", "scripts/start_ngrok_bridge.sh.bak-1",
                    "__pycache__"):
            self.assertFalse(os.path.exists(os.path.join(current, rel)), rel)
        # supervisor script is executable
        self.assertTrue(os.access(os.path.join(current, "scripts",
                                               "run_local_supervisor.sh"), os.X_OK))

    def test_install_manifest_secret_free(self):
        d, repo, state, data, config, home = self._make()
        proc = self._install(repo, state, data, config, home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for manifest in ("runtime.manifest", ".runtime-build-info"):
            path = os.path.join(data, "current", manifest)
            text = open(path, encoding="utf-8").read()
            self.assertIn("release=release-", text)
            self.assertIn("head=", text)
            self.assertIn("dirty=", text)
            self.assertIn("time=", text)
            self.assertIn("allowlist_files=", text)
            if manifest == ".runtime-build-info":
                self.assertIn("version=", text)
                self.assertRegex(text, r"version=1\.1\.0")
            self.assertNotIn(DUMMY_KEY, text)
            self.assertNotIn(DUMMY_DOMAIN, text)
            self.assertNotIn(repo, text)  # no private source path
        self.assertNotIn(DUMMY_KEY, proc.stdout + proc.stderr)
        self.assertNotIn(DUMMY_DOMAIN, proc.stdout + proc.stderr)

    def test_install_dest_flag_overrides_data_root(self):
        d, repo, state, data, config, home = self._make()
        alt = os.path.join(d, "alt-dest")
        proc = self._install(repo, state, data, config, home,
                             args=("--dest", alt))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.islink(os.path.join(alt, "current")), alt)
        self.assertFalse(os.path.exists(os.path.join(data, "current")))
        self.assertEqual(stat.S_IMODE(os.stat(alt).st_mode), 0o700, alt)
        # env override still works when --dest is absent
        proc = self._install(repo, state, data, config, home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.islink(os.path.join(data, "current")))
        proc = subprocess.run(
            ["bash", repo_script(repo, "install_runtime.sh"),
             "--instance", "local", "--dest", "relative"],
            capture_output=True, text=True,
            env=env_for(state, data, config, home), cwd=repo)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--dest must be an absolute path", proc.stderr)

    def test_uninstall_refuses_unmarked_release_dir(self):
        d, repo, state, data, config, home = self._make()
        self._install(repo, state, data, config, home)
        releases = os.path.join(data, "releases")
        # a decoy dir matching the release pattern but without the managed
        # marker must NEVER be deleted by uninstall
        decoy = os.path.join(releases,
                             "release-20260101T000000Z-deadbeef1234")
        os.makedirs(decoy)
        with open(os.path.join(decoy, "junk.txt"), "w") as fh:
            fh.write("keep me\n")
        proc = subprocess.run(
            ["bash", repo_script(repo, "uninstall_runtime.sh")],
            capture_output=True, text=True, env=env_for(state, data, config, home),
            cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isfile(os.path.join(decoy, "junk.txt")),
                        "unmarked release dir must be preserved")
        # real managed releases were removed
        self.assertFalse(os.path.islink(os.path.join(data, "current")))
        self.assertEqual([x for x in os.listdir(releases)
                          if x.startswith("release-")], ["release-20260101T000000Z-deadbeef1234"])

    def test_install_keeps_last_two_releases(self):
        d, repo, state, data, config, home = self._make()
        for _ in range(3):
            proc = self._install(repo, state, data, config, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            time.sleep(1.1)
        releases = [x for x in os.listdir(os.path.join(data, "releases"))
                    if x.startswith("release-")]
        self.assertEqual(len(releases), 2, releases)
        current = os.readlink(os.path.join(data, "current"))
        self.assertEqual(current, os.path.join(data, "releases", max(releases)))

    def test_install_prune_guard_keeps_foreign_entries(self):
        d, repo, state, data, config, home = self._make()
        releases_dir = os.path.join(data, "releases")
        os.makedirs(releases_dir, exist_ok=True)
        with open(os.path.join(releases_dir, "important.txt"), "w") as fh:
            fh.write("keep me\n")
        os.makedirs(os.path.join(releases_dir, "other"), exist_ok=False)
        for _ in range(2):
            proc = self._install(repo, state, data, config, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            time.sleep(1.1)
        # decoys are never touched by install pruning
        self.assertTrue(os.path.isfile(os.path.join(releases_dir, "important.txt")))
        self.assertTrue(os.path.isdir(os.path.join(releases_dir, "other")))

    def test_install_credential_migration_permissions_and_no_leak(self):
        d, repo, state, data, config, home = self._make()
        proc = self._install(repo, state, data, config, home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(DUMMY_KEY, proc.stdout + proc.stderr)
        self.assertNotIn(DUMMY_DOMAIN, proc.stdout + proc.stderr)
        cfg_dir = config  # BRIDGE_CONFIG_ROOT overrides the whole config root
        self.assertEqual(stat.S_IMODE(os.stat(cfg_dir).st_mode), 0o700, cfg_dir)
        for name in ("api_key", "ngrok_domain"):
            path = os.path.join(cfg_dir, name)
            self.assertTrue(os.path.isfile(path), path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
        self.assertEqual(open(os.path.join(cfg_dir, "api_key"),
                              encoding="utf-8").read().strip(), DUMMY_KEY)
        # instance config now references the config root (paths only)
        cfg = os.path.join(state, "local", "instance.conf")
        text = open(cfg, encoding="utf-8").read()
        self.assertIn('api_key_file = "%s/api_key"' % cfg_dir, text)
        self.assertIn('ngrok_domain_file = "%s/ngrok_domain"' % cfg_dir, text)
        self.assertNotIn(DUMMY_KEY, text)
        self.assertNotIn(DUMMY_DOMAIN, text)
        # repo originals untouched
        self.assertEqual(open(os.path.join(repo, ".bridge_api_key"),
                              encoding="utf-8").read().strip(), DUMMY_KEY)

    def test_install_refuses_non_local(self):
        d, repo, state, data, config, home = self._make()
        env = env_for(state, data, config, home)
        for inst in ("hpc", "maintenance"):
            proc = subprocess.run(
                ["bash", INSTALL_RT, "--instance", inst],
                capture_output=True, text=True, env=env, cwd=repo)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("only supports the explicit local", proc.stderr)

    def test_uninstall_preserves_state_and_credentials(self):
        d, repo, state, data, config, home = self._make()
        self._install(repo, state, data, config, home)
        env = env_for(state, data, config, home)
        proc = subprocess.run(
            ["bash", repo_script(repo, "uninstall_runtime.sh")],
                              capture_output=True, text=True, env=env, cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.islink(os.path.join(data, "current")))
        self.assertEqual([x for x in os.listdir(os.path.join(data, "releases"))
                          if x.startswith("release-")], [])
        # state + config-root credentials preserved
        self.assertTrue(os.path.isfile(os.path.join(state, "local", "instance.conf")))
        self.assertTrue(os.path.isfile(os.path.join(
            config, "api_key")))
        self.assertTrue(os.path.isfile(os.path.join(
            config, "ngrok_domain")))


class LocalPlistTest(unittest.TestCase):

    def test_local_plist_template_launchd_safe(self):
        with open(LOCAL_PLIST, "rb") as fh:
            plist = plistlib.load(fh)
        args = plist["ProgramArguments"]
        self.assertEqual(args[0], "/bin/bash")
        self.assertIn("__DATA_ROOT__/current/scripts/run_local_supervisor.sh", args)
        self.assertIn("--instance", args)
        self.assertIn("local", args)
        self.assertTrue(plist["RunAtLoad"])
        keep = plist["KeepAlive"]
        self.assertIsInstance(keep, dict)
        path_state = keep["PathState"]
        self.assertEqual(list(path_state), ["__STATE_RUNTIME__/supervisor.enabled"])
        self.assertTrue(path_state["__STATE_RUNTIME__/supervisor.enabled"])
        self.assertEqual(plist["ThrottleInterval"], 10)
        self.assertNotIn("AbandonProcessGroup", plist)
        self.assertNotIn("__PROJECT_ROOT__", str(args))

    def test_generated_plist_non_desktop_and_secret_free(self):
        d = tempfile.mkdtemp(prefix="lcb-plist-")
        self.addCleanup(shutil.rmtree, d, True)
        state = os.path.join(d, "state")
        data = os.path.join(d, "data")
        home = os.path.join(d, "home")
        os.makedirs(home)
        run_admin(["create", "local"], state)
        # fake runtime install so the agent install can resolve current
        repo = make_fake_repo(d)
        install_fakes(repo, os.path.join(d, "calls.log"))
        runtime = os.path.join(data, "releases", "release-20260101T000000Z-deadbeef1234")
        os.makedirs(os.path.join(runtime, "scripts"), exist_ok=True)
        for rel in ("scripts/run_local_supervisor.sh",):
            src = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(os.path.join(runtime, rel)), exist_ok=True)
            shutil.copy2(src, os.path.join(runtime, rel))
        with open(os.path.join(runtime, ".runtime-build-info"), "w") as fh:
            fh.write("release=release-20260101T000000Z-deadbeef1234\n"
                     "head=deadbeef1234\ndirty=no\n"
                     "time=2026-01-01T00:00:00Z\nversion=1.1.0\n")
        os.symlink(runtime, os.path.join(data, "current"))
        env = env_for(state, data, home=home)
        env["PATH"] = os.path.join(repo, "bin") + os.pathsep + env.get("PATH", "")
        env["FAKE_CALL_LOG"] = os.path.join(d, "calls.log")
        proc = subprocess.run(
            ["bash", repo_script(repo, "install_launch_agent.sh"), "--instance", "local"],
            capture_output=True, text=True, env=env, cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plist_path = os.path.join(home, "Library", "LaunchAgents",
                                  "com.local.codex-bridge.local.plist")
        self.assertTrue(os.path.isfile(plist_path), plist_path)
        with open(plist_path, "rb") as fh:
            plist = plistlib.load(fh)
        args = plist["ProgramArguments"]
        self.assertEqual(args[1], os.path.join(data, "current",
                                               "scripts", "run_local_supervisor.sh"))
        self.assertNotIn(repo, args[1])  # never the Desktop/repo path
        state_runtime = os.path.join(state, "local", "runtime")
        self.assertEqual(list(plist["KeepAlive"]["PathState"]),
                         [os.path.join(state_runtime, "supervisor.enabled")])
        self.assertEqual(os.path.join(state_runtime, "supervisor.enabled"),
                         list(plist["KeepAlive"]["PathState"])[0])
        raw = open(plist_path, encoding="utf-8").read()
        self.assertNotIn(DUMMY_KEY, raw)
        self.assertNotIn(DUMMY_DOMAIN, raw)
        # install created the sentinel (supervision enabled)
        self.assertTrue(os.path.isfile(os.path.join(state_runtime,
                                                    "supervisor.enabled")))

    def test_install_agent_precise_legacy_migration(self):
        d = tempfile.mkdtemp(prefix="lcb-legacy-migrate-")
        self.addCleanup(shutil.rmtree, d, True)
        state = os.path.join(d, "state")
        data = os.path.join(d, "data")
        home = os.path.join(d, "home")
        os.makedirs(home)
        os.makedirs(os.path.join(home, "Library", "LaunchAgents"))
        run_admin(["create", "local"], state)
        repo = make_fake_repo(d)
        install_fakes(repo, os.path.join(d, "calls.log"))
        runtime = os.path.join(data, "releases", "release-20260101T000000Z-deadbeef1234")
        os.makedirs(os.path.join(runtime, "scripts"), exist_ok=True)
        for rel in ("scripts/run_local_supervisor.sh",):
            src = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(os.path.join(runtime, rel)), exist_ok=True)
            shutil.copy2(src, os.path.join(runtime, rel))
        with open(os.path.join(runtime, ".runtime-build-info"), "w") as fh:
            fh.write("release=release-20260101T000000Z-deadbeef1234\n"
                     "version=1.1.0\n")
        os.symlink(runtime, os.path.join(data, "current"))
        legacy_plist = os.path.join(home, "Library", "LaunchAgents",
                                    "com.local.codex-bridge.plist")
        with open(legacy_plist, "w") as fh:
            fh.write("<?xml version=\"1.0\"?>\n<plist version=\"1.0\"><dict>"
                     "<key>Label</key><string>com.local.codex-bridge</string>"
                     "</dict></plist>\n")
        env = env_for(state, data, home=home)
        env["PATH"] = os.path.join(repo, "bin") + os.pathsep + env.get("PATH", "")
        env["FAKE_CALL_LOG"] = os.path.join(d, "calls.log")
        proc = subprocess.run(
            ["bash", repo_script(repo, "install_launch_agent.sh"), "--instance", "local"],
            capture_output=True, text=True, env=env, cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # legacy plist is BACKED UP, never deleted, and the local agent is
        # installed under the new label
        backups = [f for f in os.listdir(os.path.join(home, "Library", "LaunchAgents"))
                   if f.startswith("com.local.codex-bridge.plist.bak-")]
        self.assertEqual(len(backups), 1, backups)
        self.assertFalse(os.path.exists(legacy_plist))
        self.assertTrue(os.path.isfile(os.path.join(
            home, "Library", "LaunchAgents", "com.local.codex-bridge.local.plist")))
        # every launchctl call is label-precise: no wildcards / broad unload
        for line in calls(os.path.join(d, "calls.log")):
            if line.startswith("launchctl "):
                self.assertNotIn("*", line)
                self.assertNotIn("unload", line)
                self.assertTrue(
                    "com.local.codex-bridge" in line and
                    line.count("com.local.codex-bridge") <= 2,
                    line)

    def test_install_agent_requires_runtime_first(self):
        d = tempfile.mkdtemp(prefix="lcb-agent-")
        self.addCleanup(shutil.rmtree, d, True)
        state = os.path.join(d, "state")
        data = os.path.join(d, "data")
        home = os.path.join(d, "home")
        os.makedirs(home)
        run_admin(["create", "local"], state)
        repo = make_fake_repo(d)
        env = env_for(state, data, home=home)
        env["PATH"] = os.path.join(repo, "bin") + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            ["bash", repo_script(repo, "install_launch_agent.sh"), "--instance", "local"],
            capture_output=True, text=True, env=env, cwd=repo)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("install_runtime.sh --instance local", proc.stderr)

    def test_uninstall_agent_removes_sentinel_and_keeps_state(self):
        d = tempfile.mkdtemp(prefix="lcb-uninst-")
        self.addCleanup(shutil.rmtree, d, True)
        repo = make_fake_repo(d)
        install_fakes(repo, os.path.join(d, "calls.log"))
        state = os.path.join(d, "state")
        home = os.path.join(d, "home")
        os.makedirs(home)
        run_admin(["create", "local"], state)
        sent = sentinel(state)
        os.makedirs(os.path.dirname(sent), exist_ok=True)
        with open(sent, "w") as fh:
            fh.write("")
        pause = os.path.join(state, "local", "pause.marker")
        with open(pause, "w") as fh:
            fh.write("stale window hold\n")
        env = env_for(state, home=home)
        env["PATH"] = os.path.join(repo, "bin") + os.pathsep + "/usr/bin:/bin"
        proc = subprocess.run(
            ["bash", repo_script(repo, "uninstall_launch_agent.sh"), "--instance", "local"],
            capture_output=True, text=True, env=env, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(sent))
        self.assertFalse(os.path.exists(pause),
                         "stale pause marker cleared on uninstall")
        self.assertTrue(os.path.isfile(os.path.join(state, "local",
                                                    "instance.conf")))


class SupervisorControlTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="lcb-ctl-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.repo = make_fake_repo(self.d)
        self.call_log = os.path.join(self.d, "calls.log")
        install_fakes(self.repo, self.call_log)
        self.state = os.path.join(self.d, "state")
        run_admin(["create", "local"], self.state)
        self.env = env_for(self.state)
        self.env["FAKE_CALL_LOG"] = self.call_log
        self.env["PATH"] = os.path.join(self.repo, "bin") + os.pathsep + \
            self.env.get("PATH", "")

    def _run(self, *args):
        return subprocess.run(
            ["bash", repo_script(self.repo, "supervisor_control.sh"), *args],
            capture_output=True, text=True,
            env=self.env, cwd=self.repo)

    def test_enable_creates_sentinel_and_legacy_start(self):
        proc = self._run("enable")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isfile(sentinel(self.state)))
        self.assertEqual(calls(self.call_log), ["local start"])
        self.assertIn("falling back to the legacy start flow", proc.stdout)

    def test_disable_removes_sentinel_and_stops(self):
        self._run("enable")
        self._run("disable", "--stop")
        self.assertFalse(os.path.exists(sentinel(self.state)))
        self.assertIn("local stop", calls(self.call_log))

    def test_status_no_secrets(self):
        self._run("enable")
        proc = self._run("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("supervisor: enabled", proc.stdout)
        self.assertIn("release: none", proc.stdout)
        self.assertNotIn(DUMMY_KEY, proc.stdout)
        self.assertNotIn(DUMMY_DOMAIN, proc.stdout)
        self._run("disable")
        proc = self._run("status")
        self.assertIn("supervisor: disabled", proc.stdout)


class SupervisorRuntimeTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="lcb-sup-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.repo = make_fake_repo(self.d)
        self.call_log = os.path.join(self.d, "calls.log")
        install_fakes(self.repo, self.call_log)
        self.state = os.path.join(self.d, "state")
        run_admin(["create", "local"], self.state)
        self.env = env_for(self.state)
        self.env["FAKE_CALL_LOG"] = self.call_log
        self.env["PATH"] = os.path.join(self.repo, "bin") + os.pathsep + \
            self.env.get("PATH", "")

    def _spawn(self, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.Popen(
            ["bash", repo_script(self.repo, "run_local_supervisor.sh"),
             "--instance", "local"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, cwd=self.repo)

    def setUp(self):
        super().setUp()
        if not ps_available():
            self.skipTest("ps is unavailable in this sandbox; live supervisor "
                          "identity checks cannot run (CI has ps)")

    def test_refuses_hpc_maintenance_and_legacy(self):
        for inst in ("hpc", "maintenance"):
            proc = subprocess.run(
                ["bash", repo_script(self.repo, "run_local_supervisor.sh"),
                 "--instance", inst],
                                  capture_output=True, text=True,
                                  env=self.env, cwd=self.repo)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("only manages the explicit local", proc.stderr)
        proc = subprocess.run(
            ["bash", repo_script(self.repo, "run_local_supervisor.sh")],
            capture_output=True, text=True, env=self.env, cwd=self.repo)
        self.assertNotEqual(proc.returncode, 0)

    def test_disabled_exits_zero_without_start(self):
        proc = subprocess.run(
            ["bash", repo_script(self.repo, "run_local_supervisor.sh"),
             "--instance", "local"],
                              capture_output=True, text=True,
                              env=self.env, cwd=self.repo)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(calls(self.call_log), [])

    def test_supervisor_runs_and_terms_gracefully(self):
        with open(sentinel(self.state), "w") as fh:
            fh.write("")
        proc = self._spawn()
        try:
            self.assertTrue(wait_for(
                lambda: pid_alive(read_pid(self.state, "bridge")) and
                pid_alive(read_pid(self.state, "ngrok"))), "children up")
            spid = read_pid(self.state, "supervisor")
            self.assertTrue(spid, "supervisor.pid written")
            self.assertTrue(pid_alive(spid), "supervisor alive")
            os.kill(int(spid), signal.SIGTERM)
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("local start", calls(self.call_log))
        self.assertIn("local stop", calls(self.call_log))
        self.assertFalse(os.path.exists(os.path.join(
            local_runtime(self.state), "supervisor.pid")))
        self.assertFalse(pid_alive(read_pid(self.state, "bridge")))

    def test_supervisor_restarts_bridge_crash(self):
        with open(sentinel(self.state), "w") as fh:
            fh.write("")
        proc = self._spawn()
        try:
            self.assertTrue(wait_for(
                lambda: pid_alive(read_pid(self.state, "bridge"))))
            first = read_pid(self.state, "bridge")
            os.kill(int(first), signal.SIGTERM)
            self.assertTrue(wait_for(
                lambda: read_pid(self.state, "bridge") and
                read_pid(self.state, "bridge") != first and
                pid_alive(read_pid(self.state, "bridge")), timeout=20),
                "bridge restarted with a new pid")
            self.assertGreaterEqual(len([c for c in calls(self.call_log)
                                         if c == "local start"]), 2)
        finally:
            if proc.poll() is None:
                os.kill(int(read_pid(self.state, "supervisor")), signal.SIGTERM)
                proc.wait(timeout=15)

    def test_supervisor_restarts_ngrok_crash(self):
        with open(sentinel(self.state), "w") as fh:
            fh.write("")
        proc = self._spawn()
        try:
            self.assertTrue(wait_for(
                lambda: pid_alive(read_pid(self.state, "ngrok"))))
            first = read_pid(self.state, "ngrok")
            os.kill(int(first), signal.SIGTERM)
            self.assertTrue(wait_for(
                lambda: read_pid(self.state, "ngrok") and
                read_pid(self.state, "ngrok") != first and
                pid_alive(read_pid(self.state, "ngrok")), timeout=20),
                "ngrok restarted with a new pid")
        finally:
            if proc.poll() is None:
                os.kill(int(read_pid(self.state, "supervisor")), signal.SIGTERM)
                proc.wait(timeout=15)

    def test_network_failure_never_kills_children(self):
        with open(sentinel(self.state), "w") as fh:
            fh.write("")
        proc = self._spawn()
        try:
            self.assertTrue(wait_for(
                lambda: pid_alive(read_pid(self.state, "bridge")) and
                pid_alive(read_pid(self.state, "ngrok"))))
            time.sleep(3)  # several polls with healthy children
            self.assertEqual([c for c in calls(self.call_log) if c.endswith(" stop")],
                             [], "supervisor must not stop healthy children")
            self.assertTrue(pid_alive(read_pid(self.state, "bridge")))
            self.assertTrue(pid_alive(read_pid(self.state, "ngrok")))
        finally:
            if proc.poll() is None:
                os.kill(int(read_pid(self.state, "supervisor")), signal.SIGTERM)
                proc.wait(timeout=15)

    def test_pause_marker_stops_children_and_waits_then_resumes(self):
        with open(sentinel(self.state), "w") as fh:
            fh.write("")
        proc = self._spawn()
        try:
            self.assertTrue(wait_for(
                lambda: pid_alive(read_pid(self.state, "bridge")) and
                pid_alive(read_pid(self.state, "ngrok"))))
            spid = read_pid(self.state, "supervisor")
            self.assertTrue(spid, "supervisor pid present")
            pause = os.path.join(self.state, "local", "pause.marker")
            with open(pause, "w") as fh:
                fh.write("maintenance window\n")
            # supervisor notices the marker and stops the children but stays
            # alive (sentinel untouched -> no launchd crash-loop)
            self.assertTrue(wait_for(
                lambda: not pid_alive(read_pid(self.state, "bridge")) and
                not pid_alive(read_pid(self.state, "ngrok")), timeout=15),
                "children stopped while paused")
            self.assertTrue(pid_alive(int(spid)), "supervisor stays alive while paused")
            time.sleep(2)  # several polls while paused
            self.assertEqual([c for c in calls(self.call_log) if c == "local start"],
                             ["local start"], "no restarts while paused")
            self.assertTrue(os.path.exists(sentinel(self.state)),
                            "sentinel kept during the window")
            # resume: marker removed -> supervisor restarts the stack
            os.remove(pause)
            self.assertTrue(wait_for(
                lambda: pid_alive(read_pid(self.state, "bridge")) and
                pid_alive(read_pid(self.state, "ngrok")), timeout=15),
                "children resumed after marker removal")
            self.assertGreaterEqual(
                len([c for c in calls(self.call_log) if c == "local start"]), 2)
        finally:
            if proc.poll() is None:
                os.kill(int(read_pid(self.state, "supervisor")), signal.SIGTERM)
                proc.wait(timeout=15)

    def test_pause_marker_at_startup_holds_children(self):
        with open(sentinel(self.state), "w") as fh:
            fh.write("")
        pause = os.path.join(self.state, "local", "pause.marker")
        with open(pause, "w") as fh:
            fh.write("maintenance window\n")
        proc = self._spawn()
        try:
            self.assertTrue(wait_for(
                lambda: read_pid(self.state, "supervisor") and
                pid_alive(read_pid(self.state, "supervisor")), timeout=15),
                "supervisor alive at startup while paused")
            time.sleep(2)
            self.assertIsNone(proc.poll(), "supervisor stays up while paused")
            self.assertNotIn("local start", calls(self.call_log))
            self.assertFalse(pid_alive(read_pid(self.state, "bridge")))
            os.remove(pause)
            self.assertTrue(wait_for(
                lambda: pid_alive(read_pid(self.state, "bridge")), timeout=15),
                "children start after pause marker removal")
        finally:
            if proc.poll() is None:
                os.kill(int(read_pid(self.state, "supervisor")), signal.SIGTERM)
                proc.wait(timeout=15)

    def test_supervisor_runs_from_runtime_copy_without_repo(self):
        # The supervisor must run from the installed non-Desktop runtime copy
        # even when the repo/Desktop checkout is absent: install the runtime
        # from the fake repo, then invoke it with a repo-free PATH.
        d = tempfile.mkdtemp(prefix="lcb-runtime-copy-")
        self.addCleanup(shutil.rmtree, d, True)
        repo = make_fake_repo(d)
        call_log = os.path.join(d, "calls.log")
        install_fakes(repo, call_log)
        state = os.path.join(d, "state")
        run_admin(["create", "local"], state)
        home = os.path.join(d, "home")
        os.makedirs(home)
        data = os.path.join(d, "data")
        config = os.path.join(d, "config")
        env = env_for(state, data, config, home)
        env["PATH"] = os.path.join(repo, "bin") + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            ["bash", repo_script(repo, "install_runtime.sh"), "--instance", "local"],
            capture_output=True, text=True, env=env, cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        supervisor_copy = os.path.join(data, "current", "scripts",
                                       "run_local_supervisor.sh")
        self.assertTrue(os.path.isfile(supervisor_copy))
        with open(sentinel(state), "w") as fh:
            fh.write("")
        env.pop("BRIDGE_DATA_ROOT", None)
        env["BRIDGE_STATE_ROOT"] = state
        env["HOME"] = home
        env["FAKE_CALL_LOG"] = call_log
        # repo/bin and the repo itself are NOT on PATH (no Desktop dependency)
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        runner = subprocess.Popen(
            ["bash", supervisor_copy, "--instance", "local"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, cwd=os.path.join(data, "current"))
        try:
            self.assertTrue(wait_for(
                lambda: pid_alive(read_pid(state, "bridge")) and
                pid_alive(read_pid(state, "ngrok")), timeout=15),
                "children started from the runtime copy")
            self.assertIn("local start", calls(call_log))
            os.kill(int(read_pid(state, "supervisor")), signal.SIGTERM)
            runner.wait(timeout=15)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()
        self.assertEqual(runner.returncode, 0)
        self.assertIn("local stop", calls(call_log))

    def test_supervisor_backoff_retries_failed_start(self):
        with open(sentinel(self.state), "w") as fh:
            fh.write("")
        proc = self._spawn({"FAKE_FAIL_START": "1"})
        try:
            self.assertTrue(wait_for(
                lambda: len([c for c in calls(self.call_log)
                             if c == "local start"]) >= 2, timeout=20),
                "supervisor keeps retrying a failing start")
            self.assertIsNone(proc.poll(), "supervisor stays up during retries")
        finally:
            if proc.poll() is None:
                os.kill(int(read_pid(self.state, "supervisor")), signal.SIGTERM)
                proc.wait(timeout=15)

    def test_sentinel_removal_stops_children_and_exits_zero(self):
        with open(sentinel(self.state), "w") as fh:
            fh.write("")
        proc = self._spawn()
        try:
            self.assertTrue(wait_for(
                lambda: pid_alive(read_pid(self.state, "bridge"))))
            os.remove(sentinel(self.state))
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("local stop", calls(self.call_log))
        self.assertFalse(os.path.exists(os.path.join(
            local_runtime(self.state), "supervisor.pid")))


class MaintenanceCooperationTest(unittest.TestCase):
    """Dynamic activate/deactivate cooperation via the pause marker
    (sentinel stays in place during the window; the pause marker holds the
    local supervisor)."""

    def _setup(self):
        d = tempfile.mkdtemp(prefix="lcb-maint-sup-")
        self.addCleanup(shutil.rmtree, d, True)
        repo = make_fake_repo(d)
        call_log = os.path.join(d, "calls.log")
        install_fakes(repo, call_log)
        state = os.path.join(d, "state")
        run_admin(["create", "local"], state)
        run_admin(["create", "maintenance"], state)
        with open(sentinel(state), "w") as fh:
            fh.write("")
        env = env_for(state)
        env.update({
            "HOME": os.path.join(d, "home"),
            "FAKE_CALL_LOG": call_log,
            "PATH": os.path.join(repo, "bin") + os.pathsep + env.get("PATH", ""),
        })
        os.makedirs(os.path.join(d, "home"))
        os.makedirs(os.path.join(d, "home", ".codex-deepseek"), exist_ok=True)
        with open(os.path.join(d, "home", ".codex-deepseek", "config.toml"), "w") as fh:
            fh.write('model = "test-model"\n')
        # maintenance instance needs the repo credential files
        with open(os.path.join(repo, ".bridge_api_key"), "w") as fh:
            fh.write(DUMMY_KEY + "\n")
        with open(os.path.join(repo, ".ngrok_domain"), "w") as fh:
            fh.write(DUMMY_DOMAIN + "\n")
        return d, repo, state, call_log, env

    def test_activate_pause_marker_and_deactivate_legacy_restores(self):
        d, repo, state, call_log, env = self._setup()
        activate = os.path.join(repo, "scripts", "activate_maintenance_instance.sh")
        deactivate = os.path.join(repo, "scripts", "deactivate_maintenance_instance.sh")
        proc = subprocess.run(["bash", activate], capture_output=True, text=True,
                              env=env, cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # the sentinel STAYS during the window (supervisor stays alive, no
        # launchd crash-loop); the pause marker holds local children stopped
        self.assertTrue(os.path.exists(sentinel(state)),
                        "sentinel kept during the window")
        pause = os.path.join(state, "local", "pause.marker")
        self.assertTrue(os.path.isfile(pause),
                        "pause marker created before stopping local")
        marker = os.path.join(state, "maintenance", "activate.marker")
        self.assertTrue(os.path.isfile(marker))
        self.assertIn("supervisor_state=enabled",
                      open(marker, encoding="utf-8").read())
        self.assertEqual(calls(call_log), ["local stop", "maintenance start"])
        # deactivate: no launchd agent in tests -> legacy start flow
        proc = subprocess.run(["bash", deactivate], capture_output=True, text=True,
                              env=env, cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls(call_log),
                         ["local stop", "maintenance start",
                          "maintenance stop", "local start"])
        self.assertTrue(os.path.exists(sentinel(state)),
                        "sentinel kept after deactivate")
        self.assertFalse(os.path.exists(pause), "pause marker cleared")
        self.assertFalse(os.path.exists(marker))

    def test_deactivate_disabled_window_leaves_local_stopped(self):
        d, repo, state, call_log, env = self._setup()
        marker = os.path.join(state, "maintenance", "activate.marker")
        with open(marker, "w") as fh:
            fh.write("instance=maintenance\nsupervisor_state=disabled\n")
        pause = os.path.join(state, "local", "pause.marker")
        with open(pause, "w") as fh:
            fh.write("window\n")
        deactivate = os.path.join(repo, "scripts", "deactivate_maintenance_instance.sh")
        proc = subprocess.run(["bash", deactivate], capture_output=True, text=True,
                              env=env, cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(sentinel(state)))
        self.assertFalse(os.path.exists(pause), "pause marker cleared")
        self.assertNotIn("local start", calls(call_log))
        self.assertFalse(os.path.exists(marker))

    def test_deactivate_missing_marker_legacy_compat(self):
        d, repo, state, call_log, env = self._setup()
        os.remove(sentinel(state))  # window left it disabled; no marker (legacy)
        deactivate = os.path.join(repo, "scripts", "deactivate_maintenance_instance.sh")
        proc = subprocess.run(["bash", deactivate], capture_output=True, text=True,
                              env=env, cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # missing marker -> treated as enabled -> local restored (legacy flow)
        self.assertIn("local start", calls(call_log))
        self.assertTrue(os.path.exists(sentinel(state)))


class NewScriptInvariantsTest(unittest.TestCase):

    SCRIPTS = (INSTALL_RT, UNINSTALL_RT, SUPERVISOR, CONTROL,
               INSTALL_AGENT, UNINSTALL_AGENT,
               os.path.join(ROOT, "scripts", "activate_maintenance_instance.sh"),
               os.path.join(ROOT, "scripts", "deactivate_maintenance_instance.sh"))

    def test_no_pkill_killall_and_no_hpc_touch(self):
        for path in self.SCRIPTS:
            text = open(path, encoding="utf-8").read()
            body = "\n".join(ln for ln in text.splitlines()
                             if not ln.lstrip().startswith("#"))
            self.assertNotIn("pkill", body, path)
            self.assertNotIn("killall", body, path)
            self.assertNotIn("BRIDGE_INSTANCE=hpc", text, path)

    def test_rm_rf_only_guarded_in_installers(self):
        for path in (SUPERVISOR, CONTROL):
            text = open(path, encoding="utf-8").read()
            body = "\n".join(ln for ln in text.splitlines()
                             if not ln.lstrip().startswith("#"))
            self.assertNotIn("rm -rf", body, path)
            self.assertNotIn("rm -r ", body, path)
        for path in (INSTALL_RT, UNINSTALL_RT):
            text = open(path, encoding="utf-8").read()
            body = "\n".join(ln for ln in text.splitlines()
                             if not ln.lstrip().startswith("#"))
            for ln in body.splitlines():
                if "rm -rf" in ln:
                    self.assertIn("$target", ln, (path, ln))
                    self.assertIn("runtime_rm_tree", path and text, (path, ln))

    def test_no_secret_or_domain_literals_in_new_scripts(self):
        for path in self.SCRIPTS:
            with open(path, "rb") as fh:
                data = fh.read()
            self.assertNotIn(DUMMY_KEY.encode(), data, path)
            self.assertNotIn(DUMMY_DOMAIN.encode(), data, path)

    def test_supervisor_and_control_bash_n(self):
        for path in (SUPERVISOR, CONTROL, INSTALL_RT, UNINSTALL_RT):
            proc = subprocess.run(["bash", "-n", path], capture_output=True,
                                  text=True)
            self.assertEqual(proc.returncode, 0, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
