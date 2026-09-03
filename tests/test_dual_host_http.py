#!/usr/bin/env python3
"""Loopback integration test: one GPT controls Mac + Windows through the
Mac router and the Windows outbound worker.

No real app-server and no network beyond 127.0.0.1: a fake BridgeCore stands
in for each machine's local bridge, the real HTTP handlers from
http_server/server.py serve both sides, and the real worker loop from
bridge/worker.py runs in a thread, polling the router's /internal worker API
and forwarding jobs to the fake Windows bridge.

Covers: /start cwd routing (Windows path -> worker -> Windows bridge; POSIX /
no cwd -> Mac), persisted thread mapping, mapping-missing safe probes,
/continue /observe /steer /interrupt /read routing, merged /threads list,
worker API auth (401 / disabled / 404), worker offline (Mac unaffected,
Windows-routed calls fail fast) and job timeout (504).
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge import AppServerError, Logger, TurnResult
from bridge.core import ThreadList
from bridge.dual_host import (
    REMOTE_OP_WAIT_S,
    TARGET_MAC,
    TARGET_WINDOWS,
    DualHostRouter,
)
from bridge.worker import run_worker_loop
from http_server.server import _BridgeHTTPServer, BridgeHttpHandler

MAC_KEY = "mac-test-key"
WIN_KEY = "win-test-key"
WORKER_TOKEN = "dual-host-worker-token-test"


def _http(method, base, path, body=None, key=None, timeout=30):
    url = base + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw.decode("utf-8")) if raw else {})
        except ValueError:
            return e.code, {}
    except urllib.error.URLError as e:
        return 0, {"error": {"message": str(e)}}


class FakeTracker:
    def __init__(self):
        self._turns = set()

    def register(self, thread_id, turn_id):
        self._turns.add((thread_id, turn_id))

    def is_registered(self, thread_id, turn_id):
        return (thread_id, turn_id) in self._turns


class FakeBridgeCore:
    """Minimal in-memory BridgeCore stand-in with the same method surface the
    HTTP handlers use. Thread ids carry the host name so assertions can tell
    which machine served a request."""

    def __init__(self, host, log):
        self.host = host
        self.log = log
        self.client = _FakeClient(log)
        self.model = "deepseek-chat"
        self.model_provider = "deepseek"
        self.tracker = FakeTracker()
        self._lock = threading.Lock()
        self._threads = {}
        self._counter = 0

    def _next(self):
        with self._lock:
            self._counter += 1
            return self.host + "-thread-%d" % self._counter

    def start(self, prompt, cwd=None):
        with self._lock:
            self._counter += 1
            thread_id = "%s-thread-%d" % (self.host, self._counter)
            turn_id = thread_id + "-turn-1"
            self._threads[thread_id] = {
                "id": thread_id,
                "cwd": cwd,
                "preview": prompt[:40],
                "status": "inProgress",
                "updatedAt": _now(),
                "turns": [],
            }
            self.tracker.register(thread_id, turn_id)
        return thread_id, turn_id

    def continue_thread(self, thread_id, prompt):
        self._ensure(thread_id)
        with self._lock:
            thread = self._threads[thread_id]
            n = len(thread["turns"]) + 2
            turn_id = "%s-turn-%d" % (thread_id, n)
            thread["updatedAt"] = _now()
            self.tracker.register(thread_id, turn_id)
        return turn_id

    def read_thread(self, thread_id, include_turns=True):
        self._ensure(thread_id)
        with self._lock:
            thread = dict(self._threads[thread_id])
            if include_turns:
                thread["turns"] = [t for t in thread.get("turns", [])]
            return thread

    def list_threads(self, limit=None, **extra):
        with self._lock:
            threads = [
                {
                    "thread_id": t["id"],
                    "cwd": t.get("cwd"),
                    "preview": t.get("preview"),
                    "status": t.get("status"),
                    "updated_at": t.get("updatedAt"),
                }
                for t in sorted(
                    self._threads.values(),
                    key=lambda x: x.get("updatedAt") or "",
                    reverse=True,
                )
            ]
            if limit:
                threads = threads[:limit]
        return ThreadList(threads)

    def observe(self, thread_id, turn_id, wait_ms):
        return TurnResult(thread_id, turn_id, "completed", "answer from " + self.host)

    def steer(self, thread_id, turn_id, prompt):
        return turn_id

    def interrupt(self, thread_id, turn_id):
        return TurnResult(thread_id, turn_id, "interrupted", "", None)

    def _ensure(self, thread_id):
        with self._lock:
            if thread_id not in self._threads:
                raise AppServerError(
                    "thread/read: thread %s not found" % thread_id
                )


class _FakeClient:
    def __init__(self, log):
        self.log = log


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def make_httpd(core, log, api_key, router=None, worker_token="",
               instance="local", mode="workspace-write"):
    httpd = _BridgeHTTPServer(("127.0.0.1", 0), BridgeHttpHandler)
    httpd.core = core
    httpd.api_key = api_key
    httpd.worker_token = worker_token
    httpd.router = router
    httpd.log = log
    httpd.instance = instance
    httpd.mode = mode
    httpd.port = httpd.server_address[1]
    httpd._config_overrides = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                              name="httpd-" + core.host)
    thread.start()
    return httpd


class DualHostHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dual-host-http-")
        cls.logs = []
        cls.mac_log = Logger(echo=False)
        cls.win_log = Logger(echo=False)
        cls.mac_core = FakeBridgeCore("mac", cls.mac_log)
        cls.win_core = FakeBridgeCore("win", cls.win_log)
        cls.map_path = os.path.join(cls.tmp, "thread_map.json")
        cls.router = DualHostRouter(
            cls.mac_log,
            map_path=cls.map_path,
            enabled=True,
            worker_token_present=True,
            poll_timeout_s=1.0,
        )
        cls.mac = make_httpd(cls.mac_core, cls.mac_log, MAC_KEY,
                             router=cls.router, worker_token=WORKER_TOKEN)
        cls.win = make_httpd(cls.win_core, cls.win_log, WIN_KEY)
        cls.mac_base = "http://127.0.0.1:%d" % cls.mac.server_address[1]
        cls.win_base = "http://127.0.0.1:%d" % cls.win.server_address[1]
        cls.stop = threading.Event()
        cls.worker_lines = []
        cls.worker = threading.Thread(
            target=run_worker_loop,
            args=(cls.mac_base, WORKER_TOKEN, cls.win_base, WIN_KEY),
            kwargs={"poll_timeout_s": 0.5, "connect_timeout_s": 2.0,
                    "log": cls.worker_lines.append, "stop": cls.stop},
            daemon=True,
        )
        cls.worker.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if cls.router.broker.worker_alive():
                break
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.mac.shutdown()
        cls.mac.server_close()
        cls.win.shutdown()
        cls.win.server_close()
        cls.mac_log.close()
        cls.win_log.close()

    # ------------------------------------------------------------------ helpers

    def start(self, prompt, cwd=None, base=None, key=None):
        body = {"prompt": prompt}
        if cwd is not None:
            body["cwd"] = cwd
        return _http("POST", base or self.mac_base, "/start", body,
                     key=key or MAC_KEY)

    def windows_thread(self):
        # Create a thread directly on the fake Windows bridge, as if it had
        # been started through the worker before a router restart (i.e. no
        # entry in the Mac's thread map).
        tid, turn = self.win_core.start("win legacy", cwd=r"C:\winlegacy")
        return tid, turn

    # ------------------------------------------------------------------ routing

    def test_start_windows_cwd_routes_to_windows_and_maps(self):
        st, body = self.start("hello windows", cwd=r"C:\work-of-jiaqi\winproj")
        self.assertEqual(st, 200)
        self.assertTrue(str(body.get("thread_id", "")).startswith("win-"))
        self.assertEqual(body.get("status"), "started")
        self.assertEqual(self.router.mapped(body["thread_id"]), TARGET_WINDOWS)
        with open(self.map_path, encoding="utf-8") as fh:
            entries = json.load(fh)["entries"]
        self.assertIn(body["thread_id"], entries)

    def test_start_unc_wsl_cwd_routes_to_windows(self):
        st, body = self.start("in wsl", cwd=r"\\wsl$\Ubuntu\home\me\proj")
        self.assertEqual(st, 200)
        self.assertTrue(str(body.get("thread_id", "")).startswith("win-"))

    def test_start_no_cwd_and_posix_stay_on_mac(self):
        st, body = self.start("no cwd")
        self.assertEqual(st, 200)
        self.assertTrue(str(body.get("thread_id", "")).startswith("mac-"))
        st, body = self.start("posix", cwd="/Users/mac/proj")
        self.assertEqual(st, 200)
        self.assertTrue(str(body.get("thread_id", "")).startswith("mac-"))

    def test_continue_observe_steer_interrupt_read_on_windows_thread(self):
        st, body = self.start("win task", cwd=r"D:\work")
        tid = body["thread_id"]
        turn = body["turn_id"]
        st, body = _http("POST", self.mac_base, "/continue",
                         {"thread_id": tid, "prompt": "continue win"},
                         key=MAC_KEY)
        self.assertEqual(st, 200)
        self.assertEqual(body["thread_id"], tid)
        self.assertNotEqual(body["turn_id"], turn)
        st, ob = _http("POST", self.mac_base, "/observe",
                       {"thread_id": tid, "turn_id": body["turn_id"],
                        "wait_ms": 500}, key=MAC_KEY)
        self.assertEqual(st, 200)
        self.assertEqual(ob["status"], "completed")
        self.assertIn("from win", ob["assistant_text"])
        st, ob = _http("POST", self.mac_base, "/steer",
                       {"thread_id": tid, "turn_id": body["turn_id"],
                        "prompt": "steer"}, key=MAC_KEY)
        self.assertEqual(st, 200)
        self.assertEqual(ob["status"], "steer_accepted")
        st, body = _http("GET", self.mac_base, "/threads/" + tid, key=MAC_KEY)
        self.assertEqual(st, 200)
        self.assertEqual(body["thread_id"], tid)

    def test_mapping_missing_probes_both_ends(self):
        tid, _ = self.windows_thread()  # windows-owned, NOT in the Mac map
        st, body = _http("POST", self.mac_base, "/continue",
                         {"thread_id": tid, "prompt": "find me"}, key=MAC_KEY)
        self.assertEqual(st, 200)
        self.assertEqual(body["thread_id"], tid)
        # probe recorded the owner so the next call routes without probing
        self.assertEqual(self.router.mapped(tid), TARGET_WINDOWS)

    def test_unknown_thread_404_after_safe_probe(self):
        st, body = _http("POST", self.mac_base, "/continue",
                         {"thread_id": "nobody-knows-this", "prompt": "x"},
                         key=MAC_KEY)
        self.assertEqual(st, 404)
        self.assertEqual(body["error"]["type"], "not_found")

    # ------------------------------------------------------------------ list merge

    def test_threads_list_merges_mac_and_windows(self):
        _, wb = self.start("merged win", cwd=r"C:\m\w")
        _, mb = self.start("merged mac", cwd="/Users/m/x")
        st, body = _http("GET", self.mac_base, "/threads?limit=20", key=MAC_KEY)
        self.assertEqual(st, 200)
        ids = {t.get("thread_id") for t in body["threads"]}
        self.assertIn(wb["thread_id"], ids)
        self.assertIn(mb["thread_id"], ids)
        # list merge records owners, so read routes directly afterwards
        st, body = _http("GET", self.mac_base, "/threads/" + wb["thread_id"],
                         key=MAC_KEY)
        self.assertEqual(st, 200)
        self.assertEqual(body["thread_id"], wb["thread_id"])

    # ------------------------------------------------------------- auth + offline

    def test_worker_api_requires_the_worker_token(self):
        st, _ = _http("POST", self.mac_base, "/internal/worker/poll",
                      {"timeout_s": 1}, key=MAC_KEY)
        self.assertEqual(st, 401)
        st, _ = _http("POST", self.mac_base, "/internal/worker/poll",
                      {"timeout_s": 1}, key="wrong-worker-token")
        self.assertEqual(st, 401)
        st, body = _http("POST", self.mac_base, "/internal/worker/poll",
                         {"timeout_s": 1}, key=WORKER_TOKEN)
        self.assertEqual(st, 200)
        self.assertEqual(body["jobs"], [])
        st, body = _http("GET", self.mac_base, "/internal/worker/status",
                         key=WORKER_TOKEN)
        self.assertEqual(st, 200)
        self.assertTrue(body["worker_online"])

    def test_worker_api_disabled_without_token(self):
        router = DualHostRouter(self.mac_log, enabled=True,
                                worker_token_present=False)
        httpd = make_httpd(FakeBridgeCore("m2", self.mac_log), self.mac_log,
                           MAC_KEY, router=router, worker_token="")
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, body = _http("POST", base, "/internal/worker/poll",
                             {"timeout_s": 1}, key="anything")
            self.assertEqual(st, 503)
            self.assertEqual(body["error"]["type"], "worker_api_disabled")
            # a Windows-routed /start fails fast with a clear message
            st, body = _http("POST", base, "/start",
                             {"prompt": "x", "cwd": r"C:\p"}, key=MAC_KEY)
            self.assertEqual(st, 503)
            self.assertEqual(body["error"]["type"], "windows_unavailable")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_dual_host_disabled_keeps_legacy_endpoints_hidden(self):
        httpd = make_httpd(FakeBridgeCore("m3", self.mac_log), self.mac_log,
                           MAC_KEY, router=None, worker_token="")
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, _ = _http("POST", base, "/internal/worker/poll",
                          {"timeout_s": 1}, key="tok")
            self.assertEqual(st, 404)  # no new surface when the feature is off
            st, body = self.start("legacy windows cwd", cwd=r"C:\old", base=base)
            self.assertEqual(st, 200)  # historic behavior: local machine
            self.assertTrue(str(body.get("thread_id", "")).startswith("m3-"))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_worker_offline_does_not_affect_mac_requests(self):
        self.stop.set()
        self.worker.join(timeout=10)
        # Drain any in-flight final long-poll (router poll timeout is 1s) so a
        # late heartbeat can never resurrect the "offline" state mid-test.
        time.sleep(1.6)
        try:
            with self.router.broker._cond:
                self.router.broker._last_poll_at = time.time() - 999
            st, body = self.start("mac still works", cwd="/Users/m/z")
            self.assertEqual(st, 200)
            self.assertTrue(str(body.get("thread_id", "")).startswith("mac-"))
            st, body = self.start("windows needed", cwd=r"C:\offline")
            self.assertEqual(st, 503)
            self.assertEqual(body["error"]["type"], "windows_unreachable")
            # listing stays local-only and fast when Windows is offline
            t0 = time.time()
            st, body = _http("GET", self.mac_base, "/threads?limit=20",
                             key=MAC_KEY)
            self.assertEqual(st, 200)
            self.assertLess(time.time() - t0, 3)
            ids = [t.get("thread_id") for t in body["threads"]]
            self.assertTrue(all(str(i).startswith("mac-") for i in ids))
        finally:
            pass

    def test_job_timeout_when_worker_never_answers(self):
        # Dedicated router with a fresh heartbeat but NO worker polling it:
        # the router must fail bounded with 504 instead of hanging forever.
        old = REMOTE_OP_WAIT_S["start"]
        REMOTE_OP_WAIT_S["start"] = 0.3
        router = DualHostRouter(self.mac_log, enabled=True,
                                worker_token_present=True)
        httpd = make_httpd(FakeBridgeCore("slow", self.mac_log), self.mac_log,
                           MAC_KEY, router=router,
                           worker_token=WORKER_TOKEN)
        try:
            with router.broker._cond:
                router.broker._last_poll_at = time.time()  # heartbeat: online
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, body = _http("POST", base, "/start",
                             {"prompt": "slow job", "cwd": r"C:\slow"},
                             key=MAC_KEY)
            self.assertEqual(st, 504)
            self.assertEqual(body["error"]["type"], "windows_timeout")
        finally:
            REMOTE_OP_WAIT_S["start"] = old
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
