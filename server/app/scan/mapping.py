"""Manual page mapping for answer sheets NOT printed by FairPlay (no QR codes).

The teacher uploads the pages in order (`POST /api/exams/{id}/scans?unprinted=1`, or the phone
capture in "not printed" mode), sets "pages per student" (e.g. every 4 pages = one student)
and on which page each question is answered, then assigns a student to each group of pages
(auto-filled in roster order, fixable per page). Each mapped question gets the WHOLE page
image as its answer (`{"image", "page", "whole_page": true}`); the AI is told to grade only
that question on the page. QR-printed booklets stay automatic.

ScanExamConfig.mapping = {"pages_per_student": int, "question_pages": {qid: page_no}}
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..grading import exam_questions
from ..models import Exam, User
from ..security import Principal, require_teacher
from ..timeutil import utcnow
from .answer_key import _own_exam, get_config
from .models import ScanBatch, ScanPage

router = APIRouter(prefix="/api", tags=["scan-ai"])


def settings_of(mapping: dict | None, qs: list[dict]) -> tuple[int, dict[str, int]]:
    m = mapping or {}
    pps = int(m.get("pages_per_student") or 1)
    qp = {q["id"]: int((m.get("question_pages") or {}).get(q["id"]) or 1) for q in qs}
    return pps, {k: min(max(1, v), pps) for k, v in qp.items()}


def crops_for_page(image_url: str, page_no: int, question_pages: dict[str, int]) -> dict:
    """Whole-page answers for every question on this page."""
    return {qid: {"image": image_url, "page": page_no, "blank": False, "whole_page": True, "mcq": None,
                  "marks": None}
            for qid, pno in question_pages.items() if pno == page_no}


async def _map_pages(session: AsyncSession, e: Exam) -> list[ScanPage]:
    return list((await session.execute(
        select(ScanPage).join(ScanBatch, ScanBatch.id == ScanPage.batch_id)
        .where(ScanPage.exam_id == e.id, ScanBatch.mode == "unprinted", ScanPage.status != "ignored")
        .order_by(ScanBatch.created_at, ScanPage.batch_id, ScanPage.seq))).scalars())


async def map_out(session: AsyncSession, e: Exam) -> dict:
    from ..routes_scan import roster_of  # late import (routes_scan imports app.scan)
    qs = exam_questions(e)
    row = await get_config(session, e.id)
    pps, qp = settings_of(row.mapping if row else None, qs)
    _, users = await roster_of(session, e)
    pages = await _map_pages(session, e)
    return {
        "exam_id": e.id, "pages_per_student": pps, "question_pages": qp,
        "questions": [{"question_id": q["id"], "index": i, "prompt": q.get("prompt", ""), "type": q.get("type")}
                      for i, q in enumerate(qs)],
        "students": [{"id": u.id, "username": u.username, "name": u.name} for u in users],
        "pages": [{"page_id": pg.id, "batch_id": pg.batch_id, "seq": pg.seq, "status": pg.status,
                   "preview": pg.preview, "student_id": pg.student_id if pg.status == "assigned" else None,
                   "page_no": pg.page_no if pg.status == "assigned" else None} for pg in pages],
    }


@router.get("/exams/{exam_id}/scans/map")
async def get_map(exam_id: str, p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    return await map_out(session, e)


class MapSettingsIn(BaseModel):
    pages_per_student: int = Field(ge=1, le=60)
    question_pages: dict[str, int] = Field(default_factory=dict)


@router.put("/exams/{exam_id}/scans/map-settings")
async def put_map_settings(exam_id: str, body: MapSettingsIn, p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    ids = {q["id"] for q in exam_questions(e)}
    for qid, n in body.question_pages.items():
        if qid not in ids:
            raise HTTPException(422, f"Unknown question {qid}")
        if not 1 <= n <= body.pages_per_student:
            raise HTTPException(422, f"Each question must be on page 1 to {body.pages_per_student}")
    row = await get_config(session, e.id, create=True)
    row.mapping = {"pages_per_student": body.pages_per_student, "question_pages": dict(body.question_pages)}
    row.updated_at = utcnow()
    await session.commit()
    return await map_out(session, e)


class Assignment(BaseModel):
    page_id: int
    student_id: int | None = None  # None = take the page back (unassigned)
    page_no: int = Field(default=1, ge=1, le=60)


class MapIn(BaseModel):
    assignments: list[Assignment] = Field(min_length=1, max_length=5000)


@router.post("/exams/{exam_id}/scans/map")
async def apply_map(exam_id: str, body: MapIn, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    from ..routes_scan import _spawn, draft_answers, merge_pages  # late import
    e = await _own_exam(session, exam_id, p)
    qs = exam_questions(e)
    row = await get_config(session, e.id)
    pps, qp = settings_of(row.mapping if row else None, qs)
    pages = {pg.id: pg for pg in await _map_pages(session, e)}
    sids = {a.student_id for a in body.assignments if a.student_id is not None}
    students = {u.id for u in (await session.execute(
        select(User).where(User.id.in_(sids), User.role == "student"))).scalars()} if sids else set()
    touched: list[ScanPage] = []
    for a in body.assignments:
        pg = pages.get(a.page_id)
        if pg is None:
            raise HTTPException(404, f"Page {a.page_id} is not a page to map in this exam")
        if a.student_id is None:
            pg.status, pg.student_id, pg.page_no, pg.crops = "unmatched", None, None, {}
            pg.reason = "Not a FairPlay booklet — map this page by hand"
            continue
        if a.student_id not in students:
            raise HTTPException(422, "Pick a student for every page")
        if a.page_no > pps:
            raise HTTPException(422, f"Each student has {pps} pages")
        image = pg.source_image or pg.preview
        if not image:
            raise HTTPException(409, "This page has no picture — upload it again")
        pg.status, pg.student_id, pg.page_no = "assigned", a.student_id, a.page_no
        pg.crops = crops_for_page(image, a.page_no, qp)
        pg.reason = "Mapped by hand"
        touched.append(pg)
    await session.commit()
    pairs = await merge_pages(session, e, touched) if touched else []
    if pairs:
        _spawn(draft_answers(e.id, pairs))
    return {"assigned": len(touched), "students": len({pg.student_id for pg in touched}), "queued": len(pairs)}
