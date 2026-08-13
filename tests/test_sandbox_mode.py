#!/usr/bin/env python3
"""Offline tests for the persistent sandbox-mode file tooling.

Covers scripts/bridge_mode_lib.sh precedence (env > file > default),
scripts/set_bridge_sandbox_mode.sh (validate / write / unset / show). All file
writes go to temp paths via BRIDGE_SANDBOX_MODE_FILE; no secrets, no live
Bridge interaction.
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "scripts", "bridge_mode_lib.sh")
SETTER = os.path.join(ROOT, "scripts", "set_bridge_sandbox_mode.sh")
MODES = ("workspace-write", "bridge-workspace", "danger-full-access")


def run_bash(script, env_extra=None, cwd=None, args=()):
    env = dict(os.environ)
    # The ambient BRIDGE_SANDBOX_MODE (e.g. from the calling shell) must not
    # leak in: precedence tests set it explicitly when they mean to.
    env.pop("BRIDGE_SANDBOX_MODE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", script, "test-sh", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


def effective(mode_file, env_mode=""):
    env = {"BRIDGE_SANDBOX_MODE_FILE": mode_file}
    if env_mode:
        env["BRIDGE_SANDBOX_MODE"] = env_mode
    proc = run_bash(
        '. "$1"; bridge_mode_effective "$2"',
        env_extra=env, cwd=ROOT, args=(LIB, ROOT),
    )
    return proc


class ModeLibTest(unittest.TestCase):

    def test_precedence_env_over_file_over_default(self):
        with tempfile.TemporaryDirectory() as d:
            mode_file = os.path.join(d, "mode")
            # 1) missing file + no env -> default
            out = effective(mode_file)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.strip(), "workspace-write")
            # 2) file only -> file wins
            with open(mode_file, "w") as fh:
                fh.write("bridge-workspace\n")
            out = effective(mode_file)
            self.assertEqual(out.stdout.strip(), "bridge-workspace")
            # 3) env wins over file
            out = effective(mode_file, env_mode="danger-full-access")
            self.assertEqual(out.stdout.strip(), "danger-full-access")
            # 4) env empty string does not win
            out = effective(mode_file, env_mode="")
            self.assertEqual(out.stdout.strip(), "bridge-workspace")

class SetterTest(unittest.TestCase):

    def test_valid_modes_roundtrip(self):
        for mode in MODES:
            with tempfile.TemporaryDirectory() as d:
                mode_file = os.path.join(d, "mode")
                proc = run_bash(
                    '"$1" "$2"',
                    env_extra={"BRIDGE_SANDBOX_MODE_FILE": mode_file},
                    cwd=ROOT, args=(SETTER, mode),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                with open(mode_file) as fh:
                    self.assertEqual(fh.read().strip(), mode)

    def test_setter_writes_600_and_show(self):
        with tempfile.TemporaryDirectory() as d:
            mode_file = os.path.join(d, "mode")
            env = {"BRIDGE_SANDBOX_MODE_FILE": mode_file}
            proc = run_bash('"$1" "$2"', env_extra=env, cwd=ROOT,
                            args=(SETTER, "bridge-workspace"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(mode_file) as fh:
                self.assertEqual(fh.read().strip(), "bridge-workspace")
            self.assertEqual(
                stat.S_IMODE(os.stat(mode_file).st_mode), 0o600
            )
            proc = run_bash('"$1"', env_extra=env, cwd=ROOT, args=(SETTER,))
            self.assertIn("bridge-workspace", proc.stdout)
            self.assertIn("file", proc.stdout)

    def test_setter_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            mode_file = os.path.join(d, "mode")
            proc = run_bash(
                '"$1" "$2"',
                env_extra={"BRIDGE_SANDBOX_MODE_FILE": mode_file},
                cwd=ROOT, args=(SETTER, "full-access"),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(os.path.exists(mode_file))

    def test_setter_unset_removes_file(self):
        with tempfile.TemporaryDirectory() as d:
            mode_file = os.path.join(d, "mode")
            env = {"BRIDGE_SANDBOX_MODE_FILE": mode_file}
            run_bash('"$1" "$2"', env_extra=env, cwd=ROOT,
                     args=(SETTER, "bridge-workspace"))
            self.assertTrue(os.path.exists(mode_file))
            proc = run_bash('"$1" --unset', env_extra=env, cwd=ROOT,
                            args=(SETTER,))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(mode_file))


class WriteGuardTest(unittest.TestCase):

    def test_lib_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            mode_file = os.path.join(d, "mode")
            env = {"BRIDGE_SANDBOX_MODE_FILE": mode_file}
            # Sourcing the lib must not expose a writer...
            proc = run_bash(
                '. "$1"; type bridge_mode_write >/dev/null 2>&1',
                env_extra=env, cwd=ROOT, args=(LIB,),
            )
            self.assertNotEqual(proc.returncode, 0)
            # ...and read-only lookups must not create the mode file.
            proc = run_bash(
                '. "$1"; bridge_mode_effective "$2"',
                env_extra=env, cwd=ROOT, args=(LIB, ROOT),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "workspace-write")
            self.assertFalse(os.path.exists(mode_file))

    def test_only_setter_writes_mode_file(self):
        offenders = []
        for directory in ("scripts", "tests"):
            base = os.path.join(ROOT, directory)
            for name in sorted(os.listdir(base)):
                if not (name.endswith(".sh") or name.endswith(".py")):
                    continue
                if directory == "scripts" and name == "set_bridge_sandbox_mode.sh":
                    continue
                if directory == "tests" and name == "test_sandbox_mode.py":
                    continue
                with open(os.path.join(base, name), encoding="utf-8",
                          errors="replace") as fh:
                    text = fh.read()
                if re.search(r"bridge_mode_write|\.bridge_sandbox_mode\s*>", text):
                    offenders.append(os.path.join(directory, name))
        self.assertEqual(
            offenders, [],
            "only scripts/set_bridge_sandbox_mode.sh may write .bridge_sandbox_mode",
        )

if __name__ == "__main__":
    unittest.main()
