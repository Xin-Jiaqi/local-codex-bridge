"""Outbound Windows worker: one GPT controls Mac + Windows through the router.

The Windows machine never opens a public port and never takes the ngrok
domain. This worker (Python stdlib only) connects OUTBOUND to the Mac
bridge's internal worker API:

    POST <Mac router URL>/internal/worker/poll     (long-poll, Bearer token)
    POST <Mac router URL>/internal/worker/result   (finished job)

Each job is allowlisted to exactly one fixed local-bridge endpoint and is
forwarded to the local Windows bridge on http://127.0.0.1:8321 (the same
HTTP API as the Mac bridge); the result is reported back to the router.

Safety properties (mirrored on the router side in bridge/dual_host.py):
- independent worker token (BRIDGE_WORKER_TOKEN / --worker-token), separate
  from the GPT-facing bridge API key; invalid tokens abort with a clear
  message instead of retrying forever
- allowlisted job kinds; the local request URL is derived from the kind, never
  taken from the router
- bounded local response bodies (a larger response becomes an error result)
- per-request and per-connection timeouts; transient failures retry with
  exponential backoff, a dead Mac router never crashes the worker
- logs contain only timestamps / job ids / kinds / status codes - never
  prompts, API keys or tokens

POSIX worker hosts (WSL): with WORKER_POSIX_MNT_MAP=1 a /start cwd such as
``D:\\work\\x`` is mapped to ``/mnt/d/work/x`` before the local bridge call,
so a Linux local bridge executes jobs the router addressed to the Windows
machine (router-side thread mapping is unchanged).

Run:  python -m bridge.worker --router-url https://<mac>.ngrok-free.dev
      (token / local key via env BRIDGE_WORKER_TOKEN / BRIDGE_API_KEY or args)
"""

import argparse
import json
import os
import random
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# kind -> (method, local path template). The worker never forwards a URL
# received from the router; the mapping below is the only forwarding table.
KIND_ENDPOINTS = {
    "start": ("POST", "/start"),
    "continue": ("POST", "/continue"),
    "observe": ("POST", "/observe"),
    "steer": ("POST", "/steer"),
    "interrupt": ("POST", "/interrupt"),
    "read": ("GET", "/threads/{thread_id}"),
    "list": ("GET", "/threads"),
}
ALLOWED_KINDS = frozenset(KIND_ENDPOINTS)

MAX_LOCAL_RESPONSE_BYTES = 512 * 1024
MAX_LOCAL_READ_EXTRA_S = 15.0
KIND_READ_TIMEOUT_S = {
    "start": 90.0,
    "continue": 90.0,
    "steer": 60.0,
    "interrupt": 30.0,
    "read": 60.0,
    "list": 30.0,
}
MAX_BACKOFF_S = 30.0

_WIN_DRIVE_RE = None


def _compile_win_drive_re():
    global _WIN_DRIVE_RE
    if _WIN_DRIVE_RE is None:
        import re as _re
        _WIN_DRIVE_RE = _re.compile(r"^([A-Za-z]):[\\/](.*)$", _re.S)


def map_posix_cwd(cwd):
    """Map a Windows drive-absolute cwd to /mnt/<drive>/... on POSIX hosts.

    Active only when WORKER_POSIX_MNT_MAP is truthy (1/true/yes); non-drive
    paths (UNC, bare drive, POSIX) pass through unchanged.
    """
    if os.environ.get("WORKER_POSIX_MNT_MAP", "").strip().lower() not in ("1", "true", "yes"):
        return cwd
    if not isinstance(cwd, str) or not cwd.strip():
        return cwd
    _compile_win_drive_re()
    m = _WIN_DRIVE_RE.match(cwd.strip())
    if not m:
        return cwd
    return "/mnt/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/"))


class WorkerConfigError(Exception):
    """Fatal, user-fixable configuration problem (bad token, old router)."""


def _log(msg):
    sys.stdout.write("[worker] %s\n" % msg)
    sys.stdout.flush()


def _iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_response(resp, max_bytes=MAX_LOCAL_RESPONSE_BYTES):
    """Read an HTTP response bounded to max_bytes.

    Returns (raw_bytes, truncated). The socket is intentionally not drained
    past the cap; urllib closes it on context exit, which aborts the upload
    on the (local) server side - safe here because the bridge API never
    depends on a complete request once the caller gives up.
    """
    chunk = resp.read(max_bytes + 1)
    if len(chunk) > max_bytes:
        return chunk[:max_bytes], True
    return chunk, False


