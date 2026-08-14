#!/usr/bin/env python3
"""Real integration test for the local HTTP API over BridgeCore.

Covers: /health, bearer auth (401s), /start, /observe (completed),
/continue (same thread), /threads list, /threads/{id} read,
observe wait_ms clamp, /interrupt, error shapes, and openapi.yaml
basic validation.

Backend: real Codex app-server 0.147.0 with DeepSeek config.
Single run, single log file, single verdict.
"""

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import yaml  # only used for openapi.yaml basic validation

from bridge import Logger
from http_server import BridgeHttpServer

CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex-deepseek")
API_KEY = "test-bridge-key-0x"  # test-only key, never a real secret
LOG_PATH = os.path.join(ROOT, "http_api_test.log")
LONG_TASK = (
    "Write an extremely long, detailed essay of at least 5000 words about the "
    "history of timekeeping, section by section. Do not use tools or shell. "
    "Do not stop early."
)

results = {}


def scenario(name, ok, detail):
    results[name] = (ok, detail)
    log.info("SCENARIO %s: %s - %s" % (name, "PASS" if ok else "FAIL", detail))


def http(method, path, body=None, key=None, timeout=90):
    url = BASE + path
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
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except urllib.error.URLError as e:
        return 0, {"error": {"message": str(e)}}


