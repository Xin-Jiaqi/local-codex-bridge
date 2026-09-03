"""Dual-host routing for one GPT controlling the Mac bridge + a Windows bridge.

The Mac bridge keeps the existing public HTTP API byte-compatible. When
dual-host mode is enabled (BRIDGE_DUAL_HOST=true, see http_server/server.py),
this module provides:

- classify_cwd(): route /start by workspace path. Windows native paths
  (C:\\..., D:\\..., UNC \\\\server\\share including the WSL mounts
  \\\\wsl$\\<distro> and \\\\wsl.localhost\\<distro>) target the Windows
  machine; macOS POSIX paths (and "no cwd") stay on the Mac.
- ThreadTargetMap: persistent thread_id -> target ("mac" | "windows")
  mapping. Only thread ids and targets are stored (never prompts, never
  secrets); the file lives in the instance state dir / repo .runtime and is
  written atomically.
- WorkerBroker: the internal worker API side. The Windows machine never opens
  a public port and never takes the ngrok domain; its outbound worker
  long-polls POST /internal/worker/poll on the Mac router, receives bounded,
  allowlisted jobs, forwards them to the local Windows bridge on
  http://127.0.0.1:8321 and reports results back. The queue is bounded, every
  job expires, responses are size-capped and logs never contain prompts or
  secrets.
- PairingCodeStore: one-time, short-lived worker-token pairing codes. The
  Mac registers only the SHA-256 hash + expiry of each code; the Windows
  machine claims one over HTTPS and receives the existing worker token.
  Codes are never stored in plaintext and never logged.
- DualHostRouter: the facade used by the HTTP handlers: target resolution
  (mapping first, then a safe non-mutating probe of both ends when the
  mapping is missing) and remote dispatch with bounded waits.

Everything here is stdlib-only and runs on every host so the whole module can
be unit-tested offline (CI runs on Linux).
"""

import json
import calendar
import hashlib
import os
import tempfile
import threading
import time
import uuid

TARGET_MAC = "mac"
TARGET_WINDOWS = "windows"
VALID_TARGETS = (TARGET_MAC, TARGET_WINDOWS)

# Job kinds the Mac router may hand to a Windows worker. The worker maps each
# kind to exactly one fixed local-bridge endpoint; a router (or compromised
# bridge) can never point the worker at an arbitrary URL.
ALLOWED_JOB_KINDS = frozenset({
    "start", "continue", "observe", "steer", "interrupt", "read", "list",
})

# Bounds for the internal worker API (bounded queue / response / timeouts).
MAX_PENDING_JOBS = 64
JOB_TTL_S = 300.0            # a job nobody picks up expires
MAX_RESULTS = 128            # retained finished-job results (FIFO eviction)
MAX_RESULT_BODY_BYTES = 512 * 1024   # result bodies never exceed this
MAX_JOB_BODY_BYTES = 64 * 1024       # mirrors the public API body cap
DEFAULT_POLL_TIMEOUT_S = 15.0
MAX_POLL_TIMEOUT_S = 30.0
WORKER_STALE_S = 45.0        # no poll/result for this long => worker offline

# One-time worker-token pairing bounds (endpoint-level, see server.py).
MAX_PAIRING_CODES = 16
DEFAULT_PAIRING_TTL_S = 600
MAX_PAIRING_TTL_S = 900
MIN_PAIRING_CODE_LEN = 16
MAX_PAIRING_CODE_LEN = 200

# Bounded router-side waits for remote operations (seconds). Observe is
# special-cased: wait_ms/1000 + headroom, because the local bridge itself
# waits up to wait_ms before answering.
REMOTE_OP_WAIT_S = {
    "start": 90.0,
    "continue": 90.0,
    "steer": 60.0,
    "interrupt": 30.0,
    "read": 30.0,
    "probe": 15.0,
    "list": 10.0,
}
OBSERVE_HEADROOM_S = 20.0
MAX_MAP_ENTRIES = 4000

