"""Agreement metrics between AI marks and human marks. Pure functions, no I/O.

A `Pair` is one answer marked by both: `ai` (the AI's suggested points), `human` (the
reference / final teacher points) and `max_points`. Rates are fractions 0..1.

- exact: |ai - human| < 1e-6
- within_1: |ai - human| <= 1 point
- within_10pct: |ai - human| <= 10 % of max points
- mae: mean |ai - human| (points);  mae_pct: mean |ai - human| / max * 100
- bias: mean(ai) - mean(human) (points; > 0 means the AI is more generous)
- kappa: quadratic weighted Cohen's kappa on integer marks (round half up), categories
  0..ceil(largest max points) of the group; None when undefined (n < 2 or no spread)
- severe_rate: share with |ai - human| >= severe_fraction * max (default 25 %)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable

EPS = 1e-6
DEFAULT_SEVERE_FRACTION = 0.25


@dataclass
class Pair:
    ai: float
    human: float
    max_points: float
    question_type: str = ""
    subject: str = ""
    ref: dict = field(default_factory=dict)

    @property
    def diff(self) -> float:
        return self.ai - self.human

    @property
    def abs_frac(self) -> float:
        return abs(self.diff) / self.max_points if self.max_points > 0 else 0.0


def r4(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def int_mark(x: float) -> int:
    """Round half up (2.5 -> 3), so half points map to the upper integer mark."""
    return int(math.floor(x + 0.5))


def quadratic_weighted_kappa(a: list[int], b: list[int], min_rating: int | None = None,
                             max_rating: int | None = None) -> float | None:
    """Cohen's kappa with quadratic weights w_ij = (i-j)^2 / (K-1)^2."""
    if len(a) != len(b) or len(a) < 2:
        return None
    lo = min(min(a), min(b)) if min_rating is None else min_rating
    hi = max(max(a), max(b)) if max_rating is None else max_rating
    k = hi - lo + 1
    if k < 2:
        return None
    n = len(a)
    obs = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b):
        obs[x - lo][y - lo] += 1
    hist_a = [sum(row) for row in obs]
    hist_b = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = (i - j) ** 2 / (k - 1) ** 2
            num += w * obs[i][j]
            den += w * hist_a[i] * hist_b[j] / n
    if den <= 0:
        return None
    return 1.0 - num / den


def summarize(pairs: list[Pair], severe_fraction: float = DEFAULT_SEVERE_FRACTION) -> dict:
    n = len(pairs)
    if n == 0:
        return {"n": 0, "exact": None, "within_1": None, "within_10pct": None, "mae": None,
                "mae_pct": None, "bias": None, "kappa": None, "severe_rate": None, "severe": 0,
                "mean_ai": None, "mean_human": None}
    diffs = [abs(p.diff) for p in pairs]
    severe = sum(1 for p in pairs if is_severe(p, severe_fraction))
    top = max(int(math.ceil(p.max_points - EPS)) for p in pairs)
    kappa = quadratic_weighted_kappa([int_mark(p.ai) for p in pairs], [int_mark(p.human) for p in pairs],
                                     0, max(top, 1))
    mean_ai = sum(p.ai for p in pairs) / n
    mean_h = sum(p.human for p in pairs) / n
    return {
        "n": n,
        "exact": r4(sum(1 for d in diffs if d < EPS) / n),
        "within_1": r4(sum(1 for d in diffs if d <= 1 + EPS) / n),
        "within_10pct": r4(sum(1 for p in pairs if abs(p.diff) <= 0.1 * p.max_points + EPS) / n),
        "mae": r4(sum(diffs) / n),
        "mae_pct": r4(sum(p.abs_frac for p in pairs) / n * 100),
        "bias": r4(mean_ai - mean_h),
        "kappa": r4(kappa),
        "severe_rate": r4(severe / n),
        "severe": severe,
        "mean_ai": r4(mean_ai),
        "mean_human": r4(mean_h),
    }


def is_severe(p: Pair, severe_fraction: float = DEFAULT_SEVERE_FRACTION) -> bool:
    return abs(p.diff) > EPS and abs(p.diff) >= severe_fraction * p.max_points - EPS


