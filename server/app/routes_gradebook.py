"""Slice 2 teacher overviews: grading summary (/api/grading) and gradebook (/api/gradebook[.csv]).

Teacher reports, not the student hot path: each request is a handful of indexed queries
scoped to this teacher's exams.
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .grading import exam_questions, grade_attempt, num, percent
from .models import Attempt, Exam, User
from .security import Principal, require_teacher

grading_router = APIRouter(prefix="/api/grading", tags=["teacher"])
gradebook_router = APIRouter(prefix="/api", tags=["teacher"])


@grading_router.get("")
async def grading_overview(p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Exam.id, Exam.title, Exam.course, Exam.status, Exam.grades_released,
               func.count(Attempt.id), func.coalesce(func.sum(Attempt.needs_grading), 0))
        .join(Attempt, Attempt.exam_id == Exam.id)
        .where(Exam.teacher_id == p.user_id, Attempt.status == "submitted")
        .group_by(Exam.id, Exam.title, Exam.course, Exam.status, Exam.grades_released,
                  Exam.created_at)
        .order_by(Exam.created_at.desc(), Exam.id))).all()
    return [{"exam_id": eid, "title": title, "course": course or "", "status": status,
             "pending": int(pending or 0), "submitted": int(n), "grades_released": bool(rel)}
            for eid, title, course, status, rel, n, pending in rows]


async def _gradebook(session: AsyncSession, p: Principal, course: str | None) -> dict:
    course = (course or "").strip()
    all_exams = list((await session.execute(
        select(Exam).where(Exam.teacher_id == p.user_id, Exam.status != "draft")
        .order_by(Exam.created_at, Exam.id))).scalars())
    courses = sorted({e.course for e in all_exams if e.course})
    exams = [e for e in all_exams if not course or e.course == course]
    exam_by_id = {e.id: e for e in exams}

    students = (await session.execute(
        select(User.id, User.username, User.name).where(User.role == "student")
        .order_by(User.name, User.username))).all()

    # (student_id, exam_id) -> cell, from every attempt of the selected exams.
    cells: dict[tuple[int, str], dict] = {}
    if exams:
        attempts = (await session.execute(
            select(Attempt.student_id, Attempt.exam_id, Attempt.status, Attempt.answers,
                   Attempt.grades)
            .where(Attempt.exam_id.in_(list(exam_by_id))))).all()
        for sid, eid, status, answers, grades in attempts:
            e = exam_by_id[eid]
            if status != "submitted":
                cells[(sid, eid)] = {"state": "in_progress", "score": None, "percent": None}
                continue
            score, pending, _ = grade_attempt(exam_questions(e), answers, grades)
            cells[(sid, eid)] = {"state": "needs_grading" if pending else "graded",
                                 "score": num(score), "percent": percent(score, e.total_points)}

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
                    p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    return await _gradebook(session, p, course)


@gradebook_router.get("/gradebook.csv")
async def gradebook_csv(course: str | None = Query(default=None, max_length=64),
                        p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    gb = await _gradebook(session, p, course)
    buf = io.StringIO()
    w = csv.writer(buf)
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