_WINDOWS_DRIVE_RE = None
_UNC_RE = None


def _compile_regexes():
    global _WINDOWS_DRIVE_RE, _UNC_RE
    if _WINDOWS_DRIVE_RE is None:
        import re
        _WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:($|[\\/])")
        _UNC_RE = re.compile(r"^(\\\\|//)")


def classify_cwd(cwd):
    """Route a /start workspace path to a host target.

    Returns TARGET_WINDOWS for Windows native paths: drive-absolute
    (C:\\foo, C:/foo, bare drive C:) and UNC paths starting with two
    backslashes (\\\\server\\share, \\\\wsl$\\Ubuntu, \\\\wsl.localhost\\...)
    or their forward-slash form (//server/share). Everything else - macOS
    POSIX paths, relative paths and a missing cwd - stays on the Mac
    (TARGET_MAC), keeping the pre-dual-host default unchanged.
    """
    _compile_regexes()
    if not isinstance(cwd, str) or not cwd.strip():
        return TARGET_MAC
    value = cwd.strip()
    if _WINDOWS_DRIVE_RE.match(value) or _UNC_RE.match(value):
        return TARGET_WINDOWS
    return TARGET_MAC


class ThreadTargetMap:
    """Persistent thread_id -> target mapping (JSON file, atomic writes).

    Thread-safe: concurrent /start, probe and list handlers may write while
    readers look up. The file contains only non-secret routing facts. A
    missing or corrupt file degrades to an empty map instead of failing the
    bridge (the map is an optimization + routing authority, never a secret).
    """

    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()
        self._entries = {}  # thread_id -> {"target": ..., "updated_at": iso}
        if path:
            self._load()

    # ------------------------------------------------------------------ io

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return  # first run or corrupt file: start empty, never crash
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return
        loaded = {}
        for thread_id, rec in entries.items():
            if (isinstance(thread_id, str) and isinstance(rec, dict)
                    and rec.get("target") in VALID_TARGETS):
                loaded[thread_id] = {
                    "target": rec["target"],
                    "updated_at": rec.get("updated_at") or _iso_now(),
                }
        with self._lock:
            self._entries = loaded

    def _persist(self):
        if not self.path:
            return
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            return
        try:
            fd, tmp = tempfile.mkstemp(prefix=".thread_map.", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(
                        {"version": 1, "entries": self._entries},
                        fh,
                        sort_keys=True,
                    )
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            pass  # routing still works in memory if the disk is unavailable

    # ------------------------------------------------------------------ api

    def get(self, thread_id):
        """Return "mac" | "windows" when mapped, else None."""
        with self._lock:
            rec = self._entries.get(thread_id)
        return rec["target"] if rec else None

    def set(self, thread_id, target):
        """Record thread_id -> target (idempotent, refreshes timestamp)."""
        if target not in VALID_TARGETS:
            raise ValueError("invalid target %r" % (target,))
        with self._lock:
            self._entries[thread_id] = {
                "target": target,
                "updated_at": _iso_now(),
            }
            self._prune_locked()
            self._persist()

    def _prune_locked(self):
        """Drop the oldest entries beyond MAX_MAP_ENTRIES."""
        if len(self._entries) <= MAX_MAP_ENTRIES:
            return
        ordered = sorted(
            self._entries.items(), key=lambda kv: kv[1].get("updated_at", "")
        )
        for thread_id, _ in ordered[: len(ordered) - MAX_MAP_ENTRIES]:
            del self._entries[thread_id]

    def __len__(self):
        with self._lock:
            return len(self._entries)


class PairingCodeStore:
    """One-time, short-lived worker-token pairing codes (single-use claim).

    Only the SHA-256 digest of each code is ever persisted, next to its
    expiry timestamp (one small json file per code; never the code itself and
    never the worker token). ``consume`` is atomic - the file is renamed, so
    exactly one concurrent claimant can ever win - which makes a code
    strictly single-use even when two Windows machines race for it. Expired
    or unknown codes are indistinguishable and simply fail the claim.

    The store lives in the instance state dir (next to the thread map), so a
    Mac bridge restart does not invalidate an already-minted code.
    """

    def __init__(self, directory, clock=None):
        self.directory = directory
        self._clock = clock or time.time

    def _path_for(self, code):
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        return os.path.join(self.directory, digest + ".json")

    def register(self, code, ttl_s=DEFAULT_PAIRING_TTL_S):
        """Atomically write (or refresh) one pairing code. False on invalid input."""
        if not isinstance(code, str) or not code.strip():
            return False
        code = code.strip()
        if not (MIN_PAIRING_CODE_LEN <= len(code) <= MAX_PAIRING_CODE_LEN):
            return False
        ttl_s = max(1, min(int(ttl_s), MAX_PAIRING_TTL_S))
        self._prune_expired()
        os.makedirs(self.directory, exist_ok=True)
        payload = json.dumps({"expires_at": self._clock() + ttl_s})
        fd, tmp_path = tempfile.mkstemp(prefix="pairing-", dir=self.directory)
        try:
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, self._path_for(code))
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        self._enforce_cap()
        return True

    def consume(self, code):
        """Atomically claim one code. True exactly once per registered code."""
        if not isinstance(code, str) or not code.strip():
            return False
        code = code.strip()
        if not (MIN_PAIRING_CODE_LEN <= len(code) <= MAX_PAIRING_CODE_LEN):
            return False
        path = self._path_for(code)
        if not os.path.isfile(path):
            return False
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            try:
                os.unlink(path)
            except OSError:
                pass
            return False
        expires_at = data.get("expires_at") if isinstance(data, dict) else None
        if not isinstance(expires_at, (int, float)) or self._clock() > expires_at:
            try:
                os.unlink(path)
            except OSError:
                pass
            return False
        claimed_path = path + ".claimed"
        try:
            os.rename(path, claimed_path)  # atomic: only one claimant wins
        except OSError:
            return False
        try:
            os.unlink(claimed_path)  # consumed; the winner cleans up
        except OSError:
            pass
        return True

    def _prune_expired(self):
        """Best-effort removal of expired codes and stale .claimed leftovers."""
        if not os.path.isdir(self.directory):
            return
        now = self._clock()
        for name in os.listdir(self.directory):
            path = os.path.join(self.directory, name)
            if not name.endswith((".json", ".claimed")):
                continue
            if not os.path.isfile(path):
                continue
            try:
                if name.endswith(".claimed"):
                    # the winner deletes it right after a successful claim,
                    # so any leftover is stale by construction
                    os.unlink(path)
                    continue
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if (not isinstance(data, dict)
                        or not isinstance(data.get("expires_at"), (int, float))
                        or now > data["expires_at"]):
                    os.unlink(path)
            except (OSError, ValueError):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _enforce_cap(self):
        """Keep at most MAX_PAIRING_CODES live codes (drop oldest first)."""
        if not os.path.isdir(self.directory):
            return
        self._prune_expired()
        live = sorted(
            (os.path.join(self.directory, name) for name in os.listdir(self.directory)
             if name.endswith(".json")),
            key=os.path.getmtime,
        )
        for path in live[:max(0, len(live) - MAX_PAIRING_CODES)]:
            try:
                os.unlink(path)
            except OSError:
                pass


