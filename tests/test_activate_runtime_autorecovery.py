#!/usr/bin/env python3
"""Offline tests for scripts/activate_runtime_autorecovery.sh (1.1.0).

The activator is the one-shot HOST-ADMIN orchestration that converts an
ACTIVE maintenance window into a permanently auto-recovered local stack
(stable runtime + per-instance launchd supervisor agent). These tests are
STATIC / FIXTURE / STUB only:

  - no real launchctl is called (stateful fake launchctl in PATH);
  - no real signal is delivered (the kill builtin is stubbed with a shell
    function in the recovery tests; no real process is signalled);
  - no real bridge/ngrok/secret/domain is used (dummy fixture values);
  - health is faked by a port/state-aware fake curl.

Covers: maintenance preflight (identity), pause-marker idempotency, the
runtime -> agent -> deactivate stage order, generated-plist verification
(label + ProgramArguments -> installed runtime supervisor), managed-PID
guard before TERM, no pkill/killall/rm -rf, no hpc touch, no secret/domain
echo, AUTORECOVERY_ACTIVATION_OK YES/NO markers and bash -n.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIVATOR = os.path.join(ROOT, "scripts", "activate_runtime_autorecovery.sh")
ADMIN = os.path.join(ROOT, "scripts", "bridge_instance.sh")

DUMMY_KEY = "dummy-api-key-not-a-real-secret"
DUMMY_DOMAIN = "autorecovery-test.invalid"


def clean_env(extra=None):
    env = dict(os.environ)
    for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT", "BRIDGE_DATA_ROOT",
                "BRIDGE_CONFIG_ROOT", "XDG_STATE_HOME", "XDG_DATA_HOME",
                "XDG_CONFIG_HOME", "BRIDGE_SANDBOX_MODE",
                "BRIDGE_SANDBOX_MODE_FILE", "NGROK_DOMAIN",
                "SUPERVISOR_AGENT_LABEL", "AR_CRASH_RECOVERY",
                "AR_HEALTH_WAIT", "AR_RECOVERY_WAIT", "FAKE_MAINT_OFF",
                "FAKE_CALL_LOG"):
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def run_admin(args, state_root, home):
    env = clean_env({"BRIDGE_STATE_ROOT": state_root, "HOME": home})
    return subprocess.run(
        ["bash", ADMIN] + list(args),
        capture_output=True, text=True, env=env, cwd=ROOT,
    )


def make_fake_repo(d):
    """Mini git repo with the real runtime/script files + dummy secrets."""
    repo = os.path.join(d, "repo")
    os.makedirs(os.path.join(repo, "scripts"))
    for name in ("bridge", "http_server", "config"):
        shutil.copytree(os.path.join(ROOT, name), os.path.join(repo, name))
    for f in os.listdir(os.path.join(ROOT, "scripts")):
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
    with open(os.path.join(repo, ".bridge_api_key"), "w") as fh:
        fh.write(DUMMY_KEY + "\n")
    with open(os.path.join(repo, ".ngrok_domain"), "w") as fh:
        fh.write(DUMMY_DOMAIN + "\n")
    subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-q", "-m", "seed"], check=True)
    return repo


def install_fakes(repo, call_log):
    """Stateful fakes: launchctl (print = plist present), curl (health by
    port/state), ps (managed command shapes) and instance-aware start/stop
    (log only + pid bookkeeping). `kill` is a bash BUILTIN and cannot be
    shadowed via PATH: the recovery tests stub it with a shell function
    instead (no real signal is ever delivered)."""
    # Fake start/stop OVERWRITE the repo copies: every script reaches them
    # via "$ROOT/scripts/start_ngrok_bridge.sh" / stop_ngrok_bridge.sh.
    bindir = os.path.join(repo, "bin")
    os.makedirs(bindir, exist_ok=True)
    with open(os.path.join(bindir, "launchctl"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'label="${2##*/}"\n'
                 'plist="$HOME/Library/LaunchAgents/$label.plist"\n'
                 'if [[ "$1" == "print" ]]; then\n'
                 '  if [[ -f "$plist" ]]; then exit 0; else exit 1; fi\n'
                 'fi\n'
                 'echo "launchctl $*" >> "${FAKE_CALL_LOG:?}"\n'
                 'exit 0\n')
    with open(os.path.join(bindir, "curl"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'port="8321"\n'
                 'for arg in "$@"; do\n'
                 '  case "$arg" in\n'
                 '    http://127.0.0.1:*) port="${arg#http://127.0.0.1:}"; port="${port%%/*}" ;;\n'
                 '    https://*) port="public" ;;\n'
                 '  esac\n'
                 'done\n'
                 'if [[ -n "${FAKE_MAINT_OFF:-}" ]]; then\n'
                 '  printf \'{"status":"ok","instance":"local","mode":"bridge-workspace","port":8321}\\n\'\n'
                 '  exit 0\n'
                 'fi\n'
                 'if [[ "$port" == "8323" ]]; then\n'
                 '  printf \'{"status":"ok","instance":"maintenance","mode":"bridge-workspace","port":8323}\\n\'\n'
                 'elif [[ "$port" == "public" && -f "${BRIDGE_STATE_ROOT:?}/maintenance/runtime/bridge.pid" ]]; then\n'
                 '  printf \'{"status":"ok","instance":"maintenance","mode":"bridge-workspace","port":8323}\\n\'\n'
                 'else\n'
                 '  printf \'{"status":"ok","instance":"local","mode":"bridge-workspace","port":8321}\\n\'\n'
                 'fi\n'
                 'exit 0\n')
    with open(os.path.join(bindir, "ps"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'pid="$2"\n'
                 'case "$pid" in\n'
                 '  8*) printf "bash %s/current/scripts/run_local_supervisor.sh --instance local\\n" "${BRIDGE_STATE_ROOT:?}" ;;\n'
                 '  9*) printf "python3 -m http_server --host 127.0.0.1 --port 8321 --log %s/local/runtime/bridge.log\\n" "${BRIDGE_STATE_ROOT:?}" ;;\n'
                 '  *) printf "sleep 5\\n" ;;\n'
                 'esac\n')
    with open(os.path.join(repo, "scripts", "start_ngrok_bridge.sh"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'echo "${BRIDGE_INSTANCE:-local} start" >> "${FAKE_CALL_LOG:?}"\n'
                 'RUNTIME_DIR="${BRIDGE_STATE_ROOT:?}/${BRIDGE_INSTANCE:-local}/runtime"\n'
                 'mkdir -p "$RUNTIME_DIR"\n'
                 'for role in bridge ngrok; do\n'
                 '  pid="$(cat "$RUNTIME_DIR/$role.pid" 2>/dev/null || true)"\n'
                '  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then\n'
                '    if [[ "$role" == "bridge" ]]; then\n'
                '      nohup python3 -c \'import time; time.sleep(600)\' http_server --log "$RUNTIME_DIR/bridge.log" >/dev/null 2>&1 &\n'
                '    else\n'
                '      nohup python3 -c \'import time; time.sleep(600)\' ngrok http 8321 >/dev/null 2>&1 &\n'
                '    fi\n'
                '    echo $! > "$RUNTIME_DIR/$role.pid"\n'
                 '  fi\n'
                 'done\n'
                 'exit 0\n')
    with open(os.path.join(repo, "scripts", "stop_ngrok_bridge.sh"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'echo "${BRIDGE_INSTANCE:-local} stop" >> "${FAKE_CALL_LOG:?}"\n'
                 'RUNTIME_DIR="${BRIDGE_STATE_ROOT:?}/${BRIDGE_INSTANCE:-local}/runtime"\n'
                 'for f in bridge.pid ngrok.pid; do\n'
                 '  pid="$(cat "$RUNTIME_DIR/$f" 2>/dev/null || true)"\n'
                 '  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then\n'
                 '    kill "$pid" 2>/dev/null || true\n'
                 '  fi\n'
                 '  rm -f "$RUNTIME_DIR/$f"\n'
                 'done\n'
                 'exit 0\n')
    for name in ("launchctl", "curl", "ps"):
        os.chmod(os.path.join(bindir, name), 0o755)
    for name in ("start_ngrok_bridge.sh", "stop_ngrok_bridge.sh"):
        os.chmod(os.path.join(repo, "scripts", name), 0o755)
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-q", "-m", "fakes"], check=True)


def base_env(state, data, config, home, repo, call_log, **extra):
    env = clean_env({
        "BRIDGE_STATE_ROOT": state,
        "BRIDGE_DATA_ROOT": data,
        "BRIDGE_CONFIG_ROOT": config,
        "HOME": home,
        "FAKE_CALL_LOG": call_log,
        "PATH": os.path.join(repo, "bin") + os.pathsep + env_path(),
    })
    env.update(extra)
    return env


def env_path():
    return os.environ.get("PATH", "/usr/bin:/bin")


def sourced(script, env, args=(), prelude=""):
    return subprocess.run(
        ["bash", "-c",
         prelude + 'src="$1"; shift; . "$src"\n' + script,
         "test-sh", ACTIVATOR, *args],
        capture_output=True, text=True, env=env,
    )


def local_runtime(state):
    return os.path.join(state, "local", "runtime")


def setup(d, with_legacy_plist=False, seed_local_pids=True):
    """Shared fixture: fake repo + local/maintenance configs + fakes."""
    repo = make_fake_repo(d)
    call_log = os.path.join(d, "calls.log")
    install_fakes(repo, call_log)
    state = os.path.join(d, "state")
    home = os.path.join(d, "home")
    os.makedirs(os.path.join(home, "Library", "LaunchAgents"))
    data = os.path.join(d, "data")
    config = os.path.join(d, "config")
    for inst in ("local", "maintenance"):
        proc = run_admin(["create", inst], state, home)
        assert proc.returncode == 0, proc.stderr
    # maintenance window uses a fixed endpoint: point the maintenance config
    # at a fixture domain file (content is never printed by the scripts).
    domain_file = os.path.join(d, "domain.txt")
    with open(domain_file, "w") as fh:
        fh.write(DUMMY_DOMAIN + "\n")
    proc = run_admin(
        ["update", "maintenance", "ngrok_domain_file=%s" % domain_file],
        state, home)
    assert proc.returncode == 0, proc.stderr
    # maintenance "active": its runtime pid file exists (fake curl key)
    maint_runtime = os.path.join(state, "maintenance", "runtime")
    os.makedirs(maint_runtime, exist_ok=True)
    with open(os.path.join(maint_runtime, "bridge.pid"), "w") as fh:
        fh.write("999999\n")
    if seed_local_pids:
        # The test runner's own pid is alive forever (the `kill -0` builtin
        # succeeds) and never matches the managed command shapes (ps shows
        # the python test runner, not http_server/ngrok). No throwaway
        # children are needed.
        lr = local_runtime(state)
        os.makedirs(lr, exist_ok=True)
        for name in ("bridge", "ngrok"):
            with open(os.path.join(lr, name + ".pid"), "w") as fh:
                fh.write(str(os.getpid()) + "\n")
    if with_legacy_plist:
        with open(os.path.join(home, "Library", "LaunchAgents",
                               "com.local.codex-bridge.plist"), "w") as fh:
            fh.write('<?xml version="1.0"?>\n<plist version="1.0"><dict>'
                     '<key>Label</key><string>com.local.codex-bridge</string>'
                     '</dict></plist>\n')
    return repo, call_log, state, home, data, config


def flow_env(d, repo, call_log, state, home, data, config, **extra):
    return base_env(state, data, config, home, repo, call_log, **extra)


class ActivatorStaticTest(unittest.TestCase):

    def test_bash_n(self):
        proc = subprocess.run(["bash", "-n", ACTIVATOR], capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_dangerous_commands_and_no_hpc_touch(self):
        with open(ACTIVATOR, encoding="utf-8") as fh:
            text = fh.read()
        body = "\n".join(ln for ln in text.splitlines()
                         if not ln.lstrip().startswith("#"))
        for needle in ("pkill", "killall", "rm -rf", "rm -r ", " rm ",
                       "BRIDGE_INSTANCE=hpc"):
            self.assertNotIn(needle, body, needle)
        self.assertNotIn("launchctl", body)  # launchctl only via the install scripts

    def test_pid_guard_before_any_kill(self):
        with open(ACTIVATOR, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("managed_bridge_pid", text)
        self.assertIn('kill "$bpid"', text)
        self.assertLess(text.index("managed_bridge_pid"),
                        text.index('kill "$bpid"'))
        self.assertEqual(text.count('kill "$bpid"'), 1)
        self.assertNotIn('kill "$npid"', text)  # ngrok is never killed

    def test_no_secret_or_domain_literals(self):
        with open(ACTIVATOR, "rb") as fh:
            data = fh.read()
        self.assertNotIn(DUMMY_KEY.encode(), data)
        self.assertNotIn(DUMMY_DOMAIN.encode(), data)
        self.assertNotIn(b"ngrok-free.dev", data)


class ActivatorPreflightTest(unittest.TestCase):

    def _base(self):
        d = tempfile.mkdtemp(prefix="lcb-ar-pre-")
        self.addCleanup(shutil.rmtree, d, True)
        repo, call_log, state, home, data, config = setup(d)
        env = flow_env(d, repo, call_log, state, home, data, config)
        return env

    def test_preflight_accepts_maintenance_window(self):
        env = self._base()
        proc = sourced("verify_maintenance_preflight", env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stage: preflight", proc.stdout)
        self.assertIn("preflight OK", proc.stdout)

    def test_preflight_fails_closed_when_window_not_active(self):
        env = self._base()
        env["FAKE_MAINT_OFF"] = "1"
        proc = sourced("verify_maintenance_preflight", env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("stage=preflight", proc.stderr)
        self.assertIn("AUTORECOVERY_ACTIVATION_OK NO", proc.stderr)
        self.assertIn("maintenance window must be ACTIVE", proc.stderr)

    def test_preflight_fails_closed_without_maintenance_config(self):
        d = tempfile.mkdtemp(prefix="lcb-ar-nomaint-")
        self.addCleanup(shutil.rmtree, d, True)
        repo, call_log, state, home, data, config = setup(d)
        os.remove(os.path.join(state, "maintenance", "instance.conf"))
        env = flow_env(d, repo, call_log, state, home, data, config)
        proc = sourced("verify_maintenance_preflight", env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("activate_maintenance_instance.sh", proc.stderr)
        self.assertIn("AUTORECOVERY_ACTIVATION_OK NO", proc.stderr)


class ActivatorPauseMarkerTest(unittest.TestCase):

    def test_pause_marker_idempotent(self):
        d = tempfile.mkdtemp(prefix="lcb-ar-pause-")
        self.addCleanup(shutil.rmtree, d, True)
        repo, call_log, state, home, data, config = setup(d)
        env = flow_env(d, repo, call_log, state, home, data, config)
        proc = sourced("ensure_local_pause_marker\nensure_local_pause_marker", env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        marker = os.path.join(state, "local", "pause.marker")
        self.assertTrue(os.path.isfile(marker), marker)
        self.assertEqual(stat.S_IMODE(os.stat(marker).st_mode), 0o600, marker)


class ActivatorFlowTest(unittest.TestCase):

    def test_full_flow_order_marker_plist_and_no_secret_echo(self):
        d = tempfile.mkdtemp(prefix="lcb-ar-flow-")
        self.addCleanup(shutil.rmtree, d, True)
        repo, call_log, state, home, data, config = setup(
            d, with_legacy_plist=True)
        env = flow_env(d, repo, call_log, state, home, data, config, **{
            "AR_CRASH_RECOVERY": "0",
            "AR_HEALTH_WAIT": "5",
        })
        proc = subprocess.run(
            ["bash", os.path.join(repo, "scripts",
                                  "activate_runtime_autorecovery.sh")],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(repo),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout + proc.stderr
        self.assertNotIn(DUMMY_KEY, out)
        self.assertNotIn(DUMMY_DOMAIN, out)
        self.assertIn("AUTORECOVERY_ACTIVATION_OK YES", proc.stdout)
        self.assertIn("bridge crash-recovery self-test skipped", proc.stdout)
        # stage order: preflight -> pause -> runtime -> agent -> deactivate
        stages = [ln for ln in proc.stdout.splitlines()
                  if "stage:" in ln]
        order = [re.search(r"stage: (\S+)", ln).group(1) for ln in stages]
        wanted = ["preflight", "pause-marker", "install-runtime",
                  "install-launch-agent", "deactivate-maintenance",
                  "final-status"]
        idx = [order.index(w) for w in wanted]
        self.assertEqual(idx, sorted(idx), order)
        # pause marker created before the agent install, cleared by deactivate
        self.assertLess(order.index("pause-marker"),
                        order.index("install-launch-agent"))
        self.assertFalse(os.path.exists(
            os.path.join(state, "local", "pause.marker")))
        # supervisor sentinel present (auto-recovery enabled)
        self.assertTrue(os.path.isfile(
            os.path.join(state, "local", "runtime", "supervisor.enabled")))
        # legacy plist backed up, never deleted; new label installed
        agents = os.path.join(home, "Library", "LaunchAgents")
        backups = [f for f in os.listdir(agents)
                   if f.startswith("com.local.codex-bridge.plist.bak-")]
        self.assertEqual(len(backups), 1, backups)
        self.assertFalse(os.path.exists(
            os.path.join(agents, "com.local.codex-bridge.plist")))
        plist_path = os.path.join(agents, "com.local.codex-bridge.local.plist")
        self.assertTrue(os.path.isfile(plist_path), plist_path)
        with open(plist_path, "rb") as fh:
            import plistlib
            plist = plistlib.load(fh)
        self.assertEqual(plist["Label"], "com.local.codex-bridge.local")
        self.assertEqual(plist["ProgramArguments"], [
            "/bin/bash",
            os.path.join(data, "current", "scripts",
                         "run_local_supervisor.sh"),
            "--instance", "local",
        ])
        # call order: maintenance stopped, local never started during window
        log = [ln for ln in open(call_log, encoding="utf-8").read().splitlines()
               if ln]
        self.assertIn("maintenance stop", log)
        self.assertNotIn("local start", log)


class ActivatorRecoveryTest(unittest.TestCase):

    # Stub the kill builtin with a shell function (no real signal): the
    # scripts call plain `kill`, which bash resolves as a function here.
    KILL_PRELUDE = ('kill() { echo "kill $*" >> "${FAKE_CALL_LOG:?}"; return 0; }\n')

    def _env(self, d):
        repo, call_log, state, home, data, config = setup(d)
        env = flow_env(d, repo, call_log, state, home, data, config, **{
            "AR_RECOVERY_WAIT": "8",
        })
        # post-deactivate state: maintenance is stopped, local is being
        # supervised. The fake curl keys the public identity on the
        # maintenance runtime pid file, so remove it here.
        os.remove(os.path.join(state, "maintenance", "runtime", "bridge.pid"))
        lr = local_runtime(state)
        with open(os.path.join(lr, "supervisor.pid"), "w") as fh:
            fh.write("80001\n")
        with open(os.path.join(lr, "bridge.pid"), "w") as fh:
            fh.write("90002\n")
        return env, call_log, state

    def test_recovery_fails_closed_without_managed_supervisor(self):
        d = tempfile.mkdtemp(prefix="lcb-ar-rec-nosup-")
        self.addCleanup(shutil.rmtree, d, True)
        env, call_log, state = self._env(d)
        os.remove(os.path.join(state, "local", "runtime", "supervisor.pid"))
        proc = sourced("bridge_crash_recovery", env, prelude=self.KILL_PRELUDE)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no managed supervisor", proc.stderr)
        self.assertIn("AUTORECOVERY_ACTIVATION_OK NO", proc.stderr)

    def test_recovery_terms_managed_bridge_and_waits_for_new_pid(self):
        d = tempfile.mkdtemp(prefix="lcb-ar-rec-ok-")
        self.addCleanup(shutil.rmtree, d, True)
        env, call_log, state = self._env(d)
        bridge_pid_file = os.path.join(state, "local", "runtime", "bridge.pid")
        restarter = subprocess.Popen(
            ["bash", "-c", 'sleep 1.2; printf "90003" > "$1"',
             "restarter", bridge_pid_file])
        self.addCleanup(restarter.wait, 10)
        proc = sourced("bridge_crash_recovery", env, prelude=self.KILL_PRELUDE)
        restarter.wait(timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("bridge crash recovery OK", proc.stdout)
        self.assertEqual(open(bridge_pid_file).read().strip(), "90003")
        with open(call_log, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("kill 90002", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
