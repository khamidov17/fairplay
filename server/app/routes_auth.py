from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import User
from .routes_register import check_status, login_filter, touch_last_login
from .schemas import LoginIn
from .security import Principal, current_principal, make_token, verify_password_async

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)):
    row = (await session.execute(
        select(User.id, User.role, User.name, User.password_hash, User.status)
        .where(login_filter(body.username)))).first()
    # Release the pooled connection before the CPU-heavy hash check.
    await session.close()
    if row is None or not await verify_password_async(body.password, row.password_hash):
        raise HTTPException(401, "Invalid username or password")
    check_status(row.status)  # pending / disabled -> 403 (only after the password is right)
    await touch_last_login(row.id)
    s = request.app.state.settings
    return {"token": make_token(s.secret, row.id, row.role, s.token_ttl_s),
            "role": row.role, "name": row.name}


@router.get("/me")
async def me(p: Principal = Depends(current_principal),
             session: AsyncSession = Depends(get_session)):
    u = await session.get(User, p.user_id)
    if u is None:
        raise HTTPException(401, "Invalid or expired token")
    return {"username": u.username, "name": u.name, "role": u.role}
