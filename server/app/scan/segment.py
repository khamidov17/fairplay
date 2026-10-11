"""Template-free answer finding: where on a student's pages is the answer to each question?

Works on ANY paper (own answer sheets, notebook pages, phone photos already flattened by
photo.py) — no FairPlay layout or QR code needed.

    seg = await segment_pages(exam_questions, page_images)          # async, never raises
    answers = region_answers(questions, seg["regions"], page_urls)   # sync: crops -> uploads
    seg, answers = await find_answers(exam_questions, page_urls)     # both, for stored uploads

Two stages:
1. Deterministic pre-pass (OpenCV, `candidate_regions`): remove ruled lines, find text lines
   by the row ink profile, merge lines into blocks split at large horizontal gaps. Every block
   is a numbered candidate box drawn onto the page image sent to the model.
2. Vision prompt (`PROMPT_VERSION`) via app.ai.complete_json: for every question the model
   picks candidate numbers (or, rarely, a raw box) per page, with a confidence; it also
   reports crossed-out work and the student's name / ID. `parse_segmentation` validates
   everything (unknown questions dropped, boxes clamped / snapped to candidates, missing
   questions listed) — the model's output is never trusted as is.

Coordinates are normalised to the page image: [x0, y0, x1, y1] in 0..1, origin top-left.

Result of `segment_pages` (also documented in CONTRACT.md "Any-paper answer finding"):
{
  "prompt_version": "fp-segment-v1", "source": "ai" | "fallback",
  "pages": [{"index": 0, "candidates": [{"n": 1, "box": [...]}]}],
  "regions": [{"id": "r1", "question_id": "q…" | None, "page": 0, "box": [x0,y0,x1,y1],
               "confidence": 0.9, "crossed_out": False, "label": "2b", "order": 0,
               "source": "ai", "needs_check": False}],
  "missing": ["q…"],                       # questions with no (non-crossed-out) region
  "name": {"text": "Alice Smith", "student_id": "S123", "page": 0, "box": [...] | None,
           "confidence": 0.8} | None,
  "confidence": 0.85,                       # lowest region confidence (0 when nothing found)
  "needs_check": bool, "reasons": ["low_confidence", "missing", "ai_off", ...],
}
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import cv2
import numpy as np

from .. import ai

log = logging.getLogger("fairplay.scan.segment")

PROMPT_VERSION = "fp-segment-v1"
LOW_CONFIDENCE = 0.7
MAX_PAGES_PER_CALL = 8
MAX_CANDIDATES = 40          # per page
WORK_W = 1000                # width of the working copy for the OpenCV pre-pass
SEND_LONG = 1400             # long side of the annotated page image sent to the model
MIN_BOX = 0.01               # smallest box side (normalised)
RETRIES = 1

SYSTEM = (
    "You locate students' handwritten answers on photographed or scanned exam pages. "
    "The pages were NOT printed from a template: students write question numbers themselves "
    "(like \"2b\", \"Q3\", \"3)\"), may answer out of order, continue an answer on the next page, "
    "and cross out work. Each page image has numbered candidate boxes drawn on it (thin coloured "
    "rectangles with a number at their top-left corner) around blocks of writing. For every exam "
    "question, choose the candidate box numbers that contain the student's answer (all pages, in "
    "reading order). Work the student crossed out does not count as an answer: report it under "
    "crossed_out. Also read the student's name and student ID if written (usually at the top of "
    "the first page). Never invent answers: if a question is not answered, list it in not_found. "
    "Reply with ONE JSON object only."
)

SCHEMA = (
    '{"name": {"text": "name as written or empty", "student_id": "ID as written or empty", '
    '"candidates": [numbers], "confidence": 0-1}, '
    '"answers": [{"question": "question id (or its number)", "label": "number as the student wrote it", '
    '"candidates": [numbers], "boxes": [{"page": 1-based page, "box": [x0, y0, x1, y1] in 0..1}], '
    '"confidence": 0-1}], '
    '"crossed_out": [{"question": "id or null", "candidates": [numbers]}], '
    '"not_found": ["question ids"]}'
)


# ---------------------------------------------------------------- geometry helpers
def clamp_box(box: Any) -> list[float] | None:
    """[x0,y0,x1,y1] in 0..1 with x0<x1, y0<y1 and a minimum size, or None when unusable."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if any(v != v or v in (float("inf"), float("-inf")) for v in (x0, y0, x1, y1)):
        return None
    # some models answer in percent
    if 2.0 < max(x0, y0, x1, y1) <= 100.0:
        x0, y0, x1, y1 = x0 / 100, y0 / 100, x1 / 100, y1 / 100
    x0, x1 = sorted((min(1.0, max(0.0, x0)), min(1.0, max(0.0, x1))))
    y0, y1 = sorted((min(1.0, max(0.0, y0)), min(1.0, max(0.0, y1))))
    if x1 - x0 < MIN_BOX or y1 - y0 < MIN_BOX:
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            return None
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hw, hh = max(MIN_BOX, x1 - x0) / 2, max(MIN_BOX, y1 - y0) / 2
        x0, x1 = max(0.0, cx - hw), min(1.0, cx + hw)
        y0, y1 = max(0.0, cy - hh), min(1.0, cy + hh)
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def union(boxes: list[list[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def area(b: list[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def overlap(a: list[float], b: list[float]) -> float:
    return area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])


def snap_box(box: list[float], candidates: list[list[float]]) -> list[float]:
    """Grow a raw model box to the candidate blocks it mostly covers or mostly lies in (model
    coordinates are rough; the OpenCV blocks hug the ink)."""
    hits = [c for c in candidates if area(c) > 0 and (overlap(box, c) / area(c) >= 0.5 or
                                                     overlap(box, c) / max(area(box), 1e-9) >= 0.6)]
    return clamp_box(union([box] + hits)) or box if hits else box


# ---------------------------------------------------------------- OpenCV pre-pass
def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def ink_mask(gray: np.ndarray) -> np.ndarray:
    """Binary ink mask (255 = ink) with ruled lines, margins and specks removed."""
    h, w = gray.shape[:2]
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    mask = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    # light printed rules / grid lines are much paler than ink: keep only clearly dark pixels
    otsu, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.bitwise_and(mask, (blur < min(230, otsu + 15)).astype(np.uint8) * 255)
    # ruled notebook lines / table borders: long thin horizontal or vertical runs
    hl = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 10), 1)))
    vl = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 10))))
    lines = cv2.dilate(cv2.bitwise_or(hl, vl), np.ones((3, 3), np.uint8))
    mask = cv2.subtract(mask, lines)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    # page edges / shadows from flattening
    mx, my = max(2, int(w * 0.015)), max(2, int(h * 0.012))
    mask[:my, :] = 0
    mask[-my:, :] = 0
    mask[:, :mx] = 0
    mask[:, -mx:] = 0
    # drop specks (dust, JPEG noise)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n > 1:
        keep = np.zeros(n, dtype=np.uint8)
        keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= max(4, (w * h) // 400_000)).astype(np.uint8)
        mask = (keep[labels] * 255).astype(np.uint8)
    return mask


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start = None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(flags)))
    return out


