"""Phase 3 — grading quality API: benchmark, AI-vs-human metrics, review routing,
second marking and moderation. See CONTRACT.md "Phase 3 — Grading quality".

Permissions: everything is scoped to the requesting teacher (benchmark sets, runs, exams
they own). The only cross-teacher access is a second marker reading and marking the
answers assigned to them, blind (no first mark, no AI suggestion, no student name).
"""
from __future__ import annotations

import asyncio
import io
import math
import random
import zipfile
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ai, uploads
from ..db import get_session
from ..grading import exam_questions, grade_attempt, is_answered, num, paper_answer, percent
from ..models import Attempt, Exam, User
from ..security import Principal, require_teacher
from ..services import new_id
from ..timeutil import iso, utcnow
from . import graders, importer, metrics, routing, runner, service
from .models import (BenchmarkItem, BenchmarkRun, BenchmarkRunItem, BenchmarkSet, RoutingPolicy,
                     SecondMark)

router = APIRouter(prefix="/api", tags=["quality"])

UPLOAD_URL = r"^/api/uploads/[A-Za-z0-9_-]{16,64}$"
MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_CSV_BYTES = 5 * 1024 * 1024
CONSENT_DETAIL = "Tick the consent box: only answers the student agreed to share may be added to a benchmark"


# ---------------------------------------------------------------- helpers
async def _own_exam(session: AsyncSession, exam_id: str, p: Principal) -> Exam:
    e = await session.get(Exam, exam_id)
    if e is None or e.teacher_id != p.user_id:
        raise HTTPException(404, "Exam not found")
    return e


async def _own_set(session: AsyncSession, set_id: str, p: Principal) -> BenchmarkSet:
    s = await session.get(BenchmarkSet, set_id)
    if s is None or s.teacher_id != p.user_id:
        raise HTTPException(404, "Benchmark set not found")
    return s


def _question(e: Exam, qid: str) -> tuple[int, dict]:
    for i, q in enumerate(exam_questions(e)):
        if q["id"] == qid:
            return i, q
    raise HTTPException(404, "Question not found")


def _answer_view(q: dict, value: Any, grade: dict | None = None) -> dict:
    """Readable answer without marks: {"text", "image"}."""
    if paper_answer(value):
        text = None
        if q.get("type") == "mcq" and value.get("marks") == "one" and isinstance(value.get("mcq"), int):
            opts = q.get("options") or []
            text = opts[value["mcq"]] if value["mcq"] < len(opts) else None
        return {"text": text, "image": value.get("image")}
    if not is_answered(value):
        return {"text": None, "image": None}
    if q.get("type") == "mcq" and isinstance(value, int):
        opts = q.get("options") or []
        return {"text": opts[value] if 0 <= value < len(opts) else str(value), "image": None}
    return {"text": str(value), "image": None}


def _question_out(i: int, q: dict) -> dict:
    return {"question_id": q["id"], "index": i, "type": q.get("type"), "prompt": q.get("prompt", ""),
            "points": q.get("points", 1), "rubric": list(q.get("rubric") or []),
            "options": list(q.get("options") or []), "images": list(q.get("images") or [])}


def _answer_key(q: dict) -> str:
    t = q.get("type")
    if t == "numeric" and q.get("answer_number") is not None:
        return str(num(float(q["answer_number"])))
    if t == "short":
        return " | ".join(q.get("accepted_answers") or [])
    if t == "mcq" and isinstance(q.get("correct_index"), int):
        opts = q.get("options") or []
        ci = q["correct_index"]
        return opts[ci] if 0 <= ci < len(opts) else str(ci)
    return ""


# ---------------------------------------------------------------- benchmark sets
class SetIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=100)
    consent_note: str = Field(default="", max_length=2000)
    consent_basis: str = Field(default="", max_length=64)


@router.get("/quality/graders")
async def list_graders(p: Principal = Depends(require_teacher)):
    return {"graders": graders.grader_names(), "default": graders.default_grader_name(),
            "ai_enabled": ai.enabled()}


