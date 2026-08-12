#!/usr/bin/env python3
"""Offline unit tests for V1 app-server startup configuration.

Never spawns a real Codex app-server. Verifies:

1. http_server CONFIG_OVERRIDES enforce `approval_policy="never"` and
   `sandbox_mode="workspace-write"` (the V1 security boundary: workspace-local
   read/write/shell run without prompts; anything outside the workspace is
   auto-denied).
2. CodexAppServerClient.start() puts every -c override on the spawned
   `codex app-server` command line and sets CODEX_HOME in the child env.
"""

import io
import json
import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bridge.client as client_module
from bridge import CodexAppServerClient, Logger
from http_server.server import CONFIG_OVERRIDES


class _FakeStdin(io.StringIO):
    def __init__(self, request_written):
        super().__init__()
        self._request_written = request_written

    def write(self, s):
        result = super().write(s)
        self._request_written.set()
        return result


class _FakeProc:
    """subprocess.Popen stand-in: captures args/env, feeds one initialize
    response only after the request was written, then EOF (process exit)."""

    def __init__(self, initialize_result):
        self._request_written = threading.Event()
        self._initialize_result = initialize_result
        self._exit_code = 0
        self.stdin = _FakeStdin(self._request_written)
        self.stderr = io.StringIO()

    @property
    def stdout(self):
        def lines():
            if not self._request_written.wait(timeout=10):
                return
            yield json.dumps({
                "jsonrpc": "2.0",
                "id": "1",
                "result": self._initialize_result,
            })
        return lines()

    def poll(self):
        return self._exit_code

    def terminate(self):
        self._exit_code = 0

    def kill(self):
        self._exit_code = 0

    def wait(self, timeout=None):
        self._exit_code = 0
        return 0


class StartupConfigTest(unittest.TestCase):

    def test_server_overrides_enforce_v1_policy(self):
        self.assertIn('approval_policy="never"', CONFIG_OVERRIDES)
        self.assertIn('sandbox_mode="workspace-write"', CONFIG_OVERRIDES)
        self.assertEqual(CONFIG_OVERRIDES.count('approval_policy="never"'), 1)
        self.assertEqual(CONFIG_OVERRIDES.count('sandbox_mode="workspace-write"'), 1)

    def test_client_passes_overrides_to_app_server_spawn(self):
        calls = {}

        def fake_popen(args, **kwargs):
            calls["args"] = list(args)
            calls["env"] = dict(kwargs.get("env") or os.environ)
            return _FakeProc({
                "userAgent": "local-codex-bridge-core/1.0.0 (offline test)",
                "codexHome": "/offline/codex-home",
            })

        original = client_module.subprocess.Popen
        client_module.subprocess.Popen = fake_popen
        client = None
        try:
            client = CodexAppServerClient(
                "/offline/codex",
                "/offline/codex-home",
                CONFIG_OVERRIDES,
                logger=Logger(echo=False),
            )
            client.start(timeout=10)
        finally:
            client_module.subprocess.Popen = original
            if client is not None:
                client.close()

        args = calls["args"]
        self.assertEqual(args[:2], ["/offline/codex", "app-server"])
        self.assertIn("--listen", args)
        self.assertIn("stdio://", args)
        self.assertIn("-c", args)
        for override in CONFIG_OVERRIDES:
            self.assertIn(override, args)
        self.assertEqual(calls["env"].get("CODEX_HOME"), "/offline/codex-home")


if __name__ == "__main__":
    unittest.main()
