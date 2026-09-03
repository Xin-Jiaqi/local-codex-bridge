#!/usr/bin/env python3
"""Offline tests for the one-time worker pairing flow (dual-host router).

No live bridge / app-server / network beyond 127.0.0.1:

- PairingCodeStore (bridge/dual_host.py): only a SHA-256 hash + expiry is
  persisted (never the code, never the worker token), codes expire, and a
  code can be consumed exactly once even under concurrent claims.
- HTTP endpoints on the real handlers (http_server/server.py):
  POST /internal/pairing/create (worker-token auth) and
  POST /internal/pairing/claim (the code itself is the bearer credential).
  Claims return the existing worker token once; unknown / expired / used
  codes all look identical (404); the server log never contains the code or
  the token.
- scripts/prepare_worker_pairing.py (the Mac-side mint helper): prints the
  code and the single Windows PowerShell command, and NEVER prints the
  worker token (checked through a real subprocess against a loopback HTTP
  server that records the Authorization header and request body).
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge.dual_host import (
    MAX_PAIRING_CODES,
    MAX_PAIRING_TTL_S,
    PairingCodeStore,
)
from http_server.server import _BridgeHTTPServer, BridgeHttpHandler, DualHostRouter

WORKER_TOKEN = "pairing-test-worker-token-do-not-print"


class _CaptureLog:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(message)


class PairingCodeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pairing-store-")

    def tearDown(self):
        for name in os.listdir(self.tmp):
            try:
                os.unlink(os.path.join(self.tmp, name))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def test_register_persists_only_hash_and_expiry(self):
        code = "pair-" + "a" * 24
        store = PairingCodeStore(self.tmp)
        self.assertTrue(store.register(code, ttl_s=600))
        names = os.listdir(self.tmp)
        self.assertEqual(len(names), 1)
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        self.assertEqual(names[0], digest + ".json")
        with open(os.path.join(self.tmp, names[0]), encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn(code, raw)          # never the plaintext code
        self.assertNotIn(WORKER_TOKEN, raw)  # never the worker token
        data = json.loads(raw)
        self.assertIsInstance(data["expires_at"], (int, float))
        self.assertGreater(data["expires_at"], time.time())

    def test_rejects_short_codes_and_no_plaintext_file(self):
        store = PairingCodeStore(self.tmp)
        self.assertFalse(store.register("short"))
        self.assertFalse(store.register(""))
        self.assertFalse(store.consume("short"))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_single_use_and_unknown_codes(self):
        code = "pair-" + "b" * 24
        store = PairingCodeStore(self.tmp)
        self.assertTrue(store.register(code, ttl_s=600))
        self.assertTrue(store.consume(code))
        self.assertFalse(store.consume(code))          # second claim fails
        self.assertFalse(store.consume("pair-" + "c" * 24))  # unknown code
        self.assertEqual(os.listdir(self.tmp), [])     # winner cleaned up

    def test_expired_code_cannot_be_claimed(self):
        code = "pair-" + "d" * 24
        ticks = [1000.0]

        def clock():
            return ticks[0]

        store = PairingCodeStore(self.tmp, clock=clock)
        self.assertTrue(store.register(code, ttl_s=60))
        ticks[0] = 1000.0 + 61
        self.assertFalse(store.consume(code))
        self.assertEqual(os.listdir(self.tmp), [])     # expired file removed

    def test_concurrent_claims_win_exactly_once(self):
        code = "pair-" + "e" * 24
        store = PairingCodeStore(self.tmp)
        self.assertTrue(store.register(code, ttl_s=600))
        winners = []
        barrier = threading.Barrier(8)

        def claim():
            barrier.wait()
            if store.consume(code):
                winners.append(True)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)
        self.assertFalse(store.consume(code))

    def test_cap_bounds_live_codes(self):
        store = PairingCodeStore(self.tmp)
        for i in range(MAX_PAIRING_CODES + 5):
            self.assertTrue(store.register("pair-%032x" % i, ttl_s=600))
        live = [n for n in os.listdir(self.tmp) if n.endswith(".json")]
        self.assertLessEqual(len(live), MAX_PAIRING_CODES)


class PairingHttpEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pairing-http-")
        cls.log = _CaptureLog()
        cls.map_path = os.path.join(cls.tmp, "thread_map.json")
        cls.router = DualHostRouter(
            cls.log, map_path=cls.map_path, enabled=True,
            worker_token_present=True, poll_timeout_s=1.0,
        )
        cls.httpd = _BridgeHTTPServer(("127.0.0.1", 0), BridgeHttpHandler)
        cls.httpd.core = None
        cls.httpd.api_key = "pairing-gpt-key"
        cls.httpd.worker_token = WORKER_TOKEN
        cls.httpd.router = cls.router
        cls.httpd.log = cls.log
        cls.httpd.instance = "local"
        cls.httpd.mode = "workspace-write"
        cls.httpd.port = cls.httpd.server_address[1]
        cls.httpd._config_overrides = []
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True, name="pairing-httpd")
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _http(self, method, path, body=None, key=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw.decode("utf-8"))
                                     if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw.decode("utf-8")) if raw else {})
            except ValueError:
                return e.code, {}

    def test_create_requires_worker_token(self):
        st, body = self._http("POST", "/internal/pairing/create",
                              {"code": "pair-" + "f" * 24, "ttl_s": 600})
        self.assertEqual(st, 401)

    def test_claim_returns_token_once_then_404(self):
        code = "pair-" + "g" * 24
        st, body = self._http("POST", "/internal/pairing/create",
                              {"code": code, "ttl_s": 600}, key=WORKER_TOKEN)
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        st, body = self._http("POST", "/internal/pairing/claim", {"code": code})
        self.assertEqual(st, 200)
        self.assertEqual(body["token"], WORKER_TOKEN)
        st, body = self._http("POST", "/internal/pairing/claim", {"code": code})
        self.assertEqual(st, 404)
        self.assertEqual(body["error"]["type"], "pairing_not_found")

    def test_unknown_and_malformed_codes_are_404_or_400(self):
        st, body = self._http("POST", "/internal/pairing/claim",
                              {"code": "pair-" + "h" * 24})
        self.assertEqual(st, 404)
        self.assertEqual(body["error"]["type"], "pairing_not_found")
        st, _ = self._http("POST", "/internal/pairing/claim", {"code": "tiny"})
        self.assertEqual(st, 400)
        st, _ = self._http("POST", "/internal/pairing/claim", {})
        self.assertEqual(st, 400)

    def test_disabled_router_has_no_pairing_endpoints(self):
        httpd = _BridgeHTTPServer(("127.0.0.1", 0), BridgeHttpHandler)
        httpd.core = None
        httpd.api_key = "x"
        httpd.worker_token = WORKER_TOKEN
        httpd.router = None
        httpd.log = self.log
        httpd.instance = "local"
        httpd.mode = "workspace-write"
        httpd.port = httpd.server_address[1]
        httpd._config_overrides = []
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/internal/pairing/claim",
                data=json.dumps({"code": "pair-" + "i" * 24}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("expected 404")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_server_logs_never_contain_code_or_token(self):
        code = "pair-" + "j" * 24
        st, _ = self._http("POST", "/internal/pairing/create",
                           {"code": code, "ttl_s": 600}, key=WORKER_TOKEN)
        self.assertEqual(st, 200)
        st, _ = self._http("POST", "/internal/pairing/claim", {"code": code})
        self.assertEqual(st, 200)
        lines = [line for line in self.log.lines if "pairing" in line]
        self.assertTrue(lines)
        for line in lines:
            self.assertNotIn(code, line)
            self.assertNotIn(WORKER_TOKEN, line)


class _FakePairingServer:
    """Loopback server recording pairing create requests for CLI tests."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            self.server.recorded.append({
                "path": self.path,
                "auth": self.headers.get("Authorization") or "",
                "body": json.loads(raw.decode("utf-8")) if raw else {},
            })
            if self.server.fail_status:
                payload = json.dumps(
                    {"error": {"type": "unauthorized",
                               "message": "missing or invalid Bearer API key"}}
                ).encode("utf-8")
                self.send_response(self.server.fail_status)
            else:
                payload = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def __init__(self, fail_status=0):
        self.recorded = []
        self.fail_status = fail_status
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._Handler)
        self.httpd.recorded = self.recorded
        self.httpd.fail_status = fail_status
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class PrepareWorkerPairingCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pairing-cli-")
        self.token = "worker-token-secret-0123456789abcdef"
        self.token_file = os.path.join(self.tmp, "worker.token")
        with open(self.token_file, "w", encoding="utf-8") as fh:
            fh.write(self.token)

    def tearDown(self):
        for name in os.listdir(self.tmp):
            try:
                os.unlink(os.path.join(self.tmp, name))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def _run(self, fake, extra=()):
        cli = os.path.join(ROOT, "scripts", "prepare_worker_pairing.py")
        proc = subprocess.run(
            [sys.executable, cli,
             "--worker-token-file", self.token_file,
             "--bridge-url", fake.base,
             "--mac-url", "https://router.example.ngrok-free.dev"] + list(extra),
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        return proc

    def test_mints_code_and_prints_single_windows_command(self):
        fake = _FakePairingServer()
        try:
            proc = self._run(fake)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            output = proc.stdout + proc.stderr
            self.assertNotIn(self.token, output)  # worker token NEVER printed
            m = re.search(r"-PairCode (\S+)", proc.stdout)
            self.assertIsNotNone(m, proc.stdout)
            code = m.group(1)
            self.assertGreaterEqual(len(code), 16)
            self.assertIn("start_local_codex_bridge.ps1", proc.stdout)
            self.assertIn("-MacBridgeUrl \"https://router.example.ngrok-free.dev\"",
                          proc.stdout)
            self.assertEqual(len(fake.recorded), 1)
            rec = fake.recorded[0]
            self.assertEqual(rec["path"], "/internal/pairing/create")
            self.assertEqual(rec["auth"], "Bearer " + self.token)
            self.assertEqual(rec["body"]["code"], code)
            self.assertEqual(rec["body"]["ttl_s"], 600)
            # only a hash ever lands next to the thread map server-side
        finally:
            fake.close()

    def test_failure_never_prints_token(self):
        fake = _FakePairingServer(fail_status=401)
        try:
            proc = self._run(fake)
            self.assertNotEqual(proc.returncode, 0)
            output = proc.stdout + proc.stderr
            self.assertNotIn(self.token, output)
            self.assertIn("error:", output)
            self.assertIn("401", output)
        finally:
            fake.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
