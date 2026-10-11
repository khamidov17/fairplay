"""Cheap deterministic checks on a hand-drawn graph with OpenCV (graph / diagram questions).

`analyze(image_bytes)` finds, in normalised image coordinates (0..1 from the top-left):
  - the axes: the longest near-horizontal and near-vertical ink lines and their crossing,
  - tick marks along each axis (and whether their spacing is even),
  - filled dots (plotted data points): ink blobs thicker than the pen stroke.
`cross_check(spec, features, readings, cv)` compares the AI's checklist answers and the values it
read off the axes with those detections. The checks never award points: a disagreement only
lowers the AI's confidence and sends the answer to the teacher.
"""
from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np

from .diagram_spec import AXES_KEYS, POINT_KEYS, SCALE_KEYS

MAX_SIDE = 1400


def _decode(data: bytes | None) -> np.ndarray | None:
    if not data:
        return None
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None or img.size == 0:
        return None
    h, w = img.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return img


def _ink(gray: np.ndarray) -> np.ndarray:
    """Binary mask of ink (255) — dark strokes on light paper, robust to uneven lighting."""
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    h, w = gray.shape
    block = max(15, (min(h, w) // 20) | 1)
    ada = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 12)
    # dark enough relative to the paper: faint printed ruling (answer-box lines) is not ink
    paper = max(1.0, float(np.percentile(blur, 95)))
    norm = np.clip(blur.astype(np.float32) * (255.0 / paper), 0, 255).astype(np.uint8)
    _, dark = cv2.threshold(norm, 175, 255, cv2.THRESH_BINARY_INV)
    return cv2.bitwise_and(ada, dark)


def _longest(mask: np.ndarray, horizontal: bool) -> dict | None:
    """Longest line component, ignoring lines hugging the image edge (a printed answer-box border)."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    H, W = mask.shape
    best = None
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        pos, size = (y + h / 2, H) if horizontal else (x + w / 2, W)
        if pos < 0.035 * size or pos > 0.965 * size:
            continue
        length = w if horizontal else h
        if best is None or length > best[0]:
            best = (length, x, y, w, h)
    if best is None:
        return None
    _, x, y, w, h = best
    return {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}


def _stroke_width(ink: np.ndarray) -> float:
    dist = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    skel_vals = dist[dist > 0]
    if skel_vals.size == 0:
        return 2.0
    # ridge values ~ half the stroke width; the 75th percentile ignores edge pixels
    return float(max(1.5, 2 * np.percentile(skel_vals, 75)))


def _runs(profile: np.ndarray, min_val: int) -> list[float]:
    """Centres of runs where profile >= min_val."""
    out, start = [], None
    for i, v in enumerate(list(profile) + [0]):
        if v >= min_val and start is None:
            start = i
        elif v < min_val and start is not None:
            out.append((start + i - 1) / 2)
            start = None
    return out


def _regular(pos: list[float]) -> list[float]:
    """Drop spurious 'ticks' (a curve meeting the axis) that crowd a real one: of two marks
    closer than half the usual gap, keep the one nearer the regular spacing."""
    pos = sorted(pos)
    if len(pos) < 4:
        return pos
    g = float(np.median(np.diff(pos)))
    out = [pos[0]]
    for p in pos[1:]:
        if p - out[-1] >= 0.5 * g:
            out.append(p)
            continue
        if len(out) >= 2:
            target = out[-2] + g
            if abs(p - target) < abs(out[-1] - target):
                out[-1] = p
    return out


def _even(pos: list[float]) -> bool | None:
    if len(pos) < 4:
        return None
    gaps = np.diff(sorted(pos))
    gaps = gaps[gaps > 2]
    if len(gaps) < 3:
        return None
    return bool(np.std(gaps) / max(1e-6, np.mean(gaps)) < 0.25)


def analyze(data: bytes | None) -> dict:
    """Detect axes, ticks and filled dots. Never raises; {"ok": False} when nothing usable."""
    try:
        gray = _decode(data)
    except Exception:  # noqa: BLE001
        gray = None
    if gray is None:
        return {"ok": False}
    try:
        return _analyze(gray)
    except Exception:  # noqa: BLE001  (OpenCV on a weird photo must never break grading)
        return {"ok": False}


def _analyze(gray: np.ndarray) -> dict:
    h, w = gray.shape
    ink = _ink(gray)
    sw = _stroke_width(ink)
    # long straight lines: opening with a long thin kernel keeps only them
    kh = cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, int(w * 0.3)), 1))
    kv = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, int(h * 0.3))))
    close = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    joined = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, close)  # bridge small gaps in a hand-drawn line
    hmask = cv2.morphologyEx(joined, cv2.MORPH_OPEN, kh)
    vmask = cv2.morphologyEx(joined, cv2.MORPH_OPEN, kv)
    hl, vl = _longest(hmask, True), _longest(vmask, False)
    axes: dict[str, Any] = {}
    if hl and hl["w"] >= 0.3 * w:
        axes["x_axis"] = {"y": (hl["y"] + hl["h"] / 2) / h, "x0": hl["x"] / w, "x1": (hl["x"] + hl["w"]) / w}
    if vl and vl["h"] >= 0.3 * h:
        axes["y_axis"] = {"x": (vl["x"] + vl["w"] / 2) / w, "y0": vl["y"] / h, "y1": (vl["y"] + vl["h"]) / h}
    if "x_axis" in axes and "y_axis" in axes:
        axes["origin"] = [axes["y_axis"]["x"], axes["x_axis"]["y"]]

    # ticks: short marks crossing an axis, seen in a thin band just beside the axis line
    ticks: dict[str, list[float]] = {"x": [], "y": []}
    reach = int(max(6, sw * 3))
    need = max(3, int(sw * 1.2))
    if hl and "x_axis" in axes:
        rest = cv2.bitwise_and(ink, cv2.bitwise_not(cv2.dilate(hmask, close, iterations=1)))
        part = rest[max(0, hl["y"] - reach):min(h, hl["y"] + hl["h"] + reach), :]
        prof = (part > 0).sum(axis=0)
        yx = axes.get("y_axis", {}).get("x", -1) * w
        xs = [x for x in _runs(prof, need) if hl["x"] <= x <= hl["x"] + hl["w"] and abs(x - yx) > 0.02 * w]
        ticks["x"] = [round(x / w, 4) for x in _regular(xs)]
    if vl and "y_axis" in axes:
        rest = cv2.bitwise_and(ink, cv2.bitwise_not(cv2.dilate(vmask, close, iterations=1)))
        part = rest[:, max(0, vl["x"] - reach):min(w, vl["x"] + vl["w"] + reach)]
        prof = (part > 0).sum(axis=1)
        xy_ = axes.get("x_axis", {}).get("y", -1) * h
        ys = [y for y in _runs(prof, need) if vl["y"] <= y <= vl["y"] + vl["h"] and abs(y - xy_) > 0.02 * h]
        ticks["y"] = [round(y / h, 4) for y in _regular(ys)]

    # filled dots: opening with a disc wider than the pen stroke removes lines and curves
    d = int(max(3, round(sw * 1.8))) | 1
    disc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d))
    blobs = cv2.morphologyEx(cv2.morphologyEx(ink, cv2.MORPH_CLOSE, close), cv2.MORPH_OPEN, disc)
    n, _, stats, cents = cv2.connectedComponentsWithStats(blobs, connectivity=8)
    dots = []
    max_dim = 0.06 * max(w, h)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw > max_dim or bh > max_dim or area < d * d * 0.4:
            continue
        if min(bw, bh) / max(bw, bh) < 0.4:
            continue
        cx, cy = cents[i]
        nx, ny = float(cx / w), float(cy / h)
        if min(nx, ny, 1 - nx, 1 - ny) < 0.03:  # printed text / border at the crop's edge
            continue
        # tick marks and the curve crossing an axis make thick spots on the axis line: not data
        if "x_axis" in axes and abs(ny - axes["x_axis"]["y"]) < 0.025:
            continue
        if "y_axis" in axes and abs(nx - axes["y_axis"]["x"]) < 0.025:
            continue
        dots.append([round(nx, 4), round(ny, 4)])
    dots.sort()
    return {"ok": True, "size": [w, h], "stroke": round(sw, 2), "axes": axes or None,
            "ticks": ticks, "ticks_even": {"x": _even([t * w for t in ticks["x"]]),
                                           "y": _even([t * h for t in ticks["y"]])},
            "dots": dots[:200]}


# ---------------------------------------------------------------- cross-check with the AI
def _fit(pairs: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """value = a * pixel + b by least squares; returns (a, b, max relative residual)."""
    if len(pairs) < 2:
        return None
    px = np.array([p[0] for p in pairs])
    val = np.array([p[1] for p in pairs])
    if np.ptp(px) < 0.02 or np.ptp(val) == 0:
        return None
    a, b = np.polyfit(px, val, 1)
    res = np.abs(a * px + b - val)
    return float(a), float(b), float(res.max() / max(1e-9, np.ptp(val)))


def calibrate(readings: list[dict]) -> dict | None:
    """Pixel <-> value mapping from the values the AI read off the axes at given image spots."""
    pts = [r for r in readings if r.get("at")]
    fx = _fit([(r["at"][0], r["x"]) for r in pts])
    fy = _fit([(r["at"][1], r["y"]) for r in pts])
    if fx is None or fy is None or fx[0] == 0 or fy[0] == 0:
        return None
    return {"x": fx, "y": fy}


def _near(p: list[float], dots: list[list[float]], radius: float) -> bool:
    return any(math.hypot(p[0] - d[0], p[1] - d[1]) <= radius for d in dots)


def cross_check(spec: dict, features: list[dict], readings: list[dict], cv: dict | None) -> dict:
    """Compare the AI's checklist + readings with the detections. Returns
    {"ran", "axes_found", "dots_found", "ticks_even", "expected_points", "points_matched",
     "readings_on_dots", "calibrated", "agrees": bool|None, "issues": [...]}."""
    out: dict[str, Any] = {"ran": bool(cv and cv.get("ok")), "axes_found": None, "dots_found": None,
                           "ticks_even": None, "expected_points": 0, "points_matched": None,
                           "readings_on_dots": None, "calibrated": False, "agrees": None, "issues": []}
    issues: list[str] = out["issues"]
    by_key = {}
    for f in features:
        by_key.setdefault(f.get("key") or "custom", f)
    spec_feats = {f["id"]: f for f in spec.get("features") or []}

    # 1) the AI's own readings vs. the expected points (no OpenCV needed)
    for f in features:
        sf = spec_feats.get(f["id"]) or {}
        xy = sf.get("points_xy") or []
        if sf.get("key") not in POINT_KEYS or not xy or f["status"] == "not_assessed":
            continue
        tol = float(sf.get("tolerance") or 0.5)
        if readings:
            hit = sum(1 for p in xy if any(abs(r["x"] - p[0]) <= tol + 1e-9 and abs(r["y"] - p[1]) <= tol + 1e-9
                                           for r in readings))
            ai_frac = float(f["points"]) / float(f["max_points"]) if float(f["max_points"]) > 0 else 0
            if abs(hit / len(xy) - ai_frac) > 0.34:
                issues.append(f"“{f['label']}”: the AI read {hit} of {len(xy)} expected points but gave "
                              f"{f['points']} / {f['max_points']}")
    if not out["ran"]:
        out["agrees"] = not issues if issues else None
        return out

    axes = cv.get("axes") or {}
    graphish = spec.get("kind") in ("function_graph", "data_chart")
    out["axes_found"] = bool(axes.get("x_axis") and axes.get("y_axis"))
    dots = cv.get("dots") or []
    out["dots_found"] = len(dots)
    te = cv.get("ticks_even") or {}
    out["ticks_even"] = None if te.get("x") is None and te.get("y") is None else \
        all(v for v in (te.get("x"), te.get("y")) if v is not None)

    # 2) axes
    if graphish:
        seen = [by_key[k] for k in AXES_KEYS if k in by_key and by_key[k]["status"] == "present"]
        if seen and not axes:
            issues.append("The AI saw axes but none were detected in the picture")
        ax = by_key.get("axes")  # "axis labels missing" does not mean "no axes"
        if ax is not None and ax["status"] == "missing" and out["axes_found"]:
            issues.append("The AI said the axes are missing but two axes were detected")
    # 3) scale
    sc = next((by_key[k] for k in SCALE_KEYS if k in by_key), None)
    if spec.get("kind") == "data_chart" and sc is not None and sc["status"] == "present" and out["ticks_even"] is False:
        issues.append("Tick marks on the axes look unevenly spaced")
    # 4) plotted points: map the expected values to pixels with the AI's own readings
    if spec.get("kind") == "data_chart" and not dots:
        for f in features:
            if f.get("key") == "points" and f["status"] != "not_assessed" and float(f["points"]) > 0:
                issues.append(f"“{f['label']}”: the AI gave points but no plotted dots were found "
                              "(crosses are not detected — check by eye)")
    cal = calibrate(readings) if readings else None
    out["calibrated"] = cal is not None
    if dots and readings:
        on = [r for r in readings if r.get("at")]
        if on:
            hits = sum(1 for r in on if _near(r["at"], dots, 0.04))
            out["readings_on_dots"] = round(hits / len(on), 2)
            if hits / len(on) < 0.5 and spec.get("kind") == "data_chart":
                issues.append("Most points the AI read are not at a plotted dot in the picture")
    if cal is not None and dots:
        (ax_, bx, _), (ay, by, _) = cal["x"], cal["y"]
        total = matched = 0
        for f in features:
            sf = spec_feats.get(f["id"]) or {}
            xy = sf.get("points_xy") or []
            if sf.get("key") != "points" or not xy or f["status"] == "not_assessed":
                continue
            tol = float(sf.get("tolerance") or 0.5)
            rx = abs(tol / ax_) if ax_ else 0.02
            ry = abs(tol / ay) if ay else 0.02
            radius = max(0.02, min(0.08, math.hypot(rx, ry)))
            m = sum(1 for p in xy if _near([(p[0] - bx) / ax_, (p[1] - by) / ay], dots, radius))
            total += len(xy)
            matched += m
            ai_frac = float(f["points"]) / float(f["max_points"]) if float(f["max_points"]) > 0 else 0
            if abs(m / len(xy) - ai_frac) > 0.34:
                issues.append(f"“{f['label']}”: {m} of {len(xy)} expected points found in the picture, "
                              f"the AI gave {f['points']} / {f['max_points']}")
        out["expected_points"], out["points_matched"] = total, (matched if total else None)
    out["agrees"] = not issues
    return out
