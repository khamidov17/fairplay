"""Homework mode (paper assignments) + progress dashboard API. See CONTRACT.md
"Homework mode and progress".

Teacher  /api/homework...           create / list / detail / settings / worksheet / release
Student  /api/student/homework...   list / detail / submit photos / worksheet
         /api/student/notifications, /api/student/progress
Teacher  /api/progress...           class dashboard, per-student page, notes
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ai, uploads
from ..classes.models import ClassMember, ExamClass, SchoolClass
from ..db import get_session
from ..grading import exam_questions, num, percent
from ..models import Attempt, Exam, IntegrityEvent, User
from ..schemas import ExamIn
from ..security import Principal, require_student, require_teacher
from ..services import invalidate_exam, new_id
from ..store import get_store
from ..timeutil import iso, utcnow
from . import processing, progress, service
from .loader import load_items
from .models import FeedbackSeen, HomeworkAssignment, HomeworkSubmission, ProgressNote

router = APIRouter(prefix="/api/homework", tags=["homework"])
student_router = APIRouter(prefix="/api/student", tags=["homework"])
progress_router = APIRouter(prefix="/api/progress", tags=["homework"])

MAX_PHOTOS = 30
MAX_PHOTO_BYTES = 80 * 1024 * 1024
MAX_WORKSHEET = 20 * 1024 * 1024


def _naive(v: datetime) -> datetime:
    return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo is not None else v


# ---------------------------------------------------------------- helpers
async def _own(session: AsyncSession, exam_id: str, p: Principal) -> tuple[Exam, HomeworkAssignment]:
    hw = await session.get(HomeworkAssignment, exam_id)
    e = await session.get(Exam, exam_id) if hw else None
    if hw is None or e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Homework not found")
    return e, hw


async def _classes_of(session: AsyncSession, exam_id: str) -> list[dict]:
    rows = (await session.execute(
        select(SchoolClass.id, SchoolClass.name, SchoolClass.section).join(
            ExamClass, ExamClass.class_id == SchoolClass.id).where(ExamClass.exam_id == exam_id)
        .order_by(SchoolClass.name))).all()
    return [{"id": i, "name": n, "section": s} for i, n, s in rows]


async def _set_classes(session: AsyncSession, e: Exam, teacher_id: int, class_ids: list[int]) -> None:
    ids = list(dict.fromkeys(class_ids))
    own = set((await session.execute(select(SchoolClass.id).where(
        SchoolClass.id.in_(ids), SchoolClass.teacher_id == teacher_id))).scalars()) if ids else set()
    if set(ids) - own:
        raise HTTPException(404, "Class not found")
    await session.execute(delete(ExamClass).where(ExamClass.exam_id == e.id))
    for cid in ids:
        session.add(ExamClass(exam_id=e.id, class_id=cid))


def _questions_public(e: Exam, teacher: bool) -> list[dict]:
    out = []
    for i, q in enumerate(exam_questions(e)):
        d = {"question_id": q["id"], "index": i, "type": q["type"], "prompt": q.get("prompt", ""),
             "points": q.get("points", 1), "options": list(q.get("options") or []),
             "images": [dict(x) for x in q.get("images") or []]}
        if teacher:
            d["topics"] = list(q.get("topics") or [])
        out.append(d)
    return out


def _worksheet_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(uploads.uploads_dir())), "homework-worksheets")
    os.makedirs(d, exist_ok=True)
    return d


def _hw_out(e: Exam, hw: HomeworkAssignment) -> dict:
    return {"exam_id": e.id, "title": e.title, "course": e.course or "", "due_at": iso(hw.due_at),
            "instructions": hw.instructions or "", "allow_late": bool(hw.allow_late),
            "allow_resubmit": bool(hw.allow_resubmit), "auto_release": bool(hw.auto_release),
            "auto_released_at": iso(hw.auto_released_at),
            "worksheet": {"name": hw.worksheet_name, "type": hw.worksheet_type} if hw.worksheet_path else None,
            "status": e.status, "grades_released": bool(e.grades_released), "released_at": iso(e.released_at),
            "question_count": e.question_count, "total_points": num(e.total_points),
            "created_at": iso(hw.created_at), "past_due": service.is_late(hw)}


async def _student_rows(session: AsyncSession, e: Exam) -> list[dict]:
    users = await service.roster(session, e)
    atts = {a.student_id: a for a in (await session.execute(
        select(Attempt).where(Attempt.exam_id == e.id))).scalars()}
    subs = {s.student_id: s for s in (await session.execute(
        select(HomeworkSubmission).where(HomeworkSubmission.exam_id == e.id))).scalars()}
    # students outside the roster who still have work (e.g. removed from the class) are listed too
    extra_ids = (set(atts) | set(subs)) - {u.id for u in users}
    if extra_ids:
        users = users + list((await session.execute(select(User).where(User.id.in_(extra_ids)))).scalars())
    out = []
    for u in users:
        a, sub = atts.get(u.id), subs.get(u.id)
        st = service.student_status(e, a, sub)
        work = service.has_work(a)
        out.append({
            "id": u.id, "username": u.username, "name": u.name, "status": st,
            "late": bool((sub.late if sub else False) or (a.late if a and work else False)),
            "submitted_at": iso(sub.submitted_at if sub else (a.submitted_at if work else None)),
            "source": "student" if sub else ("scan" if work else None),
            "processing": sub.status if sub and sub.status in ("processing", "grading", "failed") else None,
            "error": sub.error if sub and sub.status == "failed" else None,
            "pages": [pg["url"] for pg in (sub.pages if sub else [])],
            "attempt_id": a.id if work else None,
            "needs_grading": int(a.needs_grading or 0) if work else 0,
            "score": num(a.score or 0) if work and st in ("reviewed", "released") else None,
            "percent": percent(a.score or 0, e.total_points) if work and st in ("reviewed", "released") else None,
        })
    order = {s: i for i, s in enumerate(service.STATUSES)}
    out.sort(key=lambda r: (order.get(r["status"], 0), r["name"].lower(), r["username"]))
    return out


def _counts(rows: list[dict]) -> dict:
    c = {s: 0 for s in service.STATUSES}
    for r in rows:
        c[r["status"]] = c.get(r["status"], 0) + 1
    c["total"] = len(rows)
    c["late"] = sum(1 for r in rows if r["late"])
    c["handed_in"] = len(rows) - c["not_submitted"]
    return c


# ---------------------------------------------------------------- teacher: CRUD
class HomeworkIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    course: str = Field(default="", max_length=64)
    due_at: datetime
    class_ids: list[int] = Field(min_length=1, max_length=50)
    instructions: str = Field(default="", max_length=5000)
    allow_late: bool = True
    allow_resubmit: bool = True
    auto_release: bool = False
    questions: list[dict] = Field(min_length=1, max_length=200)

    @field_validator("due_at")
    @classmethod
    def _due(cls, v: datetime) -> datetime:
        return _naive(v)


class HomeworkPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: datetime | None = None
    class_ids: list[int] | None = Field(default=None, min_length=1, max_length=50)
    instructions: str | None = Field(default=None, max_length=5000)
    allow_late: bool | None = None
    allow_resubmit: bool | None = None
    auto_release: bool | None = None
    questions: list[dict] | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("due_at")
    @classmethod
    def _due(cls, v: datetime | None) -> datetime | None:
        return _naive(v) if v else v


def _exam_in(title: str, course: str, questions: list[dict]) -> ExamIn:
    try:
        return ExamIn.model_validate({"title": title, "duration_min": 60, "course": course,
                                      "delivery": "paper", "questions": questions})
    except ValidationError as ex:
        err = ex.errors()[0]
        msg = str(err.get("msg") or "Invalid homework").removeprefix("Value error, ")
        loc = ".".join(str(x) for x in err.get("loc") or [])
        raise HTTPException(422, f"{msg}" + (f" ({loc})" if loc and "questions" in loc else "")) from None


@router.post("", status_code=201)
async def create_homework(body: HomeworkIn, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    from ..routes_teacher import _apply  # same question normalisation as the exam editor
    ein = _exam_in(body.title, body.course, body.questions)
    now = utcnow()
    e = Exam(id=new_id("e"), teacher_id=p.user_id, status="draft", created_at=now)
    _apply(e, ein, creating=True)
    session.add(e)
    await session.flush()
    await _set_classes(session, e, p.user_id, body.class_ids)
    hw = HomeworkAssignment(exam_id=e.id, teacher_id=p.user_id, due_at=body.due_at,
                            instructions=body.instructions.strip(), allow_late=body.allow_late,
                            allow_resubmit=body.allow_resubmit, auto_release=body.auto_release,
                            created_at=now, updated_at=now)
    session.add(hw)
    await session.commit()
    return await homework_detail(e.id, p, session)


@router.get("")
async def list_homework(class_id: int | None = None, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    stmt = select(HomeworkAssignment, Exam).join(Exam, Exam.id == HomeworkAssignment.exam_id).where(
        Exam.teacher_id == p.user_id)
    if class_id is not None:
        stmt = stmt.where(HomeworkAssignment.exam_id.in_(
            select(ExamClass.exam_id).where(ExamClass.class_id == class_id)))
    rows = (await session.execute(stmt.order_by(HomeworkAssignment.due_at.desc()))).all()
    out = []
    for hw, e in rows:
        srows = await _student_rows(session, e)
        out.append({**_hw_out(e, hw), "classes": await _classes_of(session, e.id), "counts": _counts(srows)})
    return out


@router.get("/{exam_id}")
async def homework_detail(exam_id: str, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    e, hw = await _own(session, exam_id, p)
    if await service.maybe_auto_release(session, e.id):
        e, hw = await _own(session, exam_id, p)
    rows = await _student_rows(session, e)
    return {**_hw_out(e, hw), "classes": await _classes_of(session, e.id),
            "questions": _questions_public(e, teacher=True), "ai_enabled": ai.enabled(),
            "counts": _counts(rows), "pending": sum(r["needs_grading"] for r in rows), "students": rows}


@router.patch("/{exam_id}")
async def update_homework(exam_id: str, body: HomeworkPatch, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    e, hw = await _own(session, exam_id, p)
    if body.questions is not None:
        has = (await session.execute(select(Attempt.id).where(Attempt.exam_id == e.id).limit(1))).first()
        if has:
            raise HTTPException(409, "Students already handed in work — questions can no longer change")
        from ..routes_teacher import _apply
        _apply(e, _exam_in(body.title or e.title, e.course or "", body.questions))
    elif body.title is not None:
        e.title = body.title.strip() or e.title
    if body.class_ids is not None:
        await _set_classes(session, e, p.user_id, body.class_ids)
    for f in ("due_at", "allow_late", "allow_resubmit", "auto_release"):
        v = getattr(body, f)
        if v is not None:
            setattr(hw, f, v)
    if body.instructions is not None:
        hw.instructions = body.instructions.strip()
    hw.updated_at = e.updated_at = utcnow()
    await session.commit()
    await invalidate_exam(get_store(), e.id)
    return await homework_detail(exam_id, p, session)


@router.delete("/{exam_id}", status_code=204)
async def delete_homework(exam_id: str, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    e, hw = await _own(session, exam_id, p)
    await session.execute(delete(HomeworkSubmission).where(HomeworkSubmission.exam_id == e.id))
    await session.execute(delete(FeedbackSeen).where(FeedbackSeen.exam_id == e.id))
    await session.execute(delete(IntegrityEvent).where(IntegrityEvent.exam_id == e.id))
    await session.execute(delete(Attempt).where(Attempt.exam_id == e.id))
    await session.execute(delete(ExamClass).where(ExamClass.exam_id == e.id))
    if hw.worksheet_path:
        processing.pipeline.safe_remove(hw.worksheet_path)
    await session.delete(hw)
    await session.delete(e)
    await session.commit()
    await invalidate_exam(get_store(), exam_id)


# ---------------------------------------------------------------- teacher: worksheet
def _sniff_doc(head: bytes) -> tuple[str, str] | None:
    if head.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    return None


@router.put("/{exam_id}/worksheet")
async def put_worksheet(exam_id: str, file: UploadFile = File(...), p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    e, hw = await _own(session, exam_id, p)
    data = await file.read(MAX_WORKSHEET + 1)
    if len(data) > MAX_WORKSHEET:
        raise HTTPException(413, "The worksheet can be at most 20 MB")
    kind = _sniff_doc(data[:16])
    if kind is None:
        raise HTTPException(422, "Use a PDF, PNG or JPEG file")
    path = os.path.join(_worksheet_dir(), f"{e.id}_{new_id('w')}.{kind[0]}")
    await asyncio.to_thread(_write, path, data)
    if hw.worksheet_path:
        processing.pipeline.safe_remove(hw.worksheet_path)
    hw.worksheet_path, hw.worksheet_type = path, kind[1]
    hw.worksheet_name = (os.path.basename(file.filename or "") or f"worksheet.{kind[0]}")[:200]
    hw.updated_at = utcnow()
    await session.commit()
    return _hw_out(e, hw)


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


@router.delete("/{exam_id}/worksheet")
async def delete_worksheet(exam_id: str, p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    e, hw = await _own(session, exam_id, p)
    if hw.worksheet_path:
        processing.pipeline.safe_remove(hw.worksheet_path)
    hw.worksheet_path, hw.worksheet_name, hw.worksheet_type = None, "", ""
    await session.commit()
    return _hw_out(e, hw)


def _worksheet_response(hw: HomeworkAssignment) -> FileResponse:
    if not hw.worksheet_path or not os.path.isfile(hw.worksheet_path):
        raise HTTPException(404, "No worksheet")
    return FileResponse(hw.worksheet_path, media_type=hw.worksheet_type or "application/octet-stream",
                        filename=hw.worksheet_name or "worksheet",
                        headers={"Cache-Control": "private, no-store"})


@router.get("/{exam_id}/worksheet")
async def get_worksheet(exam_id: str, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    _, hw = await _own(session, exam_id, p)
    return _worksheet_response(hw)


# ---------------------------------------------------------------- teacher: release
class ReleaseIn(BaseModel):
    force: bool = False


@router.post("/{exam_id}/release")
async def release_homework(exam_id: str, body: ReleaseIn | None = None, p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    e, _ = await _own(session, exam_id, p)
    await service.release(session, e, force=bool(body and body.force))
    await session.commit()
    await invalidate_exam(get_store(), e.id)
    return await homework_detail(exam_id, p, session)


@router.post("/{exam_id}/unrelease")
async def unrelease_homework(exam_id: str, p: Principal = Depends(require_teacher),
                             session: AsyncSession = Depends(get_session)):
    e, hw = await _own(session, exam_id, p)
    e.grades_released, e.released_at, e.updated_at = False, None, utcnow()
    hw.auto_release = False  # do not release again behind the teacher's back
    await session.commit()
    await invalidate_exam(get_store(), e.id)
    return await homework_detail(exam_id, p, session)


# ---------------------------------------------------------------- student
def _student_state(e: Exam, hw: HomeworkAssignment, a: Attempt | None, sub: HomeworkSubmission | None,
                   seen: bool) -> dict:
    work = service.has_work(a)
    if sub is not None and sub.status == "processing":
        state = "processing"
    elif sub is not None and sub.status == "failed" and not work:
        state = "failed"
    elif work or (sub is not None and sub.status in ("grading", "graded")):
        state = "feedback" if e.grades_released and work else "handed_in"
    else:
        state = "not_submitted"
    block = service.submit_block(hw, e, sub, a)
    released = bool(e.grades_released) and work
    return {
        "exam_id": e.id, "title": e.title, "course": e.course or "", "due_at": iso(hw.due_at),
        "past_due": service.is_late(hw), "state": state,
        "late": bool((sub.late if sub else False) or (a.late if work else False)),
        "overdue": state == "not_submitted" and service.is_late(hw),
        "submitted_at": iso(sub.submitted_at if sub else (a.submitted_at if work else None)),
        "can_submit": block is None, "block_reason": block,
        "allow_late": bool(hw.allow_late), "allow_resubmit": bool(hw.allow_resubmit),
        "released": released, "feedback_new": released and not seen,
        "score": num(a.score or 0) if released else None, "total_points": num(e.total_points),
        "percent": percent(a.score or 0, e.total_points) if released else None,
        "question_count": e.question_count,
    }


async def _student_ctx(session: AsyncSession, exam_id: str, p: Principal):
    hw = await session.get(HomeworkAssignment, exam_id)
    e = await session.get(Exam, exam_id) if hw else None
    if hw is None or e is None:
        raise HTTPException(404, "Homework not found")
    a = (await session.execute(select(Attempt).where(
        Attempt.exam_id == e.id, Attempt.student_id == p.user_id))).scalar_one_or_none()
    sub = (await session.execute(select(HomeworkSubmission).where(
        HomeworkSubmission.exam_id == e.id, HomeworkSubmission.student_id == p.user_id))).scalar_one_or_none()
    if a is None and sub is None:
        await service.require_member(session, e, p.user_id)
    return e, hw, a, sub


async def _seen_ids(session: AsyncSession, user_id: int) -> set[str]:
    return set((await session.execute(select(FeedbackSeen.exam_id).where(
        FeedbackSeen.student_id == user_id))).scalars())


@student_router.get("/homework")
async def student_homework(p: Principal = Depends(require_student), session: AsyncSession = Depends(get_session)):
    ids = set(await service.student_homework_ids(session, p.user_id))
    # work already handed in stays visible even if the student left the class
    ids |= set((await session.execute(select(HomeworkSubmission.exam_id).where(
        HomeworkSubmission.student_id == p.user_id))).scalars())
    ids |= set((await session.execute(select(Attempt.exam_id).where(
        Attempt.student_id == p.user_id, Attempt.exam_id.in_(select(HomeworkAssignment.exam_id))))).scalars())
    seen = await _seen_ids(session, p.user_id)
    out = []
    for eid in ids:
        e, hw, a, sub = await _student_ctx(session, eid, p)
        out.append({**_student_state(e, hw, a, sub, eid in seen),
                    "class_names": await service.class_names(session, eid, p.user_id)})
    order = {"not_submitted": 0, "failed": 0, "processing": 1, "handed_in": 2, "feedback": 3}
    out.sort(key=lambda r: (order.get(r["state"], 9), r["due_at"] or "", r["title"]))
    return out


@student_router.get("/homework/{exam_id}")
async def student_homework_detail(exam_id: str, p: Principal = Depends(require_student),
                                  session: AsyncSession = Depends(get_session)):
    e, hw, a, sub = await _student_ctx(session, exam_id, p)
    seen = exam_id in await _seen_ids(session, p.user_id)
    st = _student_state(e, hw, a, sub, seen)
    if st["released"] and not seen:
        session.add(FeedbackSeen(exam_id=e.id, student_id=p.user_id, seen_at=utcnow()))
        await session.commit()
    return {**st, "instructions": hw.instructions or "",
            "worksheet": {"name": hw.worksheet_name, "type": hw.worksheet_type} if hw.worksheet_path else None,
            "class_names": await service.class_names(session, exam_id, p.user_id),
            "questions": _questions_public(e, teacher=False),
            "pages": [pg["url"] for pg in (sub.pages if sub else [])],
            "error": sub.error if sub and sub.status == "failed" else None,
            "ai_enabled": ai.enabled()}


@student_router.get("/homework/{exam_id}/worksheet")
async def student_worksheet(exam_id: str, p: Principal = Depends(require_student),
                            session: AsyncSession = Depends(get_session)):
    _, hw, _, _ = await _student_ctx(session, exam_id, p)
    return _worksheet_response(hw)


def _photo_kind(head: bytes) -> str | None:
    from ..scan.photo import is_heic
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if is_heic(head):
        return "heic"
    return None


@student_router.post("/homework/{exam_id}/submit", status_code=202)
async def student_submit(exam_id: str, files: list[UploadFile] = File(...),
                         p: Principal = Depends(require_student), session: AsyncSession = Depends(get_session)):
    """Photos (or a PDF) of the student's own work, in page order. Replaces an earlier
    submission (when resubmitting is allowed); graded in the background."""
    from ..scan.photo import heic_supported
    e, hw, a, sub = await _student_ctx(session, exam_id, p)
    await service.require_member(session, e, p.user_id)
    block = service.submit_block(hw, e, sub, a)
    if block:
        raise HTTPException(409, block)
    if not files or len(files) > MAX_PHOTOS:
        raise HTTPException(422, f"Send 1 to {MAX_PHOTOS} photos")
    sid = sub.id if sub else new_id("hs")
    stamp = new_id("v")
    paths: list[str] = []
    total = 0
    try:
        for i, f in enumerate(files):
            head = await f.read(16)
            kind = _photo_kind(head)
            if kind is None:
                raise HTTPException(422, f"{f.filename or 'A file'} is not a photo (JPEG, PNG, WebP, HEIC) or PDF")
            if kind == "heic" and not heic_supported():
                raise HTTPException(422, "HEIC photos are not supported on this server yet — on the iPhone choose "
                                         "Settings › Camera › Formats › Most Compatible")
            path = processing.sources_path(f"{sid}_{stamp}", i, kind)
            with open(path, "wb") as out:
                out.write(head)
                total += len(head)
                while chunk := await f.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_PHOTO_BYTES:
                        raise HTTPException(413, "Photos can be at most 80 MB in total")
                    await asyncio.to_thread(out.write, chunk)
            paths.append(path)
    except HTTPException:
        for pth in paths:
            processing.pipeline.safe_remove(pth)
        raise
    now = utcnow()
    late = service.is_late(hw, now)
    if sub is None:
        sub = HomeworkSubmission(id=sid, exam_id=e.id, student_id=p.user_id, version=1)
        session.add(sub)
    else:
        sub.version = (sub.version or 1) + 1
        for pth in sub.sources or []:
            processing.pipeline.safe_remove(pth)
    sub.status, sub.sources, sub.pages, sub.error = "processing", paths, [], None
    sub.late, sub.submitted_at, sub.processed_at = late, now, None
    # the first hand-in "collects" the homework: closed, so grades can be released later
    if e.status != "closed":
        e.status, e.start_code, e.closed_at, e.updated_at = "closed", None, now, now
    await session.commit()
    await invalidate_exam(get_store(), e.id)
    processing.spawn(processing.process(sub.id))
    return {"submission_id": sub.id, "pages": len(paths), "late": late, "state": "processing"}


@student_router.get("/notifications")
async def student_notifications(p: Principal = Depends(require_student),
                                session: AsyncSession = Depends(get_session)):
    """In-app only: released homework feedback the student has not opened yet."""
    seen = await _seen_ids(session, p.user_id)
    rows = (await session.execute(
        select(Exam.id, Exam.title, Exam.released_at, Attempt.answers)
        .join(Attempt, Attempt.exam_id == Exam.id)
        .join(HomeworkAssignment, HomeworkAssignment.exam_id == Exam.id)
        .where(Attempt.student_id == p.user_id, Attempt.status == "submitted",
               Exam.grades_released.is_(True)))).all()
    from ..grading import paper_answer
    items = [{"exam_id": eid, "title": t, "released_at": iso(r)} for eid, t, r, ans in rows
             if eid not in seen and any(paper_answer(v) for v in (ans or {}).values())]
    items.sort(key=lambda x: x["released_at"] or "", reverse=True)
    return {"feedback_ready": len(items), "items": items}


@student_router.get("/progress")
async def student_progress(p: Principal = Depends(require_student), session: AsyncSession = Depends(get_session)):
    """My progress: released, teacher-graded work only (never unapproved AI drafts)."""
    items = await load_items(session, teacher_id=None, student_ids=[p.user_id], released_only=True)
    now = utcnow()
    out = progress.student_view(items, now)
    recent = progress.per_assignment(items)[-6:][::-1]
    out["recent"] = [{"exam_id": r["exam_id"], "title": r["title"], "date": iso(r["date"]),
                      "percent": r["percent"],
                      "homework": any(it.kind == "homework" for it in items if it.exam_id == r["exam_id"])}
                     for r in recent]
    return out


# ---------------------------------------------------------------- teacher: progress dashboard
async def _class_students(session: AsyncSession, class_id: int, teacher_id: int) -> tuple[SchoolClass, list[User]]:
    c = await session.get(SchoolClass, class_id)
    if c is None or c.teacher_id != teacher_id:
        raise HTTPException(404, "Class not found")
    users = list((await session.execute(
        select(User).join(ClassMember, ClassMember.user_id == User.id)
        .where(ClassMember.class_id == class_id).order_by(User.name, User.username))).scalars())
    return c, users


def _iso_series(rows: list[dict]) -> list[dict]:
    return [dict(r, date=iso(r["date"])) for r in rows]


@progress_router.get("")
async def class_progress(class_id: int = Query(...), p: Principal = Depends(require_teacher),
                         session: AsyncSession = Depends(get_session)):
    c, users = await _class_students(session, class_id, p.user_id)
    items = await load_items(session, teacher_id=p.user_id, student_ids=[u.id for u in users])
    now = utcnow()
    students = [progress.Student(u.id, u.username, u.name) for u in users]
    out = await asyncio.to_thread(progress.class_dashboard, items, students, now)
    out["class"] = {"id": c.id, "name": c.name, "section": c.section}
    out["generated_at"] = iso(now)
    out["assignments"] = len({it.exam_id for it in items})
    for m in out["misconceptions"]:
        m["students"] = m["students"][:12]
    return out


async def _teacher_student(session: AsyncSession, username: str, teacher_id: int) -> User:
    u = (await session.execute(select(User).where(User.username == username, User.role == "student"))).scalar_one_or_none()
    if u is None:
        raise HTTPException(404, "Student not found")
    in_class = (await session.execute(
        select(ClassMember.id).join(SchoolClass, SchoolClass.id == ClassMember.class_id)
        .where(ClassMember.user_id == u.id, SchoolClass.teacher_id == teacher_id).limit(1))).first()
    has_work = in_class or (await session.execute(
        select(Attempt.id).join(Exam, Exam.id == Attempt.exam_id)
        .where(Attempt.student_id == u.id, Exam.teacher_id == teacher_id).limit(1))).first()
    if not has_work:
        raise HTTPException(404, "Student not found")
    return u


@progress_router.get("/students/{username}")
async def student_progress_detail(username: str, p: Principal = Depends(require_teacher),
                                  session: AsyncSession = Depends(get_session)):
    u = await _teacher_student(session, username, p.user_id)
    items = await load_items(session, teacher_id=p.user_id, student_ids=[u.id])
    now = utcnow()
    d = await asyncio.to_thread(progress.student_detail, items, now)
    d["assignments"] = _iso_series(d["assignments"])
    for t in d["topics"]:
        t["series"] = _iso_series(t["series"])
    for m in d["mistakes"]:
        m["last_seen"] = iso(m["last_seen"])
        for ex in m["examples"]:
            ex["date"] = iso(ex["date"])
    notes = (await session.execute(select(ProgressNote).where(
        ProgressNote.teacher_id == p.user_id, ProgressNote.student_id == u.id)
        .order_by(ProgressNote.created_at.desc()))).scalars()
    classes = (await session.execute(
        select(SchoolClass.id, SchoolClass.name).join(ClassMember, ClassMember.class_id == SchoolClass.id)
        .where(ClassMember.user_id == u.id, SchoolClass.teacher_id == p.user_id))).all()
    return {"student": {"id": u.id, "username": u.username, "name": u.name},
            "classes": [{"id": i, "name": n} for i, n in classes], **d,
            "notes": [{"id": n.id, "text": n.text, "created_at": iso(n.created_at)} for n in notes]}


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@progress_router.post("/students/{username}/notes", status_code=201)
async def add_note(username: str, body: NoteIn, p: Principal = Depends(require_teacher),
                   session: AsyncSession = Depends(get_session)):
    u = await _teacher_student(session, username, p.user_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Write a note first")
    n = ProgressNote(teacher_id=p.user_id, student_id=u.id, text=text, created_at=utcnow())
    session.add(n)
    await session.commit()
    return {"id": n.id, "text": n.text, "created_at": iso(n.created_at)}


@progress_router.delete("/notes/{note_id}", status_code=204)
async def delete_note(note_id: int, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    n = await session.get(ProgressNote, note_id)
    if n is None or n.teacher_id != p.user_id:
        raise HTTPException(404, "Note not found")
    await session.delete(n)
    await session.commit()
