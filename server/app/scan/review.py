"""Phase 2 review: the grade record written when a teacher approves / edits an AI suggestion,
and whole-exam AI grading runs.

attempt.grades[qid] for a scanned answer (also documented in CONTRACT.md "Phase 2"):
{
  "points", "comment" (approved explanation shown to the student), "rubric_scores" (only when
  the criteria are the question's essay rubric, aligned to it; else null), "graded_at",
  "transcript", "channel": "scan",
  "source": "ai_accepted" | "ai_edited" | "manual",
  "from_draft": bool (legacy: source == "ai_accepted"),
  "ai_suggested_points": num | null, "suggestion_source": "ai"|"key"|"blank"|"bubbles"|null,
  "ai_confidence", "ai_needs_review", "ai_model", "ai_provider", "ai_prompt_version", "ai_created_at",
  "graded_by": username, "graded_by_id": int, "grading_seconds": num | null,
  "criteria": [{"id","label","points","max_points"}] | null,
  "steps": [{"n","text","status","note","teacher_flipped"}] | null, "first_error_step",
  "misconception": {"tag","text"} | null, "mistake_tags": [...], "feedback_edited": bool
}
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ai
from ..db import get_session
from ..grading import exam_questions, num, paper_answer, paper_given
from ..models import Attempt
from ..security import Principal, require_teacher
from ..store import get_store
from ..timeutil import iso, utcnow
from .answer_key import _own_exam, norm_tag

log = logging.getLogger("fairplay.scan.review")
router = APIRouter(prefix="/api", tags=["scan-ai"])

MAX_GRADING_SECONDS = 3 * 3600


# ---------------------------------------------------------------- request bodies (used by routes_scan.approve)
class CriterionPoints(BaseModel):
    id: str = Field(max_length=32)
    points: float


class StepMark(BaseModel):
    n: int = Field(ge=1, le=100)
    status: str = Field(pattern="^(correct|error|missing)$")


class MisconceptionIn(BaseModel):
    tag: str = Field(default="", max_length=60)   # "" clears it
    text: str = Field(default="", max_length=200)


# ---------------------------------------------------------------- grade record
def _same_criteria(a: list[dict] | None, b: list[dict] | None) -> bool:
    pa = {c["id"]: float(c["points"]) for c in a or []}
    pb = {c["id"]: float(c["points"]) for c in b or []}
    return pa == pb


def build_grade(q: dict, ans: dict, draft: dict | None, points: float | None, comment: str | None, *,
                eff_criteria: list[dict], criteria_source: str,
                criteria: list[CriterionPoints] | None = None, steps: list[StepMark] | None = None,
                misconception: MisconceptionIn | None = None, grading_seconds: float | None = None,
                teacher: tuple[int, str] | None = None) -> dict:
    """Validate the teacher's decision and return the grade record. Raises HTTPException."""
    max_points = float(q.get("points", 1))
    d = dict(draft or {})
    suggestion = d if d and d.get("source") != "fallback" else None

    # ---- criteria -> points
    crit_rows: list[dict] | None = None
    if criteria is not None:
        if not eff_criteria:
            raise HTTPException(422, "This question has no rubric criteria")
        known = {c["id"]: c for c in eff_criteria}
        given: dict[str, float] = {}
        for c in criteria:
            if c.id not in known:
                raise HTTPException(422, f"Unknown criterion {c.id}")
            mx = float(known[c.id]["points"])
            if not -1e-9 <= c.points <= mx + 1e-9:
                raise HTTPException(422, f"“{known[c.id]['label']}” must be between 0 and {num(mx)} points")
            given[c.id] = float(c.points)
        crit_rows = [{"id": c["id"], "label": c["label"], "points": num(round(given.get(c["id"], 0.0), 2)),
                      "max_points": num(float(c["points"]))} for c in eff_criteria]
        total = sum(float(c["points"]) for c in crit_rows)
        if points is None:
            points = total
        elif abs(points - total) > 1e-6:
            raise HTTPException(422, f"Points ({num(points)}) must equal the sum of the criteria ({num(total)})")
    elif points is None and suggestion and suggestion.get("criteria"):
        crit_rows = [{"id": c["id"], "label": c["label"], "points": c["points"], "max_points": c["max_points"]}
                     for c in suggestion["criteria"]]

    # ---- points
    if points is None:
        if d.get("source") == "fallback":
            raise HTTPException(409, "There is no AI grade for this answer — enter the points")
        if suggestion:
            points = float(suggestion.get("points") or 0)
        else:
            given_v, pend = paper_given(q, ans, None)
            if pend:
                raise HTTPException(409, "No AI draft yet — enter the points")
            points = max_points if q.get("type") == "mcq" and given_v == q.get("correct_index") else 0.0
    if not -1e-9 <= points <= max_points + 1e-9:
        raise HTTPException(422, f"Points must be between 0 and {num(max_points)}")

    # ---- steps (teacher can flip a step's mark)
    base_steps = [dict(s) for s in (suggestion or {}).get("steps") or []]
    flipped = False
    if steps:
        by_n = {s["n"]: s for s in base_steps}
        for m in steps:
            s = by_n.get(m.n)
            if s is None:
                raise HTTPException(422, f"Step {m.n} does not exist")
            if s["status"] != m.status:
                s["status"], s["teacher_flipped"], flipped = m.status, True, True
    first_err = next((s["n"] for s in base_steps if s["status"] != "correct"), None) if flipped \
        else (suggestion or {}).get("first_error_step")

    # ---- misconception
    ai_mis = (suggestion or {}).get("misconception")
    if misconception is not None:
        tag = norm_tag(misconception.tag)
        mis = {"tag": tag, "text": misconception.text.strip()} if tag else None
    else:
        mis = ai_mis
    if mis == ai_mis and suggestion:
        tags = list(suggestion.get("mistake_tags") or [])
    else:
        label = (mis or {}).get("tag")
        if label == "other":
            label = norm_tag((mis or {}).get("text")) or "other"
        tags = [label] if label else []

    # ---- provenance
    ai_points = float(suggestion["points"]) if suggestion and suggestion.get("points") is not None else None
    changed = ai_points is None or abs(points - ai_points) > 1e-9 or flipped or \
        (crit_rows is not None and suggestion is not None and suggestion.get("criteria") and
         not _same_criteria(crit_rows, suggestion.get("criteria")))
    source = "manual" if suggestion is None else ("ai_edited" if changed else "ai_accepted")
    explanation = (suggestion or {}).get("explanation", (suggestion or {}).get("comment", "")) or ""
    final_comment = comment if comment is not None else (d.get("explanation") or d.get("comment") or "")
    if d.get("source") == "fallback" and comment is None:
        final_comment = ""
    secs = None
    if grading_seconds is not None and grading_seconds >= 0:
        secs = round(min(float(grading_seconds), MAX_GRADING_SECONDS), 1)
    return {
        "points": num(points), "comment": final_comment or "",
        "rubric_scores": [float(c["points"]) for c in crit_rows] if crit_rows and criteria_source == "question" else None,
        "graded_at": iso(utcnow()), "transcript": d.get("transcript") or "", "channel": "scan",
        "source": source, "from_draft": source == "ai_accepted",
        "ai_suggested_points": None if ai_points is None else num(ai_points),
        "suggestion_source": d.get("source") if suggestion else None,
        "ai_confidence": (suggestion or {}).get("confidence"),
        "ai_needs_review": (suggestion or {}).get("needs_review"),
        "ai_model": (suggestion or {}).get("model"), "ai_provider": (suggestion or {}).get("provider"),
        "ai_prompt_version": (suggestion or {}).get("prompt_version"),
        "ai_created_at": (suggestion or {}).get("created_at"),
        "graded_by": teacher[1] if teacher else None, "graded_by_id": teacher[0] if teacher else None,
        "grading_seconds": secs,
        "criteria": crit_rows, "steps": base_steps or None, "first_error_step": first_err,
        "misconception": mis, "mistake_tags": tags,
        "feedback_edited": comment is not None and comment.strip() != explanation.strip(),
    }