def breakdown(pairs: list[Pair], key: str, severe_fraction: float = DEFAULT_SEVERE_FRACTION) -> list[dict]:
    groups: dict[str, list[Pair]] = {}
    for p in pairs:
        groups.setdefault(getattr(p, key) or "", []).append(p)
    return [dict(summarize(ps, severe_fraction), key=k) for k, ps in sorted(groups.items())]


def report(pairs: list[Pair], severe_fraction: float = DEFAULT_SEVERE_FRACTION) -> dict:
    return {"overall": summarize(pairs, severe_fraction),
            "by_type": breakdown(pairs, "question_type", severe_fraction),
            "by_subject": breakdown(pairs, "subject", severe_fraction),
            "severe_fraction": severe_fraction}


# ---------------------------------------------------------------- production grades
AI_SOURCES = ("ai_accepted", "ai_edited")


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def suggested_points(grade: dict | None, draft: dict | None) -> float | None:
    """AI points the teacher saw: grade.ai_suggested_points (Phase 2), else the stored
    AI draft's points (slice 6 drafts; 'fallback' drafts are not suggestions)."""
    if grade:
        v = _num(grade.get("ai_suggested_points"))
        if v is not None:
            return v
    if isinstance(draft, dict) and draft.get("source") != "fallback":
        return _num(draft.get("suggested_points", draft.get("points")))
    return None


def grade_source(grade: dict, suggested: float | None) -> str:
    """ai_accepted | ai_edited | manual. Missing source (pre-Phase 2 grades) is derived."""
    s = grade.get("source")
    if s in ("ai_accepted", "ai_edited", "manual"):
        return s
    if suggested is None:
        return "manual"
    pts = _num(grade.get("points")) or 0.0
    return "ai_accepted" if abs(pts - suggested) < EPS else "ai_edited"


def correction_stats(items: Iterable[dict]) -> dict:
    """items: {"suggested", "final", "max_points", "source"} for graded answers."""
    ai = [it for it in items if it["suggested"] is not None]
    edited = [it for it in ai if it["source"] == "ai_edited"]
    point_edits = [abs(it["final"] - it["suggested"]) for it in edited]
    return {
        "ai_suggested": len(ai),
        "accepted": sum(1 for it in ai if it["source"] == "ai_accepted"),
        "edited": len(edited),
        "correction_rate": r4(len(edited) / len(ai)) if ai else None,
        "points_changed": sum(1 for d in point_edits if d > EPS),
        "mean_edit": r4(sum(point_edits) / len(point_edits)) if point_edits else None,
        "mean_edit_pct": r4(sum(abs(it["final"] - it["suggested"]) / it["max_points"] * 100
                                for it in edited if it["max_points"] > 0) / len(edited)) if edited else None,
    }


def _time_summary(secs: list[float]) -> dict:
    if not secs:
        return {"n": 0, "mean": None, "median": None, "total": 0}
    return {"n": len(secs), "mean": r4(sum(secs) / len(secs)), "median": r4(median(secs)),
            "total": r4(sum(secs))}


def timing_stats(items: Iterable[dict]) -> dict:
    """items: {"seconds": float|None, "assisted": bool, "batch": str} -> per item (AI-assisted
    vs manual) and per batch. Items without grading_seconds are left out (unknown)."""
    items = [it for it in items if it.get("seconds") is not None]
    assisted = [it["seconds"] for it in items if it["assisted"]]
    manual = [it["seconds"] for it in items if not it["assisted"]]
    batches: dict[str, list[dict]] = {}
    for it in items:
        batches.setdefault(it.get("batch") or "", []).append(it)
    saved = None
    a, m = _time_summary(assisted), _time_summary(manual)
    if a["mean"] is not None and m["mean"]:
        saved = r4(1 - a["mean"] / m["mean"])
    return {
        "ai_assisted": a, "manual": m, "time_saved_fraction": saved,
        "batches": [{"batch": k, "items": len(v), "total_seconds": r4(sum(i["seconds"] for i in v)),
                     "mean_seconds": r4(sum(i["seconds"] for i in v) / len(v)),
                     "ai_assisted": sum(1 for i in v if i["assisted"])}
                    for k, v in sorted(batches.items())],
    }
