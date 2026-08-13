#!/usr/bin/env python3
"""Offline tests for the pinned Bridge instances (local | hpc).

Covers: instance state living OUTSIDE the repo under
${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/,
BRIDGE_INSTANCE pinning at startup (default local, no switchable
active-profile mechanism), the local/hpc policy matrix (modes, approval,
network, separate CODEX_HOME/port/runtime; hpc never danger-full-access),
admin-only writes (scripts/bridge_instance.sh is the only instance-state
writer; read-only lib), file/dir permissions (600 config, 700 dirs), legacy
singleton fallback when the pinned instance has no config, and the
non-secret instance config content. All writes go to temp dirs via
BRIDGE_STATE_ROOT; no secrets, no live Bridge interaction.
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "scripts", "bridge_instance_lib.sh")
MODE_LIB = os.path.join(ROOT, "scripts", "bridge_mode_lib.sh")
ADMIN = os.path.join(ROOT, "scripts", "bridge_instance.sh")
START = os.path.join(ROOT, "scripts", "start_ngrok_bridge.sh")
STATUS = os.path.join(ROOT, "scripts", "status_launch_agent.sh")
STOP = os.path.join(ROOT, "scripts", "stop_ngrok_bridge.sh")

SCHEMA_KEYS = ("name", "mode", "approval_policy", "network_access",
               "codex_home", "port", "runtime_dir",
               "api_key_file", "ngrok_domain_file")


def run_bash(script, env_extra=None, cwd=None, args=()):
    env = dict(os.environ)
    # Ambient instance/mode state must not leak in: tests set it explicitly.
    for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT", "XDG_STATE_HOME",
                "BRIDGE_SANDBOX_MODE", "BRIDGE_SANDBOX_MODE_FILE"):
        env.pop(key, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", script, "test-sh", *args],
        capture_output=True, text=True, env=env, cwd=cwd,
    )


def temp_state_root():
    tmp = tempfile.mkdtemp(prefix="lcb-state-")
    return tmp


def resolve_like_start(state_root, env_instance=""):
    """Mirror the instance resolution in scripts/start_ngrok_bridge.sh:
    BRIDGE_INSTANCE (default local) -> config present or legacy fallback."""
    env = {"BRIDGE_STATE_ROOT": state_root}
    if env_instance:
        env["BRIDGE_INSTANCE"] = env_instance
    return run_bash(
        '. "$1"; requested="$(bridge_instance_effective)" || { echo invalid; exit 1; }; '
        'if bridge_instance_exists "$requested"; then '
        'mode="$(bridge_instance_get "$requested" mode)"; '
        'allowed="$(bridge_instance_mode "$requested")"; '
        '[[ "$mode" == "$allowed" ]] || { echo "badmode=$mode"; exit 1; }; '
        'printf "instance=%s mode=%s\\n" "$requested" "$mode"; '
        'else printf "instance= legacy\\n"; fi',
        env_extra=env, cwd=ROOT, args=(LIB,),
    )


def source_start(state_root, env_instance="", extra_cmds=""):
    """Source scripts/start_ngrok_bridge.sh (main flow is guarded when
    sourced) and run resolve_instance plus optional extra commands. No
    process is spawned and no network is touched; only instance config in
    temp BRIDGE_STATE_ROOT is read."""
    env = {"BRIDGE_STATE_ROOT": state_root}
    if env_instance:
        env["BRIDGE_INSTANCE"] = env_instance
    return run_bash(
        '. "$1"; resolve_instance; '
        'printf "instance=%s\\n" "${INSTANCE:-}"; '
        'printf "runtime=%s\\n" "$RUNTIME_DIR"; '
        'printf "pid=%s|%s\\n" "$BRIDGE_PID_FILE" "$NGROK_PID_FILE"; '
        'printf "logs=%s|%s|%s\\n" "$BRIDGE_LOG" "$BRIDGE_OUT_LOG" "$NGROK_LOG"; '
        'printf "public=%s\\n" "$PUBLIC_URL_FILE"; '
        + extra_cmds,
        env_extra=env, cwd=ROOT, args=(START, LIB),
    )


def run_admin(args, state_root, env_extra=None, cwd=None):
    env = dict(os.environ)
    for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT", "XDG_STATE_HOME",
                "BRIDGE_SANDBOX_MODE", "BRIDGE_SANDBOX_MODE_FILE"):
        env.pop(key, None)
    env["BRIDGE_STATE_ROOT"] = state_root
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", ADMIN] + list(args),
        capture_output=True, text=True, env=env, cwd=cwd,
    )


class StateLocationTest(unittest.TestCase):

    def test_default_state_root_outside_repo(self):
        env = dict(os.environ)
        env.pop("BRIDGE_STATE_ROOT", None)
        env.pop("XDG_STATE_HOME", None)
        proc = subprocess.run(
            ["bash", "-c", '. "$1"; bridge_instance_state_root', "test-sh", LIB],
            capture_output=True, text=True, env=env,
        )
        expected = os.path.join(os.path.expanduser("~"), ".local", "state",
                                "local-codex-bridge")
        self.assertEqual(proc.stdout.strip(), expected)
        self.assertNotIn(ROOT, proc.stdout)

    def test_state_root_override_and_instance_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "state")
            proc = run_bash(
                '. "$1"; printf "%s|%s|%s" '
                '"$(bridge_instance_state_root)" '
                '"$(bridge_instance_dir local)" '
                '"$(bridge_instance_config local)"',
                env_extra={"BRIDGE_STATE_ROOT": root},
                cwd=ROOT, args=(LIB,),
            )
            out = proc.stdout.split("|")
            self.assertEqual(out[0], root)
            self.assertEqual(out[1], os.path.join(root, "local"))
            self.assertEqual(out[2], os.path.join(root, "local", "instance.conf"))
            self.assertTrue(out[1].startswith(root))
            self.assertNotIn(ROOT, out[1])


class PolicyTest(unittest.TestCase):

    def test_mode_mapping_never_danger_full_access(self):
        out = run_bash('. "$1"; bridge_instance_mode local', cwd=ROOT, args=(LIB,))
        self.assertEqual(out.stdout.strip(), "bridge-workspace")
        out = run_bash('. "$1"; bridge_instance_mode hpc', cwd=ROOT, args=(LIB,))
        self.assertEqual(out.stdout.strip(), "workspace-write")
        out = run_bash('. "$1"; bridge_instance_mode maintenance', cwd=ROOT, args=(LIB,))
        self.assertEqual(out.stdout.strip(), "bridge-workspace")
        for name in ("local", "hpc", "maintenance"):
            out = run_bash('. "$1"; bridge_instance_mode "$2"', cwd=ROOT, args=(LIB, name))
            self.assertNotIn("danger-full-access", out.stdout)

    def test_local_template(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            proc = run_admin(["create", "local"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cfg = os.path.join(state, "local", "instance.conf")
            with open(cfg) as fh:
                text = fh.read()
            self.assertIn('mode = "bridge-workspace"', text)
            self.assertIn('approval_policy = "on-request"', text)
            self.assertIn("network_access = \"true\"", text)
            self.assertIn('codex_home = "%s"' % os.path.join(os.path.expanduser("~"), ".codex-deepseek"), text)
            self.assertIn('port = "8321"', text)
            self.assertIn('runtime_dir = "%s"' % os.path.join(state, "local", "runtime"), text)

    def test_hpc_template_separate_and_no_danger(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            proc = run_admin(["create", "hpc"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cfg = os.path.join(state, "hpc", "instance.conf")
            with open(cfg) as fh:
                text = fh.read()
            self.assertIn('mode = "workspace-write"', text)
            self.assertIn('approval_policy = "on-request"', text)
            self.assertIn("network_access = \"true\"", text)
            self.assertIn('codex_home = "%s"' % os.path.join(os.path.expanduser("~"), ".codex-deepseek-hpc"), text)
            self.assertIn('port = "8322"', text)
            self.assertNotIn("danger-full-access", text)
        # separate codex_home / port / runtime vs local
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["create", "hpc"], state)
            with open(os.path.join(state, "local", "instance.conf")) as fh:
                local = fh.read()
            with open(os.path.join(state, "hpc", "instance.conf")) as fh:
                hpc = fh.read()
            for key in ("codex_home", "port", "runtime_dir"):
                self.assertNotEqual(
                    re.search(r'%s = "([^"]*)"' % key, local).group(1),
                    re.search(r'%s = "([^"]*)"' % key, hpc).group(1),
                    key,
                )

    def test_admin_rejects_danger_full_access_for_hpc(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "hpc"], state)
            proc = run_admin(["update", "hpc", "mode=danger-full-access"], state)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("danger-full-access", proc.stderr)
            cfg = os.path.join(state, "hpc", "instance.conf")
            with open(cfg) as fh:
                self.assertIn('mode = "workspace-write"', fh.read())

    def test_admin_rejects_danger_full_access_for_maintenance(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "maintenance"], state)
            proc = run_admin(["update", "maintenance", "mode=danger-full-access"], state)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("danger-full-access", proc.stderr)
            cfg = os.path.join(state, "maintenance", "instance.conf")
            with open(cfg) as fh:
                self.assertIn('mode = "bridge-workspace"', fh.read())

    def test_effective_defaults_to_local_and_env_pins_instance(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            out = resolve_like_start(state)
            self.assertEqual(out.stdout.strip(), "instance= legacy")
            run_admin(["create", "local"], state)
            out = resolve_like_start(state)
            self.assertEqual(out.stdout.strip(), "instance=local mode=bridge-workspace")
            run_admin(["create", "hpc"], state)
            out = resolve_like_start(state, env_instance="hpc")
            self.assertEqual(out.stdout.strip(), "instance=hpc mode=workspace-write")
            out = resolve_like_start(state, env_instance="bogus")
            self.assertEqual(out.stdout.strip(), "invalid")
            self.assertNotEqual(out.returncode, 0)

    def test_tampered_mode_is_rejected_at_start_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            cfg = os.path.join(state, "local", "instance.conf")
            with open(cfg) as fh:
                text = fh.read().replace(
                'mode = "bridge-workspace"', 'mode = "danger-full-access"')
            with open(cfg, "w") as fh:
                fh.write(text)
            out = resolve_like_start(state)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("badmode=danger-full-access", out.stdout)


class AdminOnlyWriterTest(unittest.TestCase):

    def test_only_bridge_instance_sh_writes_instance_state(self):
        offenders = []
        pattern = re.compile(
            r"bridge_instance_write|active_profile|\.bridge-control|bridge_profile_|"
            r"instance\.conf\s*>"
        )
        sanctioned = {ADMIN}
        for directory in ("scripts", "tests", "http_server", "bridge"):
            base = os.path.join(ROOT, directory)
            for name in sorted(os.listdir(base)):
                path = os.path.join(base, name)
                if not (name.endswith(".sh") or name.endswith(".py")):
                    continue
                if path in sanctioned:
                    continue
                if directory == "tests" and name == "test_instance_isolation.py":
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                if pattern.search(text):
                    offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(
            offenders, [],
            "only scripts/bridge_instance.sh may write instance state; "
            "the switchable active_profile mechanism must not exist",
        )

    def test_lib_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            env = {"BRIDGE_STATE_ROOT": state}
            proc = run_bash(
                '. "$1"; type bridge_instance_write >/dev/null 2>&1',
                env_extra=env, cwd=ROOT, args=(LIB,),
            )
            self.assertNotEqual(proc.returncode, 0)
            # read-only lookups must not create any state
            proc = run_bash(
                '. "$1"; bridge_instance_get local mode; bridge_instance_effective',
                env_extra=env, cwd=ROOT, args=(LIB,),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(state, "local")))

    def test_update_writes_backup_first(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            cfg = os.path.join(state, "local", "instance.conf")
            before = open(cfg).read()
            proc = run_admin(["update", "local", "port=8421"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            backups = os.listdir(os.path.join(state, "local", "backups"))
            self.assertEqual(len(backups), 1)
            backup = os.path.join(state, "local", "backups", backups[0])
            self.assertEqual(open(backup).read(), before)
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
            with open(cfg) as fh:
                self.assertIn('port = "8421"', fh.read())

    def test_create_permissions_600_700(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            cfg = os.path.join(state, "local", "instance.conf")
            self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)
            for sub in ("local", os.path.join("local", "backups"),
                        os.path.join("local", "runtime")):
                self.assertEqual(
                    stat.S_IMODE(os.stat(os.path.join(state, sub)).st_mode), 0o700,
                    sub,
                )


class LegacyFallbackTest(unittest.TestCase):

    def test_absent_config_falls_back_to_legacy_mode_chain(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            mode_file = os.path.join(d, "mode")
            env = {"BRIDGE_STATE_ROOT": state, "BRIDGE_SANDBOX_MODE_FILE": mode_file}
            # no instance config, no legacy mode file -> default workspace-write
            proc = run_bash(
                '. "$1"; . "$2"; '
                'requested="$(bridge_instance_effective)"; '
                'if bridge_instance_exists "$requested"; then echo instance; '
                'else echo "legacy mode=$(bridge_mode_effective "$3")"; fi',
                env_extra=env, cwd=ROOT, args=(LIB, MODE_LIB, ROOT),
            )
            self.assertEqual(proc.stdout.strip(), "legacy mode=workspace-write")
            # legacy mode file still honored when no instance config exists
            with open(mode_file, "w") as fh:
                fh.write("bridge-workspace\n")
            proc = run_bash(
                '. "$1"; . "$2"; '
                'requested="$(bridge_instance_effective)"; '
                'if bridge_instance_exists "$requested"; then echo instance; '
                'else echo "legacy mode=$(bridge_mode_effective "$3")"; fi',
                env_extra=env, cwd=ROOT, args=(LIB, MODE_LIB, ROOT),
            )
            self.assertEqual(proc.stdout.strip(), "legacy mode=bridge-workspace")

    def test_start_status_stop_source_instance_lib(self):
        for path in (START, STATUS, STOP):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("bridge_instance_lib.sh", text, path)
            self.assertIn("bridge_instance_effective", text, path)
            self.assertNotIn("bridge_profile", text, path)
            self.assertNotIn("active_profile", text, path)


class RuntimePathIsolationTest(unittest.TestCase):
    """P1: every start runtime path must derive from the effective
    RUNTIME_DIR (instance runtime or legacy $ROOT/.runtime); explicit
    local/hpc instances must never share runtime files; stop/status must
    resolve the same paths; collision fails closed before any runtime file
    is touched."""

    def _dump(self, state_root, instance, extra_cmds=""):
        return source_start(state_root, instance, extra_cmds)

    def test_start_local_derives_all_runtime_files_under_instance_runtime(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            proc = self._dump(state, "local")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = dict(
                ln.split("=", 1) for ln in proc.stdout.splitlines() if "=" in ln)
            runtime = os.path.join(state, "local", "runtime")
            self.assertEqual(lines["instance"], "local")
            self.assertEqual(lines["runtime"], runtime)
            self.assertNotEqual(runtime, os.path.join(ROOT, ".runtime"))
            self.assertEqual(lines["pid"], "%s|%s" % (
                os.path.join(runtime, "bridge.pid"),
                os.path.join(runtime, "ngrok.pid")))
            self.assertEqual(lines["logs"], "%s|%s|%s" % (
                os.path.join(runtime, "bridge.log"),
                os.path.join(runtime, "bridge.out.log"),
                os.path.join(runtime, "ngrok.log")))
            self.assertEqual(lines["public"], os.path.join(state, "local", "public_url"))
            self.assertNotIn(os.path.join(ROOT, ".runtime"), proc.stdout)

    def test_start_hpc_derives_distinct_runtime_files(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["create", "hpc"], state)
            proc = self._dump(state, "hpc")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = dict(
                ln.split("=", 1) for ln in proc.stdout.splitlines() if "=" in ln)
            runtime = os.path.join(state, "hpc", "runtime")
            self.assertEqual(lines["instance"], "hpc")
            self.assertEqual(lines["runtime"], runtime)
            self.assertEqual(lines["pid"], "%s|%s" % (
                os.path.join(runtime, "bridge.pid"),
                os.path.join(runtime, "ngrok.pid")))
            self.assertEqual(lines["logs"], "%s|%s|%s" % (
                os.path.join(runtime, "bridge.log"),
                os.path.join(runtime, "bridge.out.log"),
                os.path.join(runtime, "ngrok.log")))
            self.assertNotIn(os.path.join(state, "local", "runtime"), lines["pid"])
            self.assertNotIn(os.path.join(ROOT, ".runtime"), proc.stdout)

    def test_legacy_fallback_keeps_repo_runtime_paths(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            proc = self._dump(state, "")  # no configs -> legacy fallback
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = dict(
                ln.split("=", 1) for ln in proc.stdout.splitlines() if "=" in ln)
            repo_runtime = os.path.join(ROOT, ".runtime")
            self.assertEqual(lines["instance"], "")
            self.assertEqual(lines["runtime"], repo_runtime)
            self.assertEqual(lines["pid"], "%s|%s" % (
                os.path.join(repo_runtime, "bridge.pid"),
                os.path.join(repo_runtime, "ngrok.pid")))
            self.assertEqual(lines["logs"], "%s|%s|%s" % (
                os.path.join(repo_runtime, "bridge.log"),
                os.path.join(repo_runtime, "bridge.out.log"),
                os.path.join(repo_runtime, "ngrok.log")))
            self.assertEqual(lines["public"], os.path.join(ROOT, ".public_url"))

    def test_start_lock_is_per_instance(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["create", "hpc"], state)
            # local instance lock
            proc = self._dump(state, "local",
                              'acquire_start_lock; '
                              'printf "lock=%s\\n" "$RUNTIME_DIR/start.lock"; '
                              'release_start_lock; ')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("lock=%s/start.lock" % os.path.join(state, "local", "runtime"),
                          proc.stdout)
            self.assertFalse(os.path.exists(os.path.join(ROOT, ".runtime", "start.lock")))
            # hpc instance lock is distinct
            proc = self._dump(state, "hpc",
                              'acquire_start_lock; '
                              'printf "lock=%s\\n" "$RUNTIME_DIR/start.lock"; '
                              'release_start_lock; ')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("lock=%s/start.lock" % os.path.join(state, "hpc", "runtime"),
                          proc.stdout)

    def test_pid_identity_uses_derived_log_path(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            proc = self._dump(
                state, "local",
                '. "$2"; set +e; '
                'is_bridge_command "python3 -m http_server --host 127.0.0.1 '
                '--port 8321 --log $BRIDGE_LOG" "$ROOT" "$RUNTIME_DIR"; '
                'printf "match=%s\\n" "$?"; '
                'is_bridge_command "python3 -m http_server --log $BRIDGE_LOG" '
                '"$ROOT"; '
                'printf "legacy_mismatch=%s\\n" "$?"; ')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("match=0", proc.stdout)
            self.assertIn("legacy_mismatch=1", proc.stdout)
        # static: ensure_bridge/cleanup_started verify identity against the
        # effective runtime dir, not just the repo .runtime
        with open(START, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(
            text.count('managed_bridge_pid "$pid" "$ROOT" "$RUNTIME_DIR"'), 2, text)

    def test_stop_and_status_resolve_same_paths(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            home = os.path.join(d, "home")
            os.makedirs(home)
            run_admin(["create", "local"], state)
            stop = run_bash(
                'bash "$1"',
                env_extra={"BRIDGE_STATE_ROOT": state, "BRIDGE_INSTANCE": "local"},
                cwd=ROOT, args=(STOP,),
            )
            self.assertEqual(stop.returncode, 0, stop.stderr)
            self.assertIn("runtime: %s" % os.path.join(state, "local", "runtime"),
                          stop.stdout)
            self.assertIn(os.path.join(state, "local", "runtime", "bridge.pid"),
                          stop.stdout)
            status = run_bash(
                'bash "$1"',
                env_extra={"BRIDGE_STATE_ROOT": state, "BRIDGE_INSTANCE": "local",
                           "HOME": home},
                cwd=ROOT, args=(STATUS,),
            )
            self.assertIn("runtime_dir: %s" % os.path.join(state, "local", "runtime"),
                          status.stdout)
            self.assertIn("port: 8321", status.stdout)

    def test_collision_fails_closed_before_runtime_files_touched(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["create", "hpc"], state)
            run_admin(["update", "hpc", "port=8321"], state)
            proc = run_bash(
                '. "$1"; resolve_instance',
                env_extra={"BRIDGE_STATE_ROOT": state, "BRIDGE_INSTANCE": "hpc"},
                cwd=ROOT, args=(START,),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("collision", proc.stderr)
            self.assertIn("port", proc.stderr)
            for runtime in (os.path.join(state, "local", "runtime"),
                            os.path.join(state, "hpc", "runtime")):
                self.assertFalse(os.path.exists(os.path.join(runtime, "start.lock")),
                                 "collision must fail before the start lock is taken")
                self.assertFalse(os.path.exists(os.path.join(runtime, "bridge.pid")))
                self.assertFalse(os.path.exists(os.path.join(runtime, "ngrok.pid")))
            self.assertFalse(os.path.exists(os.path.join(ROOT, ".runtime", "start.lock")))


class MigrateCurrentTest(unittest.TestCase):

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            proc = run_admin(["migrate-current", "--dry-run"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(os.path.join(state, "local")))

    def test_apply_creates_local_from_singleton_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            singleton = os.path.join(ROOT, ".bridge_sandbox_mode")
            before = None
            if os.path.exists(singleton):
                before = open(singleton, "rb").read()
            proc = run_admin(["migrate-current", "--apply"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cfg = os.path.join(state, "local", "instance.conf")
            self.assertTrue(os.path.exists(cfg))
            with open(cfg) as fh:
                text = fh.read()
            self.assertIn('mode = "bridge-workspace"', text)
            self.assertIn('approval_policy = "on-request"', text)
            if before is not None:
                self.assertEqual(open(singleton, "rb").read(), before,
                                 "migrate-current must never modify legacy state")
            key_file = os.path.join(ROOT, ".bridge_api_key")
            domain_file = os.path.join(ROOT, ".ngrok_domain")
            if os.path.exists(key_file):
                with open(key_file, "rb") as fh:
                    key_content = fh.read()
                self.assertNotIn(key_content, text.encode("utf-8"),
                                 "instance config must never contain key material")
                self.assertIn('api_key_file = "%s"' % key_file, text,
                              "migration references the secret file path only")
            if os.path.exists(domain_file):
                with open(domain_file, "rb") as fh:
                    domain_content = fh.read()
                self.assertNotIn(domain_content, text.encode("utf-8"),
                                 "instance config must never copy the domain file content")
                self.assertIn('ngrok_domain_file = "%s"' % domain_file, text)
            self.assertNotIn("token", text)
            self.assertNotIn("secret", text)
            self.assertNotIn("password", text)
            self.assertNotIn("sk-", text)

    def test_apply_is_idempotent_with_backup(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["migrate-current", "--apply"], state)
            run_admin(["migrate-current", "--apply"], state)
            backups = os.listdir(os.path.join(state, "local", "backups"))
            self.assertEqual(len(backups), 1)


class NoSecretsTest(unittest.TestCase):

    def test_config_holds_only_schema_keys(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            for name in ("local", "hpc", "maintenance"):
                run_admin(["create", name], state)
                cfg = os.path.join(state, name, "instance.conf")
                for line in open(cfg).read().splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    self.assertRegex(line, r'^[a-z_]+ = "[^"]*"$', line)
                    key = line.split("=", 1)[0].strip()
                    self.assertIn(key, SCHEMA_KEYS, line)
                    self.assertNotIn("sk-", line)

    def test_list_and_show_do_not_print_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            for args in (["list"], ["show", "local"]):
                proc = run_admin(args, state)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("sk-", proc.stdout)
                key_file = os.path.join(ROOT, ".bridge_api_key")
                if os.path.exists(key_file):
                    with open(key_file, "rb") as fh:
                        self.assertNotIn(
                            fh.read(), proc.stdout.encode("utf-8"),
                            "admin output must never print key material",
                        )


class NoSwitchableProfileTest(unittest.TestCase):

    def test_active_profile_mechanism_is_gone(self):
        for path in (os.path.join(ROOT, "scripts", "bridge_profile.sh"),
                     os.path.join(ROOT, "scripts", "bridge_profile_lib.sh"),
                     os.path.join(ROOT, "tests", "test_profile_isolation.py")):
            self.assertFalse(os.path.exists(path), path)
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            gitignore = fh.read()
        self.assertNotIn(".bridge-control", gitignore)
        self.assertFalse(os.path.exists(os.path.join(ROOT, ".bridge-control")))


class VerifyCommandTest(unittest.TestCase):

    def test_verify_ok_after_create(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            proc = run_admin(["verify", "local"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("verify: OK", proc.stdout)

    def test_verify_fails_on_tampered_mode_and_missing_config(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            proc = run_admin(["verify", "hpc"], state)
            self.assertNotEqual(proc.returncode, 0)
            run_admin(["create", "hpc"], state)
            cfg = os.path.join(state, "hpc", "instance.conf")
            with open(cfg) as fh:
                text = fh.read()
            with open(cfg, "w") as fh:
                fh.write(text.replace(
                    'mode = "workspace-write"', 'mode = "danger-full-access"'))
            proc = run_admin(["verify", "hpc"], state)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("danger-full-access", proc.stdout)

    def test_verify_fails_on_missing_referenced_file(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["update", "local", "api_key_file=/tmp/does-not-exist-xyz"], state)
            proc = run_admin(["verify", "local"], state)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("missing file", proc.stdout)


class CollisionTest(unittest.TestCase):

    def test_port_collision_detected_and_verify_fails(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["create", "hpc"], state)
            proc = run_bash(
                '. "$1"; bridge_instance_collision local',
                env_extra={"BRIDGE_STATE_ROOT": state},
                cwd=ROOT, args=(LIB,),
            )
            self.assertNotEqual(proc.returncode, 0)
            run_admin(["update", "hpc", "port=8321"], state)
            proc = run_bash(
                '. "$1"; bridge_instance_collision hpc',
                env_extra={"BRIDGE_STATE_ROOT": state},
                cwd=ROOT, args=(LIB,),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "port:local")
            proc = run_admin(["verify", "hpc"], state)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("port", proc.stdout)

    def test_runtime_collision_detected(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["create", "hpc"], state)
            local_runtime = os.path.join(state, "local", "runtime")
            run_admin(["update", "hpc", "runtime_dir=%s" % local_runtime], state)
            proc = run_bash(
                '. "$1"; bridge_instance_collision hpc',
                env_extra={"BRIDGE_STATE_ROOT": state},
                cwd=ROOT, args=(LIB,),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "runtime:local")

    def test_stop_fails_closed_on_collision(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["create", "hpc"], state)
            local_runtime = os.path.join(state, "local", "runtime")
            run_admin(["update", "hpc", "runtime_dir=%s" % local_runtime], state)
            proc = run_bash(
                'bash "$1"',
                env_extra={"BRIDGE_STATE_ROOT": state, "BRIDGE_INSTANCE": "hpc"},
                cwd=ROOT, args=(STOP,),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("collides", proc.stderr)
            self.assertIn("runtime", proc.stderr)

    def test_status_flags_collision(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            home = os.path.join(d, "home")
            os.makedirs(home)
            run_admin(["create", "local"], state)
            run_admin(["create", "hpc"], state)
            run_admin(["update", "hpc", "port=8321"], state)
            proc = run_bash(
                'bash "$1"',
                env_extra={"BRIDGE_STATE_ROOT": state, "BRIDGE_INSTANCE": "hpc",
                           "HOME": home},
                cwd=ROOT, args=(STATUS,),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("collides", proc.stderr)


class DomainIsolationTest(unittest.TestCase):

    def test_only_local_may_use_legacy_domain(self):
        proc = run_bash(
            '. "$1"; bridge_instance_may_use_legacy_domain local',
            cwd=ROOT, args=(LIB,),
        )
        self.assertEqual(proc.returncode, 0)
        for name in ("hpc", "maintenance"):
            proc = run_bash(
                '. "$1"; bridge_instance_may_use_legacy_domain "$2"',
                cwd=ROOT, args=(LIB, name),
            )
            self.assertNotEqual(proc.returncode, 0, name)

    def test_migration_references_domain_file_not_content(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            domain_file = os.path.join(ROOT, ".ngrok_domain")
            if os.path.exists(domain_file):
                with open(domain_file, "rb") as fh:
                    domain_content = fh.read()
            proc = run_admin(["migrate-current", "--apply"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(state, "local", "instance.conf")) as fh:
                text = fh.read()
            if os.path.exists(domain_file):
                self.assertIn('ngrok_domain_file = "%s"' % domain_file, text)
                self.assertNotIn(domain_content.decode("utf-8").strip(), text)


class SchemaRefsTest(unittest.TestCase):

    def test_new_fields_default_empty_and_update_validates(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            cfg = os.path.join(state, "local", "instance.conf")
            with open(cfg) as fh:
                text = fh.read()
            self.assertIn('api_key_file = ""', text)
            self.assertIn('ngrok_domain_file = ""', text)
            proc = run_admin(["update", "local", "api_key_file=relative.key"], state)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("absolute path", proc.stderr)
            proc = run_admin(["update", "local",
                              "api_key_file=/tmp/a.key",
                              "ngrok_domain_file=/tmp/d.domain"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(cfg) as fh:
                text = fh.read()
            self.assertIn('api_key_file = "/tmp/a.key"', text)
            self.assertIn('ngrok_domain_file = "/tmp/d.domain"', text)


class LaunchAgentPlumbingTest(unittest.TestCase):

    def test_install_uses_instance_label_and_env(self):
        with open(os.path.join(ROOT, "scripts", "install_launch_agent.sh"),
                  encoding="utf-8") as fh:
            install = fh.read()
        self.assertIn("--instance", install)
        # the local supervisor agent uses the fixed per-instance label and the
        # stable non-Desktop runtime path
        self.assertIn('LABEL="$(bridge_supervisor_label)"', install)
        self.assertIn("com.local.codex-bridge.local", install)
        self.assertIn("run_local_supervisor.sh", install)
        self.assertIn(".runtime-build-info", install)
        self.assertIn("runtime.manifest", install)
        self.assertIn("PathState", install)
        self.assertIn("supervisor.enabled", install)
        self.assertIn("BRIDGE_INSTANCE", install)
        self.assertIn("bridge_instance_lib.sh", install)
        self.assertIn("bridge_instance_collision", install)

    def test_uninstall_and_status_support_instance(self):
        with open(os.path.join(ROOT, "scripts", "uninstall_launch_agent.sh"),
                  encoding="utf-8") as fh:
            uninstall = fh.read()
        self.assertIn("--instance", uninstall)
        self.assertIn('com.local.codex-bridge.$INSTANCE', uninstall)
        with open(STATUS, encoding="utf-8") as fh:
            status = fh.read()
        self.assertIn("--instance", status)
        self.assertIn('com.local.codex-bridge.$SELECTED', status)

    def test_plist_template_has_label_and_log_placeholders(self):
        with open(os.path.join(ROOT, "scripts", "launch_agent",
                               "com.local.codex-bridge.plist"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("__LABEL__", text)
        self.assertIn("__LOG_DIR__", text)


if __name__ == "__main__":
    unittest.main()
