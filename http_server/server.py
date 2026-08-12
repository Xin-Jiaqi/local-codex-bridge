"""Zero-dependency HTTP server exposing BridgeCore over JSON.

One persistent Codex app-server client is spawned at startup
(CODEX_HOME=~/.codex-deepseek, model=deepseek-chat,
model_provider=deepseek). All endpoints except GET /health require
`Authorization: Bearer <BRIDGE_API_KEY>`.

The app-server is always spawned with `approval_policy="never"` and
`sandbox_mode="workspace-write"` (see CONFIG_OVERRIDES): commands and file
writes inside the workspace run without any approval prompt, and anything
outside the workspace is auto-denied. This is the V1 security boundary; the
bridge never forwards interactive approval requests to ChatGPT.
"""

import hmac
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge import AppServerError, BridgeCore, CodexAppServerClient, Logger
from bridge.core import MODEL, MODEL_PROVIDER, REASONING_EFFORT

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321
MAX_OBSERVE_WAIT_MS = 10_000
MAX_ASSISTANT_TEXT = 4000
MAX_READ_TURNS = 20
MAX_BODY_BYTES = 64 * 1024

CONFIG_OVERRIDES = [
    'model="%s"' % MODEL,
    'model_reasoning_effort="%s"' % REASONING_EFFORT,
    'model_provider="%s"' % MODEL_PROVIDER,
    # V1 security boundary: workspace-local read/write/shell run without
    # prompts; anything outside the workspace is auto-denied. Without these
    # overrides the app-server falls back to its config-file approval policy
    # (e.g. "untrusted"), which sends item/commandExecution/requestApproval
    # for every command and makes the bridge reply -32601 -> "approval
    # request failed".
    'approval_policy="never"',
    'sandbox_mode="workspace-write"',
]


