"""Zero-dependency HTTP server exposing BridgeCore over JSON.

One persistent Codex app-server client is spawned at startup
(CODEX_HOME=~/.codex-deepseek, model=deepseek-chat,
model_provider=deepseek). All endpoints except GET /health require
`Authorization: Bearer <BRIDGE_API_KEY>`.

The app-server is always spawned with `approval_policy="on-request"` plus a
sandbox boundary selected by the BRIDGE_SANDBOX_MODE environment variable
(see build_config_overrides):

- workspace-write (default): the V1 boundary. Workspace-local read/write and
  shell run without prompts; anything outside the workspace is auto-denied.
  `.git` stays a protected read-only path (Codex 0.147.0), so git index/object
  writes are denied; network stays off unless BRIDGE_NETWORK_ACCESS=true.
- bridge-workspace: opt-in beta permission profile, fully injected via `-c`
  flags: `default_permissions="bridge-workspace"` +
  `[permissions.bridge-workspace]` (extends=":workspace" for baseline
  protections, `.git/` metadata writes, `.env` read-only, GitHub-only network
  allowlist). This mode NEVER injects `sandbox_mode` or
  `sandbox_workspace_write` — mixing legacy sandbox keys with
  `default_permissions` is not allowed: if any loaded config contains them,
  Codex falls back to the legacy sandbox and ignores the profile. The bridge
  refuses to start in this mode while the dedicated CODEX_HOME config still
  has legacy keys (see scripts/migrate_codex_home_permissions.py).
- danger-full-access: explicit opt-in, identical to running the direct CLI
  sandbox-free. Only for fully automated setups that accept the risk.

The pinned instance (BRIDGE_INSTANCE, default local) selects the mode and
the task-cwd scope: local/hpc use the task guard (explicit project dirs
outside the control plane); maintenance is a HOST-ADMIN window that only
accepts the Bridge repo itself or a real subdirectory as a task workspace.
/health reports the running instance, mode and port.

The bridge never forwards interactive approval requests to ChatGPT.
"""

import hmac
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge import AppServerError, BridgeCore, CodexAppServerClient, Logger
from bridge.core import MODEL, MODEL_PROVIDER, REASONING_EFFORT
from bridge.workspace_guard import TaskCwdError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321
MAX_OBSERVE_WAIT_MS = 10_000
MAX_ASSISTANT_TEXT = 4000
MAX_READ_TURNS = 20
MAX_BODY_BYTES = 64 * 1024

SANDBOX_MODE_WORKSPACE_WRITE = "workspace-write"
SANDBOX_MODE_BRIDGE_WORKSPACE = "bridge-workspace"
SANDBOX_MODE_DANGER_FULL_ACCESS = "danger-full-access"
VALID_SANDBOX_MODES = (
    SANDBOX_MODE_WORKSPACE_WRITE,
    SANDBOX_MODE_BRIDGE_WORKSPACE,
    SANDBOX_MODE_DANGER_FULL_ACCESS,
)
DEFAULT_SANDBOX_MODE = SANDBOX_MODE_WORKSPACE_WRITE
BRIDGE_PERMISSION_PROFILE = SANDBOX_MODE_BRIDGE_WORKSPACE
_TRUTHY = ("1", "true", "yes", "on")

# bridge-workspace profile, injected entirely via `-c` dotted TOML overrides
# so the mode needs no change to $CODEX_HOME/config.toml (apart from removing
# legacy sandbox keys — see _guard_bridge_workspace_home). Rules mirror
# config/bridge-workspace.example.toml; keep both in sync (tests assert it).
BRIDGE_WORKSPACE_EXTENDS_OVERRIDE = (
    'permissions.bridge-workspace.extends=":workspace"'
)
BRIDGE_WORKSPACE_FILESYSTEM_OVERRIDE = (
    'permissions.bridge-workspace.filesystem='
    '{ ":minimal" = "read", ":tmpdir" = "write", ":slash_tmp" = "write", '
    '":workspace_roots" = { "." = "write", ".git/" = "write", '
    '".git/hooks/" = "read", ".codex/" = "read", ".agents/" = "read", '
    '".env" = "read" } }'
)
BRIDGE_WORKSPACE_NETWORK_OVERRIDE = (
    'permissions.bridge-workspace.network='
    '{ enabled = true, domains = { "github.com" = "allow", '
    '"*.github.com" = "allow", "api.github.com" = "allow", '
    '"ssh.github.com" = "allow", "*.githubusercontent.com" = "allow", '
    '"objects.githubusercontent.com" = "allow", '
    '"raw.githubusercontent.com" = "allow" } }'
)