# ---------------------------------------------------------------- whole-exam AI runs
def _run_key(exam_id: str) -> str:
    return f"scan:airun:{exam_id}"


async def _get_run(exam_id: str) -> dict:
    raw = await get_store().get(_run_key(exam_id))
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


async def _set_run(exam_id: str, run: dict) -> None:
    await get_store().set(_run_key(exam_id), json.dumps(run), ttl=24 * 3600)


def _run_out(exam_id: str, run: dict) -> dict:
    return {"exam_id": exam_id, "running": bool(run.get("running")), "total": int(run.get("total", 0)),
            "done": int(run.get("done", 0)), "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"), "ai_enabled": ai.enabled()}


class AiRunIn(BaseModel):
    question_ids: list[str] = Field(default_factory=list, max_length=500)
    redo: bool = False  # also re-draft answers that already have an AI suggestion


def pairs_to_grade(e: Any, attempts: list[Attempt], question_ids: set[str], redo: bool) -> list[tuple[str, str]]:
    """(attempt, question) pairs that need a suggestion: paper answers still pending, never
    already graded by the teacher."""
    out: list[tuple[str, str]] = []
    for a in attempts:
        for q in exam_questions(e):
            qid = q["id"]
            if question_ids and qid not in question_ids:
                continue
            ans = (a.answers or {}).get(qid)
            if not paper_answer(ans) or (a.grades or {}).get(qid) is not None:
                continue
            if not paper_given(q, ans, None)[1]:
                continue
            d = (a.ai_drafts or {}).get(qid)
            if not redo and d and d.get("source") not in ("fallback",):
                continue
            out.append((a.id, qid))
    return out


