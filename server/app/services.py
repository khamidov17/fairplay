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


START_CODE_DIGITS = 6   # audit #13: was 4 (10 000 codes for a whole room)


def new_start_code() -> str:
    return f"{secrets.randbelow(10 ** START_CODE_DIGITS):0{START_CODE_DIGITS}d}"


def new_invigilator_code() -> str:
    return f"{secrets.randbelow(10 ** 6):06d}"


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


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
        # audit #5: per-exam code for the app's emergency exit (teacher only)
        "invigilator_code": e.invigilator_code if e.status == "live" else None,
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
        "class_id": e.class_id,        # audit S-03
        "allowed_apps": list(e.allowed_apps or []),   # 2026-10-14
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


# Hosts that serve many unrelated things (any shared Doc, Search, Gemini...): the exam
# address only opens its own path there, never the whole host (audit #10).
BROAD_HOSTS = {"docs.google.com", "drive.google.com", "sites.google.com", "www.google.com",
               "google.com", "forms.office.com", "forms.microsoft.com", "onedrive.live.com",
               "www.notion.so", "notion.so", "github.com", "gist.github.com", "www.dropbox.com",
               "dropbox.com", "sharepoint.com", "www.bing.com", "bing.com"}


def start_url_entry(url: str | None) -> str | None:
    """Allowed-site entry the exam address always gets: its exact host, or host + path
    folder on BROAD_HOSTS (https://docs.google.com/forms/d/e/X/viewform ->
    docs.google.com/forms/d/e/X/)."""
    host = external_host(url)
    if not host:
        return None
    if host not in BROAD_HOSTS and not host.endswith(".sharepoint.com"):
        return host
    from urllib.parse import urlsplit
    path = urlsplit((url or "").strip()).path or "/"
    folder = path[:path.rfind("/") + 1] if "/" in path else "/"
    return host if folder in ("", "/") else host + folder


def student_allowed_domains(e: Exam) -> list[str]:
    """Allowed websites for the app: the stored entries (exact host, "*.host" for
    subdomains, optional /path prefix) plus, always, the exam address entry."""
    domains = list(e.allowed_domains or [])
    entry = start_url_entry(e.external_url)
    if entry and entry not in domains:
        domains.insert(0, entry)
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
    await store.delete(exam_key(exam_id))


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
        "class_id": e.class_id,              # audit S-03: ... or to the exam's class
        "delivery": exam_delivery(e),  # slice 6: paper exams are never started in the app
        "scheduled_at": iso(e.scheduled_at),
        "allowed_apps": list(e.allowed_apps or []),   # 2026-10-14
        "questions": [student_question(q) for q in e.questions or []],
        **external_fields(e),
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
            "deadline_at": iso(a.deadline_at), "session_id": a.session_id,
            "device_id": a.device_id, "started_at": iso(a.started_at)}


async def cache_attempt(store: Store, a: Attempt) -> None:
    await store.set(attempt_key(a.id), json.dumps(attempt_meta(a)), ATTEMPT_META_TTL)


async def get_attempt_meta(store: Store, session: AsyncSession, aid: str) -> dict | None:
    raw = await store.get(attempt_key(aid))
    if raw:
        meta = json.loads(raw)
        if "session_id" in meta:  # entries cached before the audit fixes lack it
            return meta
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
        # 2026-10-14: apps the app keeps open while locked (everything else is closed)
        "allowed_apps": _student_apps(exam.get("allowed_apps")),
    }


def _student_apps(keys):
    from .allowed_apps import for_student
    return for_student(keys)