class BrokerBusyError(Exception):
    """The worker job queue is full (bounded queue)."""


class JobExpiredError(Exception):
    """A job expired before a worker picked it up."""


class WorkerBroker:
    """Thread-safe job queue + result store for the internal worker API.

    The Windows worker long-polls ``poll()``; the Mac handler that needs an
    answer waits on ``wait_result()`` with its own bounded timeout. Results
    are retained briefly (FIFO-capped) so a result posted just after the
    caller timed out does not accumulate forever.
    """

    def __init__(self, max_pending=MAX_PENDING_JOBS, job_ttl_s=JOB_TTL_S,
                 poll_timeout_s=DEFAULT_POLL_TIMEOUT_S):
        self.max_pending = max_pending
        self.job_ttl_s = job_ttl_s
        self.poll_timeout_s = min(max(poll_timeout_s, 1.0), MAX_POLL_TIMEOUT_S)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending = []          # FIFO of job dicts not yet picked up
        self._results = {}          # job_id -> result dict (bounded)
        self._last_poll_at = None   # worker heartbeat: last poll/result time
        self._last_result_at = None

    # ------------------------------------------------------------------ jobs

    def enqueue(self, kind, params=None):
        """Add one allowlisted job. Returns the job dict or raises."""
        if kind not in ALLOWED_JOB_KINDS:
            raise ValueError("disallowed job kind %r" % (kind,))
        params = dict(params or {})
        try:
            params_bytes = len(json.dumps(params, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            raise ValueError("job params are not JSON-serializable")
        if params_bytes > MAX_JOB_BODY_BYTES:
            raise ValueError(
                "job params exceed the %d-byte cap" % MAX_JOB_BODY_BYTES
            )
        now = time.time()
        job = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "params": params,
            "created_at": _iso_now(now),
            "expires_at": _iso_now(now + self.job_ttl_s),
        }
        with self._cond:
            if len(self._pending) >= self.max_pending:
                raise BrokerBusyError(
                    "worker job queue is full (%d pending); retry later"
                    % self.max_pending
                )
            self._pending.append(job)
            self._cond.notify_all()
        return job

    def poll(self, timeout_s=None):
        """Long-poll for the next pending job (bounded wait).

        Returns a single job dict when one is available, else None after the
        timeout. Expired jobs are skipped (the enqueuer has already timed
        out). Updating the heartbeat here marks the worker as online.
        """
        if timeout_s is None:
            timeout_s = self.poll_timeout_s
        deadline = time.time() + min(max(timeout_s, 0.0), MAX_POLL_TIMEOUT_S)
        with self._cond:
            while True:
                now = time.time()
                self._last_poll_at = now
                while self._pending and self._expired_locked(self._pending[0], now):
                    self._pending.pop(0)
                if self._pending:
                    return self._pending.pop(0)
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def _expired_locked(self, job, now):
        try:
            expires = _parse_iso(job.get("expires_at"))
        except (TypeError, ValueError):
            return True
        return expires <= now

    # ------------------------------------------------------------------ results

    def submit_result(self, job_id, result):
        """Worker posts a finished job. Returns True when accepted."""
        if not isinstance(job_id, str) or not isinstance(result, dict):
            return False
        result = dict(result)
        try:
            envelope_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            result = {"status": 500, "body": None,
                      "error": "worker result is not JSON-serializable"}
            envelope_bytes = 0
        if envelope_bytes > MAX_RESULT_BODY_BYTES:
            # Keep the status/error, drop the oversized body: responses are
            # bounded end to end (worker already caps reads; this is the
            # router-side belt-and-suspenders check).
            result["body"] = None
            result["error"] = (result.get("error") or "") + " [body dropped: too large]"
        with self._cond:
            self._last_result_at = time.time()
            self._last_poll_at = self._last_result_at
            self._results[job_id] = result
            while len(self._results) > MAX_RESULTS:
                self._results.pop(next(iter(self._results)))
            self._cond.notify_all()
        return True

    def wait_result(self, job_id, timeout_s):
        """Wait for a job result with a bounded timeout.

        Returns the result dict or None on timeout / unknown job.
        """
        deadline = time.time() + max(timeout_s, 0.0)
        with self._cond:
            while True:
                result = self._results.pop(job_id, None)
                if result is not None:
                    return result
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    # ------------------------------------------------------------------ status

    def status(self):
        with self._cond:
            kinds = {}
            for job in self._pending:
                kinds[job.get("kind")] = kinds.get(job.get("kind"), 0) + 1
            return {
                "pending": len(self._pending),
                "pending_kinds": kinds,
                "last_poll_at": _iso_now(self._last_poll_at) if self._last_poll_at else None,
                "last_result_at": _iso_now(self._last_result_at) if self._last_result_at else None,
            }

    def worker_alive(self, stale_s=WORKER_STALE_S):
        """True when a worker polled or reported recently."""
        with self._cond:
            last = self._last_poll_at or self._last_result_at
        return bool(last) and (time.time() - last) <= stale_s


class RemoteError(Exception):
    """A Windows-routed request could not be fulfilled by the Mac router."""

    def __init__(self, status, error_type, message):
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message


class DualHostRouter:
    """Facade the HTTP handlers use for routing decisions and remote calls."""

    def __init__(self, log, map_path=None, enabled=False,
                 worker_token_present=False, poll_timeout_s=DEFAULT_POLL_TIMEOUT_S):
        self.log = log
        self.enabled = bool(enabled)
        self.worker_token_present = bool(worker_token_present)
        self.thread_map = ThreadTargetMap(map_path)
        self.broker = WorkerBroker(poll_timeout_s=poll_timeout_s)

    # ------------------------------------------------------------------ routing

    def record(self, thread_id, target):
        """Record an authoritative thread -> target fact."""
        if thread_id:
            self.thread_map.set(thread_id, target)

    def mapped(self, thread_id):
        """Mapped target for a thread, else None (probe required)."""
        return self.thread_map.get(thread_id) if thread_id else None

    def windows_available(self):
        """True when a worker is configured AND currently polling."""
        return bool(self.enabled and self.worker_token_present
                    and self.broker.worker_alive())

    def configured(self):
        """True when the worker API is enabled and a token is configured."""
        return bool(self.enabled and self.worker_token_present)

    # ------------------------------------------------------------------ remote

    def remote(self, kind, params=None, wait_s=None):
        """Dispatch one allowlisted job to Windows and wait for the result.

        Returns the worker result dict: {"status": int, "body": dict|None,
        "error": str|None}. Raises RemoteError for router-side failures
        (not configured / worker offline / queue full / job timeout).
        """
        if not self.enabled:
            raise RemoteError(
                503, "dual_host_disabled",
                "dual-host routing is disabled on this bridge (set "
                "BRIDGE_DUAL_HOST=true to let one GPT control Mac + Windows)",
            )
        if not self.worker_token_present:
            raise RemoteError(
                503, "windows_unavailable",
                "Windows worker API is not configured: set BRIDGE_WORKER_TOKEN "
                "on the Mac bridge and restart it",
            )
        if not self.broker.worker_alive():
            raise RemoteError(
                503, "windows_unreachable",
                "Windows worker is offline; start it on the Windows machine "
                "with scripts/windows/start_local_codex_bridge.ps1 "
                "(the Mac bridge keeps serving Mac requests normally)",
            )
        if wait_s is None:
            wait_s = REMOTE_OP_WAIT_S.get(kind, 30.0)
        try:
            job = self.broker.enqueue(kind, params)
        except BrokerBusyError as e:
            raise RemoteError(503, "worker_busy", str(e)) from e
        except ValueError as e:
            raise RemoteError(413, "job_too_large", str(e)) from e
        result = self.broker.wait_result(job["id"], wait_s)
        if result is None:
            raise RemoteError(
                504, "windows_timeout",
                "Windows worker did not answer within %.0fs (job %s); "
                "retry when the worker is healthy" % (wait_s, job["id"]),
            )
        self.log.info(
            "remote %s: job=%s worker_status=%s"
            % (kind, job["id"], result.get("status"))
        )
        return result


# ------------------------------------------------------------------ helpers

def _iso_now(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def _parse_iso(value):
    if not isinstance(value, str):
        raise ValueError("not an iso timestamp")
    return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
