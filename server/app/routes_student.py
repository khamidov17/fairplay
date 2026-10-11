from __future__ import annotations

import json
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .classes.service import require_exam_access, student_exam_visibility
from .db import get_session
from .grading import exam_questions, grade_attempt, is_answered, num, percent
from .models import Attempt, Exam, IntegrityEvent
from .schemas import AnswersIn, EventsIn, HeartbeatIn, StartIn
from .security import Principal, require_student
from .services import (LIVE_TTL, attempt_view, cache_attempt, find_attempt, get_attempt_meta,
                       get_exam_cached, new_id, new_receipt)
from .store import get_store
from .timeutil import iso, utcnow

router = APIRouter(prefix="/api/student", tags=["student"])

MAX_WRONG_CODES = 5
WRONG_CODE_LOCK_S = 60
WRONG_CODE_WINDOW_S = 600
LATE_GRACE = timedelta(seconds=60)
OFFLINE_START_MAX = timedelta(minutes=30)


def offline_start(offline_started_at, now):
    """Slice 6: start time for an attempt begun offline. Up to 30 min in the past is
    accepted; older values are clamped to 30 min, future ones to now."""
    if offline_started_at is None:
        return now
    return min(now, max(offline_started_at, now - OFFLINE_START_MAX))


@router.get("/exams")
async def my_exams(p: Principal = Depends(require_student),
                   session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Exam.id, Exam.title, Exam.duration_min, Exam.question_count, Exam.status,
               Attempt.id, Attempt.status, Attempt.submitted_at, Attempt.receipt,
               Exam.course, Exam.room, Exam.scheduled_at, Exam.grades_released,
               Exam.total_points, Attempt.score, Exam.mode, Exam.delivery)
        .outerjoin(Attempt, and_(Attempt.exam_id == Exam.id, Attempt.student_id == p.user_id))
        .where(or_(Exam.status == "live", Attempt.id.is_not(None)))
        # slice 6: paper exams are printed; students never see them in the app
        .where(or_(Exam.delivery.is_(None), Exam.delivery != "paper"))
        .order_by(Exam.created_at.desc(), Exam.id))).all()
    # classes: hide exams restricted to a roster / classes the student is not in (unless
    # they already have an attempt) and name the student's classes for each exam.
    hidden, class_names = await student_exam_visibility(
        session, p.user_id, [r[0] for r in rows if r[5] is None])
    out = []
    for (eid, title, dur, qc, status, aid, astatus, sub, receipt, course, room, sched, released,
         total, score, mode, delivery) in rows:
        if eid in hidden:
            continue
        mode = mode or "questions"
        delivery = delivery or ("website" if mode == "external" else "computer")
        attempt = None
        if aid is not None:
            released = bool(released)
            show = released and astatus == "submitted"
            attempt = {"attempt_id": aid, "status": astatus, "submitted_at": iso(sub),
                       "receipt": receipt, "released": released,
                       "percent": percent(score or 0.0, total) if show else None}
        out.append({"id": eid, "title": title, "duration_min": dur, "question_count": qc,
                    "status": status, "course": course or "", "room": room or "",
                    "scheduled_at": iso(sched), "mode": mode, "delivery": delivery,
                    "attempt": attempt, "class_names": class_names.get(eid, [])})
    return out


