"""AI grading calibration: Fritsch-Carlson monotone Hermite spline + fairness presets.

The spline reshapes the model's raw score distribution to match human score
distributions (trained on 3,470 Mohler + jorGPT pairs).  Three strictness
presets control how much extra lift is added on top of the spline.

Anchor points are grouped per model.  The default set comes from the
Gemma-4-31B-AWQ-8bit training run (current production model); the
Qwen3.6-27B-AWQ-INT4 anchors are retained as historical reference and can be
selected via the ``model`` argument (see DKUOJ_JUDGE_PRODUCTION.md §2).
"""
import math

# --- trained anchor points (model_percentile, human_percentile) ---
# Each set has 9 anchors at percentiles [0, 5, 15, 30, 50, 70, 85, 95, 100].

# Gemma-4-31B-AWQ-8bit — current production model.
_ANCHORS_GEMMA4 = {
    5: [
        (0.0, 0.0), (2.30, 2.0), (3.07, 3.0), (3.58, 3.5), (4.60, 4.5),
        (4.86, 5.0), (4.86, 5.0), (4.86, 5.0), (5.0, 5.0),
    ],
    10: [
        (0.0, 0.0), (0.71, 0.0), (2.33, 2.0), (3.14, 3.0), (3.95, 5.0),
        (5.57, 6.0), (6.78, 7.0), (7.59, 9.0), (10.0, 10.0),
    ],
}

# Qwen3.6-27B-AWQ-INT4 — historical, kept for reference / rollback.
_ANCHORS_QWEN = {
    5: [
        (0.0, 0.0), (2.83, 2.0), (3.23, 3.0), (4.02, 4.0), (4.82, 4.63),
        (4.82, 5.0), (4.82, 5.0), (4.82, 5.0), (5.0, 5.0),
    ],
    10: [
        (0.0, 0.0), (1.81, 0.0), (3.20, 2.0), (3.20, 3.0), (5.29, 5.0),
        (7.03, 7.0), (7.72, 8.0), (8.42, 9.0), (10.0, 10.0),
    ],
}

DEFAULT_MODEL = 'gemma4'
_ANCHOR_SETS = {
    'gemma4': _ANCHORS_GEMMA4,
    'qwen': _ANCHORS_QWEN,
}

# Fit anchors without duplicate x-values (keep first occurrence), per model.
_FIT_ANCHORS: dict[str, dict[int, tuple]] = {}
for _model, _set in _ANCHOR_SETS.items():
    _fit = {}
    for _ms, _pairs in _set.items():
        _xs, _ys = [], []
        _seen = set()
        for _x, _y in _pairs:
            if _x not in _seen:
                _xs.append(_x)
                _ys.append(_y)
                _seen.add(_x)
        _fit[_ms] = (_xs, _ys)
    _FIT_ANCHORS[_model] = _fit


def _fritsch_carlson(x_anchors, y_anchors, x_eval):
    """Fritsch-Carlson piecewise monotone cubic Hermite interpolation.

    *x_anchors*  : sorted list of N anchor x-values (model percentiles on the
                   original score scale).
    *y_anchors*  : parallel list of N anchor y-values (human percentiles on the
                   original score scale).
    *x_eval*     : scalar to interpolate.

    Returns a scalar — the spline-mapped score on the original scale.
    """
    n = len(x_anchors)
    if n < 2:
        return y_anchors[0] if x_eval <= x_anchors[0] else y_anchors[-1]

    # Slopes of each segment
    slopes = []
    for i in range(n - 1):
        dx = x_anchors[i + 1] - x_anchors[i]
        dy = y_anchors[i + 1] - y_anchors[i]
        slopes.append(dy / dx if dx > 1e-9 else 0.0)

    # Fritsch-Carlson interior derivatives (harmonic mean of neighbours)
    derivatives = [0.0] * n
    for i in range(1, n - 1):
        s0, s1 = slopes[i - 1], slopes[i]
        if s0 > 0 and s1 > 0:
            # Unweighted double harmonic mean (original FITPACK formula)
            derivatives[i] = 3 * s0 * s1 / (s0 + s1) if (s0 + s1) > 0 else 0
        elif s0 > 0:
            derivatives[i] = 0.75 * s0
        elif s1 > 0:
            derivatives[i] = 0.75 * s1
        else:
            derivatives[i] = 0.0

    # Endpoint derivatives (one-sided)
    if n == 2:
        derivatives[0] = derivatives[1] = slopes[0]
    else:
        derivatives[0] = ((2 * slopes[0] - slopes[1])
                          if slopes[0] * slopes[1] > 0 else 0.5 * slopes[0])
        derivatives[-1] = ((2 * slopes[-1] - slopes[-2])
                           if slopes[-1] * slopes[-2] > 0 else 0.5 * slopes[-1])

    # Locate segment
    if x_eval <= x_anchors[0]:
        return max(0.0, y_anchors[0] + derivatives[0] * (x_eval - x_anchors[0]))
    if x_eval >= x_anchors[-1]:
        return y_anchors[-1] + derivatives[-1] * (x_eval - x_anchors[-1])

    i = next((k for k in range(n - 1) if x_anchors[k + 1] >= x_eval), n - 2)
    x0, x1 = x_anchors[i], x_anchors[i + 1]
    y0, y1 = y_anchors[i], y_anchors[i + 1]
    d0, d1 = derivatives[i], derivatives[i + 1]
    h = x1 - x0
    t = (x_eval - x0) / h if h > 0 else 0

    # Cubic Hermite basis
    h00 = 2 * t ** 3 - 3 * t ** 2 + 1
    h10 = t ** 3 - 2 * t ** 2 + t
    h01 = -2 * t ** 3 + 3 * t ** 2
    h11 = t ** 3 - t ** 2

    return h00 * y0 + h10 * h * d0 + h01 * y1 + h11 * h * d1