class HttpApiError(Exception):
    def __init__(self, status, message, code="bad_request"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class _BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.core = None
        self.api_key = ""
        self.log = None


class BridgeHttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    # ------------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):
        try:
            self.server.log.info("[http] " + fmt % args)
        except Exception:
            pass

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message, code="bad_request"):
        self._send_json(status, {"error": {"type": code, "message": message}})

    def _read_body(self):
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            raise HttpApiError(400, "invalid Content-Length", "bad_request")
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise HttpApiError(413, "request body too large", "payload_too_large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpApiError(400, "request body must be valid JSON", "invalid_json")
        if not isinstance(data, dict):
            raise HttpApiError(400, "request body must be a JSON object", "invalid_json")
        return data

    def _require_auth(self):
        expected = self.server.api_key or ""
        if not expected:
            raise HttpApiError(503, "server started without BRIDGE_API_KEY", "auth_unconfigured")
        header = self.headers.get("Authorization") or ""
        token = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token, expected):
            raise HttpApiError(401, "missing or invalid Bearer API key", "unauthorized")

    @staticmethod
    def _require_str(body, key, max_len=4000):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip():
            raise HttpApiError(400, "missing or invalid field: %s" % key)
        if len(value) > max_len:
            raise HttpApiError(400, "field too long: %s" % key)
        return value.strip()

    @staticmethod
    def _require_int(body, key, default, max_value):
        value = body.get(key, default)
        if value is None:
            value = default
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise HttpApiError(400, "field must be an integer: %s" % key)
        return max(0, min(value, max_value))

    # ------------------------------------------------------------------ routes

    def do_GET(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                self._handle_health()
                return
            if path == "/threads":
                self._require_auth()
                self._handle_list()
                return
            if path.startswith("/threads/"):
                self._require_auth()
                thread_id = urllib.parse.unquote(path[len("/threads/"):])
                if not thread_id:
                    raise HttpApiError(404, "missing thread_id", "not_found")
                self._handle_read(thread_id)
                return
            raise HttpApiError(404, "not found: %s" % path, "not_found")
        except HttpApiError as e:
            self._error(e.status, e.message, e.code)
        except AppServerError as e:
            self._error(502, "app-server error: %s" % e, "app_server_error")
        except Exception as e:
            self.server.log.info("GET handler error: %r" % e)
            self._error(500, "internal server error", "internal_error")

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            self._require_auth()
            if path == "/start":
                self._handle_start(self._read_body())
            elif path == "/continue":
                self._handle_continue(self._read_body())
            elif path == "/observe":
                self._handle_observe(self._read_body())
            elif path == "/steer":
                self._handle_steer(self._read_body())
            elif path == "/interrupt":
                self._handle_interrupt(self._read_body())
            else:
                raise HttpApiError(404, "not found: %s" % path, "not_found")
        except HttpApiError as e:
            self._error(e.status, e.message, e.code)
        except AppServerError as e:
            self._error(502, "app-server error: %s" % e, "app_server_error")
        except Exception as e:
            self.server.log.info("POST handler error: %r" % e)
            self._error(500, "internal server error", "internal_error")

    # ------------------------------------------------------------------ handlers

    def _handle_health(self):
        core = self.server.core
        alive = core.client.alive
        payload = {
            "status": "ok" if alive else "unavailable",
            "app_server_alive": alive,
            "model": core.model,
            "model_provider": core.model_provider,
        }
        self._send_json(200 if alive else 503, payload)

    def _handle_start(self, body):
        prompt = self._require_str(body, "prompt")
        cwd = body.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            raise HttpApiError(400, "invalid field: cwd")
        core = self.server.core
        thread_id, turn_id = core.start(prompt, cwd=cwd.strip() if cwd else None)
        self.server.log.info("http /start: thread=%s turn=%s" % (thread_id, turn_id))
        self._send_json(200, {"thread_id": thread_id, "turn_id": turn_id, "status": "started"})

    def _handle_continue(self, body):
        thread_id = self._require_str(body, "thread_id")
        prompt = self._require_str(body, "prompt")
        core = self.server.core
        turn_id = core.continue_thread(thread_id, prompt)
        self.server.log.info("http /continue: thread=%s turn=%s" % (thread_id, turn_id))
        self._send_json(200, {"thread_id": thread_id, "turn_id": turn_id, "status": "started"})

    def _handle_observe(self, body):
        thread_id = self._require_str(body, "thread_id")
        turn_id = self._require_str(body, "turn_id")
        wait_ms = self._require_int(body, "wait_ms", 5000, MAX_OBSERVE_WAIT_MS)
        core = self.server.core
        if not core.tracker.is_registered(thread_id, turn_id):
            raise HttpApiError(404, "unknown turn_id %s on thread %s" % (turn_id, thread_id), "not_found")
        r = core.observe(thread_id, turn_id, wait_ms)
        self.server.log.info(
            "http /observe: thread=%s turn=%s wait_ms=%d status=%s"
            % (thread_id, turn_id, wait_ms, r.status)
        )
        self._send_json(200, {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": r.status,
            "assistant_text": r.assistant_text,
            "error": r.error,
        })

    def _handle_steer(self, body):
        thread_id = self._require_str(body, "thread_id")
        turn_id = self._require_str(body, "turn_id")
        prompt = self._require_str(body, "prompt")
        core = self.server.core
        accepted = core.steer(thread_id, turn_id, prompt)
        self.server.log.info("http /steer: thread=%s turn=%s accepted" % (thread_id, accepted))
        self._send_json(200, {
            "thread_id": thread_id,
            "turn_id": accepted,
            "status": "steer_accepted",
            "note": (
                "Codex 0.147.0 steer queues the instruction into the active turn; "
                "it does not interrupt the current generation. Use /interrupt then "
                "/continue to change direction immediately."
            ),
        })

    def _handle_interrupt(self, body):
        thread_id = self._require_str(body, "thread_id")
        turn_id = self._require_str(body, "turn_id")
        core = self.server.core
        if not core.tracker.is_registered(thread_id, turn_id):
            raise HttpApiError(404, "unknown turn_id %s on thread %s" % (turn_id, thread_id), "not_found")
        r = core.interrupt(thread_id, turn_id)
        self.server.log.info("http /interrupt: thread=%s turn=%s status=%s" % (thread_id, turn_id, r.status))
        self._send_json(200, {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": r.status,
            "assistant_text": r.assistant_text,
            "error": r.error,
        })

    def _handle_list(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = (query.get("limit") or ["10"])[0]
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            raise HttpApiError(400, "query parameter limit must be an integer", "bad_request")
        limit = max(1, min(limit, 20))  # clamp to [1, 20]; schema: default 10, min 1, max 20
        core = self.server.core
        tl = core.list_threads(limit=limit)
        self.server.log.info(
            "http /threads: returned %d thread(s) (limit=%d)" % (len(tl.threads), limit)
        )
        self._send_json(200, {"threads": tl.threads})

    def _handle_read(self, thread_id):
        core = self.server.core
        try:
            thread = core.read_thread(thread_id, include_turns=True)
        except AppServerError as e:
            if "no thread" in str(e) or "not found" in str(e):
                raise HttpApiError(404, "unknown thread_id %s" % thread_id, "not_found")
            raise
        summary = self._summarize_thread(thread)
        self.server.log.info(
            "http /threads/%s: turns=%d" % (thread_id, len(summary["turns"]))
        )
        self._send_json(200, summary)

    @staticmethod
    def _summarize_thread(thread):
        status = thread.get("status")
        if isinstance(status, dict):
            status = status.get("type")
        turns = []
        for t in (thread.get("turns") or [])[:MAX_READ_TURNS]:
            texts = []
            for it in (t.get("items") or []):
                if it.get("type") == "agentMessage" and it.get("text"):
                    texts.append(it["text"])
            joined = "\n".join(texts).strip()
            if len(joined) > MAX_ASSISTANT_TEXT:
                joined = joined[:MAX_ASSISTANT_TEXT] + "\n...[truncated]"
            turns.append({
                "turn_id": t.get("id"),
                "status": t.get("status"),
                "started_at": t.get("startedAt"),
                "assistant_text": joined,
            })
        return {
            "thread_id": thread.get("id"),
            "cwd": thread.get("cwd"),
            "preview": thread.get("preview"),
            "status": status,
            "updated_at": thread.get("updatedAt"),
            "turns": turns,
        }


class BridgeHttpServer:
    """Owns one persistent app-server client + BridgeCore + HTTP listener."""

    def __init__(self, codex_bin, codex_home, api_key, host=DEFAULT_HOST, port=0,
                 config_overrides=None, logger=None):
        if not api_key:
            raise ValueError("BRIDGE_API_KEY (api_key) is required")
        self.log = logger or Logger()
        self.api_key = api_key
        self.host = host
        self.port = port
        self.client = CodexAppServerClient(
            codex_bin,
            codex_home,
            config_overrides if config_overrides is not None else CONFIG_OVERRIDES,
            logger=self.log,
        )
        self.core = BridgeCore(self.client)
        self.httpd = _BridgeHTTPServer((host, port), BridgeHttpHandler)
        self.httpd.core = self.core
        self.httpd.api_key = api_key
        self.httpd.log = self.log
        self.port = self.httpd.server_address[1]
        self._thread = None
        self._serving = False

    def start(self, init_timeout=60.0):
        self.client.start(timeout=init_timeout)
        self._serving = True
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True, name="bridge-http"
        )
        self._thread.start()
        self.log.info(
            "http server listening on http://%s:%d auth=enabled"
            % (self.host, self.port)
        )

    def stop(self):
        try:
            if self._serving:
                self.httpd.shutdown()
        finally:
            try:
                self.httpd.server_close()
            finally:
                self.client.close()
        self.log.info("http server stopped")


def main():
    import argparse

    try:
        default_port = int(os.environ.get("BRIDGE_PORT", str(DEFAULT_PORT)))
    except ValueError:
        print("error: BRIDGE_PORT must be an integer", file=os.sys.stderr)
        raise SystemExit(2)

    parser = argparse.ArgumentParser(description="Local Codex Bridge HTTP API")
    parser.add_argument("--host", default=os.environ.get("BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex-deepseek"),
    )
    parser.add_argument("--log", default="http_server.log")
    args = parser.parse_args()

    api_key = os.environ.get("BRIDGE_API_KEY", "")
    if not api_key:
        print("error: BRIDGE_API_KEY environment variable is required", file=os.sys.stderr)
        raise SystemExit(2)

    log = Logger(args.log, echo=True)
    server = BridgeHttpServer(
        args.codex_bin,
        args.codex_home,
        api_key,
        host=args.host,
        port=args.port,
        logger=log,
    )
    try:
        server.start()
        log.info("ready: http://%s:%d  (Ctrl-C to stop)" % (args.host, server.port))
        threading.Event().wait()
    except KeyboardInterrupt:
        log.info("shutting down")
    except Exception as e:
        log.info("startup failed: %r" % e)
        raise
    finally:
        server.stop()


if __name__ == "__main__":
    main()
