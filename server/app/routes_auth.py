from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import User
from .schemas import LoginIn
from .security import Principal, current_principal, hash_password, make_token, verify_password_async
from .store import get_store

router = APIRouter(prefix="/api", tags=["auth"])

# Audit 2026-10-12 S-05. Every wrong password costs ~0.1-0.2 s of PBKDF2, so login is
# throttled and the number of password checks running at once is bounded per process
# (other requests keep their worker threads during an exam).
#   per IP:              300 tries / minute  (a whole lab behind one campus NAT logs in at once)
#   per username + IP:    10 wrong / 5 minutes
#   per username:         50 wrong / 5 minutes (someone guessing one student's password)
IP_WINDOW = 60   # per-IP limit: Settings.login_ip_limit (FAIRPLAY_LOGIN_IP_LIMIT, default 300)
USER_IP_FAILS, USER_FAILS, FAIL_WINDOW = 10, 50, 300
MAX_PARALLEL_HASHES = 4
TOO_MANY = "Too many sign-in attempts. Wait a few minutes and try again."

_dummy_hash: dict[int, str] = {}


def _slots(request: Request) -> asyncio.Semaphore:
    # One per app (and so per event loop); created on first use.
    st = request.app.state
    if getattr(st, "hash_slots", None) is None:
        st.hash_slots = asyncio.Semaphore(MAX_PARALLEL_HASHES)
    return st.hash_slots


async def _dummy(iterations: int) -> str:
    """Unknown usernames are checked against this, so they take as long as real ones."""
    if iterations not in _dummy_hash:
        _dummy_hash[iterations] = await asyncio.to_thread(hash_password, "not-a-real-password", iterations)
    return _dummy_hash[iterations]


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers sets this from nginx's X-Forwarded-For (nginx overwrites it).
    return (request.client.host if request.client else "") or "unknown"


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)):
    s = request.app.state.settings
    store = get_store()
    username = body.username.strip()
    uname = username.lower()[:64]
    ip = client_ip(request)

    if await store.incr(f"login:ip:{ip}", IP_WINDOW) > s.login_ip_limit:
        raise HTTPException(429, TOO_MANY)
    fails_ui, fails_u = f"login:fail:{uname}:{ip}", f"login:fail:{uname}"
    if (int(await store.get(fails_ui) or 0) >= USER_IP_FAILS
            or int(await store.get(fails_u) or 0) >= USER_FAILS):
        raise HTTPException(429, TOO_MANY)

    row = (await session.execute(
        select(User.id, User.role, User.name, User.password_hash, User.token_version)
        .where(User.username == username))).first()
    # Release the pooled connection before the CPU-heavy hash check.
    await session.close()
    stored = row.password_hash if row is not None else await _dummy(s.pbkdf2_iterations)
    async with _slots(request):
        ok = await verify_password_async(body.password, stored)
    if row is None or not ok:
        await store.incr(fails_ui, FAIL_WINDOW)
        await store.incr(fails_u, FAIL_WINDOW)
        raise HTTPException(401, "Invalid username or password")
    ttl = s.student_token_ttl_s if row.role == "student" else s.token_ttl_s
    return {"token": make_token(s.secret, row.id, row.role, ttl, version=row.token_version or 0),
            "role": row.role, "name": row.name}


@router.post("/auth/logout")
async def logout_everywhere(p: Principal = Depends(current_principal),
                            session: AsyncSession = Depends(get_session)):
    """Audit S-15: sign this user out on every device (all their tokens stop working)."""
    await bump_token_version(session, p.user_id)
    return {"ok": True}


async def bump_token_version(session: AsyncSession, user_id: int) -> None:
    from sqlalchemy import update
    from .security import forget_user_cache
    await session.execute(update(User).where(User.id == user_id)
                          .values(token_version=User.token_version + 1))
    await session.commit()
    await forget_user_cache(user_id)


@router.get("/me")
async def me(p: Principal = Depends(current_principal),
             session: AsyncSession = Depends(get_session)):
    u = await session.get(User, p.user_id)
    if u is None:
        raise HTTPException(401, "Invalid or expired token")
    return {"username": u.username, "name": u.name, "role": u.role}
