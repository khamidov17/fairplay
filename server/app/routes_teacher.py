from __future__ import annotations

import hashlib
import json
import statistics
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .grading import exam_questions, auto_points, external_question, grade_attempt, is_answered, num, percent, word_count
from .models import Attempt, Exam, IntegrityEvent, User
from .schemas import QID_RE, CommentIn, ExamIn, ExtendIn, GradeIn, ReleaseIn, ReviewIn
from .security import Principal, require_teacher
from .services import (attempt_key, exam_delivery, exam_out, exam_out_full, invalidate_exam,
                       new_id, new_receipt, new_start_code, pending_by_exam)
from .store import get_store
from .timeutil import iso, iso_from_epoch, utcnow

router = APIRouter(prefix="/api/exams", tags=["teacher"])

ONLINE_WINDOW_S = 15
LATE_GRACE = timedelta(seconds=60)
# Integrity events that are bookkeeping, not "flags" a teacher should look at.
NON_FLAG_TYPES = ("lock_started", "lock_ended")
MAX_LIVE_FLAGS = 50
EXTERNAL_GRADES_DETAIL = "This exam is graded on the other website"


def is_flag(event_type: str) -> bool:
    return event_type not in NON_FLAG_TYPES


def _questions_from(body: ExamIn) -> list[dict]:
    seen: set[str] = set()
    out = []
    for q in body.questions:
        qid = q.id if q.id and QID_RE.match(q.id) and q.id not in seen else new_id("q")
        seen.add(qid)
        d = q.to_dict()
        out.append({"id": qid, **d})
    return out


def _apply(e: Exam, body: ExamIn, creating: bool = False) -> None:
    qs = _questions_from(body)
    if body.mode == "external":
        qs = [external_question(body.external_points)]
    e.title = body.title
    e.duration_min = body.duration_min
    e.questions = qs
    e.question_count = len(qs)
    e.total_points = float(sum(q["points"] for q in qs))
    e.updated_at = utcnow()
    # Optional slice-2 fields: on PUT, a field the client did not send keeps its value.
    for f in ("course", "room", "scheduled_at", "shuffle_questions"):
        if creating or f in body.model_fields_set:
            setattr(e, f, getattr(body, f))
    # Slice 3: mode and its fields always come from the body (already normalised by
    # ExamIn: null/[] for "questions" mode, so switching back clears them).
    e.mode = body.mode
    e.external_url = body.external_url
    e.allowed_domains = list(body.allowed_domains)
    e.finish_url_contains = body.finish_url_contains
    # Slice 6: delivery (ExamIn keeps it consistent with mode). The hidden website-exam
    # grading item gets a topic so insights can place it: the course, else "general".
    e.delivery = body.delivery
    if body.mode == "external":
        for q in qs:
            q["topics"] = [(e.course or "").strip().lower()[:60] or "general"]


async def _own_exam(session: AsyncSession, exam_id: str, p: Principal) -> Exam:
    e = await session.get(Exam, exam_id)
    if e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Exam not found")
    return e


