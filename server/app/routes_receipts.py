"""Phase 1b: a teacher can verify a student's submission receipt.

A student who says "I submitted, here is my receipt K7Q2-M9XD" can be checked against the
server in one lookup: which student and attempt the receipt belongs to, when it was
submitted and how many questions the server holds an answer for.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .grading import exam_questions, is_answered
from .models import Attempt, Exam, User
from .security import Principal, require_teacher
from .timeutil import iso

router = APIRouter(prefix="/api/exams", tags=["teacher"])

_RECEIPT_RE = re.compile(r"^[A-Z0-9]{8}$")


def normalize_receipt(raw: str) -> str | None:
    """Accept what people type: any case, spaces, with or without the dash."""
    s = re.sub(r"[\s\-]", "", raw or "").upper()
    if not _RECEIPT_RE.match(s):
        return None
    return f"{s[:4]}-{s[4:]}"


@router.get("/{exam_id}/receipts/{receipt}")
async def verify_receipt(exam_id: str, receipt: str, p: Principal = Depends(require_teacher),
                         session: AsyncSession = Depends(get_session)):
    e = await session.get(Exam, exam_id)
    if e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Exam not found")
    code = normalize_receipt(receipt)
    if code is None:
        raise HTTPException(404, "Receipt not found")
    rows = (await session.execute(
        select(Attempt, User.username, User.name)
        .join(User, User.id == Attempt.student_id)
        .where(Attempt.exam_id == exam_id, Attempt.receipt == code,
               Attempt.status == "submitted"))).all()
    if not rows:
        raise HTTPException(404, "Receipt not found")
    a, username, name = rows[0]
    answers = a.answers or {}
    qs = exam_questions(e)
    return {
        "valid": True, "receipt": code, "exam_id": exam_id, "attempt_id": a.id,
        "username": username, "name": name, "status": a.status,
        "submitted_at": iso(a.submitted_at), "auto_submitted": bool(a.auto_submitted),
        "late": bool(a.late),
        "answered": sum(1 for q in qs if is_answered(answers.get(q["id"]))),
        "question_count": len(qs),
    }