def execute_job(job, local_base_url, local_api_key, log=_log):
    """Forward one allowlisted job to the local Windows bridge.

    Returns the router result envelope:
    {"status": int, "body": dict|None, "error": str|None, "completed_at": iso}.
    The envelope never contains prompt text, only the local bridge's own
    response/error shape (the local bridge already summarizes/truncates).
    """
    job_id = job.get("id")
    kind = job.get("kind")
    if kind not in ALLOWED_KINDS:
        return _error_result(
            job_id, 400, "disallowed job kind %r from router" % (kind,)
        )
    method, path_template = KIND_ENDPOINTS[kind]
    params = job.get("params") or {}
    if not isinstance(params, dict):
        return _error_result(job_id, 400, "job params must be a JSON object")
    if kind == "start" and isinstance(params.get("cwd"), str):
        mapped = map_posix_cwd(params["cwd"])
        if mapped != params["cwd"]:
            params = dict(params)
            params["cwd"] = mapped
            log("job %s: mapped local cwd for POSIX execution" % (job_id,))
    timeout = KIND_READ_TIMEOUT_S.get(kind, 30.0)
    if kind == "observe":
        try:
            timeout = min(max(int(params.get("wait_ms") or 0), 0), 10000) / 1000.0
        except (TypeError, ValueError):
            timeout = 10.0
        timeout += MAX_LOCAL_READ_EXTRA_S
    try:
        if kind == "read":
            thread_id = urllib.parse.quote(
                str(params.get("thread_id") or ""), safe=""
            )
            if not thread_id:
                return _error_result(job_id, 400, "missing thread_id")
            path = path_template.format(thread_id=thread_id)
        elif kind == "list":
            query = urllib.parse.urlencode(
                {"limit": max(1, min(int(params.get("limit") or 10), 20))}
            )
            path = path_template + "?" + query
        else:
            path = path_template
        body_bytes = None
        headers = {
            "Authorization": "Bearer " + (local_api_key or ""),
            "Content-Type": "application/json",
        }
        if method == "POST":
            body_bytes = json.dumps(params).encode("utf-8")
            if len(body_bytes) > MAX_LOCAL_RESPONSE_BYTES:
                return _error_result(job_id, 413, "job payload too large")
        req = urllib.request.Request(
            local_base_url.rstrip("/") + path,
            data=body_bytes,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, truncated = _read_response(resp)
        if truncated:
            return _error_result(
                job_id, 502,
                "local bridge response exceeded the %d-byte cap"
                % MAX_LOCAL_RESPONSE_BYTES,
            )
        text = raw.decode("utf-8", "replace").strip()
        body = None
        error = None
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = None
            error = "local bridge returned a non-JSON response (status %d)" % resp.status
        return {
            "status": resp.status,
            "body": body if isinstance(body, dict) else None,
            "error": error,
            "completed_at": _iso_now(),
        }
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(MAX_LOCAL_RESPONSE_BYTES + 1)
            body = json.loads(raw.decode("utf-8", "replace")) if raw else None
        except Exception:
            body = None
        return {
            "status": e.code,
            "body": body if isinstance(body, dict) else None,
            "error": None,
            "completed_at": _iso_now(),
        }
    except Exception as e:
        return _error_result(
            job_id, 502, "local bridge unreachable: %s" % _safe_error(e)
        )


def _error_result(job_id, status, message):
    return {
        "status": status,
        "body": None,
        "error": message,
        "completed_at": _iso_now(),
    }


def _safe_error(exc):
    """Short exception text that can never contain a request body/URL."""
    name = exc.__class__.__name__
    reason = getattr(exc, "reason", None)
    if isinstance(reason, Exception):
        return "%s: %s" % (name, reason)
    return name


def _http_json(base_url, path, payload=None, token=None, timeout_s=60.0):
    """POST JSON to the router and return (status, parsed_body_or_None)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode("utf-8") if payload is not None else b"{}"
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(MAX_LOCAL_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read(MAX_LOCAL_RESPONSE_BYTES + 1)
        except Exception:
            pass
        return e.code, _parse_body(raw)
    return 200, _parse_body(raw)


def _parse_body(raw):
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _poll_router(router_url, token, poll_timeout_s, connect_timeout_s):
    """One long-poll. Returns (jobs_list, fatal_error_or_None)."""
    timeout = poll_timeout_s + connect_timeout_s + 5.0
    try:
        status, body = _http_json(
            router_url,
            "/internal/worker/poll",
            {"timeout_s": poll_timeout_s},
            token=token,
            timeout_s=timeout,
        )
    except Exception as e:
        return None, None  # transient: caller backs off and retries
    if status == 200:
        jobs = body.get("jobs") if isinstance(body, dict) else None
        return (jobs if isinstance(jobs, list) else []), None
    if status in (401, 403):
        return None, WorkerConfigError(
            "router rejected the worker token (HTTP %d): make sure "
            "BRIDGE_WORKER_TOKEN matches the token configured on the Mac "
            "bridge" % status
        )
    err_body = body if isinstance(body, dict) else {}
    error = ((err_body.get("error") or {}).get("message")) or ""
    if status == 404:
        return None, WorkerConfigError(
            "the Mac bridge URL does not expose /internal/worker/poll "
            "(HTTP 404): the Mac bridge is not dual-host enabled or the URL "
            "is wrong"
        )
    if status == 503 and ("worker" in error or "disabled" in error.lower()):
        return None, WorkerConfigError(
            "Mac bridge worker API is disabled: %s" % error
        )
    return None, None  # 5xx / transient: back off and retry


def _write_state_file(path, router_url):
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".worker-state.", dir=directory or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "connected_at": _iso_now(),
                        "pid": os.getpid(),
                        "router_url": router_url,
                    },
                    fh,
                )
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass  # the state file is only a convenience for the bootstrap script


def run_worker_loop(router_url, token, local_base_url, local_api_key, *,
                    poll_timeout_s=15.0, connect_timeout_s=15.0,
                    state_file=None, log=_log, stop=None):
    """Run the poll -> forward -> report loop until stop is set or a fatal
    configuration error is raised. ``stop`` is a threading.Event (tests use
    it to end the loop)."""
    if stop is None:
        stop = threading.Event()
    failures = 0
    connected = False
    log("starting: router=%s local=%s (secrets never logged)"
        % (router_url, local_base_url))
    while not stop.is_set():
        try:
            jobs, fatal = _poll_router(
                router_url, token, poll_timeout_s, connect_timeout_s
            )
        except Exception as e:  # defensive: never let one poll kill the loop
            jobs, fatal = None, None
            log("poll error: %s" % _safe_error(e))
        if fatal is not None:
            raise fatal
        if jobs is None:
            failures += 1
            delay = min(MAX_BACKOFF_S, 2.0 * (2 ** min(failures - 1, 4)))
            delay += random.uniform(0, 1)
            if failures == 1 or failures % 10 == 0:
                log("router unreachable (attempt %d); retrying in %.0fs"
                    % (failures, delay))
            stop.wait(delay)
            continue
        if not connected:
            connected = True
            failures = 0
            _write_state_file(state_file, router_url)
            log("connected to router %s; worker ready" % router_url)
        failures = 0
        for job in jobs:
            job_id = job.get("id") if isinstance(job, dict) else None
            kind = job.get("kind") if isinstance(job, dict) else None
            if not job_id or kind not in ALLOWED_KINDS:
                continue  # never forward a malformed/unlisted job
            log("job %s kind=%s: forwarding to %s" % (job_id, kind, local_base_url))
            result = execute_job(job, local_base_url, local_api_key, log=log)
            log("job %s kind=%s: local status=%s"
                % (job_id, kind, result.get("status")))
            try:
                status, _ = _http_json(
                    router_url,
                    "/internal/worker/result",
                    {"job_id": job_id, "result": result},
                    token=token,
                    timeout_s=connect_timeout_s + 5.0,
                )
            except Exception as e:
                log("job %s: result report failed: %s" % (job_id, _safe_error(e)))
                continue
            if status != 200:
                log("job %s: result report returned HTTP %d" % (job_id, status))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Local Codex Bridge outbound Windows worker"
    )
    parser.add_argument(
        "--router-url",
        default=os.environ.get("MAC_BRIDGE_URL", ""),
        help="Mac router public URL (default: $env:MAC_BRIDGE_URL)",
    )
    parser.add_argument(
        "--worker-token",
        default=os.environ.get("BRIDGE_WORKER_TOKEN", ""),
        help="independent worker API token (default: $env:BRIDGE_WORKER_TOKEN)",
    )
    parser.add_argument(
        "--local-url",
        default=os.environ.get("LOCAL_BRIDGE_URL", "http://127.0.0.1:8321"),
        help="local Windows bridge URL (default 127.0.0.1:8321)",
    )
    parser.add_argument(
        "--local-api-key",
        default=os.environ.get("BRIDGE_API_KEY", ""),
        help="local bridge API key (default: $env:BRIDGE_API_KEY)",
    )
    parser.add_argument(
        "--local-api-key-file",
        default="",
        help="read the local bridge API key from this file",
    )
    parser.add_argument("--poll-timeout-s", type=float, default=15.0)
    parser.add_argument("--connect-timeout-s", type=float, default=15.0)
    parser.add_argument("--state-file", default="", help="first-connect marker")
    args = parser.parse_args(argv)

    router_url = (args.router_url or "").strip().rstrip("/")
    if not router_url:
        parser.error("--router-url is required (or set MAC_BRIDGE_URL)")
    token = (args.worker_token or "").strip()
    if not token:
        parser.error(
            "--worker-token is required (or set BRIDGE_WORKER_TOKEN); use the "
            "same token configured on the Mac bridge"
        )
    local_key = (args.local_api_key or "").strip()
    if not local_key and args.local_api_key_file:
        try:
            with open(args.local_api_key_file, "r", encoding="utf-8") as fh:
                local_key = fh.read().strip()
        except OSError as e:
            parser.error("cannot read --local-api-key-file: %s" % e)
    if not local_key:
        parser.error(
            "--local-api-key (or BRIDGE_API_KEY / --local-api-key-file) is "
            "required to call the local Windows bridge"
        )
    try:
        run_worker_loop(
            router_url,
            token,
            args.local_url,
            local_key,
            poll_timeout_s=max(1.0, min(args.poll_timeout_s, 30.0)),
            connect_timeout_s=max(1.0, args.connect_timeout_s),
            state_file=args.state_file or None,
        )
    except WorkerConfigError as e:
        print("error: %s" % e, file=sys.stderr)
        raise SystemExit(3) from e
    except KeyboardInterrupt:
        _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