def validate_openapi():
    spec = yaml.safe_load(open(os.path.join(ROOT, "openapi.yaml"), encoding="utf-8"))
    paths = spec.get("paths") or {}
    expected_paths = {
        "/health", "/ready", "/start", "/continue", "/observe", "/steer",
        "/interrupt", "/threads", "/threads/{thread_id}",
    }
    ops = {}
    for path, item in paths.items():
        for verb in ("get", "post"):
            if verb in item:
                ops[path] = item[verb].get("operationId")
    expected_ops = {
        "/health": "codexHealth",
        "/ready": "codexReady",
        "/start": "codexStart",
        "/continue": "codexContinue",
        "/observe": "codexObserve",
        "/steer": "codexSteer",
        "/interrupt": "codexInterrupt",
        "/threads": "codexList",
        "/threads/{thread_id}": "codexRead",
    }
    ok = True
    problems = []
    if spec.get("openapi") != "3.1.0":
        ok, problems = False, ["openapi != 3.1.0"]
    if set(paths) != expected_paths:
        ok = False
        problems.append("paths mismatch: %s" % sorted(set(paths) ^ expected_paths))
    if ops != expected_ops:
        ok = False
        problems.append("operationIds mismatch: %r" % ops)
    servers = spec.get("servers") or []
    if not servers or servers[0].get("url") != "https://REPLACE_WITH_PUBLIC_URL":
        ok = False
        problems.append("servers[0].url != https://REPLACE_WITH_PUBLIC_URL")
    auth = spec.get("components", {}).get("securitySchemes", {}).get("bearerAuth")
    if not auth or auth.get("type") != "http" or auth.get("scheme") != "bearer":
        ok = False
        problems.append("bearerAuth security scheme missing/malformed")
    if spec.get("security") != [{"bearerAuth": []}]:
        ok = False
        problems.append("global security missing")
    for probe in ("/health", "/ready"):
        if paths[probe].get("get", {}).get("security") != []:
            ok = False
            problems.append("%s security override missing" % probe)
    observe_schema = (
        paths["/observe"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    )
    if observe_schema["properties"]["wait_ms"].get("maximum") != 10000:
        ok = False
        problems.append("observe wait_ms maximum != 10000")
    if "codexSteer" in ops.values():
        desc = paths["/steer"]["post"].get("description") or ""
        if "interrupt" not in desc.lower():
            ok = False
            problems.append("steer description missing interrupt+continue guidance")
    list_params = {
        param["name"]: param.get("schema")
        for param in paths["/threads"]["get"].get("parameters") or []
    }
    limit_schema = list_params.get("limit")
    if limit_schema != {"type": "integer", "default": 10, "minimum": 1, "maximum": 20}:
        ok = False
        problems.append("/threads limit param schema wrong: %r" % limit_schema)
    if spec.get("info", {}).get("version") != "1.1.0":
        ok = False
        problems.append("info.version != 1.1.0")
    # Deployment copies (openapi.ngrok.yaml / openapi.public.yaml) are
    # optional and gitignored: they exist only on machines that generated
    # them with a real tunnel URL. The tracked template is openapi.yaml.
    for name in ("openapi.ngrok.yaml", "openapi.public.yaml"):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        copy = yaml.safe_load(open(path, encoding="utf-8"))
        if copy.get("paths", {}).get("/threads", {}).get("get", {}).get("parameters") \
                != paths["/threads"]["get"]["parameters"]:
            ok = False
            problems.append("%s /threads limit param differs" % name)
        copy_url = ((copy.get("servers") or [{}])[0]).get("url")
        if copy_url in (None, "https://REPLACE_WITH_PUBLIC_URL"):
            problems.append("%s still has placeholder URL (fill in a real URL before use)" % name)
    return ok, problems


def main():
    global log, BASE
    log = Logger(LOG_PATH, echo=True)
    log.info("=== Local Codex Bridge HTTP API integration test ===")
    log.info("pid=%s log=%s codex_bin=%s codex_home=%s" % (os.getpid(), LOG_PATH, CODEX_BIN, CODEX_HOME))

    server = BridgeHttpServer(CODEX_BIN, CODEX_HOME, API_KEY, host="127.0.0.1", port=0, logger=log)
    try:
        server.start()
        BASE = "http://127.0.0.1:%d" % server.port
        log.info("http base=%s (api_key configured)" % BASE)

        # ---- 1. health + /ready: real readiness gate (no auth)
        try:
            st, body = http("GET", "/health")
            ok = st == 200 and body.get("status") == "ok" \
                and body.get("ready") is True \
                and body.get("app_server_alive") is True \
                and body.get("provider_secret") is True \
                and body.get("provider_config_ok") is True \
                and body.get("model") == "deepseek-chat" \
                and body.get("model_provider") == "deepseek"
            scenario("health", ok, "status=%s body=%r" % (st, body))
        except Exception as e:
            scenario("health", False, "exception: %r" % e)

        # ---- 1b. /ready (same non-sensitive readiness payload, no auth)
        try:
            st_r, body_r = http("GET", "/ready")
            ok_r = st_r == 200 and body_r.get("ready") is True \
                and body_r.get("status") == "ok" \
                and body_r.get("provider_secret") is True
            scenario("ready", ok_r, "status=%s body=%r" % (st_r, body_r))
        except Exception as e:
            scenario("ready", False, "exception: %r" % e)

        # ---- 2. bearer auth
        try:
            st_no, _ = http("GET", "/threads")
            st_wrong, _ = http("GET", "/threads", key="wrong-key")
            st_start_unauth, _ = http("POST", "/start", {"prompt": "x"})
            st_ok, body = http("GET", "/threads", key=API_KEY)
            ok = st_no == 401 and st_wrong == 401 and st_start_unauth == 401 \
                and st_ok == 200 and "threads" in body
            scenario("auth", ok,
                     "no_key=%s wrong_key=%s start_no_key=%s ok_key=%s"
                     % (st_no, st_wrong, st_start_unauth, st_ok))
        except Exception as e:
            scenario("auth", False, "exception: %r" % e)

        # ---- 3. start + 4. observe -> completed (DeepSeek reply)
        tid_a = turn_a = None
        try:
            st, body = http("POST", "/start", {
                "prompt": "Remember this secret token: HTTP-ZEBRA-77. Reply exactly: HTTP_SAVED",
                "cwd": ROOT,
            }, key=API_KEY)
            tid_a, turn_a = body.get("thread_id"), body.get("turn_id")
            ok_start = st == 200 and tid_a and turn_a and body.get("status") == "started"
            st_o, ob = http("POST", "/observe", {
                "thread_id": tid_a, "turn_id": turn_a, "wait_ms": 10000,
            }, key=API_KEY)
            ok_observe = st_o == 200 and ob.get("status") == "completed" \
                and "HTTP_SAVED" in (ob.get("assistant_text") or "")
            scenario("start", ok_start, "status=%s thread=%s turn=%s" % (st, tid_a, turn_a))
            scenario("observe", ok_observe,
                     "status=%s turn_status=%s text=%r"
                     % (st_o, ob.get("status"), (ob.get("assistant_text") or "")[:80]))
        except Exception as e:
            scenario("start", False, "exception: %r" % e)
            scenario("observe", False, "exception: %r" % e)

        # ---- 5. continue -> same thread, recalls context
        tid_b = turn_b = None
        try:
            st, body = http("POST", "/continue", {
                "thread_id": tid_a,
                "prompt": "What secret token did I tell you earlier? Reply exactly with the token only.",
            }, key=API_KEY)
            tid_b, turn_b = body.get("thread_id"), body.get("turn_id")
            ok_cont = st == 200 and tid_b == tid_a and turn_b and turn_b != turn_a
            st_o, ob = http("POST", "/observe", {
                "thread_id": tid_a, "turn_id": turn_b, "wait_ms": 10000,
            }, key=API_KEY)
            ok_observe2 = st_o == 200 and ob.get("status") == "completed" \
                and "HTTP-ZEBRA-77" in (ob.get("assistant_text") or "")
            scenario("continue", ok_cont and ok_observe2,
                     "same_thread=%s turn_old=%s turn_new=%s final_status=%s text=%r"
                     % (tid_b == tid_a, turn_a, turn_b, ob.get("status"),
                        (ob.get("assistant_text") or "")[:80]))
        except Exception as e:
            scenario("continue", False, "exception: %r" % e)

        # ---- 6. list
        try:
            st, body = http("GET", "/threads", key=API_KEY)
            ids = [t.get("thread_id") for t in (body.get("threads") or [])]
            found = [t for t in (tid_a, tid_b) if t and t in ids]
            keys_ok = all(
                {"thread_id", "cwd", "preview", "status", "updated_at"} <= set(t.keys())
                for t in (body.get("threads") or [])[:5]
            )
            ok = st == 200 and len(found) >= 1 and keys_ok
            scenario("list", ok,
                     "status=%s listed=%d found=%s keys_ok=%s"
                     % (st, len(body.get("threads") or []), found, keys_ok))
        except Exception as e:
            scenario("list", False, "exception: %r" % e)

        # ---- 6b. /threads limit
        try:
            st, body = http("GET", "/threads", key=API_KEY)
            n_default = len(body.get("threads") or [])
            st3, b3 = http("GET", "/threads?limit=3", key=API_KEY)
            n3 = len(b3.get("threads") or [])
            st50, b50 = http("GET", "/threads?limit=50", key=API_KEY)
            n50 = len(b50.get("threads") or [])
            stbad, bbad = http("GET", "/threads?limit=abc", key=API_KEY)
            ok = st == 200 and n_default <= 10 \
                and st3 == 200 and 1 <= n3 <= 3 \
                and st50 == 200 and n50 <= 20 \
                and stbad == 400 and "error" in bbad
            scenario("list_limit", ok,
                     "default=%d(<=10) limit3=%d(<=3) limit50_clamped=%d(<=20) bad=%s"
                     % (n_default, n3, n50, stbad))
        except Exception as e:
            scenario("list_limit", False, "exception: %r" % e)

        # ---- 7. read
        try:
            st, body = http("GET", "/threads/%s" % tid_a, key=API_KEY)
            turns = body.get("turns") or []
            texts = [t.get("assistant_text") or "" for t in turns]
            ok = st == 200 and body.get("thread_id") == tid_a and len(turns) >= 2 \
                and any("HTTP_SAVED" in x for x in texts) \
                and any("HTTP-ZEBRA-77" in x for x in texts)
            scenario("read", ok,
                     "status=%s turns=%d last_text=%r"
                     % (st, len(turns), (texts[-1] if texts else "")[:80]))
        except Exception as e:
            scenario("read", False, "exception: %r" % e)

        # ---- 8. observe clamp (wait_ms > 10000 is capped) + 9. interrupt
        tid_long = turn_long = None
        try:
            st, body = http("POST", "/start", {"prompt": LONG_TASK, "cwd": ROOT}, key=API_KEY)
            tid_long, turn_long = body.get("thread_id"), body.get("turn_id")
            t0 = time.monotonic()
            st_o, ob = http("POST", "/observe", {
                "thread_id": tid_long, "turn_id": turn_long, "wait_ms": 60000,
            }, key=API_KEY)
            elapsed = time.monotonic() - t0
            ok_clamp = st_o == 200 and ob.get("status") == "running" \
                and elapsed < 15 and elapsed >= 5
            scenario("observe_clamp", ok_clamp,
                     "requested=60000 elapsed=%.1fs status=%s (clamped to 10000)"
                     % (elapsed, ob.get("status")))
            st_i, ib = http("POST", "/interrupt", {
                "thread_id": tid_long, "turn_id": turn_long,
            }, key=API_KEY)
            ok_interrupt = st_i == 200 and ib.get("status") == "interrupted"
            scenario("interrupt", ok_interrupt,
                     "status=%s final_status=%s" % (st_i, ib.get("status")))
        except Exception as e:
            scenario("observe_clamp", False, "exception: %r" % e)
            scenario("interrupt", False, "exception: %r" % e)

        # ---- 10. error shapes + unknown turn
        try:
            st_bad, b1 = http("POST", "/start", {}, key=API_KEY)
            st_unknown, b2 = http("POST", "/observe", {
                "thread_id": tid_a or "x", "turn_id": "does-not-exist", "wait_ms": 100,
            }, key=API_KEY)
            st_badjson, b3 = http("POST", "/start", "not-json", key=API_KEY)
            ok = st_bad == 400 and "error" in b1 and st_unknown == 404 and "error" in b2 \
                and st_badjson == 400
            scenario("errors", ok,
                     "missing_field=%s unknown_turn=%s bad_json=%s"
                     % (st_bad, st_unknown, st_badjson))
        except Exception as e:
            scenario("errors", False, "exception: %r" % e)

        # ---- 11. openapi.yaml basic validation
        try:
            ok, problems = validate_openapi()
            scenario("openapi", ok, "problems=%r" % problems)
        except Exception as e:
            scenario("openapi", False, "exception: %r" % e)

    except Exception as e:
        log.info("FATAL: %r" % e)
    finally:
        try:
            server.stop()
        except Exception as e:
            log.info("stop error: %r" % e)
        log.info("=== SUMMARY ===")
        for name in ("health", "ready", "auth", "start", "observe", "continue",
                     "list", "list_limit", "read", "observe_clamp", "interrupt",
                     "errors", "openapi"):
            ok, detail = results.get(name, (False, "not run"))
            log.info("  %s: %s" % (name, "PASS" if ok else "FAIL"))
        all_ok = all(ok for ok, _ in results.values()) and len(results) == 13
        log.info("RESULT: %s" % ("PASS" if all_ok else "FAIL"))
        log.close()
        return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
