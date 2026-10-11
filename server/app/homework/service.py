"""Homework rules shared by the teacher and student routes: who is in it, what a student may
still do, the per-student status, and release / auto-release."""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..classes.models import ClassMember, ExamClass, SchoolClass
from ..grading import exam_questions, paper_answer, paper_given
from ..models import Attempt, Exam, User
from ..scan.models import ExamRoster
from ..services import invalidate_exam, pending_by_exam
from ..store import get_store
from ..timeutil import utcnow
from .models import HomeworkAssignment, HomeworkSubmission

NOT_IN_CLASS = "You are not in a class for this homework"

# per-student status, in workflow order
STATUSES = ("not_submitted", "submitted", "ai_graded", "reviewed", "released")


async def roster(session: AsyncSession, e: Exam) -> list[User]:
    from ..routes_scan import roster_of  # late import (routes_scan imports app.scan)
    _, users = await roster_of(session, e)
    return users


async def require_member(session: AsyncSession, e: Exam, user_id: int) -> None:
    if not any(u.id == user_id for u in await roster(session, e)):
        raise HTTPException(403, NOT_IN_CLASS)


async def student_homework_ids(session: AsyncSession, user_id: int) -> list[str]:
    """Homework the student belongs to (assigned classes or an explicit roster)."""
    via_class = select(ExamClass.exam_id).join(ClassMember, ClassMember.class_id == ExamClass.class_id).where(
        ClassMember.user_id == user_id)
    via_roster = select(ExamRoster.exam_id).where(ExamRoster.user_id == user_id)
    cands = list((await session.execute(select(HomeworkAssignment.exam_id).where(or_(
        HomeworkAssignment.exam_id.in_(via_class), HomeworkAssignment.exam_id.in_(via_roster))))).scalars())
    out = []
    for eid in cands:
        e = await session.get(Exam, eid)
        if e is not None and any(u.id == user_id for u in await roster(session, e)):
            out.append(eid)
    return out


async def class_names(session: AsyncSession, exam_id: str, user_id: int | None = None) -> list[str]:
    q = select(SchoolClass.name).join(ExamClass, ExamClass.class_id == SchoolClass.id).where(
        ExamClass.exam_id == exam_id)
    if user_id is not None:
        q = q.join(ClassMember, ClassMember.class_id == SchoolClass.id).where(ClassMember.user_id == user_id)
    return sorted(set((await session.execute(q)).scalars()))


def is_late(hw: HomeworkAssignment, at: datetime | None = None) -> bool:
    return (at or utcnow()) > hw.due_at


def submit_block(hw: HomeworkAssignment, e: Exam, sub: HomeworkSubmission | None,
                 attempt: Attempt | None, now: datetime | None = None) -> str | None:
    """Why the student may NOT submit now (None = may submit)."""
    now = now or utcnow()
    if e.grades_released and (sub is not None or has_work(attempt)):
        return "Feedback is already released"
    already = sub is not None and sub.status != "failed" or (sub is None and has_work(attempt))
    if already and not hw.allow_resubmit:
        return "You already handed this in"
    if now > hw.due_at:
        if not hw.allow_late:
            return "The due date has passed"
        if already:
            return "The due date has passed — your work is handed in"
    if sub is not None and sub.status in ("processing", "grading") and attempt_busy(sub):
        return "Your last upload is still being read — wait a moment"
    return None


def attempt_busy(sub: HomeworkSubmission) -> bool:
    return sub.status == "processing"


def has_work(a: Attempt | None) -> bool:
    return a is not None and a.status == "submitted" and any(paper_answer(v) for v in (a.answers or {}).values())


def student_status(e: Exam, a: Attempt | None, sub: HomeworkSubmission | None) -> str:
    if sub is not None and sub.status in ("processing", "grading"):
        return "submitted"
    if not has_work(a):
        return "not_submitted"
    if (a.needs_grading or 0) == 0:
        return "released" if e.grades_released else "reviewed"
    qs = exam_questions(e)
    grades, drafts = a.grades or {}, a.ai_drafts or {}
    for q in qs:
        ans = (a.answers or {}).get(q["id"])
        if paper_answer(ans) and grades.get(q["id"]) is None and paper_given(q, ans, None)[1] \
                and not drafts.get(q["id"]):
            return "submitted"
    return "ai_graded"


async def release(session: AsyncSession, e: Exam, force: bool = False) -> None:
    """Close (papers collected) and release. Raises 409 when answers still need grading."""
    pending = (await pending_by_exam(session, [e.id])).get(e.id, 0)
    if pending > 0 and not force:
        raise HTTPException(409, f"{pending} answers still need grading")
    busy = (await session.execute(select(HomeworkSubmission.id).where(
        HomeworkSubmission.exam_id == e.id, HomeworkSubmission.status == "processing").limit(1))).first()
    if busy and not force:
        raise HTTPException(409, "Some uploads are still being read")
    if not force:
        from ..quality.service import moderation_open
        open_n = (await moderation_open(session, e.id))["open"]
        if open_n > 0:
            raise HTTPException(409, f"{open_n} second-marked answers still need moderation: grades may change")
    now = utcnow()
    if e.status != "closed":
        e.status, e.start_code, e.closed_at = "closed", None, e.closed_at or now
    if not e.grades_released:
        e.grades_released, e.released_at = True, now
    e.updated_at = now


async def maybe_auto_release(session: AsyncSession, exam_id: str) -> bool:
    """Gentle auto-release: when the teacher turned it on, every submitted answer has a
    teacher grade, nothing is still being read, at least one student handed in, and either
    the due date passed or every student in the classes handed in. Commits when it releases."""
    hw = await session.get(HomeworkAssignment, exam_id)
    e = await session.get(Exam, exam_id)
    if hw is None or e is None or not hw.auto_release or e.grades_released:
        return False
    atts = list((await session.execute(select(Attempt).where(
        Attempt.exam_id == exam_id, Attempt.status == "submitted"))).scalars())
    worked = [a for a in atts if has_work(a)]
    if not worked or any((a.needs_grading or 0) > 0 for a in worked):
        return False
    if (await session.execute(select(HomeworkSubmission.id).where(
            HomeworkSubmission.exam_id == exam_id,
            HomeworkSubmission.status.in_(("processing", "grading"))).limit(1))).first():
        return False
    if not is_late(hw):
        ids = {u.id for u in await roster(session, e)}
        if not ids or not ids <= {a.student_id for a in worked}:
            return False
    try:
        await release(session, e)
    except HTTPException:
        return False
    hw.auto_released_at = utcnow()
    await session.commit()
    await invalidate_exam(get_store(), e.id)
    return True
