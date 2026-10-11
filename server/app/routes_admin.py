"""Superadmin dashboard API: platform counts and teacher accounts (approve / disable /
enable). Every endpoint requires the superadmin role."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import distinct, func, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import Attempt, Exam, User
from .security import Principal, require_superadmin
from .timeutil import iso, utcnow

router = APIRouter(prefix="/api/admin", tags=["admin"])

# An exam counts as "held" once it has been opened: live now, or closed afterwards.
HELD_STATUSES = ("live", "closed")


def _active_teacher_ids(since):
    """Teachers who created/edited/opened/closed/released an exam (exam timestamps) or
    graded/commented a submission (attempt touched after it was submitted) since `since`."""
    by_exam = select(Exam.teacher_id.label("tid")).where(
        or_(Exam.created_at >= since, Exam.updated_at >= since))
    by_grading = (select(Exam.teacher_id.label("tid"))
                  .join(Attempt, Attempt.exam_id == Exam.id)
                  .where(Attempt.status == "submitted", Attempt.updated_at >= since,
                         Attempt.submitted_at.is_not(None),
                         Attempt.updated_at > Attempt.submitted_at))
    return union(by_exam, by_grading).subquery()


async def _count(session: AsyncSession, stmt) -> int:
    return int((await session.execute(stmt)).scalar() or 0)


@router.get("/stats")
async def stats(_: Principal = Depends(require_superadmin),
                session: AsyncSession = Depends(get_session)):
    users = dict(((r, s), n) for r, s, n in (await session.execute(
        select(User.role, User.status, func.count()).group_by(User.role, User.status))).all())

    def users_with(role: str, status: str | None = None) -> int:
        return sum(n for (r, s), n in users.items() if r == role and (status is None or s == status))

    now = utcnow()
    active = {}
    for days in (7, 30):
        sub = _active_teacher_ids(now - timedelta(days=days))
        active[days] = await _count(session, select(func.count(distinct(sub.c.tid))))
    return {
        "teachers": {"active": users_with("teacher", "active"),
                     "pending": users_with("teacher", "pending"),
                     "disabled": users_with("teacher", "disabled"),
                     "total": users_with("teacher")},
        "students": users_with("student"),
        "exams_created": await _count(session, select(func.count()).select_from(Exam)),
        "exams_held": await _count(session, select(func.count()).select_from(Exam)
                                   .where(Exam.status.in_(HELD_STATUSES))),
        "attempts_submitted": await _count(session, select(func.count()).select_from(Attempt)
                                           .where(Attempt.status == "submitted")),
        "active_teachers_7d": active[7],
        "active_teachers_30d": active[30],
        "generated_at": iso(now),
    }


def _teacher_out(u: User, exams: int = 0, students: int = 0) -> dict:
    return {"id": u.id, "username": u.username, "name": u.name, "email": u.email,
            "status": u.status, "created_at": iso(u.created_at),
            "last_login_at": iso(u.last_login_at) if u.last_login_at else None,
            "exams": exams, "students_reached": students}


@router.get("/teachers")
async def teachers(_: Principal = Depends(require_superadmin),
                   session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(User).where(User.role == "teacher").order_by(User.created_at.desc(), User.id.desc())
    )).scalars().all()
    exams = dict((await session.execute(
        select(Exam.teacher_id, func.count()).group_by(Exam.teacher_id))).all())
    reached = dict((await session.execute(
        select(Exam.teacher_id, func.count(distinct(Attempt.student_id)))
        .join(Attempt, Attempt.exam_id == Exam.id).group_by(Exam.teacher_id))).all())
    # Pending first (they need action), then newest sign-ups.
    rows = sorted(rows, key=lambda u: u.status != "pending")
    return [_teacher_out(u, exams.get(u.id, 0), reached.get(u.id, 0)) for u in rows]


async def _set_status(session: AsyncSession, user_id: int, allowed_from: tuple[str, ...],
                      to: str) -> dict:
    u = await session.get(User, user_id)
    if u is None or u.role != "teacher":
        raise HTTPException(404, "Teacher not found")
    if u.status not in allowed_from:
        raise HTTPException(409, f"Teacher is {u.status}")
    u.status = to
    await session.commit()
    exams = await session.scalar(select(func.count()).select_from(Exam).where(Exam.teacher_id == u.id))
    reached = await session.scalar(
        select(func.count(distinct(Attempt.student_id))).join(Exam, Attempt.exam_id == Exam.id)
        .where(Exam.teacher_id == u.id))
    return _teacher_out(u, int(exams or 0), int(reached or 0))


@router.post("/teachers/{user_id}/approve")
async def approve(user_id: int, _: Principal = Depends(require_superadmin),
                  session: AsyncSession = Depends(get_session)):
    return await _set_status(session, user_id, ("pending",), "active")


@router.post("/teachers/{user_id}/disable")
async def disable(user_id: int, _: Principal = Depends(require_superadmin),
                  session: AsyncSession = Depends(get_session)):
    return await _set_status(session, user_id, ("active", "pending", "disabled"), "disabled")


@router.post("/teachers/{user_id}/enable")
async def enable(user_id: int, _: Principal = Depends(require_superadmin),
                 session: AsyncSession = Depends(get_session)):
    return await _set_status(session, user_id, ("disabled",), "active")
