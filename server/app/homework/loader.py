"""Turn submitted attempts into progress `Item`s (one query for exams, one for attempts)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..analytics import exam_date, question_topics
from ..grading import exam_questions, grade_attempt, paper_answer
from ..models import Attempt, Exam
from ..scan.struggles import _label
from .models import HomeworkAssignment
from .progress import Item


def _label_for(q: dict, row: dict, grade: dict | None, draft: dict | None, use_drafts: bool) -> tuple[str | None, bool]:
    """(misconception label, approved) for one answer that lost points."""
    if row["pending"]:
        if not use_drafts or not draft or draft.get("source") in (None, "fallback"):
            return None, False
        try:
            if float(draft.get("points", 0)) >= float(q.get("points", 1)):
                return None, False
        except (TypeError, ValueError):
            return None, False
        lab = _label(draft.get("misconception"))
        if lab is None and draft.get("mistake_tags"):
            lab = str(draft["mistake_tags"][0])
        return lab, False
    if float(row["points_awarded"]) >= float(q.get("points", 1)) - 1e-9:
        return None, False
    if grade is not None:
        lab = _label(grade.get("misconception"))
        if lab is None and grade.get("mistake_tags"):
            lab = str(grade["mistake_tags"][0])
        return lab, True
    return None, False


async def load_items(session: AsyncSession, *, teacher_id: int | None, student_ids: list[int] | None,
                     released_only: bool = False) -> list[Item]:
    stmt = select(Exam)
    if teacher_id is not None:
        stmt = stmt.where(Exam.teacher_id == teacher_id)
    if released_only:
        stmt = stmt.where(Exam.grades_released.is_(True))
    if student_ids is not None:
        if not student_ids:
            return []
        stmt = stmt.where(Exam.id.in_(select(Attempt.exam_id).where(Attempt.student_id.in_(student_ids))))
    exams = {e.id: e for e in (await session.execute(stmt)).scalars() if not getattr(e, "formative", False)}
    if not exams:
        return []
    hw = set((await session.execute(select(HomeworkAssignment.exam_id).where(
        HomeworkAssignment.exam_id.in_(list(exams))))).scalars())
    q = select(Attempt).where(Attempt.exam_id.in_(list(exams)), Attempt.status == "submitted")
    if student_ids is not None:
        q = q.where(Attempt.student_id.in_(student_ids))
    out: list[Item] = []
    qcache: dict[str, list[dict]] = {}
    for a in (await session.execute(q)).scalars():
        e = exams[a.exam_id]
        qs = qcache.setdefault(e.id, exam_questions(e))
        _, _, rows = grade_attempt(qs, a.answers, a.grades)
        date = a.submitted_at or exam_date(e)
        kind = "homework" if e.id in hw else ("paper" if (a.source or "") == "paper" else "computer")
        for qd, row in zip(qs, rows):
            if qd.get("type") == "external":
                continue
            qid = qd["id"]
            g = (a.grades or {}).get(qid)
            d = (a.ai_drafts or {}).get(qid)
            lab, approved = _label_for(qd, row, g, d, use_drafts=not released_only)
            ans = (a.answers or {}).get(qid)
            comment = (g or {}).get("comment") or ("" if released_only else (d or {}).get("explanation") or "")
            out.append(Item(
                student_id=a.student_id, exam_id=e.id, exam_title=e.title, question_id=qid,
                topics=question_topics(qd, e.course or ""),
                awarded=None if row["pending"] else float(row["points_awarded"]),
                max_points=float(row["max_points"] or 0), date=date, label=lab, approved=approved,
                image=ans.get("image") if paper_answer(ans) else None, prompt=qd.get("prompt", ""),
                comment=str(comment or ""), kind=kind))
    return out
