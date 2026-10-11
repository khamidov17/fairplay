"""Slice 6 H — practice assignments (formative; never part of grades).

Teacher: /api/practice/...   Student: /api/student/practice/...
Practice data lives in its own tables (app.practice.models) which the gradebook, results,
averages, release and LMS push never read.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import ai, mathcheck
from .db import get_session
from .models import Exam, User
from .practice import generator
from .practice.models import PracticeAttempt, PracticeSet
from .practice.service import (check_answer, correct_answer, progress, result_out, set_summary,
                               student_item, teacher_item)
from .practice.weak import norm_topic, question_topics, teacher_exams, topic_stats_by_student, weak_topics
from .security import Principal, require_student, require_teacher
from .services import new_id
from .timeutil import utcnow

router = APIRouter(prefix="/api/practice", tags=["practice"])
student_router = APIRouter(prefix="/api/student/practice", tags=["practice"])

MAX_ITEMS = 30
MAX_STUDENTS = 200
SOURCE_TYPES = ("mcq", "numeric", "short", "essay")


# ---------------------------------------------------------------- schemas
class GenerateIn(BaseModel):
    username: str | None = Field(default=None, max_length=64)
    usernames: list[str] | None = Field(default=None, max_length=MAX_STUDENTS)
    topics: list[str] | None = Field(default=None, max_length=20)
    count: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def _who(self):
        if not self.username and not self.usernames:
            raise ValueError("Pick at least one student")
        return self


class ItemEditIn(BaseModel):
    prompt: str | None = Field(default=None, min_length=1, max_length=5000)
    options: list[str] | None = Field(default=None, min_length=2, max_length=8)
    correct_index: int | None = None
    answer_number: float | None = None
    tolerance: float | None = Field(default=None, ge=0)
    accepted_answers: list[str] | None = Field(default=None, max_length=10)
    explanation: str | None = Field(default=None, max_length=2000)


class AddItemIn(BaseModel):
    exam_id: str = Field(min_length=1, max_length=32)
    question_id: str = Field(min_length=1, max_length=64)


class AnswerIn(BaseModel):
    answer: Any = None


# ---------------------------------------------------------------- helpers
async def _students(session: AsyncSession, usernames: list[str]) -> dict[str, User]:
    rows = (await session.scalars(select(User).where(User.username.in_(usernames),
                                                     User.role == "student"))).all()
    return {u.username: u for u in rows}


def _sources(exams: list[Exam], topics: list[str]) -> list[dict]:
    want = set(topics)
    out = []
    for e in exams:
        if getattr(e, "mode", "questions") == "external":
            continue
        for q in e.questions or []:
            if q.get("type") not in SOURCE_TYPES:
                continue
            ts = question_topics(q, e)
            if want and not (want & set(ts)):
                continue
            out.append({"q": q, "exam_id": e.id, "exam_title": e.title, "exam": e, "topics": ts})
    return out


def _title(topics: list[str]) -> str:
    if not topics:
        return "Practice"
    t = "Practice: " + ", ".join(topics[:3]) + ("…" if len(topics) > 3 else "")
    return t[:200]


async def _own_set(session: AsyncSession, sid: str, p: Principal) -> PracticeSet:
    s = await session.get(PracticeSet, sid)
    if s is None or s.teacher_id != p.user_id:
        raise HTTPException(404, "Practice set not found")
    return s


async def _attempts(session: AsyncSession, set_ids: list[str]) -> dict[str, dict[str, PracticeAttempt]]:
    out: dict[str, dict[str, PracticeAttempt]] = {sid: {} for sid in set_ids}
    if not set_ids:
        return out
    for a in (await session.scalars(select(PracticeAttempt).where(PracticeAttempt.set_id.in_(set_ids)))).all():
        out.setdefault(a.set_id, {})[a.item_id] = a
    return out


async def _set_full(session: AsyncSession, s: PracticeSet) -> dict:
    student = await session.get(User, s.student_id)
    atts = (await _attempts(session, [s.id]))[s.id]
    out = set_summary(s, student, atts)
    out["items"] = []
    for it in s.items or []:
        row = teacher_item(it)
        a = atts.get(it["id"])
        row["student_result"] = None if a is None else {
            "answer": a.answer, "correct": a.correct, "tries": a.tries, "ever_correct": a.ever_correct}
        out["items"].append(row)
    return out


def _editable(s: PracticeSet) -> None:
    if s.status == "sent":
        raise HTTPException(409, "This set was already sent")


# ---------------------------------------------------------------- teacher
@router.get("/students")
async def practice_students(p: Principal = Depends(require_teacher),
                            session: AsyncSession = Depends(get_session)):
    """Every student with their topic scores and weak topics (computed here from graded answers)."""
    exams = await teacher_exams(session, p.user_id)
    students = (await session.scalars(select(User).where(User.role == "student").order_by(User.name))).all()
    stats = await topic_stats_by_student(session, p.user_id, None, exams)
    topic_count: dict[str, int] = {}
    for s in _sources(exams, []):
        for t in s["topics"]:
            topic_count[t] = topic_count.get(t, 0) + 1
    out = []
    for u in students:
        rows = (stats.get(u.id) or {}).get("topics", [])
        weak, fallback = weak_topics(rows)
        out.append({"username": u.username, "name": u.name, "topics": rows, "weak": weak,
                    "weak_is_fallback": fallback})
    return {"students": out,
            "topics": [{"topic": t, "question_count": n} for t, n in sorted(topic_count.items())],
            "ai_enabled": ai.enabled()}


@router.get("/bank")
async def practice_bank(topic: str | None = None, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    exams = await teacher_exams(session, p.user_id)
    srcs = _sources(exams, [norm_topic(topic)] if topic else [])
    return [{"exam_id": s["exam_id"], "exam_title": s["exam_title"], "question_id": s["q"].get("id"),
             "type": s["q"].get("type"), "prompt": s["q"].get("prompt", ""), "topics": s["topics"]}
            for s in srcs]


@router.post("/generate")
async def practice_generate(body: GenerateIn, p: Principal = Depends(require_teacher),
                            session: AsyncSession = Depends(get_session)):
    names = list(dict.fromkeys(([body.username] if body.username else []) + (body.usernames or [])))
    users = await _students(session, names)
    exams = await teacher_exams(session, p.user_id)
    stats = await topic_stats_by_student(session, p.user_id, [u.id for u in users.values()], exams)
    req_topics = [t for t in dict.fromkeys(norm_topic(t) for t in (body.topics or [])) if t]
    sets, skipped = [], []
    for name in names:
        u = users.get(name)
        if u is None:
            skipped.append({"username": name, "reason": "No such student"})
            continue
        st = stats.get(u.id) or {"topics": [], "wrong": set()}
        if req_topics:
            topics = req_topics
        else:
            weak, _ = weak_topics(st["topics"])
            topics = [r["topic"] for r in weak]
            if not topics:
                skipped.append({"username": name, "reason": "No graded answers yet — pick topics to practise"})
                continue
        srcs = _sources(exams, topics)
        if not srcs:
            skipped.append({"username": name, "reason": "None of your questions have these topics"})
            continue
        items, dropped = await generator.generate_items(srcs, body.count, topics, st["wrong"])
        if not items:
            skipped.append({"username": name, "reason": "Could not make practice items from these questions"})
            continue
        now = utcnow()
        s = PracticeSet(id=new_id("ps"), teacher_id=p.user_id, student_id=u.id, status="draft",
                        title=_title(topics), topics=topics, items=items, formative=True,
                        created_at=now, updated_at=now)
        session.add(s)
        await session.flush()
        full = await _set_full(session, s)
        full["dropped"] = dropped
        sets.append(full)
    await session.commit()
    return {"sets": sets, "skipped": skipped}


@router.get("/sets")
async def practice_sets(status: str | None = None, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    stmt = select(PracticeSet).where(PracticeSet.teacher_id == p.user_id)
    if status:
        stmt = stmt.where(PracticeSet.status == status)
    rows = (await session.scalars(stmt.order_by(PracticeSet.created_at.desc()))).all()
    users = {u.id: u for u in (await session.scalars(
        select(User).where(User.id.in_({s.student_id for s in rows} or {-1})))).all()}
    atts = await _attempts(session, [s.id for s in rows])
    return [set_summary(s, users.get(s.student_id), atts.get(s.id)) for s in rows]


@router.get("/sets/{sid}")
async def practice_set(sid: str, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    return await _set_full(session, await _own_set(session, sid, p))


@router.delete("/sets/{sid}", status_code=204)
async def practice_set_delete(sid: str, p: Principal = Depends(require_teacher),
                              session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, sid, p)
    await session.execute(delete(PracticeAttempt).where(PracticeAttempt.set_id == s.id))
    await session.delete(s)
    await session.commit()
    return Response(status_code=204)


def _validate_item(it: dict) -> None:
    t = it.get("type")
    if t == "mcq":
        opts = it.get("options") or []
        if not (2 <= len(opts) <= 8) or any(not str(o).strip() for o in opts):
            raise HTTPException(422, "Multiple choice needs 2–8 non-empty options")
        ci = it.get("correct_index")
        if not isinstance(ci, int) or not 0 <= ci < len(opts):
            raise HTTPException(422, "Pick the correct option")


async def _save_items(session: AsyncSession, s: PracticeSet, items: list[dict]) -> dict:
    s.items = items
    s.updated_at = utcnow()
    if s.status == "approved":  # any edit needs a fresh approval
        s.status, s.approved_at = "draft", None
    await session.commit()
    return await _set_full(session, s)


@router.put("/sets/{sid}/items/{iid}")
async def practice_item_edit(sid: str, iid: str, body: ItemEditIn, p: Principal = Depends(require_teacher),
                             session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, sid, p)
    _editable(s)
    items = [dict(it) for it in s.items or []]
    idx = next((i for i, it in enumerate(items) if it.get("id") == iid), None)
    if idx is None:
        raise HTTPException(404, "Item not found")
    it = items[idx]
    changed_key = False
    if body.prompt is not None:
        it["prompt"] = body.prompt.strip()
    if body.explanation is not None:
        it["explanation"] = body.explanation.strip()
    if it.get("type") == "mcq":
        if body.options is not None:
            it["options"] = [o.strip()[:500] for o in body.options]
            changed_key = True
        if body.correct_index is not None:
            it["correct_index"] = body.correct_index
            changed_key = True
    if it.get("type") == "numeric":
        if body.answer_number is not None:
            if not math.isfinite(body.answer_number):
                raise HTTPException(422, "Answer must be a number")
            it["answer_number"] = body.answer_number
            changed_key = True
        if body.tolerance is not None:
            it["tolerance"] = body.tolerance
    if it.get("type") == "short" and body.accepted_answers is not None:
        acc = [a.strip()[:200] for a in body.accepted_answers if a.strip()]
        if it.get("math"):
            bad = [a for a in acc if await asyncio.to_thread(mathcheck.try_parse, a) is None]
            if bad:
                raise HTTPException(422, f"SymPy cannot read “{bad[0]}” — write it like 2x+2 or sqrt(2)/2")
        it["accepted_answers"] = acc
        changed_key = True
    _validate_item(it)
    if body.prompt is not None or changed_key:
        it["kind"] = "teacher_edit"
        missing = ((it.get("type") == "numeric" and it.get("answer_number") is None)
                   or (it.get("type") == "short" and not it.get("accepted_answers")))
        it["status"] = "needs_answer" if missing else "ready"
        it["verification"] = None if missing else {"method": "teacher"}
        it["note"] = ""
        it.pop("suggested_answer", None)
    items[idx] = it
    return await _save_items(session, s, items)


@router.delete("/sets/{sid}/items/{iid}")
async def practice_item_delete(sid: str, iid: str, p: Principal = Depends(require_teacher),
                               session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, sid, p)
    _editable(s)
    items = [it for it in s.items or [] if it.get("id") != iid]
    if len(items) == len(s.items or []):
        raise HTTPException(404, "Item not found")
    return await _save_items(session, s, items)


@router.post("/sets/{sid}/items")
async def practice_item_add(sid: str, body: AddItemIn, p: Principal = Depends(require_teacher),
                            session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, sid, p)
    _editable(s)
    if len(s.items or []) >= MAX_ITEMS:
        raise HTTPException(409, f"A set can have at most {MAX_ITEMS} items")
    e = await session.get(Exam, body.exam_id)
    if e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Question not found")
    q = next((q for q in e.questions or [] if q.get("id") == body.question_id), None)
    if q is None or q.get("type") not in SOURCE_TYPES:
        raise HTTPException(404, "Question not found")
    it = await asyncio.to_thread(generator.original_item,
                                 {"q": q, "exam_id": e.id, "exam_title": e.title, "exam": e})
    it["kind"] = "bank"
    return await _save_items(session, s, list(s.items or []) + [it])


@router.post("/sets/{sid}/approve")
async def practice_approve(sid: str, p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, sid, p)
    _editable(s)
    items = s.items or []
    if not items:
        raise HTTPException(409, "Add at least one item first")
    n = sum(1 for it in items if it.get("status") == "needs_answer")
    if n:
        raise HTTPException(409, f"{n} item{'s' if n != 1 else ''} still need{'s' if n == 1 else ''} your answer")
    s.status, s.approved_at, s.updated_at = "approved", utcnow(), utcnow()
    await session.commit()
    return await _set_full(session, s)


@router.post("/sets/{sid}/send")
async def practice_send(sid: str, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, sid, p)
    if s.status == "sent":
        return await _set_full(session, s)
    if s.status != "approved":
        raise HTTPException(409, "Approve the set first")
    s.status, s.sent_at, s.updated_at = "sent", utcnow(), utcnow()
    await session.commit()
    return await _set_full(session, s)


# ---------------------------------------------------------------- student
async def _student_set(session: AsyncSession, sid: str, p: Principal) -> PracticeSet:
    s = await session.get(PracticeSet, sid)
    if s is None or s.student_id != p.user_id or s.status != "sent":
        raise HTTPException(404, "Practice set not found")
    return s


@student_router.get("")
async def student_practice_list(p: Principal = Depends(require_student),
                                session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(PracticeSet).where(
        PracticeSet.student_id == p.user_id, PracticeSet.status == "sent")
        .order_by(PracticeSet.sent_at.desc()))).all()
    atts = await _attempts(session, [s.id for s in rows])
    teachers = {u.id: u for u in (await session.scalars(
        select(User).where(User.id.in_({s.teacher_id for s in rows} or {-1})))).all()}
    out = []
    for s in rows:
        t = teachers.get(s.teacher_id)
        pr = progress(s.items or [], atts.get(s.id, {}))
        out.append({"id": s.id, "title": s.title, "topics": list(s.topics or []), "formative": True,
                    "item_count": len(s.items or []), "answered": pr["answered"], "correct": pr["correct"],
                    "progress": pr, "sent_at": set_summary(s, None)["sent_at"],
                    "teacher_name": t.name if t else ""})
    return out


@student_router.get("/{sid}")
async def student_practice_get(sid: str, p: Principal = Depends(require_student),
                               session: AsyncSession = Depends(get_session)):
    s = await _student_set(session, sid, p)
    atts = (await _attempts(session, [s.id]))[s.id]
    items = []
    for it in s.items or []:
        row = student_item(it)
        row["result"] = result_out(it, atts.get(it["id"]))
        items.append(row)
    return {"id": s.id, "title": s.title, "topics": list(s.topics or []), "formative": True,
            "sent_at": set_summary(s, None)["sent_at"], "items": items,
            "progress": progress(s.items or [], atts)}


@student_router.post("/{sid}/items/{iid}/answer")
async def student_practice_answer(sid: str, iid: str, body: AnswerIn, p: Principal = Depends(require_student),
                                  session: AsyncSession = Depends(get_session)):
    s = await _student_set(session, sid, p)
    it = next((it for it in s.items or [] if it.get("id") == iid), None)
    if it is None:
        raise HTTPException(404, "Item not found")
    given = body.answer
    if isinstance(given, str):
        if len(given) > (20000 if it.get("type") == "essay" else 500):
            raise HTTPException(422, "Answer is too long")
    elif not (given is None or isinstance(given, (int, float))):
        raise HTTPException(422, "Answer must be text or a number")
    verdict = await asyncio.to_thread(check_answer, it, given)
    atts = (await _attempts(session, [s.id]))[s.id]
    a = atts.get(iid)
    if not verdict["readable"]:
        # Unreadable input does not use up a try.
        return {"item_id": iid, "correct": False, "readable": False, "feedback": verdict["feedback"],
                "result": result_out(it, a), "progress": progress(s.items or [], atts)}
    now = utcnow()
    if a is None:
        a = PracticeAttempt(set_id=s.id, student_id=p.user_id, item_id=iid, tries=0, ever_correct=False,
                            created_at=now)
        session.add(a)
    a.answer = given
    a.correct = verdict["correct"]
    a.tries = (a.tries or 0) + 1
    a.ever_correct = bool(a.ever_correct or verdict["correct"] is True)
    a.updated_at = now
    await session.commit()
    atts[iid] = a
    res = result_out(it, a)
    feedback = verdict["feedback"]
    if verdict["correct"] is False:
        ca = res["correct_answer"] if res else None
        if ca is not None:
            shown = ca["text"] if isinstance(ca, dict) else ca
            feedback = f"Not quite. The answer is {shown}."
        else:
            feedback = "Not quite — try again."
    return {"item_id": iid, "correct": verdict["correct"], "readable": True, "feedback": feedback,
            "explanation": res["explanation"] if res else "", "correct_answer": res["correct_answer"] if res else None,
            "tries": a.tries, "result": res, "progress": progress(s.items or [], atts)}


__all__ = ["router", "student_router", "correct_answer"]
