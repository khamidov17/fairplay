"""Answer key + marking rubric per exam (Phase 2, AI paper grading).

Stored in ScanExamConfig.answer_key:
{
  "subject": "math" | "physics" | "chemistry" | "computer science" | "general",
  "text": "pasted answer key (and text extracted from uploaded key files)",
  "files": [{"id", "name", "kind": "pdf"|"image"|"text", "pages": [upload url...], "chars", "uploaded_at"}],
  "questions": {qid: {
      "solution": "worked solution / final answer",
      "criteria": [{"id": "c1", "label", "points", "description"}],   # sum == question points
      "partial_credit": "rules for partial credit",
      "common_mistakes": [{"label", "tag", "deduction": num|null}],
      "updated_at", "updated_by",
      "ai_suggestion": {solution, criteria, partial_credit, common_mistakes,
                        provider, model, prompt_version, created_at}   # draft only, never applied
  }},
  "updated_at"
}
Grading uses `effective_criteria(q, entry)`: the key's criteria, else the essay rubric of the
question (ids r1, r2, ...), else none.
"""
from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any

import pymupdf
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ai
from ..db import get_session
from ..grading import exam_questions, num
from ..models import Exam, User
from ..security import Principal, require_teacher
from ..timeutil import iso, utcnow
from ..uploads import save_image_bytes, sniff_image
from . import diagram_spec
from .config_models import ScanExamConfig

router = APIRouter(prefix="/api", tags=["scan-ai"])

SUBJECTS = ("math", "physics", "chemistry", "computer science", "general")

# Small fixed misconception lists per subject (plus "other" with free text). The AI must pick
# from these so the class dashboard can count them.
MISCONCEPTIONS: dict[str, list[str]] = {
    "math": ["sign error", "arithmetic error", "algebra error", "wrong formula", "misapplied rule",
             "wrong method", "missing step", "notation", "incomplete", "misread question",
             "no work shown", "conceptual misunderstanding"],
    "physics": ["sign error", "arithmetic error", "wrong formula", "units", "vector components",
                "free-body diagram", "missing step", "incomplete", "misread question", "no work shown",
                "conceptual misunderstanding"],
    "chemistry": ["balancing", "stoichiometry", "units", "significant figures", "wrong formula",
                  "arithmetic error", "incomplete", "misread question", "no work shown",
                  "conceptual misunderstanding"],
    "computer science": ["off-by-one", "wrong algorithm", "complexity", "syntax", "logic error",
                         "edge case missed", "incomplete", "misread question", "conceptual misunderstanding"],
    "general": ["factual error", "unsupported claim", "unclear explanation", "incomplete",
                "misread question", "no work shown", "conceptual misunderstanding"],
}

MAX_KEY_BYTES = 20 * 1024 * 1024
MAX_KEY_PAGES = 30
MAX_TEXT = 60_000
KEY_PROMPT_VERSION = "fp-key-draft-v1"


# ---------------------------------------------------------------- pure helpers
def misconceptions_for(subject: str | None) -> list[str]:
    return MISCONCEPTIONS.get(subject or "general", MISCONCEPTIONS["general"])


def norm_tag(s: Any) -> str:
    return " ".join(str(s or "").strip().lower().split())[:60]


def question_rubric_criteria(q: dict) -> list[dict]:
    return [{"id": f"r{i + 1}", "label": str(r.get("label", ""))[:200], "points": num(float(r.get("points", 0))),
             "description": ""} for i, r in enumerate(q.get("rubric") or [])]


def effective_criteria(q: dict, entry: dict | None) -> tuple[list[dict], str]:
    """(criteria, source) used for grading: key criteria, else the question's essay rubric.
    Graph / diagram questions use their feature checklist (source "diagram")."""
    feats = diagram_spec.feature_criteria(diagram_spec.effective(q, entry))  # graph / diagram hook
    if feats:
        return feats, "diagram"
    crit = list((entry or {}).get("criteria") or [])
    if crit:
        return crit, "key"
    rub = question_rubric_criteria(q)
    if rub:
        return rub, "question"
    return [], "none"


