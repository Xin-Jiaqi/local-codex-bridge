#!/usr/bin/env python3
"""Offline tests for the task-cwd / control-plane boundary (PART 2).

Covers the reusable validator (bridge/workspace_guard.py): HOME and its
ancestors, the bridge repo root (equal/ancestor/descendant), the instance
state root (equal/ancestor/descendant), CODEX_HOME (equal/ancestor/
descendant), symlink bypasses, and that a normal sibling project under HOME
remains allowed; the structured TaskCwdError (generic reason, category, no
private paths); wiring into the real start path (BridgeCore.start before any
thread/start request) and the HTTP /start + /continue handlers; the server's
guard-path construction; and the hardened .git/hooks read-only rule in the
bridge-workspace permission overrides. No live app-server is spawned.
"""

import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge import BridgeCore, Logger
from bridge.workspace_guard import TaskCwdError, validate_task_cwd
from http_server.server import (
    BRIDGE_WORKSPACE_FILESYSTEM_OVERRIDE,
    BridgeHttpHandler,
    build_cwd_guard,
)

TEMPLATE = os.path.join(ROOT, "config", "bridge-workspace.example.toml")


def make_layout():
    """Each control path gets its OWN parent so ancestor tests are
    unambiguous (a shared parent would already be rejected as 'home')."""
    layout = {}
    for key in ("home", "repo", "state", "codex"):
        parent = tempfile.mkdtemp(prefix="lcb-%s-" % key)
        layout[key] = os.path.join(parent, key)
        layout[key + "_parent"] = parent
    for p in (layout["home"], layout["repo"], layout["state"], layout["codex"],
              os.path.join(layout["home"], "Desktop", "some-project"),
              os.path.join(layout["repo"], "scripts"),
              os.path.join(layout["state"], "sub"),
              os.path.join(layout["codex"], "config")):
        os.makedirs(p)
    return layout


def guard_for(layout):
    return {"home": layout["home"], "repo_root": layout["repo"],
            "state_root": layout["state"], "codex_home": layout["codex"]}


class ValidatorTest(unittest.TestCase):

    def assert_rejected(self, cwd, guard, category):
        with self.assertRaises(TaskCwdError) as ctx:
            validate_task_cwd(cwd, **guard)
        self.assertEqual(ctx.exception.category, category)
        # generic reason: must not leak private paths
        self.assertNotIn(cwd, ctx.exception.reason)
        for value in guard.values():
            if value:
                self.assertNotIn(value, ctx.exception.reason)

    def test_rejects_home_and_ancestors(self):
        layout = make_layout()
        guard = guard_for(layout)
        self.assert_rejected(layout["home"], guard, "home")
        self.assert_rejected(layout["home_parent"], guard, "home")
        self.assert_rejected(os.path.sep, guard, "home")
        project = os.path.join(layout["home"], "Desktop", "some-project")
        self.assertEqual(validate_task_cwd(project, **guard),
                         os.path.realpath(project))

    def test_rejects_bridge_repo_ancestor_inside(self):
        layout = make_layout()
        guard = guard_for(layout)
        self.assert_rejected(layout["repo"], guard, "bridge_repo")
        self.assert_rejected(layout["repo_parent"], guard, "bridge_repo")
        self.assert_rejected(os.path.join(layout["repo"], "scripts"),
                             guard, "bridge_repo")
        self.assert_rejected(
            os.path.join(layout["repo"], "scripts", "..", "scripts"),
            guard, "bridge_repo")

    def test_rejects_state_root_ancestor_inside(self):
        layout = make_layout()
        guard = guard_for(layout)
        self.assert_rejected(layout["state"], guard, "instance_state")
        self.assert_rejected(layout["state_parent"], guard, "instance_state")
        self.assert_rejected(os.path.join(layout["state"], "sub"),
                             guard, "instance_state")

    def test_rejects_codex_home_ancestor_inside(self):
        layout = make_layout()
        guard = guard_for(layout)
        self.assert_rejected(layout["codex"], guard, "codex_home")
        self.assert_rejected(layout["codex_parent"], guard, "codex_home")
        self.assert_rejected(os.path.join(layout["codex"], "config"),
                             guard, "codex_home")

    def test_rejects_symlink_bypass(self):
        layout = make_layout()
        link = os.path.join(layout["home"], "Desktop", "innocent-link")
        os.symlink(layout["repo"], link)
        guard = guard_for(layout)
        self.assert_rejected(link, guard, "bridge_repo")
        link2 = os.path.join(layout["home"], "Desktop", "state-link")
        os.symlink(layout["state"], link2)
        self.assert_rejected(link2, guard, "instance_state")

    def test_missing_cwd_is_structured_error(self):
        layout = make_layout()
        guard = guard_for(layout)
        with self.assertRaises(TaskCwdError) as ctx:
            validate_task_cwd(None, **guard)
        self.assertEqual(ctx.exception.category, "missing")
        with self.assertRaises(TaskCwdError):
            validate_task_cwd("  ", **guard)

    def test_accepts_unrelated_project_and_canonicalizes(self):
        layout = make_layout()
        real_dir = os.path.join(layout["home_parent"], "real-projects", "x")
        os.makedirs(real_dir)
        link_dir = os.path.join(layout["home"], "Desktop", "link-project")
        os.symlink(real_dir, link_dir)
        guard = guard_for(layout)
        got = validate_task_cwd(link_dir, **guard)
        self.assertEqual(got, os.path.realpath(real_dir))


class _FakeClient:
    def __init__(self):
        self.log = Logger(echo=False)
        self.calls = []

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


