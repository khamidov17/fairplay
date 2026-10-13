"""Reading scans (synchronous, CPU-bound: always call through asyncio.to_thread).

Steps per page: render (PyMuPDF, ~200 dpi) -> find the QR codes (OpenCV, several
strategies and rotations) -> homography from the QR corners to the printed layout ->
warp to a canonical upright A4 image -> crop every answer box -> read MCQ bubbles by
darkness and flag empty text boxes.

Accuracy notes (see the report in the PR): QR corners give a projective transform, so
flatbed/copier scans and fairly flat phone photos line up within a few pixels; curled
pages or strong lens distortion are not corrected. Bubble reading uses relative darkness;
faint ticks/crosses come back as "unclear" and go to the teacher.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

# Audit L-03: OpenCV's own decoder limit (read at decode time). Belt and braces: the
# header check in load_page refuses huge images before any decoding.
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(2 ** 28))

import cv2
import numpy as np
import pymupdf

from . import print as layout_mod
from .storage import save_file

DPI = 200
S = DPI / 72.0  # pixels per PDF point in the canonical image
CANON_W = int(round(layout_mod.PAGE_W * S))
CANON_H = int(round(layout_mod.PAGE_H * S))
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
MAX_IMAGE_PIXELS = 60_000_000
# Audit L-03: a 1 MB PNG can claim 30 000 x 30 000 pixels (2.7 GB decoded).
MAX_SOURCE_PIXELS = 250_000_000     # bigger than any real scan; refused from the header
MAX_PDF_PAGES = 2000                # per file

# bubble thresholds (darkness = mean(255-gray)/255 inside the circle)
FILLED = 0.32      # a properly filled bubble is well above this
FAINT = 0.17       # a tick/cross/light mark
EMPTY_INK = 40      # dark pixels (at 200 dpi) below which a text box counts as blank
MIN_BLOB = 12       # ignore specks smaller than this (dust, scanner noise)


# ---------------------------------------------------------------- sources
def is_pdf(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(5) == b"%PDF-"


class SourceError(ValueError):
    """A file that can't be used as a scan (shown to the teacher)."""


def source_page_count(path: str) -> int:
    """Pages in a PDF (capped) or 1 for an image. Raises SourceError for unreadable,
    encrypted or oversized files (audit L-03, L-05)."""
    if is_pdf(path):
        try:
            with pymupdf.open(path) as doc:
                if doc.needs_pass:
                    raise SourceError("the PDF is password-protected")
                n = doc.page_count
        except SourceError:
            raise
        except Exception as e:  # truncated / broken PDF
            raise SourceError(f"the PDF could not be read ({e.__class__.__name__})") from e
        if n > MAX_PDF_PAGES:
            raise SourceError(f"the PDF has {n} pages; at most {MAX_PDF_PAGES} per file")
        return n
    image_size(path)
    return 1


def image_size(path: str) -> tuple[int, int]:
    """Width and height from the file header only (no pixel decoding)."""
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Image.DecompressionBombError as e:      # Pillow's own limit (~179 MP) tripped
        raise SourceError("the image is too large to be a scan") from e
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise SourceError("the image could not be read") from e
    if w <= 0 or h <= 0 or w * h > MAX_SOURCE_PIXELS:
        raise SourceError(f"the image is {w} x {h} pixels; too large to be a scan")
    return w, h


