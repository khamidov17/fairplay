"""Phase 3 grading quality: loaders and report builders shared by the routes.

Production metrics read `attempt.grades[qid]` (final teacher grade) and the AI suggestion
(`grade.ai_suggested_points`, else `attempt.ai_drafts[qid].points`). Phase 2 fields
(`source`, `graded_by`, `graded_at`, `grading_seconds`) are optional: missing = unknown.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..grading import MANUAL_TYPES, exam_questions, num, paper_answer
from ..models import Attempt, Exam
from . import metrics, routing
from .models import BenchmarkItem, BenchmarkRun, BenchmarkRunItem, BenchmarkSet, RoutingPolicy, SecondMark

WORST_LIMIT = 25


def anon_id(attempt_id: str) -> str:
    return "#" + hashlib.sha256(attempt_id.encode()).hexdigest()[:4].upper()


def _num(v: Any) -> float | None:
    return metrics._num(v)


# ---------------------------------------------------------------- routing policy
async def policy_rows(session: AsyncSession, teacher_id: int, exam_id: str | None) -> tuple[dict, dict | None]:
    """(teacher default settings, exam override settings or None)."""
    rows = (await session.execute(select(RoutingPolicy).where(
        RoutingPolicy.teacher_id == teacher_id,
        RoutingPolicy.exam_id.in_(["", exam_id or ""])))).scalars().all()
    default = next((r.settings for r in rows if r.exam_id == ""), None) or {}
    exam = next((r.settings for r in rows if exam_id and r.exam_id == exam_id), None)
    return default, exam


async def effective_policy(session: AsyncSession, teacher_id: int, exam_id: str | None) -> dict:
    default, exam = await policy_rows(session, teacher_id, exam_id)
    return routing.merge_policy(default, exam)


# ---------------------------------------------------------------- benchmark weak areas
async def latest_runs(session: AsyncSession, teacher_id: int) -> list[BenchmarkRun]:
    """Latest finished run of each of the teacher's benchmark sets."""
    runs = (await session.execute(
        select(BenchmarkRun).where(BenchmarkRun.teacher_id == teacher_id, BenchmarkRun.status == "done")
        .order_by(BenchmarkRun.created_at.desc(), BenchmarkRun.id))).scalars().all()
    seen: set[str] = set()
    out = []
    for r in runs:
        if r.set_id not in seen:
            seen.add(r.set_id)
            out.append(r)
    return out


def run_pairs(items: Iterable[BenchmarkRunItem]) -> list[metrics.Pair]:
    return [metrics.Pair(ai=float(i.ai_points), human=float(i.human_points), max_points=float(i.max_points),
                         question_type=i.question_type or "", subject=(i.subject or "").strip().lower(),
                         ref={"item_id": i.item_id})
            for i in items if i.ai_points is not None]


async def weak_areas(session: AsyncSession, teacher_id: int, policy: dict) -> dict:
    """Question types / subjects whose benchmark kappa is below policy.weak_kappa (latest run
    per set, pooled; groups with fewer than policy.weak_min_items items are ignored)."""
    runs = await latest_runs(session, teacher_id)
    if not runs:
        return {"types": {}, "subjects": {}, "runs": []}
    items = (await session.execute(
        select(BenchmarkRunItem).where(BenchmarkRunItem.run_id.in_([r.id for r in runs])))).scalars().all()
    pairs = run_pairs(items)
    limit = float(policy["weak_kappa"])
    min_n = int(policy["weak_min_items"])
    out: dict[str, Any] = {"types": {}, "subjects": {}, "runs": [r.id for r in runs]}
    for key, field in (("types", "question_type"), ("subjects", "subject")):
        for row in metrics.breakdown(pairs, field):
            if row["key"] and row["n"] >= min_n and row["kappa"] is not None and row["kappa"] < limit:
                out[key][row["key"]] = row["kappa"]
    return out


# ---------------------------------------------------------------- exams + attempts
async def teacher_exams(session: AsyncSession, teacher_id: int) -> list[Exam]:
    return list((await session.execute(
        select(Exam).where(Exam.teacher_id == teacher_id, Exam.status != "draft")
        .order_by(Exam.created_at, Exam.id))).scalars())


async def submitted_attempts(session: AsyncSession, exam_ids: list[str]) -> list[Attempt]:
    if not exam_ids:
        return []
    return list((await session.execute(
        select(Attempt).where(Attempt.exam_id.in_(exam_ids), Attempt.status == "submitted")
        .order_by(Attempt.started_at, Attempt.id))).scalars())