@router.post("/exams/{exam_id}/start")
async def start(exam_id: str, body: StartIn, request: Request,
                p: Principal = Depends(require_student),
                session: AsyncSession = Depends(get_session)):
    store = get_store()
    exam = await get_exam_cached(store, session, exam_id)
    if exam is None or exam.get("delivery") == "paper":  # slice 6: paper is never started
        raise HTTPException(404, "Exam not found")
    if exam["status"] != "live":
        raise HTTPException(409, "Exam is not open")
    # classes: explicit roster > assigned classes > linked LMS course > open (app.classes.service)
    await require_exam_access(session, exam, p.user_id)
    lock_key, count_key = f"codelock:{p.user_id}", f"wrongcode:{p.user_id}"
    if await store.get(lock_key):
        raise HTTPException(429, "Too many wrong codes, wait 60 seconds")
    if body.start_code.strip() != exam["start_code"]:
        n = await store.incr(count_key, WRONG_CODE_WINDOW_S)
        if n >= MAX_WRONG_CODES:
            await store.set(lock_key, "1", WRONG_CODE_LOCK_S)
            await store.delete(count_key)
        await audit.record("start_code_wrong", request=request, actor_id=p.user_id,
                           actor_role="student", exam_id=exam_id,
                           detail={"offline": body.offline_started_at is not None})
        raise HTTPException(403, "Wrong start code")
    await store.delete(count_key)

    now = utcnow()
    a = await find_attempt(session, exam_id, p.user_id)
    created = False
    if a is None:
        started = offline_start(body.offline_started_at, now)
        a = Attempt(id=new_id("a"), exam_id=exam_id, student_id=p.user_id, status="in_progress",
                    started_at=started,
                    deadline_at=started + timedelta(minutes=exam["duration_min"]),
                    answers={}, client_seq=-1, updated_at=now)
        session.add(a)
        detail = {"deadline_at": iso(a.deadline_at)}
        if body.offline_started_at is not None:
            detail.update(offline=True, offline_started_at=iso(started),
                          claimed_started_at=iso(body.offline_started_at))
        audit.add(session, "attempt_started", request=request, actor_id=p.user_id,
                  actor_role="student", exam_id=exam_id, attempt_id=a.id, at=started,
                  detail=detail)
        try:
            await session.commit()
            created = True
        except IntegrityError:  # double-click / two tabs: someone else inserted it first
            await session.rollback()
            a = await find_attempt(session, exam_id, p.user_id)
            if a is None:
                raise
    if a.status == "submitted":
        raise HTTPException(409, "Already submitted")
    if not created:
        await audit.record("attempt_resumed", request=request, actor_id=p.user_id,
                           actor_role="student", exam_id=exam_id, attempt_id=a.id)
    await cache_attempt(store, a)
    return attempt_view(a, exam, now)


async def _own_attempt(session: AsyncSession, aid: str, p: Principal) -> Attempt:
    a = await session.get(Attempt, aid)
    if a is None or a.student_id != p.user_id:
        raise HTTPException(404, "Attempt not found")
    return a


@router.get("/attempts/{aid}")
async def get_attempt(aid: str, p: Principal = Depends(require_student),
                      session: AsyncSession = Depends(get_session)):
    a = await _own_attempt(session, aid, p)
    exam = await get_exam_cached(get_store(), session, a.exam_id)
    if exam is None:
        raise HTTPException(404, "Attempt not found")
    return attempt_view(a, exam, utcnow())


@router.put("/attempts/{aid}/answers")
async def save_answers(aid: str, body: AnswersIn, request: Request,
                       p: Principal = Depends(require_student),
                       session: AsyncSession = Depends(get_session)):
    now = utcnow()
    # Hot path: exactly one conditional UPDATE, no read-modify-write.
    res = await session.execute(
        update(Attempt)
        .where(Attempt.id == aid, Attempt.student_id == p.user_id,
               Attempt.status == "in_progress", Attempt.client_seq < body.client_seq,
               Attempt.deadline_at > now)
        .values(answers=body.answers, client_seq=body.client_seq, updated_at=now))
    await session.commit()
    if res.rowcount == 1:
        # phase 1a: coalesced audit (store only; a DB row at most once per window)
        await audit.note_answer_saved(get_store(), aid, None, p.user_id, body.client_seq, request)
        return {"saved": True, "client_seq": body.client_seq, "server_time": iso(now)}
    # Slow path: find out why nothing was updated.
    row = (await session.execute(
        select(Attempt.student_id, Attempt.status, Attempt.deadline_at, Attempt.client_seq,
               Attempt.answers)
        .where(Attempt.id == aid))).first()
    if row is None or row.student_id != p.user_id:
        raise HTTPException(404, "Attempt not found")
    if row.status == "submitted":
        raise HTTPException(409, "Attempt already submitted")
    if now >= row.deadline_at:
        raise HTTPException(409, "Time is up")
    # An exact repeat of the stored save (response lost, client retried) is reported as
    # saved. The same seq with DIFFERENT answers (a second tab or computer) is not: that
    # client must bump its seq and resend, or it would show "saved" for unsaved work.
    dup = row.client_seq == body.client_seq and (row.answers or {}) == body.answers
    return {"saved": dup, "client_seq": row.client_seq, "server_time": iso(now)}


