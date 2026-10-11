"""Slice 6 C: topic tags for autocomplete. `GET /api/topics` (teacher) ->
[{"topic","question_count"}] over this teacher's exams, most used first."""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import Exam
from .security import Principal, require_teacher

router = APIRouter(prefix="/api/topics", tags=["topics"])


def count_topics(question_lists) -> list[dict]:
    counts: Counter[str] = Counter()
    for questions in question_lists:
        for q in questions or []:
            if q.get("type") == "external":
                continue  # hidden website-exam item: not a teacher-written topic
            for t in {str(t).strip().lower() for t in q.get("topics") or [] if str(t).strip()}:
                counts[t] += 1
    return [{"topic": t, "question_count": n}
            for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


@router.get("")
async def list_topics(p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Exam.questions).where(Exam.teacher_id == p.user_id))).scalars().all()
    return count_topics(rows)
