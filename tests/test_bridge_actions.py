#!/usr/bin/env python3
"""Integration test for the complete 7-action Local Codex Bridge Core.

Actions under test (all against the real DeepSeek-backed app-server):
  A. start          - create native thread, first turn, first reply
  B. continue       - same thread, recalls previous context
  C. observe        - bounded, event-driven wait -> completed
  D. steer          - steer a RUNNING turn (same thread, same turn id)
  E. interrupt      - abort a deliberately long turn
  F. list           - thread/list finds the threads we created
  G. read           - thread/read returns real turn history

Note on steer semantics (verified against codex-rust-v0.147.0 source): turn/steer
does NOT preempt the in-flight model response. The steer input is queued on the
same active turn and is injected after the current response finishes, then the
model produces a follow-up answer. So the test steers a still-running turn and
verifies the SAME turn id finishes with the steer instruction reflected in the
final assistant text.

Single run, single log file, single final verdict.
"""

import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge import (
    BridgeCore,
    CodexAppServerClient,
    Logger,
)

CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex-deepseek")
LOG_PATH = os.environ.get("BRIDGE_TEST_LOG", os.path.join(ROOT, "bridge_actions_test.log"))
CONFIG_OVERRIDES = [
    'model="deepseek-chat"',
    'model_reasoning_effort="max"',
    'model_provider="deepseek"',
    # V1 security boundary: no approval prompts for workspace-local work;
    # anything outside the workspace is auto-denied.
    'approval_policy="never"',
    'sandbox_mode="workspace-write"',
]

OBSERVE_TIMEOUT_MS = 120_000
LONG_TASK = (
    "Write an extremely long, detailed essay of at least 5000 words about the "
    "history of timekeeping, section by section. Do not use tools or shell. "
    "Do not stop early."
)
STEER_TASK = (
    "Write a detailed 150-word essay about the history of timekeeping. "
    "Do not use tools or shell. Do not stop early."
)
STEER_PROMPT = "Stop the original task. Reply exactly: STEER_OK"
STEER_TIMEOUT_MS = 300_000

results = {}
tid_b = tid_c = None


def scenario(name, ok, detail):
    results[name] = (ok, detail)
    log.info("SCENARIO %s: %s - %s" % (name, "PASS" if ok else "FAIL", detail))


