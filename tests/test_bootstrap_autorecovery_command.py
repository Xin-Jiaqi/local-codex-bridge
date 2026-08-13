#!/usr/bin/env python3
"""Offline tests for scripts/bootstrap_autorecovery.command (1.1.0).

The .command is the one-click (Finder double-click) THIN orchestrator that
bootstraps the LOCAL auto-recovery stack: local -> open the maintenance
window -> run the existing activation script (ends on local) -> re-open the
maintenance window -> verify maintenance/bridge-workspace/8323.

These tests are DETERMINISTIC and spawn no real Bridge/ngrok/launchctl:
  - the two host-admin callees (activate_maintenance_instance.sh and
    activate_runtime_autorecovery.sh) are STUBBED in a fixture repo copy of
    the .command: they only log their invocation and flip a fixture health
    scenario file (never touching the real scripts or system state);
  - health is faked by a scenario-driven fake `curl` in PATH (maintenance
    active => 8323/public maintenance, 8321 refused; local => 8321/public
    local, 8323 refused; anomaly => wrong identity on 8323);
  - instance configs are created with the real file-only admin script
    (bridge_instance.sh create - no network, no processes).

Covers: set -euo pipefail + self-locating ROOT, calls ONLY the two existing
host-admin scripts, local flow call order + final YES marker, already
maintenance skips the initial activate, health anomaly fails closed before
any mutation, a failed activation stops before the re-open, failure marker
NO, no dangerous commands, no secret/domain echo, executable bit, bash -n.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMAND = os.path.join(ROOT, "scripts", "bootstrap_autorecovery.command")

DUMMY_KEY = "dummy-api-key-not-a-real-secret"
DUMMY_DOMAIN = "bootstrap-test.invalid"


def clean_env(extra=None):
    env = dict(os.environ)
    for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT", "BRIDGE_DATA_ROOT",
                "BRIDGE_CONFIG_ROOT", "XDG_STATE_HOME", "XDG_DATA_HOME",
                "XDG_CONFIG_HOME", "BRIDGE_SANDBOX_MODE",
                "BRIDGE_SANDBOX_MODE_FILE", "NGROK_DOMAIN",
                "FAKE_CALL_LOG", "FAKE_ACTIVATION_FAIL"):
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def create_instances(state, home, *names):
    """File-only real admin: create the pinned instance configs."""
    env = clean_env({"BRIDGE_STATE_ROOT": state, "HOME": home})
    for name in names:
        proc = subprocess.run(
            ["bash", os.path.join(ROOT, "scripts", "bridge_instance.sh"),
             "create", name],
            capture_output=True, text=True, env=env, cwd=ROOT,
        )
        assert proc.returncode == 0, proc.stderr


def fixture(d, scenario):
    """Stub-only fixture: a repo copy of the .command whose callees are
    logging stubs; health is scenario-driven; no real processes."""
    repo = os.path.join(d, "repo")
    os.makedirs(os.path.join(repo, "scripts"))
    # The REAL .command runs here, but ROOT resolves to the fixture repo so
    # its script calls hit the stubs below (no real host-admin runs).
    cmd_copy = os.path.join(repo, "scripts", "bootstrap_autorecovery.command")
    shutil.copy2(COMMAND, cmd_copy)
    os.chmod(cmd_copy, 0o755)
    shutil.copy2(os.path.join(ROOT, "scripts", "bridge_instance_lib.sh"),
                 os.path.join(repo, "scripts", "bridge_instance_lib.sh"))
    with open(os.path.join(repo, ".ngrok_domain"), "w") as fh:
        fh.write(DUMMY_DOMAIN + "\n")

    state = os.path.join(d, "state")
    home = os.path.join(d, "home")
    os.makedirs(home)
    create_instances(state, home, "local", "maintenance")

    call_log = os.path.join(d, "calls.log")
    scenario_file = os.path.join(state, "health.scenario")
    with open(scenario_file, "w") as fh:
        fh.write(scenario + "\n")

    bindir = os.path.join(d, "bin")
    os.makedirs(bindir)
    with open(os.path.join(bindir, "curl"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'port="8321"\n'
                 'for arg in "$@"; do\n'
                 '  case "$arg" in\n'
                 '    http://127.0.0.1:*) port="${arg#http://127.0.0.1:}"; port="${port%%/*}" ;;\n'
                 '    https://*) port="public" ;;\n'
                 '  esac\n'
                 'done\n'
                 'scenario="$(cat "${BRIDGE_STATE_ROOT:?}/health.scenario" 2>/dev/null || true)"\n'
                 'if [[ "$scenario" == "maintenance" ]]; then\n'
                 '  if [[ "$port" == "8323" || "$port" == "public" ]]; then\n'
                 '    printf \'{"status":"ok","instance":"maintenance","mode":"bridge-workspace","port":8323}\\n\'\n'
                 '    exit 0\n'
                 '  fi\n'
                 '  printf \'curl: (7) connection refused\\n\' >&2\n'
                 '  exit 7\n'
                 'fi\n'
                 'if [[ "$scenario" == "anomaly" ]]; then\n'
                 '  if [[ "$port" == "8323" ]]; then\n'
                 '    printf \'{"status":"ok","instance":"local","mode":"bridge-workspace","port":8321}\\n\'\n'
                 '    exit 0\n'
                 '  fi\n'
                 '  printf \'curl: (7) connection refused\\n\' >&2\n'
                 '  exit 7\n'
                 'fi\n'
                 'if [[ "$port" == "8321" || "$port" == "public" ]]; then\n'
                 '  printf \'{"status":"ok","instance":"local","mode":"bridge-workspace","port":8321}\\n\'\n'
                 '  exit 0\n'
                 'fi\n'
                 'printf \'curl: (7) connection refused\\n\' >&2\n'
                 'exit 7\n')
    os.chmod(os.path.join(bindir, "curl"), 0o755)

    # Stub callees: log their invocation, flip the health scenario exactly
    # like the real flow (window open => maintenance; activation ends local;
    # re-open => maintenance again). FAKE_ACTIVATION_FAIL makes the
    # activation stub fail after logging (no state flip).
    with open(os.path.join(repo, "scripts",
                           "activate_maintenance_instance.sh"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'echo "activate_maintenance_instance" >> "${FAKE_CALL_LOG:?}"\n'
                 'printf \'maintenance\\n\' > "${BRIDGE_STATE_ROOT:?}/health.scenario"\n'
                 'exit 0\n')
    with open(os.path.join(repo, "scripts",
                           "activate_runtime_autorecovery.sh"), "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'echo "activate_runtime_autorecovery" >> "${FAKE_CALL_LOG:?}"\n'
                 'if [[ -n "${FAKE_ACTIVATION_FAIL:-}" ]]; then exit 1; fi\n'
                 'printf \'local\\n\' > "${BRIDGE_STATE_ROOT:?}/health.scenario"\n'
                 'exit 0\n')
    for name in ("activate_maintenance_instance.sh",
                 "activate_runtime_autorecovery.sh"):
        os.chmod(os.path.join(repo, "scripts", name), 0o755)

    env = clean_env({
        "BRIDGE_STATE_ROOT": state,
        "BRIDGE_DATA_ROOT": os.path.join(d, "data"),
        "BRIDGE_CONFIG_ROOT": os.path.join(d, "config"),
        "HOME": home,
        "FAKE_CALL_LOG": call_log,
        "PATH": bindir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
    })
    return repo, call_log, state, env


def run_command(env, repo, **extra_env):
    env = dict(env)
    env.update(extra_env)
    return subprocess.run(
        ["bash", os.path.join(repo, "scripts",
                              "bootstrap_autorecovery.command")],
        capture_output=True, text=True, env=env, cwd=os.path.join(repo),
        timeout=60)


def read_log(call_log):
    if not os.path.isfile(call_log):
        return []
    return [ln for ln in open(call_log, encoding="utf-8").read().splitlines()
            if ln]


class BootstrapStaticTest(unittest.TestCase):

    def test_bash_n(self):
        proc = subprocess.run(["bash", "-n", COMMAND], capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_executable_bit(self):
        self.assertTrue(os.stat(COMMAND).st_mode & stat.S_IXUSR, COMMAND)

    def test_strict_mode_and_self_locating_root(self):
        with open(COMMAND, encoding="utf-8") as fh:
            text = fh.read()
        body = "\n".join(ln for ln in text.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertIn("set -euo pipefail", body)
        self.assertIn('ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
                      body)

    def test_calls_only_the_two_existing_host_admin_scripts(self):
        with open(COMMAND, encoding="utf-8") as fh:
            text = fh.read()
        body = "\n".join(ln for ln in text.splitlines()
                         if not ln.lstrip().startswith("#"))
        # only `bash "$ROOT/scripts/..."` invocations count as calls; plain
        # mentions inside user-facing hint text are fine
        called = set(re.findall(r'\$ROOT/scripts/([A-Za-z0-9_.-]+\.sh)', body))
        self.assertEqual(
            called,
            {"activate_maintenance_instance.sh",
             "activate_runtime_autorecovery.sh",
             "bridge_instance_lib.sh"},
            called)

    def test_no_dangerous_commands_and_no_hpc_touch(self):
        with open(COMMAND, encoding="utf-8") as fh:
            text = fh.read()
        body = "\n".join(ln for ln in text.splitlines()
                         if not ln.lstrip().startswith("#"))
        for needle in ("pkill", "killall", "rm -rf", "rm -r ", " rm ",
                       "launchctl", "BRIDGE_INSTANCE=hpc"):
            self.assertNotIn(needle, body, needle)

    def test_no_secret_or_domain_literals(self):
        with open(COMMAND, "rb") as fh:
            data = fh.read()
        self.assertNotIn(DUMMY_KEY.encode(), data)
        self.assertNotIn(DUMMY_DOMAIN.encode(), data)
        self.assertNotIn(b"ngrok-free.dev", data)

    def test_ok_and_fail_markers_present(self):
        with open(COMMAND, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("BOOTSTRAP_AUTORECOVERY_OK YES", text)
        self.assertIn("BOOTSTRAP_AUTORECOVERY_OK NO", text)
        self.assertIn("you can close this window", text)


class BootstrapFlowTest(unittest.TestCase):

    def test_local_flow_order_markers_and_final_maintenance(self):
        d = tempfile.mkdtemp(prefix="lcb-boot-local-")
        self.addCleanup(shutil.rmtree, d, True)
        repo, call_log, state, env = fixture(d, scenario="local")
        proc = run_command(env, repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout + proc.stderr
        self.assertNotIn(DUMMY_KEY, out)
        self.assertNotIn(DUMMY_DOMAIN, out)
        self.assertIn("current instance: local", proc.stdout)
        self.assertIn("BOOTSTRAP_AUTORECOVERY_OK YES", proc.stdout)
        self.assertIn("you can close this window", proc.stdout)
        # local -> open window -> activation -> re-open window
        log = read_log(call_log)
        self.assertEqual(log.count("activate_maintenance_instance"), 2, log)
        self.assertEqual(log.count("activate_runtime_autorecovery"), 1, log)
        self.assertEqual(log[0], "activate_maintenance_instance")
        self.assertEqual(log[1], "activate_runtime_autorecovery")
        self.assertEqual(log[2], "activate_maintenance_instance")
        # final health scenario is the maintenance window (re-opened)
        with open(os.path.join(state, "health.scenario")) as fh:
            self.assertEqual(fh.read().strip(), "maintenance")

    def test_already_maintenance_skips_initial_activate(self):
        d = tempfile.mkdtemp(prefix="lcb-boot-maint-")
        self.addCleanup(shutil.rmtree, d, True)
        repo, call_log, state, env = fixture(d, scenario="maintenance")
        proc = run_command(env, repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("current instance: maintenance", proc.stdout)
        self.assertIn("skipping the initial activate", proc.stdout)
        self.assertIn("BOOTSTRAP_AUTORECOVERY_OK YES", proc.stdout)
        log = read_log(call_log)
        # activation, then only the re-open starts the maintenance window
        self.assertEqual(log[0], "activate_runtime_autorecovery")
        self.assertEqual(log.count("activate_maintenance_instance"), 1, log)
        with open(os.path.join(state, "health.scenario")) as fh:
            self.assertEqual(fh.read().strip(), "maintenance")

    def test_health_anomaly_fails_closed_before_any_mutation(self):
        d = tempfile.mkdtemp(prefix="lcb-boot-bad-")
        self.addCleanup(shutil.rmtree, d, True)
        repo, call_log, state, env = fixture(d, scenario="anomaly")
        proc = run_command(env, repo)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("neither local nor maintenance", proc.stderr)
        self.assertIn("BOOTSTRAP_AUTORECOVERY_OK NO", proc.stderr)
        self.assertNotIn("BOOTSTRAP_AUTORECOVERY_OK YES", proc.stdout)
        self.assertNotIn("you can close this window", proc.stdout)
        self.assertEqual(read_log(call_log), [])

    def test_activation_failure_stops_before_reopen(self):
        d = tempfile.mkdtemp(prefix="lcb-boot-fail-")
        self.addCleanup(shutil.rmtree, d, True)
        repo, call_log, state, env = fixture(d, scenario="local")
        proc = run_command(env, repo, FAKE_ACTIVATION_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("BOOTSTRAP_AUTORECOVERY_OK NO", proc.stderr)
        self.assertNotIn("BOOTSTRAP_AUTORECOVERY_OK YES", proc.stdout)
        self.assertNotIn("you can close this window", proc.stdout)
        log = read_log(call_log)
        # window opened and activation attempted, but the re-open never ran
        self.assertEqual(log[0], "activate_maintenance_instance")
        self.assertEqual(log[1], "activate_runtime_autorecovery")
        self.assertEqual(log.count("activate_maintenance_instance"), 1, log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
