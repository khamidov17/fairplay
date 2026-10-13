"""Slice 2 teacher overviews: grading summary (/api/grading) and gradebook (/api/gradebook[.csv]).

Teacher reports, not the student hot path: each request is a handful of indexed queries
scoped to this teacher's exams.
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .grading import exam_questions, grade_attempt, num, percent
from .models import Attempt, ClassMember, CourseMember, Exam, SchoolClass, User
from .security import Principal, require_teacher

grading_router = APIRouter(prefix="/api/grading", tags=["teacher"])
gradebook_router = APIRouter(prefix="/api", tags=["teacher"])


@grading_router.get("")
async def grading_overview(p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Exam.id, Exam.title, Exam.course, Exam.status, Exam.grades_released, Exam.class_id,
               func.count(Attempt.id), func.coalesce(func.sum(Attempt.needs_grading), 0))
        .join(Attempt, Attempt.exam_id == Exam.id)
        .where(Exam.teacher_id == p.user_id, Attempt.status == "submitted")
        .group_by(Exam.id, Exam.title, Exam.course, Exam.status, Exam.grades_released,
                  Exam.class_id, Exam.created_at)
        .order_by(Exam.created_at.desc(), Exam.id))).all()
    return [{"exam_id": eid, "title": title, "course": course or "", "status": status,
             "class_id": class_id,
             "pending": int(pending or 0), "submitted": int(n), "grades_released": bool(rel)}
            for eid, title, course, status, rel, class_id, n, pending in rows]


async def _gradebook(session: AsyncSession, p: Principal, course: str | None,
                     class_id: int | None = None) -> dict:
    course = (course or "").strip()
    all_exams = list((await session.execute(
        select(Exam).where(Exam.teacher_id == p.user_id, Exam.status != "draft")
        .order_by(Exam.created_at, Exam.id))).scalars())
    courses = sorted({e.course for e in all_exams if e.course})
    exams = [e for e in all_exams if (not course or e.course == course)
             and (class_id is None or e.class_id == class_id)]
    exam_by_id = {e.id: e for e in exams}

    # Audit S-10: stored score / needs_grading (kept by submit, sweep and every grade), and
    # only the students of these exams' classes / LMS courses plus anyone who took one --
    # not every student in FairPlay.
    cells: dict[tuple[int, str], dict] = {}
    student_ids: set[int] = set()
    if exams:
        ids = list(exam_by_id)
        attempts = (await session.execute(
            select(Attempt.id, Attempt.student_id, Attempt.exam_id, Attempt.status,
                   Attempt.score, Attempt.needs_grading, Attempt.late)
            .where(Attempt.exam_id.in_(ids)))).all()
        missing_score = [aid for aid, _, _, st, score, _, _ in attempts if st == "submitted" and score is None]
        recomputed: dict[str, tuple[float, int]] = {}
        for i in range(0, len(missing_score), 500):   # old rows only (before score was stored)
            for aid, eid, answers, grades in (await session.execute(
                    select(Attempt.id, Attempt.exam_id, Attempt.answers, Attempt.grades)
                    .where(Attempt.id.in_(missing_score[i:i + 500])))).all():
                sc, pend, _ = grade_attempt(exam_questions(exam_by_id[eid]), answers, grades)
                recomputed[aid] = (sc, pend)
        for aid, sid, eid, status, score, pending, late in attempts:
            e = exam_by_id[eid]
            student_ids.add(sid)
            if status != "submitted":
                cells[(sid, eid)] = {"state": "in_progress", "score": None, "percent": None}
                continue
            if aid in recomputed:
                score, pending = recomputed[aid]
            score = float(score or 0)
            cells[(sid, eid)] = {"state": "needs_grading" if pending else "graded",
                                 "score": num(score), "percent": percent(score, e.total_points),
                                 "late": bool(late)}
        class_ids = sorted({e.class_id for e in exams if e.class_id and not e.course_link_id})
        link_ids = sorted({e.course_link_id for e in exams if e.course_link_id})
        if class_ids:
            student_ids.update((await session.execute(
                select(ClassMember.user_id).where(ClassMember.class_id.in_(class_ids)))).scalars())
        if link_ids:
            student_ids.update((await session.execute(
                select(CourseMember.user_id).where(CourseMember.course_link_id.in_(link_ids)))).scalars())

    if class_id is not None:            # 2026-10-13: a class's marks list all its members
        student_ids.update((await session.execute(
            select(ClassMember.user_id).where(ClassMember.class_id == class_id))).scalars())

    students = []
    ordered = sorted(student_ids)
    for i in range(0, len(ordered), 1000):
        students += (await session.execute(
            select(User.id, User.username, User.name)
            .where(User.id.in_(ordered[i:i + 1000]), User.role == "student"))).all()
    students.sort(key=lambda r: (r[2] or "", r[1]))

    out_students = []
    for sid, username, name in students:
        row_cells, graded = {}, []
        for e in exams:
            c = cells.get((sid, e.id))
            if c is None:
                c = {"state": "missing" if e.status == "closed" else "not_taken",
                     "score": None, "percent": None}
            if c["state"] == "graded":
                graded.append(c["percent"])
            row_cells[e.id] = c
        out_students.append({
            "username": username, "name": name,
            "average_percent": round(sum(graded) / len(graded), 1) if graded else None,
            "cells": row_cells})
    return {
        "courses": courses,
        "exams": [{"exam_id": e.id, "title": e.title, "course": e.course or "",
                   "total_points": num(e.total_points), "status": e.status,
                   "grades_released": bool(e.grades_released)} for e in exams],
        "students": out_students,
    }


@gradebook_router.get("/gradebook")
async def gradebook(course: str | None = Query(default=None, max_length=64),
                    class_id: int | None = Query(default=None, ge=1),
                    p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    if class_id is not None:
        c = await session.get(SchoolClass, class_id)
        if c is None or c.teacher_id != p.user_id:
            raise HTTPException(404, "Class not found")
    return await _gradebook(session, p, course, class_id)


def csv_cell(v):
    """Audit S-16: text that Excel/Sheets would run as a formula (=HYPERLINK(...), +, -,
    @, tab, CR) gets a leading apostrophe. Numbers stay numbers."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


class _SafeWriter:
    def __init__(self, w):
        self._w = w

    def writerow(self, row):
        self._w.writerow([csv_cell(v) for v in row])


@gradebook_router.get("/gradebook.csv")
async def gradebook_csv(course: str | None = Query(default=None, max_length=64),
                        class_id: int | None = Query(default=None, ge=1),
                        p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    if class_id is not None:
        c = await session.get(SchoolClass, class_id)
        if c is None or c.teacher_id != p.user_id:
            raise HTTPException(404, "Class not found")
    gb = await _gradebook(session, p, course, class_id)
    buf = io.StringIO()
    w = _SafeWriter(csv.writer(buf))
    w.writerow(["Student", "Username", *[e["title"] for e in gb["exams"]], "Average %"])
    for s in gb["students"]:
        row = [s["name"], s["username"]]
        for e in gb["exams"]:
            c = s["cells"][e["exam_id"]]
            row.append(c["percent"] if c["state"] == "graded" else c["state"])
        row.append("" if s["average_percent"] is None else s["average_percent"])
        w.writerow(row)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="gradebook.csv"'})