def main():
    global log
    log = Logger(LOG_PATH, echo=True)
    log.info("=== Local Codex Bridge Core 7-action integration test ===")
    log.info("pid=%s log=%s" % (os.getpid(), LOG_PATH))
    log.info("codex_bin=%s codex_home=%s" % (CODEX_BIN, CODEX_HOME))

    client = CodexAppServerClient(CODEX_BIN, CODEX_HOME, CONFIG_OVERRIDES, logger=log)
    core = BridgeCore(client)
    try:
        client.start()

        # ---- A. start
        try:
            tid, turn = core.start("Reply exactly: START_OK", cwd=ROOT)
            r = core.observe(tid, turn, OBSERVE_TIMEOUT_MS)
            ok = r.status == "completed" and "START_OK" in r.assistant_text
            scenario("A.start", ok, "thread=%s turn=%s status=%s text=%r"
                     % (tid, turn, r.status, r.assistant_text[:120]))
        except Exception as e:
            scenario("A.start", False, "exception: %r" % e)

        # ---- B. continue (own thread, 2 turns)
        try:
            tid_b, turn_b1 = core.start(
                "Remember this secret token: ZEBRA-7788. Reply exactly: CONTEXT_SAVED", cwd=ROOT
            )
            r1 = core.observe(tid_b, turn_b1, OBSERVE_TIMEOUT_MS)
            turn_b2 = core.continue_thread(
                tid_b, "What secret token did I tell you earlier? Reply exactly with the token only."
            )
            r2 = core.observe(tid_b, turn_b2, OBSERVE_TIMEOUT_MS)
            state = core.read_thread(tid_b, include_turns=True)
            turns_after = len(state.get("turns") or [])
            ok = r1.status == "completed" and r2.status == "completed" \
                and "ZEBRA-7788" in r2.assistant_text and turns_after >= 2
            scenario("B.continue", ok,
                     "thread=%s turn1=%s | turn2=%s status=%s text2=%r thread_turns_after=%s"
                     % (tid_b, turn_b1, turn_b2, r2.status, r2.assistant_text[:120], turns_after))
        except Exception as e:
            scenario("B.continue", False, "exception: %r" % e)

        # ---- C. observe bounded
        try:
            tid_c, turn_c = core.start("Reply exactly: OBSERVE_OK", cwd=ROOT)
            t0 = time.monotonic()
            r = core.observe(tid_c, turn_c, 30_000)
            elapsed = time.monotonic() - t0
            ok = r.status == "completed" and "OBSERVE_OK" in r.assistant_text and elapsed <= 35
            scenario("C.observe", ok,
                     "thread=%s turn=%s status=%s elapsed=%.1fs text=%r"
                     % (tid_c, turn_c, r.status, elapsed, r.assistant_text[:120]))
        except Exception as e:
            scenario("C.observe", False, "exception: %r" % e)

        # ---- D. steer a running turn
        try:
            tid_d, turn_d = core.start(STEER_TASK, cwd=ROOT)
            time.sleep(5)  # let the turn start generating
            pre = core.observe(tid_d, turn_d, 2_000)
            steer_ret = core.steer(tid_d, turn_d, STEER_PROMPT)
            r = core.observe(tid_d, steer_ret, STEER_TIMEOUT_MS)
            thread = core.read_thread(tid_d, include_turns=True)
            turns = thread.get("turns") or []
            last_turn_id = turns[-1].get("id") if turns else None
            steer_in_turn = any(
                STEER_PROMPT in json.dumps(it)
                for t in turns
                for it in (t.get("items") or [])
            )
            ok = (pre.status == "running"
                  and steer_ret == turn_d
                  and r.status == "completed"
                  and "STEER_OK" in r.assistant_text
                  and last_turn_id == turn_d
                  and steer_in_turn)
            scenario("D.steer", ok,
                     "thread=%s turn=%s pre_status=%s (unchanged=%s) final_status=%s "
                     "text=%r last_turn_id=%s steer_in_turn=%s"
                     % (tid_d, turn_d, pre.status, steer_ret == turn_d, r.status,
                        r.assistant_text[:120], last_turn_id, steer_in_turn))
        except Exception as e:
            scenario("D.steer", False, "exception: %r" % e)

        # ---- E. interrupt a long turn
        try:
            tid_e, turn_e = core.start(LONG_TASK, cwd=ROOT)
            time.sleep(4)
            r = core.interrupt(tid_e, turn_e)
            ok = r.status == "interrupted"
            scenario("E.interrupt", ok,
                     "thread=%s turn=%s final_status=%s"
                     % (tid_e, turn_e, r.status))
        except Exception as e:
            scenario("E.interrupt", False, "exception: %r" % e)

        # ---- F. list threads
        try:
            tl = core.list_threads(limit=200)
            ids = [t["thread_id"] for t in tl.threads]
            # verify the two threads created by scenarios B and C appear in the native list
            check_ids = [t for t in (tid_b, tid_c) if t]
            found = [tid for tid in check_ids if tid in ids]
            keys_ok = all(
                {"thread_id", "cwd", "preview", "title", "status", "updated_at"} <= set(t.keys())
                for t in tl.threads[:5]
            )
            ok = len(found) == 2 and keys_ok
            scenario("F.list", ok,
                     "listed=%d found=%s keys_present=%s sample=%s"
                     % (len(tl.threads), found, keys_ok,
                        {k: tl.threads[0][k] for k in ("thread_id", "cwd", "preview", "status", "updated_at")}
                        if tl.threads else None))
        except Exception as e:
            scenario("F.list", False, "exception: %r" % e)

        # ---- G. read thread history
        try:
            thread = core.read_thread(tid_b, include_turns=True)
            turns = thread.get("turns") or []
            texts = []
            for t in turns:
                for it in (t.get("items") or []):
                    if it.get("type") == "agentMessage" and it.get("text"):
                        texts.append(it["text"])
            ok = len(turns) >= 2 and any("CONTEXT_SAVED" in x for x in texts) \
                and any("ZEBRA-7788" in x for x in texts)
            scenario("G.read", ok,
                     "thread=%s turns=%d agent_texts=%r"
                     % (tid_b, len(turns), [x[:40] for x in texts]))
        except Exception as e:
            scenario("G.read", False, "exception: %r" % e)

    except Exception as e:
        log.info("FATAL: %r" % e)
    finally:
        try:
            client.close()
        except Exception as e:
            log.info("close error: %r" % e)

        log.info("=== SUMMARY (7 actions) ===")
        for name in ("A.start", "B.continue", "C.observe", "D.steer", "E.interrupt", "F.list", "G.read"):
            ok, detail = results.get(name, (False, "not run"))
            log.info("  %s: %s" % (name, "PASS" if ok else "FAIL"))
        all_ok = all(ok for ok, _ in results.values()) and len(results) == 7
        log.info("RESULT: %s" % ("PASS" if all_ok else "FAIL"))
        log.close()
        return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
