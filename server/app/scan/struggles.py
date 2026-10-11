"""Where students struggled, for one exam (Frizzle-style class view, kept light).

Per question: average score over graded answers, and the most common misconception tags
with counts and the students affected. Approved grades count first; for answers not graded
yet the AI suggestion's tag is counted separately (`count` includes them, `approved` does
not). The same tags also feed the course-wide insights (app.analytics reads
grade.mistake_tags / ai_drafts[qid].mistake_tags).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..grading import exam_questions, grade_attempt, num
from ..models import Attempt, User
from ..security import Principal, require_teacher
from .answer_key import _own_exam
from .review import MAX_GRADING_SECONDS

router = APIRouter(prefix="/api", tags=["scan-ai"])

ESTIMATE = {"essay": 120, "mcq": 10}


def _label(m: dict | None) -> str | None:
    if not m:
        return None
    tag = m.get("tag")
    if tag == "other":
        return " ".join(str(m.get("text") or "other").lower().split())[:60] or "other"
    return tag or None


@router.get("/exams/{exam_id}/scan/struggles")
async def struggles(exam_id: str, p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    qs = exam_questions(e)
    rows = (await session.execute(
        select(Attempt, User.username, User.name).join(User, User.id == Attempt.student_id)
        .where(Attempt.exam_id == e.id, Attempt.status == "submitted")
        .order_by(User.name, User.username))).all()
    stats = {q["id"]: {"graded": 0, "total": 0, "sum": 0.0, "tags": {}} for q in qs}
    t_graded, t_seconds, t_estimate = 0, 0.0, 0
    for a, username, name in rows:
        _, _, grows = grade_attempt(qs, a.answers, a.grades)
        for q, r in zip(qs, grows):
            st = stats[q["id"]]
            st["total"] += 1  # unanswered counts as an answer worth 0
            g = (a.grades or {}).get(q["id"])
            d = (a.ai_drafts or {}).get(q["id"])
            label, approved = None, False
            if not r["pending"]:
                st["graded"] += 1
                st["sum"] += float(r["points_awarded"])
                if g is not None:
                    label, approved = _label(g.get("misconception")), True
                    if label is None and g.get("mistake_tags") and float(r["points_awarded"]) < float(q.get("points", 1)):
                        label = str(g["mistake_tags"][0])
                    secs = g.get("grading_seconds")
                    if isinstance(secs, (int, float)) and 0 <= secs <= MAX_GRADING_SECONDS:
                        t_graded += 1
                        t_seconds += float(secs)
                        t_estimate += ESTIMATE.get(q.get("type"), 30)
            elif d and d.get("source") not in (None, "fallback"):
                label = _label(d.get("misconception"))
            if label:
                tag = st["tags"].setdefault(label, {"tag": label, "count": 0, "approved": 0, "students": []})
                tag["count"] += 1
                tag["approved"] += int(approved)
                tag["students"].append({"username": username, "name": name})
    out = []
    for i, q in enumerate(qs):
        st = stats[q["id"]]
        mx = float(q.get("points", 1))
        avg = st["sum"] / st["graded"] if st["graded"] else None
        tags = sorted(st["tags"].values(), key=lambda t: (-t["count"], t["tag"]))[:6]
        out.append({"question_id": q["id"], "index": i, "prompt": q.get("prompt", ""), "points": num(mx),
                    "graded": st["graded"], "total": st["total"],
                    "avg_points": None if avg is None else num(round(avg, 2)),
                    "avg_percent": None if avg is None or not mx else round(avg / mx * 100, 1),
                    "misconceptions": tags})
    return {"exam_id": e.id, "questions": out,
            "time": {"graded": t_graded, "seconds": round(t_seconds, 1), "estimate_manual_seconds": t_estimate}}
