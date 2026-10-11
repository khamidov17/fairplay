"""Where is each question answered on a student's own pages (no printed layout)?

Template-free answer finding lives in `app.scan.segment.segment_pages(questions, page_images)`
(built in parallel by another team). This adapter calls it when it exists and turns whatever
it returns into per-question image lists; when the module is missing, raises, or returns
nothing usable for a question, that question gets the WHOLE page set (every page, in order)
and the AI is told to grade only that question on the pages (`whole_page`).

Accepted shapes from segment_pages (defensive, any of):
  {qid: region | [region, ...]}  or  [{"question_id": qid, ...region}, ...]
  region = bytes (PNG/JPEG) | numpy image | int page index | {"image": bytes|ndarray} |
           {"page": index (0-based, or "page_no" 1-based), "bbox"|"box": [x0, y0, x1, y1]}
           (bbox in pixels, or fractions 0..1 of the page)

Synchronous (OpenCV): call through asyncio.to_thread.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

log = logging.getLogger("fairplay.homework.segment")


@dataclass
class Located:
    """Per question: answer images (encoded bytes) and the 1-based page of the first one.
    `whole_page` True = the fallback (all pages)."""
    images: list[bytes] = field(default_factory=list)
    page: int = 1
    whole_page: bool = True


def _segmenter():
    try:
        from ..scan.segment import segment_pages  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - module missing (or broken): fall back
        return None
    return segment_pages


def _encode(img: np.ndarray) -> bytes | None:
    if img is None or not isinstance(img, np.ndarray) or img.size == 0:
        return None
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


def _decode(data: bytes) -> np.ndarray | None:
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _region(r: Any, pages: list[bytes], decoded: dict[int, np.ndarray]) -> tuple[bytes, int] | None:
    """One region -> (image bytes, 1-based page) or None."""
    if isinstance(r, (bytes, bytearray)):
        return bytes(r), 1
    if isinstance(r, np.ndarray):
        b = _encode(r)
        return (b, 1) if b else None
    if isinstance(r, bool):
        return None
    if isinstance(r, int):
        return (pages[r], r + 1) if 0 <= r < len(pages) else None
    if isinstance(r, dict):
        page_idx = None
        if isinstance(r.get("page"), int) and not isinstance(r.get("page"), bool):
            page_idx = r["page"]
        elif isinstance(r.get("page_no"), int):
            page_idx = r["page_no"] - 1
        img = r.get("image")
        if isinstance(img, (bytes, bytearray, np.ndarray)):
            got = _region(img, pages, decoded)
            return (got[0], (page_idx or 0) + 1) if got else None
        if page_idx is None or not 0 <= page_idx < len(pages):
            return None
        box = r.get("bbox") or r.get("box")
        if not box:
            return pages[page_idx], page_idx + 1
        if page_idx not in decoded:
            decoded[page_idx] = _decode(pages[page_idx])
        im = decoded[page_idx]
        if im is None:
            return None
        h, w = im.shape[:2]
        try:
            x0, y0, x1, y1 = (float(v) for v in box[:4])
        except (TypeError, ValueError):
            return None
        if max(x0, y0, x1, y1) <= 1.0:
            x0, x1, y0, y1 = x0 * w, x1 * w, y0 * h, y1 * h
        x0, x1 = sorted((max(0, int(x0)), min(w, int(x1))))
        y0, y1 = sorted((max(0, int(y0)), min(h, int(y1))))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        b = _encode(im[y0:y1, x0:x1])
        return (b, page_idx + 1) if b else None
    return None


def _by_question(raw: Any) -> dict[str, list]:
    out: dict[str, list] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = list(v) if isinstance(v, (list, tuple)) else [v]
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict) and item.get("question_id") is not None:
                regs = item.get("regions")
                out.setdefault(str(item["question_id"]), []).extend(
                    list(regs) if isinstance(regs, (list, tuple)) else [item])
    return out


def locate(questions: list[dict], pages: list[bytes]) -> tuple[dict[str, Located], str]:
    """(answers per question id, mode) with mode "segment" | "partial" | "whole_page"."""
    whole = {q["id"]: Located(images=list(pages), page=1, whole_page=True) for q in questions}
    if not pages:
        return whole, "whole_page"
    fn = _segmenter()
    if fn is None:
        return whole, "whole_page"
    try:
        raw = fn(questions, pages)
        if hasattr(raw, "__await__"):  # app.scan.segment.segment_pages is async; we run in a worker thread
            import asyncio
            raw = asyncio.run(raw)
        if isinstance(raw, dict) and isinstance(raw.get("regions"), list):
            # segment_pages result: live (not crossed-out) regions of known questions
            raw = [r for r in raw["regions"]
                   if isinstance(r, dict) and r.get("question_id") and not r.get("crossed_out")]
    except Exception:  # noqa: BLE001 - never lose a submission over segmentation
        log.exception("segment_pages failed; grading on whole pages")
        return whole, "whole_page"
    regions = _by_question(raw)
    decoded: dict[int, np.ndarray] = {}
    found = 0
    out: dict[str, Located] = {}
    for q in questions:
        imgs: list[bytes] = []
        first = None
        for r in regions.get(q["id"], []):
            try:
                got = _region(r, pages, decoded)
            except Exception:  # noqa: BLE001
                got = None
            if got:
                imgs.append(got[0])
                first = first or got[1]
        if imgs:
            out[q["id"]] = Located(images=imgs[:6], page=first or 1, whole_page=False)
            found += 1
        else:
            out[q["id"]] = whole[q["id"]]
    mode = "segment" if found == len(questions) else "partial" if found else "whole_page"
    return out, mode