@router.post("/attempts/{aid}/heartbeat")
async def heartbeat(aid: str, body: HeartbeatIn, request: Request,
                    p: Principal = Depends(require_student),
                    session: AsyncSession = Depends(get_session)):
    store = get_store()
    meta = await get_attempt_meta(store, session, aid)  # store hit; DB only on cache miss
    if meta is None or meta["student_id"] != p.user_id:
        raise HTTPException(404, "Attempt not found")
    now = time.time()
    await store.hset(f"hb:{meta['exam_id']}", aid,
                     json.dumps({"t": now, "cq": body.current_question, "locked": body.locked}),
                     LIVE_TTL)
    if meta["status"] == "in_progress":  # phase 1a: connection gaps -> audit
        await audit.note_heartbeat(store, aid, meta["exam_id"], p.user_id, request)
    return {"ok": True, "status": meta["status"], "server_time": iso(utcnow()),
            "deadline_at": meta["deadline_at"]}


@router.post("/attempts/{aid}/events")
async def events(aid: str, body: EventsIn, p: Principal = Depends(require_student),
                 session: AsyncSession = Depends(get_session)):
    store = get_store()
    meta = await get_attempt_meta(store, session, aid)
    if meta is None or meta["student_id"] != p.user_id:
        raise HTTPException(404, "Attempt not found")
    if not body.events:
        return {"ok": True, "stored": 0}
    now = utcnow()
    rows = [{"attempt_id": aid, "exam_id": meta["exam_id"], "type": ev.type[:64],
             "detail": (ev.detail or "")[:500],
             "client_time": (ev.client_time or None) and ev.client_time[:64],
             "server_time": now} for ev in body.events]
    await session.execute(insert(IntegrityEvent), rows)  # one batched INSERT
    await session.commit()
    exam_id = meta["exam_id"]
    await store.hincrby(f"evc:{exam_id}", aid, len(rows), LIVE_TTL)
    last = rows[-1]
    await store.hset(f"evl:{exam_id}", aid, f"{last['type']} {last['detail']}".strip(), LIVE_TTL)
    return {"ok": True, "stored": len(rows)}


@router.post("/attempts/{aid}/submit")
async def submit(aid: str, body: AnswersIn, request: Request,
                 p: Principal = Depends(require_student),
                 session: AsyncSession = Depends(get_session)):
    a = await _own_attempt(session, aid, p)
    exam = await session.get(Exam, a.exam_id)
    questions = exam_questions(exam) if exam else []
    if a.status != "submitted":
        now = utcnow()
        # Same rule as PUT /answers: the body wins only if its client_seq is strictly newer
        # (no deadline check: a late submit still carries its answers). The UPDATE is a
        # compare-and-swap on the client_seq we read, so a save landing between our read
        # and this write can never be overwritten by an older snapshot: the swap then
        # misses, we re-read and decide again. One statement in the normal case.
        for _ in range(5):
            use_body = body.client_seq > a.client_seq
            answers = body.answers if use_body else (a.answers or {})
            score, pending, _ = grade_attempt(questions, answers, a.grades)
            receipt, late = new_receipt(), now > a.deadline_at + LATE_GRACE
            res = await session.execute(
                update(Attempt)
                .where(Attempt.id == aid, Attempt.status == "in_progress",
                       Attempt.client_seq == a.client_seq)
                .values(status="submitted", submitted_at=now, receipt=receipt,
                        score=score, needs_grading=pending, answers=answers,
                        client_seq=max(body.client_seq, a.client_seq),
                        late=late, auto_submitted=False, updated_at=now))
            if res.rowcount == 1:
                store = get_store()
                audit.add_many(session, await audit.flush_answer_saves(store, aid))
                audit.add(session, "submitted", request=request, actor_id=p.user_id,
                          actor_role="student", exam_id=a.exam_id, attempt_id=aid, at=now,
                          detail={"receipt": receipt, "late": late, "client_seq": body.client_seq,
                                  "answers_from_submit": use_body,
                                  "answered": sum(1 for v in answers.values() if is_answered(v))})
                await session.commit()
                break
            await session.commit()
            await session.refresh(a)  # a save, submit or close got in first
            if a.status == "submitted":
                break
        else:
            raise HTTPException(503, "Busy, try again")
        # what is stored now: our submit, or the receipt of a submit/close that won
        await session.refresh(a)
        await cache_attempt(get_store(), a)
    answers = a.answers or {}
    answered = sum(1 for q in questions if is_answered(answers.get(q["id"])))
    return {"status": "submitted", "submitted_at": iso(a.submitted_at), "receipt": a.receipt,
            "answered": answered}


