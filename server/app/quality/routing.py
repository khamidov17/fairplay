"""Which AI-suggested grades a teacher must look at by hand (review routing).

`route_item(exam, attempt, qid, policy=None, weak=None) -> {"review": bool, "reasons": [...]}`
is pure (no I/O) so the Phase 2 review screen can call it on objects it already loaded.
`policy` defaults to DEFAULT_POLICY; `weak` = weak areas from benchmark runs
(`{"types": {type: kappa}, "subjects": {subject: kappa}}`, see service.weak_areas).
Every reason is `{"code", "message"}`; codes:

  no_ai_suggestion      no usable AI draft (missing, or the "AI is off" fallback)
  confidence_unknown    the draft has no confidence value
  low_confidence        confidence < policy.confidence_threshold
  illegible_handwriting the AI said the handwriting is hard to read
  ai_flagged            the AI set needs_review (reason passed through)
  near_boundary         the attempt total with the AI's points is within
                        policy.boundary_margin points of a grade boundary
  criteria_inconsistent per-criterion points do not add up to the total (or exceed a criterion)
  points_out_of_range   suggested points < 0 or > the question's points
  weak_question_type    benchmark kappa for this question type < policy.weak_kappa
  weak_subject          benchmark kappa for the exam's subject/course or a topic < policy.weak_kappa

Phase 2 drafts may carry: confidence, needs_review, needs_review_reason (or review_reason),
illegible, criteria / criterion_points / rubric_scores (numbers or {"points","max_points"}).
Anything missing is "unknown" and only triggers the rule that is about it.
"""
from __future__ import annotations

import math
from typing import Any

from ..grading import exam_questions, grade_attempt, num

DEFAULT_POLICY: dict[str, Any] = {
    "confidence_threshold": 0.7,
    "review_illegible": True,
    "review_ai_flagged": True,
    "boundary_margin": 1.0,                  # points; 0 turns the rule off
    "grade_boundaries": [50, 60, 70, 80, 90],  # percent of the exam total
    "check_criteria": True,
    "weak_kappa": 0.6,                       # benchmark kappa below this = weak area
    "weak_min_items": 5,                     # ... measured on at least this many items
    "review_without_ai": True,
}

ILLEGIBLE_WORDS = ("illegib", "unreadable", "hard to read", "can't read", "cannot read", "handwriting",
                   "unclear writing", "unclear mark")

MESSAGES = {
    "no_ai_suggestion": "No AI suggestion: grade by hand",
    "confidence_unknown": "The AI did not say how sure it is",
    "illegible_handwriting": "Handwriting may be hard to read",
    "criteria_inconsistent": "The AI's per-criterion points do not add up",
    "points_out_of_range": "The AI's points are outside the allowed range",
}


def merge_policy(*layers: dict | None) -> dict:
    out = dict(DEFAULT_POLICY)
    for layer in layers:
        for k, v in (layer or {}).items():
            if k in DEFAULT_POLICY and v is not None:
                out[k] = v
    return out


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _reason(code: str, message: str | None = None) -> dict:
    return {"code": code, "message": message or MESSAGES.get(code, code)}


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def usable_draft(draft: Any) -> bool:
    return isinstance(draft, dict) and draft.get("source") != "fallback" and \
        _num(draft.get("suggested_points", draft.get("points"))) is not None


def _draft_points(draft: dict) -> float:
    return _num(draft.get("suggested_points", draft.get("points"))) or 0.0


def _illegible(draft: dict) -> bool:
    if draft.get("illegible") is True or draft.get("handwriting_illegible") is True:
        return True
    texts = [str(draft.get(k) or "") for k in ("needs_review_reason", "review_reason", "reason")]
    texts += [str(t) for t in (draft.get("mistake_tags") or []) if isinstance(t, str)]
    blob = " ".join(texts).lower()
    return any(w in blob for w in ILLEGIBLE_WORDS)


def _criteria(draft: dict) -> list | None:
    for k in ("criteria", "criterion_points", "rubric_scores"):
        v = draft.get(k)
        if isinstance(v, list) and v:
            return v
    return None


