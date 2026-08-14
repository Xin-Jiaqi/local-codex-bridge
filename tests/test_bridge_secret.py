#!/usr/bin/env python3
"""Offline tests for scripts/bridge_secret_lib.sh (macOS Keychain secret ref).

Covers:
  - one-time env -> Keychain migration (value never printed);
  - present / get / load into the child environment;
  - fail-closed when the Keychain reference is unreadable (load returns 1);
  - the real start_ngrok_bridge.sh refuses to start the app-server when the
    Keychain ref is unreadable and DEEPSEEK_API_KEY is absent, and reports a
    clear readiness failure (no secret content, no bridge pid, no spawn);
  - no secret value ever appears on stdout/stderr/logs.
All Keychain interactions are redirected to a fake `security` binary backed
by a temp dir; the real login Keychain is never touched. No secrets here.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "scripts", "bridge_secret_lib.sh")
START = os.path.join(ROOT, "scripts", "start_ngrok_bridge.sh")
ADMIN = os.path.join(ROOT, "scripts", "bridge_instance.sh")

FAKE_VALUE = "fake-deepseek-key-0123456789abcdef"
FAKE_SERVICE = "lcb-test-service"
FAKE_ACCOUNT = "DEEPSEEK_API_KEY"


def write_fake_security(d, fail_read=False):
    """Functional fake `security` backed by a temp keychain dir."""
    keydir = os.path.join(d, "keychain")
    os.makedirs(keydir, exist_ok=True)
    script = os.path.join(d, "bin", "security")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'set -euo pipefail\n'
                 'KEYDIR="%s"\n'
                 'FAIL_READ="%s"\n'
                 'op="$1"; shift\n'
                 'case "$op" in\n'
                 '  find-generic-password)\n'
                 '    service=""; account=""; want=0\n'
                 '    while [[ $# -gt 0 ]]; do\n'
                 '      case "$1" in\n'
                 '        -w) want=1 ;;\n'
                 '        -s) service="$2"; shift ;;\n'
                 '        -a) account="$2"; shift ;;\n'
                 '      esac\n'
                 '      shift\n'
                 '    done\n'
                 '    f="$KEYDIR/${service}__${account}"\n'
                 '    if [[ "$FAIL_READ" == "1" || ! -f "$f" ]]; then exit 44; fi\n'
                 '    if [[ "$want" == "1" ]]; then cat "$f"; fi\n'
                 '    exit 0\n'
                 '    ;;\n'
                 '  add-generic-password)\n'
                 '    service=""; account=""; value=""\n'
                 '    while [[ $# -gt 0 ]]; do\n'
                 '      case "$1" in\n'
                 '        -s) service="$2"; shift ;;\n'
                 '        -a) account="$2"; shift ;;\n'
                 '        -w) value="$2"; shift ;;\n'
                 '      esac\n'
                 '      shift\n'
                 '    done\n'
                 '    mkdir -p "$KEYDIR"\n'
                 '    printf "%%s" "$value" > "$KEYDIR/${service}__${account}"\n'
                 '    exit 0\n'
                 '    ;;\n'
                 '  *) exit 2 ;;\n'
                 'esac\n' % (keydir, "1" if fail_read else "0"))
    os.chmod(script, 0o755)
    return script, keydir


def lib_env(d, security_bin, extra=None):
    env = dict(os.environ)
    for key in ("DEEPSEEK_API_KEY", "BRIDGE_KEYCHAIN_SERVICE",
                "BRIDGE_KEYCHAIN_ACCOUNT", "BRIDGE_SECURITY_BIN"):
        env.pop(key, None)
    env.update({
        "BRIDGE_KEYCHAIN_SERVICE": FAKE_SERVICE,
        "BRIDGE_KEYCHAIN_ACCOUNT": FAKE_ACCOUNT,
        "BRIDGE_SECURITY_BIN": security_bin,
    })
    if extra:
        env.update(extra)
    return env


def run_bash(env, script, args=()):
    return subprocess.run(
        ["bash", "-c", script, "test-sh", *args],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )


class BridgeSecretLibTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="lcb-secret-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.security, self.keydir = write_fake_security(self.d)

    def test_import_is_one_time_and_never_prints_value(self):
        env = lib_env(self.d, self.security, {"DEEPSEEK_API_KEY": FAKE_VALUE})
        r = run_bash(env, "source '%s'; bridge_secret_keychain_import" % LIB)
        self.assertEqual(r.returncode, 0, r.stderr)
        stored = open(os.path.join(self.keydir, "%s__%s" % (FAKE_SERVICE, FAKE_ACCOUNT)),
                      encoding="utf-8").read()
        self.assertEqual(stored, FAKE_VALUE)
        self.assertNotIn(FAKE_VALUE, r.stdout + r.stderr, "value must never be printed")

        # second import is a no-op even if env changes
        env2 = dict(env)
        env2["DEEPSEEK_API_KEY"] = "other-value"
        r2 = run_bash(env2, "source '%s'; bridge_secret_keychain_import; "
                            "bridge_secret_keychain_get" % LIB)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(r2.stdout.strip(), FAKE_VALUE,
                         "first migration wins; later env values never overwrite")

    def test_load_exports_keychain_value_into_env(self):
        env = lib_env(self.d, self.security, {"DEEPSEEK_API_KEY": FAKE_VALUE})
        run_bash(env, "source '%s'; bridge_secret_keychain_import" % LIB)
        env.pop("DEEPSEEK_API_KEY")
        r = run_bash(env, "source '%s'; bridge_secret_load_deepseek && "
                          "test \"${DEEPSEEK_API_KEY:-}\" = '%s' && "
                          "printf 'LOADED_OK'" % (LIB, FAKE_VALUE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "LOADED_OK")
        self.assertNotIn(FAKE_VALUE, r.stderr)

    def test_load_prefers_existing_env_without_keychain(self):
        env = lib_env(self.d, os.path.join(self.d, "missing-security"),
                      {"DEEPSEEK_API_KEY": FAKE_VALUE})
        r = run_bash(env, "source '%s'; bridge_secret_load_deepseek && "
                          "test \"${DEEPSEEK_API_KEY:-}\" = '%s' && "
                          "printf 'ENV_OK'" % (LIB, FAKE_VALUE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ENV_OK")

    def test_load_fails_closed_when_keychain_unreadable(self):
        env = lib_env(self.d, self.security)
        r = run_bash(env, "source '%s'; bridge_secret_load_deepseek" % LIB)
        self.assertNotEqual(r.returncode, 0, "load must fail without a secret ref")

    def test_check_fails_closed_when_keychain_unreadable(self):
        env = lib_env(self.d, self.security)
        r = run_bash(env, "source '%s'; bridge_secret_check" % LIB)
        self.assertNotEqual(r.returncode, 0)

    def test_keychain_import_requires_env_value(self):
        env = lib_env(self.d, self.security)
        r = run_bash(env, "source '%s'; bridge_secret_keychain_import" % LIB)
        self.assertNotEqual(r.returncode, 0, "import without a value must fail")


class StartScriptFailClosedTest(unittest.TestCase):
    """The real start_ngrok_bridge.sh must refuse to spawn the app-server
    when the Keychain ref is unreadable and DEEPSEEK_API_KEY is absent."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="lcb-start-fail-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = os.path.join(self.d, "state")
        env = dict(os.environ)
        for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT"):
            env.pop(key, None)
        env["BRIDGE_STATE_ROOT"] = self.state
        proc = subprocess.run(["bash", ADMIN, "create", "local"],
                              capture_output=True, text=True, env=env, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_start_refuses_when_keychain_unreadable(self):
        security, _ = write_fake_security(self.d, fail_read=True)
        env = dict(os.environ)
        for key in ("DEEPSEEK_API_KEY", "BRIDGE_KEYCHAIN_SERVICE",
                    "BRIDGE_KEYCHAIN_ACCOUNT", "BRIDGE_SECURITY_BIN",
                    "BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT",
                    "BRIDGE_SANDBOX_MODE", "NGROK_DOMAIN"):
            env.pop(key, None)
        env.update({
            "BRIDGE_STATE_ROOT": self.state,
            "BRIDGE_INSTANCE": "local",
            "BRIDGE_SANDBOX_MODE": "workspace-write",
            "BRIDGE_KEYCHAIN_SERVICE": FAKE_SERVICE,
            "BRIDGE_KEYCHAIN_ACCOUNT": FAKE_ACCOUNT,
            "BRIDGE_SECURITY_BIN": security,
            "NGROK_DOMAIN": "secret-test.invalid",
            "CODEX_BIN": "/usr/bin/true",
            "NGROK_BIN": "/usr/bin/true",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        })
        proc = subprocess.run(["bash", START], capture_output=True, text=True,
                              env=env, cwd=ROOT)
        self.assertNotEqual(proc.returncode, 0, "start must fail closed")
        self.assertIn("provider secret unavailable", proc.stderr)
        self.assertIn("refusing to start the app-server", proc.stderr)
        runtime = os.path.join(self.state, "local", "runtime")
        self.assertFalse(os.path.exists(os.path.join(runtime, "bridge.pid")),
                         "no bridge pid must be written without the secret")
        self.assertNotIn("starting bridge", proc.stdout + proc.stderr)


class StaticSourceTest(unittest.TestCase):

    def test_start_script_wires_secret_lib_before_spawn(self):
        text = open(START, encoding="utf-8").read()
        self.assertIn(". \"$ROOT/scripts/bridge_secret_lib.sh\"", text)
        self.assertIn("bridge_secret_load_deepseek", text)
        self.assertIn("export DEEPSEEK_API_KEY", text)
        # the load must happen inside ensure_bridge, before the spawn line
        spawn_at = text.index("python3 -m http_server")
        load_at = text.index("bridge_secret_load_deepseek")
        self.assertLess(load_at, spawn_at)

    def test_runtime_allowlist_includes_secret_lib(self):
        text = open(os.path.join(ROOT, "scripts", "install_runtime.sh"),
                    encoding="utf-8").read()
        self.assertIn("scripts/bridge_secret_lib.sh", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
