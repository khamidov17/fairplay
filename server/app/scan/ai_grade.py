"""Step-level AI grading suggestions for one paper answer (Phase 2).

A suggestion is a DRAFT stored in attempt.ai_drafts[qid]; it never becomes a grade until a
teacher approves it. Record shape (also documented in CONTRACT.md "Phase 2"):

{
  "points": 3.5, "max_points": 5,                        # suggested total, clamped to [0, max]
  "model_points": 4,                                      # what the model said before checks (null if none)
  "criteria": [{"id", "label", "points", "max_points", "met": bool, "note"}],
  "steps": [{"n": 1, "text": "student's step", "status": "correct"|"error"|"missing", "note"}],
  "first_error_step": 2 | null,                           # 1-based, first step where reasoning breaks
  "explanation": "for the student", "comment": same as explanation (legacy field),
  "teacher_note": "short note for the teacher",
  "misconception": {"tag": "<fixed list tag>"|"other", "text": "free text"} | null,
  "mistake_tags": ["sign error", ...],                    # legacy, feeds insights
  "transcript": "what the student wrote",
  "confidence": 0.82, "low_confidence": false,
  "legibility": "clear"|"partly"|"illegible"|null,
  "needs_review": bool, "review_reasons": ["low_confidence", ...], "review_reason": "readable text",
  "source": "ai"|"key"|"fallback"|"blank"|"bubbles",
  "provider": "openai", "model": "gpt-4o-mini", "prompt_version": "fp-step-grade-v1",
  "created_at": ISO, "image": url (set by the caller)
}
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from .. import ai
from ..grading import grade_question, num
from ..timeutil import iso, utcnow
from . import diagrams
from .answer_key import misconceptions_for, norm_tag

PROMPT_VERSION = "fp-step-grade-v1"
LOW_CONFIDENCE = 0.7
RETRIES = 2
LETTERS = "ABCDEFGH"

REVIEW_TEXT = {
    "low_confidence": "AI is unsure",
    "illegible": "Handwriting is hard to read",
    "partly_legible": "Some handwriting is hard to read",
    "points_out_of_range": "AI gave points outside the allowed range",
    "missing_criteria": "AI skipped some rubric criteria",
    "total_mismatch": "AI total did not match its criteria",
    "no_steps": "AI could not list the student's steps",
    "ai_off": "AI is off — grade by hand",
    "ai_failed": "AI could not grade this — grade by hand",
    "no_image": "The scanned picture is missing",
    "unclear_mark": "Faint or multiple bubble marks",
}

SYSTEM = (
    "You are a careful, fair teaching assistant grading ONE answer from a scanned paper exam. "
    "The image(s) show the student's handwritten or typed work for this question. "
    "1) Transcribe the student's work as a list of steps, exactly as written (keep their mistakes). "
    "2) Mark each step correct, error, or missing (a needed step the student skipped), with a short note. "
    "3) Award points per rubric criterion using the answer key and partial-credit rules; never more "
    "than a criterion's maximum. 4) Write an explanation FOR THE STUDENT: kind, specific, say what "
    "went wrong and how to fix it (not just the score). 5) Pick ONE misconception tag from the allowed "
    "list (or \"other\" with a short text, or \"none\" if the answer is fully correct). "
    "If the handwriting is hard to read, say so in legibility and lower your confidence. "
    "Reply with ONE JSON object only."
)

SCHEMA = (
    '{"legibility": "clear|partly|illegible", "transcript": "the student\'s work as plain text", '
    '"steps": [{"text": "student step", "status": "correct|error|missing", "note": "short"}], '
    '"first_error_step": 1-based number or null, '
    '"criteria": [{"id": "criterion id", "points": number, "note": "short"}], '
    '"points": number, "explanation": "2-4 sentences for the student", '
    '"teacher_note": "one sentence for the teacher", '
    '"misconception": {"tag": "allowed tag|other|none", "text": "free text"}, '
    '"confidence": number between 0 and 1}'
)


# ---------------------------------------------------------------- prompt
def build_prompt(q: dict, ctx: dict, hint: str = "") -> str:
    maxp = num(float(q.get("points", 1)))
    parts = [
        f"Prompt version: {PROMPT_VERSION}",
        f"Subject: {ctx.get('subject') or 'general'}",
        f"Question type: {q.get('type')}",
        f"Question: {q.get('prompt', '')}",
        f"Maximum points: {maxp}",
    ]
    if q.get("type") == "mcq":
        parts.append("Options: " + "; ".join(f"{LETTERS[i]}. {o}" for i, o in enumerate(q.get("options") or [])))
    if ctx.get("correct_answer"):
        parts.append(f"Correct answer: {ctx['correct_answer']}")
    if ctx.get("solution"):
        parts.append(f"Worked solution from the teacher:\n{ctx['solution']}")
    crit = ctx.get("criteria") or []
    if crit:
        parts.append("Rubric criteria (award each between 0 and its points):\n" + "\n".join(
            f"- id {c['id']}: {c['label']} ({num(float(c['points']))} pts)"
            + (f" — {c['description']}" if c.get("description") else "") for c in crit))
    else:
        parts.append("No rubric: judge correctness and completeness yourself; return criteria as [].")
    if ctx.get("partial_credit"):
        parts.append(f"Partial credit rules: {ctx['partial_credit']}")
    if ctx.get("common_mistakes"):
        parts.append("Common mistakes to look for: " + "; ".join(
            m["label"] + (f" (tag: {m['tag']})" if m.get("tag") else "")
            + (f" (-{num(float(m['deduction']))})" if m.get("deduction") is not None else "")
            for m in ctx["common_mistakes"]))
    if ctx.get("key_text") and not ctx.get("solution"):
        parts.append(f"Answer key document (may cover several questions):\n{ctx['key_text']}")
    if ctx.get("whole_page"):
        parts.append("The image is the whole answer page: grade only the answer to THIS question.")
    parts.append("Allowed misconception tags: " + ", ".join(misconceptions_for(ctx.get("subject"))))
    if hint:
        parts.append(f"Teacher's note for this regrade: {hint}")
    parts.append(f"Return JSON exactly like: {SCHEMA}")
    # ---- hook: graph / chart / diagram questions (feature checklist, app.scan.diagrams)
    extra = diagrams.prompt_extra(q, ctx)
    if extra:
        parts.append(extra)
    return "\n".join(parts)


# ---------------------------------------------------------------- parsing
def _float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


_STATUS = {
    "correct": "correct", "ok": "correct", "right": "correct", "true": "correct", "valid": "correct",
    "error": "error", "wrong": "error", "incorrect": "error", "mistake": "error", "false": "error",
    "missing": "missing", "omitted": "missing", "skipped": "missing", "absent": "missing",
}


def parse_steps(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for s in raw[:40]:
        if isinstance(s, str):
            s = {"text": s, "status": "correct"}
        if not isinstance(s, dict):
            continue
        text = str(s.get("text") or s.get("step") or "").strip()[:500]
        status = _STATUS.get(str(s.get("status") or "").strip().lower())
        if status is None:
            ok = s.get("correct")
            status = "correct" if ok is True else "error" if ok is False else "error"
        if not text and status != "missing":
            continue
        out.append({"n": len(out) + 1, "text": text, "status": status,
                    "note": str(s.get("note") or "").strip()[:300]})
    return out


def first_error(steps: list[dict], claimed: Any) -> int | None:
    c = _float(claimed)
    if c is not None and float(c).is_integer() and 1 <= int(c) <= len(steps) and \
            steps[int(c) - 1]["status"] != "correct":
        return int(c)
    for s in steps:
        if s["status"] != "correct":
            return s["n"]
    return None


def parse_criteria(raw: Any, criteria: list[dict]) -> tuple[list[dict], bool]:
    """Per-criterion points aligned to the rubric. Returns (rows, some_missing)."""
    given: dict[str, dict] = {}
    if isinstance(raw, list):
        by_label = {" ".join(c["label"].lower().split()): c["id"] for c in criteria}
        for i, r in enumerate(raw):
            if not isinstance(r, dict):
                continue
            cid = str(r.get("id") or "").strip()
            if cid not in {c["id"] for c in criteria}:
                cid = by_label.get(" ".join(str(r.get("label") or "").lower().split()), "")
            if not cid and len(raw) == len(criteria):
                cid = criteria[i]["id"]  # positional fallback
            if cid and cid not in given:
                given[cid] = r
    rows: list[dict] = []
    missing = False
    for c in criteria:
        mx = float(c["points"])
        r = given.get(c["id"])
        if r is None:
            missing = True
            pts, note = 0.0, "Not assessed by AI"
        else:
            v = _float(r.get("points"))
            if v is None:
                missing = True
                v = 0.0
            pts = max(0.0, min(mx, v))
            note = str(r.get("note") or "").strip()[:300]
        rows.append({"id": c["id"], "label": c["label"], "points": num(round(pts, 2)), "max_points": num(mx),
                     "met": abs(pts - mx) < 1e-9 and mx > 0, "note": note})
    return rows, missing


def parse_misconception(raw: Any, subject: str | None, legacy_tags: Any) -> dict | None:
    allowed = misconceptions_for(subject)
    tag, text = "", ""
    if isinstance(raw, dict):
        tag, text = norm_tag(raw.get("tag")), str(raw.get("text") or "").strip()[:200]
    elif isinstance(raw, str):
        tag = norm_tag(raw)
    if not tag and isinstance(legacy_tags, list) and legacy_tags:
        tag = norm_tag(legacy_tags[0])
    if not tag or tag in ("none", "no mistake", "n/a"):
        return None
    if tag not in allowed:
        # keep the model's wording as free text, bucket as "other"
        text = text or tag
        tag = "other"
    return {"tag": tag, "text": text}


def _legacy_tags(v: Any) -> list[str]:
    out: list[str] = []
    if isinstance(v, list):
        for t in v[:5]:
            s = norm_tag(t)
            if s and s not in out:
                out.append(s)
    return out


def _final_answer(text: str) -> str:
    nums = re.findall(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?", text)
    return nums[-1].replace(",", ".") if nums else text


def meta() -> dict:
    return {"provider": ai.provider(), "model": ai.model_name(), "prompt_version": PROMPT_VERSION}


def finalize(d: dict, q: dict, reasons: list[str] | None = None) -> dict:
    """Fill every field of the record (also for rule drafts and fallbacks)."""
    max_points = float(q.get("points", 1))
    conf = _float(d.get("confidence"))
    d["confidence"] = round(max(0.0, min(1.0, conf if conf is not None else 0.0)), 2)
    d["low_confidence"] = d["confidence"] < LOW_CONFIDENCE
    pts = _float(d.get("points")) or 0.0
    d["points"] = num(round(max(0.0, min(max_points, pts)), 2))
    d["max_points"] = num(max_points)
    d.setdefault("model_points", None)
    d.setdefault("criteria", [])
    d.setdefault("steps", [])
    d.setdefault("first_error_step", None)
    d.setdefault("comment", "")
    d.setdefault("explanation", d.get("comment", ""))
    d["comment"] = d["explanation"]
    d.setdefault("teacher_note", "")
    d.setdefault("misconception", None)
    d.setdefault("mistake_tags", [])
    d.setdefault("transcript", "")
    d.setdefault("legibility", None)
    rs = list(dict.fromkeys(list(reasons or []) + list(d.get("review_reasons") or [])))
    if d["low_confidence"] and d.get("source") in ("ai", "key") and "low_confidence" not in rs:
        rs.append("low_confidence")
    d["review_reasons"] = rs
    d["needs_review"] = bool(rs)
    d["review_reason"] = "; ".join(REVIEW_TEXT.get(r, r) for r in rs)
    for k, v in meta().items():
        d.setdefault(k, v)
    d["created_at"] = iso(utcnow())
    return d


def parse_suggestion(out: Any, q: dict, ctx: dict) -> dict:
    """Turn the model's JSON (possibly junk) into a suggestion record. Never raises."""
    if not isinstance(out, dict):
        out = {}
    max_points = float(q.get("points", 1))
    reasons: list[str] = []
    steps = parse_steps(out.get("steps"))
    transcript = str(out.get("transcript") or "").strip()[:4000]
    if not transcript and steps:
        transcript = "\n".join(s["text"] for s in steps if s["text"])
    legibility = str(out.get("legibility") or "").strip().lower() or None
    if legibility not in (None, "clear", "partly", "illegible"):
        legibility = "partly" if "part" in legibility else "illegible" if "illeg" in legibility else "clear"
    model_points = _float(out.get("points"))
    crit_rows: list[dict] = []
    criteria = ctx.get("criteria") or []
    if criteria:
        crit_rows, missing = parse_criteria(out.get("criteria"), criteria)
        if missing:
            reasons.append("missing_criteria")
        total = sum(float(c["points"]) for c in crit_rows)
        if model_points is not None and abs(model_points - total) > 0.01 and not missing:
            reasons.append("total_mismatch")
        points = total
    else:
        points = model_points if model_points is not None else 0.0
        points = round(points * 2) / 2
    if model_points is not None and not (-1e-9 <= model_points <= max_points + 1e-9):
        reasons.append("points_out_of_range")
    points = max(0.0, min(max_points, points))
    conf = _float(out.get("confidence"))
    conf = 0.5 if conf is None else conf
    if conf > 1 and conf <= 100:
        conf = conf / 100.0  # "85" meaning 85 %
    if legibility == "illegible":
        reasons.append("illegible")
        conf = min(conf, 0.4)
    elif legibility == "partly":
        reasons.append("partly_legible")
        conf = min(conf, 0.69)
    if not steps and not transcript:
        reasons.append("no_steps")
    explanation = str(out.get("explanation") or out.get("comment") or out.get("feedback") or "").strip()[:2000]
    mis = parse_misconception(out.get("misconception"), ctx.get("subject"), out.get("mistake_tags"))
    tags = _legacy_tags(out.get("mistake_tags"))
    if mis:
        first = mis["tag"] if mis["tag"] != "other" else norm_tag(mis["text"])
        if first and first not in tags:
            tags.insert(0, first)
    d = {
        "points": points, "model_points": None if model_points is None else num(model_points),
        "criteria": crit_rows, "steps": steps, "first_error_step": first_error(steps, out.get("first_error_step")),
        "explanation": explanation, "teacher_note": str(out.get("teacher_note") or "").strip()[:500],
        "misconception": mis, "mistake_tags": tags[:5], "transcript": transcript,
        "confidence": conf, "legibility": legibility, "source": "ai",
    }
    # numeric / short: a transcript that matches the key is full marks (still a draft)
    if q.get("type") in ("numeric", "short") and transcript:
        candidate = _final_answer(transcript) if q.get("type") == "numeric" else transcript
        if grade_question(q, candidate):
            d["points"] = max_points
            d["source"] = "key"
            d["confidence"] = max(float(d["confidence"]), 0.9)
            for c in d["criteria"]:
                c["points"], c["met"] = c["max_points"], True
            reasons = [r for r in reasons if r not in ("missing_criteria", "total_mismatch", "no_steps")]
            if not d["explanation"]:
                d["explanation"] = "Matches the answer key."
    # ---- hook: graph / chart / diagram questions (checklist results, app.scan.diagrams)
    d = diagrams.parse_extra(out, d, q, ctx, reasons)
    return finalize(d, q, reasons)


def fallback(q: dict, reason_code: str, message: str | None = None) -> dict:
    return finalize({"points": 0, "explanation": message or REVIEW_TEXT[reason_code], "confidence": 0,
                     "source": "fallback"}, q, [reason_code])


def _concurrency() -> int:
    try:
        return max(1, min(32, int(os.environ.get("FAIRPLAY_AI_CONCURRENCY", "4"))))
    except ValueError:
        return 4


async def suggest(q: dict, ctx: dict, images: list[bytes], hint: str = "") -> dict:
    """Ask the AI for one step-level suggestion, with retries. Never raises."""
    if not ai.enabled():
        return fallback(q, "ai_off")
    if not images:
        return fallback(q, "no_image", "The scanned picture is missing — grade by hand")
    last = ""
    for attempt in range(RETRIES + 1):
        try:
            out = await ai.complete_json(SYSTEM, build_prompt(q, ctx, hint), images=images, max_tokens=1500)
            return parse_suggestion(out, q, ctx)
        except ai.AIUnavailable as e:
            last = str(e)
            if attempt < RETRIES:
                await asyncio.sleep(0.2 * (3 ** attempt))
    return fallback(q, "ai_failed", f"AI could not grade this ({last[:80]}) — grade by hand")
