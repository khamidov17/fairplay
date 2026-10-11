"""Views and server-side answer checking for practice sets."""
from __future__ import annotations

from typing import Any

from .. import mathcheck
from ..grading import is_answered, normalize_short
from ..mathcheck import MathError
from ..timeutil import iso
from .models import PracticeAttempt, PracticeSet

REVEAL_AFTER_TRIES = 2


def teacher_item(it: dict) -> dict:
    out = dict(it)
    out.setdefault("note", "")
    out.setdefault("verification", None)
    return out


def student_item(it: dict) -> dict:
    out = {"id": it["id"], "type": it.get("type"), "prompt": it.get("prompt", ""),
           "options": list(it.get("options") or []), "images": list(it.get("images") or []),
           "topics": list(it.get("topics") or []), "math": bool(it.get("math"))}
    if it.get("type") == "essay":
        out["rubric"] = list(it.get("rubric") or [])
    return out


def correct_answer(it: dict) -> Any:
    t = it.get("type")
    if t == "mcq":
        ci = it.get("correct_index")
        opts = it.get("options") or []
        return {"index": ci, "text": opts[ci] if isinstance(ci, int) and 0 <= ci < len(opts) else None}
    if t == "numeric":
        v = it.get("answer_number")
        return None if v is None else mathcheck.format_number(float(v))
    if t == "short":
        acc = it.get("accepted_answers") or []
        return acc[0] if acc else None
    return None


def check_answer(it: dict, given: Any) -> dict:
    """{"correct": bool|None, "readable": bool, "feedback": str}. Sync: call via asyncio.to_thread."""
    t = it.get("type")
    if not is_answered(given):
        return {"correct": False, "readable": False, "feedback": "Write an answer first."}
    if t == "mcq":
        if isinstance(given, bool):
            return {"correct": False, "readable": False, "feedback": "Pick one of the options."}
        try:
            idx = int(given)
        except (TypeError, ValueError):
            return {"correct": False, "readable": False, "feedback": "Pick one of the options."}
        ok = idx == it.get("correct_index")
        return {"correct": ok, "readable": True, "feedback": "Correct!" if ok else "Not quite."}
    if t == "numeric":
        if it.get("answer_number") is None:
            return {"correct": None, "readable": True, "feedback": "Saved."}
        try:
            mathcheck.parse_number(given)
        except MathError as e:
            return {"correct": False, "readable": False,
                    "feedback": f"We couldn't read that as a number ({e}). Try e.g. 12, 0.5 or 3/4."}
        ok = mathcheck.numbers_equal(given, float(it["answer_number"]), it.get("tolerance"))
        return {"correct": ok, "readable": True, "feedback": "Correct!" if ok else "Not quite."}
    if t == "short":
        acc = [str(a) for a in it.get("accepted_answers") or []]
        g = str(given)
        if any(normalize_short(g) == normalize_short(a) for a in acc):
            return {"correct": True, "readable": True, "feedback": "Correct!"}
        if it.get("math"):
            try:
                mathcheck.parse(g)
            except MathError as e:
                return {"correct": False, "readable": False,
                        "feedback": f"We couldn't read that as math ({e})."}
            for a in acc:
                if mathcheck.equivalent(g, a):
                    return {"correct": True, "readable": True, "feedback": "Correct — same as the expected answer."}
        return {"correct": False, "readable": True, "feedback": "Not quite."}
    if t == "essay":
        return {"correct": None, "readable": True,
                "feedback": "Saved. Compare your answer with the guide below — essays aren't marked automatically."}
    return {"correct": None, "readable": True, "feedback": "Saved."}


def result_out(it: dict, att: PracticeAttempt | None) -> dict | None:
    if att is None:
        return None
    reveal = att.correct is True or att.ever_correct or att.tries >= REVEAL_AFTER_TRIES or att.correct is None
    return {"given": att.answer, "correct": att.correct, "tries": att.tries, "ever_correct": att.ever_correct,
            "explanation": (it.get("explanation") or "") if reveal else "",
            "correct_answer": correct_answer(it) if reveal else None,
            "answered_at": iso(att.updated_at)}


def progress(items: list[dict], atts: dict[str, PracticeAttempt]) -> dict:
    ids = {it["id"] for it in items}
    answered = sum(1 for k in atts if k in ids)
    correct = sum(1 for k, a in atts.items() if k in ids and (a.ever_correct or a.correct))
    return {"answered": answered, "correct": correct, "total": len(items)}


def set_summary(s: PracticeSet, student: Any, atts: dict[str, PracticeAttempt] | None = None) -> dict:
    items = s.items or []
    return {"id": s.id, "username": student.username if student else None,
            "name": student.name if student else None, "status": s.status, "title": s.title,
            "topics": list(s.topics or []), "formative": True, "item_count": len(items),
            "needs_answer": sum(1 for it in items if it.get("status") == "needs_answer"),
            "verified": sum(1 for it in items if it.get("status") == "verified"),
            "created_at": iso(s.created_at), "approved_at": iso(s.approved_at), "sent_at": iso(s.sent_at),
            "progress": progress(items, atts or {})}
