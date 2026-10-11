"""Any-paper answer finding in the scan pipeline (pages NOT printed by FairPlay, no QR codes).

After an "unprinted" upload is read (pipeline.analyze_unprinted keeps each flattened page),
`process_batch` runs when AI is on:

  pages -> grouping.read_headers -> grouping.group_pages (roster fuzzy match) -> one AnswerSet
  per student -> segment.segment_pages -> regions stored on the set -> crops (exact answer
  regions, multi-page answers stitched) become the paper answers -> AI grading drafts.

The teacher checks the result in "Check answer regions" (`/scans/sets` API below): move /
resize / reassign boxes, mark a question not answered, move pages between students, pick the
student from the roster, confirm. Only the questions whose regions changed are re-cropped and
re-drafted. The manual "Map pages" step and QR booklets stay as they were (fallbacks).

AnswerSet (table scan_answer_sets) API shape — see CONTRACT.md "Any-paper answer finding".
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import cv2
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import JSON, BigInteger, DateTime, Float, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .. import ai
from ..db import Base, get_database, get_session
from ..grading import exam_questions, grade_attempt
from ..models import Attempt, Exam, User
from ..security import Principal, require_teacher
from ..services import attempt_key, new_id
from ..store import get_store
from ..timeutil import iso, utcnow
from . import grouping, segment
from .answer_key import _own_exam, get_config
from .models import ScanBatch, ScanPage
from .storage import read_url

log = logging.getLogger("fairplay.scan.any_paper")
router = APIRouter(prefix="/api", tags=["scan-ai"])

HEADER_LONG = 1000      # long side of the page image sent for the header read


class AnswerSet(Base):
    """One student's pages from an upload without QR codes, with the found answer regions."""
    __tablename__ = "scan_answer_sets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    exam_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    batch_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    student_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    page_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # {"name", "student_id_text", "confidence", "candidates": [...], "reasons": [...]}
    match: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # [{"id","question_id","page_id","box","confidence","crossed_out","label","source","needs_check"}]
    regions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # {"prompt_version","source","reasons","confidence","name","candidates": {page_id: [box]}}
    segment: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # needs_check | ok | confirmed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="needs_check")
    group_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# ---------------------------------------------------------------- helpers
def _concurrency() -> int:
    from .ai_grade import _concurrency as c  # same knob as grading (FAIRPLAY_AI_CONCURRENCY)
    return c()


def _small_jpeg(data: bytes | None) -> bytes:
    if not data:
        return b""
    img = segment.decode(data)
    if img is None:
        return b""
    h, w = img.shape[:2]
    f = HEADER_LONG / max(h, w) if max(h, w) > HEADER_LONG else 1.0
    if f < 1:
        img = cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    return segment.jpeg(img, 80)


def regions_to_set(seg: dict, page_ids: list[int]) -> list[dict]:
    """segment_pages regions (page = index into the set) -> stored regions (page_id)."""
    out = []
    for r in seg.get("regions") or []:
        idx = int(r.get("page", -1))
        if not 0 <= idx < len(page_ids):
            continue
        out.append({"id": r.get("id") or new_id("r"), "question_id": r.get("question_id"), "page_id": page_ids[idx],
                    "box": r["box"], "confidence": float(r.get("confidence") or 0), "crossed_out": bool(r.get("crossed_out")),
                    "label": r.get("label") or "", "source": r.get("source") or "ai",
                    "needs_check": bool(r.get("needs_check"))})
    return out


def segment_meta(seg: dict, page_ids: list[int]) -> dict:
    cands = {}
    for p in seg.get("pages") or []:
        if 0 <= p["index"] < len(page_ids):
            cands[str(page_ids[p["index"]])] = [c["box"] for c in p["candidates"]]
    return {"prompt_version": seg.get("prompt_version"), "source": seg.get("source"),
            "reasons": list(seg.get("reasons") or []), "confidence": seg.get("confidence", 0.0),
            "name": seg.get("name"), "candidates": cands, "model": ai.model_name(), "provider": ai.provider(),
            "created_at": iso(utcnow())}


