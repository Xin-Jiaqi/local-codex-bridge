#!/usr/bin/env python3
"""Offline tests for scripts/prepare_macos_cutover.py (no third-party deps).

Covers:
  - pure-Python AES-256/AES-128 against the published FIPS-197 KAT vectors;
  - AES-256-CBC + package container round-trip, wrong-key and tamper reject;
  - minimal ngrok authtoken extraction (top-level, agent:, quotes, CRLF);
  - bridge key file validation (64 hex, trailing-EOL trim);
  - generated PowerShell command contains the one-time key/URL/hash but never
    the bridge key value or the ngrok authtoken;
  - CLI secret hygiene: running with fixture secrets prints only hashes;
  - LAN server serves the package exactly once, then stops.
No real secret files are read here; everything uses /tmp fixtures.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "prepare_macos_cutover.py")

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import prepare_macos_cutover as cutover  # noqa: E402


FAKE_BRIDGE_KEY = "0123456789abcdef" * 4  # 64 hex chars, like openssl rand -hex 32
FAKE_AUTHTOKEN = "2fakeNgrokToken_doNotPrint_0123456789"


class AesKATTest(unittest.TestCase):
    def test_aes256_kat(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f"
                            "101112131415161718191a1b1c1d1e1f")
        pt = bytes.fromhex("00112233445566778899aabbccddeeff")
        ct = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")
        self.assertEqual(cutover.aes_ecb(key, pt), ct)
        self.assertEqual(cutover.aes_ecb(key, ct, decrypt=True), pt)

    def test_aes128_kat(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        pt = bytes.fromhex("00112233445566778899aabbccddeeff")
        ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(cutover.aes_ecb(key, pt), ct)
        self.assertEqual(cutover.aes_ecb(key, ct, decrypt=True), pt)


class PackageCryptoTest(unittest.TestCase):
    def test_cbc_roundtrip_various_sizes(self):
        key = bytes(range(32))
        iv = b"0123456789abcdef"
        for size in (0, 1, 15, 16, 17, 500):
            data = os.urandom(size)
            ct = cutover.aes256_cbc_encrypt(key, iv, data)
            self.assertEqual(cutover.aes256_cbc_decrypt(key, iv, ct), data)

    def test_package_roundtrip_and_key_validation(self):
        plaintext = b'{"bridge_api_key": "abc"}'
        key = bytes(range(32))
        pkg = cutover.encrypt_package(plaintext, key)
        self.assertTrue(pkg.startswith(cutover.MAGIC))
        self.assertEqual(cutover.decrypt_package(pkg, key), plaintext)
        with self.assertRaises(ValueError):
            cutover.encrypt_package(plaintext, b"short")

    def test_wrong_key_and_tamper_rejected(self):
        plaintext = b"secret payload bytes"
        key = bytes(range(32))
        pkg = bytearray(cutover.encrypt_package(plaintext, key))
        with self.assertRaises(ValueError):
            cutover.decrypt_package(bytes(pkg), bytes(reversed(range(32))))
        pkg[len(pkg) - 20] ^= 0x01  # flip a bit inside the ciphertext
        with self.assertRaises(ValueError):
            cutover.decrypt_package(bytes(pkg), key)
        pkg[-1] ^= 0x01  # flip a bit inside the HMAC tag
        with self.assertRaises(ValueError):
            cutover.decrypt_package(bytes(pkg), key)
        bad = bytearray(pkg)
        bad[0:5] = b"XXXXX"
        with self.assertRaises(ValueError):
            cutover.decrypt_package(bytes(bad), key)


class SourceExtractionTest(unittest.TestCase):
    def test_authtoken_top_level(self):
        text = 'version: "2"\nauthtoken: %s\nlog_level: info\n' % FAKE_AUTHTOKEN
        self.assertEqual(cutover.extract_ngrok_authtoken(text), FAKE_AUTHTOKEN)

    def test_authtoken_quoted_commented_crlf(self):
        text = ('version: "2"\r\n'
                'authtoken: "%s"   # copied by ngrok config add-authtoken\r\n'
                "agent:\r\n"
                "  authtoken: '%s'\r\n" % (FAKE_AUTHTOKEN, FAKE_AUTHTOKEN))
        self.assertEqual(cutover.extract_ngrok_authtoken(text), FAKE_AUTHTOKEN)

    def test_authtoken_missing_or_duplicate(self):
        with self.assertRaises(ValueError):
            cutover.extract_ngrok_authtoken('version: "2"\nlog: /tmp/x.log\n')
        dup = "authtoken: %s\nauthtoken: other-value-123\n" % FAKE_AUTHTOKEN
        with self.assertRaises(ValueError):
            cutover.extract_ngrok_authtoken(dup)

    def test_bridge_key_file_validation(self):
        d = tempfile.mkdtemp(prefix="cutover-test-")
        try:
            path = os.path.join(d, ".bridge_api_key")
            with open(path, "w") as fh:
                fh.write(FAKE_BRIDGE_KEY + "\n")
            self.assertEqual(cutover.read_bridge_key(path), FAKE_BRIDGE_KEY)
            with open(path, "w") as fh:
                fh.write("not-hex-short\n")
            with self.assertRaises(ValueError):
                cutover.read_bridge_key(path)
            with open(path, "w") as fh:
                fh.write("f" * 63 + "\n")
            with self.assertRaises(ValueError):
                cutover.read_bridge_key(path)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class PowershellRenderTest(unittest.TestCase):
    def test_render_contains_expected_pieces_only(self):
        key = bytes(range(32))
        key_b64 = base64.b64encode(key).decode("ascii")
        url = "http://192.168.1.23:45555/dl/" + "ab" * 16
        sha = cutover.sha256_hex(FAKE_BRIDGE_KEY)
        cmd = cutover.render_powershell_command(
            url, key_b64, sha, r"D:\work-of-jiaqi\actions-bridge\.bridge_api_key"
        )
        self.assertNotIn("__CUTOVER_", cmd)
        self.assertIn(key_b64, cmd)  # one-time transport key must be embedded
        self.assertIn(url, cmd)
        self.assertIn(sha, cmd)
        self.assertIn("Invoke-WebRequest", cmd)
        self.assertIn("ngrok config check", cmd)
        self.assertIn("JQBC1", cmd)
        self.assertNotIn(FAKE_BRIDGE_KEY, cmd)  # never the long-term key value
        self.assertNotIn(FAKE_AUTHTOKEN, cmd)  # never the ngrok authtoken

    def test_payload_json_shape(self):
        payload = cutover.build_plaintext(FAKE_BRIDGE_KEY, FAKE_AUTHTOKEN,
                                          domain="x.ngrok-free.dev")
        doc = json.loads(payload.decode("ascii"))
        self.assertEqual(doc["bridge_api_key"], FAKE_BRIDGE_KEY)
        self.assertEqual(doc["bridge_api_key_sha256"],
                         cutover.sha256_hex(FAKE_BRIDGE_KEY))
        self.assertEqual(doc["ngrok_authtoken"], FAKE_AUTHTOKEN)
        self.assertEqual(doc["ngrok_domain"], "x.ngrok-free.dev")


class ServerTest(unittest.TestCase):
    def test_serves_once_then_stops(self):
        payload = os.urandom(200)
        box = {}

        def ready(port, path):
            box["url"] = "http://127.0.0.1:%d%s" % (port, path)

        thread = threading.Thread(
            target=lambda: box.setdefault(
                "outcome", cutover.serve_once("127.0.0.1", payload, 30, ready)
            )
        )
        thread.start()
        deadline = time.time() + 10
        while "url" not in box and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn("url", box)
        self.assertEqual(urlopen(box["url"], timeout=5).read(), payload)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box.get("outcome"), "downloaded")
        with self.assertRaises(Exception):  # server already closed
            urlopen(box["url"], timeout=2)

    def test_expires_without_download(self):
        outcome = cutover.serve_once("127.0.0.1", b"x" * 16, 1, None)
        self.assertEqual(outcome, "expired")


class CliHygieneTest(unittest.TestCase):
    def test_no_secret_reaches_stdout_or_stderr(self):
        d = tempfile.mkdtemp(prefix="cutover-cli-")
        try:
            key_path = os.path.join(d, ".bridge_api_key")
            cfg_path = os.path.join(d, "ngrok.yml")
            with open(key_path, "w") as fh:
                fh.write(FAKE_BRIDGE_KEY + "\n")
            with open(cfg_path, "w") as fh:
                fh.write('version: "2"\nauthtoken: %s\n' % FAKE_AUTHTOKEN)
            proc = subprocess.run(
                [sys.executable, SCRIPT,
                 "--bridge-key-file", key_path,
                 "--ngrok-config", cfg_path,
                 "--domain-file", os.path.join(d, "no-such-domain-file"),
                 "--no-serve"],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            combined = proc.stdout + proc.stderr
            self.assertIn(cutover.sha256_hex(FAKE_BRIDGE_KEY), proc.stdout)
            self.assertNotIn(FAKE_BRIDGE_KEY, combined)
            self.assertNotIn(FAKE_AUTHTOKEN, combined)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
