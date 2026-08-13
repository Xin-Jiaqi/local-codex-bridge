"""High-level Local Codex Bridge API on top of CodexAppServerClient.

Provides thread/turn lifecycle helpers that are safe to build an MCP layer
on later:

- start(prompt, cwd)          -> (thread_id, turn_id)
- continue_thread(thread_id, prompt) -> turn_id  (same native thread, no history copy)
- observe(thread_id, turn_id, wait_ms) -> TurnResult (bounded, event-driven wait)
- interrupt(thread_id, turn_id)       -> TurnResult (final status)
"""

import json
import threading
import time

from .client import AppServerError
from .workspace_guard import (
    TaskCwdError,
    validate_maintenance_cwd,
    validate_task_cwd,
)

MODEL = "deepseek-chat"
MODEL_PROVIDER = "deepseek"
REASONING_EFFORT = "max"  # mirrors the user's zsh wrapper

_STATUS_NORMALIZED = {
    "inProgress": "running",
    "completed": "completed",
    "interrupted": "interrupted",
    "failed": "failed",
}


class TurnResult:
    """Outcome summary of a turn observation."""

    __slots__ = ("thread_id", "turn_id", "status", "assistant_text", "error")

    def __init__(self, thread_id, turn_id, status, assistant_text="", error=None):
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.status = status
        self.assistant_text = assistant_text or ""
        self.error = error

    @property
    def completed(self):
        return self.status == "completed"

    def as_dict(self):
        return {
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "status": self.status,
            "assistant_text": self.assistant_text,
            "error": self.error,
        }

    def __repr__(self):
        return "TurnResult(thread=%s turn=%s status=%s text=%r)" % (
            self.thread_id,
            self.turn_id,
            self.status,
            self.assistant_text[:80],
        )


class ThreadList:
    """Normalized result of list_threads()."""

    __slots__ = ("threads", "next_cursor", "backwards_cursor")

    def __init__(self, threads, next_cursor=None, backwards_cursor=None):
        self.threads = threads
        self.next_cursor = next_cursor
        self.backwards_cursor = backwards_cursor

    def __len__(self):
        return len(self.threads)

    def __iter__(self):
        return iter(self.threads)

    def __repr__(self):
        return "ThreadList(n=%d)" % len(self.threads)


