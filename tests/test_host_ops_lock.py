#!/usr/bin/env python3
"""Offline tests for the global host-ops single-writer lock
(scripts/host_ops_lock_lib.sh).

The lock serializes control-plane mutations (activate/deactivate
maintenance, autorecovery bootstrap) across humans / ChatGPT / unattended
automation: atomic `mkdir` acquisition under the state root, BUSY for a
concurrent live owner, reentrancy via the exported token, one-time stale
cleanup when the recorded owner pid is dead, trap-based release on EXIT.

All tests run in temp BRIDGE_STATE_ROOT dirs. The only signal-related call
is `kill -0` (liveness probe); the stale-owner pid is a value that cannot
exist on any host, so no real process is ever signalled or killed.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_LIB = os.path.join(ROOT, "scripts", "host_ops_lock_lib.sh")
INSTANCE_LIB = os.path.join(ROOT, "scripts", "bridge_instance_lib.sh")

PRELUDE = (
    'lock="$1"; inst="$2"; . "$lock"; . "$inst"\n'
)

DUMMY_KEY = "dummy-api-key-not-a-real-secret"
DUMMY_DOMAIN = "lock-test.invalid"
IMPOSSIBLE_PID = "99999999"  # > pid_max on every supported host


def run_lock(script, state_root, extra_env=None, timeout=60):
    env = dict(os.environ)
    for key in ("BRIDGE_STATE_ROOT", "XDG_STATE_HOME", "HOST_OPS_LOCK_TOKEN"):
        env.pop(key, None)
    env["BRIDGE_STATE_ROOT"] = state_root
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", PRELUDE + script, "test-sh", LOCK_LIB, INSTANCE_LIB],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=timeout,
    )


def lock_dir(state_root):
    return os.path.join(state_root, "host-ops.lock")


class HostOpsLockStaticTest(unittest.TestCase):

    def test_bash_n_and_sourceable(self):
        proc = subprocess.run(["bash", "-n", LOCK_LIB], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            os.makedirs(state)
            proc = run_lock("host_ops_lock_dir >/dev/null", state)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_dangerous_commands_and_no_hpc_touch(self):
        with open(LOCK_LIB, encoding="utf-8") as fh:
            text = fh.read()
        body = "\n".join(ln for ln in text.splitlines()
                         if not ln.lstrip().startswith("#"))
        for needle in ("pkill", "killall", "kill -9", "kill -TERM",
                       "rm -rf", "rm -r ", "BRIDGE_INSTANCE=hpc"):
            self.assertNotIn(needle, body, needle)
        # the ONLY signal-related call is the `kill -0` liveness probe
        self.assertEqual(body.count("kill -0"), 1, body)

    def test_no_secret_or_domain_literals(self):
        with open(LOCK_LIB, "rb") as fh:
            data = fh.read()
        self.assertNotIn(DUMMY_KEY.encode(), data)
        self.assertNotIn(DUMMY_DOMAIN.encode(), data)
        self.assertNotIn(b"ngrok-free.dev", data)


class HostOpsLockBehaviorTest(unittest.TestCase):

    def test_acquire_records_pid_operation_token_epoch_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            os.makedirs(state)
            proc = run_lock(
                'host_ops_lock_acquire "test-op"\n'
                'd="$(host_ops_lock_dir)"\n'
                'for f in owner.pid operation token epoch; do\n'
                '  [[ -f "$d/$f" ]] || { echo "missing $d/$f" >&2; exit 9; }\n'
                'done\n'
                '[[ "$(cat "$d/operation")" == "test-op" ]] || exit 10\n'
                '[[ "$(cat "$d/owner.pid")" == "$$" ]] || exit 11\n'
                '[[ -n "$(cat "$d/token")" ]] || exit 12\n'
                '[[ "$(cat "$d/epoch")" =~ ^[0-9]+$ ]] || exit 13\n'
                '[[ "$HOST_OPS_LOCK_TOKEN" == "$(cat "$d/token")" ]] || exit 14\n'
                'host_ops_lock_release\n'
                '[[ ! -e "$d" ]] || exit 15\n'
                'exit 0\n', state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(lock_dir(state)))

    def test_exit_trap_releases(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            os.makedirs(state)
            proc = run_lock('host_ops_lock_acquire "trap-op"; exit 0\n', state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(lock_dir(state)), "EXIT trap must release")

    def test_concurrent_owner_gets_busy(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            os.makedirs(state)
            holder = subprocess.Popen(
                ["bash", "-c", PRELUDE + 'host_ops_lock_acquire "holder-op"; echo HELD; sleep 30\n',
                 "test-sh", LOCK_LIB, INSTANCE_LIB],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "BRIDGE_STATE_ROOT": state,
                     "HOST_OPS_LOCK_TOKEN": ""},
            )
            try:
                for _ in range(100):
                    if os.path.isfile(os.path.join(lock_dir(state), "token")):
                        break
                    time.sleep(0.05)
                self.assertTrue(os.path.isfile(os.path.join(lock_dir(state), "token")))
                proc = run_lock(
                    'host_ops_lock_acquire "competing-op" || exit 2\n'
                    'echo "UNEXPECTED-ACQUIRE" >&2\n'
                    'exit 0\n', state)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("BUSY", proc.stderr)
                self.assertIn("holder-op", proc.stderr)
                self.assertIn("pid", proc.stderr)
                self.assertNotIn("UNEXPECTED-ACQUIRE", proc.stderr)
            finally:
                holder.terminate()
                holder.wait(timeout=10)

    def test_reentrant_same_token_nested_script(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            os.makedirs(state)
            proc = run_lock(
                'host_ops_lock_acquire "parent-op"\n'
                'd="$(host_ops_lock_dir)"\n'
                'owner="$(cat "$d/owner.pid")"\n'
                # a nested sub-script inherits the exported token: reentrant,
                # must NOT create a second lock or release the parent's
                'bash -c \'lock="$1"; inst="$2"; . "$lock"; . "$inst"; '
                'host_ops_lock_acquire "nested-op" || exit 30; '
                '[[ -n "$HOST_OPS_LOCK_TOKEN" ]] || exit 31; '
                'exit 0\' test-sh "$1" "$2" || exit 32\n'
                '[[ "$(cat "$d/owner.pid")" == "$owner" ]] || exit 33\n'
                '[[ "$(cat "$d/operation")" == "parent-op" ]] || exit 34\n'
                'host_ops_lock_release\n'
                '[[ ! -e "$d" ]] || exit 35\n'
                'exit 0\n', state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(lock_dir(state)))

    def test_reentrant_after_parent_acquire_inside_same_script(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            os.makedirs(state)
            proc = run_lock(
                'host_ops_lock_acquire "parent-op"\n'
                'host_ops_lock_acquire "parent-op"\n'
                'host_ops_lock_acquire "another-stage"\n'
                'd="$(host_ops_lock_dir)"\n'
                '[[ "$(cat "$d/operation")" == "parent-op" ]] || exit 40\n'
                'host_ops_lock_release\n'
                '[[ ! -e "$d" ]] || exit 41\n'
                'exit 0\n', state)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_stale_lock_cleaned_once_and_acquired(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            os.makedirs(state)
            stale = lock_dir(state)
            os.makedirs(stale)
            for name, value in (("owner.pid", IMPOSSIBLE_PID),
                                ("operation", "dead-op"),
                                ("token", "stale-token"),
                                ("epoch", "0")):
                with open(os.path.join(stale, name), "w") as fh:
                    fh.write(value)
            proc = run_lock(
                'host_ops_lock_acquire "fresh-op"\n'
                'd="$(host_ops_lock_dir)"\n'
                '[[ "$(cat "$d/owner.pid")" == "$$" ]] || exit 50\n'
                '[[ "$(cat "$d/operation")" == "fresh-op" ]] || exit 51\n'
                '[[ "$(cat "$d/owner.pid")" != "%s" ]] || exit 52\n'
                'host_ops_lock_release\n'
                'exit 0\n' % IMPOSSIBLE_PID, state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("stale lock from dead owner pid %s" % IMPOSSIBLE_PID,
                          proc.stderr)
            self.assertFalse(os.path.exists(lock_dir(state)))

    def test_release_does_not_remove_new_owner(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            os.makedirs(state)
            proc = run_lock(
                'host_ops_lock_acquire "first-op"\n'
                'host_ops_lock_release\n'
                'exit 0\n', state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(lock_dir(state)))
            # a NEW live owner takes the lock afterwards (fresh token)
            holder = subprocess.Popen(
                ["bash", "-c", PRELUDE + 'host_ops_lock_acquire "second-op"; echo HELD; sleep 30\n',
                 "test-sh", LOCK_LIB, INSTANCE_LIB],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "BRIDGE_STATE_ROOT": state,
                     "HOST_OPS_LOCK_TOKEN": ""},
            )
            try:
                for _ in range(100):
                    if os.path.isfile(os.path.join(lock_dir(state), "token")):
                        break
                    time.sleep(0.05)
                self.assertTrue(os.path.isfile(os.path.join(lock_dir(state), "token")))
                # an old (released) owner releasing again must NEVER remove
                # the new owner's lock
                proc = run_lock(
                    'host_ops_lock_release\n'
                    'd="$(host_ops_lock_dir)"\n'
                    '[[ -d "$d" ]] || exit 70\n'
                    '[[ "$(cat "$d/operation")" == "second-op" ]] || exit 71\n'
                    'exit 0\n', state)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                with open(os.path.join(lock_dir(state), "operation"),
                          encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "second-op")
            finally:
                holder.terminate()
                holder.wait(timeout=10)

    def test_lock_records_never_contain_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            os.makedirs(state)
            proc = run_lock(
                'host_ops_lock_acquire "op-with-secret-env"\n'
                'd="$(host_ops_lock_dir)"\n'
                'for f in owner.pid operation token epoch; do\n'
                '  cat "$d/$f"\n'
                'done\n'
                'exit 0\n', state,
                extra_env={"DEEPSEEK_API_KEY": DUMMY_KEY,
                           "BRIDGE_API_KEY": DUMMY_KEY})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn(DUMMY_KEY, proc.stdout)
            self.assertNotIn(DUMMY_KEY, proc.stderr)


if __name__ == "__main__":
    unittest.main()