# Git proxy keys the bridge-workspace child env overrides to empty (direct
# GitHub connections; the profile must not depend on the user's local proxy).
GIT_PROXY_UNSET_KEYS = ("http.https://github.com.proxy", "http.proxy")
PROXY_ENV_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def build_config_overrides(env=None):
    """Build the `-c` overrides passed to the spawned `codex app-server`.

    BRIDGE_SANDBOX_MODE selects the sandbox boundary (see module docstring);
    BRIDGE_NETWORK_ACCESS=true additionally enables network in the default
    workspace-write mode via the documented `sandbox_workspace_write` table.
    Raises ValueError for unknown modes so a typo fails startup loudly instead
    of silently running with a weaker boundary.
    """
    env = os.environ if env is None else env
    mode = (env.get("BRIDGE_SANDBOX_MODE") or DEFAULT_SANDBOX_MODE).strip().lower()
    if mode not in VALID_SANDBOX_MODES:
        raise ValueError(
            "BRIDGE_SANDBOX_MODE=%r is invalid; use one of: %s"
            % (mode, ", ".join(VALID_SANDBOX_MODES))
        )
    # approval_policy defaults to "on-request" (sandbox-internal work runs
    # without prompts; out-of-boundary operations raise
    # item/commandExecution/requestApproval instead of being auto-denied).
    # The pinned instance config may select on-request or never via
    # BRIDGE_APPROVAL_POLICY; pinning it in the spawn flags prevents the
    # app-server from falling back to the config-file approval policy.
    approval = (env.get("BRIDGE_APPROVAL_POLICY") or "on-request").strip().lower()
    if approval not in ("on-request", "never"):
        raise ValueError(
            "BRIDGE_APPROVAL_POLICY=%r is invalid; use one of: on-request, never"
            % approval
        )
    overrides = [
        'model="%s"' % MODEL,
        'model_reasoning_effort="%s"' % REASONING_EFFORT,
        'model_provider="%s"' % MODEL_PROVIDER,
        'approval_policy="%s"' % approval,
    ]
    if mode == SANDBOX_MODE_WORKSPACE_WRITE:
        overrides.append('sandbox_mode="workspace-write"')
        if (env.get("BRIDGE_NETWORK_ACCESS") or "").strip().lower() in _TRUTHY:
            overrides.append("sandbox_workspace_write.network_access=true")
    elif mode == SANDBOX_MODE_BRIDGE_WORKSPACE:
        overrides.append('default_permissions="%s"' % BRIDGE_PERMISSION_PROFILE)
        overrides.append(BRIDGE_WORKSPACE_EXTENDS_OVERRIDE)
        overrides.append(BRIDGE_WORKSPACE_FILESYSTEM_OVERRIDE)
        overrides.append(BRIDGE_WORKSPACE_NETWORK_OVERRIDE)
    else:
        overrides.append('sandbox_mode="danger-full-access"')
    return overrides


def thread_start_permission_config(config_overrides):
    """Return the thread/start `config` object that explicitly selects the
    app-server permission profile, or None for legacy sandbox modes.

    Codex 0.147.0 app-server protocol: a named permission profile
    (e.g. bridge-workspace) is selected per-thread via
    `config.default_permissions` in thread/start params, and the legacy
    `sandbox` field must stay absent (its enum cannot express a profile and
    forces the legacy sandbox). Derived from the same overrides used for the
    app-server spawn so spawn flags and thread params can never drift.
    Legacy modes never inject default_permissions, so this returns None.
    """
    for override in config_overrides or ():
        if override.startswith('default_permissions="'):
            name = override.split('"', 2)[1]
            if name:
                return {"default_permissions": name}
    return None