def review_candidates(exam: Exam, a: Attempt) -> list[str]:
    """Answers that are graded by a person (with or without an AI suggestion): every AI
    draft, every paper answer and every essay / website item."""
    answers, drafts = a.answers or {}, a.ai_drafts or {}
    out = []
    for q in exam_questions(exam):
        qid = q["id"]
        if qid in drafts or paper_answer(answers.get(qid)) or q.get("type") in MANUAL_TYPES:
            if q.get("type") == "mcq" and paper_answer(answers.get(qid)) and \
                    (answers[qid] or {}).get("marks") in ("one", "none") and qid not in drafts:
                continue  # bubbles read automatically
            out.append(qid)
    return out


def routing_rows(exam: Exam, attempts: list[Attempt], policy: dict, weak: dict) -> list[dict]:
    rows = []
    for a in attempts:
        for qid in review_candidates(exam, a):
            r = routing.route_item(exam, a, qid, policy, weak)
            d = (a.ai_drafts or {}).get(qid)
            rows.append({"attempt_id": a.id, "anon_id": anon_id(a.id), "question_id": qid,
                         "review": r["review"], "reasons": r["reasons"],
                         "graded": (a.grades or {}).get(qid) is not None,
                         "ai_points": d.get("points") if routing.usable_draft(d) else None,
                         "confidence": d.get("confidence") if isinstance(d, dict) else None})
    return rows


def routing_summary(rows: list[dict]) -> dict:
    by_reason: dict[str, int] = {}
    for r in rows:
        for x in r["reasons"]:
            by_reason[x["code"]] = by_reason.get(x["code"], 0) + 1
    return {"total": len(rows), "routed": sum(1 for r in rows if r["review"]),
            "routed_ungraded": sum(1 for r in rows if r["review"] and not r["graded"]),
            "by_reason": dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])))}


# ---------------------------------------------------------------- production grades
def graded_items(exam: Exam, attempts: list[Attempt]) -> list[dict]:
    """One row per teacher-graded answer."""
    qs = exam_questions(exam)
    qmap = {q["id"]: (i, q) for i, q in enumerate(qs)}
    out = []
    for a in attempts:
        drafts = a.ai_drafts or {}
        for qid, g in (a.grades or {}).items():
            if qid not in qmap or not isinstance(g, dict):
                continue
            idx, q = qmap[qid]
            final = _num(g.get("points"))
            if final is None:
                continue
            d = drafts.get(qid)
            suggested = metrics.suggested_points(g, d)
            secs = _num(g.get("grading_seconds"))
            out.append({
                "exam_id": exam.id, "exam_title": exam.title, "course": exam.course or "",
                "attempt_id": a.id, "anon_id": anon_id(a.id), "question_id": qid, "question_index": idx,
                "prompt": (q.get("prompt") or "")[:300], "type": q.get("type", ""),
                "max_points": float(q.get("points", 1)), "suggested": suggested, "final": final,
                "source": metrics.grade_source(g, suggested),
                "moderated": bool(g.get("moderated")),
                "seconds": secs if secs is not None and secs >= 0 else None,
                "assisted": suggested is not None,
                "batch": str(g.get("batch_id") or (d or {}).get("batch_id") or exam.id),
                "graded_by": g.get("graded_by"),
                "graded_at": g.get("graded_at"),
                "confidence": _num((d or {}).get("confidence")) if isinstance(d, dict) else None,
            })
    return out


def item_pairs(items: list[dict]) -> list[metrics.Pair]:
    return [metrics.Pair(ai=it["suggested"], human=it["final"], max_points=it["max_points"],
                         question_type=it["type"], subject=(it["course"] or "").lower(), ref=it)
            for it in items if it["suggested"] is not None]


def worst(pairs: list[metrics.Pair], severe_fraction: float, limit: int = WORST_LIMIT) -> list[dict]:
    bad = sorted((p for p in pairs if abs(p.diff) > metrics.EPS),
                 key=lambda p: (-p.abs_frac, -abs(p.diff), p.ref.get("exam_id", ""), p.ref.get("attempt_id", "")))
    out = []
    for p in bad[:limit]:
        it = p.ref
        out.append({k: it[k] for k in ("exam_id", "exam_title", "attempt_id", "anon_id", "question_id",
                                        "question_index", "prompt", "type", "source", "graded_by", "confidence")}
                   | {"ai_points": num(p.ai), "final_points": num(p.human), "max_points": num(p.max_points),
                      "diff": num(p.diff), "severe": metrics.is_severe(p, severe_fraction)})
    return out


