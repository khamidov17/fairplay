"""Slice 6 D — Scan & Grade API (paper exams).

Print booklets -> upload scans (processed in the background) -> fix unmatched pages ->
review AI drafts (approve / edit / regrade / bulk approve) -> release as usual.
Paper attempts are ordinary Attempt rows (status "submitted", source "paper"), so results,
release, feedback, gradebook and LMS push work unchanged.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import ai
from .db import get_database, get_session
from .grading import exam_questions, grade_attempt, is_answered, num, paper_answer, paper_given, percent
from .models import Attempt, ClassMember, CourseMember, Exam, User
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
_pending: set[str] = set()            # batch ids queued or running in this process
# Audit L-06: batches survive a restart. The worker holding a batch refreshes its heartbeat;
# any server process sweeps for unfinished batches whose heartbeat is stale and resumes them.
UNFINISHED = ("queued", "reading", "grading")
CLAIM_STALE = timedelta(seconds=120)
HEARTBEAT_EVERY = 30.0
SWEEP_EVERY = 60.0
LOST_SOURCE = "the file was lost when the server restarted; upload it again"
# Audit L-03: scan reading is bounded per server process -- one batch at a time on its own
# two threads, so a big upload never takes the threads live exams need.
MAX_BATCH_PAGES = 4000
SCAN_THREADS = 2
_scan_pool = concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_THREADS, thread_name_prefix="scan")
_batch_slots: dict[int, asyncio.Semaphore] = {}


def _slot() -> asyncio.Semaphore:
    """One per event loop (tests run many loops in one process)."""
    key = id(asyncio.get_running_loop())
    if key not in _batch_slots:
        _batch_slots.clear()
        _batch_slots[key] = asyncio.Semaphore(1)
    return _batch_slots[key]


async def _in_scan_pool(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_scan_pool, fn, *args)


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


MAX_TEACHER_STUDENTS = 5000


def teacher_student_ids(teacher_id: int):
    """Audit L-11 (and S-03): the students a teacher works with -- members of their
    classes, of their linked LMS courses, and anyone who took one of their exams.
    Never every student on the server."""
    from .models import CourseLink, LmsConnection, SchoolClass
    in_class = (select(ClassMember.user_id).join(SchoolClass, SchoolClass.id == ClassMember.class_id)
                .where(SchoolClass.teacher_id == teacher_id))
    in_course = (select(CourseMember.user_id).join(CourseLink, CourseLink.id == CourseMember.course_link_id)
                 .join(LmsConnection, LmsConnection.id == CourseLink.connection_id)
                 .where(LmsConnection.teacher_id == teacher_id))
    took = (select(Attempt.student_id).join(Exam, Exam.id == Attempt.exam_id)
            .where(Exam.teacher_id == teacher_id))
    return in_class.union(in_course, took)


async def _all_students(session: AsyncSession, teacher_id: int) -> list[User]:
    return list((await session.execute(
        select(User).where(User.role == "student", User.id.in_(teacher_student_ids(teacher_id)))
        .order_by(User.name, User.username).limit(MAX_TEACHER_STUDENTS))).scalars())


async def roster_of(session: AsyncSession, e: Exam) -> tuple[str, list[User]]:
    picked = (await session.execute(
        select(User).join(ExamRoster, ExamRoster.user_id == User.id)
        .where(ExamRoster.exam_id == e.id).order_by(User.name, User.username))).scalars().all()
    if picked:
        return "picked", list(picked)
    if e.course_link_id:
        members = (await session.execute(
            select(User).join(CourseMember, CourseMember.user_id == User.id)
            .where(CourseMember.course_link_id == e.course_link_id)
            .order_by(User.name, User.username))).scalars().all()
        return "course", list(members)
    if e.class_id:
        members = (await session.execute(
            select(User).join(ClassMember, ClassMember.user_id == User.id)
            .where(ClassMember.class_id == e.class_id, User.role == "student")
            .order_by(User.name, User.username))).scalars().all()
        return "class", list(members)
    return "all", await _all_students(session, e.teacher_id)


# Audit L-08 / L-10: the layout is computed off the event loop and cached per question set;
# booklets come in parts of at most BOOKLETS_PER_PDF students, and never for "every student
# in FairPlay" by default.
BOOKLETS_PER_PDF = 500
_layout_cache: dict[tuple[str, str], int] = {}
_LAYOUT_CACHE_MAX = 256


async def _pages_per_student(exam_id: str, qs: list[dict]) -> int:
    key = (exam_id, print_mod.questions_hash(qs))
    hit = _layout_cache.get(key)
    if hit is not None:
        return hit
    layout, _ = await asyncio.to_thread(print_mod.build_layout, exam_id, qs)
    if len(_layout_cache) >= _LAYOUT_CACHE_MAX:
        _layout_cache.pop(next(iter(_layout_cache)))
    _layout_cache[key] = int(layout["page_count"])
    return _layout_cache[key]


def _parts(n: int) -> int:
    return max(1, -(-n // BOOKLETS_PER_PDF))


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
            "all_students": [_user_out(u) for u in await _all_students(session, e.teacher_id)]}


@router.put("/exams/{exam_id}/roster")
async def put_roster(exam_id: str, body: RosterIn, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    """Pick the students who get a booklet. An empty list goes back to the default
    (the linked LMS course, else all students)."""
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
    pages = await _pages_per_student(e.id, qs) if qs and e.mode != "external" else 0
    source, users = await roster_of(session, e)
    return {
        "exam_id": e.id, "delivery": delivery_of(e),
        "printed_at": iso(row.created_at) if row else None,
        "printed_pages": row.page_count if row else None,
        "pages_per_student": pages,
        "layout_changed": bool(row and row.questions_hash != print_mod.questions_hash(qs)),
        "roster_source": source, "student_count": len(users),
        "booklet_parts": _parts(len(users)), "booklets_per_part": BOOKLETS_PER_PDF,
        "needs_roster": source == "all",
        "questions": [{"question_id": q["id"], "index": i, "type": q["type"],
                       "prompt": q.get("prompt", ""), "answer_lines": q.get("answer_lines"),
                       "default_lines": print_mod.DEFAULT_LINES.get(q["type"])}
                      for i, q in enumerate(qs)],
    }


@router.get("/exams/{exam_id}/print.pdf")
async def print_pdf(exam_id: str, blank: int = 0, part: int = 1, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    _printable(e)
    qs = exam_questions(e)
    source, users = await roster_of(session, e)
    parts = _parts(len(users))
    if not blank:
        if source == "all":
            raise HTTPException(409, "Pick the students who get a booklet, or put the exam in a class")
        if not users:
            raise HTTPException(409, "No students to print for — pick students first")
        if not 1 <= part <= parts:
            raise HTTPException(422, f"There are {parts} part(s) of booklets")
        users = users[(part - 1) * BOOKLETS_PER_PDF:part * BOOKLETS_PER_PDF]
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
    suffix = "blank" if blank else ("booklets" if parts == 1 else f"booklets-part{part}-of-{parts}")
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{name}-{suffix}.pdf"', "Cache-Control": "no-store",
        "X-FairPlay-Parts": str(1 if blank else parts)})


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


async def _claim(batch_id: str) -> bool:
    """Take a batch for this process: atomic, so two workers never run the same batch."""
    now = utcnow()
    async with get_database().sessionmaker() as s:
        r = await s.execute(
            update(ScanBatch)
            .where(ScanBatch.id == batch_id, ScanBatch.status.in_(UNFINISHED),
                   or_(ScanBatch.heartbeat_at.is_(None), ScanBatch.heartbeat_at < now - CLAIM_STALE))
            .values(heartbeat_at=now)
            .execution_options(synchronize_session=False))
        await s.commit()
        return (r.rowcount or 0) == 1


@asynccontextmanager
async def _heartbeat(batch_id: str):
    async def beat():
        while True:
            await asyncio.sleep(HEARTBEAT_EVERY)
            try:
                async with get_database().sessionmaker() as s:
                    await s.execute(update(ScanBatch).where(ScanBatch.id == batch_id)
                                    .values(heartbeat_at=utcnow())
                                    .execution_options(synchronize_session=False))
                    await s.commit()
            except Exception:  # noqa: BLE001 - a missed beat only risks a resume elsewhere
                log.warning("heartbeat of scan batch %s failed", batch_id, exc_info=True)

    t = asyncio.get_running_loop().create_task(beat())
    try:
        yield
    finally:
        t.cancel()


async def run_batch(batch_id: str) -> None:
    _pending.add(batch_id)
    try:
        async with _slot():
            if not await _claim(batch_id):
                return              # another worker has it, or it already finished
            async with _heartbeat(batch_id):
                await _run_batch(batch_id)
    finally:
        _pending.discard(batch_id)


async def sweep_batches() -> int:
    """Resume unfinished batches nobody is working on. Returns how many were started."""
    now = utcnow()
    async with get_database().sessionmaker() as s:
        ids = list((await s.execute(select(ScanBatch.id).where(
            ScanBatch.status.in_(UNFINISHED),
            or_(ScanBatch.heartbeat_at < now - CLAIM_STALE,
                (ScanBatch.heartbeat_at.is_(None)) & (ScanBatch.created_at < now - CLAIM_STALE)),
        ))).scalars())
    started = 0
    for bid in ids:
        if bid in _pending or bid in _running:
            continue
        log.info("resuming scan batch %s", bid)
        _spawn(run_batch(bid))
        started += 1
    return started


async def sweep_forever() -> None:
    while True:
        try:
            await sweep_batches()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("scan batch sweep failed", exc_info=True)
        await asyncio.sleep(SWEEP_EVERY)


async def _draft_pairs(session: AsyncSession, e: Exam, batch_id: str, only_missing: bool) -> list[tuple[str, str]]:
    """(attempt, question) pairs from this batch's pages that still want an AI draft: the
    attempt still holds this page's crop and the item is not graded. only_missing skips
    items that already have a draft of this very crop (used when resuming)."""
    pages = (await session.execute(select(ScanPage).where(
        ScanPage.batch_id == batch_id, ScanPage.status.in_(("matched", "assigned"))))).scalars().all()
    qmap = _question_map(e)
    sids = {int(pg.student_id) for pg in pages if pg.student_id is not None}
    attempts = {a.student_id: a for a in (await session.execute(select(Attempt).where(
        Attempt.exam_id == e.id, Attempt.student_id.in_(sids)))).scalars()} if sids else {}
    pairs: list[tuple[str, str]] = []
    for pg in pages:
        a = attempts.get(int(pg.student_id)) if pg.student_id is not None else None
        if a is None:
            continue
        for qid, crop in (pg.crops or {}).items():
            if qid not in qmap or not isinstance(crop, dict):
                continue
            ans = (a.answers or {}).get(qid)
            if not paper_answer(ans) or ans.get("image") != crop.get("image"):
                continue
            if (a.grades or {}).get(qid) is not None or not paper_given(qmap[qid], ans, None)[1]:
                continue
            if only_missing and ((a.ai_drafts or {}).get(qid) or {}).get("image") == ans.get("image"):
                continue
            if (a.id, qid) not in pairs:
                pairs.append((a.id, qid))
    return pairs


async def _run_batch(batch_id: str) -> None:
    db = get_database()
    _running.add(batch_id)
    sources: list[str] = []
    finished = False        # sources are removed only once the batch is done or failed
    try:
        async with db.sessionmaker() as s:
            b = await s.get(ScanBatch, batch_id)
            if b is None:
                return
            e = await s.get(Exam, b.exam_id)
            row = await s.get(PrintLayout, b.exam_id)
            sources = list(b.sources or [])
            if e is None or row is None:
                raise ValueError("The exam or its printed layout is gone")
            layout = row.layout
            resume_grading = b.status == "grading"
            if not resume_grading:
                b.status = "reading"
            known = list(b.source_counts or [])
            names = (b.filename or "").split(", ")
            done_seqs = set((await s.execute(
                select(ScanPage.seq).where(ScanPage.batch_id == batch_id))).scalars())
            await s.commit()

        if resume_grading:
            async with db.sessionmaker() as s:
                pairs = await _draft_pairs(s, e, batch_id, only_missing=True)
                b = await s.get(ScanBatch, batch_id)
                b.ai_total, b.ai_done = len(pairs), 0
                await s.commit()
        else:
            # Audit L-05: one unreadable/encrypted/oversized file is one unreadable page,
            # not a failed batch. L-06: counts are stored, so a resumed run skips stored pages.
            problems: dict[int, str] = {}
            if len(known) == len(sources):
                counts = [int(c) for c in known]
            else:
                counts = []
                for k, pth in enumerate(sources):
                    if not os.path.exists(pth):
                        counts.append(-1)
                        problems[k] = LOST_SOURCE
                        continue
                    try:
                        counts.append(await _in_scan_pool(pipeline.source_page_count, pth))
                    except pipeline.SourceError as ex:
                        counts.append(-1)
                        problems[k] = str(ex)
            total = sum(max(c, 0) for c in counts)
            if total > MAX_BATCH_PAGES:
                raise ValueError(f"{total} pages in one upload; at most {MAX_BATCH_PAGES}. Upload in parts.")
            async with db.sessionmaker() as s:
                b = await s.get(ScanBatch, batch_id)
                b.source_counts = counts
                b.page_count = total + sum(1 for c in counts if c < 0)
                await s.commit()
            seq = 0
            for k, (pth, n) in enumerate(zip(sources, counts)):
                name = names[k] if k < len(names) else "a file"
                for i in range(max(n, 1) if n < 0 else n):
                    if seq not in done_seqs:
                        if n < 0 or not os.path.exists(pth):
                            why = problems.get(k) or (LOST_SOURCE if not os.path.exists(pth)
                                                      else "the file could not be read")
                            res = {"status": "unmatched", "reason": f"{name}: {why}"[:300],
                                   "qr_found": 0, "rotation": 0, "crops": {}}
                        else:
                            res = await _in_scan_pool(pipeline.process_source_page, pth, i, e.id, layout)
                        async with db.sessionmaker() as s:
                            b = await s.get(ScanBatch, batch_id)
                            s.add(_page_row(b, seq, res))
                            b.processed = seq + 1
                            await s.commit()
                    seq += 1
            async with db.sessionmaker() as s:
                pages = list((await s.execute(
                    select(ScanPage).where(ScanPage.batch_id == batch_id).order_by(ScanPage.seq))).scalars())
                e = await s.get(Exam, e.id)
                pairs = await merge_pages(s, e, [pg for pg in pages if pg.status == "matched"])
                await _suggest_names(s, e, [pg for pg in pages if pg.status == "unmatched" and pg.name_crop])
                b = await s.get(ScanBatch, batch_id)
                b.status = "grading"
                b.processed = max(b.processed, seq)
                b.ai_total = len(pairs)
                b.ai_done = 0
                await s.commit()
        await draft_answers(e.id, pairs, batch_id=batch_id)
        async with db.sessionmaker() as s:
            b = await s.get(ScanBatch, batch_id)
            if b is not None:
                b.status = "done"
                b.finished_at = utcnow()
                await s.commit()
        finished = True
    except Exception as ex:  # noqa: BLE001 - report on the batch, never crash the server
        log.exception("scan batch %s failed", batch_id)
        finished = True
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
        if finished:
            for pth in sources:      # audit L-05: never leave uploaded sources on disk
                pipeline.safe_remove(pth)
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


async def _unmerge_page(session: AsyncSession, e: Exam, pg: ScanPage) -> None:
    """Take this page's answers back out of its current student's paper attempt (only the
    answers that are this page's crops), drop their drafts/grades and regrade. A paper
    attempt left without any answer is removed (that student handed nothing in)."""
    a = (await session.execute(
        select(Attempt).where(Attempt.exam_id == e.id, Attempt.student_id == int(pg.student_id))
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if a is None:
        return
    answers, drafts, grades = dict(a.answers or {}), dict(a.ai_drafts or {}), dict(a.grades or {})
    for qid, crop in (pg.crops or {}).items():
        if qid.startswith("_"):
            continue
        cur = answers.get(qid)
        if isinstance(cur, dict) and isinstance(crop, dict) and cur.get("image") == crop.get("image"):
            answers.pop(qid, None)
            drafts.pop(qid, None)
            grades.pop(qid, None)
    if not answers and (a.source or "computer") == "paper":
        await session.delete(a)
    else:
        a.answers, a.ai_drafts, a.grades = answers, drafts, grades
        score, pending, _ = grade_attempt(exam_questions(e), answers, grades)
        a.score, a.needs_grading, a.updated_at = score, pending, utcnow()
    await session.flush()
    await get_store().delete(attempt_key(a.id))


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
    if status in UNFINISHED and b.id not in _pending and b.id not in _running and \
            (utcnow() - b.heartbeat_at > CLAIM_STALE if b.heartbeat_at else utcnow() - b.created_at > STALE_AFTER):
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
    # Audit L-12: a page that already belonged to another student leaves their paper first,
    # so the answers are not credited twice.
    for target, _n in targets:
        if target.student_id and int(target.student_id) != u.id and target.status in ("matched", "assigned"):
            await _unmerge_page(session, e, target)
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
    if b.id in _running or b.id in _pending:
        raise HTTPException(409, "This batch is still being processed")
    pairs = await _draft_pairs(session, e, b.id, only_missing=False)
    now = utcnow()
    # Audit L-06: take the batch atomically (no other worker is on it) and mark it ours.
    r = await session.execute(
        update(ScanBatch)
        .where(ScanBatch.id == b.id,
               or_(ScanBatch.status.notin_(UNFINISHED), ScanBatch.heartbeat_at.is_(None),
                   ScanBatch.heartbeat_at < now - CLAIM_STALE))
        .values(status="grading", heartbeat_at=now, ai_total=len(pairs), ai_done=0,
                error=None, finished_at=None)
        .execution_options(synchronize_session=False))
    await session.commit()
    if (r.rowcount or 0) != 1:
        raise HTTPException(409, "This batch is still being processed")
    bid, eid = b.id, e.id

    async def job():
        _pending.add(bid)
        _running.add(bid)
        ok, err = False, None
        try:
            async with _heartbeat(bid):
                await draft_answers(eid, pairs, batch_id=bid)
            ok = True
        except Exception as ex:  # noqa: BLE001
            log.exception("re-drafting scan batch %s failed", bid)
            err = f"{ex.__class__.__name__}: {ex}"[:500]
        finally:
            _running.discard(bid)
            _pending.discard(bid)
            try:
                async with get_database().sessionmaker() as s:
                    bb = await s.get(ScanBatch, bid)
                    if bb is not None:
                        bb.status = "done" if ok else "failed"
                        bb.error = None if ok else err
                        bb.finished_at = utcnow()
                        await s.commit()
            except Exception:  # noqa: BLE001
                log.exception("could not record the end of re-drafting %s", bid)

    _spawn(job())
    return {"batch_id": bid, "queued": len(pairs)}


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
    # Audit L-11: building the queue is pure CPU over every answer: off the event loop.
    return await asyncio.to_thread(_build_queue, e, qs, rows)


def _build_queue(e: Exam, qs: list[dict], rows) -> dict:
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