def load_page(path: str, index: int, dpi: int = DPI) -> np.ndarray:
    """BGR image of page `index` of a PDF, or the image file itself."""
    if is_pdf(path):
        with pymupdf.open(path) as doc:
            page = doc[index]
            # cap pixels for huge pages
            zoom = dpi / 72.0
            area = page.rect.width * page.rect.height * zoom * zoom
            if area > MAX_IMAGE_PIXELS:
                zoom *= math.sqrt(MAX_IMAGE_PIXELS / area)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    w, h = image_size(path)               # audit L-03: size from the header first
    flag = cv2.IMREAD_COLOR
    if w * h > MAX_IMAGE_PIXELS:              # decode straight to the smallest needed size
        flag = cv2.IMREAD_REDUCED_COLOR_8
        for f, reduced in ((2, cv2.IMREAD_REDUCED_COLOR_2), (4, cv2.IMREAD_REDUCED_COLOR_4)):
            if w * h / (f * f) <= MAX_IMAGE_PIXELS:
                flag = reduced
                break
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, flag)
    if img is None:
        raise ValueError("not a readable image")
    h, w = img.shape[:2]
    if h * w > MAX_IMAGE_PIXELS:
        f = math.sqrt(MAX_IMAGE_PIXELS / (h * w))
        img = cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    return img


# ---------------------------------------------------------------- QR detection
@dataclass
class QR:
    text: str
    pts: np.ndarray  # 4x2 float32, symbol corners TL, TR, BR, BL in the code's own orientation


def _unrotate(pts: np.ndarray, code: int | None, w: int, h: int) -> np.ndarray:
    """Map points found in a cv2.rotate()d image back to the original (w x h) image."""
    p = pts.copy()
    if code is None:
        return p
    x, y = pts[:, 0].copy(), pts[:, 1].copy()
    if code == cv2.ROTATE_90_CLOCKWISE:        # (x,y) -> (h-1-y, x)
        p[:, 0], p[:, 1] = y, h - 1 - x
    elif code == cv2.ROTATE_180:
        p[:, 0], p[:, 1] = w - 1 - x, h - 1 - y
    elif code == cv2.ROTATE_90_COUNTERCLOCKWISE:  # (x,y) -> (y, w-1-x)
        p[:, 0], p[:, 1] = w - 1 - y, x
    return p


def _detect_multi(gray: np.ndarray) -> list[QR]:
    out: list[QR] = []
    detectors = [cv2.QRCodeDetector()]
    if hasattr(cv2, "QRCodeDetectorAruco"):
        detectors.append(cv2.QRCodeDetectorAruco())
    for det in detectors:
        try:
            ok, texts, pts, _ = det.detectAndDecodeMulti(gray)
        except cv2.error:
            continue
        if ok and pts is not None:
            for t, p in zip(texts, pts):
                if t:
                    out.append(QR(t, np.asarray(p, dtype=np.float32).reshape(4, 2)))
        if len(out) >= 2:
            break
    return out


def _detect_single(gray: np.ndarray) -> QR | None:
    for det in (cv2.QRCodeDetector(),):
        try:
            t, p, _ = det.detectAndDecode(gray)
        except cv2.error:
            continue
        if t and p is not None:
            return QR(t, np.asarray(p, dtype=np.float32).reshape(4, 2))
    return None


def _dedupe(qrs: list[QR]) -> list[QR]:
    out: list[QR] = []
    for q in qrs:
        c = q.pts.mean(axis=0)
        if all(np.linalg.norm(c - o.pts.mean(axis=0)) > 20 for o in out):
            out.append(q)
    return out


