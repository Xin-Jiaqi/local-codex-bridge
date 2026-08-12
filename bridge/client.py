"""Reusable Codex app-server client.

Owns the app-server subprocess lifecycle (spawn / shutdown), speaks JSON-RPC
2.0 over stdio (newline-delimited JSON frames), dispatches responses to
pending requests and notifications to registered handlers, logs server stderr,
and fails pending work cleanly when the process dies.
"""

import json
import os
import subprocess
import threading
import time


class AppServerError(Exception):
    """Base error for the app-server client."""


class RequestTimeoutError(AppServerError):
    """A JSON-RPC request did not get a response in time."""


class AppServerProcessError(AppServerError):
    """The app-server process is dead, failed to spawn, or closed its pipes."""


class Logger:
    """Thread-safe line logger writing to stdout and (optionally) a file.

    Every line is written under a lock, so concurrent writers (reader thread,
    main thread) can never interleave or corrupt each other.
    """

    def __init__(self, path=None, echo=True):
        self._lock = threading.Lock()
        self._file = open(path, "w", encoding="utf-8") if path else None
        self._echo = echo

    def info(self, msg):
        line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with self._lock:
            if self._echo:
                print(line, flush=True)
            if self._file is not None:
                self._file.write(line + "\n")
                self._file.flush()

    def close(self):
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


class CodexAppServerClient:
    """JSON-RPC client for `codex app-server` over stdio."""

    # High-frequency stream notifications: captured by the core tracker, but
    # far too noisy to print one line per token delta.
    QUIET_NOTIFICATIONS = frozenset({"item/reasoning/textDelta", "item/agentMessage/delta"})

    def __init__(self, codex_bin, codex_home, config_overrides=None, logger=None):
        self.codex_bin = os.path.abspath(os.path.expanduser(codex_bin))
        self.codex_home = os.path.abspath(os.path.expanduser(codex_home))
        self.config_overrides = list(config_overrides or [])
        self.log = logger or Logger()
        self._proc = None
        self._reader = None
        self._stderr_reader = None
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending = {}  # request id -> (event, holder)
        self._handlers = {}  # method -> [handler(method, params, raw_msg)]
        self._process_exit = threading.Event()
        self._exit_code = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def alive(self):
        return self._proc is not None and not self._process_exit.is_set()

    def start(self, timeout=60.0):
        """Spawn app-server and complete `initialize`."""
        if self._proc is not None:
            raise AppServerError("client already started")
        env = dict(os.environ)
        env["CODEX_HOME"] = self.codex_home
        args = [self.codex_bin, "app-server", "--listen", "stdio://"]
        for kv in self.config_overrides:
            args += ["-c", kv]
        self.log.info("spawn: %s" % " ".join(args))
        self.log.info("CODEX_HOME: %s" % self.codex_home)
        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )
        except OSError as e:
            raise AppServerProcessError("failed to spawn app-server: %s" % e) from e
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="app-server-reader")
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._stderr_loop, daemon=True, name="app-server-stderr")
        self._stderr_reader.start()
        try:
            res = self.request(
                "initialize",
                {"clientInfo": {"name": "local-codex-bridge-core", "version": "1.0.0"}},
                timeout=timeout,
            )
            self.log.info(
                "initialize: userAgent=%s codexHome=%s"
                % (res.get("userAgent"), res.get("codexHome"))
            )
        except Exception:
            self.close()
            raise

    def close(self, timeout=5.0):
        """Gracefully shut down the app-server (shutdown request, then SIGTERM)."""
        proc = self._proc
        if proc is None:
            return
        try:
            self.request("shutdown", {}, timeout=3.0)
        except Exception:
            pass  # server may not implement shutdown; terminate below anyway
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self._process_exit.wait(timeout=2)  # let the reader drain final lines
        self.log.info("app-server closed")
        self._proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ------------------------------------------------------------------ events

    def on(self, method, handler):
        """Register a notification handler. Use method="*" for all notifications."""
        with self._lock:
            self._handlers.setdefault(method, []).append(handler)

    def wait_exit(self, timeout=None):
        """Block until the app-server process exits (or timeout)."""
        return self._process_exit.wait(timeout)

    # ------------------------------------------------------------------ transport

    def request(self, method, params=None, timeout=60.0):
        """Send a JSON-RPC request and wait for its response."""
        self._ensure_alive()
        with self._lock:
            self._next_id += 1
            rid = str(self._next_id)
            event = threading.Event()
            holder = {}
            self._pending[rid] = (event, holder)
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        self.log.info("-> %s %s" % (method, json.dumps(payload["params"])[:200]))
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._handle_eof()
            raise AppServerProcessError("app-server pipe closed: %s" % e) from e
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            if self._process_exit.is_set():
                raise AppServerProcessError(
                    "app-server exited while waiting for %s (exit code=%s)"
                    % (method, self._exit_code)
                )
            raise RequestTimeoutError("%s timed out after %ss" % (method, timeout))
        resp = holder["resp"]
        if "error" in resp:
            err = resp["error"]
            raise AppServerError("%s error %s: %s" % (method, err.get("code"), err.get("message")))
        self.log.info("<- %s ok" % method)
        return resp.get("result")

    # ------------------------------------------------------------------ internals

    def _read_loop(self):
        for raw in self._proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                self.log.info("[server] non-JSON line: %s" % raw[:200])
                continue
            if "id" in msg:
                self._on_response(msg)
            else:
                self._on_notification(msg)
        self._handle_eof()

    def _on_response(self, msg):
        rid = str(msg.get("id"))
        with self._lock:
            item = self._pending.pop(rid, None)
        if item is None:
            # Incoming server->client REQUEST (e.g. a permission prompt).
            # The bridge core does not handle interactive approvals yet, so
            # reply method-not-found and let the server decide.
            self.log.info(
                "[server->client request] %s id=%s (unsupported; replying -32601)"
                % (msg.get("method"), rid)
            )
            self._reply_raw(
                {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {"code": -32601, "message": "method not supported by bridge core client"},
                }
            )
            return
        item[1]["resp"] = msg
        item[0].set()

    def _on_notification(self, msg):
        method = msg.get("method") or "?"
        params = msg.get("params") or {}
        if method not in self.QUIET_NOTIFICATIONS:
            self.log.info("[event] %s %s" % (method, json.dumps(params)[:200]))
        with self._lock:
            handlers = list(self._handlers.get(method, ())) + list(self._handlers.get("*", ()))
        for h in handlers:
            try:
                h(method, params, msg)
            except Exception as e:
                self.log.info("[handler error] %s: %r" % (method, e))

    def _stderr_loop(self):
        try:
            for line in self._proc.stderr:
                self.log.info("[stderr] %s" % line.rstrip())
        except Exception:
            pass

    def _reply_raw(self, payload):
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except Exception:
            pass

    def _handle_eof(self):
        """Process exited or pipe closed: fail all pending requests once."""
        if self._process_exit.is_set():
            return
        self._process_exit.set()
        try:
            self._exit_code = self._proc.poll()
        except Exception:
            self._exit_code = None
        self.log.info("app-server process exited (code=%s)" % self._exit_code)
        err = AppServerProcessError("app-server exited unexpectedly (code=%s)" % self._exit_code)
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
        for event, holder in items:
            holder["resp"] = {"error": {"code": -32000, "message": str(err)}}
            event.set()

    def _ensure_alive(self):
        if self._proc is None:
            raise AppServerError("client not started; call start() first")
        if self._process_exit.is_set():
            raise AppServerProcessError("app-server not running (exit code=%s)" % self._exit_code)
