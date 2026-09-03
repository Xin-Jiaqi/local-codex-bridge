#!/usr/bin/env python3
r"""Mac -> Windows one-time bridge cutover package helper (stdlib only).

Builds a short-lived, encrypted migration package from two local sources:
  * <repo root>/.bridge_api_key            (bridge bearer key, 64 hex chars)
  * the ngrok user config authtoken         (same-account fixed-domain takeover)

The package is encrypted in memory with a random one-time AES-256 key and is
served over the LAN by a tiny HTTP server bound to the primary LAN IPv4
address only, on a random high port, for at most 600 seconds and exactly one
download. The one-time transport key is printed once (inside the generated
PowerShell command) so the user can copy it to Windows; it is worthless after
this run. No secret value is ever written to stdout/stderr or to disk in
plaintext, and the encrypted package file is removed when the run finishes.

The printed PowerShell command downloads the package, verifies its
HMAC-SHA256 tag, decrypts it with .NET AES-256-CBC (built into Windows
PowerShell 5.1+, no third-party code), atomically installs:
  * D:\work-of-jiaqi\actions-bridge\.bridge_api_key  (64 hex, no trailing EOL)
  * %USERPROFILE%\.config\ngrok\ngrok.yml           (authtoken only)
verifies (length + SHA256 against the Mac key, hash only) and 'ngrok config
check', then deletes every temporary file. Mac ngrok is intentionally NOT
touched: the operator stops it only after Windows took over the fixed domain.

Crypto notes: AES-256 in CBC mode with PKCS#7 padding and a random IV, plus an
HMAC-SHA256 tag over magic||iv||ciphertext (encrypt-then-MAC). The AES core is
a compact, pure-Python FIPS-197 implementation (verified with the published
AES-256 known-answer vector in tests) so no third-party dependency is needed
on either side; PowerShell decrypts with the .NET AES provider.

Usage:
  python3 scripts/prepare_macos_cutover.py
  python3 scripts/prepare_macos_cutover.py --bridge-key-file /path/.bridge_api_key
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


# --------------------------------------------------------------------------- #
# Container format (shared with the PowerShell side):
#   magic(5) | iv(16) | AES-256-CBC ciphertext (PKCS#7, len % 16 == 0) |
#   HMAC-SHA256 tag(32)  over magic||iv||ciphertext
# --------------------------------------------------------------------------- #
MAGIC = b"JQBC1"
TAG_PREFIX = b"JQB-TAG-v1:"
MAGIC_LEN = len(MAGIC)
IV_LEN = 16
TAG_LEN = 32
HEADER_LEN = MAGIC_LEN + IV_LEN  # 21
KEY_BYTES = 32


# --------------------------------------------------------------------------- #
# Pure-Python AES (FIPS-197).  Tested against the published KAT vectors.
# --------------------------------------------------------------------------- #


def _gf_xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1)


def _gf_mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a <<= 1
        if a & 0x100:
            a ^= 0x11B
        b >>= 1
    return r


def _build_sboxes():
    inv = [0] * 256
    for i in range(1, 256):
        for j in range(1, 256):
            if _gf_mul(i, j) == 1:
                inv[i] = j
                break
    sbox = [0] * 256
    for i in range(256):
        b = inv[i]
        s = b
        s ^= ((b << 1) | (b >> 7)) & 0xFF
        s ^= ((b << 2) | (b >> 6)) & 0xFF
        s ^= ((b << 3) | (b >> 5)) & 0xFF
        s ^= ((b << 4) | (b >> 4)) & 0xFF
        sbox[i] = s ^ 0x63
    inv_sbox = [0] * 256
    for i in range(256):
        inv_sbox[sbox[i]] = i
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_sboxes()


def _expand_key(key):
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    i = nk
    while i < 4 * (nr + 1):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[x] for x in t]
            t[0] ^= rcon
            rcon = _gf_xtime(rcon)
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[x] for x in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
        i += 1
    return nr, [b"".join(bytes(x) for x in w[4 * r:4 * r + 4]) for r in range(nr + 1)]


def _xor16(block, rk):
    return bytes(a ^ b for a, b in zip(block, rk))


def _sub_bytes(block, sbox):
    return bytes(sbox[x] for x in block)


def _shift_rows(block, inverse=False):
    # Column-major flat layout: byte index = 4*col + row.
    out = bytearray(16)
    for r in range(4):
        for c in range(4):
            old_c = (c + r) % 4 if not inverse else (c - r) % 4
            out[4 * c + r] = block[4 * old_c + r]
    return bytes(out)


def _mix_columns(block):
    out = bytearray(block)
    for c in range(4):
        x0, x1, x2, x3 = block[4 * c:4 * c + 4]
        out[4 * c] = _gf_xtime(x0) ^ (_gf_xtime(x1) ^ x1) ^ x2 ^ x3
        out[4 * c + 1] = x0 ^ _gf_xtime(x1) ^ (_gf_xtime(x2) ^ x2) ^ x3
        out[4 * c + 2] = x0 ^ x1 ^ _gf_xtime(x2) ^ (_gf_xtime(x3) ^ x3)
        out[4 * c + 3] = (_gf_xtime(x0) ^ x0) ^ x1 ^ x2 ^ _gf_xtime(x3)
    return bytes(out)


def _x9(x):
    return _gf_xtime(_gf_xtime(_gf_xtime(x))) ^ x


def _x11(x):
    x2 = _gf_xtime(x)
    return _gf_xtime(_gf_xtime(x2)) ^ x2 ^ x


def _x13(x):
    x2 = _gf_xtime(x)
    x4 = _gf_xtime(x2)
    x8 = _gf_xtime(x4)
    return x8 ^ x4 ^ x


def _x14(x):
    x2 = _gf_xtime(x)
    x4 = _gf_xtime(x2)
    x8 = _gf_xtime(x4)
    return x8 ^ x4 ^ x2


def _inv_mix_columns(block):
    out = bytearray(block)
    for c in range(4):
        x0, x1, x2, x3 = block[4 * c:4 * c + 4]
        out[4 * c] = _x14(x0) ^ _x11(x1) ^ _x13(x2) ^ _x9(x3)
        out[4 * c + 1] = _x9(x0) ^ _x14(x1) ^ _x11(x2) ^ _x13(x3)
        out[4 * c + 2] = _x13(x0) ^ _x9(x1) ^ _x14(x2) ^ _x11(x3)
        out[4 * c + 3] = _x11(x0) ^ _x13(x1) ^ _x9(x2) ^ _x14(x3)
    return bytes(out)


def aes_ecb(key, data, decrypt=False):
    """Raw AES ECB over full 16-byte blocks (internal; no padding)."""
    if len(key) not in (16, 24, 32):
        raise ValueError("AES key must be 16/24/32 bytes")
    if len(data) % 16:
        raise ValueError("AES block input must be a multiple of 16 bytes")
    nr, rk = _expand_key(key)
    out = bytearray()
    for off in range(0, len(data), 16):
        block = bytes(data[off:off + 16])
        if not decrypt:
            block = _xor16(block, rk[0])
            for rnd in range(1, nr):
                block = _mix_columns(_shift_rows(_sub_bytes(block, _SBOX)))
                block = _xor16(block, rk[rnd])
            block = _xor16(_shift_rows(_sub_bytes(block, _SBOX)), rk[nr])
        else:
            block = _xor16(block, rk[nr])
            for rnd in range(nr - 1, 0, -1):
                block = _sub_bytes(_shift_rows(block, inverse=True), _INV_SBOX)
                block = _xor16(block, rk[rnd])
                block = _inv_mix_columns(block)
            block = _sub_bytes(_shift_rows(block, inverse=True), _INV_SBOX)
            block = _xor16(block, rk[0])
        out += block
    return bytes(out)


def _pkcs7_pad(data):
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data):
    if not data or len(data) % 16:
        raise ValueError("invalid ciphertext length")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16 or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("invalid PKCS#7 padding")
    return data[:-pad_len]


def aes256_cbc_encrypt(key, iv, plaintext):
    if len(key) != KEY_BYTES or len(iv) != IV_LEN:
        raise ValueError("AES-256-CBC needs a 32-byte key and a 16-byte IV")
    data = _pkcs7_pad(plaintext)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        block = _xor16(data[off:off + 16], prev)
        enc = aes_ecb(key, block)
        out += enc
        prev = enc
    return bytes(out)


def aes256_cbc_decrypt(key, iv, ciphertext):
    if len(key) != KEY_BYTES or len(iv) != IV_LEN:
        raise ValueError("AES-256-CBC needs a 32-byte key and a 16-byte IV")
    if len(ciphertext) == 0 or len(ciphertext) % 16:
        raise ValueError("invalid ciphertext length")
    out = bytearray()
    prev = iv
    for off in range(0, len(ciphertext), 16):
        block = ciphertext[off:off + 16]
        out += _xor16(aes_ecb(key, block, decrypt=True), prev)
        prev = block
    return _pkcs7_unpad(bytes(out))


def _tag_key(key):
    return hashlib.sha256(TAG_PREFIX + key).digest()


def _tag(data, key):
    return hmac.new(_tag_key(key), data, hashlib.sha256).digest()


def encrypt_package(plaintext, key):
    """Encrypt-then-MAC -> magic | iv | ciphertext | tag (see container doc)."""
    if len(key) != KEY_BYTES:
        raise ValueError("one-time key must be 32 bytes")
    iv = secrets.token_bytes(IV_LEN)
    ct = aes256_cbc_encrypt(key, iv, plaintext)
    return MAGIC + iv + ct + _tag(MAGIC + iv + ct, key)


def decrypt_package(package, key):
    """Reverse of encrypt_package; verifies magic, length and HMAC tag."""
    if len(key) != KEY_BYTES:
        raise ValueError("one-time key must be 32 bytes")
    if len(package) < HEADER_LEN + TAG_LEN + 16 or package[:MAGIC_LEN] != MAGIC:
        raise ValueError("not a JQBC1 migration package")
    body_len = len(package) - HEADER_LEN - TAG_LEN
    if body_len % 16:
        raise ValueError("package ciphertext length is invalid")
    body = package[HEADER_LEN:HEADER_LEN + body_len]
    tag = package[HEADER_LEN + body_len:]
    if not hmac.compare_digest(tag, _tag(package[:HEADER_LEN + body_len], key)):
        raise ValueError("package integrity check failed (wrong key or tampered data)")
    return aes256_cbc_decrypt(key, package[MAGIC_LEN:HEADER_LEN], body)


# --------------------------------------------------------------------------- #
# Source material handling (values stay in memory; never printed).
# --------------------------------------------------------------------------- #


def sha256_hex(text):
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def read_bridge_key(path):
    """Read <path>, trim trailing EOLs, require exactly 64 hex chars."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ValueError("cannot read bridge key file %s: %s" % (path, exc)) from exc
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("bridge key file %s is not ASCII text" % path) from exc
    value = text.rstrip("\r\n")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(
            "bridge key file %s does not contain exactly 64 hex chars" % path
        )
    return value