def find_qrs(img: np.ndarray) -> list[QR]:
    """All FairPlay QR codes on a page, with corner points in `img` coordinates."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = gray.shape[:2]
    found = _dedupe([q for q in _detect_multi(gray) if q.text.startswith("FP1|")])
    if len(found) >= 2:
        return found
    # Corner crops (each QR alone is easier to find), at 4 rotations, plus a binarised try.
    cw, ch = int(w * 0.4), int(h * 0.3)
    regions = [(0, 0), (w - cw, 0), (0, h - ch), (w - cw, h - ch)]
    for rx, ry in regions:
        crop = gray[ry:ry + ch, rx:rx + cw]
        variants = [crop, cv2.adaptiveThreshold(crop, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                cv2.THRESH_BINARY, 51, 10)]
        hit = None
        for v in variants:
            for code in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
                rv = v if code is None else cv2.rotate(v, code)
                q = _detect_single(rv)
                if q and q.text.startswith("FP1|"):
                    pts = _unrotate(q.pts, code, v.shape[1], v.shape[0])
                    hit = QR(q.text, pts + np.array([rx, ry], dtype=np.float32))
                    break
            if hit:
                break
        if hit:
            found = _dedupe(found + [hit])
        if len(found) >= 2:
            break
    return found


# ---------------------------------------------------------------- page transform
def _layout_qr_pts(corner: str) -> np.ndarray:
    x, y = layout_mod.QR_TR if corner == "tr" else layout_mod.QR_BL
    q = layout_mod.QR_SIZE
    return np.array([[x, y], [x + q, y], [x + q, y + q], [x, y + q]], dtype=np.float32) * S


def _page_fit_score(H: np.ndarray, w: int, h: int) -> float:
    """How well the predicted page outline (canonical -> image) sits inside the image."""
    inv = np.linalg.inv(H)
    corners = np.array([[[0, 0]], [[CANON_W, 0]], [[CANON_W, CANON_H]], [[0, CANON_H]]], dtype=np.float32)
    proj = cv2.perspectiveTransform(corners, inv).reshape(4, 2)
    tol_x, tol_y = 0.08 * w, 0.08 * h
    inside = sum(1 for x, y in proj if -tol_x <= x <= w + tol_x and -tol_y <= y <= h + tol_y)
    return inside


def page_transform(qrs: list[QR], w: int, h: int) -> tuple[np.ndarray | None, int]:
    """Homography image -> canonical page, and the rotation (0/90/180/270) of the scan."""
    if not qrs:
        return None, 0
    if len(qrs) >= 2:
        a, b = qrs[0], qrs[1]
        # In A's own frame, the other code (BL) lies to the left and below when A is TR.
        ax = a.pts[1] - a.pts[0]
        ay = a.pts[3] - a.pts[0]
        d = b.pts.mean(axis=0) - a.pts.mean(axis=0)
        a_is_tr = float(np.dot(d, ax)) < 0 and float(np.dot(d, ay)) > 0
        tr, bl = (a, b) if a_is_tr else (b, a)
        src = np.vstack([tr.pts, bl.pts]).astype(np.float32)
        dst = np.vstack([_layout_qr_pts("tr"), _layout_qr_pts("bl")]).astype(np.float32)
        H, _ = cv2.findHomography(src, dst, 0)
    else:
        q = qrs[0]
        best = None
        for corner in ("tr", "bl"):
            Hc = cv2.getPerspectiveTransform(q.pts.astype(np.float32), _layout_qr_pts(corner))
            score = _page_fit_score(Hc, w, h)
            if best is None or score > best[0]:
                best = (score, Hc)
        H = best[1]
    if H is None:
        return None, 0
    v = qrs[0].pts[1] - qrs[0].pts[0]
    angle = math.degrees(math.atan2(float(v[1]), float(v[0])))
    rotation = int(round(angle / 90.0)) % 4 * 90
    return H, rotation


def warp(img: np.ndarray, H: np.ndarray | None) -> np.ndarray:
    """Canonical upright page (gray). Without a transform the image is just scaled to A4."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    if H is None:
        if gray.shape[1] > gray.shape[0] * 1.15:  # landscape photo of a portrait page
            gray = cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
        return cv2.resize(gray, (CANON_W, CANON_H), interpolation=cv2.INTER_AREA)
    return cv2.warpPerspective(gray, H, (CANON_W, CANON_H), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=255)


# ---------------------------------------------------------------- reading boxes
def _px(rect: list[float], pad: float = 0.0) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = rect
    return (max(0, int((x0 - pad) * S)), max(0, int((y0 - pad) * S)),
            min(CANON_W, int((x1 + pad) * S)), min(CANON_H, int((y1 + pad) * S)))