@router.get("/quality/benchmarks")
async def list_sets(p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    sets = (await session.execute(select(BenchmarkSet).where(BenchmarkSet.teacher_id == p.user_id)
                                  .order_by(BenchmarkSet.created_at.desc(), BenchmarkSet.id))).scalars().all()
    counts = await service.set_counts(session, [s.id for s in sets])
    latest = {r.set_id: r for r in await service.latest_runs(session, p.user_id)}
    return [service.set_out(s, counts.get(s.id, 0), latest.get(s.id)) for s in sets]


@router.post("/quality/benchmarks", status_code=201)
async def create_set(body: SetIn, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    s = BenchmarkSet(id=new_id("bs"), teacher_id=p.user_id, name=body.name.strip(), subject=body.subject.strip(),
                     consent_note=body.consent_note.strip(), consent_basis=body.consent_basis.strip(),
                     created_at=utcnow())
    session.add(s)
    await session.commit()
    return service.set_out(s)


@router.get("/quality/benchmarks/{set_id}")
async def get_set(set_id: str, p: Principal = Depends(require_teacher),
                  session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, set_id, p)
    items = (await session.execute(select(BenchmarkItem).where(BenchmarkItem.set_id == s.id)
                                   .order_by(BenchmarkItem.created_at, BenchmarkItem.id))).scalars().all()
    runs = (await session.execute(select(BenchmarkRun).where(BenchmarkRun.set_id == s.id)
                                  .order_by(BenchmarkRun.created_at.desc(), BenchmarkRun.id))).scalars().all()
    out = service.set_out(s, len(items), next((r for r in runs if r.status == "done"), None))
    out["items"] = [service.item_out(i) for i in items]
    out["runs"] = [service.run_out(r) for r in runs]
    return out


@router.put("/quality/benchmarks/{set_id}")
async def update_set(set_id: str, body: SetIn, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, set_id, p)
    s.name, s.subject = body.name.strip(), body.subject.strip()
    s.consent_note, s.consent_basis = body.consent_note.strip(), body.consent_basis.strip()
    await session.commit()
    return service.set_out(s, (await service.set_counts(session, [s.id])).get(s.id, 0))


@router.delete("/quality/benchmarks/{set_id}", status_code=204)
async def delete_set(set_id: str, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, set_id, p)
    run_ids = list((await session.execute(select(BenchmarkRun.id).where(BenchmarkRun.set_id == s.id))).scalars())
    if run_ids:
        await session.execute(delete(BenchmarkRunItem).where(BenchmarkRunItem.run_id.in_(run_ids)))
    await session.execute(delete(BenchmarkRun).where(BenchmarkRun.set_id == s.id))
    await session.execute(delete(BenchmarkItem).where(BenchmarkItem.set_id == s.id))
    await session.delete(s)
    await session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------- benchmark items
class RubricIn(BaseModel):
    label: str = Field(min_length=1, max_length=300)
    points: float = Field(ge=0, le=1000)


class ItemIn(BaseModel):
    question_prompt: str = Field(min_length=1, max_length=20000)
    question_type: str = Field(default="essay", pattern=r"^(mcq|numeric|short|essay|work|external)$")
    subject: str = Field(default="", max_length=100)
    max_points: float = Field(gt=0, le=1000)
    rubric: list[RubricIn] = Field(default_factory=list, max_length=30)
    answer_key: str = Field(default="", max_length=2000)
    answer_text: str = Field(default="", max_length=50000)
    answer_image: str | None = Field(default=None, pattern=UPLOAD_URL)
    human_marks: list[float] = Field(min_length=1, max_length=5)
    human_comment: str = Field(default="", max_length=5000)
    consent: bool = False


def _check_consent(consent: bool) -> None:
    if consent is not True:
        raise HTTPException(422, CONSENT_DETAIL)


@router.post("/quality/benchmarks/{set_id}/items", status_code=201)
async def add_item(set_id: str, body: ItemIn, p: Principal = Depends(require_teacher),
                   session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, set_id, p)
    _check_consent(body.consent)
    if not body.answer_text.strip() and not body.answer_image:
        raise HTTPException(422, "Give the answer text or an answer image")
    if any(not 0 <= m <= body.max_points for m in body.human_marks):
        raise HTTPException(422, f"Marks must be between 0 and {num(body.max_points)}")
    if body.rubric and abs(sum(r.points for r in body.rubric) - body.max_points) > 1e-6:
        raise HTTPException(422, "Rubric points must add up to max_points")
    now = utcnow()
    it = BenchmarkItem(
        id=new_id("bi"), set_id=s.id, question_prompt=body.question_prompt.strip(),
        question_type=body.question_type, subject=body.subject.strip() or s.subject,
        max_points=body.max_points, rubric=[r.model_dump() for r in body.rubric],
        answer_key=body.answer_key.strip(), answer_text=body.answer_text, answer_image=body.answer_image,
        human_marks=list(body.human_marks), human_points=body.human_marks[0],
        human_comment=body.human_comment, consent=True, consent_by=p.user_id, consent_at=now, created_at=now)
    session.add(it)
    await session.commit()
    return service.item_out(it)


class FromGradeIn(BaseModel):
    exam_id: str = Field(max_length=32)
    attempt_id: str = Field(max_length=32)
    question_id: str = Field(max_length=64)
    consent: bool = False
    human_comment: str | None = Field(default=None, max_length=5000)


@router.post("/quality/benchmarks/{set_id}/items/from-grade", status_code=201)
async def add_from_grade(set_id: str, body: FromGradeIn, p: Principal = Depends(require_teacher),
                         session: AsyncSession = Depends(get_session)):
    """Copy a graded answer (question, answer, teacher mark) into the benchmark. The student's
    name is not copied; the source ids are kept so the item can be traced by its owner."""
    s = await _own_set(session, set_id, p)
    _check_consent(body.consent)
    e = await _own_exam(session, body.exam_id, p)
    _, q = _question(e, body.question_id)
    a = await session.get(Attempt, body.attempt_id)
    if a is None or a.exam_id != e.id or a.status != "submitted":
        raise HTTPException(404, "Attempt not found")
    g = service.first_grade(a, q["id"])
    if g is None:
        raise HTTPException(409, "Grade this answer first")
    exists = (await session.execute(select(BenchmarkItem.id).where(
        BenchmarkItem.set_id == s.id, BenchmarkItem.source_attempt_id == a.id,
        BenchmarkItem.source_question_id == q["id"]))).first()
    if exists:
        raise HTTPException(409, "This answer is already in the benchmark set")
    value = (a.answers or {}).get(q["id"])
    view = _answer_view(q, value)
    text = view["text"] or ""
    if paper_answer(value) and not text:
        text = str(g.get("transcript") or "")
    if not text and not view["image"]:
        raise HTTPException(409, "This answer is blank")
    marks = [float(g["points"])]
    sm = (await session.execute(select(SecondMark).where(
        SecondMark.exam_id == e.id, SecondMark.attempt_id == a.id, SecondMark.question_id == q["id"]))).scalar()
    if sm is not None and sm.second_points is not None:
        marks.append(float(sm.second_points))
    now = utcnow()
    it = BenchmarkItem(
        id=new_id("bi"), set_id=s.id, question_prompt=q.get("prompt", ""), question_type=q.get("type", "essay"),
        subject=s.subject or (e.course or ""), max_points=float(q.get("points", 1)),
        rubric=list(q.get("rubric") or []), answer_key=_answer_key(q), answer_text=text,
        answer_image=view["image"], human_marks=marks, human_points=marks[0],
        human_comment=body.human_comment if body.human_comment is not None else str(g.get("comment") or ""),
        source_exam_id=e.id, source_attempt_id=a.id, source_question_id=q["id"],
        consent=True, consent_by=p.user_id, consent_at=now, created_at=now)
    session.add(it)
    await session.commit()
    return service.item_out(it)


@router.delete("/quality/benchmarks/{set_id}/items/{item_id}", status_code=204)
async def delete_item(set_id: str, item_id: str, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, set_id, p)
    it = await session.get(BenchmarkItem, item_id)
    if it is None or it.set_id != s.id:
        raise HTTPException(404, "Item not found")
    await session.delete(it)
    await session.commit()
    return Response(status_code=204)


def _zip_images(data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        total = 0
        for info in zf.infolist():
            if info.is_dir() or info.file_size > uploads.MAX_UPLOAD_BYTES:
                continue
            total += info.file_size
            if total > MAX_ZIP_BYTES:
                raise ValueError("The zip is too large once unpacked")
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if name and not name.startswith("."):
                out[name.lower()] = zf.read(info)
    return out


@router.post("/quality/benchmarks/{set_id}/import")
async def import_items(set_id: str, file: UploadFile = File(...), images: UploadFile | None = File(None),
                       consent: str = Form(""), p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, set_id, p)
    _check_consent(consent.strip().lower() in ("1", "true", "yes", "on"))
    data = await file.read(MAX_CSV_BYTES + 1)
    if len(data) > MAX_CSV_BYTES:
        raise HTTPException(413, "The CSV can be at most 5 MB")
    rows, errors, ignored = importer.parse_csv(data)
    pics: dict[str, bytes] = {}
    if images is not None:
        blob = await images.read(MAX_ZIP_BYTES + 1)
        if len(blob) > MAX_ZIP_BYTES:
            raise HTTPException(413, "The images zip can be at most 200 MB")
        if blob:
            try:
                pics = await asyncio.to_thread(_zip_images, blob)
            except (zipfile.BadZipFile, ValueError) as e:
                raise HTTPException(422, f"Cannot read the images zip: {e}") from None
    now = utcnow()
    added = 0
    for r in rows:
        url = None
        if r["image"]:
            img = pics.get(r["image"].replace("\\", "/").rsplit("/", 1)[-1].lower())
            if img is None:
                errors.append({"row": r["row"], "error": f"Image '{r['image']}' is not in the zip"})
                continue
            try:
                url = (await asyncio.to_thread(uploads.save_image_bytes, img))["url"]
            except ValueError as e:
                errors.append({"row": r["row"], "error": f"Image '{r['image']}': {e}"})
                continue
        session.add(BenchmarkItem(
            id=new_id("bi"), set_id=s.id, question_prompt=r["question_prompt"], question_type=r["question_type"],
            subject=r["subject"] or s.subject, max_points=r["max_points"], rubric=r["rubric"],
            answer_key=r["answer_key"], answer_text=r["answer_text"], answer_image=url,
            human_marks=r["human_marks"], human_points=r["human_marks"][0], human_comment=r["human_comment"],
            consent=True, consent_by=p.user_id, consent_at=now, created_at=now))
        added += 1
    await session.commit()
    pii = [c for c in ignored if importer._norm(c) in importer.PII_COLUMNS]
    return {"imported": added, "errors": sorted(errors, key=lambda x: x["row"]),
            "ignored_columns": ignored, "dropped_personal_columns": pii}


# ---------------------------------------------------------------- benchmark runs
class RunIn(BaseModel):
    grader: str | None = Field(default=None, max_length=64)
    severe_fraction: float = Field(default=metrics.DEFAULT_SEVERE_FRACTION, ge=0.01, le=1)


@router.post("/quality/benchmarks/{set_id}/runs", status_code=202)
async def start_run(set_id: str, body: RunIn | None = None, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, set_id, p)
    body = body or RunIn()
    g = graders.get_grader(body.grader)
    if g is None:
        raise HTTPException(422, f"Unknown grader '{body.grader}'")
    if g.needs_ai and not ai.enabled():
        raise HTTPException(409, "AI is off: set FAIRPLAY_AI_PROVIDER and FAIRPLAY_AI_KEY to run a benchmark")
    n = len((await session.execute(select(BenchmarkItem.id).where(BenchmarkItem.set_id == s.id))).all())
    if n == 0:
        raise HTTPException(409, "Add items to the benchmark set first")
    running = (await session.execute(select(BenchmarkRun.id).where(
        BenchmarkRun.set_id == s.id, BenchmarkRun.status == "running"))).first()
    if running:
        raise HTTPException(409, "A run of this set is still going")
    prov, model = runner.provider_info()
    r = BenchmarkRun(id=new_id("br"), set_id=s.id, teacher_id=p.user_id, grader=g.name, provider=prov,
                     model=model, prompt_version=g.prompt_version, status="running",
                     severe_fraction=body.severe_fraction, item_count=n, done_count=0, metrics={},
                     created_at=utcnow())
    session.add(r)
    await session.commit()
    runner.spawn(runner.execute_run(r.id))
    return service.run_out(r)


@router.get("/quality/benchmarks/{set_id}/runs")
async def list_runs(set_id: str, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    s = await _own_set(session, set_id, p)
    runs = (await session.execute(select(BenchmarkRun).where(BenchmarkRun.set_id == s.id)
                                  .order_by(BenchmarkRun.created_at.desc(), BenchmarkRun.id))).scalars().all()
    return [service.run_out(r) for r in runs]


@router.get("/quality/runs/{run_id}")
async def get_run(run_id: str, p: Principal = Depends(require_teacher),
                  session: AsyncSession = Depends(get_session)):
    r = await session.get(BenchmarkRun, run_id)
    if r is None or r.teacher_id != p.user_id:
        raise HTTPException(404, "Run not found")
    s = await session.get(BenchmarkSet, r.set_id)
    rows = (await session.execute(select(BenchmarkRunItem).where(BenchmarkRunItem.run_id == r.id)
                                  .order_by(BenchmarkRunItem.id))).scalars().all()
    items = {i.id: i for i in (await session.execute(
        select(BenchmarkItem).where(BenchmarkItem.set_id == r.set_id))).scalars()}
    out = service.run_out(r)
    out["set"] = {"id": s.id, "name": s.name, "subject": s.subject} if s else None
    sev = r.severe_fraction
    res = []
    for row in rows:
        it = items.get(row.item_id)
        diff = None if row.ai_points is None else row.ai_points - row.human_points
        res.append({"item_id": row.item_id, "question_type": row.question_type, "subject": row.subject,
                    "prompt": (it.question_prompt[:300] if it else ""), "max_points": num(row.max_points),
                    "human_points": num(row.human_points),
                    "ai_points": None if row.ai_points is None else num(row.ai_points),
                    "diff": None if diff is None else num(diff),
                    "severe": diff is not None and metrics.is_severe(
                        metrics.Pair(row.ai_points, row.human_points, row.max_points), sev),
                    "confidence": row.confidence, "needs_review": row.needs_review,
                    "explanation": row.explanation, "error": row.error})
    res.sort(key=lambda x: (x["diff"] is None, -abs(x["diff"] or 0) / max(float(x["max_points"]), 1e-9)))
    out["items"] = res
    return out


# ---------------------------------------------------------------- routing policy
class PolicyIn(BaseModel):
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    review_illegible: bool | None = None
    review_ai_flagged: bool | None = None
    boundary_margin: float | None = Field(default=None, ge=0, le=1000)
    grade_boundaries: list[float] | None = Field(default=None, max_length=20)
    check_criteria: bool | None = None
    weak_kappa: float | None = Field(default=None, ge=-1, le=1)
    weak_min_items: int | None = Field(default=None, ge=1, le=100000)
    review_without_ai: bool | None = None

    @field_validator("grade_boundaries")
    @classmethod
    def _bounds(cls, v):
        if v is not None and any(not 0 <= b <= 100 for b in v):
            raise ValueError("Grade boundaries are percentages between 0 and 100")
        return sorted(set(v)) if v is not None else v


async def _save_policy(session: AsyncSession, teacher_id: int, exam_id: str, body: PolicyIn) -> None:
    row = (await session.execute(select(RoutingPolicy).where(
        RoutingPolicy.teacher_id == teacher_id, RoutingPolicy.exam_id == exam_id))).scalar()
    settings = {k: v for k, v in body.model_dump().items() if v is not None}
    if row is None:
        session.add(RoutingPolicy(teacher_id=teacher_id, exam_id=exam_id, settings=settings, updated_at=utcnow()))
    else:
        row.settings = {**(row.settings or {}), **settings}
        row.updated_at = utcnow()
    await session.commit()


async def _policy_out(session: AsyncSession, teacher_id: int, exam_id: str | None) -> dict:
    default, exam = await service.policy_rows(session, teacher_id, exam_id)
    out = {"policy": routing.merge_policy(default, exam), "teacher_settings": default,
           "defaults": routing.DEFAULT_POLICY}
    if exam_id is not None:
        out["exam_settings"] = exam
        out["inherits"] = exam is None
    return out


@router.get("/quality/routing-policy")
async def get_default_policy(p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    return await _policy_out(session, p.user_id, None)


@router.put("/quality/routing-policy")
async def put_default_policy(body: PolicyIn, p: Principal = Depends(require_teacher),
                             session: AsyncSession = Depends(get_session)):
    await _save_policy(session, p.user_id, "", body)
    return await _policy_out(session, p.user_id, None)


@router.get("/exams/{exam_id}/quality/routing-policy")
async def get_exam_policy(exam_id: str, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    return await _policy_out(session, p.user_id, e.id)


@router.put("/exams/{exam_id}/quality/routing-policy")
async def put_exam_policy(exam_id: str, body: PolicyIn, p: Principal = Depends(require_teacher),
                          session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    await _save_policy(session, p.user_id, e.id, body)
    return await _policy_out(session, p.user_id, e.id)


@router.delete("/exams/{exam_id}/quality/routing-policy")
async def reset_exam_policy(exam_id: str, p: Principal = Depends(require_teacher),
                            session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    await session.execute(delete(RoutingPolicy).where(RoutingPolicy.teacher_id == p.user_id,
                                                      RoutingPolicy.exam_id == e.id))
    await session.commit()
    return await _policy_out(session, p.user_id, e.id)


# ---------------------------------------------------------------- routing
@router.get("/exams/{exam_id}/quality/routing")
async def exam_routing(exam_id: str, review_only: bool = False, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    policy = await service.effective_policy(session, p.user_id, e.id)
    weak = await service.weak_areas(session, p.user_id, policy)
    attempts = await service.submitted_attempts(session, [e.id])
    rows = service.routing_rows(e, attempts, policy, weak)
    summary = service.routing_summary(rows)
    if review_only:
        rows = [r for r in rows if r["review"]]
    return {"exam_id": e.id, "policy": policy, "weak_areas": {"types": weak["types"], "subjects": weak["subjects"]},
            "summary": summary, "items": rows}


@router.get("/exams/{exam_id}/quality/routing/{attempt_id}/{question_id}")
async def item_routing(exam_id: str, attempt_id: str, question_id: str, p: Principal = Depends(require_teacher),
                       session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    _question(e, question_id)
    a = await session.get(Attempt, attempt_id)
    if a is None or a.exam_id != e.id:
        raise HTTPException(404, "Attempt not found")
    policy = await service.effective_policy(session, p.user_id, e.id)
    weak = await service.weak_areas(session, p.user_id, policy)
    return {"attempt_id": a.id, "question_id": question_id, **routing.route_item(e, a, question_id, policy, weak)}


# ---------------------------------------------------------------- quality reports
def _filter_exams(exams: list[Exam], course: str | None, exam_ids: str | None) -> list[Exam]:
    course = (course or "").strip()
    ids = {x.strip() for x in (exam_ids or "").split(",") if x.strip()}
    return [e for e in exams if (not course or (e.course or "") == course) and (not ids or e.id in ids)]


@router.get("/quality/overview")
async def overview(course: str | None = Query(default=None, max_length=64),
                   exam_ids: str | None = Query(default=None, max_length=4000),
                   severe_fraction: float = Query(default=metrics.DEFAULT_SEVERE_FRACTION, ge=0.01, le=1),
                   p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    all_exams = await service.teacher_exams(session, p.user_id)
    exams = _filter_exams(all_exams, course, exam_ids)
    attempts = await service.submitted_attempts(session, [e.id for e in exams])
    by_exam: dict[str, list[Attempt]] = {}
    for a in attempts:
        by_exam.setdefault(a.exam_id, []).append(a)
    items_all: list[dict] = []
    exam_rows = []
    for e in exams:
        items = service.graded_items(e, by_exam.get(e.id, []))
        items_all += items
        if not items:
            continue
        pairs = service.item_pairs(items)
        corr = metrics.correction_stats(items)
        tm = metrics.timing_stats(items)
        exam_rows.append({"exam_id": e.id, "title": e.title, "course": e.course or "",
                          "graded": len(items), **{k: v for k, v in metrics.summarize(pairs, severe_fraction).items()
                                                   if k in ("n", "exact", "within_1", "mae", "kappa", "severe_rate",
                                                            "bias")},
                          "correction_rate": corr["correction_rate"],
                          "mean_seconds_ai": tm["ai_assisted"]["mean"], "mean_seconds_manual": tm["manual"]["mean"]})
    block = service.quality_block(items_all, severe_fraction)
    policy = await service.effective_policy(session, p.user_id, None)
    weak = await service.weak_areas(session, p.user_id, policy)
    runs = await service.latest_runs(session, p.user_id)
    sets = {s.id: s for s in (await session.execute(select(BenchmarkSet).where(
        BenchmarkSet.id.in_([r.set_id for r in runs])))).scalars()} if runs else {}
    return {**block, "exams": exam_rows, "courses": sorted({e.course for e in all_exams if e.course}),
            "all_exams": [{"exam_id": e.id, "title": e.title, "course": e.course or ""} for e in all_exams],
            "weak_areas": {"types": weak["types"], "subjects": weak["subjects"]},
            "benchmarks": [{"set_id": r.set_id, "name": sets[r.set_id].name if r.set_id in sets else "",
                            "run_id": r.id, "finished_at": iso(r.finished_at),
                            "overall": (r.metrics or {}).get("overall")} for r in runs]}


@router.get("/exams/{exam_id}/quality")
async def exam_quality(exam_id: str,
                       severe_fraction: float = Query(default=metrics.DEFAULT_SEVERE_FRACTION, ge=0.01, le=1),
                       p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    attempts = await service.submitted_attempts(session, [e.id])
    items = service.graded_items(e, attempts)
    block = service.quality_block(items, severe_fraction)
    policy = await service.effective_policy(session, p.user_id, e.id)
    weak = await service.weak_areas(session, p.user_id, policy)
    routing_summary = service.routing_summary(service.routing_rows(e, attempts, policy, weak))
    return {**block, "exam_id": e.id, "title": e.title, "course": e.course or "", "status": e.status,
            "grades_released": bool(e.grades_released),
            "questions": [_question_out(i, q) for i, q in enumerate(exam_questions(e))],
            "routing": routing_summary, "moderation": await service.moderation_open(session, e.id)}


@router.get("/exams/{exam_id}/quality/graded")
async def graded_answers(exam_id: str, p: Principal = Depends(require_teacher),
                         session: AsyncSession = Depends(get_session)):
    """Graded answers of an exam (for "Add to benchmark" and second-marking pickers)."""
    e = await _own_exam(session, exam_id, p)
    attempts = await service.submitted_attempts(session, [e.id])
    items = service.graded_items(e, attempts)
    return [{k: it[k] for k in ("attempt_id", "anon_id", "question_id", "question_index", "prompt", "type",
                                 "max_points", "suggested", "final", "source")} for it in items]


@router.get("/exams/{exam_id}/quality/release-check")
async def release_check(exam_id: str, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    mod = await service.moderation_open(session, e.id)
    return {"exam_id": e.id, "moderation_open": mod["open"], "by_status": mod["by_status"], "ok": mod["open"] == 0}


# ---------------------------------------------------------------- second marking (lead)
class SecondMarkingIn(BaseModel):
    marker_username: str = Field(min_length=1, max_length=64)
    question_ids: list[str] | None = Field(default=None, max_length=200)
    sample_percent: float | None = Field(default=None, gt=0, le=100)
    routed_only: bool = False
    tolerance: float | None = Field(default=None, ge=0, le=1000)
    seed: int | None = None


@router.get("/quality/second-marking/teachers")
async def other_teachers(p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(User.username, User.name).where(
        User.role == "teacher", User.id != p.user_id).order_by(User.name, User.username))).all()
    return [{"username": u, "name": n} for u, n in rows]


@router.post("/exams/{exam_id}/quality/second-marking", status_code=201)
async def assign_second_marking(exam_id: str, body: SecondMarkingIn, p: Principal = Depends(require_teacher),
                                session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    marker = (await session.execute(select(User).where(User.username == body.marker_username.strip()))).scalar()
    if marker is None or marker.role != "teacher":
        raise HTTPException(404, "No teacher account with that username")
    if marker.id == p.user_id:
        raise HTTPException(422, "The second marker must be another teacher")
    qs = exam_questions(e)
    qids = {q["id"] for q in qs}
    if body.question_ids is not None:
        unknown = [x for x in body.question_ids if x not in qids]
        if unknown:
            raise HTTPException(404, f"Question not found: {unknown[0]}")
    attempts = await service.submitted_attempts(session, [e.id])
    if body.routed_only:
        policy = await service.effective_policy(session, p.user_id, e.id)
        weak = await service.weak_areas(session, p.user_id, policy)
        cands = [(r["attempt_id"], r["question_id"]) for r in service.routing_rows(e, attempts, policy, weak)
                 if r["review"]]
    else:
        cands = []
        for a in attempts:
            for qid in service.review_candidates(e, a):
                cands.append((a.id, qid))
            if body.question_ids is not None:  # explicit questions: every answer of them
                cands += [(a.id, qid) for qid in body.question_ids
                          if (a.id, qid) not in cands and is_answered((a.answers or {}).get(qid))]
    if body.question_ids is not None:
        cands = [c for c in cands if c[1] in set(body.question_ids)]
    cands = list(dict.fromkeys(cands))
    if body.sample_percent is not None and cands:
        k = max(1, math.ceil(len(cands) * body.sample_percent / 100))
        rng = random.Random(body.seed if body.seed is not None else e.id)
        cands = sorted(rng.sample(cands, min(k, len(cands))))
    existing = {(s.attempt_id, s.question_id) for s in (await session.execute(
        select(SecondMark).where(SecondMark.exam_id == e.id))).scalars()}
    qmap = {q["id"]: q for q in qs}
    reason = "routed" if body.routed_only else ("sample" if body.sample_percent is not None else "selected")
    created = []
    for aid, qid in cands:
        if (aid, qid) in existing:
            continue
        tol = body.tolerance if body.tolerance is not None else round(0.1 * float(qmap[qid].get("points", 1)), 2)
        sm = SecondMark(id=new_id("sm"), exam_id=e.id, attempt_id=aid, question_id=qid, lead_id=p.user_id,
                        marker_id=marker.id, tolerance=tol, reason=reason, created_at=utcnow())
        session.add(sm)
        created.append(sm.id)
    await session.commit()
    return {"created": len(created), "skipped": len(cands) - len(created), "ids": created,
            "marker": {"username": marker.username, "name": marker.name}}


def _first_out(g: dict | None) -> dict | None:
    if g is None:
        return None
    return {"points": num(float(g["points"])), "comment": g.get("comment", "") or "",
            "graded_by": g.get("graded_by"), "graded_at": g.get("graded_at")}


@router.get("/exams/{exam_id}/quality/second-marking")
async def list_second_marking(exam_id: str, p: Principal = Depends(require_teacher),
                              session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    sms = (await session.execute(select(SecondMark).where(SecondMark.exam_id == e.id)
                                 .order_by(SecondMark.created_at, SecondMark.id))).scalars().all()
    atts = {a.id: a for a in (await session.execute(select(Attempt).where(
        Attempt.id.in_({s.attempt_id for s in sms})))).scalars()} if sms else {}
    users = {u.id: u for u in (await session.execute(select(User).where(
        User.id.in_({s.marker_id for s in sms} | {a.student_id for a in atts.values()})))).scalars()} if sms else {}
    qindex = {q["id"]: (i, q) for i, q in enumerate(exam_questions(e))}
    items, counts = [], {}
    for s in sms:
        a = atts.get(s.attempt_id)
        st = service.sm_status(s, a)
        counts[st] = counts.get(st, 0) + 1
        i, q = qindex.get(s.question_id, (-1, {}))
        d = (a.ai_drafts or {}).get(s.question_id) if a else None
        g = service.first_grade(a, s.question_id)
        student = users.get(a.student_id) if a else None
        marker = users.get(s.marker_id)
        first = ({"points": num(s.first_points), "comment": s.first_comment, "graded_by": s.first_by,
                  "graded_at": s.first_at} if st == "moderated" and s.first_points is not None else _first_out(g))
        value = (a.answers or {}).get(s.question_id) if a else None
        items.append({
            "id": s.id, "attempt_id": s.attempt_id, "anon_id": service.anon_id(s.attempt_id),
            "username": student.username if student else None, "name": student.name if student else None,
            "question_id": s.question_id, "question_index": i, "prompt": (q.get("prompt") or "")[:300],
            "type": q.get("type"), "max_points": q.get("points", 1),
            "answer": _answer_view(q, value) if q else {"text": None, "image": None},
            "marker": {"username": marker.username, "name": marker.name} if marker else None,
            "tolerance": num(s.tolerance), "reason": s.reason, "status": st,
            "first": first,
            "second": ({"points": num(s.second_points), "comment": s.second_comment, "marked_at": iso(s.second_at)}
                       if s.second_points is not None else None),
            "ai": ({"points": d.get("points"), "confidence": d.get("confidence"), "comment": d.get("comment", "")}
                   if routing.usable_draft(d) else None),
            "moderated": ({"points": num(s.moderated_points), "comment": s.moderated_comment,
                           "by": s.moderated_by, "at": iso(s.moderated_at)} if s.moderated_at else None),
            "created_at": iso(s.created_at)})
    open_n = sum(v for k, v in counts.items() if k not in ("agreed", "moderated"))
    return {"exam_id": e.id, "title": e.title, "counts": counts, "open": open_n, "items": items}


@router.delete("/exams/{exam_id}/quality/second-marking/{sm_id}", status_code=204)
async def remove_second_marking(exam_id: str, sm_id: str, p: Principal = Depends(require_teacher),
                                session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    s = await session.get(SecondMark, sm_id)
    if s is None or s.exam_id != e.id:
        raise HTTPException(404, "Second marking not found")
    if s.moderated_at is not None:
        raise HTTPException(409, "This answer is already moderated")
    await session.delete(s)
    await session.commit()
    return Response(status_code=204)


class ModerateIn(BaseModel):
    points: float = Field(allow_inf_nan=False)
    comment: str | None = Field(default=None, max_length=5000)


@router.post("/exams/{exam_id}/quality/second-marking/{sm_id}/moderate")
async def moderate(exam_id: str, sm_id: str, body: ModerateIn, p: Principal = Depends(require_teacher),
                   session: AsyncSession = Depends(get_session)):
    """Lead teacher sets the agreed mark; it becomes the final grade (history kept)."""
    e = await _own_exam(session, exam_id, p)
    s = (await session.execute(select(SecondMark).where(SecondMark.id == sm_id).with_for_update()
                                .execution_options(populate_existing=True))).scalar()
    if s is None or s.exam_id != e.id:
        raise HTTPException(404, "Second marking not found")
    a = (await session.execute(select(Attempt).where(Attempt.id == s.attempt_id).with_for_update()
                                .execution_options(populate_existing=True))).scalar()
    st = service.sm_status(s, a)
    if st != "needs_moderation":
        raise HTTPException(409, {"moderated": "Already moderated", "awaiting_second": "The second mark is not in yet",
                                  "awaiting_first": "Grade this answer first",
                                  "agreed": "The two marks agree: nothing to moderate"}.get(st, st))
    _, q = _question(e, s.question_id)
    max_points = float(q.get("points", 1))
    if not -1e-9 <= body.points <= max_points + 1e-9:
        raise HTTPException(422, f"Points must be between 0 and {num(max_points)}")
    lead = await session.get(User, p.user_id)
    old = service.first_grade(a, s.question_id) or {}
    now = utcnow()
    draft = (a.ai_drafts or {}).get(s.question_id)
    suggested = metrics.suggested_points(old, draft)
    s.first_points, s.first_comment = float(old["points"]), str(old.get("comment") or "")
    gb = old.get("graded_by")
    s.first_by = gb if isinstance(gb, int) else None
    s.first_at = old.get("graded_at")
    s.ai_points = suggested
    s.moderated_points, s.moderated_by, s.moderated_at = body.points, p.user_id, now
    s.moderated_comment = body.comment if body.comment is not None else s.first_comment
    grade = dict(old)
    changed = abs(float(old["points"]) - body.points) > 1e-9
    grade.update({
        "points": num(body.points), "comment": s.moderated_comment, "graded_at": iso(now),
        "graded_by": lead.username if lead else p.user_id, "moderated": True,
        "moderation": {"second_mark_id": s.id, "first_points": num(s.first_points), "first_by": gb,
                       "second_points": num(s.second_points), "second_by": s.marker_id,
                       "moderated_points": num(body.points), "moderated_by": p.user_id, "at": iso(now)}})
    if changed:
        grade["rubric_scores"] = None
    if suggested is not None:
        grade["ai_suggested_points"] = grade.get("ai_suggested_points", suggested)
        grade["source"] = "ai_accepted" if abs(body.points - suggested) < 1e-9 else "ai_edited"
    else:
        grade["source"] = "manual"
    grades = dict(a.grades or {})
    grades[s.question_id] = grade
    score, pending, _ = grade_attempt(exam_questions(e), a.answers, grades)
    a.grades, a.score, a.needs_grading, a.updated_at = grades, score, pending, now
    await session.commit()
    return {"id": s.id, "status": "moderated", "grade": grade, "score": num(score),
            "percent": percent(score, e.total_points)}


# ---------------------------------------------------------------- second marking (marker, blind)
def _marker_item(s: SecondMark, e: Exam | None, a: Attempt | None) -> dict:
    i, q = (-1, {})
    if e is not None:
        try:
            i, q = _question(e, s.question_id)
        except HTTPException:
            pass
    value = (a.answers or {}).get(s.question_id) if a else None
    return {"id": s.id, "exam_id": s.exam_id, "exam_title": e.title if e else "", "course": (e.course or "") if e else "",
            "anon_id": service.anon_id(s.attempt_id), "question": _question_out(i, q) if q else None,
            "answer": _answer_view(q, value) if q else {"text": None, "image": None},
            "status": service.marker_status(s),
            "my_mark": ({"points": num(s.second_points), "comment": s.second_comment, "marked_at": iso(s.second_at)}
                        if s.second_points is not None else None)}


@router.get("/quality/second-marking/queue")
async def marker_queue(p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    """Answers assigned to me for blind second marking (no first mark, AI or student name)."""
    sms = (await session.execute(select(SecondMark).where(SecondMark.marker_id == p.user_id)
                                 .order_by(SecondMark.created_at, SecondMark.id))).scalars().all()
    exams = {x.id: x for x in (await session.execute(select(Exam).where(
        Exam.id.in_({s.exam_id for s in sms})))).scalars()} if sms else {}
    atts = {x.id: x for x in (await session.execute(select(Attempt).where(
        Attempt.id.in_({s.attempt_id for s in sms})))).scalars()} if sms else {}
    items = [_marker_item(s, exams.get(s.exam_id), atts.get(s.attempt_id)) for s in sms]
    return {"items": items, "to_mark": sum(1 for x in items if x["status"] == "to_mark")}


class MarkIn(BaseModel):
    points: float = Field(allow_inf_nan=False)
    comment: str = Field(default="", max_length=5000)


@router.put("/quality/second-marking/{sm_id}/mark")
async def second_mark(sm_id: str, body: MarkIn, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    s = (await session.execute(select(SecondMark).where(SecondMark.id == sm_id).with_for_update()
                                .execution_options(populate_existing=True))).scalar()
    if s is None or s.marker_id != p.user_id:
        raise HTTPException(404, "Not found")
    if s.moderated_at is not None:
        raise HTTPException(409, "This answer has been moderated")
    e = await session.get(Exam, s.exam_id)
    if e is None:
        raise HTTPException(404, "Not found")
    _, q = _question(e, s.question_id)
    max_points = float(q.get("points", 1))
    if not -1e-9 <= body.points <= max_points + 1e-9:
        raise HTTPException(422, f"Points must be between 0 and {num(max_points)}")
    s.second_points, s.second_comment, s.second_at = body.points, body.comment, utcnow()
    await session.commit()
    a = await session.get(Attempt, s.attempt_id)
    return _marker_item(s, e, a)