def _strip_inline_comment(text):
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
        i += 1
    return text


def extract_ngrok_authtoken(text):
    """Minimal, fail-closed YAML-subset scan for the authtoken scalar.

    Understands the shapes ngrok itself writes: a top-level
    `authtoken: <value>` or `agent:` -> `  authtoken: <value>`, with optional
    single/double quotes and trailing comments. Duplicate or malformed values
    are rejected instead of guessed at.
    """
    found = []
    top_level = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            key, sep, rest = stripped.partition(":")
            if key.strip() == "agent" and sep and not rest.strip():
                top_level = "agent"
            else:
                top_level = None
        inside_agent = indent == 2 and top_level == "agent"
        if indent != 0 and not inside_agent:
            continue
        key, sep, rest = stripped.partition(":")
        if key.strip() != "authtoken" or not sep:
            continue
        value = _strip_inline_comment(rest).strip()
        if value.startswith("'") or value.startswith('"'):
            if len(value) >= 2 and value[-1] == value[0]:
                value = value[1:-1]
                if value.startswith("'"):
                    value = value.replace("''", "'")
            else:
                raise ValueError("malformed quoted authtoken in ngrok config")
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("unsupported authtoken formatting in ngrok config")
        found.append(value)
    distinct = set(found)
    if not distinct:
        raise ValueError("no authtoken found in the ngrok config")
    if len(distinct) > 1:
        raise ValueError("multiple distinct authtokens found; refusing to guess")
    return found[0]


