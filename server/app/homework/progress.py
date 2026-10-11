"""Progress across assignments (the Frizzle-style class dashboard) — pure functions.

Input is a flat list of `Item`s: one per (student, question) of every submitted attempt of
the teacher's work (homework, paper and computer exams). The loaders in routes.py build them;
everything here is plain Python so the math is tested on a fixed fixture.

Rules (also in CONTRACT.md "Homework mode and progress"):
- Mastery of a student in a topic = recency-weighted score: sum(w * awarded) / sum(w * max)
  over graded answers in that topic (pending answers are skipped), with
  w = 0.5 ** (age_days / HALF_LIFE_DAYS), age measured from `now` to the work's date.
  Level: >= 80 "strong", >= 60 "developing", else "struggling".
- Trend of a student (or of a student in one topic) = per-assignment percents in date order;
  recent = the last min(2, n // 2) assignments, before = up to 3 assignments before those;
  delta = mean(recent) - mean(before) (needs n >= 2). Slipping: delta <= -10. Improving:
  delta >= +10.
- Misconceptions: one label per answer that lost points — the teacher-approved
  misconception tag (grade), else the first approved mistake tag, else (answers not graded
  yet) the AI draft's tag. Counted per calendar window: this week = [now - 7 d, now),
  last week = [now - 14 d, now - 7 d), by the work's date.
- Topic actions: topics where class mastery < 70 or >= 2 students struggle. "reteach" when
  class mastery < 60 or at least 40 % of the students with answers struggle, else
  "practice" (targeted practice for the struggling group).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

HALF_LIFE_DAYS = 21.0
STRONG = 80.0
DEVELOPING = 60.0
TREND_DELTA = 10.0
ACTION_BELOW = 70.0
RETEACH_BELOW = 60.0
RETEACH_SHARE = 0.4
MAX_EXAMPLES = 4


@dataclass
class Item:
    student_id: int
    exam_id: str
    exam_title: str
    question_id: str
    topics: list[str]
    awarded: float | None          # None = pending (not graded yet)
    max_points: float
    date: datetime
    label: str | None = None       # misconception label (see module doc)
    approved: bool = False         # label comes from a teacher grade
    image: str | None = None       # the student's answer picture (paper work)
    prompt: str = ""
    comment: str = ""
    kind: str = "exam"             # "homework" | "paper" | "computer" (context)


@dataclass
class Student:
    id: int
    username: str
    name: str


def r1(x: float) -> float:
    return round(x, 1)


def level(pct: float | None) -> str | None:
    if pct is None:
        return None
    return "strong" if pct >= STRONG else "developing" if pct >= DEVELOPING else "struggling"


def weight(date: datetime, now: datetime) -> float:
    age = max(0.0, (now - date).total_seconds() / 86400.0)
    return 0.5 ** (age / HALF_LIFE_DAYS)


def mastery(items: Iterable[Item], now: datetime) -> tuple[float | None, int]:
    """(recency-weighted percent, number of graded answers)."""
    got = mx = 0.0
    n = 0
    for it in items:
        if it.awarded is None or it.max_points <= 0:
            continue
        w = weight(it.date, now)
        got += w * it.awarded
        mx += w * it.max_points
        n += 1
    return (r1(got / mx * 100) if mx > 0 else None), n


def per_assignment(items: Iterable[Item]) -> list[dict]:
    """Graded items grouped by exam, in date order: [{exam_id, title, date, percent}]."""
    acc: dict[str, dict] = {}
    for it in items:
        if it.awarded is None or it.max_points <= 0:
            continue
        a = acc.setdefault(it.exam_id, {"exam_id": it.exam_id, "title": it.exam_title, "date": it.date,
                                        "got": 0.0, "max": 0.0})
        a["got"] += it.awarded
        a["max"] += it.max_points
        a["date"] = min(a["date"], it.date)
    rows = sorted(acc.values(), key=lambda a: (a["date"], a["exam_id"]))
    return [{"exam_id": a["exam_id"], "title": a["title"], "date": a["date"],
             "percent": r1(a["got"] / a["max"] * 100)} for a in rows if a["max"] > 0]


def trend(percents: list[float]) -> float | None:
    n = len(percents)
    if n < 2:
        return None
    k = min(2, n // 2)
    recent = percents[-k:]
    before = percents[max(0, n - k - 3):n - k]
    return r1(sum(recent) / len(recent) - sum(before) / len(before))


def trend_label(delta: float | None) -> str:
    if delta is None:
        return "new"
    if delta <= -TREND_DELTA:
        return "slipping"
    if delta >= TREND_DELTA:
        return "improving"
    return "steady"


def _by(items: Iterable[Item], key) -> dict:
    out: dict = defaultdict(list)
    for it in items:
        for k in key(it):
            out[k].append(it)
    return out


def topics_of(items: Iterable[Item]) -> list[str]:
    return sorted({t for it in items for t in it.topics})


# ---------------------------------------------------------------- class dashboard
def misconceptions(items: list[Item], now: datetime, students: dict[int, Student], limit: int = 10) -> list[dict]:
    week = now - timedelta(days=7)
    prev = now - timedelta(days=14)
    acc: dict[str, dict] = {}
    for it in items:
        if not it.label:
            continue
        if it.date >= now + timedelta(days=1) or it.date < prev:
            continue
        m = acc.setdefault(it.label, {"label": it.label, "this_week": 0, "last_week": 0, "approved": 0,
                                      "students": {}, "topics": set()})
        if it.date >= week:
            m["this_week"] += 1
            st = students.get(it.student_id)
            if st:
                m["students"][st.id] = {"username": st.username, "name": st.name}
            m["topics"].update(it.topics)
            m["approved"] += int(it.approved)
        else:
            m["last_week"] += 1
    out = []
    for m in acc.values():
        out.append({"label": m["label"], "this_week": m["this_week"], "last_week": m["last_week"],
                    "change": m["this_week"] - m["last_week"], "approved": m["approved"],
                    "students": sorted(m["students"].values(), key=lambda s: (s["name"], s["username"])),
                    "topics": sorted(m["topics"])})
    out.sort(key=lambda m: (-m["this_week"], -m["last_week"], m["label"]))
    return out[:limit]


def class_dashboard(items: list[Item], students: list[Student], now: datetime) -> dict:
    sdict = {s.id: s for s in students}
    items = [it for it in items if it.student_id in sdict]
    topics = topics_of(it for it in items if it.awarded is not None)
    by_student = _by(items, lambda it: [it.student_id])
    cells: list[dict] = []
    rows: list[dict] = []
    for s in students:
        mine = by_student.get(s.id, [])
        by_topic = _by(mine, lambda it: it.topics)
        row_cells = {}
        for t in topics:
            pct, n = mastery(by_topic.get(t, []), now)
            if n:
                d = trend([a["percent"] for a in per_assignment(by_topic[t])])
                row_cells[t] = {"mastery": pct, "n": n, "level": level(pct), "trend": d}
                cells.append({"student_id": s.id, "topic": t, "mastery": pct, "n": n})
        overall, n_all = mastery(mine, now)
        series = per_assignment(mine)
        d = trend([a["percent"] for a in series])
        rows.append({"student_id": s.id, "username": s.username, "name": s.name, "mastery": overall,
                     "level": level(overall), "n": n_all, "assignments": len(series), "trend": d,
                     "trend_label": trend_label(d), "cells": row_cells})
    # class mastery per topic = mean of the students' masteries (each student counts once)
    topic_rows = []
    for t in topics:
        vals = [r["cells"][t]["mastery"] for r in rows if t in r["cells"] and r["cells"][t]["mastery"] is not None]
        struggling = [r for r in rows if t in r["cells"] and r["cells"][t]["level"] == "struggling"]
        avg = r1(sum(vals) / len(vals)) if vals else None
        topic_rows.append({"topic": t, "mastery": avg, "level": level(avg), "students": len(vals),
                           "struggling": [{"username": r["username"], "name": r["name"],
                                           "mastery": r["cells"][t]["mastery"]} for r in struggling]})
    mis = misconceptions(items, now, sdict)
    actions = topic_actions(topic_rows, mis)
    slipping = sorted([r for r in rows if r["trend_label"] == "slipping"], key=lambda r: r["trend"])
    improving = sorted([r for r in rows if r["trend_label"] == "improving"], key=lambda r: -r["trend"])
    brief = lambda r: {"username": r["username"], "name": r["name"], "trend": r["trend"],  # noqa: E731
                       "mastery": r["mastery"]}
    return {"topics": topic_rows, "students": rows, "misconceptions": mis, "actions": actions,
            "slipping": [brief(r) for r in slipping], "improving": [brief(r) for r in improving]}


def topic_actions(topic_rows: list[dict], mis: list[dict]) -> list[dict]:
    out = []
    for t in topic_rows:
        if t["mastery"] is None:
            continue
        n_str = len(t["struggling"])
        if not (t["mastery"] < ACTION_BELOW or n_str >= 2):
            continue
        reteach = t["mastery"] < RETEACH_BELOW or (t["students"] and n_str / t["students"] >= RETEACH_SHARE)
        top = next((m for m in mis if t["topic"] in m["topics"] and m["this_week"] > 0), None)
        kind = "reteach" if reteach else "practice"
        text = (f"Re-teach {t['topic']} to the class" if reteach
                else f"Targeted practice on {t['topic']} for {n_str} student{'s' if n_str != 1 else ''}")
        if top:
            text += f" — common mistake this week: {top['label']}"
        out.append({"topic": t["topic"], "kind": kind, "mastery": t["mastery"], "text": text,
                    "students": [s["username"] for s in t["struggling"]],
                    "misconception": top["label"] if top else None})
    out.sort(key=lambda a: (a["kind"] != "reteach", a["mastery"]))
    return out


# ---------------------------------------------------------------- one student
def student_detail(items: list[Item], now: datetime) -> dict:
    by_topic = _by(items, lambda it: it.topics)
    topics = []
    for t in topics_of(it for it in items if it.awarded is not None):
        pct, n = mastery(by_topic[t], now)
        series = per_assignment(by_topic[t])
        d = trend([a["percent"] for a in series])
        topics.append({"topic": t, "mastery": pct, "level": level(pct), "n": n, "trend": d,
                       "trend_label": trend_label(d), "series": series})
    mistakes: dict[str, dict] = {}
    for it in sorted(items, key=lambda x: x.date, reverse=True):
        if not it.label:
            continue
        m = mistakes.setdefault(it.label, {"label": it.label, "count": 0, "topics": set(), "examples": [],
                                           "last_seen": it.date})
        m["count"] += 1
        m["topics"].update(it.topics)
        if len(m["examples"]) < MAX_EXAMPLES:
            m["examples"].append({"exam_id": it.exam_id, "title": it.exam_title, "question_id": it.question_id,
                                  "prompt": it.prompt[:200], "image": it.image, "date": it.date,
                                  "comment": it.comment[:300], "approved": it.approved})
    recurring = sorted(mistakes.values(), key=lambda m: (-m["count"], m["label"]))
    for m in recurring:
        m["topics"] = sorted(m["topics"])
        m["recurring"] = m["count"] >= 2
    overall, n = mastery(items, now)
    series = per_assignment(items)
    d = trend([a["percent"] for a in series])
    return {"mastery": overall, "level": level(overall), "n": n, "trend": d, "trend_label": trend_label(d),
            "assignments": series, "topics": topics, "mistakes": recurring}


def student_view(items: list[Item], now: datetime) -> dict:
    """The student's own "My progress": released, teacher-graded work only (the caller
    filters). Topics improving / to work on."""
    d = student_detail(items, now)
    improving = [t for t in d["topics"] if t["trend_label"] == "improving"]
    work_on = [t for t in d["topics"]
               if (t["level"] == "struggling" or t["trend_label"] == "slipping") and t not in improving]
    strong = [t for t in d["topics"] if t["level"] == "strong" and t not in improving]
    slim = lambda t: {"topic": t["topic"], "mastery": t["mastery"], "trend": t["trend"],  # noqa: E731
                      "trend_label": t["trend_label"], "n": t["n"]}
    return {"mastery": d["mastery"], "level": d["level"], "trend_label": d["trend_label"],
            "improving": [slim(t) for t in improving],
            "work_on": [slim(t) for t in sorted(work_on, key=lambda t: t["mastery"] or 0)],
            "strong": [slim(t) for t in strong],
            "topics": [slim(t) for t in d["topics"]],
            "mistakes": [{"label": m["label"], "count": m["count"], "topics": m["topics"]}
                         for m in d["mistakes"]][:6]}