def text_lines(mask: np.ndarray) -> list[tuple[int, int]]:
    """Row ranges (y0, y1) that contain writing."""
    h, w = mask.shape[:2]
    rows = (mask > 0).sum(axis=1)
    thresh = max(2, int(w * 0.002))
    lines = _runs(rows >= thresh)
    # bridge tiny gaps inside a line (dots of i, accents, fraction bars)
    merged: list[list[int]] = []
    for y0, y1 in lines:
        if merged and y0 - merged[-1][1] <= max(2, h // 400):
            merged[-1][1] = y1
        else:
            merged.append([y0, y1])
    return [(a, b) for a, b in merged if b - a >= max(3, h // 500)]


def _x_extent(mask: np.ndarray, y0: int, y1: int) -> tuple[int, int] | None:
    cols = (mask[y0:y1] > 0).sum(axis=0)
    xs = np.nonzero(cols)[0]
    if xs.size == 0:
        return None
    return int(xs[0]), int(xs[-1]) + 1


def candidate_regions(img: np.ndarray, max_candidates: int = MAX_CANDIDATES) -> list[list[float]]:
    """Candidate answer blocks on one page, top to bottom, normalised [x0,y0,x1,y1]."""
    gray = _gray(img)
    h0, w0 = gray.shape[:2]
    if h0 < 20 or w0 < 20:
        return []
    f = WORK_W / w0 if w0 > WORK_W else 1.0
    if f < 1:
        gray = cv2.resize(gray, (int(w0 * f), int(h0 * f)), interpolation=cv2.INTER_AREA)
    h, w = gray.shape[:2]
    mask = ink_mask(gray)
    lines = text_lines(mask)
    if not lines:
        return []
    heights = sorted(b - a for a, b in lines)
    line_h = heights[len(heights) // 2]
    gap_limit = max(int(1.1 * line_h), int(h * 0.018))
    blocks: list[list[int]] = []
    for y0, y1 in lines:
        if blocks and y0 - blocks[-1][1] <= gap_limit:
            blocks[-1][1] = y1
        else:
            blocks.append([y0, y1])
    # too many blocks: merge the pair with the smallest gap until under the cap
    while len(blocks) > max_candidates:
        gaps = [blocks[i + 1][0] - blocks[i][1] for i in range(len(blocks) - 1)]
        i = int(np.argmin(gaps))
        blocks[i] = [blocks[i][0], blocks[i + 1][1]]
        del blocks[i + 1]
    out: list[list[float]] = []
    pad_y, pad_x = max(3, line_h // 3), max(4, w // 150)
    for y0, y1 in blocks:
        ext = _x_extent(mask, y0, y1)
        if ext is None:
            continue
        x0, x1 = ext
        if (x1 - x0) < w * 0.015 and (y1 - y0) < h * 0.01:
            continue  # a lone speck that survived
        box = clamp_box([(x0 - pad_x) / w, (y0 - pad_y) / h, (x1 + pad_x) / w, (y1 + pad_y) / h])
        if box:
            out.append(box)
    return out


PALETTE = [(216, 75, 47), (12, 121, 180), (40, 150, 60), (160, 60, 170), (200, 120, 0), (0, 140, 140)]


def annotate(img: np.ndarray, candidates: list[list[float]], first_number: int) -> np.ndarray:
    """Copy of the page (resized for the model) with numbered candidate boxes drawn on it."""
    gray = _gray(img)
    h, w = gray.shape[:2]
    f = SEND_LONG / max(h, w) if max(h, w) > SEND_LONG else 1.0
    if f < 1:
        gray = cv2.resize(gray, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    out = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    h, w = out.shape[:2]
    scale = max(0.45, w / 1600)
    for i, b in enumerate(candidates):
        n = first_number + i
        color = PALETTE[n % len(PALETTE)]
        p0 = (int(b[0] * w), int(b[1] * h))
        p1 = (int(b[2] * w), int(b[3] * h))
        cv2.rectangle(out, p0, p1, color, 2)
        label = str(n)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        lx, ly = max(0, p0[0] - tw - 6), max(th + 2, p0[1] + th + 2)
        cv2.rectangle(out, (lx, ly - th - 3), (lx + tw + 4, ly + 3), color, -1)
        cv2.putText(out, label, (lx + 2, ly), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2)
    return out


def jpeg(img: np.ndarray, quality: int = 82) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes()


def decode(data: bytes) -> np.ndarray | None:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    return img


# ---------------------------------------------------------------- prompt
def question_lines(questions: list[dict]) -> list[str]:
    out = []
    for i, q in enumerate(questions):
        prompt = " ".join(str(q.get("prompt") or "").split())[:240]
        out.append(f"{i + 1}. id={q['id']} ({q.get('type')}): {prompt}")
    return out


def build_prompt(questions: list[dict], pages: list[dict], hint: str = "") -> str:
    parts = [f"Prompt version: {PROMPT_VERSION}",
             f"The student's pages: {len(pages)} image(s), in the order given (page 1 = first image).",
             "Exam questions (number. id (type): text):", *question_lines(questions),
             "Candidate boxes per page (number: [x0, y0, x1, y1] as fractions of the page width/height):"]
    for p in pages:
        parts.append(f"Page {p['index'] + 1}: " + ("; ".join(
            f"{c['n']}: [{', '.join(f'{v:.3f}' for v in c['box'])}]" for c in p["candidates"]) or "no candidates"))
    parts.append("Use candidate numbers whenever the answer is inside candidate boxes; give a raw box "
                 "(with its page) only for writing no candidate covers. A box can contain the question "
                 "number written by the student — include it. Diagrams and graphs belong to the answer "
                 "they illustrate.")
    if hint:
        parts.append(f"Teacher's note: {hint}")
    parts.append(f"Return JSON exactly like: {SCHEMA}")
    return "\n".join(parts)


# ---------------------------------------------------------------- parsing / validation
def _float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _conf(v: Any, default: float = 0.5) -> float:
    c = _float(v)
    if c is None:
        return default
    if 1 < c <= 100:
        c = c / 100
    return round(max(0.0, min(1.0, c)), 2)


_QNUM = re.compile(r"^\s*(?:q(?:uestion)?\.?\s*|#|no\.?\s*|№\s*)?(\d{1,3})\s*([a-z])?\s*[).:\]]?\s*$", re.I)


def resolve_question(raw: Any, questions: list[dict]) -> str | None:
    """Question id from the model's reference: the id itself, or a number as the student
    wrote it ("2", "Q2", "2)", "#2"); "2b" maps to question 2 unless a question's prompt
    starts with that sub-label."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)) and float(raw).is_integer():
        raw = str(int(raw))
    s = str(raw).strip()
    if not s:
        return None
    ids = {q["id"]: q["id"] for q in questions}
    if s in ids:
        return s
    m = _QNUM.match(s)
    if not m:  # an id with different case (a written label like "Q2" wins over an id "q2")
        low = s.lower()
        return next((q["id"] for q in questions if q["id"].lower() == low), None)
    n, sub = int(m.group(1)), (m.group(2) or "").lower()
    if sub:  # a question whose prompt starts with "2b" / "2b)" / "(b)"
        for q in questions:
            head = str(q.get("prompt") or "").strip().lower()
            if re.match(rf"^(q\s*)?{n}\s*\(?{sub}\b", head):
                return q["id"]
    if 1 <= n <= len(questions):
        return questions[n - 1]["id"]
    return None


def _int_list(v: Any) -> list[int]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = [v]
    out: list[int] = []
    if isinstance(v, list):
        for x in v[:200]:
            f = _float(x)
            if f is not None and float(f).is_integer() and int(f) not in out:
                out.append(int(f))
    return out


def _boxes_of(item: dict, cand_index: dict[int, tuple[int, list[float]]], n_pages: int,
              page_candidates: dict[int, list[list[float]]]) -> list[tuple[int, list[float]]]:
    """(page, box) parts of one answer: candidates grouped by page (union per page), then raw boxes."""
    by_page: dict[int, list[list[float]]] = {}
    for n in _int_list(item.get("candidates")):
        hit = cand_index.get(n)
        if hit is not None:
            by_page.setdefault(hit[0], []).append(hit[1])
    parts = [(p, clamp_box(union(bs))) for p, bs in by_page.items()]
    raw = item.get("boxes")
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list):
        for r in raw[:10]:
            if not isinstance(r, dict):
                continue
            pg = _float(r.get("page"))
            page = int(pg) - 1 if pg is not None and float(pg).is_integer() else 0
            if not 0 <= page < n_pages:
                continue
            b = clamp_box(r.get("box"))
            if b is None:
                continue
            b = snap_box(b, page_candidates.get(page, []))
            # merge with a candidate union on the same page
            same = [i for i, (p, _) in enumerate(parts) if p == page]
            if same:
                i = same[0]
                parts[i] = (page, clamp_box(union([parts[i][1], b])))
            else:
                parts.append((page, b))
    return sorted([(p, b) for p, b in parts if b is not None], key=lambda x: (x[0], x[1][1]))


def parse_segmentation(out: Any, questions: list[dict], pages: list[dict]) -> dict:
    """Validate the model's JSON (possibly junk) into a segmentation result. Never raises."""
    if not isinstance(out, dict):
        out = {}
    n_pages = len(pages)
    cand_index: dict[int, tuple[int, list[float]]] = {}
    page_candidates: dict[int, list[list[float]]] = {}
    for p in pages:
        page_candidates[p["index"]] = [c["box"] for c in p["candidates"]]
        for c in p["candidates"]:
            cand_index[c["n"]] = (p["index"], c["box"])
    regions: list[dict] = []
    raw_answers = out.get("answers")
    if isinstance(raw_answers, dict):  # {"q1": {...}} variant
        raw_answers = [dict(v, question=k) if isinstance(v, dict) else {"question": k, "candidates": v}
                       for k, v in raw_answers.items()]
    seen_q: set[str] = set()
    for item in raw_answers if isinstance(raw_answers, list) else []:
        if not isinstance(item, dict):
            continue
        qid = resolve_question(item.get("question", item.get("question_id", item.get("id"))), questions)
        if qid is None:
            continue  # unknown question: dropped
        conf = _conf(item.get("confidence"))
        label = str(item.get("label") or "").strip()[:20]
        crossed = bool(item.get("crossed_out"))
        for page, box in _boxes_of(item, cand_index, n_pages, page_candidates):
            regions.append({"question_id": qid, "page": page, "box": box, "confidence": conf,
                            "crossed_out": crossed, "label": label, "source": "ai"})
        seen_q.add(qid)
    crossed_raw = out.get("crossed_out")
    for item in crossed_raw if isinstance(crossed_raw, list) else []:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            item = {"candidates": [item]}
        if not isinstance(item, dict):
            continue
        qid = resolve_question(item.get("question"), questions)
        for page, box in _boxes_of(item, cand_index, n_pages, page_candidates):
            regions.append({"question_id": qid, "page": page, "box": box, "confidence": _conf(item.get("confidence"), 0.8),
                            "crossed_out": True, "label": "", "source": "ai"})
    # the same box claimed by two questions: keep the more confident claim
    regions = _dedupe(regions)
    regions.sort(key=lambda r: (r["page"], r["box"][1], r["box"][0]))
    for i, r in enumerate(regions):
        r["id"] = f"r{i + 1}"
        r["order"] = i
        r["needs_check"] = r["confidence"] < LOW_CONFIDENCE and not r["crossed_out"]
    found = {r["question_id"] for r in regions if not r["crossed_out"] and r["question_id"]}
    missing = [q["id"] for q in questions if q["id"] not in found]
    name = _parse_name(out.get("name"), cand_index)
    live = [r for r in regions if not r["crossed_out"]]
    reasons: list[str] = []
    if any(r["needs_check"] for r in live):
        reasons.append("low_confidence")
    if missing:
        reasons.append("missing")
    if not regions and not out:
        reasons.append("ai_failed")
    return {
        "prompt_version": PROMPT_VERSION, "source": "ai", "pages": pages, "regions": regions,
        "missing": missing, "name": name,
        "confidence": round(min((r["confidence"] for r in live), default=0.0), 2),
        "needs_check": bool(reasons), "reasons": reasons,
    }


def _dedupe(regions: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in sorted(regions, key=lambda r: -r["confidence"]):
        clash = next((o for o in out if o["page"] == r["page"] and o["question_id"] != r["question_id"]
                      and not o["crossed_out"] and not r["crossed_out"]
                      and overlap(o["box"], r["box"]) >= 0.85 * min(area(o["box"]), area(r["box"]))), None)
        if clash is not None:
            clash["needs_check"] = True
            clash["confidence"] = min(clash["confidence"], LOW_CONFIDENCE - 0.01)
            continue
        if any(o["page"] == r["page"] and o["question_id"] == r["question_id"] and o["box"] == r["box"]
               for o in out):
            continue
        out.append(r)
    return out


def _parse_name(raw: Any, cand_index: dict[int, tuple[int, list[float]]]) -> dict | None:
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return None
    text = " ".join(str(raw.get("text") or "").split())[:120]
    sid = " ".join(str(raw.get("student_id") or raw.get("id") or "").split())[:60]
    if not text and not sid:
        return None
    page, box = 0, None
    cands = [cand_index[n] for n in _int_list(raw.get("candidates")) if n in cand_index]
    if cands:
        page = cands[0][0]
        box = clamp_box(union([b for p, b in cands if p == page]))
    return {"text": text, "student_id": sid, "page": page, "box": box, "confidence": _conf(raw.get("confidence"), 0.6)}


def fallback(questions: list[dict], pages: list[dict], reason: str) -> dict:
    """No AI (off or failed): candidates only, every question "not found", teacher checks."""
    return {"prompt_version": PROMPT_VERSION, "source": "fallback", "pages": pages, "regions": [],
            "missing": [q["id"] for q in questions], "name": None, "confidence": 0.0,
            "needs_check": True, "reasons": [reason]}


# ---------------------------------------------------------------- main entry point
def prepare_pages(page_images: list[bytes | np.ndarray]) -> tuple[list[dict], list[bytes]]:
    """OpenCV pre-pass for all pages (sync): candidate boxes numbered across pages, and the
    annotated JPEGs for the model."""
    pages: list[dict] = []
    annotated: list[bytes] = []
    n = 1
    for i, data in enumerate(page_images):
        img = decode(data) if isinstance(data, (bytes, bytearray)) else _gray(data)
        if img is None:
            pages.append({"index": i, "candidates": []})
            annotated.append(b"")
            continue
        boxes = candidate_regions(img)
        pages.append({"index": i, "candidates": [{"n": n + k, "box": b} for k, b in enumerate(boxes)]})
        annotated.append(jpeg(annotate(img, boxes, n)))
        n += len(boxes)
    return pages, annotated


async def segment_pages(exam_questions: list[dict], page_images: list[bytes | np.ndarray],
                        hint: str = "") -> dict:
    """Find each question's answer region(s) on one student's pages (any order, any paper).

    exam_questions: the exam's questions (dicts with "id", "type", "prompt"), in exam order.
    page_images:    the student's pages as encoded image bytes (PNG/JPEG) or BGR/gray arrays,
                    already flattened (photo.rectify) — any order.
    Returns the result dict described in the module docstring. Never raises: with AI off or
    failing, the result has source "fallback", no regions and needs_check True.
    """
    questions = [q for q in exam_questions if isinstance(q, dict) and q.get("id")]
    pages, annotated = await asyncio.to_thread(prepare_pages, list(page_images))
    if not questions:
        return fallback(questions, pages, "no_questions")
    if not ai.enabled():
        return fallback(questions, pages, "ai_off")
    if not pages:
        return fallback(questions, pages, "no_pages")
    # long sets: several calls of MAX_PAGES_PER_CALL pages, results concatenated
    results: list[dict] = []
    for start in range(0, len(pages), MAX_PAGES_PER_CALL):
        chunk = pages[start:start + MAX_PAGES_PER_CALL]
        local = [{"index": p["index"] - start, "candidates": p["candidates"]} for p in chunk]
        imgs = [a for a in annotated[start:start + MAX_PAGES_PER_CALL] if a]
        out = None
        last = ""
        for attempt in range(RETRIES + 1):
            try:
                out = await ai.complete_json(SYSTEM, build_prompt(questions, local, hint), images=imgs,
                                             max_tokens=1800)
                break
            except ai.AIUnavailable as e:
                last = str(e)
                if attempt < RETRIES:
                    await asyncio.sleep(0.3)
        if out is None:
            log.warning("segmentation failed: %s", last)
            return fallback(questions, pages, "ai_failed")
        res = parse_segmentation(out, questions, local)
        for r in res["regions"]:
            r["page"] += start
        if res["name"]:
            res["name"]["page"] += start
        results.append(res)
    return _combine(results, questions, pages)


def _combine(results: list[dict], questions: list[dict], pages: list[dict]) -> dict:
    if len(results) == 1:
        res = results[0]
        res["pages"] = pages
        return res
    regions = [r for res in results for r in res["regions"]]
    for i, r in enumerate(regions):
        r["id"], r["order"] = f"r{i + 1}", i
    found = {r["question_id"] for r in regions if not r["crossed_out"] and r["question_id"]}
    missing = [q["id"] for q in questions if q["id"] not in found]
    live = [r for r in regions if not r["crossed_out"]]
    reasons = sorted({x for res in results for x in res["reasons"] if x != "missing"} | ({"missing"} if missing else set()))
    return {"prompt_version": PROMPT_VERSION, "source": "ai", "pages": pages, "regions": regions,
            "missing": missing, "name": next((res["name"] for res in results if res["name"]), None),
            "confidence": round(min((r["confidence"] for r in live), default=0.0), 2),
            "needs_check": bool(reasons), "reasons": reasons}


# ---------------------------------------------------------------- crops
def crop_answer(page_images: list[np.ndarray], parts: list[tuple[int, list[float]]], pad: float = 0.008) -> np.ndarray | None:
    """One image of an answer: every (page, box) part cropped and stacked top to bottom
    (an answer that continues on the next page becomes one picture)."""
    crops = []
    for page, box in parts:
        if not 0 <= page < len(page_images) or page_images[page] is None:
            continue
        img = _gray(page_images[page])
        h, w = img.shape[:2]
        x0, y0 = max(0, int((box[0] - pad) * w)), max(0, int((box[1] - pad) * h))
        x1, y1 = min(w, int((box[2] + pad) * w)), min(h, int((box[3] + pad) * h))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        crops.append(img[y0:y1, x0:x1])
    if not crops:
        return None
    width = max(c.shape[1] for c in crops)
    sep = np.full((6, width), 200, np.uint8)
    rows = []
    for i, c in enumerate(crops):
        if c.shape[1] < width:
            c = np.hstack([c, np.full((c.shape[0], width - c.shape[1]), 255, np.uint8)])
        if i:
            rows.append(sep)
        rows.append(c)
    return np.vstack(rows)


def png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    return buf.tobytes()


def region_answers(questions: list[dict], regions: list[dict], page_urls: list[str],
                   page_keys: list[Any] | None = None, extra: dict | None = None) -> dict[str, dict]:
    """Paper answer values ({qid: value}) from regions (sync: reads page images, writes crops
    as uploads). `regions[i]["page"]` indexes `page_urls`; `page_keys` (e.g. ScanPage ids) are
    copied into the value's region list. Questions without a live region get a blank
    "not found" answer pointing at the first page so the teacher can still see the paper.

    Value: {"image": crop url, "page": 1-based page of the first part, "blank": False,
            "mcq": None, "marks": None, "regions": [{"page", "page_key", "box"}],
            "region_confidence": 0.9, "found": "ai" | "teacher", **extra}
    """
    from .storage import read_url, save_file
    imgs: list[np.ndarray | None] = []
    for u in page_urls:
        data = read_url(u)
        imgs.append(decode(data) if data else None)
    keys = page_keys if page_keys is not None else list(range(len(page_urls)))
    out: dict[str, dict] = {}
    for q in questions:
        qid = q["id"]
        mine = sorted([r for r in regions if r.get("question_id") == qid and not r.get("crossed_out")
                       and 0 <= int(r.get("page", -1)) < len(page_urls)],
                      key=lambda r: (r["page"], r["box"][1]))
        crop = crop_answer(imgs, [(int(r["page"]), r["box"]) for r in mine]) if mine else None
        if crop is None:
            out[qid] = {"image": page_urls[0] if page_urls else None, "page": 1, "blank": True, "not_found": True,
                        "mcq": None, "marks": None, "regions": [], **(extra or {})}
            continue
        out[qid] = {
            "image": save_file(png(crop), "png"), "page": int(mine[0]["page"]) + 1, "blank": False,
            "mcq": None, "marks": None,
            "regions": [{"page": int(r["page"]) + 1, "page_key": keys[int(r["page"])], "box": r["box"]} for r in mine],
            "region_confidence": min(float(r.get("confidence", 0)) for r in mine),
            "found": "teacher" if all(r.get("source") == "teacher" for r in mine) else "ai",
            **(extra or {}),
        }
    return out


async def find_answers(exam_questions: list[dict], page_urls: list[str],
                       extra: dict | None = None) -> tuple[dict, dict[str, dict]]:
    """One call for callers that already stored the pages as uploads (e.g. homework photos a
    student sends): segment, then crop. Returns (segment_pages result, {qid: paper answer
    value}) — the answer values are ready for Attempt.answers / routes_scan.merge_pages and
    the AI grading (draft_answers). `extra` is copied into every answer value. Never raises."""
    from .storage import read_url
    imgs = await asyncio.to_thread(lambda: [read_url(u) or b"" for u in page_urls])
    seg = await segment_pages(exam_questions, imgs)
    answers = await asyncio.to_thread(region_answers, exam_questions, seg["regions"], page_urls, None, extra)
    for v in answers.values():
        if v.get("not_found"):
            v["whole_page"] = True
    return seg, answers
