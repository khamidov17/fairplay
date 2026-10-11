"""Printable answer booklets (A4 PDF via PyMuPDF) and their layout.

Every page carries the QR code `FP1|<exam_id>|<student_id>|<page_no>` twice (top-right and
bottom-left) so a scan can be identified and deskewed even if one corner is damaged.
All coordinates in the layout are PDF points with the origin at the TOP-LEFT of the page.
The geometry is identical for every student (and for the blank copy), so one layout per
exam is stored and used to crop every scan.
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field

import pymupdf
import qrcode
from qrcode.constants import ERROR_CORRECT_M

from .storage import resolve_image

PAGE_W, PAGE_H = 595.28, 841.89  # A4
MARGIN = 36.0
QR_SIZE = 62.0           # printed size of the QR symbol (no quiet zone; the page is white)
QR_TOP = 24.0
CONTENT_TOP = 108.0
CONTENT_BOTTOM = PAGE_H - QR_TOP - QR_SIZE - 16.0
INDENT = 22.0
BODY = 10.5
LEADING = 14.0
LINE_H = 22.0            # one ruled line of essay space
DEFAULT_LINES = {"essay": 12, "numeric": 1, "short": 1}
BUBBLE_R = 8.0
BUBBLE_STEP = 36.0
LETTERS = "ABCDEFGH"
LAYOUT_VERSION = 1

QR_TR = (PAGE_W - MARGIN - QR_SIZE, QR_TOP)
QR_BL = (MARGIN, PAGE_H - QR_TOP - QR_SIZE)
NAME_BOX = (MARGIN + 44.0, 46.0, MARGIN + 300.0, 70.0)

_HELV = pymupdf.Font("helv")
_HEBO = pymupdf.Font("hebo")
_UNI: pymupdf.Font | None = None


def _uni() -> pymupdf.Font:
    global _UNI
    if _UNI is None:
        _UNI = pymupdf.Font("cjk")  # Droid Sans Fallback: Latin, Cyrillic, CJK
    return _UNI


def qr_payload(exam_id: str, student_id: int, page_no: int) -> str:
    return f"FP1|{exam_id}|{student_id}|{page_no}"


def parse_qr(text: str) -> tuple[str, int, int] | None:
    parts = (text or "").strip().split("|")
    if len(parts) != 4 or parts[0] != "FP1":
        return None
    try:
        sid, page = int(parts[2]), int(parts[3])
    except ValueError:
        return None
    if sid < 0 or page < 1:
        return None
    return parts[1], sid, page


def questions_hash(questions: list[dict]) -> str:
    keep = [{k: q.get(k) for k in ("id", "type", "prompt", "options", "images", "answer_lines")}
            for q in questions]
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def _latin(text: str) -> bool:
    return all(ord(c) < 256 for c in text)


def _font(text: str, bold: bool = False) -> tuple[str, pymupdf.Font]:
    if _latin(text):
        return ("hebo", _HEBO) if bold else ("helv", _HELV)
    return "uni", _uni()


def _wrap(text: str, width: float, size: float, bold: bool = False) -> list[str]:
    """Greedy word wrap (characters for scripts without spaces)."""
    _, font = _font(text, bold)
    lines: list[str] = []
    for para in (text or "").replace("\r", "").split("\n"):
        cur = ""
        tokens = para.split(" ") if " " in para or _latin(para) else list(para)
        sep = " " if " " in para or _latin(para) else ""
        for tok in tokens:
            cand = (cur + sep + tok) if cur else tok
            if font.text_length(cand, size) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
            # a single token longer than the line: hard-break it
            while font.text_length(tok, size) > width and len(tok) > 1:
                cut = len(tok)
                while cut > 1 and font.text_length(tok[:cut], size) > width:
                    cut -= 1
                lines.append(tok[:cut])
                tok = tok[cut:]
            cur = tok
        lines.append(cur)
    return lines


@dataclass
class Draw:
    """One drawing operation, recorded during layout and replayed per student."""
    kind: str
    args: dict = field(default_factory=dict)


@dataclass
class PlannedPage:
    ops: list[Draw] = field(default_factory=list)
    boxes: list[dict] = field(default_factory=list)


def _qr_png(text: str) -> bytes:
    q = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=0)
    q.add_data(text)
    q.make(fit=True)
    img = q.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _image_size(data: bytes) -> tuple[int, int] | None:
    try:
        pix = pymupdf.Pixmap(data)
        return pix.width, pix.height
    except Exception:
        return None


def plan(questions: list[dict]) -> list[PlannedPage]:
    """Lay out all questions over pages; returns draw ops + answer boxes per page."""
    pages: list[PlannedPage] = [PlannedPage()]
    y = CONTENT_TOP
    width = PAGE_W - 2 * MARGIN - INDENT

    def new_page() -> None:
        nonlocal y
        pages.append(PlannedPage())
        y = CONTENT_TOP

    def room() -> float:
        return CONTENT_BOTTOM - y

    for n, q in enumerate(questions, start=1):
        qtype = q.get("type")
        if qtype not in ("mcq", "numeric", "short", "essay"):
            continue
        # ---- prompt part
        prompt_lines = _wrap(q.get("prompt", ""), width, BODY)
        images = []
        for im in (q.get("images") or [])[:6]:
            data = resolve_image(im.get("url", "")) if isinstance(im, dict) else None
            size = _image_size(data) if data else None
            if data and size:
                w, h = size
                scale = min(width / w, 150.0 / h, 1.0 if w > 300 else 2.0)
                images.append((data, w * scale, h * scale))
            else:
                images.append((None, width, 14.0))  # placeholder line "[picture]"
        option_lines: list[tuple[str, list[str]]] = []
        if qtype == "mcq":
            for i, opt in enumerate((q.get("options") or [])[:8]):
                option_lines.append((LETTERS[i], _wrap(str(opt), width - 20, BODY)))
        head_h = LEADING + 2
        prompt_h = head_h + len(prompt_lines) * LEADING + sum(h + 6 for _, _, h in images) + \
            sum(len(ls) * LEADING for _, ls in option_lines) + 6

        # ---- answer box part
        lines = q.get("answer_lines")
        if qtype == "mcq":
            box_h = 2 * BUBBLE_R + 18
        else:
            if not isinstance(lines, int) or lines < 1:
                lines = DEFAULT_LINES[qtype]
            box_h = 36.0 if lines == 1 else lines * LINE_H + 8
        box_h = min(box_h, CONTENT_BOTTOM - CONTENT_TOP - head_h - 8)
        total = prompt_h + box_h + 18

        if total > room() and y > CONTENT_TOP:
            new_page()
        page = pages[-1]
        # header line: number + points
        page.ops.append(Draw("text", {"x": MARGIN, "y": y + 11, "text": f"{n}.", "size": 11, "bold": True}))
        pts = q.get("points", 1)
        pts_text = f"{pts} point" + ("" if pts == 1 else "s")
        page.ops.append(Draw("text", {"x": PAGE_W - MARGIN - _HELV.text_length(pts_text, 8.5),
                                      "y": y + 11, "text": pts_text, "size": 8.5, "grey": True}))
        cy = y
        first = True
        for ln in prompt_lines:
            if not first and cy + LEADING > CONTENT_BOTTOM:
                new_page()
                page = pages[-1]
                cy = y
            page.ops.append(Draw("text", {"x": MARGIN + INDENT, "y": cy + 11, "text": ln, "size": BODY}))
            cy += LEADING
            first = False
        cy += 2
        for data, w, h in images:
            if cy + h > CONTENT_BOTTOM:
                new_page()
                page = pages[-1]
                cy = y
            if data:
                page.ops.append(Draw("image", {"rect": (MARGIN + INDENT, cy, MARGIN + INDENT + w, cy + h),
                                               "data": data}))
            else:
                page.ops.append(Draw("text", {"x": MARGIN + INDENT, "y": cy + 10,
                                              "text": "[picture not available]", "size": 8.5, "grey": True}))
            cy += h + 6
        for letter, ls in option_lines:
            for j, ln in enumerate(ls):
                if cy + LEADING > CONTENT_BOTTOM:
                    new_page()
                    page = pages[-1]
                    cy = y
                if j == 0:
                    page.ops.append(Draw("text", {"x": MARGIN + INDENT, "y": cy + 11, "text": letter,
                                                  "size": BODY, "bold": True}))
                page.ops.append(Draw("text", {"x": MARGIN + INDENT + 20, "y": cy + 11, "text": ln,
                                              "size": BODY}))
                cy += LEADING
        cy += 6
        if cy + box_h + 4 > CONTENT_BOTTOM:
            new_page()
            page = pages[-1]
            cy = y
            page.ops.append(Draw("text", {"x": MARGIN, "y": cy + 11, "text": f"{n}. (answer)", "size": 9,
                                          "bold": True}))
            cy += LEADING + 2
        x0, x1 = MARGIN + INDENT, PAGE_W - MARGIN
        rect = (x0, cy, x1, cy + box_h)
        box: dict = {"qid": q["id"], "number": n, "type": qtype, "rect": [round(v, 2) for v in rect]}
        page.ops.append(Draw("box", {"rect": rect}))
        if qtype == "mcq":
            n_opt = max(2, min(8, len(q.get("options") or [])))
            page.ops.append(Draw("text", {"x": x0 + 8, "y": cy + box_h / 2 + 3, "text": "Fill one circle:",
                                          "size": 8.5, "grey": True}))
            bubbles = []
            bx = x0 + 100
            for i in range(n_opt):
                cx, cyy = bx + i * BUBBLE_STEP, cy + box_h / 2
                bubbles.append([round(cx, 2), round(cyy, 2), BUBBLE_R])
                page.ops.append(Draw("bubble", {"c": (cx, cyy), "r": BUBBLE_R, "letter": LETTERS[i]}))
            box["bubbles"] = bubbles
        elif box_h > 40:
            ly = cy + LINE_H
            while ly < cy + box_h - 6:
                page.ops.append(Draw("rule", {"x0": x0 + 6, "x1": x1 - 6, "y": ly}))
                ly += LINE_H
        page.boxes.append(box)
        y = cy + box_h + 18
    return pages


def build_layout(exam_id: str, questions: list[dict]) -> tuple[dict, list[PlannedPage]]:
    pages = plan(questions)
    layout = {
        "version": LAYOUT_VERSION, "exam_id": exam_id, "page_size": [PAGE_W, PAGE_H],
        "page_count": len(pages), "qr_size": QR_SIZE,
        "qr": {"tr": [QR_TR[0], QR_TR[1]], "bl": [QR_BL[0], QR_BL[1]]},
        "name_box": list(NAME_BOX),
        "pages": [{"page_no": i + 1, "boxes": p.boxes} for i, p in enumerate(pages)],
        "questions_hash": questions_hash(questions),
    }
    return layout, pages


def _text(page: pymupdf.Page, x: float, y: float, text: str, size: float, bold: bool = False,
          grey: bool = False, used: set | None = None) -> None:
    name, _ = _font(text, bold)
    if name == "uni" and used is not None and "uni" not in used:
        page.insert_font(fontname="uni", fontbuffer=_uni().buffer)
        used.add("uni")
    page.insert_text((x, y), text, fontsize=size, fontname=name,
                     color=(0.42, 0.46, 0.52) if grey else (0.1, 0.14, 0.2))


def render(exam: dict, pages: list[PlannedPage], students: list[dict], blank: bool = False) -> bytes:
    """students: [{"id", "name", "username"}]; blank -> one copy with student id 0."""
    doc = pymupdf.open()
    copies = [{"id": 0, "name": "", "username": ""}] if blank else students
    total = len(pages)
    title = exam.get("title", "")
    course = exam.get("course", "")
    qr_cache: dict[str, bytes] = {}
    for st in copies:
        for i, planned in enumerate(pages, start=1):
            page = doc.new_page(width=PAGE_W, height=PAGE_H)
            used: set = set()
            # QR codes
            payload = qr_payload(exam["id"], int(st["id"]), i)
            png = qr_cache.get(payload) or _qr_png(payload)
            qr_cache[payload] = png
            for (qx, qy) in (QR_TR, QR_BL):
                page.insert_image(pymupdf.Rect(qx, qy, qx + QR_SIZE, qy + QR_SIZE), stream=png)
            # header
            head_w = QR_TR[0] - MARGIN - 14
            t_lines = _wrap((f"{course} · " if course else "") + title, head_w, 12.5, bold=True)[:1]
            _text(page, MARGIN, QR_TOP + 12, t_lines[0] if t_lines else "", 12.5, bold=True, used=used)
            _text(page, MARGIN, NAME_BOX[1] + 15, "Name", 9, grey=True, used=used)
            if blank:
                page.draw_rect(pymupdf.Rect(*NAME_BOX), color=(0.2, 0.25, 0.32), width=0.9)
            else:
                _text(page, NAME_BOX[0] + 4, NAME_BOX[1] + 16, st["name"], 12, bold=True, used=used)
                page.draw_line((NAME_BOX[0], NAME_BOX[3]), (NAME_BOX[2], NAME_BOX[3]),
                               color=(0.75, 0.78, 0.82), width=0.6)
            who = "Blank copy — write your full name above" if blank else st.get("username", "")
            _text(page, MARGIN, 88, f"Page {i} of {total}" + (f"  ·  {who}" if who else ""), 9, grey=True,
                  used=used)
            page.draw_line((MARGIN, 98), (PAGE_W - MARGIN, 98), color=(0.85, 0.87, 0.9), width=0.6)
            # footer
            fx = QR_BL[0] + QR_SIZE + 12
            _text(page, fx, QR_BL[1] + 26, "FairPlay · Write only inside the boxes. Do not fold or staple over "
                  "the codes.", 8, grey=True, used=used)
            _text(page, fx, QR_BL[1] + 40, f"Page {i} of {total}", 8, grey=True, used=used)
            # content
            for op in planned.ops:
                a = op.args
                if op.kind == "text":
                    _text(page, a["x"], a["y"], a["text"], a["size"], a.get("bold", False),
                          a.get("grey", False), used=used)
                elif op.kind == "image":
                    try:
                        page.insert_image(pymupdf.Rect(*a["rect"]), stream=a["data"], keep_proportion=True)
                    except Exception:
                        pass
                elif op.kind == "box":
                    page.draw_rect(pymupdf.Rect(*a["rect"]), color=(0.2, 0.25, 0.32), width=1.1)
                elif op.kind == "rule":
                    page.draw_line((a["x0"], a["y"]), (a["x1"], a["y"]), color=(0.86, 0.88, 0.9), width=0.5)
                elif op.kind == "bubble":
                    cx, cy = a["c"]
                    page.draw_circle((cx, cy), a["r"], color=(0.2, 0.25, 0.32), width=0.9)
                    lw = _HELV.text_length(a["letter"], 7)
                    page.insert_text((cx - lw / 2, cy + 2.5), a["letter"], fontsize=7, fontname="helv",
                                     color=(0.6, 0.63, 0.68))
    try:
        doc.subset_fonts()
    except Exception:
        pass
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out
