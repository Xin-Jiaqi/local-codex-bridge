#!/usr/bin/env python3
"""Offline unit tests for the dual-host router core (runs on every host).

Covers bridge/dual_host.py (Windows-path classification, persisted
thread_id -> host mapping, bounded worker job broker) and bridge/worker.py
(allowlisted job forwarding, bounded responses, worker-token auth failures,
log hygiene). No real app-server, no network beyond loopback HTTP stubs.
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge.dual_host import (
    MAX_PENDING_JOBS,
    TARGET_MAC,
    TARGET_WINDOWS,
    BrokerBusyError,
    RemoteError,
    ThreadTargetMap,
    WorkerBroker,
    classify_cwd,
)
from bridge.worker import (
    WorkerConfigError,
    _poll_router,
    execute_job,
)


class ClassifyCwdTest(unittest.TestCase):
    def test_windows_drive_paths(self):
        for cwd in (r"C:\work\proj", r"C:/work/proj", r"D:\a", r"c:\a\b",
                    r"C:", "Z:\\"):
            self.assertEqual(classify_cwd(cwd), TARGET_WINDOWS, cwd)

    def test_unc_and_wsl_paths(self):
        for cwd in (r"\\server\share", r"\\wsl$\Ubuntu\home\x",
                    r"\\wsl.localhost\Ubuntu\home", r"//server/share",
                    r"//wsl$/Ubuntu"):
            self.assertEqual(classify_cwd(cwd), TARGET_WINDOWS, cwd)

    def test_macos_and_default_stay_mac(self):
        for cwd in (None, "", "   ", "/Users/x/proj", "relative/dir",
                    r"~/x", "D:foo/relative-looking"):
            # "D:foo" without a slash is drive-relative Windows, but only the
            # documented native forms (drive root + separators / UNC) route
            # to Windows; anything ambiguous keeps the historic Mac default.
            self.assertEqual(classify_cwd(cwd), TARGET_MAC, cwd)

    def test_non_string_stays_mac(self):
        self.assertEqual(classify_cwd(123), TARGET_MAC)
        self.assertEqual(classify_cwd(["C:\\x"]), TARGET_MAC)


class ThreadTargetMapTest(unittest.TestCase):
    def test_memory_map(self):
        m = ThreadTargetMap()
        self.assertIsNone(m.get("t1"))
        m.set("t1", TARGET_WINDOWS)
        m.set("t1", TARGET_WINDOWS)  # idempotent
        self.assertEqual(m.get("t1"), TARGET_WINDOWS)
        self.assertEqual(len(m), 1)
        with self.assertRaises(ValueError):
            m.set("t2", "mars")

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "thread_map.json")
            m = ThreadTargetMap(path)
            m.set("t1", TARGET_MAC)
            m.set("t2", TARGET_WINDOWS)
            m2 = ThreadTargetMap(path)
            self.assertEqual(m2.get("t1"), TARGET_MAC)
            self.assertEqual(m2.get("t2"), TARGET_WINDOWS)

    def test_corrupt_or_missing_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "thread_map.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            m = ThreadTargetMap(path)
            self.assertIsNone(m.get("x"))
            m.set("x", TARGET_MAC)
            self.assertEqual(m.get("x"), TARGET_MAC)

    def test_prune_keeps_newest(self):
        m = ThreadTargetMap()
        for i in range(4100):
            m.set("t%04d" % i, TARGET_MAC if i % 2 else TARGET_WINDOWS)
        self.assertLessEqual(len(m), 4000)
        self.assertIsNone(m.get("t0000"))
        self.assertEqual(m.get("t4099"), TARGET_MAC)

    def test_map_never_contains_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "thread_map.json")
            m = ThreadTargetMap(path)
            m.set("t1", TARGET_WINDOWS)
            data = open(path, encoding="utf-8").read()
            self.assertIn("t1", data)
            self.assertNotIn("prompt", data.lower())


class WorkerBrokerTest(unittest.TestCase):
    def test_poll_wait_result_flow(self):
        b = WorkerBroker(poll_timeout_s=1)
        job = b.enqueue("start", {"prompt": "x", "cwd": r"C:\p"})
        self.assertEqual(job["kind"], "start")
        self.assertEqual(b.status()["pending_kinds"], {"start": 1})
        picked = b.poll(0.5)
        self.assertEqual(picked["id"], job["id"])
        self.assertIsNone(b.poll(0.1))  # queue empty -> bounded wait expires
        b.submit_result(job["id"], {"status": 200, "body": {"thread_id": "w1"}})
        res = b.wait_result(job["id"], 1)
        self.assertEqual(res["status"], 200)
        self.assertIsNone(b.wait_result(job["id"], 0.1))  # delivered once

    def test_result_before_wait_is_retained(self):
        b = WorkerBroker()
        b.submit_result("late", {"status": 200, "body": None})
        self.assertEqual(b.wait_result("late", 1)["status"], 200)

    def test_queue_is_bounded(self):
        b = WorkerBroker(max_pending=3)
        for i in range(3):
            b.enqueue("list", {"limit": 5})
        with self.assertRaises(BrokerBusyError):
            b.enqueue("list", {"limit": 5})

    def test_disallowed_kind_and_oversize_rejected(self):
        b = WorkerBroker()
        with self.assertRaises(ValueError):
            b.enqueue("rm -rf", {})
        with self.assertRaises(ValueError):
            b.enqueue("start", {"prompt": "x" * (64 * 1024 + 1)})

    def test_expired_jobs_are_skipped(self):
        b = WorkerBroker(job_ttl_s=0.1, poll_timeout_s=0.2)
        job = b.enqueue("list", {})
        time.sleep(0.25)
        self.assertIsNone(b.poll(0.2))
        # enqueuer that gave up leaves no trace: status pending drops
        self.assertEqual(b.status()["pending"], 0)

    def test_heartbeat_and_offline(self):
        b = WorkerBroker()
        self.assertFalse(b.worker_alive())
        b.enqueue("list", {})
        b.poll(0.05)
        self.assertTrue(b.worker_alive())
        with b._cond:
            b._last_poll_at = time.time() - 999
        self.assertFalse(b.worker_alive())

    def test_result_cap_drops_oversize_body(self):
        b = WorkerBroker()
        b.submit_result("big", {
            "status": 200,
            "body": {"blob": "x" * (600 * 1024)},
        })
        res = b.wait_result("big", 1)
        self.assertIsNone(res["body"])
        self.assertIn("too large", res["error"])


class RemoteCallTest(unittest.TestCase):
    def test_remote_errors_map_to_http_shapes(self):
        from bridge.dual_host import DualHostRouter
        from bridge import Logger
        log = Logger(echo=False)
        try:
            r = DualHostRouter(log, enabled=False)
            with self.assertRaises(RemoteError) as ctx:
                r.remote("list")
            self.assertEqual(ctx.exception.status, 503)
            self.assertEqual(ctx.exception.error_type, "dual_host_disabled")

            r = DualHostRouter(log, enabled=True, worker_token_present=False)
            with self.assertRaises(RemoteError) as ctx:
                r.remote("start", {"prompt": "x"})
            self.assertEqual(ctx.exception.status, 503)
            self.assertEqual(ctx.exception.error_type, "windows_unavailable")

            r = DualHostRouter(log, enabled=True, worker_token_present=True)
            with self.assertRaises(RemoteError) as ctx:
                r.remote("start", {"prompt": "x"})  # no worker heartbeat
            self.assertEqual(ctx.exception.status, 503)
            self.assertEqual(ctx.exception.error_type, "windows_unreachable")
        finally:
            log.close()

    def test_remote_timeout_when_worker_alive_but_silent(self):
        from bridge.dual_host import DualHostRouter
        from bridge import Logger
        log = Logger(echo=False)
        try:
            r = DualHostRouter(log, enabled=True, worker_token_present=True)
            with r.broker._cond:
                r.broker._last_poll_at = time.time()  # heartbeat: online
            with self.assertRaises(RemoteError) as ctx:
                r.remote("list", {}, wait_s=0.3)  # no worker picks it up
            self.assertEqual(ctx.exception.status, 504)
            self.assertEqual(ctx.exception.error_type, "windows_timeout")
            self.assertEqual(r.broker.status()["pending"], 1)  # TTL keeps it
        finally:
            log.close()


class _StubHandler(BaseHTTPRequestHandler):
    """Loopback stub used to exercise worker auth + job forwarding."""

    jobs = []

    def log_message(self, *args):
        pass

    def _send(self, code, obj=None, raw=None):
        if raw is not None:
            body = raw
            ctype = "application/octet-stream"
        else:
            body = json.dumps(obj).encode("utf-8")
            ctype = "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/threads/") and "big" not in self.path:
            self._send(200, {"thread_id": self.path.rsplit("/", 1)[-1]})
        elif self.path.startswith("/threads?limit="):
            self._send(200, {"threads": [{"thread_id": "wt-1"}]})
        elif self.path.startswith("/threads/") and "big" in self.path:
            self._send(200, raw=b"x" * (600 * 1024))
        else:
            self._send(404, {"error": {"type": "not_found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.path == "/internal/worker/poll":
            auth = self.headers.get("Authorization") or ""
            if auth != "Bearer worker-token-ok":
                self._send(401, {"error": {"type": "unauthorized"}})
                return
            self._send(200, {"jobs": []})
            return
        if self.path == "/internal/worker/result":
            self._send(200, {"ok": True})
            return
        body = json.loads(raw) if raw else {}
        if self.path == "/start":
            self._send(200, {"thread_id": "win-1", "turn_id": "t1",
                             "status": "started"})
        elif self.path == "/continue":
            self._send(200, {"thread_id": body.get("thread_id"),
                             "turn_id": "t2", "status": "started"})
        else:
            self._send(200, {"path": self.path})


class WorkerForwardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_forward_start_and_continue_shapes(self):
        job = {"id": "job1", "kind": "start",
               "params": {"prompt": "do it", "cwd": r"C:\proj"}}
        res = execute_job(job, self.base, "local-key")
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["body"]["thread_id"], "win-1")

        job = {"id": "job2", "kind": "continue",
               "params": {"thread_id": "win-1", "prompt": "again"}}
        res = execute_job(job, self.base, "local-key")
        self.assertEqual(res["body"]["turn_id"], "t2")

    def test_read_and_list_allowlist(self):
        res = execute_job({"id": "j", "kind": "read",
                           "params": {"thread_id": "win-1"}}, self.base, "k")
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["body"]["thread_id"], "win-1")
        res = execute_job({"id": "j", "kind": "list",
                           "params": {"limit": 99}}, self.base, "k")
        self.assertEqual(res["body"]["threads"][0]["thread_id"], "wt-1")

    def test_unknown_kind_is_never_forwarded(self):
        res = execute_job({"id": "j", "kind": "shell",
                           "params": {"cmd": "calc.exe"}}, self.base, "k")
        self.assertEqual(res["status"], 400)
        self.assertIn("disallowed", res["error"])

    def test_oversize_local_response_is_capped(self):
        res = execute_job({"id": "j", "kind": "read",
                           "params": {"thread_id": "big"}}, self.base, "k")
        self.assertEqual(res["status"], 502)
        self.assertIn("cap", res["error"])

    def test_local_bridge_down_is_an_error_envelope(self):
        res = execute_job({"id": "j", "kind": "start",
                           "params": {"prompt": "x"}},
                          "http://127.0.0.1:1", "k")
        self.assertEqual(res["status"], 502)
        self.assertIn("local bridge unreachable", res["error"])

    def test_logs_never_contain_prompt_or_secret(self):
        lines = []
        job = {"id": "job-secret", "kind": "start",
               "params": {"prompt": "MY-SECRET-PROMPT-42",
                          "cwd": r"C:\proj"}}
        res = execute_job(job, self.base, "SECRET-LOCAL-KEY-7", log=lines.append)
        self.assertEqual(res["status"], 200)
        joined = "\n".join(lines)
        self.assertNotIn("MY-SECRET-PROMPT-42", joined)
        self.assertNotIn("SECRET-LOCAL-KEY-7", joined)

    def test_poll_router_token_auth_and_config_errors(self):
        url = self.base
        jobs, fatal = _poll_router(url, "worker-token-ok", 0.1, 2)
        self.assertEqual(jobs, [])
        self.assertIsNone(fatal)
        _, fatal = _poll_router(url, "wrong-token", 0.1, 2)
        self.assertIsInstance(fatal, WorkerConfigError)
        self.assertIn("401", str(fatal))


class _NotFoundHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class WorkerRouterCompatTest(unittest.TestCase):
    def test_old_router_without_internal_api_is_fatal(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NotFoundHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d" % httpd.server_address[1]
            _, fatal = _poll_router(url, "tok", 0.1, 2)
            self.assertIsInstance(fatal, WorkerConfigError)
            self.assertIn("404", str(fatal))
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