def apply_spline(raw_score: float, max_score: float,
                 model: str = DEFAULT_MODEL) -> float:
    """Map *raw_score* through the trained spline for *max_score*.

    *model* selects the anchor set ('gemma4' default, 'qwen' historical).
    If no anchors exist for *max_score* the raw score is returned unchanged.
    """
    anchors = _FIT_ANCHORS.get(model, {}).get(int(max_score))
    if anchors is None:
        return raw_score

    x_anchors, y_anchors = anchors
    value = _fritsch_carlson(x_anchors, y_anchors, raw_score)
    # Fritsch-Carlson can overshoot by a few hundredths on flat tail segments
    # (e.g. raw 4.86→5.0 on max_score=5); clip to keep the public invariant
    # 0 ≤ spline ≤ max_score.
    return max(0.0, min(float(max_score), value))


def apply_preset(spline_score: float, max_score: float, strictness: str) -> float:
    """Apply fairness lift on top of the spline-mapped score.

    *strictness* must be one of ``'strict'``, ``'normal'``, ``'lenient'``.

    The lift is applied as percentage points on the 0-100 scale, then
    denormalised back to the original *max_score*.
    """
    if max_score <= 0:
        return 0.0

    pct = spline_score / max_score * 100.0

    if strictness == 'strict':
        corrected_pct = pct
    elif strictness == 'lenient':
        corrected_pct = min(100.0, pct + 12.0)
    else:  # normal (default)
        corrected_pct = min(100.0, pct + 5.0 * math.sin(math.pi * pct / 100.0))

    return corrected_pct / 100.0 * max_score


def round_half(value: float) -> float:
    """Round to nearest 0.5."""
    return round(value * 2.0) / 2.0


def redistribute_half_points(raw_weights, total):
    """Snap *raw_weights* onto a 0.5 grid that sums exactly to *total*.

    Used so AI-decomposed / auto-split grading-table rows never land on awkward
    fractions like 0.3 / 0.7.  *total* itself is also snapped to 0.5.  When every
    weight is zero (or the list is empty) the total is split as evenly as the
    0.5 grid allows.
    """
    n = len(raw_weights or [])
    if n == 0:
        return []
    total_halves = int(round(round_half(float(total or 0)) * 2.0))
    if total_halves <= 0:
        return [0.0] * n

    cleaned = [max(0.0, float(w or 0)) for w in raw_weights]
    weight_sum = sum(cleaned)
    if weight_sum <= 0:
        cleaned = [1.0] * n
        weight_sum = float(n)

    exact = [w / weight_sum * total_halves for w in cleaned]
    floors = [int(math.floor(x)) for x in exact]
    remainder = total_halves - sum(floors)
    # Largest-remainder: give leftover half-units to the biggest fractional parts.
    order = sorted(
        range(n),
        key=lambda i: (exact[i] - floors[i], cleaned[i]),
        reverse=True,
    )
    for i in order[:max(0, remainder)]:
        floors[i] += 1
    return [h / 2.0 for h in floors]


def calibrate(raw_score: float, max_score: float,
              strictness: str = 'normal', model: str = DEFAULT_MODEL) -> float:
    """Full calibration pipeline: spline → preset → round.

    Returns the calibrated score on the original *max_score* scale,
    rounded to 0.5 precision and clipped to [0, max_score].
    Safe for max_score ≤ 0 (returns 0).
    """
    if max_score <= 0:
        return 0.0

    spline = apply_spline(raw_score, max_score, model)
    corrected = apply_preset(spline, max_score, strictness)
    rounded = round_half(corrected)
    return max(0.0, min(float(max_score), rounded))
