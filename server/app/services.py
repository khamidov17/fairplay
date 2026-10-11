"""Shared helpers: ID/code generation, serialization and store-backed caches."""
from __future__ import annotations

import hashlib
import json
import random
import secrets
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .grading import num
from .models import Attempt, Exam
from .schemas import external_host
from .store import Store
from .timeutil import iso

EXAM_CACHE_TTL = 120          # seconds; also explicitly invalidated on edit/open/close/delete
ATTEMPT_META_TTL = 26 * 3600
LIVE_TTL = 26 * 3600
RECEIPT_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
TEACHER_ONLY_FIELDS = ("correct_index", "answer_number", "tolerance", "accepted_answers")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(9).replace('-', 'x').replace('_', 'y')}"


def new_start_code() -> str:
    return str(1000 + secrets.randbelow(9000))


def new_receipt() -> str:
    s = "".join(secrets.choice(RECEIPT_ALPHABET) for _ in range(8))
    return f"{s[:4]}-{s[4:]}"


def teacher_question(q: dict) -> dict:
    return {
        "id": q["id"], "type": q["type"], "prompt": q["prompt"], "points": q.get("points", 1),
        "options": list(q.get("options") or []),
        "correct_index": q.get("correct_index"),
        "answer_number": q.get("answer_number"),
        "tolerance": q.get("tolerance"),
        "accepted_answers": list(q.get("accepted_answers") or []),
        "rubric": [dict(r) for r in q.get("rubric") or []],
        "min_words": q.get("min_words"),
        "max_words": q.get("max_words"),
        # slice 6
        "images": [dict(i) for i in q.get("images") or []],
        "topics": list(q.get("topics") or []),
        "answer_lines": q.get("answer_lines"),  # slice 6: paper answer space
    }


def student_question(q: dict) -> dict:
    tq = teacher_question(q)
    for f in TEACHER_ONLY_FIELDS:
        tq.pop(f)
    # slice 6: topic tags can hint at the method ("integration by parts"): not shown while
    # writing. Images are shown above the prompt.
    tq.pop("topics", None)
    return tq


def exam_delivery(e) -> str:
    """Slice 6 delivery; rows from before slice 6 derive it from mode."""
    d = getattr(e, "delivery", None)
    if d in ("computer", "paper", "website"):
        return d
    return "website" if getattr(e, "mode", None) == "external" else "computer"


def exam_out(e: Exam, pending_grading: int = 0) -> dict:
    return {
        "id": e.id, "title": e.title, "duration_min": e.duration_min, "status": e.status,
        "start_code": e.start_code if e.status == "live" else None,
        "questions": [teacher_question(q) for q in e.questions or []],
        "created_at": iso(e.created_at),
        "question_count": e.question_count, "total_points": num(e.total_points),
        "course": e.course or "", "room": e.room or "", "scheduled_at": iso(e.scheduled_at),
        "shuffle_questions": bool(e.shuffle_questions),
        "grades_released": bool(e.grades_released), "released_at": iso(e.released_at),
        "closed_at": iso(e.closed_at), "pending_grading": int(pending_grading),
        **external_fields(e),
        # slice 5: LMS link (ids only; never any credential)
        "course_link_id": e.course_link_id, "lms_assignment_id": e.lms_assignment_id,
        "delivery": exam_delivery(e),  # slice 6
        # phase 1a: invigilator exit code (teacher view only; issued at open)
        "exit_code": e.exit_code if e.status != "draft" else None,
    }


def external_fields(e: Exam) -> dict:
    """Slice 3 exam fields as stored (teacher view)."""
    external = e.mode == "external"
    return {
        "mode": e.mode or "questions",
        "external_url": e.external_url if external else None,
        "allowed_domains": list(e.allowed_domains or []) if external else [],
        "finish_url_contains": e.finish_url_contains if external else None,
    }


def student_allowed_domains(e: Exam) -> list[str]:
    """Allowed websites for the app: the stored list plus, always, the external_url host."""
    domains = list(e.allowed_domains or [])
    host = external_host(e.external_url)
    if host and host not in domains:
        domains.insert(0, host)
    return domains


async def pending_by_exam(session: AsyncSession, exam_ids: list[str]) -> dict[str, int]:
    """Pending essay answers per exam from the denormalised Attempt.needs_grading (one query)."""
    if not exam_ids:
        return {}
    rows = await session.execute(
        select(Attempt.exam_id, func.coalesce(func.sum(Attempt.needs_grading), 0))
        .where(Attempt.exam_id.in_(exam_ids), Attempt.status == "submitted")
        .group_by(Attempt.exam_id))
    return {eid: int(n or 0) for eid, n in rows.all()}


