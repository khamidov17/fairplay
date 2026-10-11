"""Phone photos of answer sheets: find the page, flatten it, raise the contrast.

Synchronous OpenCV code (call through asyncio.to_thread / the scan pipeline thread).

rectify(img) looks for the sheet as the largest bright quadrilateral in the photo
(Otsu threshold + contours, Canny as a second try), warps it to an upright rectangle with
the page's own aspect ratio (A4 portrait when close), and evens out lighting (shadow
removal + contrast stretch). When
no page outline is found (e.g. a flatbed scan that is already just the page) it returns
None and the caller uses the image as is. FairPlay booklets are then matched by their QR
codes on the flattened page, which is easier than on the raw photo.

HEIC (iPhone) photos are decoded with pillow-heif when it is installed; otherwise uploads
of HEIC files are refused with a clear message.
"""
from __future__ import annotations

import io

import cv2
import numpy as np

A4_RATIO = 297.0 / 210.0
MIN_AREA = 0.18      # the page must cover at least this share of the photo
MAX_AREA = 0.97      # ... and less than this (else it is already a clean scan)
WORK = 1000          # long side of the working copy used for detection


def heic_supported() -> bool:
    try:
        import pillow_heif  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def is_heic(head: bytes) -> bool:
    return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (
        b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis")


def decode_heic(data: bytes) -> np.ndarray:
    """BGR image from HEIC bytes (requires pillow-heif)."""
    import pillow_heif
    from PIL import Image
    pillow_heif.register_heif_opener()
    with Image.open(io.BytesIO(data)) as im:
        rgb = np.array(im.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def order_corners(pts: np.ndarray) -> np.ndarray:
    """TL, TR, BR, BL."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).reshape(4)
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def _quad_from_mask(mask: np.ndarray, area_img: float) -> np.ndarray | None:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        area = cv2.contourArea(c)
        if area < MIN_AREA * area_img:
            break
        peri = cv2.arcLength(c, True)
        for eps in (0.015, 0.02, 0.03, 0.05):
            approx = cv2.approxPolyDP(c, eps * peri, True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                return approx.reshape(4, 2).astype(np.float32)
        # rounded / slightly curled corners: fall back to the minimum-area rectangle
        if area > 0.85 * cv2.contourArea(cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)):
            return cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
    return None


def find_page_quad(img: np.ndarray) -> np.ndarray | None:
    """Corners (TL, TR, BR, BL) of the sheet in `img` coordinates, or None."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = gray.shape[:2]
    f = WORK / max(h, w) if max(h, w) > WORK else 1.0
    small = cv2.resize(gray, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA) if f < 1 else gray
    area_img = float(small.shape[0] * small.shape[1])
    blur = cv2.GaussianBlur(small, (5, 5), 0)
    # 1) bright paper on a darker background
    _, otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    otsu = cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    quad = _quad_from_mask(otsu, area_img)
    if quad is None:
        # 2) edges (paper on a light desk)
        edges = cv2.Canny(blur, 40, 120)
        edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
        quad = _quad_from_mask(edges, area_img)
    if quad is None:
        return None
    quad = order_corners(quad) / f
    area = cv2.contourArea(quad.astype(np.float32))
    if area > MAX_AREA * w * h:
        return None
    return quad


def rectify(img: np.ndarray, quad: np.ndarray | None = None) -> np.ndarray | None:
    """Flattened, contrast-enhanced page (BGR) or None when no page outline was found."""
    if quad is None:
        quad = find_page_quad(img)
    if quad is None:
        return None
    tl, tr, br, bl = quad
    width = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    height = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    if width < 200 or height < 200:
        return None
    ratio = height / width
    portrait = ratio >= 1
    r = ratio if portrait else 1 / ratio
    if abs(r - A4_RATIO) < 0.12:  # snap to A4 so the printed layout lines up
        r = A4_RATIO
    long_side = max(width, height)
    out_w, out_h = (int(long_side / r), int(long_side)) if portrait else (int(long_side), int(long_side / r))
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
    warped = cv2.warpPerspective(img, M, (out_w, out_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return enhance(warped)


def enhance(img: np.ndarray) -> np.ndarray:
    """Even out lighting and raise contrast (gray, returned as 3-channel BGR)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    # divide by a heavily blurred copy = remove shadows / vignetting
    bg = cv2.GaussianBlur(gray, (0, 0), sigmaX=max(gray.shape) / 40)
    norm = cv2.divide(gray, bg, scale=240)
    # stretch so the darkest ink is near black (robust percentiles, no noise amplification)
    lo, hi = np.percentile(norm, 0.5), np.percentile(norm, 99.5)
    if hi - lo > 20:
        norm = np.clip((norm.astype(np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
    return cv2.cvtColor(norm, cv2.COLOR_GRAY2BGR)