def missing_of(qs: list[dict], regions: list[dict], page_ids: list[int]) -> list[str]:
    found = {r.get("question_id") for r in regions if not r.get("crossed_out") and r.get("page_id") in page_ids}
    return [q["id"] for q in qs if q["id"] not in found]


def set_reasons(s: AnswerSet, qs: list[dict]) -> list[str]:
    rs: list[str] = []
    if s.student_id is None:
        rs.append("no_student")
    m = s.match or {}
    if s.student_id is not None and (m.get("needs_check") or s.group_confidence < grouping.MATCH_SURE) \
            and not m.get("teacher"):
        rs.append("check_student")
    live = [r for r in s.regions or [] if not r.get("crossed_out") and r.get("page_id") in (s.page_ids or [])]
    if any(r.get("needs_check") and r.get("source") != "teacher" for r in live):
        rs.append("low_confidence")
    if missing_of(qs, s.regions or [], s.page_ids or []):
        rs.append("missing")
    if (s.segment or {}).get("source") == "fallback":
        rs.append("not_found_by_ai")
    return rs


def refresh_status(s: AnswerSet, qs: list[dict]) -> None:
    if s.status == "confirmed":
        return
    s.status = "needs_check" if set_reasons(s, qs) else "ok"


def set_out(s: AnswerSet, qs: list[dict], pages: dict[int, ScanPage], users: dict[int, User]) -> dict:
    u = users.get(s.student_id) if s.student_id is not None else None
    m = s.match or {}
    return {
        "set_id": s.id, "batch_id": s.batch_id, "student_id": s.student_id,
        "student": {"id": u.id, "username": u.username, "name": u.name} if u else None,
        "name_read": m.get("name") or "", "id_read": m.get("student_id_text") or "",
        "match": {"confidence": s.group_confidence, "candidates": m.get("candidates") or [],
                  "reasons": m.get("reasons") or [], "teacher": bool(m.get("teacher"))},
        "status": s.status, "needs_check": s.status == "needs_check", "reasons": set_reasons(s, qs),
        "pages": [{"page_id": pid, "seq": pages[pid].seq, "preview": pages[pid].preview,
                   "image": pages[pid].source_image or pages[pid].preview,
                   "candidates": ((s.segment or {}).get("candidates") or {}).get(str(pid), [])}
                  for pid in s.page_ids or [] if pid in pages],
        "regions": [r for r in s.regions or [] if r.get("page_id") in (s.page_ids or [])],
        "missing": missing_of(qs, s.regions or [], s.page_ids or []),
        "segment": {k: (s.segment or {}).get(k) for k in ("prompt_version", "source", "reasons", "confidence", "name")},
        "updated_at": iso(s.updated_at),
    }