def read_ngrok_authtoken(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError("cannot read ngrok config %s: %s" % (path, exc)) from exc
    return extract_ngrok_authtoken(text)


def read_optional_domain(path):
    """Fixed ngrok domain is not a secret, but keep it optional and tidy."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    if not value or any(ch.isspace() for ch in value):
        return None
    return value


# --------------------------------------------------------------------------- #
# Payload and PowerShell rendering.
# --------------------------------------------------------------------------- #


def build_plaintext(bridge_key, authtoken, domain=None):
    doc = {
        "schema": 1,
        "created_utc": int(time.time()),
        "bridge_api_key": bridge_key,
        "bridge_api_key_sha256": sha256_hex(bridge_key),
        "ngrok_authtoken": authtoken,
    }
    if domain:
        doc["ngrok_domain"] = domain
    return json.dumps(doc, indent=2, sort_keys=True).encode("ascii")


PS_TEMPLATE = r"""& {
  # Local Codex Bridge: Mac -> Windows one-time cutover (paste this ENTIRE block).
  $ErrorActionPreference = 'Stop'
  $K = '__CUTOVER_KEY__'
  $Url = '__CUTOVER_URL__'
  $ExpectedKeySha256 = '__CUTOVER_KEY_SHA256__'
  $KeyPath = '__CUTOVER_KEY_PATH__'
  $Work = Join-Path $env:TEMP ('lcb-cutover-' + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $Work -Force | Out-Null
  try {
    Write-Host '[cutover] downloading encrypted package (single-use LAN URL)'
    $Pkg = Join-Path $Work 'migration.bin'
    Invoke-WebRequest -Uri $Url -OutFile $Pkg -UseBasicParsing
    $raw = [IO.File]::ReadAllBytes($Pkg)
    if ($raw.Length -lt 69 -or (($raw.Length - 53) % 16) -ne 0) { throw 'downloaded package has an invalid size' }
    if ([Text.Encoding]::ASCII.GetString($raw, 0, 5) -ne 'JQBC1') { throw 'downloaded package has an unexpected magic' }
    $iv = New-Object byte[] 16
    [Array]::Copy($raw, 5, $iv, 0, 16)
    $ctLen = $raw.Length - 53
    $ct = New-Object byte[] $ctLen
    [Array]::Copy($raw, 21, $ct, 0, $ctLen)
    $tag = New-Object byte[] 32
    [Array]::Copy($raw, 21 + $ctLen, $tag, 0, 32)
    $key = [Convert]::FromBase64String($K)
    if ($key.Length -ne 32) { throw 'one-time key must decode to 32 bytes' }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
      $hmac = New-Object Security.Cryptography.HMACSHA256
      $hmac.Key = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes('JQB-TAG-v1:' + $K))
      $computedTag = $hmac.ComputeHash($raw, 0, 21 + $ctLen)
      if ([Convert]::ToBase64String($computedTag) -ne [Convert]::ToBase64String($tag)) { throw 'integrity check failed (wrong one-time key or tampered download)' }
      $aes = [Security.Cryptography.Aes]::Create()
      try {
        $aes.KeySize = 256
        $aes.BlockSize = 128
        $aes.Mode = [Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
        $aes.Key = $key
        $aes.IV = $iv
        $plain = $aes.CreateDecryptor().TransformFinalBlock($ct, 0, $ct.Length)
      } finally { $aes.Dispose() }
      $json = ([Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json)
      $keyValue = [string]$json.bridge_api_key
      if ($keyValue -notmatch '^[0-9a-fA-F]{64}$') { throw 'bridge api key in the package is not 64 hex chars' }
      $keyHashBytes = $sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($keyValue))
      $keySha256 = ''
      foreach ($b in $keyHashBytes) { $keySha256 += $b.ToString('x2') }
      if ($keySha256 -ne $ExpectedKeySha256) { throw 'bridge key hash does not match the Mac key; refusing to install' }
      $keyDir = Split-Path -Parent $KeyPath
      if (-not (Test-Path -LiteralPath $keyDir)) { New-Item -ItemType Directory -Path $keyDir -Force | Out-Null }
      $tmpKey = Join-Path $keyDir ('.bridge_api_key.tmp-' + [guid]::NewGuid().ToString('N'))
      try {
        [IO.File]::WriteAllText($tmpKey, $keyValue, (New-Object Text.UTF8Encoding($false)))
        & icacls.exe $tmpKey /inheritance:r /grant:r ("{0}:(F)" -f $env:USERNAME) *> $null
        if ($LASTEXITCODE -ne 0) { throw 'failed to lock down .bridge_api_key permissions' }
        Move-Item -LiteralPath $tmpKey -Destination $KeyPath -Force
      } finally {
        if (Test-Path -LiteralPath $tmpKey) { Remove-Item -LiteralPath $tmpKey -Force -ErrorAction SilentlyContinue }
      }
      $onDisk = [IO.File]::ReadAllText($KeyPath)
      if ($onDisk -ne $keyValue) { throw 'verification of the written key file failed' }
      Write-Host ('[cutover] key installed: ' + $KeyPath)
      Write-Host ('[cutover] key: 64 hex, SHA256 ' + $keySha256 + ' (matches Mac: True)')
      $token = [string]$json.ngrok_authtoken
      if ([string]::IsNullOrWhiteSpace($token)) { throw 'package contains no ngrok authtoken' }
      $cfgDir = Join-Path $env:USERPROFILE '.config\ngrok'
      if (-not (Test-Path -LiteralPath $cfgDir)) { New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null }
      $cfg = Join-Path $cfgDir 'ngrok.yml'
      $backup = ''
      if (Test-Path -LiteralPath $cfg) {
        $backup = $cfg + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Move-Item -LiteralPath $cfg -Destination $backup -Force
        Write-Host ('[cutover] existing ngrok config backed up to ' + $backup)
      }
      $tmpCfg = Join-Path $cfgDir ('ngrok.yml.tmp-' + [guid]::NewGuid().ToString('N'))
      try {
        $cfgText = 'version: "2"' + [Environment]::NewLine + 'authtoken: ' + $token + [Environment]::NewLine
        [IO.File]::WriteAllText($tmpCfg, $cfgText, (New-Object Text.UTF8Encoding($false)))
        & icacls.exe $tmpCfg /inheritance:r /grant:r ("{0}:(F)" -f $env:USERNAME) *> $null
        if ($LASTEXITCODE -ne 0) { throw 'failed to lock down ngrok.yml permissions' }
        Move-Item -LiteralPath $tmpCfg -Destination $cfg -Force
      } finally {
        if (Test-Path -LiteralPath $tmpCfg) { Remove-Item -LiteralPath $tmpCfg -Force -ErrorAction SilentlyContinue }
      }
      Write-Host '[cutover] ngrok authtoken installed into the Windows user config (token never printed)'
      $ngrok = $null
      if (-not [string]::IsNullOrWhiteSpace($env:NGROK_BIN) -and (Test-Path -LiteralPath $env:NGROK_BIN)) { $ngrok = $env:NGROK_BIN }
      if (-not $ngrok) {
        $found = Get-Command ngrok.exe -ErrorAction SilentlyContinue
        if ($found) { $ngrok = $found.Source }
      }
      if (-not $ngrok -and $env:LOCALAPPDATA) {
        $local = Join-Path $env:LOCALAPPDATA 'ngrok\ngrok.exe'
        if (Test-Path -LiteralPath $local) { $ngrok = $local }
      }
      if ($ngrok) {
        $null = & $ngrok config check 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Host '[cutover] ngrok config check: OK' }
        else { Write-Host '[cutover] ngrok config check FAILED - run "ngrok config check" manually for details' }
      } else {
        Write-Host '[cutover] ngrok.exe not found - install it (or set NGROK_BIN), then run "ngrok config check" once'
      }
      Write-Host '[cutover] done. Mac ngrok was NOT touched - stop it only after Windows took over the fixed domain.'
    } finally { $sha.Dispose() }
  } finally {
    if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue }
  }
}"""


def render_powershell_command(url, key_b64, expected_key_sha256, key_path):
    cmd = (
        PS_TEMPLATE.replace("__CUTOVER_KEY__", key_b64)
        .replace("__CUTOVER_URL__", url)
        .replace("__CUTOVER_KEY_SHA256__", expected_key_sha256)
        .replace("__CUTOVER_KEY_PATH__", key_path)
    )
    if "__CUTOVER_" in cmd:
        raise RuntimeError("PowerShell template placeholders were not all replaced")
    return cmd


# --------------------------------------------------------------------------- #
# LAN-only, single-download HTTP serving.
# --------------------------------------------------------------------------- #


def looks_lan_ipv4(ip):
    return not (
        ip.startswith("127.")
        or ip.startswith("169.254.")
        or ip.startswith("0.")
        or ip.startswith("255.")
    )


def detect_lan_ipv4():
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))  # UDP connect only; no packets sent
            ip = probe.getsockname()[0]
        finally:
            probe.close()
        if looks_lan_ipv4(ip):
            return ip
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if looks_lan_ipv4(ip):
                return ip
    except OSError:
        pass
    raise RuntimeError(
        "cannot determine the LAN IPv4 address; pass --bind-ip <address>"
    )


def _make_handler(expected_path, payload):
    class CutoverHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):  # quiet: never log the token path
            pass

        def _reply(self, code, body=b"", content_type="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "GET" and body:
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        def do_GET(self):
            server = self.server
            if getattr(server, "cutover_downloaded", False):
                self._reply(410, b"package already downloaded once\n")
                return
            if self.path != expected_path:
                self._reply(404, b"not found\n")
                return
            self._reply(200, payload, "application/octet-stream")
            server.cutover_downloaded = True

        def do_HEAD(self):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return CutoverHandler


def serve_once(host, payload, ttl_seconds, ready):
    """Serve `payload` once at http://<host>:<random-port>/dl/<token>.

    `ready(port, path)` is called after the socket is bound so the caller can
    print the URL and the PowerShell command exactly once. Returns
    'downloaded' or 'expired'. Plaintext is never exposed; every other path or
    repeated download gets 404/410.
    """
    if not payload:
        raise ValueError("refusing to serve an empty package")
    expected_path = "/dl/" + secrets.token_hex(16)
    httpd = HTTPServer((host, 0), _make_handler(expected_path, payload))
    httpd.cutover_path = expected_path
    httpd.cutover_downloaded = False
    try:
        ip, port = httpd.server_address[:2]
        if ready:
            ready(port, expected_path)
        httpd.socket.settimeout(0.5)
        deadline = time.time() + ttl_seconds
        while time.time() < deadline and not httpd.cutover_downloaded:
            try:
                httpd.handle_request()
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.05)
        return "downloaded" if httpd.cutover_downloaded else "expired"
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="prepare_macos_cutover.py",
        description=(
            "Build a one-time AES-256 encrypted Mac->Windows bridge cutover "
            "package and serve it once over the LAN (never prints secrets)."
        ),
    )
    parser.add_argument(
        "--bridge-key-file",
        default=None,
        help="bridge .bridge_api_key file (default: <repo root>/.bridge_api_key)",
    )
    parser.add_argument(
        "--ngrok-config",
        default=None,
        help=(
            "ngrok user config to read the authtoken from "
            "(default: ~/.config/ngrok/ngrok.yml, then ~/.ngrok2/ngrok.yml)"
        ),
    )
    parser.add_argument(
        "--domain-file",
        default=None,
        help="optional file holding the fixed ngrok domain (default: <repo root>/.ngrok_domain)",
    )
    parser.add_argument(
        "--windows-key-path",
        default=r"D:\work-of-jiaqi\actions-bridge\.bridge_api_key",
        help="where Windows installs .bridge_api_key (default: %(default)r)",
    )
    parser.add_argument(
        "--bind-ip",
        default=None,
        help="LAN IPv4 to bind (default: auto-detected primary LAN address)",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=600,
        help="max server lifetime in seconds, 1..600 (default: 600)",
    )
    parser.add_argument(
        "--no-serve",
        action="store_true",
        help="validate sources and build/delete a package without serving (testing)",
    )
    return parser


def _emit(text=""):
    """Line-buffered stdout so the command appears even when piped."""
    print(text, flush=True)


def main(argv=None):
    args = _build_arg_parser().parse_args(argv)
    root = repo_root()
    if args.ttl_seconds < 1 or args.ttl_seconds > 600:
        print("[cutover] error: --ttl-seconds must be between 1 and 600", file=sys.stderr)
        return 2

    bridge_path = args.bridge_key_file or os.path.join(root, ".bridge_api_key")
    domain_path = args.domain_file if args.domain_file is not None else os.path.join(root, ".ngrok_domain")

    try:
        bridge_key = read_bridge_key(bridge_path)
    except ValueError as exc:
        print("[cutover] error: %s" % exc, file=sys.stderr)
        return 1

    ngrok_config = args.ngrok_config
    if not ngrok_config:
        home = os.path.expanduser("~")
        for candidate in (
            os.path.join(home, ".config", "ngrok", "ngrok.yml"),
            os.path.join(home, ".ngrok2", "ngrok.yml"),
        ):
            if os.path.isfile(candidate):
                ngrok_config = candidate
                break
    if not ngrok_config or not os.path.isfile(ngrok_config):
        print(
            "[cutover] error: no ngrok config found; run 'ngrok config add-authtoken'"
            " first or pass --ngrok-config <path>",
            file=sys.stderr,
        )
        return 1
    try:
        authtoken = read_ngrok_authtoken(ngrok_config)
    except ValueError as exc:
        print("[cutover] error: %s" % exc, file=sys.stderr)
        return 1
    domain = read_optional_domain(domain_path)

    plaintext = build_plaintext(bridge_key, authtoken, domain=domain)
    one_time_key = secrets.token_bytes(KEY_BYTES)
    key_b64 = base64.b64encode(one_time_key).decode("ascii")
    expected_sha = sha256_hex(bridge_key)

    work_dir = tempfile.mkdtemp(prefix="lcb-cutover-")
    pkg_path = os.path.join(work_dir, "bridge-migration.bin")
    try:
        package = encrypt_package(plaintext, one_time_key)
        with open(pkg_path, "wb") as fh:
            fh.write(package)
        os.chmod(pkg_path, 0o600)

        _emit("[cutover] bridge key file: %s" % bridge_path)
        _emit("[cutover] bridge key SHA256 (hash only, never the value): %s" % expected_sha)
        _emit("[cutover] ngrok authtoken source: %s (value never printed)" % ngrok_config)
        if domain:
            _emit("[cutover] fixed domain (informational): %s" % domain)
        _emit("[cutover] encrypted package: %s (AES-256-CBC + HMAC-SHA256; nothing plaintext on disk)" % pkg_path)
        if args.no_serve:
            _emit("[cutover] --no-serve: package built and removed without serving")
            return 0

        host = args.bind_ip or detect_lan_ipv4()
        _emit()
        _emit("[cutover] paste this ENTIRE block into a Windows PowerShell window (single command):")
        _emit("----- BEGIN PowerShell -----")

        def ready(port, path):
            url = "http://%s:%d%s" % (host, port, path)
            cmd = render_powershell_command(url, key_b64, expected_sha, args.windows_key_path)
            _emit(cmd)
            _emit("----- END PowerShell -----")
            _emit()
            _emit(
                "[cutover] serving on %s:%d (LAN only, random high port, max %d s, one download,"
                " no plaintext endpoint)..." % (host, port, args.ttl_seconds)
            )

        outcome = serve_once(host, package, args.ttl_seconds, ready)
        if outcome == "downloaded":
            _emit("[cutover] package downloaded once; server stopped")
        else:
            _emit("[cutover] no download within %d s; server stopped" % args.ttl_seconds)
        _emit()
        _emit("[cutover] next steps (do not run them together):")
        if domain:
            default_domain = "diploma-ideology-skier.ngrok-free.dev"
            if domain == default_domain:
                _emit("  1. on Windows, in D:\\work-of-jiaqi\\actions-bridge, run:")
                _emit("     powershell -ExecutionPolicy Bypass -File scripts\\windows\\start_local_codex_bridge.ps1")
            else:
                _emit("  1. on Windows, in D:\\work-of-jiaqi\\actions-bridge, run (fixed domain %s):" % domain)
                _emit("     powershell -ExecutionPolicy Bypass -File scripts\\windows\\start_local_codex_bridge.ps1 -Domain %s" % domain)
        _emit("  2. when Windows ngrok owns the fixed domain, stop the Mac side:")
        _emit("     ./scripts/stop_ngrok_bridge.sh")
        _emit("[cutover] temp files removed; Mac ngrok was NOT touched")
        return 0
    except KeyboardInterrupt:
        print("\n[cutover] interrupted; temp files removed; nothing was migrated", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print("[cutover] error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
