#!/usr/bin/env python3
"""Offline tests for the maintenance HOST-ADMIN instance (1.1.0).

Covers: the maintenance template/policy (bridge-workspace, on-request,
network, ~/.codex-deepseek-maintenance, port 8323, dedicated runtime),
never danger-full-access, no automatic legacy/local ngrok domain
inheritance, collision rules including maintenance, list/install/status/
stop/start whitelists, the maintenance cwd guard + BridgeCore.start scope
dispatch + HTTP /start maintenance scope, /health instance/mode/port
metadata, the activate/deactivate host scripts (bash -n + security
invariants + no secret literals) and the OpenAPI health schema sync.
No live app-server is spawned and no secrets are printed.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - CI has pyyaml via test_http_api
    yaml = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge import BridgeCore, Logger
from bridge.workspace_guard import TaskCwdError
from http_server.server import BridgeHttpHandler, BridgeHttpServer, build_cwd_guard

LIB = os.path.join(ROOT, "scripts", "bridge_instance_lib.sh")
ADMIN = os.path.join(ROOT, "scripts", "bridge_instance.sh")
START = os.path.join(ROOT, "scripts", "start_ngrok_bridge.sh")
ACTIVATE = os.path.join(ROOT, "scripts", "activate_maintenance_instance.sh")
DEACTIVATE = os.path.join(ROOT, "scripts", "deactivate_maintenance_instance.sh")


def run_bash(script, env_extra=None, cwd=None, args=()):
    env = dict(os.environ)
    for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT", "XDG_STATE_HOME",
                "BRIDGE_SANDBOX_MODE", "BRIDGE_SANDBOX_MODE_FILE", "NGROK_DOMAIN"):
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


def source_start(state_root, env_instance="", extra_cmds=""):
    env = {"BRIDGE_STATE_ROOT": state_root}
    if env_instance:
        env["BRIDGE_INSTANCE"] = env_instance
    return run_bash(
        '. "$1"; resolve_instance; '
        'printf "instance=%s\\n" "${INSTANCE:-}"; '
        'printf "runtime=%s\\n" "$RUNTIME_DIR"; '
        'printf "pid=%s|%s\\n" "$BRIDGE_PID_FILE" "$NGROK_PID_FILE"; '
        + extra_cmds,
        env_extra=env, cwd=ROOT, args=(START, LIB),
    )


class MaintenancePolicyTest(unittest.TestCase):

    def test_maintenance_template_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            proc = run_admin(["create", "maintenance"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cfg = os.path.join(state, "maintenance", "instance.conf")
            with open(cfg) as fh:
                text = fh.read()
            self.assertIn('mode = "bridge-workspace"', text)
            self.assertIn('approval_policy = "on-request"', text)
            self.assertIn('network_access = "true"', text)
            self.assertIn('codex_home = "%s"' % os.path.join(
                os.path.expanduser("~"), ".codex-deepseek-maintenance"), text)
            self.assertIn('port = "8323"', text)
            self.assertIn('runtime_dir = "%s"' % os.path.join(
                state, "maintenance", "runtime"), text)
            # default template carries NO api_key/domain path references
            self.assertIn('api_key_file = ""', text)
            self.assertIn('ngrok_domain_file = ""', text)
            self.assertNotIn("danger-full-access", text)

    def test_maintenance_separate_from_local(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["create", "maintenance"], state)
            with open(os.path.join(state, "local", "instance.conf")) as fh:
                local = fh.read()
            with open(os.path.join(state, "maintenance", "instance.conf")) as fh:
                maint = fh.read()
            self.assertNotIn('port = "8321"', maint)
            self.assertNotEqual(
                re.search(r'codex_home = "([^"]*)"', local).group(1),
                re.search(r'codex_home = "([^"]*)"', maint).group(1))
            self.assertNotEqual(
                re.search(r'port = "([^"]*)"', local).group(1),
                re.search(r'port = "([^"]*)"', maint).group(1))
            self.assertNotEqual(
                re.search(r'runtime_dir = "([^"]*)"', local).group(1),
                re.search(r'runtime_dir = "([^"]*)"', maint).group(1))

    def test_maintenance_never_danger_full_access(self):
        out = run_bash('. "$1"; bridge_instance_mode maintenance', cwd=ROOT, args=(LIB,))
        self.assertEqual(out.stdout.strip(), "bridge-workspace")
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "maintenance"], state)
            proc = run_admin(["update", "maintenance", "mode=danger-full-access"], state)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("danger-full-access", proc.stderr)
            with open(os.path.join(state, "maintenance", "instance.conf")) as fh:
                self.assertIn('mode = "bridge-workspace"', fh.read())

    def test_maintenance_does_not_inherit_legacy_domain(self):
        proc = run_bash(
            '. "$1"; bridge_instance_may_use_legacy_domain maintenance',
            cwd=ROOT, args=(LIB,),
        )
        self.assertNotEqual(proc.returncode, 0)
        # maintenance without an explicit ngrok_domain_file must fail closed
        # (never fall back to the local domain)
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "maintenance"], state)
            proc = source_start(
                state, "maintenance",
                'set +e; resolve_public_url 2>&1; printf "rc=%s\\n" "$?"; ',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("no public tunnel domain for instance 'maintenance'",
                          proc.stdout + proc.stderr)

    def test_maintenance_port_collision_with_local_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "local"], state)
            run_admin(["create", "maintenance"], state)
            proc = run_admin(["update", "maintenance", "port=8321"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            proc = run_admin(["verify", "maintenance"], state)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("collision", proc.stdout + proc.stderr)
            proc = source_start(state, "maintenance")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("collision", proc.stdout + proc.stderr)

    def test_list_includes_maintenance(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "maintenance"], state)
            proc = run_admin(["list"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("maintenance", proc.stdout)
            self.assertIn("8323", proc.stdout)

    def test_start_resolves_maintenance_runtime(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "maintenance"], state)
            proc = source_start(state, "maintenance")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = dict(
                ln.split("=", 1) for ln in proc.stdout.splitlines() if "=" in ln)
            runtime = os.path.join(state, "maintenance", "runtime")
            self.assertEqual(lines["instance"], "maintenance")
            self.assertEqual(lines["runtime"], runtime)
            self.assertEqual(lines["pid"], "%s|%s" % (
                os.path.join(runtime, "bridge.pid"),
                os.path.join(runtime, "ngrok.pid")))
            self.assertNotIn(os.path.join(ROOT, ".runtime"), proc.stdout)

    def test_start_propagates_instance_sandbox_mode_env(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            run_admin(["create", "maintenance"], state)
            proc = source_start(
                state, "maintenance",
                'printf "sandbox_mode=%s\\n" "$BRIDGE_SANDBOX_MODE"; ',
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("sandbox_mode=bridge-workspace", proc.stdout)

    def test_whitelist_scripts_accept_maintenance(self):
        proc = run_bash('. "$1"; bridge_instance_valid maintenance', cwd=ROOT, args=(LIB,))
        self.assertEqual(proc.returncode, 0)
        for path in (os.path.join(ROOT, "scripts", "install_launch_agent.sh"),
                     os.path.join(ROOT, "scripts", "uninstall_launch_agent.sh"),
                     os.path.join(ROOT, "scripts", "status_launch_agent.sh"),
                     os.path.join(ROOT, "scripts", "start_ngrok_bridge.sh")):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("maintenance", text, path)


class MaintenanceCwdGuardTest(unittest.TestCase):

    def make_layout(self):
        layout = {}
        for key in ("home", "repo", "state", "codex"):
            parent = tempfile.mkdtemp(prefix="lcb-maint-%s-" % key)
            layout[key] = os.path.join(parent, key)
            layout[key + "_parent"] = parent
        for p in (layout["home"], layout["repo"], layout["state"], layout["codex"],
                  os.path.join(layout["repo"], "scripts"),
                  os.path.join(layout["home"], "Desktop", "other-project"),
                  os.path.join(layout["state"], "maintenance")):
            os.makedirs(p)
        return layout

    def guard_for(self, layout):
        return {"scope": "maintenance", "home": layout["home"],
                "repo_root": layout["repo"], "state_root": layout["state"],
                "codex_home": layout["codex"]}

    def assert_rejected(self, cwd, guard, category):
        from bridge.workspace_guard import validate_maintenance_cwd
        with self.assertRaises(TaskCwdError) as ctx:
            validate_maintenance_cwd(cwd, **{k: v for k, v in guard.items()
                                             if k != "scope"})
        self.assertEqual(ctx.exception.category, category)
        self.assertNotIn(cwd, ctx.exception.reason)

    def test_accepts_repo_root_and_subdir(self):
        from bridge.workspace_guard import validate_maintenance_cwd
        layout = self.make_layout()
        guard = {k: v for k, v in self.guard_for(layout).items() if k != "scope"}
        self.assertEqual(validate_maintenance_cwd(layout["repo"], **guard),
                         os.path.realpath(layout["repo"]))
        self.assertEqual(validate_maintenance_cwd(
            os.path.join(layout["repo"], "scripts"), **guard),
            os.path.realpath(os.path.join(layout["repo"], "scripts")))

    def test_rejects_outside_home_root_ancestor(self):
        layout = self.make_layout()
        guard = self.guard_for(layout)
        self.assert_rejected(
            os.path.join(layout["home"], "Desktop", "other-project"),
            guard, "outside")
        self.assert_rejected(layout["home"], guard, "home")
        self.assert_rejected(os.path.sep, guard, "home")
        self.assert_rejected(layout["repo_parent"], guard, "outside")

    def test_rejects_state_and_codex_home(self):
        layout = self.make_layout()
        guard = self.guard_for(layout)
        self.assert_rejected(layout["state"], guard, "instance_state")
        self.assert_rejected(os.path.join(layout["state"], "maintenance"),
                             guard, "instance_state")
        self.assert_rejected(layout["codex"], guard, "codex_home")

    def test_rejects_symlink_escape(self):
        layout = self.make_layout()
        link = os.path.join(layout["repo"], "scripts", "escape-link")
        os.symlink(os.path.join(layout["home"], "Desktop", "other-project"), link)
        guard = self.guard_for(layout)
        self.assert_rejected(link, guard, "outside")
        state_link = os.path.join(layout["repo"], "scripts", "state-link")
        os.symlink(layout["state"], state_link)
        self.assert_rejected(state_link, guard, "instance_state")

    def test_rejects_missing(self):
        from bridge.workspace_guard import validate_maintenance_cwd
        layout = self.make_layout()
        guard = {k: v for k, v in self.guard_for(layout).items() if k != "scope"}
        with self.assertRaises(TaskCwdError) as ctx:
            validate_maintenance_cwd("", **guard)
        self.assertEqual(ctx.exception.category, "missing")


class _FakeClient:
    def __init__(self):
        self.log = Logger(echo=False)
        self.calls = []
        self.alive = True

    def on(self, method, handler):
        pass

    def request(self, method, params=None, timeout=60.0):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "t1"}}
        if method == "thread/read":
            return {"thread": {"id": (params or {}).get("threadId")}}
        if method == "turn/start":
            return {"turn": {"id": "tu1"}}
        return {}


class BridgeCoreMaintenanceDispatchTest(unittest.TestCase):

    def test_maintenance_scope_accepts_repo_cwd(self):
        layout = self._layout()
        guard = {"scope": "maintenance", "home": layout["home"],
                 "repo_root": layout["repo"], "state_root": layout["state"],
                 "codex_home": layout["codex"]}
        client = _FakeClient()
        core = BridgeCore(client, cwd_guard=guard)
        tid, turn = core.start("hi", cwd=layout["repo"])
        self.assertEqual((tid, turn), ("t1", "tu1"))
        self.assertEqual([m for m, _ in client.calls],
                         ["thread/start", "turn/start"])
        self.assertEqual(client.calls[0][1]["cwd"], os.path.realpath(layout["repo"]))

    def test_maintenance_scope_rejects_outside_before_request(self):
        layout = self._layout()
        guard = {"scope": "maintenance", "home": layout["home"],
                 "repo_root": layout["repo"], "state_root": layout["state"],
                 "codex_home": layout["codex"]}
        client = _FakeClient()
        core = BridgeCore(client, cwd_guard=guard)
        with self.assertRaises(TaskCwdError):
            core.start("hi", cwd=os.path.join(layout["home"], "Desktop", "other-project"))
        self.assertEqual(client.calls, [], "thread/start must never be sent")

    def test_task_scope_still_rejects_repo_cwd(self):
        layout = self._layout()
        guard = {"scope": "task", "home": layout["home"],
                 "repo_root": layout["repo"], "state_root": layout["state"],
                 "codex_home": layout["codex"]}
        client = _FakeClient()
        core = BridgeCore(client, cwd_guard=guard)
        with self.assertRaises(TaskCwdError) as ctx:
            core.start("hi", cwd=layout["repo"])
        self.assertEqual(ctx.exception.category, "bridge_repo")
        self.assertEqual(client.calls, [])

    def _layout(self):
        layout = {}
        for key in ("home", "repo", "state", "codex"):
            parent = tempfile.mkdtemp(prefix="lcb-dispatch-%s-" % key)
            layout[key] = os.path.join(parent, key)
        for p in (layout["home"], layout["repo"], layout["state"], layout["codex"],
                  os.path.join(layout["home"], "Desktop", "other-project"),
                  os.path.join(layout["repo"], "scripts")):
            os.makedirs(p)
        return layout


class HttpMaintenanceScopeTest(unittest.TestCase):

    def test_start_maintenance_scope_accepts_repo_cwd(self):
        layout = self._layout()
        client = _FakeClient()
        core = BridgeCore(client, cwd_guard={
            "scope": "maintenance", "home": layout["home"],
            "repo_root": layout["repo"], "state_root": layout["state"],
            "codex_home": layout["codex"]})
        handler = self._handler(core)
        status, body = self._post(handler, json.dumps(
            {"prompt": "hi", "cwd": layout["repo"]}))
        self.assertEqual(status, 200)
        self.assertEqual(client.calls[0][1]["cwd"], os.path.realpath(layout["repo"]))

    def test_start_maintenance_scope_rejects_outside(self):
        layout = self._layout()
        client = _FakeClient()
        core = BridgeCore(client, cwd_guard={
            "scope": "maintenance", "home": layout["home"],
            "repo_root": layout["repo"], "state_root": layout["state"],
            "codex_home": layout["codex"]})
        handler = self._handler(core)
        status, body = self._post(handler, json.dumps(
            {"prompt": "hi",
             "cwd": os.path.join(layout["home"], "Desktop", "other-project")}))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "invalid_cwd")
        self.assertEqual(client.calls, [])

    def _handler(self, core):
        handler = object.__new__(BridgeHttpHandler)
        handler.server = _FakeServer(core)
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.path = "/start"
        handler.requestline = "POST /start HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.close_connection = True
        return handler

    def _post(self, handler, body, path="/start"):
        handler.path = path
        handler.headers = {
            "Authorization": "Bearer secret-key",
            "Content-Length": str(len(body)),
        }
        handler.rfile = io.BytesIO(body.encode("utf-8"))
        handler.wfile = io.BytesIO()
        handler.do_POST()
        raw = handler.wfile.getvalue()
        text = raw.decode("utf-8")
        status = int(text.split(" ", 2)[1])
        parsed = json.loads(text.split("\r\n\r\n", 1)[1])
        return status, parsed

    def _layout(self):
        layout = {}
        for key in ("home", "repo", "state", "codex"):
            parent = tempfile.mkdtemp(prefix="lcb-http-%s-" % key)
            layout[key] = os.path.join(parent, key)
        for p in (layout["home"], layout["repo"], layout["state"], layout["codex"],
                  os.path.join(layout["home"], "Desktop", "other-project"),
                  os.path.join(layout["repo"], "scripts")):
            os.makedirs(p)
        return layout


class _FakeServer:
    def __init__(self, core, instance="maintenance", mode="bridge-workspace", port=8323):
        self.core = core
        self.instance = instance
        self.mode = mode
        self.port = port
        self.api_key = "secret-key"
        self.log = Logger(echo=False)


class HealthMetadataTest(unittest.TestCase):

    def test_health_reports_instance_mode_port(self):
        client = _FakeClient()
        core = BridgeCore(client)
        handler = object.__new__(BridgeHttpHandler)
        handler.server = _FakeServer(core, instance="maintenance",
                                     mode="bridge-workspace", port=8323)
        handler.requestline = "GET /health HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.wfile = io.BytesIO()
        handler._handle_health()
        raw = handler.wfile.getvalue().decode("utf-8")
        body = json.loads(raw.split("\r\n\r\n", 1)[1])
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["instance"], "maintenance")
        self.assertEqual(body["mode"], "bridge-workspace")
        self.assertEqual(body["port"], 8323)

    def test_http_server_wires_instance_mode_port_onto_handler_server(self):
        server = BridgeHttpServer(
            "codex", "/tmp/lcb-nonexistent-home", "secret-key",
            host="127.0.0.1", port=0, instance="maintenance",
            mode="bridge-workspace",
        )
        try:
            self.assertEqual(server.httpd.instance, "maintenance")
            self.assertEqual(server.httpd.mode, "bridge-workspace")
            self.assertEqual(server.httpd.port, server.port)
            handler = object.__new__(BridgeHttpHandler)
            handler.server = server.httpd
            handler.requestline = "GET /health HTTP/1.1"
            handler.request_version = "HTTP/1.1"
            handler.wfile = io.BytesIO()
            handler._handle_health()
            raw = handler.wfile.getvalue().decode("utf-8")
            body = json.loads(raw.split("\r\n\r\n", 1)[1])
            self.assertEqual(body["instance"], "maintenance")
            self.assertEqual(body["mode"], "bridge-workspace")
            self.assertEqual(body["port"], server.port)
        finally:
            server.httpd.server_close()

    def test_openapi_health_schema_and_operations(self):
        if yaml is None:
            self.skipTest("pyyaml not available")
        spec = yaml.safe_load(open(os.path.join(ROOT, "openapi.yaml"), encoding="utf-8"))
        self.assertEqual(spec["info"]["version"], "1.1.0")
        paths = spec["paths"]
        self.assertEqual(len(paths), 8)
        ops = []
        for item in paths.values():
            for verb in ("get", "post"):
                if verb in item:
                    ops.append(item[verb])
        self.assertEqual(len(ops), 8)
        for op in ops:
            self.assertFalse(op.get("x-openai-isConsequential", True),
                             op.get("operationId"))
        health_props = paths["/health"]["get"]["responses"]["200"]["content"] \
            ["application/json"]["schema"]["properties"]
        for key in ("instance", "mode", "port"):
            self.assertIn(key, health_props)


class MaintenanceScriptsTest(unittest.TestCase):

    def test_scripts_exist_executable_and_bash_n(self):
        for path in (ACTIVATE, DEACTIVATE):
            self.assertTrue(os.path.isfile(path), path)
            self.assertTrue(os.access(path, os.X_OK), path)
            proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, path)

    def test_no_pkill_killall_and_no_hpc_touch(self):
        for path in (ACTIVATE, DEACTIVATE):
            # scan executable lines only (comments document the guarantee)
            text = open(path, encoding="utf-8").read()
            body = "\n".join(
                ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
            self.assertNotIn("pkill", body, path)
            self.assertNotIn("killall", body, path)
            self.assertNotIn("BRIDGE_INSTANCE=hpc", text, path)
            self.assertNotIn("rm -rf", body, path)
            self.assertNotIn("rm -r ", body, path)
            self.assertNotIn("kill ", body, path)
            self.assertNotIn("kill -9", body, path)

    def test_activate_security_invariants(self):
        text = open(ACTIVATE, encoding="utf-8").read()
        # explicit CODEX_HOME migration (compat parameter, not hardcoded)
        self.assertIn("--codex-home \"$MAINT_HOME\"", text)
        # config-only copy, 700/600 permissions
        self.assertIn('cp -p "$SOURCE_HOME/config.toml" "$MAINT_HOME/config.toml"', text)
        self.assertIn("chmod 700 \"$MAINT_HOME\"", text)
        self.assertIn("chmod 600 \"$MAINT_HOME/config.toml\"", text)
        self.assertIn("threads/history/cache never copied", text)
        # pause marker BEFORE stop local, and stop local BEFORE start
        # maintenance; the sentinel stays (supervisor stays alive, no
        # launchd crash-loop during the window)
        self.assertIn("PAUSE_MARKER", text)
        self.assertIn("pause.marker", text)
        self.assertIn("PRE_SUPERVISOR", text)
        pause_idx = text.index('touch "$PAUSE_MARKER"')
        stop_idx = text.index('BRIDGE_INSTANCE=local "$ROOT/scripts/stop_ngrok_bridge.sh"')
        start_idx = text.index('BRIDGE_INSTANCE="$INSTANCE" "$ROOT/scripts/start_ngrok_bridge.sh"')
        self.assertLess(pause_idx, stop_idx)
        self.assertLess(stop_idx, start_idx)
        self.assertNotIn("supervisor_disable", text)
        # maintenance contract pinned: port 8323 + dedicated CODEX_HOME
        self.assertIn('"port=8323"', text)
        self.assertIn('"codex_home=$MAINT_HOME"', text)
        # HOST-ADMIN path references only (never contents); repo refs are the
        # fallback when the stable config-root copies do not exist
        self.assertIn('KEY_REF="$ROOT/.bridge_api_key"', text)
        self.assertIn('DOMAIN_REF="$ROOT/.ngrok_domain"', text)
        self.assertIn('"api_key_file=$KEY_REF"', text)
        self.assertIn('"ngrok_domain_file=$DOMAIN_REF"', text)
        self.assertNotIn('cat "$ROOT/.bridge_api_key"', text)
        self.assertNotIn('cat "$ROOT/.ngrok_domain"', text)
        # the pre-window supervisor state is recorded in instance state and
        # restored on rollback
        self.assertIn("activate.marker", text)
        self.assertIn("supervisor_state", text)
        self.assertIn("port 8323", text)
        self.assertIn("instance=maintenance", text)
        self.assertIn("mode=bridge-workspace", text)

    def test_deactivate_security_invariants(self):
        text = open(DEACTIVATE, encoding="utf-8").read()
        self.assertIn('BRIDGE_INSTANCE="$INSTANCE" "$ROOT/scripts/stop_ngrok_bridge.sh"', text)
        self.assertIn('BRIDGE_INSTANCE="$LOCAL" "$ROOT/scripts/start_ngrok_bridge.sh"', text)
        # maintenance state / CODEX_HOME are preserved for the next window
        self.assertIn("left in place", text)
        self.assertIn("never touches hpc", text)

    def test_no_secret_literal_in_maintenance_artifacts(self):
        secrets = []
        for name in (".bridge_api_key", ".ngrok_domain"):
            path = os.path.join(ROOT, name)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    secrets.append((name, fh.read()))
        for name, content in secrets:
            stripped = content.decode("utf-8", errors="replace").strip()
            if not stripped or len(stripped) < 4:
                continue
            for path in (ACTIVATE, DEACTIVATE):
                with open(path, "rb") as fh:
                    self.assertNotIn(content, fh.read(),
                                     "%s literal leaked into %s" % (name, path))
        # a freshly created maintenance instance config holds no secret content
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            proc = run_admin(["create", "maintenance"], state)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cfg = os.path.join(state, "maintenance", "instance.conf")
            with open(cfg, "rb") as fh:
                cfg_bytes = fh.read()
            for _, content in secrets:
                if len(content) >= 4:
                    self.assertNotIn(content, cfg_bytes)
            self.assertNotIn(b"sk-", cfg_bytes)


class GuardConstructionMaintenanceTest(unittest.TestCase):

    def test_build_cwd_guard_maintenance_scope_and_paths(self):
        guard = build_cwd_guard({
            "HOME": "/home/u",
            "BRIDGE_INSTANCE": "maintenance",
            "BRIDGE_STATE_ROOT": "/state/root",
        })
        self.assertEqual(guard["scope"], "maintenance")
        self.assertEqual(guard["state_root"], "/state/root/maintenance")
        self.assertEqual(guard["codex_home"], "/home/u/.codex-deepseek-maintenance")
        self.assertTrue(guard["repo_root"])

    def test_build_cwd_guard_maintenance_respects_explicit_codex_home(self):
        guard = build_cwd_guard({
            "HOME": "/home/u",
            "BRIDGE_INSTANCE": "maintenance",
            "CODEX_HOME": "/custom/maintenance-home",
        })
        self.assertEqual(guard["scope"], "maintenance")
        self.assertEqual(guard["codex_home"], "/custom/maintenance-home")

    def test_build_cwd_guard_local_is_task_scope(self):
        guard = build_cwd_guard({
            "HOME": "/home/u",
            "BRIDGE_INSTANCE": "local",
            "BRIDGE_STATE_ROOT": "/state/root",
            "CODEX_HOME": "/home/u/.codex-deepseek",
        })
        self.assertEqual(guard["scope"], "task")
        self.assertEqual(guard["state_root"], "/state/root/local")
        self.assertEqual(guard["codex_home"], "/home/u/.codex-deepseek")


class ActivateRollbackTest(unittest.TestCase):
    """Fail-safe rollback regression tests for activate_maintenance_instance.sh.

    Static assertions pin the state machine ordering (arm only after the local
    stop, disarm only after the full local+identity+public health pass) and the
    rollback content guarantees (stop maintenance -> restart local, never hpc,
    no pkill/killall/rm -rf, no secret/domain content printed). Dynamic tests
    run the real activate script against a temp repo copy with fake
    stop/start/curl helpers so the offline suite observes real rollback
    behavior without spawning Bridge/ngrok or touching real secrets.
    """

    @staticmethod
    def _rollback_body(text):
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("rollback() {"))
        end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "}")
        return "\n".join(lines[start:end + 1])

    def test_rollback_armed_at_supervisor_handover(self):
        text = open(ACTIVATE, encoding="utf-8").read()
        marker_idx = text.index("activate.marker")
        arm_idx = text.index("ROLLBACK_ARMED=1")
        pause_idx = text.index('touch "$PAUSE_MARKER"')
        stop_idx = text.index('BRIDGE_INSTANCE=local "$ROOT/scripts/stop_ngrok_bridge.sh"')
        start_idx = text.index('BRIDGE_INSTANCE="$INSTANCE" "$ROOT/scripts/start_ngrok_bridge.sh"')
        # armed at the supervisor handover (pre-window state marker written,
        # then arm, then pause marker, then stop local), so any failure from
        # the moment the pre-window state is recorded rolls back
        self.assertLess(marker_idx, arm_idx)
        self.assertLess(arm_idx, pause_idx)
        self.assertLess(pause_idx, stop_idx)
        self.assertLess(arm_idx, start_idx)

    def test_rollback_disarmed_after_full_success(self):
        text = open(ACTIVATE, encoding="utf-8").read()
        public_idx = text.index('if ! bridge_health_ok "$PUBLIC_URL" "$ACTIVATE_LOG"; then')
        disarm_idx = text.index("ROLLBACK_ARMED=0", public_idx)
        trap_idx = text.index("trap - ERR", public_idx)
        # disarm happens only after local health + identity + public health pass
        self.assertLess(public_idx, disarm_idx)
        self.assertLess(public_idx, trap_idx)
        self.assertLess(disarm_idx, text.index("maintenance window ACTIVE"))

    def test_rollback_attempts_stop_maintenance_then_restart_local(self):
        body = self._rollback_body(open(ACTIVATE, encoding="utf-8").read())
        stop_maint = 'BRIDGE_INSTANCE="$INSTANCE" "$ROOT/scripts/stop_ngrok_bridge.sh"'
        start_local = 'BRIDGE_INSTANCE=local "$ROOT/scripts/start_ngrok_bridge.sh"'
        self.assertIn(stop_maint, body)
        self.assertIn(start_local, body)
        self.assertLess(body.index(stop_maint), body.index(start_local))
        # the pause marker is cleared before local is restored
        self.assertIn('rm -f "$PAUSE_MARKER"', body)
        self.assertLess(body.index('rm -f "$PAUSE_MARKER"'), body.index(start_local))

    def test_rollback_never_touches_hpc(self):
        body = self._rollback_body(open(ACTIVATE, encoding="utf-8").read())
        self.assertNotIn("BRIDGE_INSTANCE=hpc", body)
        self.assertNotIn('"hpc"', body)
        self.assertIn("never touched", body)

    def test_rollback_no_pkill_killall_rm_rf(self):
        text = open(ACTIVATE, encoding="utf-8").read()
        body = "\n".join(
            ln for ln in self._rollback_body(text).splitlines()
            if not ln.lstrip().startswith("#"))
        for needle in ("pkill", "killall", "rm -rf", "rm -r ", "kill -9", "kill "):
            self.assertNotIn(needle, body, needle)

    def test_rollback_and_activate_never_print_secret_or_domain(self):
        text = open(ACTIVATE, encoding="utf-8").read()
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn('cat "$ROOT/.bridge_api_key"', body)
        self.assertNotIn('cat "$ROOT/.ngrok_domain"', body)
        # no printing statement may interpolate the domain value or the URL
        # built from it (assignment/use for curl is fine; printing is not)
        # only flag a line when the printing primitive appears BEFORE the
        # interpolated variable (a `[[ -n "$DOMAIN" ]] || die ...` guard does
        # not print the value)
        printing = re.compile(r"\b(?:echo|printf|log|rlog|die)\b.*\$(?:DOMAIN|domain)")
        for ln in body.splitlines():
            if printing.search(ln):
                self.fail("printing line interpolates domain content: %r" % ln)

    # -- dynamic state-machine tests (temp repo + fake stop/start/curl) ------

    def _make_fake_repo(self, d):
        repo = os.path.join(d, "repo")
        os.makedirs(repo)
        for name in ("scripts", "config"):
            shutil.copytree(os.path.join(ROOT, name), os.path.join(repo, name))
        # dummy secrets, never real contents
        with open(os.path.join(repo, ".bridge_api_key"), "w") as fh:
            fh.write("dummy-api-key-not-a-real-secret\n")
        with open(os.path.join(repo, ".ngrok_domain"), "w") as fh:
            fh.write("maintenance-test.invalid\n")
        home = os.path.join(d, "home")
        os.makedirs(os.path.join(home, ".codex-deepseek"))
        with open(os.path.join(home, ".codex-deepseek", "config.toml"), "w") as fh:
            fh.write('model = "test-model"\n')
        state = os.path.join(d, "state")
        return repo, home, state

    def _install_fakes(self, repo, call_log):
        stop = os.path.join(repo, "scripts", "stop_ngrok_bridge.sh")
        start = os.path.join(repo, "scripts", "start_ngrok_bridge.sh")
        with open(stop, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "${BRIDGE_INSTANCE:-<unset>} stop" >> "${FAKE_CALL_LOG:?}"\n'
                     'exit 0\n')
        with open(start, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "${BRIDGE_INSTANCE:-<unset>} start" >> "${FAKE_CALL_LOG:?}"\n'
                     'if [[ -n "${FAKE_FAIL_INSTANCE:-}" '
                     '&& "$FAKE_FAIL_INSTANCE" == "$BRIDGE_INSTANCE" ]]; then\n'
                     '  exit 1\n'
                     'fi\n'
                     'if [[ -n "${FAKE_BREAK_MAINT_CONFIG:-}" '
                     '&& "$BRIDGE_INSTANCE" == "maintenance" ]]; then\n'
                     '  mv "$BRIDGE_STATE_ROOT/maintenance/instance.conf" \\\n'
                     '     "$BRIDGE_STATE_ROOT/maintenance/instance.conf.gone" 2>/dev/null || true\n'
                     'fi\n'
                     'exit 0\n')
        os.chmod(stop, 0o755)
        os.chmod(start, 0o755)
        bindir = os.path.join(repo, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "curl"), "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'port="8323"\n'
                     'for arg in "$@"; do\n'
                     '  case "$arg" in\n'
                     '    http://127.0.0.1:*) port="${arg#http://127.0.0.1:}"; port="${port%%/*}" ;;\n'
                     '  esac\n'
                     'done\n'
                     'printf \'{"status":"ok","instance":"maintenance",'
                     '"mode":"bridge-workspace","port":%s}\\n\' "$port"\n'
                     'exit 0\n')
        os.chmod(os.path.join(bindir, "curl"), 0o755)

    def _run_activate(self, repo, home, state, call_log, fail_instance="",
                      break_maint_config=False):
        env = dict(os.environ)
        for key in ("BRIDGE_INSTANCE", "BRIDGE_STATE_ROOT", "XDG_STATE_HOME",
                    "BRIDGE_SANDBOX_MODE", "BRIDGE_SANDBOX_MODE_FILE", "NGROK_DOMAIN",
                    "SUPERVISOR_AGENT_LABEL"):
            env.pop(key, None)
        # simulate an enabled local supervisor (runtime installed): the
        # handover must create the pause marker (sentinel stays) and the
        # rollback must clear the marker and restore the pre-window state
        sentinel = os.path.join(state, "local", "runtime", "supervisor.enabled")
        os.makedirs(os.path.dirname(sentinel), exist_ok=True)
        with open(sentinel, "w") as fh:
            fh.write("")
        env.update({
            "BRIDGE_STATE_ROOT": state,
            "HOME": home,
            "FAKE_CALL_LOG": call_log,
            "FAKE_FAIL_INSTANCE": fail_instance,
            "FAKE_BREAK_MAINT_CONFIG": "1" if break_maint_config else "",
            # never touch a real launchd agent from tests
            "SUPERVISOR_AGENT_LABEL": "com.local.codex-bridge.testonly",
            "PATH": os.path.join(repo, "bin") + os.pathsep + env.get("PATH", ""),
        })
        return subprocess.run(
            ["bash", os.path.join(repo, "scripts", "activate_maintenance_instance.sh")],
            capture_output=True, text=True, env=env, cwd=repo,
        )

    def _calls(self, call_log):
        if not os.path.exists(call_log):
            return []
        return [ln for ln in open(call_log, encoding="utf-8").read().splitlines() if ln]

    def test_dynamic_success_disarms_and_does_not_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            repo, home, state = self._make_fake_repo(d)
            run_admin(["create", "local"], state)
            run_admin(["create", "maintenance"], state)
            call_log = os.path.join(d, "calls.log")
            self._install_fakes(repo, call_log)
            proc = self._run_activate(repo, home, state, call_log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            calls = self._calls(call_log)
            self.assertEqual(calls, ["local stop", "maintenance start"])
            self.assertNotIn("local start", calls)
            self.assertIn("rollback disarmed", proc.stdout)
            self.assertIn("maintenance window ACTIVE", proc.stdout)
            self.assertNotIn("rollback: activation failed", proc.stderr)
            # local supervisor PAUSED for the window: sentinel stays (no
            # launchd crash-loop) and the pause marker holds local stopped;
            # the activation marker records the pre-window state for
            # deactivate
            self.assertTrue(
                os.path.exists(os.path.join(state, "local", "runtime",
                                            "supervisor.enabled")))
            pause = os.path.join(state, "local", "pause.marker")
            self.assertTrue(os.path.isfile(pause), pause)
            marker = os.path.join(state, "maintenance", "activate.marker")
            self.assertTrue(os.path.exists(marker), marker)
            self.assertIn("supervisor_state=enabled",
                          open(marker, encoding="utf-8").read())

    def test_dynamic_post_stop_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            repo, home, state = self._make_fake_repo(d)
            run_admin(["create", "local"], state)
            run_admin(["create", "maintenance"], state)
            call_log = os.path.join(d, "calls.log")
            self._install_fakes(repo, call_log)
            proc = self._run_activate(repo, home, state, call_log,
                                      fail_instance="maintenance")
            self.assertNotEqual(proc.returncode, 0)
            calls = self._calls(call_log)
            # stop local -> start maintenance (fails) -> rollback: stop
            # maintenance -> restart local
            self.assertEqual(
                calls, ["local stop", "maintenance start",
                        "maintenance stop", "local start"])
            combined = proc.stdout + proc.stderr
            self.assertIn("maintenance start failed", combined)
            self.assertIn("rollback: activation failed", proc.stderr)
            self.assertIn("rollback: local state restore attempted", proc.stderr)
            # rollback cleared the pause marker and restored the enabled
            # sentinel (pre-window state)
            self.assertFalse(os.path.exists(
                os.path.join(state, "local", "pause.marker")))
            self.assertTrue(
                os.path.exists(os.path.join(state, "local", "runtime",
                                            "supervisor.enabled")))
            self.assertFalse(os.path.exists(
                os.path.join(state, "maintenance", "activate.marker")))
            # rollback logs never print API key / domain content
            self.assertNotIn("dummy-api-key-not-a-real-secret", combined)
            self.assertNotIn("maintenance-test.invalid", combined)

    def test_dynamic_pre_stop_failure_does_not_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            repo, home, state = self._make_fake_repo(d)
            run_admin(["create", "local"], state)
            run_admin(["create", "maintenance"], state)
            # force the local verify prerequisite to fail BEFORE any stop
            run_admin(["update", "maintenance", "port=8321"], state)
            call_log = os.path.join(d, "calls.log")
            self._install_fakes(repo, call_log)
            proc = self._run_activate(repo, home, state, call_log)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(self._calls(call_log), [])
            self.assertNotIn("rollback", proc.stderr)
            self.assertIn("local instance config failed verification", proc.stderr)

    def test_dynamic_err_trap_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            repo, home, state = self._make_fake_repo(d)
            run_admin(["create", "local"], state)
            run_admin(["create", "maintenance"], state)
            call_log = os.path.join(d, "calls.log")
            self._install_fakes(repo, call_log)
            # the fake maintenance start removes its instance config, so the
            # unguarded `MAINT_PORT="$(bridge_instance_get ...)"` after the
            # local stop raises ERR -> the trap must run the rollback
            proc = self._run_activate(repo, home, state, call_log,
                                      break_maint_config=True)
            self.assertNotEqual(proc.returncode, 0)
            calls = self._calls(call_log)
            self.assertEqual(calls, ["local stop", "maintenance start", "local start"])
            combined = proc.stdout + proc.stderr
            self.assertIn("command failed", combined)
            self.assertIn("rollback: activation failed", proc.stderr)
            self.assertNotIn("dummy-api-key-not-a-real-secret", combined)
            self.assertNotIn("maintenance-test.invalid", combined)


if __name__ == "__main__":
    unittest.main()
