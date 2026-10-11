"""Phase 1a: per-exam invigilator exit code (replaces the fixed dev code 1234).

- Issued when the teacher opens the exam (`issue()`), 8 characters from the receipt
  alphabet shown as XXXX-XXXX (~40 bits). Teacher-only: in the teacher Exam view and the
  live room, never in any student response.
- The Mac app asks the server first: `POST /api/student/attempts/{aid}/exit-code`
  `{"code"}` -> 200 `{"ok": true}` | 403 wrong | 429 too many wrong codes. Every try is
  in the audit log (exit_code_used / exit_code_rejected).
- Offline fallback (the network is often exactly what failed): the attempt view carries
  `exit_check` = PBKDF2-SHA256(normalised code, salt, 100 000) which the Mac app compares
  locally only when the server cannot be reached. With ~40 bits of code behind 100 000
  PBKDF2 rounds, guessing it from the verifier is not practical during an exam.
- The code stays valid after the exam closes (a computer may still be locked) and is
  replaced at the next open.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db import get_session
from .models import Attempt, Exam
from .security import Principal, require_student
from .services import RECEIPT_ALPHABET
from .store import get_store

router = APIRouter(prefix="/api/student", tags=["exit-code"])

CHECK_ITERATIONS = 100_000
MAX_WRONG = 5
WRONG_WINDOW_S = 600
WRONG_LOCK_S = 300


def new_exit_code() -> str:
    s = "".join(secrets.choice(RECEIPT_ALPHABET) for _ in range(8))
    return f"{s[:4]}-{s[4:]}"


def normalize(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _derive(code: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", normalize(code).encode(), salt, iterations)


def make_check(code: str, iterations: int = CHECK_ITERATIONS) -> dict:
    salt = os.urandom(16)
    return {"alg": "PBKDF2-SHA256", "iterations": iterations,
            "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(_derive(code, salt, iterations)).decode()}


def verify_check(check: dict, code: str) -> bool:
    salt = base64.b64decode(check["salt"])
    got = _derive(code, salt, int(check["iterations"]))
    return hmac.compare_digest(got, base64.b64decode(check["hash"]))


async def issue() -> tuple[str, str]:
    """(code, exit_check JSON) for a newly opened exam (PBKDF2 in a worker thread)."""
    code = new_exit_code()
    check = await asyncio.to_thread(make_check, code)
    return code, json.dumps(check, separators=(",", ":"))


def check_of(exit_check: str | None) -> dict | None:
    try:
        return json.loads(exit_check) if exit_check else None
    except ValueError:
        return None


def codes_match(stored: str | None, typed: str) -> bool:
    if not stored:
        return False
    return hmac.compare_digest(normalize(stored).encode(), normalize(typed).encode())


class ExitCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)


@router.post("/attempts/{aid}/exit-code")
async def use_exit_code(aid: str, body: ExitCodeIn, request: Request,
                        p: Principal = Depends(require_student),
                        session: AsyncSession = Depends(get_session)):
    row = (await session.execute(
        select(Attempt.student_id, Attempt.exam_id, Exam.exit_code)
        .join(Exam, Exam.id == Attempt.exam_id).where(Attempt.id == aid))).first()
    if row is None or row.student_id != p.user_id:
        raise HTTPException(404, "Attempt not found")
    store = get_store()
    lock_key, count_key = f"exitlock:{aid}", f"exitwrong:{aid}"
    if await store.get(lock_key):
        raise HTTPException(429, "Too many wrong exit codes, wait 5 minutes")
    common = dict(request=request, actor_id=p.user_id, actor_role="student",
                  exam_id=row.exam_id, attempt_id=aid)
    if not codes_match(row.exit_code, body.code):
        n = await store.incr(count_key, WRONG_WINDOW_S)
        if n >= MAX_WRONG:
            await store.set(lock_key, "1", WRONG_LOCK_S)
            await store.delete(count_key)
        await audit.record("exit_code_rejected", detail={"verified": "server"}, **common)
        raise HTTPException(403, "Wrong exit code")
    await store.delete(count_key)
    await audit.record("exit_code_used", detail={"verified": "server"}, **common)
    return {"ok": True}
