"""Graph / chart / diagram questions in AI paper grading (see diagram_spec.py for the spec).

Hooks used by the step grader (app.scan.ai_grade):
  build_prompt  -> `prompt_extra(q, ctx)`: the checklist prompt (empty for other questions)
  parse         -> `parse_extra(out, d, q, ctx, reasons)`: checklist results into the draft
and by app.scan.grading.draft_for -> `prepare(ctx, images)`: adds the teacher's reference drawing
as the last image and runs the OpenCV checks on the student's drawing.

Draft additions (attempt.ai_drafts[qid]) for these questions — `criteria` mirrors the features
so the review screen's criterion toggles and approve work unchanged:
  "diagram": {
     "kind", "kind_label",
     "features": [{"id", "key", "label", "points", "max_points", "met",
                   "status": "present"|"wrong"|"missing"|"not_assessed", "note",
                   "box": [x0, y0, x1, y1] | null, "point": [x, y] | null}],   # normalised 0..1
     "readings": [{"label", "x", "y", "at": [x, y] | null}],                 # values read off the axes
     "check": {cross-check summary, see diagram_cv.cross_check},
     "reference_image": url | null, "prompt_version": "fp-diagram-v1"
  }
Grade additions (attempt.grades[qid]) after approval: "diagram": {"kind", "kind_label",
  "features": [{"id","label","points","max_points","status","note","box","point"}], "reference_image"}
with status recomputed from the approved points (full -> present, 0 -> wrong/missing, else partial).
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..grading import exam_questions, num
from ..security import Principal, require_teacher
from ..timeutil import iso, utcnow
from .diagram_spec import KINDS, catalog, clean_diagram, effective, kind_label

router = APIRouter(prefix="/api", tags=["scan-ai"])

DIAGRAM_PROMPT_VERSION = "fp-diagram-v1"
DISAGREE_CONFIDENCE = 0.6

REVIEW_TEXT = {
    "missing_features": "AI skipped some checklist features",
    "features_inconsistent": "AI's feature marks and points disagree",
    "diagram_check_disagrees": "The picture check disagrees with the AI",
}

_STATUS = {
    "present": "present", "correct": "present", "ok": "present", "yes": "present", "true": "present",
    "met": "present", "right": "present", "partial": "wrong", "partly": "wrong",
    "wrong": "wrong", "incorrect": "wrong", "error": "wrong", "false": "wrong", "inaccurate": "wrong",
    "missing": "missing", "absent": "missing", "no": "missing", "omitted": "missing", "not drawn": "missing",
}


# ---------------------------------------------------------------- prompt
def _fmt_xy(xy: list) -> str:
    return ", ".join(f"({num(float(p[0]))}, {num(float(p[1]))})" for p in xy)


def prompt_extra(q: dict, ctx: dict) -> str:
    spec = ctx.get("diagram")
    if not spec:
        return ""
    lines = [
        f"GRAPH / DIAGRAM QUESTION (prompt {DIAGRAM_PROMPT_VERSION}). Kind: {kind_label(spec['kind'])}.",
        "The student's answer is a hand-drawn graph, chart or diagram. Grade it with this feature "
        "checklist instead of the rubric above:",
    ]
    for f in spec["features"]:
        s = f"- id {f['id']}: {f['label']} ({num(float(f['points']))} pts)"
        if f.get("expected"):
            s += f" — expected: {f['expected']}"
        if f.get("points_xy"):
            s += f" — must pass through / plot (x, y): {_fmt_xy(f['points_xy'])}"
            s += f", tolerance ±{num(float(f.get('tolerance') or 0.5))} in axis units"
        lines.append(s)
    if spec.get("notes"):
        lines.append(f"Teacher's notes: {spec['notes']}")
    if ctx.get("diagram_reference"):
        lines.append("The LAST image is the teacher's correct drawing (reference). The FIRST image is the "
                     "student's drawing. Grade only the student's drawing; compare it with the reference.")
    lines += [
        "For EVERY feature give: status \"present\" (drawn and correct), \"wrong\" (drawn but incorrect) "
        "or \"missing\" (not drawn); points between 0 and the feature's points (partial points only when "
        "part of it is correct); a short note; and where it is on the student's image: \"box\": "
        "[x0, y0, x1, y1] or \"point\": [x, y], as fractions 0..1 of the FIRST image's width and height "
        "from its top-left corner (null if missing).",
        "Where the drawing has axes with a scale, read the plotted points / key values off the axes "
        "(\"readings\", with \"at\" = where the point is on the image, 0..1).",
        "Put the checklist results in \"features\" and return \"criteria\": []. Use \"steps\" for short "
        "descriptions of what was drawn. \"points\" is the sum of the feature points.",
        'Add to the JSON: "features": [{"id": "f1", "status": "present|wrong|missing", "points": number, '
        '"note": "short", "box": [x0, y0, x1, y1] or null, "point": [x, y] or null}], '
        '"readings": [{"label": "A", "x": number, "y": number, "at": [x, y]}]',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- parsing
def _f(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _norm_coords(vals: list[float]) -> list[float] | None:
    """Fractions 0..1; percentages (any value > 1 and all <= 100) are accepted and converted."""
    if any(v < -0.05 for v in vals):
        return None
    if any(v > 1.0001 for v in vals):
        if all(v <= 100 for v in vals):
            vals = [v / 100 for v in vals]
        else:
            return None
    return [round(min(1.0, max(0.0, v)), 4) for v in vals]


def parse_box(raw: Any) -> list[float] | None:
    if isinstance(raw, dict):
        if "x0" in raw:
            raw = [raw.get("x0"), raw.get("y0"), raw.get("x1"), raw.get("y1")]
        else:  # {"x", "y", "w", "h"}
            x, y, w, h = (_f(raw.get(k)) for k in ("x", "y", "w", "h"))
            raw = None if None in (x, y, w, h) else [x, y, x + w, y + h]  # type: ignore[operator]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    vals = [_f(v) for v in raw]
    if any(v is None for v in vals):
        return None
    vals = _norm_coords(vals)  # type: ignore[arg-type]
    if vals is None:
        return None
    x0, y0, x1, y1 = vals
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if x1 - x0 < 0.002 or y1 - y0 < 0.002:
        return None
    return [x0, y0, x1, y1]


def parse_point(raw: Any) -> list[float] | None:
    if isinstance(raw, dict):
        raw = [raw.get("x"), raw.get("y")]
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    vals = [_f(v) for v in raw]
    if any(v is None for v in vals):
        return None
    return _norm_coords(vals)  # type: ignore[arg-type]


def parse_features(raw: Any, spec: dict) -> tuple[list[dict], dict[str, bool]]:
    """Checklist rows aligned to the spec (unknown features dropped; a feature the model skipped
    is not credited and marked not_assessed). Returns (rows, flags)."""
    feats = spec["features"]
    ids = {f["id"] for f in feats}
    given: dict[str, dict] = {}
    if isinstance(raw, list):
        by_label = {" ".join(f["label"].lower().split()): f["id"] for f in feats}
        dict_rows = [r for r in raw if isinstance(r, dict)]
        for i, r in enumerate(dict_rows):
            fid = str(r.get("id") or "").strip()
            if fid not in ids:
                fid = by_label.get(" ".join(str(r.get("label") or "").lower().split()), "")
            if not fid and not r.get("id") and len(dict_rows) == len(feats):
                fid = feats[i]["id"]  # positional fallback (no ids at all)
            if fid and fid not in given:
                given[fid] = r
    flags = {"missing": False, "inconsistent": False}
    rows = []
    for f in feats:
        mx = float(f["points"])
        r = given.get(f["id"])
        box = point = None
        if r is None:
            flags["missing"] = True
            status, pts, note = "not_assessed", 0.0, "Not checked by AI"
        else:
            st = str(r.get("status") or "").strip().lower()
            status = _STATUS.get(st)
            if status is None:
                met = r.get("met", r.get("present"))
                status = "present" if met is True else "missing" if met is False else None
            v = _f(r.get("points"))
            if status is None and v is not None:
                status = "present" if mx > 0 and v >= mx - 1e-9 else "wrong"
            if status is None:
                flags["missing"] = True
                status, v = "not_assessed", 0.0
            if v is None:
                v = mx if status == "present" else 0.0
            pts = max(0.0, min(mx, v))
            if status == "missing" and pts > 0:
                flags["inconsistent"], pts = True, 0.0
            elif status == "wrong" and mx > 0 and pts >= mx - 1e-9:
                flags["inconsistent"], pts = True, 0.0
            note = str(r.get("note") or "").strip()[:300]
            box = parse_box(r.get("box"))
            point = parse_point(r.get("point")) if box is None else None
            if status == "not_assessed":
                note = note or "Not checked by AI"
        rows.append({"id": f["id"], "key": f.get("key") or "custom", "label": f["label"],
                     "points": num(round(pts, 2)), "max_points": num(mx), "met": mx > 0 and abs(pts - mx) < 1e-9,
                     "status": status, "note": note, "box": box, "point": point})
    return rows, flags


def parse_readings(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for r in raw[:50]:
        if isinstance(r, (list, tuple)) and len(r) == 2:
            r = {"x": r[0], "y": r[1]}
        if not isinstance(r, dict):
            continue
        x, y = _f(r.get("x")), _f(r.get("y"))
        if x is None or y is None:
            continue
        out.append({"label": str(r.get("label") or "").strip()[:40], "x": num(x), "y": num(y),
                    "at": parse_point(r.get("at"))})
    return out


def parse_extra(out: Any, d: dict, q: dict, ctx: dict, reasons: list[str]) -> dict:
    """Checklist results -> the draft (points, criteria mirror, confidence, review reasons).
    `reasons` (the step grader's list) is edited in place. Never raises."""
    spec = ctx.get("diagram")
    if not spec:
        return d
    from . import ai_grade  # late: ai_grade imports this module
    for k, v in REVIEW_TEXT.items():
        ai_grade.REVIEW_TEXT.setdefault(k, v)
    if not isinstance(out, dict):
        out = {}
    raw = out.get("features")
    if not isinstance(raw, list) or not raw:
        raw = out.get("criteria")  # a model that answered in the rubric format
    rows, flags = parse_features(raw, spec)
    readings = parse_readings(out.get("readings"))
    from . import diagram_cv
    check = diagram_cv.cross_check(spec, rows, readings, ctx.get("diagram_cv"))
    # the checklist replaces the rubric: drop the step grader's criteria / step complaints
    for r in ("missing_criteria", "total_mismatch", "no_steps"):
        while r in reasons:
            reasons.remove(r)
    if flags["missing"]:
        reasons.append("missing_features")
    if flags["inconsistent"]:
        reasons.append("features_inconsistent")
    conf = _f(d.get("confidence"))
    conf = 0.5 if conf is None else conf
    if check.get("agrees") is False:
        reasons.append("diagram_check_disagrees")
        conf = min(conf * 0.8, DISAGREE_CONFIDENCE)
    total = sum(float(r["points"]) for r in rows)
    d["points"] = total
    d["confidence"] = conf
    d["criteria"] = [{"id": r["id"], "label": r["label"], "points": r["points"], "max_points": r["max_points"],
                      "met": r["met"], "note": r["note"]} for r in rows]
    if not d.get("transcript"):
        d["transcript"] = "; ".join(f"{r['label']}: {r['status']}" for r in rows)[:4000]
    d["diagram"] = {"kind": spec["kind"], "kind_label": kind_label(spec["kind"]), "features": rows,
                    "readings": readings, "check": check, "reference_image": spec.get("reference_image"),
                    "prompt_version": DIAGRAM_PROMPT_VERSION}
    return d


def group_key(draft: dict) -> str:
    """Bulk-approve groups for drawings: identical feature verdicts and points, and only when the
    picture check did not disagree (a disagreement keeps the answer on its own)."""
    dg = draft.get("diagram") or {}
    if (dg.get("check") or {}).get("agrees") is False or draft.get("needs_review"):
        return "none"
    sig = ",".join(f"{f['id']}={f['status']}:{f['points']}" for f in dg.get("features") or [])
    return f"d:{draft.get('points')}|{sig}"[:300]


# ---------------------------------------------------------------- before the AI call
async def prepare(ctx: dict, images: list[bytes]) -> tuple[dict, list[bytes]]:
    """Reference drawing (as the last image) + OpenCV analysis of the student's drawing."""
    spec = ctx.get("diagram")
    if not spec or not images:
        return ctx, images
    from . import diagram_cv
    from .storage import read_url
    ctx = dict(ctx)
    ctx["diagram_cv"] = await asyncio.to_thread(diagram_cv.analyze, images[0])
    if spec.get("reference_image"):
        ref = await asyncio.to_thread(read_url, spec["reference_image"])
        if ref:
            images = [*images, ref]
            ctx["diagram_reference"] = True
    return ctx, images


# ---------------------------------------------------------------- grade + student view
def grade_diagram(grade: dict, draft: dict | None, spec: dict | None) -> dict | None:
    """Feature results for the grade record, recomputed from the approved criteria points."""
    if not spec:
        return None
    dd = (draft or {}).get("diagram") or {}
    ai_rows = {r["id"]: r for r in dd.get("features") or []}
    approved = {c["id"]: c for c in grade.get("criteria") or []}
    feats = []
    for f in spec["features"]:
        a = ai_rows.get(f["id"]) or {}
        mx = float(f["points"])
        c = approved.get(f["id"])
        if c is not None:
            pts = float(c["points"])
            if mx > 0 and abs(pts - mx) < 1e-9:
                status = "present"
            elif pts <= 1e-9:
                status = a.get("status") if a.get("status") in ("wrong", "missing") else "wrong"
            else:
                status = "partial"
        else:  # teacher typed a total only: keep the AI's marks (if any)
            pts = float(a.get("points") or 0)
            status = a.get("status") or "not_assessed"
        note = a.get("note") or ""
        if c is not None and abs(pts - float(a.get("points") or 0)) > 1e-9:
            note = ""  # the teacher changed the AI's verdict: its note would contradict the result
        feats.append({"id": f["id"], "label": f["label"], "points": num(round(pts, 2)), "max_points": num(mx),
                      "status": status, "note": note, "box": a.get("box"), "point": a.get("point")})
    return {"kind": spec["kind"], "kind_label": kind_label(spec["kind"]), "features": feats,
            "reference_image": spec.get("reference_image")}


def student_view(g: dict | None) -> dict | None:
    """What the student's feedback page shows (approved grade only, never AI drafts)."""
    dg = (g or {}).get("diagram")
    if not dg:
        return None
    return {"kind": dg.get("kind"), "kind_label": dg.get("kind_label"),
            "features": [{k: f.get(k) for k in ("id", "label", "points", "max_points", "status", "note", "box", "point")}
                         for f in dg.get("features") or []],
            "reference_image": dg.get("reference_image")}


# ---------------------------------------------------------------- endpoints
@router.get("/diagram-kinds")
async def diagram_kinds(p: Principal = Depends(require_teacher)):
    return {"kinds": catalog()}


def _q_out(i: int, q: dict, entry: dict | None) -> dict:
    eff = effective(q, entry)
    src = None
    if eff is not None:
        src = "key" if (entry or {}).get("diagram") else "question"
    return {"question_id": q["id"], "index": i, "type": q.get("type"), "prompt": q.get("prompt", ""),
            "points": q.get("points", 1), "diagram": eff, "source": src,
            "question_diagram": q.get("diagram") if q.get("type") == "essay" else None}


@router.get("/exams/{exam_id}/diagrams")
async def list_diagrams(exam_id: str, p: Principal = Depends(require_teacher),
                        session: AsyncSession = Depends(get_session)):
    from .answer_key import _own_exam, load_key
    e = await _own_exam(session, exam_id, p)
    entries = (await load_key(session, e.id)).get("questions") or {}
    return {"exam_id": e.id, "kinds": catalog(),
            "questions": [_q_out(i, q, entries.get(q["id"])) for i, q in enumerate(exam_questions(e))]}


class DiagramPut(BaseModel):
    diagram: dict | None = None   # null: drop the answer-key override (use the question's own)


@router.put("/exams/{exam_id}/diagrams/{qid}")
async def put_diagram(exam_id: str, qid: str, body: DiagramPut, p: Principal = Depends(require_teacher),
                      session: AsyncSession = Depends(get_session)):
    """Mark a written-answer question as a graph / diagram (or edit its checklist) for grading this
    exam. Stored in the answer key, so it works after the exam was given."""
    from .answer_key import _own_exam, get_config
    e = await _own_exam(session, exam_id, p)
    q = next((x for x in exam_questions(e) if x["id"] == qid), None)
    if q is None:
        raise HTTPException(404, "Question not found")
    if q.get("type") != "essay":
        raise HTTPException(422, "Only written-answer questions can be graph / diagram questions")
    try:
        spec = clean_diagram(body.diagram, float(q.get("points", 1))) if body.diagram is not None else None
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from None
    row = await get_config(session, e.id, create=True)
    key = dict(row.answer_key or {})
    entries = dict(key.get("questions") or {})
    ent = dict(entries.get(qid) or {})
    if spec is None:
        ent.pop("diagram", None)
    else:
        ent["diagram"] = spec
    entries[qid] = ent
    key["questions"] = entries
    key["updated_at"] = iso(utcnow())
    row.answer_key, row.updated_at = key, utcnow()
    await session.commit()
    i = next(i for i, x in enumerate(exam_questions(e)) if x["id"] == qid)
    return _q_out(i, q, ent)


__all__ = ["KINDS", "prompt_extra", "parse_extra", "prepare", "grade_diagram", "student_view", "router"]
