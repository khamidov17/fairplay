"""Phase 1a: password change and "sign out everywhere" (token revocation).

Tokens stay stateless (HMAC, no session table). Each carries the user's token_version at
issue time; bumping User.token_version (here, or from the CLI / an admin endpoint via
`revoke_user_tokens`) makes every older token fail in app.security.current_principal.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db import get_session
from .models import User
from .security import (Principal, TOKEN_VERSION_CACHE_S, current_principal, hash_password,
                       make_token, token_version_key, verify_password_async)
from .store import get_store

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LEN = 8


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=200)


async def revoke_user_tokens(session: AsyncSession, user_id: int, commit: bool = True) -> int:
    """Invalidate every token issued so far for this user. Returns the new version."""
    await session.execute(update(User).where(User.id == user_id)
                          .values(token_version=User.token_version + 1))
    ver = (await session.execute(select(User.token_version).where(User.id == user_id))).scalar_one()
    if commit:
        await session.commit()
    await get_store().set(token_version_key(user_id), str(int(ver)), TOKEN_VERSION_CACHE_S)
    return int(ver)


@router.post("/password")
async def change_password(body: PasswordIn, request: Request,
                          p: Principal = Depends(current_principal),
                          session: AsyncSession = Depends(get_session)):
    u = await session.get(User, p.user_id)
    if u is None:
        raise HTTPException(401, "Invalid or expired token")
    if not await verify_password_async(body.current_password, u.password_hash):
        raise HTTPException(403, "Current password is wrong")
    s = request.app.state.settings
    u.password_hash = hash_password(body.new_password, s.pbkdf2_iterations)
    audit.add(session, "password_changed", request=request, actor_id=u.id, actor_role=u.role)
    ver = await revoke_user_tokens(session, u.id)
    return {"token": make_token(s.secret, u.id, u.role, s.token_ttl_s, version=ver),
            "role": u.role, "name": u.name}


@router.post("/logout-all", status_code=204)
async def logout_everywhere(request: Request, p: Principal = Depends(current_principal),
                            session: AsyncSession = Depends(get_session)):
    audit.add(session, "sessions_revoked", request=request, actor_id=p.user_id, actor_role=p.role,
              detail={"by": "self"})
    await revoke_user_tokens(session, p.user_id)
    return Response(status_code=204)