def build_child_env(env=None, config_overrides=None):
    """Build the env for the spawned `codex app-server` child process.

    Only bridge-workspace sanitizes the child env: the profile must not depend
    on the user's local Git HTTP(S) proxy (e.g. a Clash-style
    http.https://github.com.proxy pointing at a loopback port), which is
    unreachable from inside the sandbox.
    The user's ~/.gitconfig is NOT modified; the proxy is overridden per-child
    via GIT_CONFIG_* env vars (empty value disables the proxy for libcurl).
    Proxy-related environment variables are dropped as well. Other modes keep
    the caller's env untouched (danger-full-access matches direct CLI).
    """
    env = dict(os.environ if env is None else env)
    overrides = list(config_overrides or [])
    if not any(o.startswith('default_permissions="bridge-workspace"') for o in overrides):
        return env
    for key in PROXY_ENV_VARS:
        env.pop(key, None)
    for key in [k for k in env if k.startswith("GIT_CONFIG_")]:
        del env[key]
    env["GIT_CONFIG_COUNT"] = str(len(GIT_PROXY_UNSET_KEYS))
    for i, key in enumerate(GIT_PROXY_UNSET_KEYS):
        env["GIT_CONFIG_KEY_%d" % i] = key
        env["GIT_CONFIG_VALUE_%d" % i] = ""
    return env


def find_legacy_sandbox_keys(config_text):
    """Return (top_level_keys, section_headers) that force the legacy sandbox.

    Any loaded `sandbox_mode` / `sandbox_workspace_write` config disables beta
    permission profiles: Codex uses the legacy sandbox and ignores
    `default_permissions`. Detects top-level keys (incl. dotted
    `sandbox_workspace_write.*`) and `[sandbox_workspace_write]` tables.
    """
    top_level_keys = []
    section_headers = []
    in_section = None
    for raw in config_text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            in_section = stripped[1:-1].strip()
            if in_section == "sandbox_workspace_write" or in_section.startswith("sandbox_workspace_write."):
                section_headers.append(stripped)
            continue
        if in_section:
            continue
        key = stripped.split("=", 1)[0].strip().strip('"')
        if key == "sandbox_mode" or key.startswith("sandbox_workspace_write"):
            top_level_keys.append(key)
    return top_level_keys, section_headers


def legacy_sandbox_keys_in_home(codex_home, workspace_root=None):
    """Scan the dedicated CODEX_HOME (and the project .codex override) for
    legacy sandbox keys. Returns {path: (keys, sections)} for dirty files."""
    paths = []
    home_config = os.path.join(codex_home, "config.toml")
    if os.path.isfile(home_config):
        paths.append(home_config)
    config_dir = os.path.join(codex_home, "config")
    if os.path.isdir(config_dir):
        paths.extend(
            os.path.join(config_dir, name)
            for name in sorted(os.listdir(config_dir))
            if name.endswith(".toml")
        )
    if workspace_root:
        project_config = os.path.join(workspace_root, ".codex", "config.toml")
        if os.path.isfile(project_config):
            paths.append(project_config)
    found = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            keys, sections = find_legacy_sandbox_keys(fh.read())
        if keys or sections:
            found[path] = (keys, sections)
    return found


# Default boundary (V1): workspace-write, no network, `.git` protected.
CONFIG_OVERRIDES = build_config_overrides({})


VALID_INSTANCES = ("local", "hpc", "maintenance")


