#!/usr/bin/env python3
"""Integration test for Local Codex Bridge Core (real DeepSeek-backed app-server).

Scenarios:
  A. start(prompt) -> first assistant reply
  B. continue_thread -> same thread_id, recalls previous context (no history copy)
  C. observe(wait_ms) -> completes within bounded wait
  D. interrupt -> aborts a deliberately long turn with final status "interrupted"
  E. client robustness -> clean errors after app-server process exits

Single run, single log file (opened once, thread-safe), single final verdict.
"""

import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge import (
    AppServerError,
    AppServerProcessError,
    BridgeCore,
    CodexAppServerClient,
    Logger,
)

CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex-deepseek")
LOG_PATH = os.environ.get("BRIDGE_TEST_LOG", os.path.join(ROOT, "bridge_core_test.log"))
CONFIG_OVERRIDES = [
    'model="deepseek-chat"',
    'model_reasoning_effort="max"',
    'model_provider="deepseek"',
    # Workspace-local work runs without prompts; out-of-boundary operations
    # raise requestApproval (answered by the bridge client).
    'approval_policy="on-request"',
    'sandbox_mode="workspace-write"',
]

OBSERVE_TIMEOUT_MS = 120_000  # generous; short tasks complete in seconds

results = {}  # scenario -> (ok, detail)


def scenario(name, ok, detail):
    results[name] = (ok, detail)
    log.info("SCENARIO %s: %s - %s" % (name, "PASS" if ok else "FAIL", detail))


def main():
    global log
    log = Logger(LOG_PATH, echo=True)
    log.info("=== Local Codex Bridge Core integration test ===")
    log.info("pid=%s log=%s" % (os.getpid(), LOG_PATH))
    log.info("codex_bin=%s codex_home=%s" % (CODEX_BIN, CODEX_HOME))
    log.info("DEEPSEEK_API_KEY present: %s (len=%s)"
             % (bool(os.environ.get("DEEPSEEK_API_KEY")), len(os.environ.get("DEEPSEEK_API_KEY", ""))))

    client = CodexAppServerClient(CODEX_BIN, CODEX_HOME, CONFIG_OVERRIDES, logger=log)
    core = BridgeCore(client)
    ok_a = ok_b = ok_c = ok_d = ok_e = False
    try:
        client.start()

        # ---- A. start -> first reply
        try:
            tid_a, turn_a = core.start("Reply exactly: START_OK", cwd=ROOT)
            r = core.observe(tid_a, turn_a, OBSERVE_TIMEOUT_MS)
            ok_a = r.status == "completed" and "START_OK" in r.assistant_text
            scenario("A.start", ok_a, "thread=%s turn=%s status=%s text=%r"
                     % (tid_a, turn_a, r.status, r.assistant_text[:120]))
        except Exception as e:
            scenario("A.start", False, "exception: %r" % e)

        # ---- B. continue_thread: same thread, remembers context
        try:
            tid_b, turn_b1 = core.start(
                "Remember this secret token: ZEBRA-7788. Reply exactly: CONTEXT_SAVED", cwd=ROOT
            )
            r1 = core.observe(tid_b, turn_b1, OBSERVE_TIMEOUT_MS)
            turn_b2 = core.continue_thread(
                tid_b, "What secret token did I tell you earlier? Reply exactly with the token only."
            )
            r2 = core.observe(tid_b, turn_b2, OBSERVE_TIMEOUT_MS)
            state = core.client.request("thread/read", {"threadId": tid_b, "includeTurns": True}, timeout=30)
            turns_after = len((state.get("thread") or {}).get("turns") or [])
            ok_b = r1.status == "completed" and r2.status == "completed" \
                and "ZEBRA-7788" in r2.assistant_text and turns_after >= 2
            scenario("B.continue", ok_b,
                     "thread=%s turn1=%s status=%s | turn2=%s status=%s text2=%r thread_turns_after=%s"
                     % (tid_b, turn_b1, r1.status, turn_b2, r2.status, r2.assistant_text[:120], turns_after))
        except Exception as e:
            scenario("B.continue", False, "exception: %r" % e)

        # ---- C. observe bounded wait -> completed
        try:
            tid_c, turn_c = core.start("Reply exactly: OBSERVE_OK", cwd=ROOT)
            t0 = time.monotonic()
            r = core.observe(tid_c, turn_c, 30_000)
            elapsed = time.monotonic() - t0
            ok_c = r.status == "completed" and "OBSERVE_OK" in r.assistant_text and elapsed <= 35
            scenario("C.observe", ok_c,
                     "thread=%s turn=%s status=%s elapsed=%.1fs text=%r"
                     % (tid_c, turn_c, r.status, elapsed, r.assistant_text[:120]))
        except Exception as e:
            scenario("C.observe", False, "exception: %r" % e)

        # ---- D. interrupt a deliberately long turn
        try:
            tid_d, turn_d = core.start(
                "Write an extremely long, detailed essay of at least 5000 words about the "
                "history of timekeeping, section by section. Do not use tools or shell. "
                "Do not stop early.",
                cwd=ROOT,
            )
            time.sleep(4)  # let generation actually start before interrupting
            r = core.interrupt(tid_d, turn_d)
            ok_d = r.status == "interrupted"
            scenario("D.interrupt", ok_d,
                     "thread=%s turn=%s final_status=%s partial_text_len=%s"
                     % (tid_d, turn_d, r.status, len(r.assistant_text)))
        except Exception as e:
            scenario("D.interrupt", False, "exception: %r" % e)

        # ---- E. client robustness on process exit
        try:
            alive_before = client.alive
            client.close()
            dead_after = not client.alive
            try:
                client.request("initialize", {"clientInfo": {"name": "x", "version": "1"}}, timeout=2)
                raised = False
            except AppServerError:
                raised = True
            ok_e = alive_before and dead_after and raised
            scenario("E.process-exit", ok_e,
                     "alive_before=%s dead_after=%s request_after_close_raised=%s"
                     % (alive_before, dead_after, raised))
        except Exception as e:
            scenario("E.process-exit", False, "exception: %r" % e)

    except Exception as e:
        log.info("FATAL: %r" % e)
    finally:
        try:
            client.close()
        except Exception as e:
            log.info("close error: %r" % e)
        warnings = core.tracker.warnings()
        log.info("warnings recorded (not handled): %s" % len(warnings))
        for ts, tid, msg in warnings[:5]:
            log.info("  warning: %s" % msg)

        log.info("=== SUMMARY ===")
        for name in ("A.start", "B.continue", "C.observe", "D.interrupt", "E.process-exit"):
            ok, detail = results.get(name, (False, "not run"))
            log.info("  %s: %s" % (name, "PASS" if ok else "FAIL"))
        all_ok = all(ok for ok, _ in results.values()) and len(results) == 5
        log.info("RESULT: %s" % ("PASS" if all_ok else "FAIL"))
        log.close()
        return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
