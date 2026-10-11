"""Slice 6 F/G: class insights (`GET /api/insights`), the teacher's students
(`GET /api/students`) and one student's profile (`GET /api/students/{username}/profile`).

Everything is scoped to the requesting teacher's own exams. Each request is a fixed
number of queries (see analytics.load_facts); the per-answer work happens in Python.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import analytics
from .db import get_session
from .models import Attempt, CourseLink, CourseMember, Exam, LmsConnection, User
from .security import Principal, require_teacher
from .timeutil import iso

router = APIRouter(prefix="/api", tags=["teacher"])


def _filter_exams(exams: list[Exam], course: str | None, exam_ids: str | None) -> list[Exam]:
    course = (course or "").strip()
    ids = {x.strip() for x in (exam_ids or "").split(",") if x.strip()}
    return [e for e in exams
            if (not course or (e.course or "") == course) and (not ids or e.id in ids)]


@router.get("/insights")
async def insights(course: str | None = Query(default=None, max_length=64),
                   exam_ids: str | None = Query(default=None, max_length=4000),
                   p: Principal = Depends(require_teacher),
                   session: AsyncSession = Depends(get_session)):
    all_exams = await analytics.teacher_exams(session, p.user_id)
    chosen = _filter_exams(all_exams, course, exam_ids)
    facts = await analytics.load_facts(session, chosen)
    out = await asyncio.to_thread(analytics.insights_report, facts)
    out["courses"] = sorted({e.course for e in all_exams if e.course})
    # Filter options: every non-draft exam of the teacher in the chosen course.
    out["all_exams"] = [
        {"exam_id": e.id, "title": e.title, "course": e.course or "",
         "date": iso(analytics.exam_date(e))}
        for e in sorted(_filter_exams(all_exams, course, None),
                        key=lambda e: (analytics.exam_date(e), e.id))]
    return out


async def _lms_members(session: AsyncSession, teacher_id: int) -> dict[int, tuple[str, str]]:
    rows = (await session.execute(
        select(User.id, User.username, User.name)
        .join(CourseMember, CourseMember.user_id == User.id)
        .join(CourseLink, CourseLink.id == CourseMember.course_link_id)
        .join(LmsConnection, LmsConnection.id == CourseLink.connection_id)
        .where(LmsConnection.teacher_id == teacher_id, User.role == "student"))).all()
    return {uid: (username, name) for uid, username, name in rows}


@router.get("/students")
async def students(course: str | None = Query(default=None, max_length=64),
                   p: Principal = Depends(require_teacher),
                   session: AsyncSession = Depends(get_session)):
    all_exams = await analytics.teacher_exams(session, p.user_id)
    chosen = _filter_exams(all_exams, course, None)
    facts = await analytics.load_facts(session, chosen, with_mistakes=False)
    extra = {} if (course or "").strip() else await _lms_members(session, p.user_id)
    return await asyncio.to_thread(analytics.students_report, facts, extra)


async def _practice_results(session: AsyncSession, student_id: int, teacher_id: int) -> list:
    """This teacher's practice sets sent to the student, with progress (formative; never part
    of grades). Optional: if the practice feature is absent the profile has no practice."""
    try:
        from .practice.models import PracticeAttempt, PracticeSet
    except ImportError:
        return []
    sets = (await session.execute(
        select(PracticeSet).where(PracticeSet.student_id == student_id,
                                  PracticeSet.teacher_id == teacher_id,
                                  PracticeSet.status == "sent")
        .order_by(PracticeSet.sent_at.desc()).limit(50))).scalars().all()
    if not sets:
        return []
    atts: dict[str, dict[str, object]] = {}
    for a in (await session.execute(select(PracticeAttempt).where(
            PracticeAttempt.set_id.in_([x.id for x in sets])))).scalars().all():
        atts.setdefault(a.set_id, {})[a.item_id] = a
    out = []
    for ps in sets:
        ids = {it.get("id") for it in (ps.items or []) if isinstance(it, dict)}
        mine = {k: a for k, a in atts.get(ps.id, {}).items() if k in ids}
        correct = sum(1 for a in mine.values() if a.ever_correct or a.correct)
        out.append({"id": ps.id, "title": ps.title, "topics": list(ps.topics or []),
                    "status": ps.status, "date": iso(ps.sent_at), "total": len(ids),
                    "answered": len(mine), "correct": correct,
                    "percent": round(correct / len(ids) * 100, 1) if ids and mine else None})
    return out


@router.get("/students/{username}/profile")
async def student_profile(username: str, course: str | None = Query(default=None, max_length=64),
                          p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    user = (await session.execute(
        select(User.id, User.username, User.name)
        .where(User.username == username, User.role == "student"))).first()
    if user is None:
        raise HTTPException(404, "Student not found")
    sid = user.id
    all_exams = await analytics.teacher_exams(session, p.user_id)
    related = False
    if all_exams:
        related = (await session.execute(
            select(Attempt.id).where(Attempt.student_id == sid,
                                     Attempt.exam_id.in_([e.id for e in all_exams])).limit(1)
        )).first() is not None
    if not related:
        related = sid in await _lms_members(session, p.user_id)
    if not related:
        # Other teachers' students are invisible (same answer as a missing user).
        raise HTTPException(404, "Student not found")
    chosen = _filter_exams(all_exams, course, None)
    facts = await analytics.load_facts(session, chosen, student_id=sid, with_mistakes=False)
    out = analytics.profile_report(facts, {"username": user.username, "name": user.name})
    out["practice"] = await _practice_results(session, sid, p.user_id)
    out["courses"] = sorted({e.course for e in all_exams if e.course})
    return out