@router.get("/exams/{exam_id}/scan/ai-grade")
async def ai_run_status(exam_id: str, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    return _run_out(e.id, await _get_run(e.id))


@router.post("/exams/{exam_id}/scan/ai-grade", status_code=202)
async def ai_run_start(exam_id: str, body: AiRunIn | None = None, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    """Ask the AI for step-level suggestions on every pending paper answer of the exam (e.g.
    after writing the answer key). Teacher grades are never touched."""
    from ..routes_scan import _spawn, draft_answers  # late import: routes_scan imports this package
    e = await _own_exam(session, exam_id, p)
    if not ai.enabled():
        raise HTTPException(409, "AI is off on this server — grade by hand")
    run = await _get_run(e.id)
    if run.get("running"):
        raise HTTPException(409, "AI grading is already running for this exam")
    body = body or AiRunIn()
    attempts = list((await session.execute(select(Attempt).where(
        Attempt.exam_id == e.id, Attempt.status == "submitted"))).scalars())
    pairs = pairs_to_grade(e, attempts, set(body.question_ids), body.redo)
    run = {"running": bool(pairs), "total": len(pairs), "done": 0, "started_at": iso(utcnow()),
           "finished_at": None if pairs else iso(utcnow())}
    await _set_run(e.id, run)

    async def job():
        try:
            for i in range(0, len(pairs), 16):
                await draft_answers(e.id, pairs[i:i + 16])
                run["done"] = min(len(pairs), i + 16)
                await _set_run(e.id, run)
        except Exception:  # noqa: BLE001
            log.exception("AI run for %s failed", e.id)
        finally:
            run["running"], run["finished_at"] = False, iso(utcnow())
            await _set_run(e.id, run)

    if pairs:
        _spawn(job())
    out = _run_out(e.id, run)
    out["queued"] = len(pairs)
    return out
