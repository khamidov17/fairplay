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


def make_token(secret: str, user_id: int, role: str, ttl_s: int, now: float | None = None,
               version: int = 0) -> str:
    """Payload `user_id:role:exp:version` (version = User.token_version at issue time;
    bumping it revokes every older token). Tokens without the 4th field count as version 0."""
    exp = int((now or time.time()) + ttl_s)
    payload = _b64(f"{user_id}:{role}:{exp}:{int(version)}".encode())
    sig = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def parse_token_full(secret: str, token: str) -> tuple[int, str, int] | None:
    """(user_id, role, token_version) of a valid, unexpired token, else None."""
    try:
        payload, sig = token.split(".")
        expected = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        parts = _unb64(payload).decode().split(":")
        if len(parts) == 3:
            uid, role, exp = parts
            ver = "0"
        elif len(parts) == 4:
            uid, role, exp, ver = parts
        else:
            return None
        if int(exp) < time.time() or role not in ("teacher", "student"):
            return None
        return int(uid), role, int(ver)
    except (ValueError, UnicodeDecodeError):
        return None


def parse_token(secret: str, token: str) -> tuple[int, str] | None:
    parsed = parse_token_full(secret, token)
    return (parsed[0], parsed[1]) if parsed else None


class Principal:
    __slots__ = ("user_id", "role")

    def __init__(self, user_id: int, role: str):
        self.user_id = user_id
        self.role = role


# ---------------------------------------------------------------- token revocation
# User.token_version is the source of truth; the store caches it per user ("tv:<id>") so
# the per-request check is one store GET (a DB read only on a cache miss, once per
# TOKEN_VERSION_CACHE_S per user). Revocation updates the DB, then the cache.
TOKEN_VERSION_CACHE_S = 600


def token_version_key(user_id: int) -> str:
    return f"tv:{user_id}"


async def current_token_version(user_id: int) -> int | None:
    """Current token_version of a user (None: user does not exist)."""
    from .store import get_store
    store = get_store()
    raw = await store.get(token_version_key(user_id))
    if raw is not None:
        return None if raw == "x" else int(raw)
    from sqlalchemy import select
    from .db import get_database
    from .models import User
    async with get_database().sessionmaker() as session:
        ver = (await session.execute(
            select(User.token_version).where(User.id == user_id))).scalar_one_or_none()
    await store.set(token_version_key(user_id), "x" if ver is None else str(int(ver or 0)),
                    TOKEN_VERSION_CACHE_S)
    return None if ver is None else int(ver or 0)


async def current_principal(request: Request) -> Principal:
    settings: Settings = request.app.state.settings
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Not authenticated")
    parsed = parse_token_full(settings.secret, token.strip())
    if not parsed:
        raise HTTPException(401, "Invalid or expired token")
    uid, role, ver = parsed
    current = await current_token_version(uid)
    if current is None or ver < current:
        raise HTTPException(401, "Invalid or expired token")
    return Principal(uid, role)


def require_teacher(p: Principal = Depends(current_principal)) -> Principal:
    if p.role != "teacher":
        raise HTTPException(403, "Teacher role required")
    return p


def require_student(p: Principal = Depends(current_principal)) -> Principal:
    if p.role != "student":
        raise HTTPException(403, "Student role required")
    return p