# ---------------------------------------------------------------- applying a set to answers
async def _drop_answers(session: AsyncSession, e: Exam, student_id: int, set_id: str,
                        qids: set[str] | None = None) -> None:
    """Take back answers this set gave a student (never ones the teacher graded)."""
    a = (await session.execute(select(Attempt).where(Attempt.exam_id == e.id, Attempt.student_id == student_id)
                               .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if a is None:
        return
    answers, drafts, grades = dict(a.answers or {}), dict(a.ai_drafts or {}), a.grades or {}
    changed = False
    for qid, v in list(answers.items()):
        if isinstance(v, dict) and v.get("set_id") == set_id and grades.get(qid) is None and \
                (qids is None or qid in qids):
            answers.pop(qid)
            drafts.pop(qid, None)
            changed = True
    if not changed:
        return
    if not answers and not grades and (a.source or "") == "paper":
        await session.delete(a)
    else:
        a.answers, a.ai_drafts = answers, drafts
        score, pending, _ = grade_attempt(exam_questions(e), answers, a.grades)
        a.score, a.needs_grading, a.updated_at = score, pending, utcnow()
    await session.commit()
    await get_store().delete(attempt_key(a.id))


async def apply_set(session: AsyncSession, e: Exam, s: AnswerSet, pages: dict[int, ScanPage],
                    qids: set[str] | None = None) -> list[tuple[str, str]]:
    """Crop the set's regions into paper answers of its student (all questions, or only
    `qids`), update the page rows and merge into the attempt. Returns pairs to draft."""
    from ..routes_scan import merge_pages  # late import: routes_scan imports app.scan
    qs = [q for q in exam_questions(e) if qids is None or q["id"] in qids]
    pids = [pid for pid in s.page_ids or [] if pid in pages]
    pgs = [pages[pid] for pid in pids]
    if s.student_id is None or not pgs:
        for pg in pgs:
            pg.status, pg.student_id, pg.page_no, pg.crops = "unmatched", None, None, {}
            pg.reason = "Answers found — choose the student"
        await session.commit()
        return []
    urls = [pg.source_image or pg.preview for pg in pgs]
    idx = {pid: i for i, pid in enumerate(pids)}
    regions = [dict(r, page=idx[r["page_id"]]) for r in s.regions or [] if r.get("page_id") in idx]
    answers = await asyncio.to_thread(segment.region_answers, qs, regions, urls, pids, {"set_id": s.id})
    for v in answers.values():
        if v.get("not_found"):
            v["whole_page"] = True  # nothing cropped: the grader sees the first page
        for r in v.get("regions") or []:
            r["page_id"] = r.pop("page_key")
    per_page: dict[int, dict] = {pid: {} for pid in pids}
    for qid, v in answers.items():
        per_page[pids[int(v["page"]) - 1]][qid] = v
    teacher = (s.match or {}).get("teacher") or s.status == "confirmed"
    for i, pg in enumerate(pgs):
        keep = {k: v for k, v in (pg.crops or {}).items()
                if qids is not None and k not in qids and not k.startswith("_") and pg.student_id == s.student_id}
        keep.update(per_page[pg.id])
        pg.crops = keep
        pg.status, pg.student_id, pg.page_no = "assigned", s.student_id, i + 1
        pg.reason = "Checked by the teacher" if teacher else "Answers found by AI"
    await session.commit()
    fake = SimpleNamespace(student_id=s.student_id, crops=answers, created_at=utcnow(), id=0,
                           status="assigned", reason="")
    return await merge_pages(session, e, [fake])


# ---------------------------------------------------------------- pipeline
async def _roster(session: AsyncSession, e: Exam) -> list[User]:
    from ..routes_scan import roster_of  # late import
    _, users = await roster_of(session, e)
    return users


async def segment_set(qs: list[dict], urls: list[str]) -> dict:
    imgs = await asyncio.to_thread(lambda: [read_url(u) or b"" for u in urls])
    return await segment.segment_pages(qs, imgs)


async def process_batch(exam_id: str, batch_id: str) -> list[tuple[str, str]]:
    """Group the batch's pages into students, find the answers, create the paper answers.
    Returns (attempt_id, question_id) pairs for AI drafts. No-op when AI is off (the
    teacher maps pages by hand as before)."""
    if not ai.enabled():
        return []
    db = get_database()
    async with db.sessionmaker() as s:
        e = await s.get(Exam, exam_id)
        if e is None:
            return []
        pages = list((await s.execute(select(ScanPage).where(
            ScanPage.batch_id == batch_id, ScanPage.status == "unmatched").order_by(ScanPage.seq))).scalars())
        pages = [pg for pg in pages if pg.source_image or pg.preview]
        users = await _roster(s, e)
        cfg = await get_config(s, e.id)
        pps = int((cfg.mapping or {}).get("pages_per_student") or 0) if cfg else 0
    if not pages:
        return []
    qs = exam_questions(e)
    roster = [{"id": u.id, "name": u.name, "username": u.username} for u in users]
    urls = [pg.source_image or pg.preview for pg in pages]
    small = await asyncio.to_thread(lambda: [_small_jpeg(read_url(u)) for u in urls])
    if len(pages) == 1:
        headers = [await grouping.read_header(small[0])]
    else:
        headers = await grouping.read_headers(small, _concurrency())
    groups = grouping.group_pages(headers, roster, pps or None)
    pairs: list[tuple[str, str]] = []
    for g in groups:
        pids = [pages[i].id for i in g["pages"]]
        seg = await segment_set(qs, [urls[i] for i in g["pages"]])
        name = seg.get("name") or {}
        match = {"name": g["name"] or name.get("text") or "", "student_id_text": g["student_id_text"] or name.get("student_id") or "",
                 "candidates": g["candidates"], "reasons": g["reasons"], "needs_check": g["needs_check"]}
        sid = g["student_id"]
        conf = g["confidence"]
        if sid is None and (name.get("text") or name.get("student_id")) and not g["name"]:
            # the header read found nothing, the segmentation read a name: try that
            m = grouping.match_roster(name.get("text") or "", name.get("student_id") or "", roster)
            sid, conf = m["student_id"], m["confidence"]
            match.update(candidates=m["candidates"], reasons=m["reasons"], needs_check=True)
        async with db.sessionmaker() as s:
            e = await s.get(Exam, exam_id)
            row = AnswerSet(id=new_id("as"), exam_id=exam_id, batch_id=batch_id, student_id=sid, page_ids=pids,
                            match=match, regions=regions_to_set(seg, pids), segment=segment_meta(seg, pids),
                            group_confidence=round(float(conf), 3), created_at=utcnow(), updated_at=utcnow())
            refresh_status(row, qs)
            s.add(row)
            await s.commit()
            page_rows = {pg.id: pg for pg in (await s.execute(
                select(ScanPage).where(ScanPage.id.in_(pids)))).scalars()}
            pairs += await apply_set(s, e, row, page_rows)
    return pairs


# ---------------------------------------------------------------- teacher API
async def _sets(session: AsyncSession, exam_id: str) -> list[AnswerSet]:
    return list((await session.execute(select(AnswerSet).where(AnswerSet.exam_id == exam_id)
                                       .order_by(AnswerSet.created_at, AnswerSet.id))).scalars())


async def _unprinted_pages(session: AsyncSession, exam_id: str) -> dict[int, ScanPage]:
    rows = (await session.execute(
        select(ScanPage).join(ScanBatch, ScanBatch.id == ScanPage.batch_id)
        .where(ScanPage.exam_id == exam_id, ScanBatch.mode == "unprinted", ScanPage.status != "ignored")
        .order_by(ScanBatch.created_at, ScanPage.batch_id, ScanPage.seq))).scalars()
    return {pg.id: pg for pg in rows}


async def sets_out(session: AsyncSession, e: Exam) -> dict:
    qs = exam_questions(e)
    sets = await _sets(session, e.id)
    pages = await _unprinted_pages(session, e.id)
    roster = await _roster(session, e)
    ids = {s.student_id for s in sets if s.student_id is not None}
    users = {u.id: u for u in roster}
    extra = ids - set(users)
    if extra:
        users.update({u.id: u for u in (await session.execute(select(User).where(User.id.in_(extra)))).scalars()})
    in_sets = {pid for s in sets for pid in s.page_ids or []}
    out_sets = [set_out(s, qs, pages, users) for s in sets]
    return {
        "exam_id": e.id, "ai_enabled": ai.enabled(), "low_confidence_below": segment.LOW_CONFIDENCE,
        "questions": [{"question_id": q["id"], "index": i, "prompt": q.get("prompt", ""), "type": q.get("type"),
                       "points": q.get("points", 1)} for i, q in enumerate(qs)],
        "students": [{"id": u.id, "username": u.username, "name": u.name} for u in roster],
        "sets": out_sets,
        "needs_check": sum(1 for s in out_sets if s["needs_check"]),
        "loose_pages": [{"page_id": pg.id, "seq": pg.seq, "preview": pg.preview, "batch_id": pg.batch_id}
                        for pid, pg in pages.items() if pid not in in_sets and pg.status == "unmatched"],
    }


@router.get("/exams/{exam_id}/scans/sets")
async def list_sets(exam_id: str, p: Principal = Depends(require_teacher), session: AsyncSession = Depends(get_session)):
    e = await _own_exam(session, exam_id, p)
    return await sets_out(session, e)


class RegionIn(BaseModel):
    id: str = Field(default="", max_length=40)
    question_id: str | None = Field(default=None, max_length=64)
    page_id: int
    box: list[float] = Field(min_length=4, max_length=4)
    crossed_out: bool = False
    label: str = Field(default="", max_length=20)


class SetIn(BaseModel):
    student_id: int | None = None        # send null to take the student away
    page_ids: list[int] | None = Field(default=None, max_length=200)
    regions: list[RegionIn] | None = Field(default=None, max_length=500)
    confirm: bool = False


def _region_key(r: dict) -> tuple:
    return (r.get("question_id"), r.get("page_id"), tuple(round(float(v), 4) for v in r.get("box") or []),
            bool(r.get("crossed_out")))


def changed_questions(old: list[dict], new: list[dict]) -> set[str]:
    """Questions whose live regions differ between two region lists."""
    def by_q(rs: list[dict]) -> dict[str, set]:
        out: dict[str, set] = {}
        for r in rs:
            if r.get("question_id") and not r.get("crossed_out"):
                out.setdefault(r["question_id"], set()).add(_region_key(r))
        return out
    a, b = by_q(old), by_q(new)
    return {q for q in set(a) | set(b) if a.get(q) != b.get(q)}


async def _spawn_drafts(exam_id: str, pairs: list[tuple[str, str]]) -> None:
    from ..routes_scan import _spawn, draft_answers  # late import
    if pairs:
        _spawn(draft_answers(exam_id, pairs))


async def _move_pages(session: AsyncSession, e: Exam, target: AnswerSet, page_ids: list[int],
                      sets: list[AnswerSet], pages: dict[int, ScanPage]) -> list[tuple[AnswerSet, set[str] | None]]:
    """Give `page_ids` to `target` (taking them from other sets). Returns the other sets
    that lost pages, with the questions to redo."""
    qs = exam_questions(e)
    touched = []
    for other in sets:
        if other.id == target.id:
            continue
        lost = [pid for pid in other.page_ids or [] if pid in page_ids]
        if not lost:
            continue
        old_regions = list(other.regions or [])
        other.page_ids = [pid for pid in other.page_ids if pid not in lost]
        other.regions = [r for r in old_regions if r.get("page_id") not in lost]
        other.updated_at = utcnow()
        if other.status == "confirmed":
            other.status = "needs_check"
        refresh_status(other, qs)
        touched.append((other, changed_questions(old_regions, other.regions)))
    return touched


async def _apply_changes(session: AsyncSession, e: Exam, s: AnswerSet, old_student: int | None,
                         qids: set[str] | None, pages: dict[int, ScanPage]) -> list[tuple[str, str]]:
    if old_student != s.student_id:
        if old_student is not None:
            await _drop_answers(session, e, old_student, s.id)
        qids = None  # the new student gets every question
    if qids is not None and not qids:
        return []
    return await apply_set(session, e, s, pages, qids)


@router.put("/exams/{exam_id}/scans/sets/{set_id}")
async def update_set(exam_id: str, set_id: str, body: SetIn, p: Principal = Depends(require_teacher),
                     session: AsyncSession = Depends(get_session)):
    """Teacher fixes: student, pages (moved from other students), regions, confirm.
    Only questions whose regions changed are re-cropped and re-drafted."""
    e = await _own_exam(session, exam_id, p)
    qs = exam_questions(e)
    qids_known = {q["id"] for q in qs}
    sets = await _sets(session, e.id)
    s = next((x for x in sets if x.id == set_id), None)
    if s is None:
        raise HTTPException(404, "Answer set not found")
    pages = await _unprinted_pages(session, e.id)
    old_student = s.student_id
    old_regions = list(s.regions or [])
    redo: set[str] | None = set()
    others: list[tuple[AnswerSet, set[str] | None]] = []
    fields = body.model_fields_set

    if "student_id" in fields:
        if body.student_id is not None:
            u = await session.get(User, body.student_id)
            if u is None or u.role != "student":
                raise HTTPException(422, "Pick a student")
            clash = next((x for x in sets if x.id != s.id and x.student_id == body.student_id), None)
            if clash is not None:
                raise HTTPException(409, f"{u.name} already has pages here — move these pages to that student instead")
        s.student_id = body.student_id
        s.match = dict(s.match or {}, teacher=True)
        if body.student_id is not None:
            s.group_confidence = 1.0
    if body.page_ids is not None:
        if not body.page_ids:
            raise HTTPException(422, "A student needs at least one page")
        for pid in body.page_ids:
            if pid not in pages:
                raise HTTPException(404, f"Page {pid} is not a page of this exam")
        if len(set(body.page_ids)) != len(body.page_ids):
            raise HTTPException(422, "A page is listed twice")
        others = await _move_pages(session, e, s, body.page_ids, sets, pages)
        if body.page_ids != list(s.page_ids or []):
            s.page_ids = list(body.page_ids)
            redo = None  # page numbers change: rebuild every answer of this set
    if body.regions is not None:
        page_set = set(s.page_ids or [])
        new: list[dict] = []
        old_by_id = {r.get("id"): r for r in old_regions}
        for r in body.regions:
            if r.question_id is not None and r.question_id not in qids_known:
                raise HTTPException(422, f"Unknown question {r.question_id}")
            if r.page_id not in page_set:
                raise HTTPException(422, "A box is on a page that is not in this set")
            box = segment.clamp_box(r.box)
            if box is None:
                raise HTTPException(422, "A box is too small")
            prev = old_by_id.get(r.id)
            same = prev is not None and _region_key(prev) == _region_key(
                {"question_id": r.question_id, "page_id": r.page_id, "box": box, "crossed_out": r.crossed_out})
            new.append({"id": r.id or new_id("r"), "question_id": r.question_id, "page_id": r.page_id, "box": box,
                        "crossed_out": r.crossed_out, "label": r.label or (prev or {}).get("label", ""),
                        "confidence": float(prev["confidence"]) if same else 1.0,
                        "source": prev.get("source", "ai") if same else "teacher",
                        "needs_check": bool(prev.get("needs_check")) if same else False})
        s.regions = new
        if redo is not None:
            redo |= changed_questions(old_regions, new)
    if body.confirm:
        s.status = "confirmed"
        s.regions = [dict(r, needs_check=False) for r in s.regions or []]
        s.match = dict(s.match or {}, teacher=True)
    elif s.status == "confirmed" and (redo is None or redo or "student_id" in fields):
        s.status = "needs_check"
    refresh_status(s, qs)
    s.updated_at = utcnow()
    await session.commit()

    pairs: list[tuple[str, str]] = []
    for other, q in others:
        pairs += await _apply_changes(session, e, other, other.student_id, q, pages)
        if not other.page_ids:
            await session.delete(other)
            await session.commit()
    pairs += await _apply_changes(session, e, s, old_student, redo, pages)
    await _spawn_drafts(e.id, pairs)
    out = await sets_out(session, e)
    out["queued"] = len(pairs)
    out["set"] = next((x for x in out["sets"] if x["set_id"] == s.id), None)
    return out


class NewSetIn(BaseModel):
    page_ids: list[int] = Field(min_length=1, max_length=200)
    student_id: int | None = None


@router.post("/exams/{exam_id}/scans/sets", status_code=201)
async def new_set(exam_id: str, body: NewSetIn, p: Principal = Depends(require_teacher),
                  session: AsyncSession = Depends(get_session)):
    """Split pages off into a new student (or collect loose pages); the AI finds the answers."""
    e = await _own_exam(session, exam_id, p)
    qs = exam_questions(e)
    pages = await _unprinted_pages(session, e.id)
    for pid in body.page_ids:
        if pid not in pages:
            raise HTTPException(404, f"Page {pid} is not a page of this exam")
    sets = await _sets(session, e.id)
    if body.student_id is not None:
        u = await session.get(User, body.student_id)
        if u is None or u.role != "student":
            raise HTTPException(422, "Pick a student")
        if any(x.student_id == body.student_id for x in sets):
            raise HTTPException(409, f"{u.name} already has pages here — move these pages to that student instead")
    s = AnswerSet(id=new_id("as"), exam_id=e.id, batch_id=pages[body.page_ids[0]].batch_id,
                  student_id=body.student_id, page_ids=list(body.page_ids),
                  match={"teacher": body.student_id is not None}, regions=[], segment={},
                  group_confidence=1.0 if body.student_id is not None else 0.0,
                  created_at=utcnow(), updated_at=utcnow())
    session.add(s)
    others = await _move_pages(session, e, s, body.page_ids, sets, pages)
    await session.commit()
    pairs: list[tuple[str, str]] = []
    for other, q in others:
        pairs += await _apply_changes(session, e, other, other.student_id, q, pages)
        if not other.page_ids:
            await session.delete(other)
            await session.commit()
    seg = await segment_set(qs, [pages[pid].source_image or pages[pid].preview for pid in s.page_ids])
    s.regions, s.segment = regions_to_set(seg, s.page_ids), segment_meta(seg, s.page_ids)
    refresh_status(s, qs)
    await session.commit()
    pairs += await apply_set(session, e, s, pages)
    await _spawn_drafts(e.id, pairs)
    out = await sets_out(session, e)
    out["queued"] = len(pairs)
    out["set"] = next((x for x in out["sets"] if x["set_id"] == s.id), None)
    return out


@router.post("/exams/{exam_id}/scans/sets/{set_id}/resegment")
async def resegment(exam_id: str, set_id: str, p: Principal = Depends(require_teacher),
                    session: AsyncSession = Depends(get_session)):
    """Ask the AI again to find the answers on this student's pages (teacher boxes are replaced)."""
    e = await _own_exam(session, exam_id, p)
    if not ai.enabled():
        raise HTTPException(409, "AI is off on this server — draw the boxes by hand")
    qs = exam_questions(e)
    s = await session.get(AnswerSet, set_id)
    if s is None or s.exam_id != e.id:
        raise HTTPException(404, "Answer set not found")
    pages = await _unprinted_pages(session, e.id)
    pids = [pid for pid in s.page_ids or [] if pid in pages]
    seg = await segment_set(qs, [pages[pid].source_image or pages[pid].preview for pid in pids])
    old = list(s.regions or [])
    s.regions, s.segment = regions_to_set(seg, pids), segment_meta(seg, pids)
    if s.status == "confirmed":
        s.status = "needs_check"
    refresh_status(s, qs)
    s.updated_at = utcnow()
    await session.commit()
    redo = changed_questions(old, s.regions)
    pairs = await _apply_changes(session, e, s, s.student_id, redo, pages)
    await _spawn_drafts(e.id, pairs)
    out = await sets_out(session, e)
    out["queued"] = len(pairs)
    out["set"] = next((x for x in out["sets"] if x["set_id"] == s.id), None)
    return out