@router.get("")
async def list_exams(p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    res = await session.execute(
        select(Exam).where(Exam.teacher_id == p.user_id).order_by(Exam.created_at.desc(), Exam.id))
    exams = list(res.scalars())
    pending = await pending_by_exam(session, [e.id for e in exams if e.status != "draft"])
    return [exam_out(e, pending.get(e.id, 0)) for e in exams]


@router.post("", status_code=201)
async def create_exam(body: ExamIn, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    now = utcnow()
    e = Exam(id=new_id("e"), teacher_id=p.user_id, status="draft", created_at=now)
    _apply(e, body, creating=True)
    session.add(e)
    await session.commit()
    return exam_out(e)


@router.get("/{exam_id}")
async def get_exam(exam_id: str, p: Principal = Depends(require_teacher),
                   session: AsyncSession = Depends(get_session)):
    return await exam_out_full(session, await _own_exam(session, exam_id, p))


@router.put("/{exam_id}")
async def update_exam(exam_id: str, body: ExamIn, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    if e.status != "draft":
        raise HTTPException(409, "Exam is not a draft")
    _apply(e, body)
    await session.commit()
    await invalidate_exam(get_store(), exam_id)
    return exam_out(e)


@router.delete("/{exam_id}", status_code=204)
async def delete_exam(exam_id: str, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    if e.status not in ("draft", "closed"):
        raise HTTPException(409, "Exam is live; close it first")
    aids = (await session.execute(select(Attempt.id).where(Attempt.exam_id == exam_id))).scalars().all()
    await session.execute(delete(IntegrityEvent).where(IntegrityEvent.exam_id == exam_id))
    await session.execute(delete(Attempt).where(Attempt.exam_id == exam_id))
    await session.delete(e)
    await session.commit()
    store = get_store()
    await invalidate_exam(store, exam_id)
    await store.delete(f"hb:{exam_id}", f"evc:{exam_id}", f"evl:{exam_id}",
                       *[attempt_key(a) for a in aids])
    return Response(status_code=204)


@router.post("/{exam_id}/open")
async def open_exam(exam_id: str, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    if e.status == "live":
        raise HTTPException(409, "Exam is already live")
    if exam_delivery(e) == "paper":  # slice 6: printed, scanned and graded; never opened
        raise HTTPException(409, "Paper exams are printed, not opened in the app")
    e.status = "live"
    e.start_code = new_start_code()
    e.closed_at = None
    e.updated_at = utcnow()
    await session.commit()
    await invalidate_exam(get_store(), exam_id)
    return await exam_out_full(session, e)


@router.post("/{exam_id}/close")
async def close_exam(exam_id: str, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    if e.status == "closed":
        return await exam_out_full(session, e)  # idempotent
    # slice 6: a paper exam is never live; closing it (from draft) ends collecting scans
    # so grades can be released like any other exam.
    if e.status != "live" and not (e.status == "draft" and exam_delivery(e) == "paper"):
        raise HTTPException(409, "Exam is not live")
    now = utcnow()
    e.status = "closed"
    e.start_code = None
    e.closed_at = now
    e.updated_at = now
    # Flip the exam first so no new attempts start, then auto-submit stragglers.
    await session.commit()
    store = get_store()
    await invalidate_exam(store, exam_id)

    rows = (await session.execute(
        select(Attempt.id, Attempt.answers, Attempt.deadline_at)
        .where(Attempt.exam_id == exam_id, Attempt.status == "in_progress"))).all()
    for aid, answers, deadline in rows:
        score, pending, _ = grade_attempt(exam_questions(e), answers or {}, None)
        # Conditional: a student submit racing with close keeps its own receipt.
        await session.execute(
            update(Attempt).where(Attempt.id == aid, Attempt.status == "in_progress").values(
                status="submitted", submitted_at=now, auto_submitted=True, receipt=new_receipt(),
                score=score, needs_grading=pending, late=now > deadline + LATE_GRACE,
                updated_at=now))
    await session.commit()
    if rows:
        await store.delete(*[attempt_key(r[0]) for r in rows])
    return await exam_out_full(session, e)


@router.get("/{exam_id}/live")
async def live(exam_id: str, p: Principal = Depends(require_teacher),
               session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    qids = [q["id"] for q in exam_questions(e)]
    rows = (await session.execute(
        select(Attempt.id, Attempt.status, Attempt.started_at, Attempt.submitted_at,
               Attempt.deadline_at, Attempt.answers, User.username, User.name)
        .join(User, User.id == Attempt.student_id)
        .where(Attempt.exam_id == exam_id)
        .order_by(User.name, User.username))).all()
    hb, evc, evl = await get_store().live_snapshot(exam_id)
    # Open flags: indexed on (exam_id, review); only this exam's open events are touched.
    open_flag = (IntegrityEvent.exam_id == exam_id, IntegrityEvent.review == "open",
                 IntegrityEvent.type.not_in(NON_FLAG_TYPES))
    needs_look = {aid for aid, in (await session.execute(
        select(IntegrityEvent.attempt_id).where(*open_flag).distinct())).all()}
    flag_rows = (await session.execute(
        select(IntegrityEvent.id, IntegrityEvent.attempt_id, IntegrityEvent.type,
               IntegrityEvent.detail, IntegrityEvent.server_time, User.username, User.name)
        .join(Attempt, Attempt.id == IntegrityEvent.attempt_id)
        .join(User, User.id == Attempt.student_id)
        .where(*open_flag)
        .order_by(IntegrityEvent.id.desc()).limit(MAX_LIVE_FLAGS))).all()
    now = time.time()
    students = []
    for aid, status, started, submitted, deadline, answers, username, name in rows:
        h = json.loads(hb[aid]) if aid in hb else None
        answers = answers or {}
        students.append({
            "attempt_id": aid, "username": username, "name": name, "status": status,
            "started_at": iso(started), "submitted_at": iso(submitted), "deadline_at": iso(deadline),
            "answered": sum(1 for q in qids if is_answered(answers.get(q))),
            "question_count": len(qids),
            "current_question": h["cq"] if h else 0,
            "locked": bool(h["locked"]) if h else False,
            "last_heartbeat_at": iso_from_epoch(h["t"]) if h else None,
            "online": bool(h) and now - h["t"] <= ONLINE_WINDOW_S,
            "event_count": int(evc.get(aid, 0)),
            "last_event": evl.get(aid),
            "needs_look": aid in needs_look,
        })
    counts = {"writing": 0, "submitted": 0, "offline": 0, "needs_look": 0}
    for st in students:
        if st["status"] == "submitted":
            counts["submitted"] += 1
        elif st["online"]:
            counts["writing"] += 1
        else:
            counts["offline"] += 1
        counts["needs_look"] += int(st["needs_look"])
    flags = [{"event_id": eid, "attempt_id": aid, "username": username, "name": name,
              "type": typ, "detail": detail, "server_time": iso(st_time)}
             for eid, aid, typ, detail, st_time, username, name in flag_rows]
    return {"exam_id": e.id, "status": e.status,
            "start_code": e.start_code if e.status == "live" else None,
            "duration_min": e.duration_min, "students": students,
            "flags": flags, "counts": counts}


@router.get("/{exam_id}/results")
async def results(exam_id: str, p: Principal = Depends(require_teacher),
                  session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    rows = (await session.execute(
        select(Attempt, User.username, User.name)
        .join(User, User.id == Attempt.student_id)
        .where(Attempt.exam_id == exam_id)
        .order_by(User.name, User.username))).all()
    evrows = (await session.execute(
        select(IntegrityEvent).where(IntegrityEvent.exam_id == exam_id)
        .order_by(IntegrityEvent.server_time, IntegrityEvent.id))).scalars().all()
    events: dict[str, list] = {}
    flag_count: dict[str, int] = {}
    flags_total = flags_open = 0
    for ev in evrows:
        events.setdefault(ev.attempt_id, []).append(
            {"event_id": ev.id, "type": ev.type, "detail": ev.detail,
             "client_time": ev.client_time, "server_time": iso(ev.server_time),
             "review": ev.review or "open", "note": ev.note or ""})
        if is_flag(ev.type):
            flag_count[ev.attempt_id] = flag_count.get(ev.attempt_id, 0) + 1
            flags_total += 1
            flags_open += (ev.review or "open") == "open"

    qs = exam_questions(e)
    total = e.total_points
    external = False  # slice 4: external exams are graded through their hidden item
    attempts, percents = [], []
    # per question over submitted attempts, excluding pending essay answers
    q_n = {q["id"]: 0 for q in qs}
    q_sum = {q["id"]: 0.0 for q in qs}
    q_correct = {q["id"]: 0 for q in qs}
    n_submitted = pending_total = 0
    for a, username, name in rows:
        score, pending, ans_rows = grade_attempt(qs, a.answers, a.grades)
        pct = percent(score, total)
        if external:
            ans_rows, pending = [], 0
        if a.status == "submitted":
            n_submitted += 1
            pending_total += pending
            if not external:
                percents.append(pct)
            for r in ans_rows:
                if r["pending"]:
                    continue
                qid = r["question_id"]
                q_n[qid] += 1
                q_sum[qid] += float(r["points_awarded"])
                q_correct[qid] += bool(r["correct"])
        attempts.append({
            "attempt_id": a.id, "username": username, "name": name, "status": a.status,
            "auto_submitted": a.auto_submitted,
            "score": None if external else num(score), "total_points": num(total),
            "percent": None if external else pct,
            "submitted_at": iso(a.submitted_at), "receipt": a.receipt,
            "needs_grading": pending, "overall_comment": a.overall_comment or "",
            "flag_count": flag_count.get(a.id, 0),
            "answers": ans_rows, "events": events.get(a.id, []),
        })

    buckets = [0] * 10
    for pct in percents:
        buckets[min(max(int(pct // 10), 0), 9)] += 1
    labels = [f"{i * 10}\u2013{i * 10 + 9}" for i in range(9)] + ["90\u2013100"]

    def q_stats(q: dict) -> dict:
        qid, pts = q["id"], q.get("points", 1)
        n = q_n[qid]
        if q["type"] in ("essay", "external"):
            pc = round(q_sum[qid] / n / pts * 100, 1) if n and pts else None
        else:
            pc = round(q_correct[qid] / n_submitted * 100, 1) if n_submitted else 0.0
        return {"question_id": qid, "prompt": q["prompt"], "type": q["type"], "points": pts,
                "avg_points": round(q_sum[qid] / n, 2) if n else None, "percent_correct": pc}

    return {
        "exam_id": e.id, "title": e.title, "course": e.course or "",
        "total_points": num(total),
        "grades_released": bool(e.grades_released), "released_at": iso(e.released_at),
        "average_percent": (None if external else
                            round(sum(percents) / len(percents), 1) if percents else 0.0),
        "median_percent": (None if external else
                           round(statistics.median(percents), 1) if percents else 0.0),
        "pending_grading": pending_total, "submitted_count": n_submitted,
        "attempt_count": len(attempts), "flags_total": flags_total, "flags_open": flags_open,
        "score_buckets": [{"label": lab, "count": c} for lab, c in zip(labels, buckets)],
        "attempts": attempts,
        "questions": [q_stats(q) for q in qs],
    }


# ---------------------------------------------------------------- slice 2
@router.post("/{exam_id}/extend")
async def extend_exam(exam_id: str, body: ExtendIn, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    if e.status != "live":
        raise HTTPException(409, "Exam is not live")
    delta = timedelta(minutes=body.minutes)
    e.duration_min += body.minutes
    e.updated_at = utcnow()
    rows = (await session.execute(
        select(Attempt.id, Attempt.deadline_at)
        .where(Attempt.exam_id == exam_id, Attempt.status == "in_progress"))).all()
    if rows:
        # Bulk UPDATE by primary key (dialect-neutral; no interval arithmetic in SQL).
        await session.execute(update(Attempt), [{"id": aid, "deadline_at": dl + delta}
                                                for aid, dl in rows])
    await session.commit()
    store = get_store()
    await invalidate_exam(store, exam_id)  # new starts get the longer duration
    if rows:  # heartbeats re-read the new deadline from the DB on their next call
        await store.delete(*[attempt_key(aid) for aid, _ in rows])
    return await exam_out_full(session, e)


@router.post("/{exam_id}/events/{event_id}/review")
async def review_event(exam_id: str, event_id: int, body: ReviewIn,
                       p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    await _own_exam(session, exam_id, p)
    ev = await session.get(IntegrityEvent, event_id)
    if ev is None or ev.exam_id != exam_id:
        raise HTTPException(404, "Event not found")
    ev.review = body.status
    if body.note is not None:
        ev.note = body.note
    await session.commit()
    return {"event_id": ev.id, "review": ev.review, "note": ev.note or ""}


def anon_id(attempt_id: str) -> str:
    return "#" + hashlib.sha256(attempt_id.encode()).hexdigest()[:4].upper()


@router.get("/{exam_id}/grading")
async def grading_queue(exam_id: str, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    qs = exam_questions(e)
    rows = (await session.execute(
        select(Attempt, User.username, User.name)
        .join(User, User.id == Attempt.student_id)
        .where(Attempt.exam_id == exam_id, Attempt.status == "submitted")
        .order_by(Attempt.started_at, Attempt.id))).all()
    flag_count = dict((await session.execute(
        select(IntegrityEvent.attempt_id, func.count())
        .where(IntegrityEvent.exam_id == exam_id, IntegrityEvent.type.not_in(NON_FLAG_TYPES))
        .group_by(IntegrityEvent.attempt_id))).all())
    stats = {q["id"]: {"to_grade": 0, "graded": 0} for q in qs}
    items_by_q: dict[str, list] = {q["id"]: [] for q in qs}
    for a, username, name in rows:
        grades = a.grades or {}
        _, _, ans_rows = grade_attempt(qs, a.answers, grades)
        for q, r in zip(qs, ans_rows):
            qid, t = q["id"], q["type"]
            given = r["given"]
            g = grades.get(qid)
            if g is not None:
                stats[qid]["graded"] += 1
            if t in ("essay", "external"):
                include = True
                stats[qid]["to_grade"] += int(r["pending"])
            elif t == "short":
                include = given is not None and auto_points(q, given) == 0
                stats[qid]["to_grade"] += int(include and g is None)
            else:
                include = False
            if include:
                items_by_q[qid].append({
                    "attempt_id": a.id, "anon_id": anon_id(a.id), "username": username,
                    "name": name, "question_id": qid, "answer": given,
                    "word_count": word_count(given) if t == "essay" else None,
                    "auto_points": None if t in ("essay", "external") else num(auto_points(q, given)),
                    "grade": g, "flag_count": int(flag_count.get(a.id, 0)),
                })
    return {
        "exam_id": e.id, "title": e.title, "course": e.course or "", "status": e.status,
        "grades_released": bool(e.grades_released),
        "questions": [{"question_id": q["id"], "index": i, "type": q["type"],
                       "prompt": q["prompt"], "points": q.get("points", 1),
                       "rubric": [dict(x) for x in q.get("rubric") or []],
                       "images": [dict(x) for x in q.get("images") or []],  # slice 6
                       **stats[q["id"]]} for i, q in enumerate(qs)],
        "items": [it for q in qs for it in items_by_q[q["id"]]],
    }


async def _exam_attempt(session: AsyncSession, exam_id: str, aid: str,
                        p: Principal) -> tuple[Exam, Attempt]:
    e = await _own_exam(session, exam_id, p)
    # FOR UPDATE on Postgres serialises two teachers grading the same attempt (the grades
    # JSON is read-modify-write); SQLite ignores it and serialises writers anyway.
    a = (await session.execute(
        select(Attempt).where(Attempt.id == aid, Attempt.exam_id == exam_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Attempt not found")
    return e, a


def _question(e: Exam, qid: str) -> dict:
    for q in exam_questions(e):
        if q["id"] == qid:
            return q
    raise HTTPException(404, "Question not found")


async def _save_grades(session: AsyncSession, e: Exam, a: Attempt, qid: str,
                       grade: dict | None) -> dict:
    grades = dict(a.grades or {})
    if grade is None:
        grades.pop(qid, None)
    else:
        grades[qid] = grade
    score, pending, _ = grade_attempt(exam_questions(e), a.answers, grades)
    a.grades = grades
    a.score = score
    a.needs_grading = pending
    a.updated_at = utcnow()
    await session.commit()
    return {"attempt_id": a.id, "question_id": qid, "grade": grade, "score": num(score),
            "total_points": num(e.total_points), "percent": percent(score, e.total_points),
            "needs_grading": pending}


@router.put("/{exam_id}/attempts/{aid}/grades/{qid}")
async def put_grade(exam_id: str, aid: str, qid: str, body: GradeIn,
                    p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    e, a = await _exam_attempt(session, exam_id, aid, p)
    q = _question(e, qid)
    if a.status != "submitted":
        raise HTTPException(409, "Attempt is not submitted")
    max_points = q.get("points", 1)
    eps = 1e-9
    if not -eps <= body.points <= max_points + eps:
        raise HTTPException(422, f"Points must be between 0 and {num(max_points)}")
    scores = None
    if body.rubric_scores is not None:
        rubric = q.get("rubric") or []
        if len(body.rubric_scores) != len(rubric):
            raise HTTPException(422, "rubric_scores must have one score per rubric criterion")
        for s, crit in zip(body.rubric_scores, rubric):
            if not -eps <= s <= float(crit["points"]) + eps:
                raise HTTPException(
                    422, f"Rubric score for '{crit['label']}' must be between 0 and "
                         f"{num(float(crit['points']))}")
        if abs(sum(body.rubric_scores) - body.points) > 1e-6:
            raise HTTPException(422, "points must equal the sum of rubric_scores")
        scores = [num(s) for s in body.rubric_scores]
    grade = {"points": num(body.points), "comment": body.comment, "rubric_scores": scores,
             "graded_at": iso(utcnow())}
    return await _save_grades(session, e, a, qid, grade)


@router.delete("/{exam_id}/attempts/{aid}/grades/{qid}")
async def delete_grade(exam_id: str, aid: str, qid: str, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    e, a = await _exam_attempt(session, exam_id, aid, p)
    _question(e, qid)
    if a.status != "submitted":
        raise HTTPException(409, "Attempt is not submitted")
    return await _save_grades(session, e, a, qid, None)


@router.put("/{exam_id}/attempts/{aid}/comment")
async def put_comment(exam_id: str, aid: str, body: CommentIn,
                      p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    _, a = await _exam_attempt(session, exam_id, aid, p)
    a.overall_comment = body.comment
    a.updated_at = utcnow()
    await session.commit()
    return {"attempt_id": a.id, "overall_comment": a.overall_comment}


@router.post("/{exam_id}/release")
async def release_grades(exam_id: str, body: ReleaseIn | None = None,
                         p: Principal = Depends(require_teacher),
                         session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    if e.status != "closed":
        raise HTTPException(409, "Close the exam first")
    pending = (await pending_by_exam(session, [e.id])).get(e.id, 0)
    if pending > 0 and not (body and body.force):
        raise HTTPException(409, f"{pending} answers still need grading")
    if not e.grades_released:
        e.grades_released = True
        e.released_at = utcnow()
        e.updated_at = e.released_at
        await session.commit()
    return exam_out(e, pending)


@router.post("/{exam_id}/unrelease")
async def unrelease_grades(exam_id: str, p: Principal = Depends(require_teacher),
                           session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    e.grades_released = False
    e.released_at = None
    e.updated_at = utcnow()
    await session.commit()
    return await exam_out_full(session, e)
