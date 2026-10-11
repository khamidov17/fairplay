"""Graph / chart / diagram questions: kinds, default feature checklists, validation (pure).

A paper question (type "essay") may carry `diagram` — the student's answer is a drawing that is
graded against a feature checklist instead of steps. Stored in the question (exam editor) and,
optionally, overridden per exam in the answer key (`answer_key.questions[qid].diagram`,
edited in the Scan & Grade answer-key step). Shape (also in CONTRACT.md "Graph and diagram
questions"):

{
  "kind": "function_graph" | "data_chart" | "geometry" | "physics_fbd" | "physics_circuit"
          | "physics_ray" | "chemistry_structure" | "labelled_diagram" | "flowchart",
  "features": [{"id": "f1", "key": "axes", "label": "Axes drawn and labelled", "points": 1,
                "expected": "free text the AI checks against",
                "points_xy": [[x, y], ...],            # optional: points the drawing must pass through / plot
                "tolerance": 0.5}],                    # optional: tolerance for points_xy (axis units)
  "reference_image": "/api/uploads/<id>" | null,       # teacher's correct drawing
  "notes": "free text for the AI (e.g. f(x) = x^2 - 2x - 3)"
}
Feature points add up to the question's points. No `diagram` = today's behaviour.

This module imports nothing from the app except `grading.num` so schemas.py can use it.
"""
from __future__ import annotations

import math
import re
import secrets
from typing import Any

from ..grading import num

UPLOAD_URL_RE = re.compile(r"^/api/uploads/[A-Za-z0-9_-]{16,64}$")
MAX_FEATURES = 20
MAX_XY = 50

# kind -> (label, group, default checklist [(key, label, weight, expected)])
KINDS: dict[str, dict[str, Any]] = {
    "function_graph": {
        "label": "Function graph (sketch)", "group": "Graphs and charts",
        "hint": "Sketch of y = f(x): intercepts, turning points, asymptotes, shape.",
        "features": [
            ("axes", "Axes drawn and labelled (x, y)", 1, ""),
            ("intercepts", "Intercepts correct (x and y values)", 2, ""),
            ("turning_points", "Turning points / vertex correct", 2, ""),
            ("asymptotes", "Asymptotes shown and approached correctly", 1, "none"),
            ("shape", "Shape and end behaviour", 2, ""),
            ("domain", "Domain / restrictions respected", 1, "all real x"),
            ("key_points", "Passes through the key points", 1, ""),
            ("scale", "Sensible scale marked on the axes", 1, ""),
        ],
    },
    "data_chart": {
        "label": "Data chart (bar / line / scatter from a table)", "group": "Graphs and charts",
        "hint": "Plotted from a data table: chart type, labels, scale, plotted points, best fit.",
        "features": [
            ("chart_type", "Correct chart type", 1, "scatter graph"),
            ("axis_labels", "Axis labels with units", 1, ""),
            ("scale", "Even, sensible scale", 1, ""),
            ("points", "Points plotted correctly", 3, ""),
            ("best_fit", "Line / curve of best fit", 1, ""),
            ("title", "Title", 1, ""),
        ],
    },
    "geometry": {
        "label": "Geometry construction", "group": "Maths",
        "hint": "Compass-and-ruler constructions, loci, transformations.",
        "features": [
            ("construction_lines", "Construction arcs / lines left visible", 2, ""),
            ("accuracy", "Lengths and angles accurate", 2, "within 2 mm / 2°"),
            ("labels", "Points and lines labelled", 1, ""),
            ("result", "Required figure / result constructed", 2, ""),
        ],
    },
    "physics_fbd": {
        "label": "Free-body diagram", "group": "Physics diagrams",
        "hint": "Forces on one object: direction, start point, relative size.",
        "features": [
            ("gravity", "Weight (gravity) straight down", 1, ""),
            ("normal", "Normal force perpendicular to the surface", 1, ""),
            ("friction", "Friction opposite to the motion", 1, ""),
            ("tension", "Tension along the rope", 1, "none"),
            ("no_extra", "No extra or made-up forces", 1, ""),
            ("from_object", "Arrows start on the object", 1, ""),
            ("lengths", "Relative arrow lengths make sense", 1, ""),
            ("labels", "Forces labelled", 1, ""),
        ],
    },
    "physics_circuit": {
        "label": "Circuit diagram", "group": "Physics diagrams",
        "hint": "Components, series / parallel topology, symbols, meters.",
        "features": [
            ("components", "All required components present", 2, ""),
            ("topology", "Correct series / parallel connections", 2, ""),
            ("symbols", "Correct circuit symbols", 1, ""),
            ("closed", "Circuit closed (no gaps)", 1, ""),
            ("meters", "Ammeter in series, voltmeter in parallel", 1, ""),
            ("labels", "Values / labels shown", 1, ""),
        ],
    },
    "physics_ray": {
        "label": "Ray diagram", "group": "Physics diagrams",
        "hint": "Lenses and mirrors: principal rays, normal, image.",
        "features": [
            ("incident", "Rays drawn from the object", 1, ""),
            ("rule_rays", "Principal rays follow the rules", 2, ""),
            ("normal", "Normal / principal axis drawn", 1, ""),
            ("arrows", "Arrows show the direction of light", 1, ""),
            ("image", "Image position and type correct", 2, ""),
        ],
    },
    "chemistry_structure": {
        "label": "Chemistry structure", "group": "Chemistry and biology",
        "hint": "Structural / Lewis formulas, mechanisms.",
        "features": [
            ("atoms", "Correct atoms and connectivity", 2, ""),
            ("bonds", "Correct bond types (single / double / triple)", 2, ""),
            ("lone_pairs", "Lone pairs and charges shown", 1, ""),
            ("geometry", "Shape / geometry / stereochemistry", 1, ""),
            ("labels", "Functional groups / names labelled", 1, ""),
        ],
    },
    "labelled_diagram": {
        "label": "Labelled diagram (biology and other)", "group": "Chemistry and biology",
        "hint": "Cells, organs, apparatus: structures and labels.",
        "features": [
            ("structures", "Required structures drawn", 2, ""),
            ("labels", "Labels correct", 2, ""),
            ("label_lines", "Label lines point to the right part", 1, ""),
            ("proportions", "Proportions and positions realistic", 1, ""),
            ("title", "Title / magnification", 1, ""),
        ],
    },
    "flowchart": {
        "label": "Flowchart / other diagram", "group": "Other",
        "hint": "Flowcharts, concept maps, any other drawing.",
        "features": [
            ("elements", "All required steps / elements present", 2, ""),
            ("order", "Correct order / connections", 2, ""),
            ("symbols", "Correct symbols (start/end, decision, process)", 1, ""),
            ("decisions", "Decisions have labelled branches", 1, ""),
            ("clarity", "Clear and readable", 1, ""),
        ],
    },
}