def build_cwd_guard(env=None):
    """Build the task-cwd guard paths for the pinned instance (or legacy).

    Returns a dict for BridgeCore's cwd_guard: home and the bridge repo root
    are always protected; the instance state root is included only when
    BRIDGE_INSTANCE names a valid instance (local | hpc | maintenance);
    CODEX_HOME is the effective codex home (maintenance defaults to
    ~/.codex-deepseek-maintenance). The ``scope`` key selects the validator:
    ``maintenance`` restricts new-task cwds to the Bridge repo itself or a
    real subdirectory (host-admin maintenance window), while local/hpc keep
    the task guard unchanged. All paths are canonicalized by the validator.
    """
    env = os.environ if env is None else env
    home = env.get("HOME") or os.path.expanduser("~")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    state_root = None
    instance = (env.get("BRIDGE_INSTANCE") or "").strip().lower()
    if instance in VALID_INSTANCES:
        base = env.get("BRIDGE_STATE_ROOT") or os.path.join(
            env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"),
            "local-codex-bridge",
        )
        state_root = os.path.join(base, instance)
    codex_home = env.get("CODEX_HOME")
    if not codex_home:
        if instance == "maintenance":
            codex_home = os.path.join(home, ".codex-deepseek-maintenance")
        else:
            codex_home = os.path.join(home, ".codex-deepseek")
    return {
        "scope": "maintenance" if instance == "maintenance" else "task",
        "home": home,
        "repo_root": repo_root,
        "state_root": state_root,
        "codex_home": codex_home,
    }


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
        except TaskCwdError as e:
            self._error(400, e.reason, "invalid_cwd")
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
        except TaskCwdError as e:
            self._error(400, e.reason, "invalid_cwd")
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
            "instance": self.server.instance or None,
            "mode": self.server.mode or None,
            "port": self.server.port,
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
        if body.get("cwd") is not None:
            raise HttpApiError(
                400,
                "cwd is not accepted on /continue (the thread keeps the "
                "workspace validated at thread/start)",
                "invalid_cwd",
            )
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
                 config_overrides=None, child_env=None, logger=None,
                 instance=None, mode=None):
        if not api_key:
            raise ValueError("BRIDGE_API_KEY (api_key) is required")
        self.log = logger or Logger()
        self.api_key = api_key
        self.host = host
        self.port = port
        self.instance = instance
        self.mode = mode
        self.client = CodexAppServerClient(
            codex_bin,
            codex_home,
            config_overrides if config_overrides is not None else CONFIG_OVERRIDES,
            child_env=child_env,
            logger=self.log,
        )
        self.core = BridgeCore(
            self.client,
            thread_config=thread_start_permission_config(
                config_overrides if config_overrides is not None else CONFIG_OVERRIDES
            ),
            cwd_guard=build_cwd_guard(os.environ),
        )
        self.httpd = _BridgeHTTPServer((host, port), BridgeHttpHandler)
        self.httpd.core = self.core
        self.httpd.api_key = api_key
        self.httpd.log = self.log
        self.port = self.httpd.server_address[1]
        self.httpd.instance = instance
        self.httpd.mode = mode
        self.httpd.port = self.port
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

    try:
        config_overrides = build_config_overrides(os.environ)
    except ValueError as e:
        print("error: %s" % e, file=os.sys.stderr)
        raise SystemExit(2)
    if any(o.startswith('default_permissions="%s"' % BRIDGE_PERMISSION_PROFILE) for o in config_overrides):
        _guard_bridge_workspace_home(args.codex_home)
    child_env = build_child_env(os.environ, config_overrides)

    log = Logger(args.log, echo=True)
    instance = (os.environ.get("BRIDGE_INSTANCE") or "").strip().lower() or None
    if instance not in VALID_INSTANCES:
        instance = None
    mode = os.environ.get("BRIDGE_SANDBOX_MODE") or None
    server = BridgeHttpServer(
        args.codex_bin,
        args.codex_home,
        api_key,
        host=args.host,
        port=args.port,
        config_overrides=config_overrides,
        child_env=child_env,
        logger=log,
        instance=instance,
        mode=mode,
    )
    log.info(
        "sandbox mode: %s (BRIDGE_NETWORK_ACCESS=%s)"
        % (
            os.environ.get("BRIDGE_SANDBOX_MODE", DEFAULT_SANDBOX_MODE),
            os.environ.get("BRIDGE_NETWORK_ACCESS", "false"),
        )
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


def _guard_bridge_workspace_home(codex_home):
    """Fail startup when bridge-workspace is requested but any loaded config
    still carries legacy sandbox keys.

    Official rule: beta permission profiles cannot be mixed with the legacy
    `sandbox_mode` / `[sandbox_workspace_write]` mechanism — if any loaded
    config contains them, Codex uses the legacy sandbox and ignores
    `default_permissions`. bridge-workspace must be pure profile, so a dirty
    CODEX_HOME is a hard startup error (not a warning): silently falling back
    to the legacy sandbox would weaken the boundary without anyone noticing.
    """
    workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    found = legacy_sandbox_keys_in_home(codex_home, workspace_root=workspace_root)
    if not found:
        return
    print(
        "error: BRIDGE_SANDBOX_MODE=bridge-workspace requires a clean permission-profile config "
        "(legacy sandbox keys disable beta permission profiles and are never injected by the bridge)",
        file=os.sys.stderr,
    )
    for path, (keys, sections) in found.items():
        print(
            "error: legacy sandbox config in %s: %s"
            % (path, ", ".join(keys + sections)),
            file=os.sys.stderr,
        )
    print(
        "error: run ./scripts/migrate_codex_home_permissions.py --dry-run to inspect, "
        "--apply to migrate (backup created, no secrets touched), then restart the bridge",
        file=os.sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    main()
