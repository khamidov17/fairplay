"""Per-student topic scores computed locally from graded answers (no dependency on the
insights/profile endpoints). Topic score = sum awarded / sum max over graded answers of
this teacher's exams; pending essays are excluded. Weak = < 60 % with >= 2 answers;
if a student has none, the lowest-scoring topics are used instead (`fallback`)."""
from __future__ import annotations

import asyncio

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..grading import exam_questions, grade_attempt
from ..models import Attempt, Exam

WEAK_BELOW = 60.0
WEAK_MIN_ANSWERS = 2
FALLBACK_TOPICS = 2


def norm_topic(t: object) -> str:
    return " ".join(str(t).strip().lower().split())[:60]


def question_topics(q: dict, exam: Exam | None = None) -> list[str]:
    """Topics of a question; untagged (older) questions fall back to the exam course or "general"."""
    out: list[str] = []
    for t in (q.get("topics") or []):
        n = norm_topic(t)
        if n and n not in out:
            out.append(n)
    if not out:
        course = norm_topic(getattr(exam, "course", "") or "") if exam is not None else ""
        out = [course or "general"]
    return out


async def teacher_exams(session: AsyncSession, teacher_id: int) -> list[Exam]:
    return list((await session.scalars(select(Exam).where(Exam.teacher_id == teacher_id))).all())


async def topic_stats_by_student(session: AsyncSession, teacher_id: int,
                                 student_ids: list[int] | None = None,
                                 exams: list[Exam] | None = None) -> dict[int, dict]:
    """{student_id: {"topics": [{"topic","avg_percent","n"}] (lowest first), "wrong": {(exam_id, qid)}}}"""
    exams = exams if exams is not None else await teacher_exams(session, teacher_id)
    by_id = {e.id: e for e in exams}
    if not by_id:
        return {}
    stmt = select(Attempt).where(Attempt.exam_id.in_(list(by_id)), Attempt.status == "submitted")
    if student_ids is not None:
        if not student_ids:
            return {}
        stmt = stmt.where(Attempt.student_id.in_(student_ids))
    attempts = (await session.scalars(stmt)).all()
    # Audit L-11: regrading every attempt is CPU work: off the event loop.
    return await asyncio.to_thread(_topic_stats, attempts, by_id)


def _topic_stats(attempts, by_id: dict) -> dict[int, dict]:
    acc: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    wrong: dict[int, set] = defaultdict(set)
    for a in attempts:
        e = by_id[a.exam_id]
        qs = exam_questions(e)
        qmap = {q["id"]: q for q in qs}
        _, _, rows = grade_attempt(qs, a.answers, a.grades)
        for r in rows:
            if r["pending"]:
                continue
            q = qmap.get(r["question_id"]) or {}
            mx = float(r.get("max_points") or 0)
            if mx <= 0:
                continue
            got = float(r.get("points_awarded") or 0)
            if got < mx:
                wrong[a.student_id].add((e.id, r["question_id"]))
            for t in question_topics(q, e):
                cell = acc[a.student_id][t]
                cell[0] += got
                cell[1] += mx
                cell[2] += 1
    out: dict[int, dict] = {}
    for sid, topics in acc.items():
        rows = [{"topic": t, "avg_percent": round(v[0] / v[1] * 100, 1) if v[1] else 0.0, "n": int(v[2])}
                for t, v in topics.items()]
        rows.sort(key=lambda r: (r["avg_percent"], -r["n"], r["topic"]))
        out[sid] = {"topics": rows, "wrong": wrong.get(sid, set())}
    return out


def weak_topics(stats: list[dict]) -> tuple[list[dict], bool]:
    """(weak topic rows, is_fallback)."""
    weak = [r for r in stats if r["avg_percent"] < WEAK_BELOW and r["n"] >= WEAK_MIN_ANSWERS]
    if weak:
        return weak, False
    return stats[:FALLBACK_TOPICS], bool(stats)