# feature keys whose result the OpenCV helpers can cross-check
AXES_KEYS = ("axes", "axis_labels")
POINT_KEYS = ("points", "key_points")
SCALE_KEYS = ("scale",)


def kind_label(kind: str | None) -> str:
    return KINDS.get(kind or "", {}).get("label", "Diagram")


def split_points(total: float, weights: list[float], step: float = 0.5) -> list[float]:
    """Share `total` points by weight in multiples of `step` (largest remainder); sums exactly."""
    if not weights:
        return []
    wsum = sum(weights) or 1.0
    units = int(round(total / step))
    if abs(units * step - total) > 1e-9:  # total not a multiple of step: put the rest on the last one
        units = int(total // step)
    raw = [units * w / wsum for w in weights]
    base = [int(math.floor(r)) for r in raw]
    left = units - sum(base)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - base[i]), -weights[i], i))
    for i in order[:left]:
        base[i] += 1
    out = [b * step for b in base]
    out[-1] = round(out[-1] + (total - sum(out)), 4)
    return out


def default_features(kind: str, total_points: float) -> list[dict]:
    """The subtype's checklist with points shared to `total_points`."""
    spec = KINDS.get(kind)
    if spec is None:
        raise ValueError("Unknown diagram kind")
    rows = spec["features"]
    pts = split_points(float(total_points), [float(r[2]) for r in rows])
    out = []
    for i, ((key, label, _w, expected), p) in enumerate(zip(rows, pts)):
        f = {"id": f"f{i + 1}", "key": key, "label": label, "points": num(p), "expected": expected}
        if key in POINT_KEYS:
            f["points_xy"], f["tolerance"] = [], 0.5
        out.append(f)
    return out


