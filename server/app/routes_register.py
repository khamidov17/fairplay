"""Self-registration (teachers and students) and the login helpers that go with it.

- `POST /api/auth/register` creates a teacher or student account and signs it in.
- Teacher signup follows `Settings.teacher_signup` (env FAIRPLAY_TEACHER_SIGNUP):
  open -> active account; approval -> pending account (a superadmin approves it in
  /admin); closed -> 403. Student signup is always open.
- Login accepts a username or an email (`login_filter`), and refuses pending or
  disabled accounts (`check_status`).
"""
from __future__ import annotations

import asyncio
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_database, get_session
from .models import User
from .security import hash_password, make_token
from .store import get_store
from .timeutil import utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
# Deliberately loose: one @, a dot in the domain, no spaces.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
MAX_PASSWORD = 128
# Per-IP throttle (Redis in production, memory in dev): this many signups per window.
REGISTER_LIMIT = 10
REGISTER_WINDOW_S = 3600

PENDING_DETAIL = "Your teacher account is waiting for approval by an administrator."
DISABLED_DETAIL = "This account has been disabled. Contact your administrator."


class RegisterIn(BaseModel):
    name: str = Field(max_length=200)
    email: str = Field(max_length=254)
    username: str = Field(max_length=64)
    password: str = Field(max_length=MAX_PASSWORD)
    role: Literal["teacher", "student"]


def password_problem(password: str, username: str = "", email: str = "") -> str | None:
    """None if the password is acceptable, else a message for the user."""
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[^A-Za-z]", password):
        return "Password must contain letters and at least one digit or symbol"
    low = password.lower()
    if username and username in low or (email and email.split("@")[0] in low and
                                         len(email.split("@")[0]) >= 3):
        return "Password must not contain your username or email"
    if len(set(password)) < 4:
        return "Password is too simple"
    return None


def login_filter(identifier: str):
    """WHERE clause for login: an email (contains @) or a username. Both lookups use
    their unique index (usernames from registration are stored lowercase)."""
    ident = identifier.strip()
    if "@" in ident:
        return User.email == ident.lower()
    return User.username.in_({ident, ident.lower()})


def check_status(status: str | None) -> None:
    if status == "pending":
        raise HTTPException(403, PENDING_DETAIL)
    if status == "disabled":
        raise HTTPException(403, DISABLED_DETAIL)


async def touch_last_login(user_id: int) -> None:
    async with get_database().sessionmaker() as session:
        await session.execute(update(User).where(User.id == user_id).values(last_login_at=utcnow()))
        await session.commit()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/register", status_code=201)
async def register(body: RegisterIn, request: Request,
                   session: AsyncSession = Depends(get_session)):
    settings = request.app.state.settings
    name = " ".join(body.name.split())
    email = body.email.strip().lower()
    username = body.username.strip().lower()

    if len(name) < 2:
        raise HTTPException(422, "Enter your full name (at least 2 characters)")
    if len(name) > 100:
        raise HTTPException(422, "Name must be at most 100 characters")
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address")
    if not USERNAME_RE.match(username):
        raise HTTPException(422, "Username must be 3-32 characters: letters, digits, dot, dash "
                                 "or underscore, starting with a letter or digit")
    problem = password_problem(body.password, username, email)
    if problem:
        raise HTTPException(422, problem)

    status = "active"
    if body.role == "teacher":
        mode = settings.teacher_signup
        if mode == "closed":
            raise HTTPException(403, "Teacher sign-up is closed. Ask your administrator "
                                     "for an account.")
        if mode == "approval":
            status = "pending"

    n = await get_store().incr(f"reg:ip:{_client_ip(request)}", REGISTER_WINDOW_S)
    if n > REGISTER_LIMIT:
        raise HTTPException(429, "Too many sign-ups from this network. Try again later.")

    taken = (await session.execute(
        select(User.username, User.email).where(or_(
            func.lower(User.username) == username, User.email == email)))).all()
    if any((u or "").lower() == username for u, _ in taken):
        raise HTTPException(409, "That username is taken")
    if taken:
        raise HTTPException(409, "An account with that email already exists")

    ph = await asyncio.to_thread(hash_password, body.password, settings.pbkdf2_iterations)
    user = User(username=username, name=name, email=email, role=body.role,
                password_hash=ph, status=status,
                last_login_at=utcnow() if status == "active" else None)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:  # a concurrent signup won the race
        await session.rollback()
        raise HTTPException(409, "That username or email is already registered")

    if status == "pending":
        return {"status": "pending", "role": body.role, "name": name, "token": None,
                "detail": PENDING_DETAIL}
    return {"status": "active", "role": body.role, "name": name,
            "token": make_token(settings.secret, user.id, body.role, settings.token_ttl_s)}