def clean_criteria(raw: Any, max_points: float, strict: bool = True) -> list[dict]:
    """Validate criteria; raises ValueError (strict) when points do not add up."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("criteria must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for i, c in enumerate(raw[:20]):
        if not isinstance(c, dict):
            raise ValueError("each criterion needs a label and points")
        label = " ".join(str(c.get("label") or "").split())[:200]
        if not label:
            raise ValueError("Each criterion needs a label")
        try:
            pts = float(c.get("points"))
        except (TypeError, ValueError):
            raise ValueError(f"Criterion “{label}” needs points") from None
        if pts != pts or pts < 0 or pts > 1000:
            raise ValueError(f"Criterion “{label}” has invalid points")
        cid = re.sub(r"[^A-Za-z0-9_-]", "", str(c.get("id") or ""))[:16] or f"c{i + 1}"
        while cid in seen:
            cid = f"c{i + 1}_{secrets.token_hex(2)}"
        seen.add(cid)
        out.append({"id": cid, "label": label, "points": num(pts),
                    "description": str(c.get("description") or "").strip()[:1000]})
    total = sum(float(c["points"]) for c in out)
    if out and strict and abs(total - float(max_points)) > 1e-6:
        raise ValueError(f"Rubric points must add up to {num(max_points)} (now {num(total)})")
    return out


def clean_mistakes(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for m in raw[:20]:
        if isinstance(m, str):
            m = {"label": m}
        if not isinstance(m, dict):
            continue
        label = " ".join(str(m.get("label") or "").split())[:200]
        if not label:
            continue
        ded = m.get("deduction")
        try:
            ded = None if ded in (None, "") else num(max(0.0, float(ded)))
        except (TypeError, ValueError):
            ded = None
        out.append({"label": label, "tag": norm_tag(m.get("tag")) or None, "deduction": ded})
    return out


def entry_out(q: dict, entry: dict | None) -> dict:
    entry = entry or {}
    crit, src = effective_criteria(q, entry)
    return {"solution": entry.get("solution", ""), "criteria": list(entry.get("criteria") or []),
            "partial_credit": entry.get("partial_credit", ""),
            "common_mistakes": list(entry.get("common_mistakes") or []),
            "updated_at": entry.get("updated_at"), "updated_by": entry.get("updated_by"),
            "effective_criteria": crit, "criteria_source": src}


def _correct_answer_text(q: dict) -> str:
    t = q.get("type")
    if t == "mcq":
        ci, opts = q.get("correct_index"), q.get("options") or []
        if isinstance(ci, int) and 0 <= ci < len(opts):
            return f"{'ABCDEFGH'[ci]}. {opts[ci]}"
    if t == "numeric" and q.get("answer_number") is not None:
        tol = q.get("tolerance") or 0
        return f"{num(float(q['answer_number']))}" + (f" ± {num(float(tol))}" if tol else "")
    if t == "short":
        return "; ".join(q.get("accepted_answers") or [])
    return ""


def key_context(key: dict, q: dict, limit: int = 4000) -> dict:
    """What the grader prompt needs for one question."""
    entry = (key.get("questions") or {}).get(q["id"]) or {}
    crit, _ = effective_criteria(q, entry)
    return {
        "subject": key.get("subject") or "general",
        "solution": entry.get("solution") or "",
        "correct_answer": _correct_answer_text(q),
        "criteria": crit,
        "partial_credit": entry.get("partial_credit") or "",
        "common_mistakes": list(entry.get("common_mistakes") or []),
        "key_text": (key.get("text") or "")[:limit],
        "diagram": diagram_spec.effective(q, entry),  # graph / diagram questions (or None)
    }


# ---------------------------------------------------------------- storage helpers
async def get_config(session: AsyncSession, exam_id: str, create: bool = False) -> ScanExamConfig | None:
    row = await session.get(ScanExamConfig, exam_id)
    if row is None and create:
        row = ScanExamConfig(exam_id=exam_id, answer_key={}, mapping={}, updated_at=utcnow())
        session.add(row)
    return row


async def load_key(session: AsyncSession, exam_id: str) -> dict:
    row = await session.get(ScanExamConfig, exam_id)
    return dict(row.answer_key or {}) if row else {}


async def _own_exam(session: AsyncSession, exam_id: str, p: Principal) -> Exam:
    e = await session.get(Exam, exam_id)
    if e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Exam not found")
    return e


def key_out(e: Exam, key: dict) -> dict:
    qs = exam_questions(e)
    entries = key.get("questions") or {}
    subject = key.get("subject") or "general"
    return {
        "exam_id": e.id, "subject": subject, "subjects": list(SUBJECTS),
        "misconceptions": misconceptions_for(subject),
        "text": key.get("text") or "", "files": list(key.get("files") or []),
        "ai_enabled": ai.enabled(), "updated_at": key.get("updated_at"),
        "questions": [{
            "question_id": q["id"], "index": i, "type": q.get("type"), "prompt": q.get("prompt", ""),
            "points": q.get("points", 1), "options": list(q.get("options") or []),
            "correct_answer": _correct_answer_text(q) or None,
            "key": entry_out(q, entries.get(q["id"])),
            "ai_suggestion": (entries.get(q["id"]) or {}).get("ai_suggestion"),
        } for i, q in enumerate(qs)],
    }


# ---------------------------------------------------------------- endpoints
@router.get("/exams/{exam_id}/answer-key")
async def get_answer_key(exam_id: str, p: Principal = Depends(require_teacher),
                         session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    return key_out(e, await load_key(session, e.id))


class KeyIn(BaseModel):
    subject: str | None = Field(default=None, max_length=40)
    text: str | None = Field(default=None, max_length=MAX_TEXT)


@router.put("/exams/{exam_id}/answer-key")
async def put_answer_key(exam_id: str, body: KeyIn, p: Principal = Depends(require_teacher),
                         session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    row = await get_config(session, e.id, create=True)
    key = dict(row.answer_key or {})
    if body.subject is not None:
        s = body.subject.strip().lower()
        if s not in SUBJECTS:
            raise HTTPException(422, "Pick a subject: " + ", ".join(SUBJECTS))
        key["subject"] = s
    if body.text is not None:
        key["text"] = body.text.strip()
    key["updated_at"] = iso(utcnow())
    row.answer_key, row.updated_at = key, utcnow()
    await session.commit()
    return key_out(e, key)


class CriterionIn(BaseModel):
    id: str | None = Field(default=None, max_length=16)
    label: str = Field(max_length=200)
    points: float
    description: str = Field(default="", max_length=1000)


class MistakeIn(BaseModel):
    label: str = Field(max_length=200)
    tag: str | None = Field(default=None, max_length=60)
    deduction: float | None = None


class QuestionKeyIn(BaseModel):
    solution: str = Field(default="", max_length=20_000)
    criteria: list[CriterionIn] = Field(default_factory=list, max_length=20)
    partial_credit: str = Field(default="", max_length=5000)
    common_mistakes: list[MistakeIn] = Field(default_factory=list, max_length=20)


@router.put("/exams/{exam_id}/answer-key/questions/{qid}")
async def put_question_key(exam_id: str, qid: str, body: QuestionKeyIn,
                           p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    q = next((x for x in exam_questions(e) if x["id"] == qid), None)
    if q is None:
        raise HTTPException(404, "Question not found")
    try:
        crit = clean_criteria([c.model_dump() for c in body.criteria], float(q.get("points", 1)))
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from None
    u = await session.get(User, p.user_id)
    row = await get_config(session, e.id, create=True)
    key = dict(row.answer_key or {})
    entries = dict(key.get("questions") or {})
    old = dict(entries.get(qid) or {})
    old.update({"solution": body.solution.strip(), "criteria": crit,
                "partial_credit": body.partial_credit.strip(),
                "common_mistakes": clean_mistakes([m.model_dump() for m in body.common_mistakes]),
                "updated_at": iso(utcnow()), "updated_by": u.username if u else None})
    entries[qid] = old
    key["questions"] = entries
    key["updated_at"] = iso(utcnow())
    row.answer_key, row.updated_at = key, utcnow()
    await session.commit()
    return {"question_id": qid, "key": entry_out(q, old), "ai_suggestion": old.get("ai_suggestion")}


# ---------------------------------------------------------------- key files
def _sniff_kind(head: bytes, filename: str) -> str | None:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if sniff_image(head):
        return "image"
    if filename.lower().endswith((".txt", ".md", ".tex", ".csv")):
        return "text"
    return None


def ingest_key_file(data: bytes, filename: str) -> dict:
    """Blocking: store page images of a PDF/image and extract text. Raises ValueError."""
    kind = _sniff_kind(data[:16], filename)
    if kind is None:
        raise ValueError("Upload the answer key as a PDF, an image (PNG/JPEG) or a text file")
    pages: list[str] = []
    text = ""
    if kind == "pdf":
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception:  # noqa: BLE001
            raise ValueError("This PDF cannot be opened") from None
        with doc:
            if doc.page_count > MAX_KEY_PAGES:
                raise ValueError(f"The answer key can have at most {MAX_KEY_PAGES} pages")
            parts = []
            for page in doc:
                parts.append(page.get_text().strip())
                pix = page.get_pixmap(dpi=110, colorspace=pymupdf.csRGB, alpha=False)
                pages.append(save_image_bytes(pix.tobytes("jpeg", jpg_quality=80))["url"])
            text = "\n\n".join(t for t in parts if t)
    elif kind == "image":
        if len(data) > 5 * 1024 * 1024:
            raise ValueError("Answer key images can be at most 5 MB")
        pages.append(save_image_bytes(data)["url"])
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    return {"id": secrets.token_hex(6), "name": (filename or "answer key")[:200], "kind": kind,
            "pages": pages, "chars": len(text), "text": text[:MAX_TEXT], "uploaded_at": iso(utcnow())}


@router.post("/exams/{exam_id}/answer-key/files", status_code=201)
async def upload_key_file(exam_id: str, file: UploadFile = File(...), p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    data = await file.read(MAX_KEY_BYTES + 1)
    if len(data) > MAX_KEY_BYTES:
        raise HTTPException(413, "The answer key file can be at most 20 MB")
    try:
        info = await asyncio.to_thread(ingest_key_file, data, file.filename or "")
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from None
    row = await get_config(session, e.id, create=True)
    key = dict(row.answer_key or {})
    files = list(key.get("files") or [])
    if len(files) >= 10:
        raise HTTPException(409, "Remove a file first (at most 10 answer key files)")
    text = info.pop("text")
    info["text_excerpt"] = text[:300]
    files.append(info)
    key["files"] = files
    if text.strip():
        key["text"] = ((key.get("text") or "") + f"\n\n[{info['name']}]\n" + text.strip()).strip()[:MAX_TEXT]
    key["updated_at"] = iso(utcnow())
    row.answer_key, row.updated_at = key, utcnow()
    await session.commit()
    return key_out(e, key)


@router.delete("/exams/{exam_id}/answer-key/files/{file_id}")
async def delete_key_file(exam_id: str, file_id: str, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    row = await get_config(session, e.id)
    key = dict(row.answer_key or {}) if row else {}
    files = list(key.get("files") or [])
    if not any(f.get("id") == file_id for f in files):
        raise HTTPException(404, "File not found")
    key["files"] = [f for f in files if f.get("id") != file_id]
    row.answer_key, row.updated_at = key, utcnow()
    await session.commit()
    return key_out(e, key)


# ---------------------------------------------------------------- AI drafts of rubric + worked solution
KEY_SYSTEM = ("You help a university teacher write a marking scheme. From the question and the "
              "answer key, write a worked solution and a rubric with point criteria that add up "
              "exactly to the maximum points, rules for partial credit, and common student mistakes. "
              "Reply with ONE JSON object only.")
KEY_SCHEMA = ('{"solution": "step-by-step worked solution", "criteria": [{"label": "what earns the points", '
              '"points": number, "description": "how to judge it"}], "partial_credit": "rules", '
              '"common_mistakes": [{"label": "mistake", "tag": "one of the allowed tags", "deduction": number}]}')


def key_prompt(q: dict, key: dict) -> str:
    ctx = key_context(key, q, limit=6000)
    parts = [f"Subject: {ctx['subject']}", f"Question type: {q.get('type')}",
             f"Question: {q.get('prompt', '')}", f"Maximum points: {num(float(q.get('points', 1)))}"]
    if q.get("options"):
        parts.append("Options: " + "; ".join(f"{'ABCDEFGH'[i]}. {o}" for i, o in enumerate(q["options"])))
    if ctx["correct_answer"]:
        parts.append(f"Correct answer: {ctx['correct_answer']}")
    if ctx["solution"]:
        parts.append(f"Teacher's solution so far: {ctx['solution']}")
    if ctx["key_text"]:
        parts.append(f"Answer key document (may cover several questions):\n{ctx['key_text']}")
    parts.append("Allowed mistake tags: " + ", ".join(misconceptions_for(ctx["subject"])))
    parts.append(f"Return JSON exactly like: {KEY_SCHEMA}")
    return "\n".join(parts)


def parse_key_suggestion(out: dict, q: dict) -> dict:
    """Clean an AI rubric draft. Criteria that do not add up are rescaled (it is a draft)."""
    max_points = float(q.get("points", 1))
    raw = out.get("criteria") if isinstance(out.get("criteria"), list) else []
    try:
        crit = clean_criteria(raw, max_points, strict=False)
    except ValueError:
        crit = []
    total = sum(float(c["points"]) for c in crit)
    if crit and total > 0 and abs(total - max_points) > 1e-6:
        scaled = [round(float(c["points"]) * max_points / total * 2) / 2 for c in crit]
        scaled[-1] = round(max_points - sum(scaled[:-1]), 2)
        if scaled[-1] >= 0:
            for c, s in zip(crit, scaled):
                c["points"] = num(s)
        else:
            crit = []
    return {"solution": str(out.get("solution") or "").strip()[:20_000], "criteria": crit,
            "partial_credit": str(out.get("partial_credit") or "").strip()[:5000],
            "common_mistakes": clean_mistakes(out.get("common_mistakes")),
            "provider": ai.provider(), "model": ai.model_name(), "prompt_version": KEY_PROMPT_VERSION,
            "created_at": iso(utcnow())}


class KeyDraftIn(BaseModel):
    question_ids: list[str] = Field(default_factory=list, max_length=200)


@router.post("/exams/{exam_id}/answer-key/ai-draft")
async def ai_draft_key(exam_id: str, body: KeyDraftIn | None = None, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    """AI drafts a worked solution + rubric per question. Stored as `ai_suggestion` (a draft the
    teacher copies into the key and edits); never applied by itself."""
    e = await _own_exam(session, exam_id, p)
    if not ai.enabled():
        raise HTTPException(409, "AI is off on this server — write the rubric by hand")
    key = await load_key(session, e.id)
    wanted = set((body.question_ids if body else []) or [])
    qs = [q for q in exam_questions(e) if not wanted or q["id"] in wanted]
    images: list[bytes] = []
    if not (key.get("text") or "").strip():
        from .storage import read_url
        for f in (key.get("files") or [])[:2]:
            for url in (f.get("pages") or [])[:2]:
                img = await asyncio.to_thread(read_url, url)
                if img:
                    images.append(img)
    sem = asyncio.Semaphore(4)

    async def one(q: dict) -> tuple[str, dict | None]:
        async with sem:
            try:
                out = await ai.complete_json(KEY_SYSTEM, key_prompt(q, key), images=images[:3], max_tokens=1500)
            except ai.AIUnavailable:
                return q["id"], None
            return q["id"], parse_key_suggestion(out, q)

    results = await asyncio.gather(*(one(q) for q in qs))
    row = await get_config(session, e.id, create=True)
    key = dict(row.answer_key or {})
    entries = dict(key.get("questions") or {})
    drafted = 0
    for qid, sug in results:
        if sug is None:
            continue
        ent = dict(entries.get(qid) or {})
        ent["ai_suggestion"] = sug
        entries[qid] = ent
        drafted += 1
    key["questions"] = entries
    row.answer_key, row.updated_at = key, utcnow()
    await session.commit()
    out = key_out(e, key)
    out["drafted"] = drafted
    out["failed"] = len(qs) - drafted
    return out
