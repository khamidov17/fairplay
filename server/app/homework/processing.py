"""Student uploads -> flattened pages -> answers per question -> AI drafts (background).

Each submitted photo (or PDF page) is flattened with app.scan.photo (page outline found,
perspective corrected, lighting evened); when no page outline is found the photo is kept
as is (contrast enhanced). Pages are stored as uploads (JPEG, long side <= 2000 px). The
segment adapter then says where each question is answered (fallback: every page), the
student's paper Attempt is (re)written and the Phase 2 grader drafts every answer
(app.routes_scan.draft_answers). Nothing is graded without a teacher.
"""
from __future__ import annotations

import asyncio
import logging
import os

import cv2
import numpy as np
from sqlalchemy import select

from ..db import get_database
from ..grading import exam_questions, grade_attempt, paper_given
from ..models import Attempt, Exam
from ..scan import photo, pipeline
from ..scan.storage import save_file
from ..services import attempt_key, new_id, new_receipt
from ..store import get_store
from ..timeutil import utcnow
from . import segment_adapter
from .models import HomeworkSubmission

log = logging.getLogger("fairplay.homework")

MAX_SIDE = 2000
_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Tests / e2e: wait for background work started in this process."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def _jpeg(img: np.ndarray) -> bytes:
    h, w = img.shape[:2]
    f = MAX_SIDE / max(h, w)
    if f < 1:
        img = cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    for q in (85, 70, 55):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if ok and len(buf) < 4_500_000:
            return buf.tobytes()
    raise ValueError("page image too large")


def flatten_source(path: str) -> list[tuple[bytes, bool]]:
    """(JPEG bytes, flattened?) for every page of one uploaded file. Blocking."""
    out = []
    for i in range(pipeline.source_page_count(path)):
        img = pipeline.load_page(path, i)
        flat = photo.rectify(img)
        if flat is None:
            out.append((_jpeg(photo.enhance(img)), False))
        else:
            out.append((_jpeg(flat), True))
    return out


def _flatten_all(paths: list[str]) -> tuple[list[dict], list[bytes]]:
    pages, raw = [], []
    for pth in paths:
        for data, flattened in flatten_source(pth):
            url = save_file(data)
            pages.append({"url": url, "flattened": flattened})
            raw.append(data)
    return pages, raw


def _store_regions(located: dict, pages: list[dict]) -> dict[str, dict]:
    """Paper answer values {qid: {"image", "images", "page", "whole_page", ...}}. Blocking."""
    urls = [p["url"] for p in pages]
    out = {}
    for qid, loc in located.items():
        if loc.whole_page:
            imgs = list(urls)
        else:
            imgs = [save_file(b) for b in loc.images]
        out[qid] = {"image": imgs[0], "images": imgs, "page": loc.page, "whole_page": loc.whole_page,
                    "blank": False, "mcq": None, "marks": None, "homework": True}
    return out


async def process(sub_id: str) -> None:
    db = get_database()
    try:
        async with db.sessionmaker() as s:
            sub = await s.get(HomeworkSubmission, sub_id)
            if sub is None:
                return
            e = await s.get(Exam, sub.exam_id)
            version, sources = sub.version, list(sub.sources or [])
            qs = exam_questions(e)
        pages, raw = await asyncio.to_thread(_flatten_all, sources)
        if not pages:
            raise ValueError("No readable pages")
        located, mode = await asyncio.to_thread(segment_adapter.locate, qs, raw)
        answers = await asyncio.to_thread(_store_regions, located, pages)
        for pth in sources:
            pipeline.safe_remove(pth)
        async with db.sessionmaker() as s:
            sub = await s.get(HomeworkSubmission, sub_id)
            if sub is None or sub.version != version:
                return  # resubmitted meanwhile: the newer upload wins
            e = await s.get(Exam, sub.exam_id)
            pairs = await write_attempt(s, e, sub, answers)
            sub.pages, sub.sources, sub.segmented = pages, [], mode
            sub.status = "grading"
            await s.commit()
        from ..routes_scan import draft_answers  # late import (routes_scan imports app.scan)
        await draft_answers(e.id, pairs)
        async with db.sessionmaker() as s:
            sub = await s.get(HomeworkSubmission, sub_id)
            if sub is not None and sub.version == version:
                sub.status, sub.processed_at = "graded", utcnow()
                await s.commit()
    except Exception as ex:  # noqa: BLE001 - report on the submission, never crash
        log.exception("homework submission %s failed", sub_id)
        async with db.sessionmaker() as s:
            sub = await s.get(HomeworkSubmission, sub_id)
            if sub is not None:
                sub.status = "failed"
                sub.error = ("We could not read these photos — take them again in good light"
                             if isinstance(ex, ValueError) else f"{ex.__class__.__name__}")[:500]
                for pth in sub.sources or []:
                    pipeline.safe_remove(pth)
                sub.sources = []
                await s.commit()


async def write_attempt(session, e: Exam, sub: HomeworkSubmission, answers: dict[str, dict]) -> list[tuple[str, str]]:
    """Replace the student's paper answers with this submission (grades and drafts of the old
    pages are dropped: it is new work). Returns (attempt, question) pairs to draft. No commit."""
    now = utcnow()
    qs = exam_questions(e)
    a = (await session.execute(
        select(Attempt).where(Attempt.exam_id == e.id, Attempt.student_id == sub.student_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if a is None:
        a = Attempt(id=new_id("a"), exam_id=e.id, student_id=sub.student_id, status="submitted",
                    started_at=sub.submitted_at, deadline_at=sub.submitted_at, submitted_at=sub.submitted_at,
                    answers={}, client_seq=0, receipt=new_receipt(), source="paper", grades={}, ai_drafts={},
                    overall_comment="", needs_grading=0, updated_at=now)
        session.add(a)
    a.answers = dict(answers)
    a.grades, a.ai_drafts = {}, {}
    a.source = "paper"
    a.status, a.submitted_at, a.late = "submitted", sub.submitted_at, bool(sub.late)
    a.receipt = a.receipt or new_receipt()
    score, pending, _ = grade_attempt(qs, a.answers, a.grades)
    a.score, a.needs_grading, a.updated_at = score, pending, now
    sub.attempt_id = a.id
    await session.flush()
    try:
        await get_store().delete(attempt_key(a.id))
    except Exception:  # noqa: BLE001
        pass
    qmap = {q["id"]: q for q in qs}
    return [(a.id, qid) for qid, ans in answers.items() if qid in qmap and paper_given(qmap[qid], ans, None)[1]]


def sources_path(sub_id: str, i: int, kind: str) -> str:
    from ..scan.storage import sources_dir
    return os.path.join(sources_dir(), f"hw_{sub_id}_{i}.{kind}")
