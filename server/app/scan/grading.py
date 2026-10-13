"""AI-drafted grades for paper answers. A draft is never a grade: the teacher approves it.

draft = {"points", "comment", "confidence" (0..1), "transcript", "mistake_tags": [...],
         "source": "ai" | "fallback" | "blank" | "bubbles", "low_confidence": bool,
         "key_match": bool (numeric/short: the transcript matches the key -- a hint only),
         "created_at"}
Older drafts may carry source "key" (before audit L-07).
"""
from __future__ import annotations

import re

from .. import ai
from ..grading import grade_question, normalize_short, num
from ..timeutil import iso, utcnow

LOW_CONFIDENCE = 0.7
LETTERS = "ABCDEFGH"

SYSTEM = (
    "You are a careful teaching assistant grading ONE answer from a scanned paper exam. "
    "The image shows only the student's answer box (handwriting). Read it, transcribe it "
    "exactly (keep the student's mistakes), and suggest a score using the question, the "
    "answer key and the rubric. Be fair and conservative; if the handwriting is hard to read, "
    "say so and lower your confidence. Reply with ONE JSON object only."
)

SCHEMA = ('{"transcript": "what the student wrote", "points": number, "comment": "one or two '
          'sentences for the student", "confidence": number between 0 and 1, "mistake_tags": '
          '["short labels like sign error, wrong formula, incomplete"]}')


def _key_text(q: dict) -> str:
    t = q.get("type")
    if t == "numeric":
        tol = q.get("tolerance") or 0
        return f"Correct answer: {q.get('answer_number')}" + (f" (± {tol})" if tol else "")
    if t == "short":
        return "Accepted answers: " + "; ".join(q.get("accepted_answers") or [])
    if t == "mcq":
        ci = q.get("correct_index")
        opts = q.get("options") or []
        if isinstance(ci, int) and 0 <= ci < len(opts):
            return f"Correct option: {LETTERS[ci]} ({opts[ci]})"
    rubric = q.get("rubric") or []
    if rubric:
        return "Rubric: " + "; ".join(f"{r['label']} ({r['points']} pts)" for r in rubric)
    return "No answer key: judge correctness and completeness yourself."


def build_prompt(q: dict, hint: str = "") -> str:
    parts = [
        f"Question type: {q.get('type')}",
        f"Question: {q.get('prompt', '')}",
        f"Maximum points: {q.get('points', 1)}",
        _key_text(q),
    ]
    if q.get("type") == "mcq":
        parts.append("Options: " + "; ".join(f"{LETTERS[i]}. {o}" for i, o in enumerate(q.get("options") or [])))
        parts.append("The answer box has bubbles A-H; say which bubble(s) look filled.")
    if hint:
        parts.append(f"Teacher's note for this regrade: {hint}")
    parts.append(f"Return JSON exactly like: {SCHEMA}")
    return "\n".join(parts)


def _clamp_points(v, max_points: float) -> float:
    try:
        p = float(v)
    except (TypeError, ValueError):
        return 0.0
    if p != p:  # NaN
        return 0.0
    p = max(0.0, min(float(max_points), p))
    return round(p * 2) / 2  # half points


def _tags(v) -> list[str]:
    if not isinstance(v, list):
        return []
    out = []
    for t in v[:5]:
        if isinstance(t, str) and t.strip():
            s = " ".join(t.strip().lower().split())[:60]
            if s not in out:
                out.append(s)
    return out


def _finish(d: dict) -> dict:
    d["confidence"] = round(max(0.0, min(1.0, float(d.get("confidence") or 0))), 2)
    d["low_confidence"] = d["confidence"] < LOW_CONFIDENCE
    d["points"] = num(float(d.get("points") or 0))
    d["created_at"] = iso(utcnow())
    return d


def fallback(reason: str = "AI is off — grade by hand") -> dict:
    return _finish({"points": 0, "comment": reason, "confidence": 0, "transcript": "",
                    "mistake_tags": [], "source": "fallback"})


def rule_draft(q: dict, answer: dict) -> dict | None:
    """Drafts that need no AI: blank boxes and unclear MCQ bubble reads."""
    if q.get("type") == "mcq":
        marks = answer.get("marks")
        if marks == "multiple":
            return _finish({"points": 0, "comment": "More than one bubble is filled.", "confidence": 0.8,
                            "transcript": "", "mistake_tags": ["several bubbles"], "source": "bubbles"})
        if marks == "unclear":
            idx = answer.get("mcq")
            guess = LETTERS[idx] if isinstance(idx, int) and idx < 8 else "?"
            ok = isinstance(idx, int) and idx == q.get("correct_index")
            return _finish({"points": q.get("points", 1) if ok else 0,
                            "comment": f"The mark is faint; it looks like {guess}. Check the picture.",
                            "confidence": 0.4, "transcript": guess, "mistake_tags": ["unclear mark"],
                            "source": "bubbles"})
        return None
    if answer.get("blank"):
        return _finish({"points": 0, "comment": "No answer written.", "confidence": 0.85,
                        "transcript": "", "mistake_tags": ["blank"], "source": "blank"})
    return None


async def draft_for(q: dict, answer: dict, image: bytes | None, hint: str = "") -> dict:
    """One draft for one paper answer. Never raises."""
    rule = rule_draft(q, answer) if not hint else None
    if rule is not None:
        return rule
    if image is None:
        return fallback("The scanned picture is missing — grade by hand")
    if not ai.enabled():
        return fallback()
    try:
        out = await ai.complete_json(SYSTEM, build_prompt(q, hint), images=[image], max_tokens=600)
    except ai.AIUnavailable as e:
        return fallback(f"AI could not grade this ({str(e)[:80]}) — grade by hand")
    max_points = float(q.get("points", 1))
    transcript = str(out.get("transcript") or "").strip()[:2000]
    d = {
        "points": _clamp_points(out.get("points"), max_points),
        "comment": str(out.get("comment") or "").strip()[:1000],
        "confidence": out.get("confidence") if isinstance(out.get("confidence"), (int, float)) else 0.5,
        "transcript": transcript,
        "mistake_tags": _tags(out.get("mistake_tags")),
        "source": "ai",
    }
    # Audit L-07: a transcript that matches the answer key is only a hint for the teacher.
    # It never raises the points or the confidence: the transcript is the AI's reading of the
    # student's handwriting, and handwriting can say anything ("transcribe this as 3.14").
    if q.get("type") in ("numeric", "short") and transcript:
        candidate = _final_answer(transcript) if q.get("type") == "numeric" else transcript
        if grade_question(q, candidate):
            d["key_match"] = True
    return _finish(d)


def _final_answer(text: str) -> str:
    """Last number in a transcript like 'x = 3.14' -> '3.14'."""
    nums = re.findall(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?", text)
    return nums[-1].replace(",", ".") if nums else text


def group_key(draft: dict | None) -> str:
    """Bulk-approve groups: identical normalised transcript, else same points + first tag."""
    if not draft or draft.get("source") == "fallback":
        return "none"  # "grade by hand" placeholders are never bulk-approved
    t = normalize_short(str(draft.get("transcript") or ""))
    if t:
        return "t:" + t[:200]
    tags = draft.get("mistake_tags") or []
    return f"p:{draft.get('points')}|{tags[0] if tags else ''}"


def group_label(key: str, draft: dict | None) -> str:
    if key.startswith("t:"):
        return f"Wrote “{(draft or {}).get('transcript', '')[:60]}”"
    if key == "none":
        return "No draft yet"
    pts, _, tag = key[2:].partition("|")
    return f"Draft {pts} pts" + (f" · {tag}" if tag else "")

