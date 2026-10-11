"""Encrypt LMS credentials at rest with a key derived from PROCTORA_SECRET (stdlib only).

Construction: HMAC-SHA256 as a PRF in counter mode for the keystream (random 16-byte
nonce), then HMAC-SHA256 over version|nonce|ciphertext (encrypt-then-MAC). Separate
encryption and MAC keys are derived from the app secret. Changing PROCTORA_SECRET makes
stored credentials unreadable: teachers then have to reconnect (open() raises ValueError).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

VERSION = b"v1"


def _keys(app_secret: str) -> tuple[bytes, bytes]:
    root = hashlib.sha256(("fairplay-lms-credential:" + app_secret).encode()).digest()
    return (hmac.new(root, b"enc", hashlib.sha256).digest(),
            hmac.new(root, b"mac", hashlib.sha256).digest())


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hmac.new(key, nonce + ctr.to_bytes(8, "big"), hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:n])


def seal(app_secret: str, plaintext: str) -> str:
    ek, mk = _keys(app_secret)
    nonce = secrets.token_bytes(16)
    data = plaintext.encode()
    ct = bytes(a ^ b for a, b in zip(data, _stream(ek, nonce, len(data))))
    tag = hmac.new(mk, VERSION + nonce + ct, hashlib.sha256).digest()
    return VERSION.decode() + ":" + base64.urlsafe_b64encode(nonce + ct + tag).decode()


def open_(app_secret: str, sealed: str) -> str:
    ver, _, body = sealed.partition(":")
    if ver.encode() != VERSION:
        raise ValueError("unknown credential format")
    raw = base64.urlsafe_b64decode(body.encode())
    if len(raw) < 48:
        raise ValueError("credential too short")
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    ek, mk = _keys(app_secret)
    if not hmac.compare_digest(tag, hmac.new(mk, VERSION + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("credential cannot be decrypted (was PROCTORA_SECRET changed?)")
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct)))).decode()
