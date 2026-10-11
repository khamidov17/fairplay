"""Password hashing (PBKDF2-SHA256) and stateless HMAC-signed tokens."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Depends, HTTPException, Request

from .config import Settings


def hash_password(password: str, iterations: int, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(it))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


async def verify_password_async(password: str, stored: str) -> bool:
    # PBKDF2 is CPU-bound; keep it off the event loop.
    return await asyncio.to_thread(verify_password, password, stored)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(secret: str, user_id: int, role: str, ttl_s: int, now: float | None = None) -> str:
    exp = int((now or time.time()) + ttl_s)
    payload = _b64(f"{user_id}:{role}:{exp}".encode())
    sig = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def parse_token(secret: str, token: str) -> tuple[int, str] | None:
    try:
        payload, sig = token.split(".")
        expected = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        uid, role, exp = _unb64(payload).decode().split(":")
        if int(exp) < time.time() or role not in ("teacher", "student"):
            return None
        return int(uid), role
    except (ValueError, UnicodeDecodeError):
        return None


class Principal:
    __slots__ = ("user_id", "role")

    def __init__(self, user_id: int, role: str):
        self.user_id = user_id
        self.role = role


def current_principal(request: Request) -> Principal:
    settings: Settings = request.app.state.settings
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Not authenticated")
    parsed = parse_token(settings.secret, token.strip())
    if not parsed:
        raise HTTPException(401, "Invalid or expired token")
    return Principal(*parsed)


def require_teacher(p: Principal = Depends(current_principal)) -> Principal:
    if p.role != "teacher":
        raise HTTPException(403, "Teacher role required")
    return p


def require_student(p: Principal = Depends(current_principal)) -> Principal:
    if p.role != "student":
        raise HTTPException(403, "Student role required")
    return p
