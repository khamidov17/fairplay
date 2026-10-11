from __future__ import annotations

import math
from typing import Any


def normalize_short(s: str) -> str:
    return " ".join(s.strip().lower().split())


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def is_answered(v: Any) -> bool:
    return v is not None and not (isinstance(v, str) and not v.strip())


def grade_question(q: dict, given: Any) -> bool:
    if not is_answered(given):
        return False
    t = q["type"]
    if t == "mcq":
        if isinstance(given, bool):
            return False
        if isinstance(given, float) and given.is_integer():
            given = int(given)
        return isinstance(given, int) and given == q.get("correct_index")
    if t == "numeric":
        g = _number(given)
        if g is None or q.get("answer_number") is None:
            return False
        tol = q.get("tolerance") or 0.0
        return abs(g - float(q["answer_number"])) <= float(tol) + 1e-09
    if t == "short":
        if not isinstance(given, str):
            given = str(given)
        g = normalize_short(given)
        return any(g == normalize_short(a) for a in (q.get("accepted_answers") or []))
    return False


def grade(questions: list[dict], answers: dict) -> tuple[float, list[dict]]:
    """Returns (score, per-question rows) per the contract."""
    score = 0.0
    rows = []
    for q in questions:
        given = answers.get(q["id"]) if answers else None
        ok = grade_question(q, given)
        pts = q.get("points", 1) if ok else 0
        score += pts
        rows.append({"question_id": q["id"], "given": given if is_answered(given) else None,
                     "correct": ok, "points_awarded": pts})
    return score, rows


def num(x: float) -> int | float:
    """Render whole numbers as ints in JSON (7 not 7.0)."""
    return int(x) if float(x).is_integer() else round(x, 4)


# ---------------------------------------------------------------- slice 2: teacher grades
# One helper for score / needs_grading, shared by submit/close (stored score), results,
# the grading queue, the gradebook and the student grade views.

MANUAL_TYPES = ("essay", "external")

# slice 4: an exam held on another website stores one hidden grading item. Students never
# see it (their view has no questions); the teacher enters the score from the website.
EXTERNAL_QID = "q_external"


def external_question(points: float) -> dict:
    return {"id": EXTERNAL_QID, "type": "external", "prompt": "Score from the exam website",
            "points": num(float(points)), "options": [], "correct_index": None,
            "answer_number": None, "tolerance": None, "accepted_answers": [],
            "rubric": [], "min_words": None, "max_words": None}


def exam_questions(e: Any) -> list[dict]:
    """Questions used for grading. External exams created before slice 4 had none: give
    them the hidden grading item (100 points) so teachers can still enter scores."""
    qs = list(getattr(e, "questions", None) or [])
    if getattr(e, "mode", "questions") == "external" and not qs:
        qs = [external_question(100)]
    return qs


def auto_points(q: dict, given: Any) -> float | None:
    """Automatic points for one answer (slice-1 rules); None for essays (never auto-graded)."""
    if q.get("type") in MANUAL_TYPES:
        return None
    return q.get("points", 1) if grade_question(q, given) else 0


def grade_attempt(questions: list[dict], answers: dict | None,
                  grades: dict | None) -> tuple[float, int, list[dict]]:
    """Returns (score, needs_grading, rows). Per question:
    teacher grade -> its points (override, any type); essay without grade -> blank: 0 and
    graded, else pending (0, not graded); otherwise automatic grading."""
    answers = answers or {}
    grades = grades or {}
    score = 0.0
    pending_n = 0
    rows = []
    for q in questions:
        qid = q["id"]
        given = answers.get(qid)
        g = grades.get(qid)
        paper = paper_answer(given)  # slice 6: scanned paper answer (crop image)
        paper_pending = False
        if paper:
            given, paper_pending = paper_given(q, given, g)
        answered = is_answered(given)
        max_points = q.get("points", 1)
        pending = False
        if g is not None:
            pts = float(g.get("points", 0))
        elif paper_pending:
            pts = 0.0
            pending = True
        elif q.get("type") == "essay":
            pts = 0.0
            pending = answered
        elif q.get("type") == "external":
            # slice 4: score from the exam website, entered by the teacher; pending until then.
            pts = 0.0
            pending = True
        else:
            pts = float(auto_points(q, given) or 0)
        if pending:
            pending_n += 1
        score += pts
        rows.append({
            "question_id": qid, "given": given if answered else None,
            "correct": not pending and abs(pts - max_points) < 1e-9,
            "points_awarded": num(pts), "max_points": max_points,
            "manual": g is not None, "pending": pending,
            "comment": (g or {}).get("comment", "") or "",
            "rubric_scores": (g or {}).get("rubric_scores"),
        })
        if paper:
            rows[-1]["image"] = answers[qid].get("image")
    return score, pending_n, rows


# ---------------------------------------------------------------- slice 6: paper answers
# A scanned answer is stored as {"image": url, "page": n, "mcq": idx|None,
# "marks": "one"|"none"|"multiple" (mcq only), "blank": bool}. MCQ with exactly one
# bubble read is graded automatically like a computer answer; everything else written on
# paper is pending until the teacher approves a grade (AI drafts are never applied).
PAPER_PLACEHOLDER = "(written on paper)"


def paper_answer(v: Any) -> bool:
    return isinstance(v, dict) and "image" in v


def paper_given(q: dict, v: dict, g: dict | None) -> tuple[Any, bool]:
    """(effective answer value, pending without a teacher grade) for a paper answer."""
    if q.get("type") == "mcq":
        marks = v.get("marks")
        if marks == "one" and isinstance(v.get("mcq"), int):
            return v["mcq"], False
        if marks == "none":
            return None, False
        return PAPER_PLACEHOLDER, True
    transcript = (g or {}).get("transcript")
    if isinstance(transcript, str) and transcript.strip():
        return transcript, True
    return PAPER_PLACEHOLDER, True


def percent(score: float, total: float) -> float:
    return round(score / total * 100, 1) if total else 0.0


def word_count(v: Any) -> int:
    return len(v.split()) if isinstance(v, str) else 0
