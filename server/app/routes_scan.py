"""Slice 6 D — Scan & Grade API (paper exams).

Print booklets -> upload scans (processed in the background) -> fix unmatched pages ->
review AI drafts (approve / edit / regrade / bulk approve) -> release as usual.
Paper attempts are ordinary Attempt rows (status "submitted", source "paper"), so results,
release, feedback, gradebook and LMS push work unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import ai
from .db import get_database, get_session
from .grading import exam_questions, grade_attempt, is_answered, num, paper_answer, paper_given, percent
from .models import Attempt, CourseMember, Exam, User
from .scan import grading as scan_grading
from .scan import pipeline
from .scan import print as print_mod
from .scan.models import ExamRoster, PrintLayout, ScanBatch, ScanPage
from .scan.storage import read_url, sources_dir
from .security import Principal, require_teacher
from .services import attempt_key, invalidate_exam, new_id, new_receipt
from .store import get_store
from .timeutil import iso, utcnow

log = logging.getLogger("fairplay.scan")

router = APIRouter(prefix="/api", tags=["scan"])

MAX_SCAN_BYTES = 200 * 1024 * 1024
MAX_FILES = 200
AI_CONCURRENCY = 4
STALE_AFTER = timedelta(minutes=30)
_tasks: set[asyncio.Task] = set()     # keep background jobs referenced
_running: set[str] = set()            # batch ids running in this process


# ---------------------------------------------------------------- helpers
def delivery_of(e: Exam) -> str:
    d = getattr(e, "delivery", None)
    if d:
        return d
    return "website" if e.mode == "external" else "computer"


async def _own_exam(session: AsyncSession, exam_id: str, p: Principal) -> Exam:
    e = await session.get(Exam, exam_id)
    if e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Exam not found")
    return e


def _printable(e: Exam) -> None:
    if e.mode == "external" or delivery_of(e) == "website":
        raise HTTPException(409, "This exam is taken on another website and cannot be printed")
    if not e.questions:
        raise HTTPException(409, "Add questions before printing")


async def _all_students(session: AsyncSession) -> list[User]:
    return list((await session.execute(
        select(User).where(User.role == "student").order_by(User.name, User.username))).scalars())


async def roster_of(session: AsyncSession, e: Exam) -> tuple[str, list[User]]:
    picked = (await session.execute(
        select(User).join(ExamRoster, ExamRoster.user_id == User.id)
        .where(ExamRoster.exam_id == e.id).order_by(User.name, User.username))).scalars().all()
    if picked:
        return "picked", list(picked)
    # classes: union of the members of the exam's assigned classes (explicit pick wins)
    from .classes.service import class_member_users
    members = await class_member_users(session, e.id)
    if members is not None:
        return "classes", members
    if e.course_link_id:
        members = (await session.execute(
            select(User).join(CourseMember, CourseMember.user_id == User.id)
            .where(CourseMember.course_link_id == e.course_link_id)
            .order_by(User.name, User.username))).scalars().all()
        return "course", list(members)
    return "all", await _all_students(session)


def _user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "name": u.name}


def _question_map(e: Exam) -> dict[str, dict]:
    return {q["id"]: q for q in exam_questions(e)}


# ---------------------------------------------------------------- overview
@router.get("/scan/exams")
async def scan_exams(p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    """Paper exams (and any exam with printed booklets or uploaded scans), newest first."""
    exams = (await session.execute(
        select(Exam).where(Exam.teacher_id == p.user_id).order_by(Exam.created_at.desc(), Exam.id)
    )).scalars().all()
    ids = [e.id for e in exams]
    printed = set((await session.execute(
        select(PrintLayout.exam_id).where(PrintLayout.exam_id.in_(ids)))).scalars()) if ids else set()
    batch_counts = dict((await session.execute(
        select(ScanBatch.exam_id, func.count()).where(ScanBatch.exam_id.in_(ids))
        .group_by(ScanBatch.exam_id))).all()) if ids else {}
    unmatched = dict((await session.execute(
        select(ScanPage.exam_id, func.count()).where(ScanPage.exam_id.in_(ids), ScanPage.status == "unmatched")
        .group_by(ScanPage.exam_id))).all()) if ids else {}
    paper_stats = {eid: (n, pend) for eid, n, pend in (await session.execute(
        select(Attempt.exam_id, func.count(), func.coalesce(func.sum(Attempt.needs_grading), 0))
        .where(Attempt.exam_id.in_(ids), Attempt.source == "paper").group_by(Attempt.exam_id))).all()} \
        if ids else {}
    out = []
    for e in exams:
        if delivery_of(e) != "paper" and e.id not in printed and e.id not in batch_counts:
            continue
        n, pend = paper_stats.get(e.id, (0, 0))
        out.append({"exam_id": e.id, "title": e.title, "course": e.course or "", "status": e.status,
                    "delivery": delivery_of(e), "question_count": e.question_count,
                    "total_points": num(e.total_points), "printed": e.id in printed,
                    "batches": int(batch_counts.get(e.id, 0)), "unmatched": int(unmatched.get(e.id, 0)),
                    "paper_attempts": int(n), "to_review": int(pend or 0),
                    "grades_released": bool(e.grades_released), "created_at": iso(e.created_at)})
    return out


# ---------------------------------------------------------------- roster
class RosterIn(BaseModel):
    usernames: list[str] = Field(default_factory=list, max_length=5000)


@router.get("/exams/{exam_id}/roster")
async def get_roster(exam_id: str, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    source, users = await roster_of(session, e)
    return {"exam_id": e.id, "source": source, "students": [_user_out(u) for u in users],
            "all_students": [_user_out(u) for u in await _all_students(session)]}


@router.put("/exams/{exam_id}/roster")
async def put_roster(exam_id: str, body: RosterIn, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    """Pick the students who get a booklet. An empty list goes back to the default
    (members of the assigned classes, else the linked LMS course, else all students)."""
    e = await _own_exam(session, exam_id, p)
    wanted = sorted({u.strip() for u in body.usernames if u and u.strip()})
    users = []
    if wanted:
        users = list((await session.execute(
            select(User).where(User.username.in_(wanted), User.role == "student"))).scalars())
        missing = sorted(set(wanted) - {u.username for u in users})
        if missing:
            raise HTTPException(422, "Unknown students: " + ", ".join(missing[:10]))
    await session.execute(delete(ExamRoster).where(ExamRoster.exam_id == e.id))
    for u in users:
        session.add(ExamRoster(exam_id=e.id, user_id=u.id))
    await session.commit()
    return await get_roster(exam_id, p, session)


# ---------------------------------------------------------------- print
class PrintSettingsIn(BaseModel):
    answer_lines: dict[str, int | None] = Field(default_factory=dict)


@router.put("/exams/{exam_id}/print-settings")
async def put_print_settings(exam_id: str, body: PrintSettingsIn, p: Principal = Depends(require_teacher),
                             session: AsyncSession = Depends(get_session)):
    """Answer space (lines) per question. Changing it after printing changes the layout:
    papers printed earlier can then no longer be cropped correctly."""
    e = await _own_exam(session, exam_id, p)
    _printable(e)
    qs = [dict(q) for q in e.questions or []]
    ids = {q["id"] for q in qs}
    for qid, n in body.answer_lines.items():
        if qid not in ids:
            raise HTTPException(422, f"Unknown question {qid}")
        if n is not None and not 1 <= n <= 40:
            raise HTTPException(422, "Answer space must be 1 to 40 lines")
    for q in qs:
        if q["id"] in body.answer_lines:
            q["answer_lines"] = body.answer_lines[q["id"]]
    e.questions = qs
    e.updated_at = utcnow()
    await session.commit()
    await invalidate_exam(get_store(), e.id)
    return await print_info(exam_id, p, session)


@router.get("/exams/{exam_id}/print")
async def print_info(exam_id: str, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    row = await session.get(PrintLayout, e.id)
    qs = exam_questions(e)
    current, _ = print_mod.build_layout(e.id, qs) if qs and e.mode != "external" else ({"page_count": 0}, None)
    source, users = await roster_of(session, e)
    return {
        "exam_id": e.id, "delivery": delivery_of(e),
        "printed_at": iso(row.created_at) if row else None,
        "printed_pages": row.page_count if row else None,
        "pages_per_student": current["page_count"],
        "layout_changed": bool(row and row.questions_hash != print_mod.questions_hash(qs)),
        "roster_source": source, "student_count": len(users),
        "questions": [{"question_id": q["id"], "index": i, "type": q["type"],
                       "prompt": q.get("prompt", ""), "answer_lines": q.get("answer_lines"),
                       "default_lines": print_mod.DEFAULT_LINES.get(q["type"])}
                      for i, q in enumerate(qs)],
    }


@router.get("/exams/{exam_id}/print.pdf")
async def print_pdf(exam_id: str, blank: int = 0, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    _printable(e)
    qs = exam_questions(e)
    _, users = await roster_of(session, e)
    if not blank and not users:
        raise HTTPException(409, "No students to print for — pick students first")
    # the stored layout is the one scans are read with: (re)build it when questions changed
    row = await session.get(PrintLayout, e.id)
    layout, planned = await asyncio.to_thread(print_mod.build_layout, e.id, qs)
    h = layout["questions_hash"]
    if row is None:
        row = PrintLayout(exam_id=e.id)
        session.add(row)
    if row.questions_hash != h or not row.layout:
        row.layout, row.questions_hash, row.page_count, row.created_at = layout, h, layout["page_count"], utcnow()
        await session.commit()
    exam = {"id": e.id, "title": e.title, "course": e.course or ""}
    students = [{"id": u.id, "name": u.name, "username": u.username} for u in users]
    pdf = await asyncio.to_thread(print_mod.render, exam, planned, students, bool(blank))
    name = "".join(c if c.isalnum() else "-" for c in e.title)[:60].strip("-") or "exam"
    suffix = "blank" if blank else "booklets"
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{name}-{suffix}.pdf"', "Cache-Control": "no-store"})


# ---------------------------------------------------------------- upload + processing
def _sniff(head: bytes) -> str | None:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM":
        return "bmp"
    return None


@router.post("/exams/{exam_id}/scans", status_code=202)
async def upload_scans(exam_id: str, files: list[UploadFile] = File(...),
                       p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    _printable(e)
    if e.status == "live" and delivery_of(e) != "paper":
        raise HTTPException(409, "Close the exam before uploading scans")
    if await session.get(PrintLayout, e.id) is None:
        raise HTTPException(409, "Print the booklets from FairPlay first — scans are read with their layout")
    if not files or len(files) > MAX_FILES:
        raise HTTPException(422, f"Upload 1 to {MAX_FILES} files")
    batch_id = new_id("b")
    paths: list[str] = []
    total = 0
    try:
        for i, f in enumerate(files):
            head = await f.read(16)
            kind = _sniff(head)
            if kind is None:
                raise HTTPException(422, f"{f.filename or 'A file'} is not a PDF or an image")
            path = os.path.join(sources_dir(), f"{batch_id}_{i}.{kind}")
            with open(path, "wb") as out:
                out.write(head)
                total += len(head)
                while chunk := await f.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_SCAN_BYTES:
                        raise HTTPException(413, "Scans can be at most 200 MB per upload")
                    await asyncio.to_thread(out.write, chunk)
            paths.append(path)
    except HTTPException:
        for pth in os.listdir(sources_dir()):
            if pth.startswith(batch_id):
                pipeline.safe_remove(os.path.join(sources_dir(), pth))
        raise
    names = ", ".join(f.filename or "scan" for f in files)
    b = ScanBatch(id=batch_id, exam_id=e.id, teacher_id=p.user_id, status="queued",
                  filename=names[:300], sources=paths, created_at=utcnow())
    session.add(b)
    # A paper exam is "collected" once scans arrive: closed, so grades can be released.
    if e.status != "closed":
        now = utcnow()
        e.status, e.start_code, e.closed_at, e.updated_at = "closed", None, now, now
    await session.commit()
    await invalidate_exam(get_store(), e.id)
    _spawn(run_batch(batch_id))
    return {"batch_id": batch_id}


def _spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _page_row(batch: ScanBatch, seq: int, res: dict) -> ScanPage:
    return ScanPage(batch_id=batch.id, exam_id=batch.exam_id, seq=seq, status=res["status"],
                    student_id=res.get("student_id"), page_no=res.get("page_no"),
                    reason=(res.get("reason") or "")[:300], qr_found=int(res.get("qr_found") or 0),
                    rotation=int(res.get("rotation") or 0), preview=res.get("preview"),
                    name_crop=res.get("name_crop"), source_image=res.get("source_image"),
                    crops=res.get("crops") or {}, created_at=utcnow())


async def run_batch(batch_id: str) -> None:
    db = get_database()
    _running.add(batch_id)
    try:
        async with db.sessionmaker() as s:
            b = await s.get(ScanBatch, batch_id)
            e = await s.get(Exam, b.exam_id) if b else None
            row = await s.get(PrintLayout, b.exam_id) if b else None
            if b is None or e is None or row is None:
                return
            layout = row.layout
            b.status = "reading"
            await s.commit()
            sources = list(b.sources or [])
        counts = await asyncio.to_thread(lambda: [pipeline.source_page_count(pth) for pth in sources])
        async with db.sessionmaker() as s:
            b = await s.get(ScanBatch, batch_id)
            b.page_count = sum(counts)
            await s.commit()
        seq = 0
        for pth, n in zip(sources, counts):
            for i in range(n):
                res = await asyncio.to_thread(pipeline.process_source_page, pth, i, e.id, layout)
                async with db.sessionmaker() as s:
                    b = await s.get(ScanBatch, batch_id)
                    s.add(_page_row(b, seq, res))
                    b.processed = seq + 1
                    await s.commit()
                seq += 1
        for pth in sources:
            pipeline.safe_remove(pth)
        async with db.sessionmaker() as s:
            pages = list((await s.execute(
                select(ScanPage).where(ScanPage.batch_id == batch_id).order_by(ScanPage.seq))).scalars())
            e = await s.get(Exam, b.exam_id)
            pairs = await merge_pages(s, e, [pg for pg in pages if pg.status == "matched"])
            await _suggest_names(s, e, [pg for pg in pages if pg.status == "unmatched" and pg.name_crop])
            b = await s.get(ScanBatch, batch_id)
            b.status = "grading"
            b.ai_total = len(pairs)
            b.ai_done = 0
            await s.commit()
        await draft_answers(e.id, pairs, batch_id=batch_id)
        async with db.sessionmaker() as s:
            b = await s.get(ScanBatch, batch_id)
            b.status = "done"
            b.finished_at = utcnow()
            await s.commit()
    except Exception as ex:  # noqa: BLE001 - report on the batch, never crash the server
        log.exception("scan batch %s failed", batch_id)
        try:
            async with db.sessionmaker() as s:
                b = await s.get(ScanBatch, batch_id)
                if b is not None:
                    b.status = "failed"
                    b.error = f"{ex.__class__.__name__}: {ex}"[:500]
                    b.finished_at = utcnow()
                    await s.commit()
        except Exception:  # noqa: BLE001
            log.exception("could not record failure of batch %s", batch_id)
    finally:
        _running.discard(batch_id)


async def merge_pages(session: AsyncSession, e: Exam, pages: list[ScanPage]) -> list[tuple[str, str]]:
    """Create/merge one submitted paper attempt per student from matched pages.
    Returns (attempt_id, question_id) pairs that need an AI draft. Commits."""
    by_student: dict[int, list[ScanPage]] = {}
    for pg in pages:
        by_student.setdefault(int(pg.student_id), []).append(pg)
    qmap = _question_map(e)
    need: list[tuple[str, str]] = []
    touched: list[str] = []
    now = utcnow()
    for sid, pgs in by_student.items():
        u = await session.get(User, sid)
        if u is None or u.role != "student":
            for pg in pgs:
                pg.status, pg.student_id = "unmatched", None
                pg.reason = "The code names a student who is not in FairPlay"
            continue
        a = (await session.execute(
            select(Attempt).where(Attempt.exam_id == e.id, Attempt.student_id == sid)
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if a is None:
            a = Attempt(id=new_id("a"), exam_id=e.id, student_id=sid, status="submitted",
                        started_at=now, deadline_at=now, submitted_at=now, answers={}, client_seq=0,
                        receipt=new_receipt(), source="paper", grades={}, ai_drafts={},
                        overall_comment="", needs_grading=0, updated_at=now)
            session.add(a)
        answers = dict(a.answers or {})
        drafts = dict(a.ai_drafts or {})
        paper = (a.source or "computer") == "paper"
        for pg in sorted(pgs, key=lambda x: (x.created_at, x.id or 0)):
            for qid, crop in (pg.crops or {}).items():
                if qid not in qmap:
                    continue
                old = answers.get(qid)
                if not paper and is_answered(old) and not paper_answer(old):
                    continue  # a computer answer wins over a stray paper page
                answers[qid] = dict(crop)
                drafts.pop(qid, None)
                if (a.grades or {}).get(qid) is None:
                    _, pend = paper_given(qmap[qid], answers[qid], None)
                    if pend:
                        need.append((a.id, qid))
        a.answers = answers
        a.ai_drafts = drafts
        if a.status != "submitted":
            a.status, a.submitted_at = "submitted", now
            a.receipt = a.receipt or new_receipt()
        score, pending, _ = grade_attempt(exam_questions(e), answers, a.grades)
        a.score, a.needs_grading, a.updated_at = score, pending, now
        touched.append(a.id)
    await session.commit()
    if touched:
        await get_store().delete(*[attempt_key(aid) for aid in touched])
    return need


async def _suggest_names(session: AsyncSession, e: Exam, pages: list[ScanPage]) -> None:
    """Blank copies: ask the AI to read the Name box and suggest a student (never applied)."""
    if not pages or not ai.enabled():
        return
    _, users = await roster_of(session, e)
    norm = {u.id: " ".join(u.name.lower().split()) for u in users}
    for pg in pages:
        if pg.page_no != 1:
            continue
        img = await asyncio.to_thread(read_url, pg.name_crop)
        if img is None:
            continue
        try:
            out = await ai.complete_json(
                "You read a handwritten student name from a scanned exam header. Reply with JSON only.",
                'Return {"name": "the full name as written, or empty if unreadable"}', images=[img],
                max_tokens=100)
        except ai.AIUnavailable:
            return
        name = " ".join(str(out.get("name") or "").lower().split())
        if not name:
            continue
        hits = [uid for uid, n in norm.items() if n == name] or \
               [uid for uid, n in norm.items() if name in n or n in name]
        crops = dict(pg.crops or {})
        crops["_suggest"] = {"name": str(out.get("name"))[:120], "student_id": hits[0] if len(hits) == 1 else None}
        pg.crops = crops
    await session.commit()


async def draft_answers(exam_id: str, pairs: list[tuple[str, str]], batch_id: str | None = None,
                        hint: str = "") -> None:
    """AI drafts for (attempt, question) pairs; writes attempt.ai_drafts. Never grades."""
    if not pairs:
        return
    db = get_database()
    async with db.sessionmaker() as s:
        e = await s.get(Exam, exam_id)
        qmap = _question_map(e)
        aids = sorted({aid for aid, _ in pairs})
        answers = {aid: ans for aid, ans in (await s.execute(
            select(Attempt.id, Attempt.answers).where(Attempt.id.in_(aids)))).all()}
    sem = asyncio.Semaphore(AI_CONCURRENCY)

    async def one(aid: str, qid: str) -> tuple[str, str, dict] | None:
        ans = (answers.get(aid) or {}).get(qid)
        q = qmap.get(qid)
        if not paper_answer(ans) or q is None:
            return None
        async with sem:
            img = await asyncio.to_thread(read_url, ans.get("image"))
            d = await scan_grading.draft_for(q, ans, img, hint)
            d["image"] = ans.get("image")  # the draft belongs to this crop
            return aid, qid, d

    chunk = 16
    for i in range(0, len(pairs), chunk):
        results = [r for r in await asyncio.gather(*(one(a, q) for a, q in pairs[i:i + chunk])) if r]
        by_attempt: dict[str, dict[str, dict]] = {}
        for aid, qid, d in results:
            by_attempt.setdefault(aid, {})[qid] = d
        async with db.sessionmaker() as s:
            for aid, ds in by_attempt.items():
                a = (await s.execute(select(Attempt).where(Attempt.id == aid).with_for_update()
                                     .execution_options(populate_existing=True))).scalar_one_or_none()
                if a is None:
                    continue
                merged = dict(a.ai_drafts or {})
                cur = a.answers or {}
                for qid, d in ds.items():
                    if (cur.get(qid) or {}).get("image") == d.get("image"):  # not re-scanned meanwhile
                        merged[qid] = d
                a.ai_drafts = merged
            if batch_id:
                b = await s.get(ScanBatch, batch_id)
                if b is not None:
                    b.ai_done = min(b.ai_total, b.ai_done + len(pairs[i:i + chunk]))
            await s.commit()


# ---------------------------------------------------------------- batches + unmatched pages
def _batch_out(b: ScanBatch, counts: dict[str, int]) -> dict:
    status = b.status
    if status in ("queued", "reading", "grading") and b.id not in _running and \
            utcnow() - b.created_at > STALE_AFTER:
        status = "stalled"
    return {"batch_id": b.id, "status": status, "filename": b.filename, "page_count": b.page_count,
            "processed": b.processed, "matched": counts.get("matched", 0) + counts.get("assigned", 0),
            "unmatched": counts.get("unmatched", 0), "ignored": counts.get("ignored", 0),
            "ai_total": b.ai_total, "ai_done": b.ai_done, "error": b.error,
            "created_at": iso(b.created_at), "finished_at": iso(b.finished_at)}


@router.get("/exams/{exam_id}/scans")
async def list_batches(exam_id: str, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    batches = (await session.execute(
        select(ScanBatch).where(ScanBatch.exam_id == e.id).order_by(ScanBatch.created_at.desc()))).scalars().all()
    counts: dict[str, dict[str, int]] = {}
    for bid, st, n in (await session.execute(
            select(ScanPage.batch_id, ScanPage.status, func.count()).where(ScanPage.exam_id == e.id)
            .group_by(ScanPage.batch_id, ScanPage.status))).all():
        counts.setdefault(bid, {})[st] = n
    covered = (await session.execute(
        select(func.count(func.distinct(ScanPage.student_id))).where(
            ScanPage.exam_id == e.id, ScanPage.status.in_(("matched", "assigned"))))).scalar() or 0
    _, users = await roster_of(session, e)
    row = await session.get(PrintLayout, e.id)
    return {"exam_id": e.id, "ai_enabled": ai.enabled(),
            "pages_per_student": row.page_count if row else None,
            "students_expected": len(users), "students_scanned": int(covered),
            "unmatched": sum(c.get("unmatched", 0) for c in counts.values()),
            "batches": [_batch_out(b, counts.get(b.id, {})) for b in batches]}


def _page_out(pg: ScanPage) -> dict:
    sug = (pg.crops or {}).get("_suggest") or {}
    return {"page_id": pg.id, "batch_id": pg.batch_id, "seq": pg.seq, "status": pg.status,
            "reason": pg.reason, "preview": pg.preview, "name_crop": pg.name_crop,
            "qr_found": pg.qr_found, "page_no": pg.page_no, "student_id": pg.student_id,
            "suggested_name": sug.get("name"), "suggested_student_id": sug.get("student_id"),
            "can_crop": bool(pg.source_image or [k for k in (pg.crops or {}) if not k.startswith("_")])}


@router.get("/exams/{exam_id}/scans/unmatched")
async def unmatched_pages(exam_id: str, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    pages = (await session.execute(
        select(ScanPage).where(ScanPage.exam_id == e.id, ScanPage.status == "unmatched")
        .order_by(ScanPage.created_at, ScanPage.batch_id, ScanPage.seq))).scalars().all()
    return [_page_out(pg) for pg in pages]


class AssignIn(BaseModel):
    student_id: int
    page_no: int = Field(ge=1, le=500)
    # blank copies: also give the following unnamed pages of the same booklet to this student
    follow_on: bool = True


@router.post("/exams/{exam_id}/scans/pages/{page_id}/assign")
async def assign_page(exam_id: str, page_id: int, body: AssignIn, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    pg = await session.get(ScanPage, page_id)
    if pg is None or pg.exam_id != e.id:
        raise HTTPException(404, "Page not found")
    u = await session.get(User, body.student_id)
    if u is None or u.role != "student":
        raise HTTPException(422, "Pick a student")
    row = await session.get(PrintLayout, e.id)
    if row is None or body.page_no > row.page_count:
        raise HTTPException(422, f"The booklet has {row.page_count if row else 0} pages")
    targets = [(pg, body.page_no)]
    if body.follow_on and pg.page_no == 1 and pg.student_id in (None, 0) and pg.qr_found:
        # unnamed booklet: the next pages in the same upload that are pages 2, 3, ... of a
        # blank copy belong to the same student
        nxt = (await session.execute(
            select(ScanPage).where(ScanPage.batch_id == pg.batch_id, ScanPage.seq > pg.seq)
            .order_by(ScanPage.seq).limit(row.page_count - 1))).scalars().all()
        expect = 2
        for other in nxt:
            if other.status != "unmatched" or other.page_no != expect or not other.qr_found:
                break
            targets.append((other, expect))
            expect += 1
    for target, page_no in targets:
        crops = {k: v for k, v in (target.crops or {}).items() if not k.startswith("_")}
        if target.page_no != page_no or not crops:
            if not target.source_image:
                raise HTTPException(409, "This page cannot be cropped — scan it again")
            crops = await asyncio.to_thread(pipeline.crops_for_assignment, target.source_image, row.layout, page_no)
        target.crops = crops
        target.status, target.student_id, target.page_no = "assigned", u.id, page_no
        target.reason = "Assigned by hand"
    await session.commit()
    pairs = await merge_pages(session, e, [t for t, _ in targets])
    _spawn(draft_answers(e.id, pairs))
    return {"assigned": [{"page_id": t.id, "page_no": n, "student_id": u.id} for t, n in targets]}


@router.post("/exams/{exam_id}/scans/pages/{page_id}/ignore")
async def ignore_page(exam_id: str, page_id: int, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    pg = await session.get(ScanPage, page_id)
    if pg is None or pg.exam_id != e.id:
        raise HTTPException(404, "Page not found")
    if pg.status != "unmatched":
        raise HTTPException(409, "Only unmatched pages can be ignored")
    pg.status = "ignored"
    await session.commit()
    return _page_out(pg)


@router.post("/exams/{exam_id}/scans/{batch_id}/rerun-ai", status_code=202)
async def rerun_ai(exam_id: str, batch_id: str, p: Principal = Depends(require_teacher),
                   session: AsyncSession = Depends(get_session)):
    """Draft again every not-yet-approved answer from this batch (e.g. after turning AI on)."""
    e = await _own_exam(session, exam_id, p)
    b = await session.get(ScanBatch, batch_id)
    if b is None or b.exam_id != e.id:
        raise HTTPException(404, "Batch not found")
    if b.id in _running:
        raise HTTPException(409, "This batch is still being processed")
    pages = (await session.execute(select(ScanPage).where(
        ScanPage.batch_id == b.id, ScanPage.status.in_(("matched", "assigned"))))).scalars().all()
    qmap = _question_map(e)
    sids = {pg.student_id for pg in pages}
    attempts = {a.student_id: a for a in (await session.execute(select(Attempt).where(
        Attempt.exam_id == e.id, Attempt.student_id.in_(sids)))).scalars()} if sids else {}
    pairs: list[tuple[str, str]] = []
    for pg in pages:
        a = attempts.get(pg.student_id)
        if a is None:
            continue
        for qid in (pg.crops or {}):
            ans = (a.answers or {}).get(qid)
            if qid in qmap and paper_answer(ans) and (a.grades or {}).get(qid) is None and \
                    paper_given(qmap[qid], ans, None)[1] and (a.id, qid) not in pairs:
                pairs.append((a.id, qid))
    b.status, b.ai_total, b.ai_done = "grading", len(pairs), 0
    await session.commit()

    async def job():
        _running.add(b.id)
        try:
            await draft_answers(e.id, pairs, batch_id=b.id)
        finally:
            _running.discard(b.id)
            async with get_database().sessionmaker() as s:
                bb = await s.get(ScanBatch, b.id)
                bb.status, bb.finished_at = "done", utcnow()
                await s.commit()

    _spawn(job())
    return {"batch_id": b.id, "queued": len(pairs)}


# ---------------------------------------------------------------- AI grading queue
def _item_status(q: dict, ans: dict, grade: dict | None) -> str:
    if grade is not None:
        return "approved"
    return "needs_review" if paper_given(q, ans, None)[1] else "auto"


@router.get("/exams/{exam_id}/scan/queue")
async def scan_queue(exam_id: str, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    qs = exam_questions(e)
    rows = (await session.execute(
        select(Attempt, User.username, User.name).join(User, User.id == Attempt.student_id)
        .where(Attempt.exam_id == e.id, Attempt.status == "submitted")
        .order_by(User.name, User.username))).all()
    stats = {q["id"]: {"total": 0, "to_review": 0, "approved": 0, "auto": 0, "low_confidence": 0} for q in qs}
    items: list[dict] = []
    groups: dict[tuple[str, str], dict] = {}
    for a, username, name in rows:
        answers, grades, drafts = a.answers or {}, a.grades or {}, a.ai_drafts or {}
        for q in qs:
            qid = q["id"]
            ans = answers.get(qid)
            if not paper_answer(ans):
                continue
            g = grades.get(qid)
            d = drafts.get(qid)
            status = _item_status(q, ans, g)
            st = stats[qid]
            st["total"] += 1
            st[{"approved": "approved", "auto": "auto", "needs_review": "to_review"}[status]] += 1
            low = bool(d and d.get("low_confidence"))
            st["low_confidence"] += int(low and status == "needs_review")
            given, _ = paper_given(q, ans, None)
            auto = None
            if status == "auto":
                auto = num(q.get("points", 1)) if q.get("type") == "mcq" and given == q.get("correct_index") else 0
            key = scan_grading.group_key(d)
            items.append({
                "attempt_id": a.id, "username": username, "name": name, "question_id": qid,
                "image": ans.get("image"), "page": ans.get("page"), "mcq": ans.get("mcq"),
                "marks": ans.get("marks"), "blank": bool(ans.get("blank")),
                "draft": d, "grade": g, "status": status, "auto_points": auto,
                "low_confidence": low, "group": key,
            })
            if status == "needs_review" and d and key != "none":
                grp = groups.setdefault((qid, key), {"question_id": qid, "key": key,
                                                     "label": scan_grading.group_label(key, d),
                                                     "points": d.get("points"), "items": []})
                grp["items"].append({"attempt_id": a.id, "question_id": qid})
    return {
        "exam_id": e.id, "title": e.title, "course": e.course or "", "status": e.status,
        "grades_released": bool(e.grades_released), "ai_enabled": ai.enabled(),
        "low_confidence_below": scan_grading.LOW_CONFIDENCE,
        "questions": [{"question_id": q["id"], "index": i, "type": q["type"], "prompt": q.get("prompt", ""),
                       "points": q.get("points", 1), "images": list(q.get("images") or []),
                       "options": list(q.get("options") or []),
                       "correct_answer": _correct_answer(q), **stats[q["id"]]} for i, q in enumerate(qs)],
        "items": items,
        "groups": [dict(g, count=len(g["items"])) for g in groups.values() if len(g["items"]) >= 2],
    }


def _correct_answer(q: dict) -> Any:
    t = q.get("type")
    if t == "mcq":
        return q.get("correct_index")
    if t == "numeric":
        return q.get("answer_number")
    if t == "short":
        return (q.get("accepted_answers") or [None])[0]
    return None


class ApproveIn(BaseModel):
    points: float | None = None
    comment: str | None = Field(default=None, max_length=5000)


def _apply_grade(e: Exam, a: Attempt, q: dict, points: float | None, comment: str | None) -> dict:
    """Turn the draft (or the teacher's edit) into a grade on the attempt (no commit)."""
    if a.status != "submitted":
        raise HTTPException(409, "Attempt is not submitted")
    ans = (a.answers or {}).get(q["id"])
    if not paper_answer(ans):
        raise HTTPException(404, "No paper answer for this question")
    d = (a.ai_drafts or {}).get(q["id"]) or {}
    max_points = float(q.get("points", 1))
    if points is None:
        if d.get("source") == "fallback":
            raise HTTPException(409, "There is no AI grade for this answer — enter the points")
        if d:
            points = float(d.get("points") or 0)
        else:
            given, pend = paper_given(q, ans, None)
            if pend:
                raise HTTPException(409, "No AI draft yet — enter the points")
            points = max_points if q.get("type") == "mcq" and given == q.get("correct_index") else 0.0
    if not -1e-9 <= points <= max_points + 1e-9:
        raise HTTPException(422, f"Points must be between 0 and {num(max_points)}")
    grade = {"points": num(points), "comment": (comment if comment is not None else d.get("comment", "")) or "",
             "rubric_scores": None, "graded_at": iso(utcnow()),
             "transcript": d.get("transcript") or "", "source": "scan",
             "from_draft": comment is None and (d.get("points") == num(points))}
    grades = dict(a.grades or {})
    grades[q["id"]] = grade
    score, pending, _ = grade_attempt(exam_questions(e), a.answers, grades)
    a.grades, a.score, a.needs_grading, a.updated_at = grades, score, pending, utcnow()
    return grade


async def _locked_attempt(session: AsyncSession, e: Exam, aid: str) -> Attempt:
    a = (await session.execute(
        select(Attempt).where(Attempt.id == aid, Attempt.exam_id == e.id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Attempt not found")
    return a


def _q(e: Exam, qid: str) -> dict:
    q = _question_map(e).get(qid)
    if q is None:
        raise HTTPException(404, "Question not found")
    return q


@router.post("/exams/{exam_id}/scan/answers/{aid}/{qid}/approve")
async def approve(exam_id: str, aid: str, qid: str, body: ApproveIn | None = None,
                  p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    q = _q(e, qid)
    a = await _locked_attempt(session, e, aid)
    body = body or ApproveIn()
    grade = _apply_grade(e, a, q, body.points, body.comment)
    await session.commit()
    return {"attempt_id": a.id, "question_id": qid, "grade": grade, "score": num(a.score or 0),
            "total_points": num(e.total_points), "percent": percent(a.score or 0, e.total_points),
            "needs_grading": a.needs_grading}


class RegradeIn(BaseModel):
    hint: str = Field(default="", max_length=500)


@router.post("/exams/{exam_id}/scan/answers/{aid}/{qid}/regrade")
async def regrade(exam_id: str, aid: str, qid: str, body: RegradeIn | None = None,
                  p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    """Run the AI again for one answer (the existing grade, if any, is kept)."""
    e = await _own_exam(session, exam_id, p)
    q = _q(e, qid)
    a = await session.get(Attempt, aid)
    if a is None or a.exam_id != e.id:
        raise HTTPException(404, "Attempt not found")
    ans = (a.answers or {}).get(qid)
    if not paper_answer(ans):
        raise HTTPException(404, "No paper answer for this question")
    img = await asyncio.to_thread(read_url, ans.get("image"))
    d = await scan_grading.draft_for(q, ans, img, (body.hint if body else "") or "")
    d["image"] = ans.get("image")
    a = await _locked_attempt(session, e, aid)
    drafts = dict(a.ai_drafts or {})
    drafts[qid] = d
    a.ai_drafts = drafts
    await session.commit()
    return {"attempt_id": a.id, "question_id": qid, "draft": d}


class BulkItem(BaseModel):
    attempt_id: str = Field(max_length=32)
    question_id: str = Field(max_length=64)


class BulkIn(BaseModel):
    items: list[BulkItem] = Field(min_length=1, max_length=1000)


@router.post("/exams/{exam_id}/scan/approve-bulk")
async def approve_bulk(exam_id: str, body: BulkIn, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    """Approve the AI drafts of many answers as they are (answers already graded or
    without a draft are skipped)."""
    e = await _own_exam(session, exam_id, p)
    qmap = _question_map(e)
    by_attempt: dict[str, list[str]] = {}
    for it in body.items:
        by_attempt.setdefault(it.attempt_id, []).append(it.question_id)
    approved = skipped = 0
    for aid, qids in by_attempt.items():
        a = (await session.execute(
            select(Attempt).where(Attempt.id == aid, Attempt.exam_id == e.id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        for qid in dict.fromkeys(qids):
            q = qmap.get(qid)
            d = (a.ai_drafts or {}).get(qid) if a is not None else None
            if a is None or q is None or (a.grades or {}).get(qid) is not None or not d or \
                    d.get("source") == "fallback" or not paper_answer((a.answers or {}).get(qid)):
                skipped += 1
                continue
            try:
                _apply_grade(e, a, q, None, None)
                approved += 1
            except HTTPException:
                skipped += 1
    await session.commit()
    return {"approved": approved, "skipped": skipped}