def criteria_problem(draft: dict, q: dict) -> str | None:
    crit = _criteria(draft)
    if crit is None:
        return None
    rubric = q.get("rubric") or []
    total = 0.0
    for i, c in enumerate(crit):
        pts = _num(c.get("points")) if isinstance(c, dict) else _num(c)
        if pts is None:
            return "A criterion has no points"
        cap = _num(c.get("max_points")) if isinstance(c, dict) else None
        if cap is None and i < len(rubric) and isinstance(rubric[i], dict):
            cap = _num(rubric[i].get("points"))
        if pts < -1e-9 or (cap is not None and pts > cap + 1e-9):
            return f"Criterion {i + 1} has {num(pts)} points (allowed 0–{num(cap or 0)})"
        total += pts
    pts = _draft_points(draft)
    if abs(total - pts) > 1e-6:
        return f"Criteria add up to {num(total)} but the total is {num(pts)}"
    return None


def projected_score(exam: Any, attempt: Any, qid: str, ai_points: float) -> float:
    """Attempt total if this answer got the AI's points; other answers use their teacher
    grade, else their AI draft, else automatic grading."""
    grades = dict(_get(attempt, "grades") or {})
    drafts = _get(attempt, "ai_drafts") or {}
    for other, d in drafts.items():
        if other != qid and other not in grades and usable_draft(d):
            grades[other] = {"points": _draft_points(d)}
    grades[qid] = {"points": ai_points}
    score, _, _ = grade_attempt(exam_questions(exam), _get(attempt, "answers") or {}, grades)
    return score


def _subjects_for(exam: Any, q: dict) -> list[str]:
    out = []
    course = (_get(exam, "course") or "").strip().lower()
    if course:
        out.append(course)
    for t in q.get("topics") or []:
        if isinstance(t, str) and t.strip():
            out.append(" ".join(t.split()).lower())
    return out


def route_item(exam: Any, attempt: Any, qid: str, policy: dict | None = None,
               weak: dict | None = None) -> dict:
    """{"review": bool, "reasons": [{"code","message"}]} for one answer (see module doc)."""
    policy = merge_policy(policy)
    q = next((x for x in exam_questions(exam) if x["id"] == qid), None)
    if q is None:
        return {"review": False, "reasons": []}
    draft = (_get(attempt, "ai_drafts") or {}).get(qid)
    reasons: list[dict] = []
    if not usable_draft(draft):
        if policy["review_without_ai"]:
            reasons.append(_reason("no_ai_suggestion"))
        return {"review": bool(reasons), "reasons": reasons}

    max_points = float(q.get("points", 1))
    pts = _draft_points(draft)
    conf = _num(draft.get("confidence"))
    thr = float(policy["confidence_threshold"])
    if conf is None:
        reasons.append(_reason("confidence_unknown"))
    elif conf < thr:
        reasons.append(_reason("low_confidence", f"AI confidence {conf:.2f} is below {thr:.2f}"))
    illegible = _illegible(draft)
    if illegible and policy["review_illegible"]:
        reasons.append(_reason("illegible_handwriting"))
    if draft.get("needs_review") is True and policy["review_ai_flagged"] and not illegible:
        why = str(draft.get("needs_review_reason") or draft.get("review_reason") or "").strip()
        reasons.append(_reason("ai_flagged", f"The AI asked for a review{': ' + why[:200] if why else ''}"))
    if pts < -1e-9 or pts > max_points + 1e-9:
        reasons.append(_reason("points_out_of_range",
                               f"The AI suggested {num(pts)} of {num(max_points)} points"))
    if policy["check_criteria"]:
        prob = criteria_problem(draft, q)
        if prob:
            reasons.append(_reason("criteria_inconsistent", prob))

    margin = float(policy["boundary_margin"] or 0)
    total = float(_get(exam, "total_points") or 0) or sum(float(x.get("points", 1)) for x in exam_questions(exam))
    if margin > 0 and total > 0 and policy["grade_boundaries"]:
        score = projected_score(exam, attempt, qid, pts)
        for b in sorted(policy["grade_boundaries"]):
            cut = float(b) / 100.0 * total
            if abs(score - cut) <= margin + 1e-9:
                reasons.append(_reason(
                    "near_boundary",
                    f"With the AI's points the total is {num(score)}, within {num(margin)} of the "
                    f"{num(float(b))}% boundary ({num(cut)} points)"))
                break

    weak = weak or {}
    t = q.get("type", "")
    if t in (weak.get("types") or {}):
        k = weak["types"][t]
        reasons.append(_reason("weak_question_type",
                               f"The AI agrees poorly with teachers on {t} questions (kappa {k:.2f})"))
    for s in _subjects_for(exam, q):
        if s in (weak.get("subjects") or {}):
            k = weak["subjects"][s]
            reasons.append(_reason("weak_subject", f"The AI agrees poorly with teachers on “{s}” (kappa {k:.2f})"))
            break
    return {"review": bool(reasons), "reasons": reasons}