class _TurnTracker:
    """Tracks turn lifecycle from notifications (event-driven, no polling)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._turns = {}  # (thread_id, turn_id) -> {"status", "final", "event"}
        self._deltas = {}  # (thread_id, turn_id, item_id) -> accumulated text
        self._agent_items = {}  # (thread_id, turn_id) -> final agentMessage item
        self._warnings = []  # [(ts, thread_id, message)]

    # ------------------------------------------------------------------ events

    def on_notification(self, method, params, raw):
        try:
            if method == "turn/completed":
                self._on_turn_completed(params)
            elif method == "item/agentMessage/delta":
                self._on_delta(params)
            elif method == "item/completed":
                self._on_item_completed(params)
            elif method == "warning":
                with self._lock:
                    self._warnings.append(
                        (time.time(), params.get("threadId"), params.get("message"))
                    )
        except Exception:
            pass  # never let a handler break the reader loop

    def _on_turn_completed(self, params):
        thread_id = params.get("threadId")
        turn = params.get("turn") or {}
        key = (thread_id, turn.get("id"))
        with self._lock:
            rec = self._turns.get(key)
            if rec is None:
                rec = {"status": "inProgress", "final": None, "event": threading.Event()}
                self._turns[key] = rec
            rec["status"] = turn.get("status") or rec["status"]
            rec["final"] = turn
            rec["event"].set()

    def _on_delta(self, params):
        key = (params.get("threadId"), params.get("turnId"), params.get("itemId"))
        with self._lock:
            self._deltas[key] = self._deltas.get(key, "") + (params.get("delta") or "")

    def _on_item_completed(self, params):
        item = params.get("item") or {}
        if item.get("type") == "agentMessage" and item.get("text"):
            key = (params.get("threadId"), params.get("turnId"))
            with self._lock:
                self._agent_items[key] = item

    # ------------------------------------------------------------------ queries

    def register(self, thread_id, turn_id):
        """Register a turn we are about to wait on (idempotent)."""
        key = (thread_id, turn_id)
        with self._lock:
            if key not in self._turns:
                self._turns[key] = {"status": "inProgress", "final": None, "event": threading.Event()}

    def is_registered(self, thread_id, turn_id):
        """True if this turn has been seen/registered (read-only)."""
        with self._lock:
            return (thread_id, turn_id) in self._turns

    def wait(self, thread_id, turn_id, timeout_s):
        """Wait for turn completion. Returns (status_or_None, final_turn_or_None)."""
        with self._lock:
            rec = self._turns.get((thread_id, turn_id))
        if rec is None:
            return None, None
        rec["event"].wait(timeout_s)
        with self._lock:
            return rec["status"], rec["final"]

    def summary(self, thread_id, turn_id, max_chars=500):
        """Best-effort assistant text for a turn, from authoritative payload first."""
        texts = []
        with self._lock:
            rec = self._turns.get((thread_id, turn_id))
            if rec and rec["final"]:
                for it in (rec["final"].get("items") or []):
                    if it.get("type") == "agentMessage" and it.get("text"):
                        texts.append(it["text"])
            if not texts:
                item = self._agent_items.get((thread_id, turn_id))
                if item and item.get("text"):
                    texts.append(item["text"])
            if not texts:
                prefix = (thread_id, turn_id)
                texts = [t for key, t in self._deltas.items() if key[:2] == prefix]
        joined = "\n".join(t for t in texts if t).strip()
        if len(joined) > max_chars:
            joined = joined[:max_chars] + "\n...[truncated]"
        return joined

    def warnings(self):
        with self._lock:
            return list(self._warnings)


class BridgeCore:
    """High-level bridge API. Holds one app-server connection."""

    def __init__(self, client, model=MODEL, model_provider=MODEL_PROVIDER,
                 max_summary_chars=500, thread_config=None, cwd_guard=None):
        self.client = client
        self.model = model
        self.model_provider = model_provider
        self.max_summary_chars = max_summary_chars
        self._thread_config = dict(thread_config) if thread_config else None
        self._cwd_guard = dict(cwd_guard) if cwd_guard else None
        self.tracker = _TurnTracker()
        client.on("*", self.tracker.on_notification)

    # ------------------------------------------------------------------ API

    def start(self, prompt, cwd=None):
        """Create a native Codex thread and start the first turn.

        When a cwd guard is configured (always, in the HTTP bridge), the cwd
        is canonicalized and validated against the bridge control plane
        BEFORE thread/start is sent; a rejected cwd raises TaskCwdError and
        no app-server request is made. The validator is selected by the
        guard's ``scope``: ``maintenance`` (host-admin window) accepts only
        the Bridge repo itself or a real subdirectory; local/hpc use the
        task guard unchanged.

        Returns (thread_id, turn_id).
        """
        if self._cwd_guard:
            guard = dict(self._cwd_guard)
            scope = guard.pop("scope", None)
            if scope == "maintenance":
                cwd = validate_maintenance_cwd(cwd, **guard)
            else:
                cwd = validate_task_cwd(cwd, **guard)
        params = {"cwd": cwd, "model": self.model, "modelProvider": self.model_provider}
        if self._thread_config:
            # Codex 0.147.0: named permission profiles are selected per-thread
            # via config.default_permissions. The legacy `sandbox` field must
            # stay absent: its enum cannot express a profile, and sending it
            # would force the legacy sandbox and disable the profile.
            params["config"] = dict(self._thread_config)
        res = self.client.request("thread/start", params, timeout=60)
        thread = res.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise AppServerError("thread/start returned no thread: %s" % json.dumps(res)[:300])
        self.client.log.info(
            "thread/start: threadId=%s model=%s modelProvider=%s"
            % (thread_id, res.get("model"), res.get("modelProvider"))
        )
        turn_id = self._start_turn(thread_id, prompt)
        return thread_id, turn_id

    def continue_thread(self, thread_id, prompt):
        """Continue on the SAME native thread: read state, then start a new turn.

        Never creates a new thread and never copies history.
        Returns the new turn_id.

        No permission context is re-sent: the thread already carries the
        profile selected at thread/start (ThreadResumeParams would accept the
        same `config`, but the bridge never resumes threads).
        """
        state = self.client.request("thread/read", {"threadId": thread_id, "includeTurns": False}, timeout=30)
        thread = state.get("thread") or {}
        if not thread.get("id"):
            raise AppServerError("thread/read: thread %s not found" % thread_id)
        self.client.log.info(
            "thread/read: threadId=%s (state read ok; continuing in place)"
            % thread_id
        )
        return self._start_turn(thread_id, prompt)

    def observe(self, thread_id, turn_id, wait_ms):
        """Bounded, event-driven wait on a turn.

        Returns immediately if the turn completes within wait_ms; otherwise
        returns with status "running". No busy polling.
        """
        t0 = time.monotonic()
        status, final = self.tracker.wait(thread_id, turn_id, wait_ms / 1000.0)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status = _STATUS_NORMALIZED.get(status, "running")
        summary = self.tracker.summary(thread_id, turn_id, self.max_summary_chars)
        error = None
        if final and final.get("error"):
            error = json.dumps(final["error"])[:200]
        self.client.log.info(
            "observe: thread=%s turn=%s status=%s elapsed_ms=%s"
            % (thread_id, turn_id, status, elapsed_ms)
        )
        return TurnResult(thread_id, turn_id, status, summary, error)

    def interrupt(self, thread_id, turn_id, wait_s=30.0):
        """Interrupt a running turn and return its final status."""
        try:
            self.client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=15)
            self.client.log.info("turn/interrupt sent: thread=%s turn=%s" % (thread_id, turn_id))
        except AppServerError as e:
            self.client.log.info("turn/interrupt error: %s" % e)
        status, final = self.tracker.wait(thread_id, turn_id, wait_s)
        status = _STATUS_NORMALIZED.get(status, "unknown")
        summary = self.tracker.summary(thread_id, turn_id, self.max_summary_chars)
        error = None
        if final and final.get("error"):
            error = json.dumps(final["error"])[:200]
        self.client.log.info(
            "interrupt: thread=%s turn=%s final_status=%s" % (thread_id, turn_id, status)
        )
        return TurnResult(thread_id, turn_id, status, summary, error)

    def steer(self, thread_id, turn_id, prompt):
        """Steer a RUNNING turn with a new instruction (native turn/steer).

        Acts on the same thread and the same active turn (precondition
        expectedTurnId); never creates a new thread or a new turn.
        Returns the (unchanged) turn_id once the steer request is accepted.
        """
        res = self.client.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": prompt}],
            },
            timeout=30,
        )
        accepted_turn_id = res.get("turnId")
        if accepted_turn_id and accepted_turn_id != turn_id:
            raise AppServerError(
                "turn/steer moved to a different turn %s (expected %s)"
                % (accepted_turn_id, turn_id)
            )
        self.tracker.register(thread_id, turn_id)
        self.client.log.info(
            "steer: thread=%s turn=%s accepted (same active turn)" % (thread_id, turn_id)
        )
        return turn_id

    def list_threads(self, limit=None, cursor=None, search_term=None, model_providers=None, **extra):
        """List native threads via thread/list (no local thread database).

        Each entry is a normalized dict with at least thread_id, cwd,
        preview/title, status and updated_at (when the protocol provides them).
        """
        params = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        if search_term is not None:
            params["searchTerm"] = search_term
        if model_providers is not None:
            params["modelProviders"] = model_providers
        params.update(extra)
        res = self.client.request("thread/list", params, timeout=30)
        threads = [self._normalize_thread(t) for t in (res.get("data") or [])]
        self.client.log.info("thread/list: returned %d thread(s)" % len(threads))
        return ThreadList(
            threads,
            next_cursor=res.get("nextCursor"),
            backwards_cursor=res.get("backwardsCursor"),
        )

    def read_thread(self, thread_id, include_turns=True):
        """Read native thread state via thread/read.

        With include_turns=True the returned thread contains its real turn
        history (items included).
        """
        res = self.client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": include_turns}, timeout=60
        )
        thread = res.get("thread") or {}
        if not thread.get("id"):
            raise AppServerError("thread/read returned no thread for %s" % thread_id)
        self.client.log.info(
            "thread/read: threadId=%s include_turns=%s turns=%d"
            % (thread_id, include_turns, len(thread.get("turns") or []))
        )
        return thread

    def _normalize_thread(self, thread):
        status = thread.get("status") or {}
        if isinstance(status, dict):
            status = status.get("type")
        return {
            "thread_id": thread.get("id"),
            "cwd": thread.get("cwd"),
            "preview": thread.get("preview"),
            "title": thread.get("name"),
            "status": status,
            "updated_at": thread.get("updatedAt"),
            "created_at": thread.get("createdAt"),
            "model_provider": thread.get("modelProvider"),
        }

    # ------------------------------------------------------------------ internals

    def _start_turn(self, thread_id, prompt):
        # turn/start must never carry `sandboxPolicy`: the schema has no
        # named-profile variant, and sending it would override the thread's
        # permission profile with a legacy policy.
        res = self.client.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
            timeout=60,
        )
        turn = res.get("turn") or {}
        turn_id = turn.get("id")
        if not turn_id:
            raise AppServerError("turn/start returned no turn: %s" % json.dumps(res)[:300])
        self.tracker.register(thread_id, turn_id)
        self.client.log.info(
            "turn/start: threadId=%s turnId=%s status=%s"
            % (thread_id, turn_id, turn.get("status"))
        )
        return turn_id
