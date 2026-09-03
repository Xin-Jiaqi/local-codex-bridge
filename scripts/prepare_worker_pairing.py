#!/usr/bin/env python3
"""One-time Windows worker pairing for the dual-host router (run on the Mac).

Mints a short-lived, single-use pairing code and registers it on the RUNNING
Mac bridge (POST /internal/pairing/create against http://127.0.0.1:8321,
authenticated with the repo's .bridge_worker_token - the SAME token the
bridge exported at startup). The bridge stores only the SHA-256 hash + an
expiry timestamp and never prints the code or the token.

The helper prints the pairing code and the exact one-line PowerShell command
to run on the Windows machine (repo root). That command claims the token over
HTTPS from the fixed public router URL (single-use server-side), stores it
with Windows DPAPI and then starts the outbound worker automatically. The
long-lived worker token is NEVER printed by this helper or by the router.

Security notes:
- the pairing code is the only secret in the transcript, is single-use and
  expires (default 600s); mint a fresh code for each Windows machine;
- no plaintext code or token is ever written to disk on the Mac;
- never paste the code into a chat tool or save it; re-run this helper to
  mint another one.
"""

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge.dual_host import (  # noqa: E402  (path setup above)
    DEFAULT_PAIRING_TTL_S,
    MAX_PAIRING_TTL_S,
)

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8321"
DEFAULT_MAC_URL = "https://diploma-ideology-skier.ngrok-free.dev"


class PairingError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def default_mac_url(root=ROOT):
    """Public router URL: .ngrok_domain file when present, else the default."""
    domain_file = os.path.join(root, ".ngrok_domain")
    try:
        with open(domain_file, encoding="utf-8") as fh:
            domain = fh.read().strip()
        if domain:
            return "https://" + domain.lstrip("https://")
    except OSError:
        pass
    return DEFAULT_MAC_URL


def read_worker_token(path):
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as e:
        raise PairingError(
            "cannot read the worker token file %s (%s); create it on the Mac "
            "with:  openssl rand -hex 32 > .bridge_worker_token && chmod 600 "
            ".bridge_worker_token   then restart the bridge with "
            "./scripts/start_ngrok_bridge.sh" % (path, e)
        )
    if not token:
        raise PairingError("the worker token file %s is empty" % path)
    return token


def mint_code():
    """24-char URL-safe random code (~144 bits of entropy)."""
    return secrets.token_urlsafe(18)


def register_code(code, ttl_s, bridge_url, token):
    """Register hash+expiry on the running Mac bridge (worker-token auth)."""
    body = json.dumps({"code": code, "ttl_s": int(ttl_s)}).encode("utf-8")
    req = urllib.request.Request(
        bridge_url.rstrip("/") + "/internal/pairing/create",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            parsed = json.loads(e.read().decode("utf-8"))
            detail = parsed.get("error", {}).get("message", "")
        except (ValueError, UnicodeDecodeError):
            pass
        raise PairingError(
            "the Mac bridge rejected the pairing registration (HTTP %d)%s"
            % (e.code, (": " + detail) if detail else "")
            + "; is BRIDGE_DUAL_HOST=true with BRIDGE_WORKER_TOKEN set on the "
            "running bridge (restart ./scripts/start_ngrok_bridge.sh after "
            "writing .bridge_worker_token)?",
            status=e.code,
        ) from e
    except urllib.error.URLError as e:
        raise PairingError(
            "cannot reach the local Mac bridge at %s (%s); start it with "
            "./scripts/start_ngrok_bridge.sh first" % (bridge_url, e)
        ) from e


def build_windows_command(code, mac_url, default_mac_url_value=DEFAULT_MAC_URL):
    cmd = (
        "powershell -NoProfile -ExecutionPolicy Bypass -File "
        ".\\scripts\\windows\\start_local_codex_bridge.ps1 -PairCode %s"
        % code
    )
    if mac_url and mac_url.rstrip("/") != default_mac_url_value.rstrip("/"):
        cmd += ' -MacBridgeUrl "%s"' % mac_url
    return cmd


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--worker-token-file",
        default=os.path.join(ROOT, ".bridge_worker_token"),
        help="Mac router worker token file (default: <repo>/.bridge_worker_token)",
    )
    parser.add_argument(
        "--bridge-url",
        default=DEFAULT_BRIDGE_URL,
        help="local Mac bridge URL used to register the code (default: %s)"
        % DEFAULT_BRIDGE_URL,
    )
    parser.add_argument(
        "--mac-url",
        default=None,
        help="public Mac router URL the Windows command claims from "
        "(default: .ngrok_domain or the fixed router URL)",
    )
    parser.add_argument(
        "--ttl-s",
        type=int,
        default=DEFAULT_PAIRING_TTL_S,
        help="code lifetime in seconds (default %d, max %d)"
        % (DEFAULT_PAIRING_TTL_S, MAX_PAIRING_TTL_S),
    )
    args = parser.parse_args(argv)

    ttl_s = max(1, min(args.ttl_s, MAX_PAIRING_TTL_S))
    mac_url = (args.mac_url or default_mac_url()).rstrip("/")
    token = None
    try:
        token = read_worker_token(args.worker_token_file)
        code = mint_code()
        register_code(code, ttl_s, args.bridge_url, token)
    except PairingError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    finally:
        token = None  # never printed; drop it from memory as early as possible

    windows_command = build_windows_command(code, mac_url)
    print("[pairing] one-time worker pairing code minted for %s" % mac_url)
    print("[pairing] single-use, expires in %ds; the worker token is never printed" % ttl_s)
    print("[pairing] run this ONE line in a PowerShell at the Windows REPO ROOT:")
    print("")
    print("----- BEGIN PowerShell -----")
    print(windows_command)
    print("----- END PowerShell -----")
    print("")
    print("[pairing] the command claims the worker token over HTTPS (single-use), stores")
    print("[pairing] it with Windows DPAPI and starts the outbound worker. Codes expire;")
    print("[pairing] re-run this helper to mint a fresh one. Never paste the code into a")
    print("[pairing] chat tool - it is the only secret in the transcript.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