# ---------------------------------------------------------------- slice 2: grades
@router.get("/grades")
async def my_grades(p: Principal = Depends(require_student),
                    session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Exam.id, Exam.title, Exam.course, Exam.total_points, Exam.grades_released,
               Attempt.submitted_at, Attempt.score)
        .join(Attempt, Attempt.exam_id == Exam.id)
        .where(Attempt.student_id == p.user_id, Attempt.status == "submitted")
        .order_by(Attempt.submitted_at.desc(), Exam.id))).all()
    out = []
    for eid, title, course, total, released, sub, score in rows:
        released = bool(released)
        out.append({"exam_id": eid, "title": title, "course": course or "",
                    "submitted_at": iso(sub), "released": released,
                    "score": num(score or 0.0) if released else None,
                    "total_points": num(total),
                    "percent": percent(score or 0.0, total) if released else None})
    return out


def _correct_answer(q: dict):
    t = q.get("type")
    if t == "mcq":
        return q.get("correct_index")
    if t == "numeric":
        return q.get("answer_number")
    if t == "short":
        acc = q.get("accepted_answers") or []
        return acc[0] if acc else None
    return None


@router.get("/grades/{exam_id}")
async def my_grade_detail(exam_id: str, p: Principal = Depends(require_student),
                          session: AsyncSession = Depends(get_session)):
    a = await find_attempt(session, exam_id, p.user_id)
    if a is None or a.status != "submitted":
        raise HTTPException(404, "No submitted attempt for this exam")
    e = await session.get(Exam, exam_id)
    if e is None:
        raise HTTPException(404, "No submitted attempt for this exam")
    if not e.grades_released:
        raise HTTPException(403, "Grades not released yet")
    questions = exam_questions(e)
    score, _, rows = grade_attempt(questions, a.answers, a.grades)
    # Class average from the stored per-attempt score (kept in sync by every grade change):
    # one aggregate instead of re-grading every attempt per student request.
    avg = (await session.execute(
        select(func.avg(Attempt.score))
        .where(Attempt.exam_id == exam_id, Attempt.status == "submitted"))).scalar()
    out_q = []
    for i, (q, r) in enumerate(zip(questions, rows)):
        pts = float(r["points_awarded"])
        mx = q.get("points", 1)
        if not is_answered(r["given"]):
            status = "unanswered"
        elif abs(pts - mx) < 1e-9:
            status = "correct"
        elif pts > 0:
            status = "partial"
        else:
            status = "wrong"
        out_q.append({
            "question_id": q["id"], "index": i, "type": q["type"], "prompt": q["prompt"],
            "options": list(q.get("options") or []), "points": mx,
            "points_awarded": r["points_awarded"], "status": status, "given": r["given"],
            "correct_answer": _correct_answer(q),
            "tolerance": q.get("tolerance") if q.get("type") == "numeric" else None,
            "comment": r["comment"], "rubric": [dict(x) for x in q.get("rubric") or []],
            "images": [dict(x) for x in q.get("images") or []],  # slice 6
            "rubric_scores": r["rubric_scores"],
        })
        if r.get("image"):  # slice 6 D: the student's scanned answer (paper exams)
            out_q[-1]["image"] = r["image"]
    return {
        "exam_id": e.id, "title": e.title, "course": e.course or "",
        "submitted_at": iso(a.submitted_at), "receipt": a.receipt, "score": num(score),
        "total_points": num(e.total_points), "percent": percent(score, e.total_points),
        "class_average_percent": percent(float(avg), e.total_points) if avg is not None else None,
        "overall_comment": a.overall_comment or "", "questions": out_q,
    }