def guarded_core(layout):
    client = _FakeClient()
    core = BridgeCore(client, cwd_guard=guard_for(layout))
    return core, client


class BridgeCoreStartGuardTest(unittest.TestCase):

    def test_bad_cwd_blocks_thread_start_before_any_request(self):
        layout = make_layout()
        core, client = guarded_core(layout)
        with self.assertRaises(TaskCwdError):
            core.start("hi", cwd=layout["repo"])
        self.assertEqual(client.calls, [], "thread/start must never be sent")

    def test_missing_cwd_blocks_thread_start(self):
        layout = make_layout()
        core, client = guarded_core(layout)
        with self.assertRaises(TaskCwdError) as ctx:
            core.start("hi")
        self.assertEqual(ctx.exception.category, "missing")
        self.assertEqual(client.calls, [])

    def test_good_cwd_reaches_thread_start_canonicalized(self):
        layout = make_layout()
        core, client = guarded_core(layout)
        project = os.path.join(layout["home"], "Desktop", "some-project")
        thread_id, turn_id = core.start("hi", cwd=project)
        self.assertEqual((thread_id, turn_id), ("t1", "tu1"))
        self.assertEqual([m for m, _ in client.calls],
                         ["thread/start", "turn/start"])
        self.assertEqual(client.calls[0][1]["cwd"], os.path.realpath(project))


class _FakeServer:
    def __init__(self, core, api_key="secret-key"):
        self.core = core
        self.api_key = api_key
        self.log = Logger(echo=False)


class HttpHandlerGuardTest(unittest.TestCase):

    def make_handler(self, core):
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

    def post(self, handler, body, path="/start"):
        handler.path = path
        handler.headers = {
            "Authorization": "Bearer secret-key",
            "Content-Length": str(len(body)),
        }
        handler.rfile = io.BytesIO(body.encode("utf-8"))
        handler.wfile = io.BytesIO()
        handler.do_POST()
        return handler.wfile.getvalue()

    def parse(self, raw):
        text = raw.decode("utf-8")
        status = int(text.split(" ", 2)[1])
        body = json.loads(text.split("\r\n\r\n", 1)[1])
        return status, body

    def test_start_returns_invalid_cwd_error(self):
        layout = make_layout()
        core, client = guarded_core(layout)
        handler = self.make_handler(core)
        status, body = self.parse(self.post(handler, json.dumps(
            {"prompt": "hi", "cwd": layout["repo"]})))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "invalid_cwd")
        self.assertNotIn(layout["repo"], body["error"]["message"])
        self.assertEqual(client.calls, [])

    def test_start_accepts_sibling_project(self):
        layout = make_layout()
        core, client = guarded_core(layout)
        handler = self.make_handler(core)
        project = os.path.join(layout["home"], "Desktop", "some-project")
        status, body = self.parse(self.post(handler, json.dumps(
            {"prompt": "hi", "cwd": project})))
        self.assertEqual(status, 200)
        self.assertEqual(body["thread_id"], "t1")
        self.assertEqual(client.calls[0][1]["cwd"], os.path.realpath(project))

    def test_continue_rejects_cwd_field(self):
        client = _FakeClient()
        core = BridgeCore(client)
        handler = self.make_handler(core)
        status, body = self.parse(self.post(handler, json.dumps(
            {"thread_id": "t1", "prompt": "more", "cwd": "/tmp/anything"}),
            path="/continue"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "invalid_cwd")
        self.assertEqual(client.calls, [], "continue must never widen cwd")

    def test_continue_without_cwd_still_works(self):
        client = _FakeClient()
        core = BridgeCore(client)
        handler = self.make_handler(core)
        status, body = self.parse(self.post(handler, json.dumps(
            {"thread_id": "t1", "prompt": "more"}), path="/continue"))
        self.assertEqual(status, 200)
        self.assertEqual([m for m, _ in client.calls],
                         ["thread/read", "turn/start"])


class GuardConstructionTest(unittest.TestCase):

    def test_build_cwd_guard_includes_instance_state_root(self):
        guard = build_cwd_guard({
            "HOME": "/home/u",
            "BRIDGE_INSTANCE": "local",
            "BRIDGE_STATE_ROOT": "/state/root",
            "CODEX_HOME": "/home/u/.codex-deepseek",
        })
        self.assertEqual(guard["home"], "/home/u")
        self.assertEqual(guard["state_root"], "/state/root/local")
        self.assertEqual(guard["codex_home"], "/home/u/.codex-deepseek")
        self.assertTrue(guard["repo_root"])

    def test_build_cwd_guard_legacy_has_no_state_root(self):
        guard = build_cwd_guard({"HOME": "/home/u"})
        self.assertIsNone(guard["state_root"])
        self.assertEqual(guard["codex_home"], "/home/u/.codex-deepseek")

    def test_build_cwd_guard_ignores_invalid_instance(self):
        guard = build_cwd_guard({"HOME": "/home/u", "BRIDGE_INSTANCE": "bogus"})
        self.assertIsNone(guard["state_root"])


class HooksHardeningTest(unittest.TestCase):

    def test_hooks_rule_is_read_only_and_more_specific(self):
        text = BRIDGE_WORKSPACE_FILESYSTEM_OVERRIDE
        self.assertIn('".git/" = "write"', text)
        self.assertIn('".git/hooks/" = "read"', text)
        self.assertNotIn('".git/hooks/" = "write"', text)
        self.assertNotIn('".git/hooks/" = "deny"', text)

    def test_template_mirrors_hooks_read_rule(self):
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('".git/" = "write"', text)
        self.assertIn('".git/hooks/" = "read"', text)


if __name__ == "__main__":
    unittest.main()