def _darkness_disk(gray: np.ndarray, cx: float, cy: float, r: float) -> float:
    x0, y0 = int(cx - r - 1), int(cy - r - 1)
    x1, y1 = int(cx + r + 2), int(cy + r + 2)
    if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0]:
        return 0.0
    patch = gray[y0:y1, x0:x1].astype(np.float32)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    if not mask.any():
        return 0.0
    return float((255.0 - patch[mask]).mean() / 255.0)


def _ring_darkness(gray: np.ndarray, cx: float, cy: float, r: float) -> float:
    n = 48
    vals = []
    for i in range(n):
        a = 2 * math.pi * i / n
        x, y = int(round(cx + r * math.cos(a))), int(round(cy + r * math.sin(a)))
        if 0 <= x < gray.shape[1] and 0 <= y < gray.shape[0]:
            vals.append(255 - int(gray[y, x]))
    return sum(vals) / (len(vals) * 255.0) if vals else 0.0


def _refine_offset(gray: np.ndarray, bubbles: list[list[float]]) -> tuple[float, float]:
    """Small shift (pixels) that best aligns the printed bubble outlines (corrects a few px
    of residual misregistration from paper stretch or QR corner noise)."""
    best = (0.0, 0.0, -1.0)
    span = int(3 * S)
    for dx in range(-span, span + 1, 2):
        for dy in range(-span, span + 1, 2):
            score = sum(_ring_darkness(gray, b[0] * S + dx, b[1] * S + dy, b[2] * S) for b in bubbles)
            if score > best[2]:
                best = (dx, dy, score)
    return best[0], best[1]


def read_bubbles(gray: np.ndarray, bubbles: list[list[float]]) -> dict:
    """{"mcq": idx|None, "marks": one|none|multiple|unclear, "fill": [...], "confidence"}."""
    dx, dy = _refine_offset(gray, bubbles)
    fills = [round(_darkness_disk(gray, b[0] * S + dx, b[1] * S + dy, b[2] * S * 0.62), 3) for b in bubbles]
    order = sorted(range(len(fills)), key=lambda i: fills[i], reverse=True)
    top = fills[order[0]]
    second = fills[order[1]] if len(order) > 1 else 0.0
    base = float(np.median(fills))
    strong = [i for i in range(len(fills)) if fills[i] >= FILLED and fills[i] >= top * 0.6]
    if len(strong) >= 2:
        return {"mcq": None, "marks": "multiple", "fill": fills, "confidence": 0.9}
    if len(strong) == 1:
        conf = min(0.99, 0.6 + (top - max(second, base)) * 1.2)
        if second >= FAINT and second > top * 0.45:
            return {"mcq": None, "marks": "unclear", "fill": fills, "confidence": round(conf, 2)}
        return {"mcq": strong[0], "marks": "one", "fill": fills, "confidence": round(conf, 2)}
    if top >= FAINT and top - base >= 0.08:
        return {"mcq": order[0], "marks": "unclear", "fill": fills, "confidence": 0.5}
    return {"mcq": None, "marks": "none", "fill": fills, "confidence": 0.9}


def ink_pixels(gray: np.ndarray, rect: list[float]) -> int:
    """Dark pixels inside a box (border excluded), counting only blobs bigger than specks.
    An absolute count (not a ratio) so a single digit in a big work box is not "blank"."""
    x0, y0, x1, y1 = _px(rect)
    inset = int(4 * S)  # stay clear of the printed border
    region = gray[y0 + inset:y1 - inset, x0 + inset:x1 - inset]
    if region.size == 0:
        return 0
    region = cv2.medianBlur(region, 3)
    mask = (region < 140).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA] if n > 1 else np.zeros(0, dtype=np.int32)
    return int(areas[areas >= MIN_BLOB].sum())