def quality_block(items: list[dict], severe_fraction: float) -> dict:
    pairs = item_pairs(items)
    by_q: dict[str, list[metrics.Pair]] = {}
    for p in pairs:
        by_q.setdefault(f"{p.ref['exam_id']}:{p.ref['question_id']}", []).append(p)
    return {
        "agreement": metrics.summarize(pairs, severe_fraction),
        "by_type": metrics.breakdown(pairs, "question_type", severe_fraction),
        "by_question": [dict(metrics.summarize(ps, severe_fraction), exam_id=ps[0].ref["exam_id"],
                             question_id=ps[0].ref["question_id"], question_index=ps[0].ref["question_index"],
                             prompt=ps[0].ref["prompt"], type=ps[0].ref["type"])
                        for ps in sorted(by_q.values(), key=lambda ps: (ps[0].ref["exam_id"],
                                                                         ps[0].ref["question_index"]))],
        "correction": metrics.correction_stats(items),
        "timing": metrics.timing_stats(items),
        "worst": worst(pairs, severe_fraction),
        "graded": len(items),
        "severe_fraction": severe_fraction,
    }


# ---------------------------------------------------------------- second marking
def first_grade(a: Attempt | None, qid: str) -> dict | None:
    g = (a.grades or {}).get(qid) if a is not None else None
    return g if isinstance(g, dict) and _num(g.get("points")) is not None else None


def sm_status(sm: SecondMark, a: Attempt | None) -> str:
    """awaiting_second | awaiting_first | agreed | needs_moderation | moderated (lead view)."""
    if sm.moderated_at is not None:
        return "moderated"
    if sm.second_points is None:
        return "awaiting_second"
    g = first_grade(a, sm.question_id)
    if g is None:
        return "awaiting_first"
    if abs(float(g["points"]) - float(sm.second_points)) <= float(sm.tolerance) + 1e-9:
        return "agreed"
    return "needs_moderation"


def marker_status(sm: SecondMark) -> str:
    """What the second marker may know: to_mark | marked | closed (never the comparison)."""
    if sm.moderated_at is not None:
        return "closed"
    return "to_mark" if sm.second_points is None else "marked"


async def moderation_open(session: AsyncSession, exam_id: str) -> dict:
    """Second-marked answers that are not final yet, by status."""
    sms = (await session.execute(select(SecondMark).where(SecondMark.exam_id == exam_id))).scalars().all()
    if not sms:
        return {"total": 0, "open": 0, "by_status": {}}
    atts = {a.id: a for a in (await session.execute(
        select(Attempt).where(Attempt.id.in_({s.attempt_id for s in sms})))).scalars()}
    counts: dict[str, int] = {}
    for s in sms:
        st = sm_status(s, atts.get(s.attempt_id))
        counts[st] = counts.get(st, 0) + 1
    open_n = sum(v for k, v in counts.items() if k not in ("agreed", "moderated"))
    return {"total": len(sms), "open": open_n, "by_status": counts}


async def set_counts(session: AsyncSession, set_ids: list[str]) -> dict[str, int]:
    if not set_ids:
        return {}
    return dict((await session.execute(
        select(BenchmarkItem.set_id, func.count()).where(BenchmarkItem.set_id.in_(set_ids))
        .group_by(BenchmarkItem.set_id))).all())


def set_out(s: BenchmarkSet, item_count: int = 0, last_run: BenchmarkRun | None = None) -> dict:
    from ..timeutil import iso
    return {"id": s.id, "name": s.name, "subject": s.subject, "consent_note": s.consent_note,
            "consent_basis": s.consent_basis, "created_at": iso(s.created_at), "item_count": item_count,
            "last_run": run_out(last_run) if last_run else None}


def item_out(i: BenchmarkItem) -> dict:
    from ..timeutil import iso
    return {"id": i.id, "set_id": i.set_id, "question_prompt": i.question_prompt,
            "question_type": i.question_type, "subject": i.subject, "max_points": num(i.max_points),
            "rubric": i.rubric or [], "answer_key": i.answer_key, "answer_image": i.answer_image,
            "answer_text": i.answer_text, "human_marks": [num(float(m)) for m in (i.human_marks or [])],
            "human_points": num(i.human_points), "human_comment": i.human_comment,
            "source": ({"exam_id": i.source_exam_id, "attempt_id": i.source_attempt_id,
                        "question_id": i.source_question_id} if i.source_exam_id else None),
            "consent": bool(i.consent), "consent_by": i.consent_by, "consent_at": iso(i.consent_at),
            "created_at": iso(i.created_at)}


def run_out(r: BenchmarkRun) -> dict:
    from ..timeutil import iso
    return {"id": r.id, "set_id": r.set_id, "grader": r.grader, "provider": r.provider, "model": r.model,
            "prompt_version": r.prompt_version, "status": r.status, "severe_fraction": r.severe_fraction,
            "item_count": r.item_count, "done_count": r.done_count, "metrics": r.metrics or {},
            "error": r.error, "created_at": iso(r.created_at), "finished_at": iso(r.finished_at)}
