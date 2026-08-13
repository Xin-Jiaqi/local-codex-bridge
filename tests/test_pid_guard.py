#!/usr/bin/env python3
"""Offline tests for scripts/pid_guard_lib.sh and its start/stop glue.

The PID guard never signals or reuses a process based on a bare numeric PID:
identity is verified against this project's managed command shapes
(`python3 -m http_server ... --log <root>/.runtime/...` for the bridge,
`ngrok http <port>` for ngrok). Verification failure = stale/unmanaged =
report only, never kill.

Pure string-matcher tests always run. Live-process tests spawn throwaway
`sleep` children with fake argv0 (`exec -a`) and are skipped when `ps` is not
available (e.g. inside a seatbelt-sandboxed session); they run on normal
Terminals and CI.
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "scripts", "pid_guard_lib.sh")
STOP = os.path.join(ROOT, "scripts", "stop_ngrok_bridge.sh")
START = os.path.join(ROOT, "scripts", "start_ngrok_bridge.sh")

BRIDGE_CMD = (
    "python3 -m http_server --host 127.0.0.1 --port 8321 "
    "--log {root}/.runtime/bridge.log"
)
NGROK_CMD = "ngrok http 8321 --url https://example.ngrok-free.app"


def bash(script, args=()):
    return subprocess.run(
        ["bash", "-c", 'lib="$1"; shift; . "$lib"\n' + script, "test-sh", LIB, *args],
        capture_output=True, text=True,
    )


def ps_available():
    proc = subprocess.run(
        ["bash", "-c", 'lib="$1"; shift; . "$lib"\nproc_command $$', "test-sh", LIB],
        capture_output=True, text=True,
    )
    return bool(proc.stdout.strip())


class PureMatcherTest(unittest.TestCase):

    def test_is_bridge_command_matches_own_launch_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = BRIDGE_CMD.format(root=tmp)
            ok = bash('is_bridge_command "$1" "$2"', [cmd, tmp]).returncode
            self.assertEqual(ok, 0)

    def test_is_bridge_command_rejects_wrong_root_and_other_processes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            cmd = BRIDGE_CMD.format(root=tmp)
            self.assertNotEqual(bash('is_bridge_command "$1" "$2"', [cmd, other]).returncode, 0)
            for foreign in ("sleep 30", "python3 -m http.server", "ngrok http 8321",
                            "ps aux", "python3 -m http_server --port 9000"):
                self.assertNotEqual(
                    bash('is_bridge_command "$1" "$2"', [foreign, tmp]).returncode, 0,
                    foreign,
                )

    def test_is_ngrok_command_matches_own_launch_shape(self):
        ok = bash('is_ngrok_command "$1" "8321"', [NGROK_CMD]).returncode
        self.assertEqual(ok, 0)

    def test_is_ngrok_command_rejects_wrong_port_and_other_processes(self):
        self.assertNotEqual(bash('is_ngrok_command "$1" "8322"', [NGROK_CMD]).returncode, 0)
        for foreign in ("sleep 30", "curl http://127.0.0.1:8321", "ngrok tcp 8321"):
            self.assertNotEqual(
                bash('is_ngrok_command "$1" "8321"', [foreign]).returncode, 0, foreign
            )

    def test_report_unmanaged_mentions_no_kill(self):
        proc = bash('report_unmanaged bridge 424242 "sleep 30" 2>&1; echo rc=$?')
        self.assertIn("NOT killing", proc.stdout)
        self.assertIn("bridge", proc.stdout)

    def test_managed_checks_reject_invalid_pids(self):
        self.assertNotEqual(bash('managed_bridge_pid "" "/tmp"').returncode, 0)
        self.assertNotEqual(bash('managed_bridge_pid "abc" "/tmp"').returncode, 0)
        self.assertNotEqual(bash('managed_ngrok_pid "1" "8321"').returncode, 0)
        self.assertNotEqual(bash('managed_bridge_pid "999999" "/tmp"').returncode, 0)

    def test_start_stop_glue_uses_guard_before_kill_or_reuse(self):
        with open(STOP, encoding="utf-8") as fh:
            stop = fh.read()
        with open(START, encoding="utf-8") as fh:
            start = fh.read()
        for text, needle in (
            (stop, "pid_guard_lib.sh"),
            (start, "pid_guard_lib.sh"),
            (stop, "managed_bridge_pid"),
            (stop, "managed_ngrok_pid"),
            (start, "managed_bridge_pid"),
            (start, "managed_ngrok_pid"),
            (stop, "report_unmanaged"),
            (start, "report_unmanaged"),
        ):
            self.assertIn(needle, text, needle)
        # in stop: identity check must come before any kill
        self.assertLess(stop.index("managed_bridge_pid"), stop.index('kill "$pid"'))
        self.assertLess(stop.index("managed_ngrok_pid"), stop.index('kill "$pid"'))


@unittest.skipUnless(ps_available(), "ps not available in this sandbox; run in a normal Terminal or CI")
class LiveProcessTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        self.tmp.cleanup()

    def spawn_fake(self, argv0):
        proc = subprocess.Popen(
            ["bash", "-c", 'exec -a "$1" sleep 5', "fake", argv0],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        return str(proc.pid)

    def test_managed_bridge_pid_accepts_matching_live_process(self):
        cmd = BRIDGE_CMD.format(root=self.tmp.name)
        pid = self.spawn_fake(cmd)
        ok = bash('managed_bridge_pid "$1" "$2"', [pid, self.tmp.name]).returncode
        self.assertEqual(ok, 0)

    def test_managed_ngrok_pid_accepts_matching_live_process(self):
        pid = self.spawn_fake(NGROK_CMD)
        ok = bash('managed_ngrok_pid "$1" "8321"', [pid]).returncode
        self.assertEqual(ok, 0)

    def test_live_unrelated_process_is_rejected(self):
        pid = self.spawn_fake("sleep 5")
        self.assertNotEqual(bash('managed_bridge_pid "$1" "$2"', [pid, self.tmp.name]).returncode, 0)
        self.assertNotEqual(bash('managed_ngrok_pid "$1" "8321"', [pid]).returncode, 0)
        # still alive afterwards: the guard never killed it
        proc = bash('pid_alive "$1"', [pid])
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