def _png(gray: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", gray, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    return buf.tobytes()


def _jpg(gray: np.ndarray, width: int | None = None, quality: int = 80) -> bytes:
    if width and gray.shape[1] > width:
        f = width / gray.shape[1]
        gray = cv2.resize(gray, (width, int(gray.shape[0] * f)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", gray, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes()


def crop_boxes(canon: np.ndarray, page_layout: dict, page_no: int) -> dict:
    """{qid: {"image","page","blank","mcq","marks","fill","confidence"}} for one page."""
    out: dict = {}
    for box in page_layout.get("boxes", []):
        x0, y0, x1, y1 = _px(box["rect"], pad=6)
        crop = canon[y0:y1, x0:x1]
        entry: dict = {"image": save_file(_png(crop), "png"), "page": page_no}
        if box.get("type") == "mcq" and box.get("bubbles"):
            entry.update(read_bubbles(canon, box["bubbles"]))
            entry["blank"] = entry["marks"] == "none"
        else:
            ink = ink_pixels(canon, box["rect"])
            entry["ink"] = ink
            entry["blank"] = ink < EMPTY_INK
        out[box["qid"]] = entry
    return out


# ---------------------------------------------------------------- one page
def analyze_page(img: np.ndarray, exam_id: str, layout: dict) -> dict:
    """Everything the server stores for one scanned page (files are written here)."""
    h, w = img.shape[:2]
    qrs = find_qrs(img)
    parsed = [(q, layout_mod.parse_qr(q.text)) for q in qrs]
    ours = [(q, p) for q, p in parsed if p is not None]
    result: dict = {"status": "unmatched", "reason": "", "student_id": None, "page_no": None,
                    "qr_found": len(ours), "rotation": 0, "crops": {}, "name_crop": None,
                    "source_image": None}
    H, rotation = page_transform([q for q, _ in ours], w, h)
    canon = warp(img, H)
    result["rotation"] = rotation
    result["preview"] = save_file(_jpg(canon if H is not None else _gray(img), 1000), "jpg")
    if not ours:
        result["reason"] = "No FairPlay code found on this page"
        result["source_image"] = save_file(_jpg(_gray(img), 2000, 85), "jpg")
        return result
    exam, sid, page_no = ours[0][1]
    if exam != exam_id:
        result["reason"] = "This page belongs to a different exam"
        return result
    pages = {p["page_no"]: p for p in layout.get("pages", [])}
    if page_no not in pages:
        result["reason"] = f"Page {page_no} is not in the printed layout (reprinted?)"
        result["source_image"] = save_file(_jpg(canon, None, 85), "jpg")
        return result
    result["page_no"] = page_no
    if page_no == 1:
        x0, y0, x1, y1 = _px(layout.get("name_box") or list(layout_mod.NAME_BOX), pad=8)
        result["name_crop"] = save_file(_png(canon[y0:y1, x0:x1]), "png")
    result["crops"] = crop_boxes(canon, pages[page_no], page_no)
    if sid == 0:
        result["reason"] = "Blank copy — choose the student"
        result["source_image"] = save_file(_jpg(canon, None, 85), "jpg")
        return result
    result["status"] = "matched"
    result["student_id"] = sid
    return result


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def process_source_page(path: str, index: int, exam_id: str, layout: dict) -> dict:
    try:
        img = load_page(path, index)
    except Exception as e:  # unreadable page/image
        return {"status": "unmatched", "reason": f"Could not read this page ({e.__class__.__name__})",
                "student_id": None, "page_no": None, "qr_found": 0, "rotation": 0, "crops": {},
                "name_crop": None, "source_image": None, "preview": None}
    return analyze_page(img, exam_id, layout)


def crops_for_assignment(source_url: str, layout: dict, page_no: int) -> dict:
    """Re-crop a stored canonical/unmatched page image after the teacher picks the page."""
    from .storage import path_for_url
    p = path_for_url(source_url)
    if p is None:
        return {}
    img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return {}
    if img.shape[:2] != (CANON_H, CANON_W):
        img = warp(img, None)
    pages = {pg["page_no"]: pg for pg in layout.get("pages", [])}
    if page_no not in pages:
        return {}
    return crop_boxes(img, pages[page_no], page_no)


def safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