async def exam_out_full(session: AsyncSession, e: Exam) -> dict:
    return exam_out(e, (await pending_by_exam(session, [e.id])).get(e.id, 0))


# ---------------------------------------------------------------- exam cache
def exam_key(exam_id: str) -> str:
    return f"exam:{exam_id}"


async def invalidate_exam(store: Store, exam_id: str) -> None:
    # phase 1a: also the cached offline-package key (app.offline; rotated at close)
    await store.delete(exam_key(exam_id), f"pkgk:{exam_id}")


async def get_exam_cached(store: Store, session: AsyncSession, exam_id: str) -> dict | None:
    """Student-view exam content (no answer keys) + status/start_code, cached per exam."""
    raw = await store.get(exam_key(exam_id))
    if raw:
        return json.loads(raw)
    e = await session.get(Exam, exam_id)
    if e is None:
        return None
    data = {
        "id": e.id, "title": e.title, "duration_min": e.duration_min, "status": e.status,
        "start_code": e.start_code, "question_count": e.question_count,
        "course": e.course or "", "shuffle_questions": bool(e.shuffle_questions),
        "question_ids": [q["id"] for q in e.questions or []],
        "course_link_id": e.course_link_id,  # slice 5: start restricted to that LMS course
        "delivery": exam_delivery(e),  # slice 6: paper exams are never started in the app
        "scheduled_at": iso(e.scheduled_at),
        "questions": [student_question(q) for q in e.questions or []],
        **external_fields(e),
        # phase 1a: offline verifier of the invigilator exit code (never the code itself)
        "exit_check": _exit_check(e),
    }
    if data["mode"] == "external":
        data["allowed_domains"] = student_allowed_domains(e)
    await store.set(exam_key(exam_id), json.dumps(data), EXAM_CACHE_TTL)
    return data


# ---------------------------------------------------------------- attempt meta cache
def attempt_key(aid: str) -> str:
    return f"att:{aid}"


def attempt_meta(a: Attempt) -> dict:
    return {"exam_id": a.exam_id, "student_id": a.student_id, "status": a.status,
            "deadline_at": iso(a.deadline_at)}


async def cache_attempt(store: Store, a: Attempt) -> None:
    await store.set(attempt_key(a.id), json.dumps(attempt_meta(a)), ATTEMPT_META_TTL)


async def get_attempt_meta(store: Store, session: AsyncSession, aid: str) -> dict | None:
    raw = await store.get(attempt_key(aid))
    if raw:
        return json.loads(raw)
    a = await session.get(Attempt, aid)
    if a is None:
        return None
    await cache_attempt(store, a)
    return attempt_meta(a)


async def find_attempt(session: AsyncSession, exam_id: str, student_id: int) -> Attempt | None:
    res = await session.execute(
        select(Attempt).where(Attempt.exam_id == exam_id, Attempt.student_id == student_id))
    return res.scalar_one_or_none()


def shuffled_for(attempt_id: str, questions: list) -> list:
    """Per-student question order: deterministic from the attempt id (stable across reloads,
    processes and workers), different between attempts."""
    seed = int.from_bytes(hashlib.sha256(attempt_id.encode()).digest()[:8], "big")
    out = list(questions)
    random.Random(seed).shuffle(out)
    return out


def _exit_check(e: Exam) -> dict | None:
    from .exitcode import check_of
    return check_of(getattr(e, "exit_check", None))


def attempt_view(a: Attempt, exam: dict, now: datetime) -> dict:
    questions = exam["questions"]
    if exam.get("shuffle_questions"):
        questions = shuffled_for(a.id, questions)
    mode = exam.get("mode") or "questions"
    external = mode == "external"
    return {
        "attempt_id": a.id, "exam_id": a.exam_id, "title": exam["title"],
        "course": exam.get("course", ""),
        "duration_min": exam["duration_min"], "started_at": iso(a.started_at),
        "deadline_at": iso(a.deadline_at), "server_time": iso(now),
        "questions": [] if external else questions,
        "answers": {} if external else (a.answers or {}), "status": a.status,
        "mode": mode,
        "external_url": exam.get("external_url") if external else None,
        "allowed_domains": list(exam.get("allowed_domains") or []) if external else [],
        "finish_url_contains": exam.get("finish_url_contains") if external else None,
        # phase 1a: for the Mac app's invigilator exit when the server cannot be reached
        "exit_check": exam.get("exit_check"),
    }