def catalog() -> list[dict]:
    """Kinds for the editors (defaults shown for a 10-point question; the client rescales)."""
    return [{"kind": k, "label": v["label"], "group": v["group"], "hint": v["hint"],
             "features": default_features(k, 10)} for k, v in KINDS.items()]


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clean_xy(raw: Any) -> list[list[float]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("points_xy must be a list of [x, y] pairs")
    out = []
    for p in raw[:MAX_XY + 1]:
        if isinstance(p, dict):
            p = [p.get("x"), p.get("y")]
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError("points_xy must be a list of [x, y] pairs")
        x, y = _finite(p[0]), _finite(p[1])
        if x is None or y is None:
            raise ValueError("points_xy must hold numbers")
        out.append([num(x), num(y)])
    if len(out) > MAX_XY:
        raise ValueError(f"At most {MAX_XY} points per feature")
    return out


def clean_diagram(raw: Any, max_points: float, strict: bool = True) -> dict | None:
    """Validate a diagram spec. Empty features -> the subtype's defaults. Raises ValueError."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("diagram must be an object")
    kind = str(raw.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError("Pick a diagram kind: " + ", ".join(KINDS))
    feats_raw = raw.get("features")
    if feats_raw in (None, []):
        features = default_features(kind, max_points)
    else:
        if not isinstance(feats_raw, list):
            raise ValueError("diagram features must be a list")
        if len(feats_raw) > MAX_FEATURES:
            raise ValueError(f"A checklist can have at most {MAX_FEATURES} features")
        features, seen = [], set()
        for i, f in enumerate(feats_raw):
            if not isinstance(f, dict):
                raise ValueError("each feature needs a label and points")
            label = " ".join(str(f.get("label") or "").split())[:200]
            if not label:
                raise ValueError("Each checklist feature needs a label")
            pts = _finite(f.get("points"))
            if pts is None or pts < 0 or pts > 1000:
                raise ValueError(f"Feature “{label}” needs points (0 or more)")
            fid = re.sub(r"[^A-Za-z0-9_-]", "", str(f.get("id") or ""))[:16] or f"f{i + 1}"
            while fid in seen:
                fid = f"f{i + 1}_{secrets.token_hex(2)}"
            seen.add(fid)
            key = re.sub(r"[^a-z0-9_]", "", str(f.get("key") or "custom").lower())[:32] or "custom"
            row = {"id": fid, "key": key, "label": label, "points": num(pts),
                   "expected": str(f.get("expected") or "").strip()[:500]}
            xy = _clean_xy(f.get("points_xy"))
            if xy or key in POINT_KEYS or f.get("points_xy") is not None:
                row["points_xy"] = xy
                tol = _finite(f.get("tolerance"))
                row["tolerance"] = num(max(0.0, tol)) if tol is not None else 0.5
            features.append(row)
        total = sum(float(f["points"]) for f in features)
        if abs(total - float(max_points)) > 1e-6:
            if strict:
                raise ValueError(f"Checklist points must add up to {num(max_points)} (now {num(total)})")
            features = rescale(features, max_points)
    ref = raw.get("reference_image")
    ref = str(ref).strip() if ref else None
    if ref and not UPLOAD_URL_RE.match(ref):
        raise ValueError("The reference drawing must be an uploaded image")
    return {"kind": kind, "features": features, "reference_image": ref or None,
            "notes": str(raw.get("notes") or "").strip()[:2000]}


def rescale(features: list[dict], max_points: float) -> list[dict]:
    """Points no longer add up (the question's points changed): share by the old weights."""
    weights = [float(f["points"]) for f in features]
    if sum(weights) <= 0:
        weights = [1.0] * len(features)
    pts = split_points(float(max_points), weights, 0.25)
    return [dict(f, points=num(p)) for f, p in zip(features, pts)]


def effective(q: dict, entry: dict | None) -> dict | None:
    """Diagram spec used for grading: the answer key's override, else the question's. None when
    the question is not a graph / diagram question (or is not a written-answer question)."""
    if q.get("type") != "essay":
        return None
    raw = (entry or {}).get("diagram") or q.get("diagram")
    if not raw:
        return None
    try:
        return clean_diagram(raw, float(q.get("points", 1)), strict=False)
    except ValueError:
        return None


def feature_criteria(spec: dict | None) -> list[dict]:
    """The checklist as rubric criteria (same ids), so approve / toggles work like criteria."""
    if not spec:
        return []
    return [{"id": f["id"], "label": f["label"], "points": f["points"], "description": f.get("expected", "")}
            for f in spec["features"]]
