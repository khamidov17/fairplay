"""Slice 6 F/G: class insights and student profiles (teacher reports).

Everything here works on plain data so it is easy to test. `load_facts` runs a fixed
number of queries (teacher's exams, then their submitted attempts joined with the student
name) and turns every graded answer into one `Fact`; the report builders below are single
passes over those facts. Fine for 5,000+ attempts.

Scores always come from the shared `grading.grade_attempt` / `exam_questions` helpers, so
teacher overrides, essays and the hidden website-exam item behave exactly like the
gradebook. Pending answers (ungraded essays, website scores not entered yet) are skipped.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .grading import exam_questions, grade_attempt, percent
from .models import Attempt, Exam, User
from .timeutil import iso

UNTAGGED = "untagged"
STRENGTH_MIN = 80.0
WEAK_BELOW = 60.0
MIN_ANSWERS = 2          # strengths / weak topics / weakest topic need at least this many answers
HARDEST_LIMIT = 15
MISTAKE_LIMIT = 20
MISTAKE_EXAMPLES = 3
JACCARD_MIN = 0.5


# ---------------------------------------------------------------- small helpers
def r1(x: float) -> float:
    return round(x, 1)


def question_topics(q: dict, exam_course: str = "") -> list[str]:
    """Topics of a question, lower-case. Old questions have none -> ["untagged"]; the hidden
    website-exam item takes the exam course (or "general") per the contract."""
    raw = q.get("topics") or []
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        for t in raw:
            if isinstance(t, str):
                t = " ".join(t.split()).lower()
                if t and t not in out:
                    out.append(t)
    if out:
        return out
    if q.get("type") == "external":
        return [(exam_course or "").strip().lower() or "general"]
    return [UNTAGGED]


def delivery_of(e: Any) -> str:
    d = getattr(e, "delivery", None)
    if d in ("computer", "paper", "website"):
        return d
    return "website" if getattr(e, "mode", "questions") == "external" else "computer"


def exam_date(e: Any) -> datetime:
    return getattr(e, "scheduled_at", None) or getattr(e, "closed_at", None) or e.created_at


def time_on_task_min(e: Any, a: Any) -> float | None:
    """Minutes between start and submit, only for exams taken on a computer. Context only."""
    if delivery_of(e) != "computer" or getattr(a, "source", None) == "paper":
        return None
    if a.submitted_at is None or a.started_at is None:
        return None
    secs = (a.submitted_at - a.started_at).total_seconds()
    return r1(max(0.0, secs) / 60.0)


@dataclass
class ExamInfo:
    id: str
    title: str
    course: str
    date: datetime
    delivery: str
    total_points: float
    questions: list[dict]
    index: dict[str, int] = field(default_factory=dict)
    topics: list[list[str]] = field(default_factory=list)  # per question, same order


@dataclass
class AttemptInfo:
    id: str
    exam_id: str
    student_id: int
    username: str
    name: str
    score: float
    pending: int
    percent: float
    submitted_at: datetime | None
    time_on_task_min: float | None


@dataclass
class Fact:
    """One graded (non-pending) answer."""
    exam_id: str
    attempt_id: str
    student_id: int
    question_id: str
    topics: list[str]
    awarded: float
    max_points: float


@dataclass
class MistakeItem:
    text: str
    kind: str  # "tag" | "teacher" | "ai"
    exam_id: str
    attempt_id: str
    question_id: str
    student_id: int
    topics: list[str]


@dataclass
class Facts:
    exams: dict[str, ExamInfo]
    attempts: list[AttemptInfo]
    facts: list[Fact]
    mistakes: list[MistakeItem]
    students: dict[int, tuple[str, str]]  # id -> (username, name)


# ---------------------------------------------------------------- mistakes from one answer
def _tags(v: Any) -> list[str]:
    if not isinstance(v, (list, tuple)):
        return []
    return [" ".join(t.split()) for t in v if isinstance(t, str) and t.strip()]


def _draft_holders(a: Any) -> tuple:
    return tuple(h for h in (getattr(a, "ai_drafts", None), getattr(a, "drafts", None))
                 if isinstance(h, dict) and h)


def _draft_for(holders: tuple, qid: str, given: Any, grade: dict | None) -> dict | None:
    """AI draft for one answer, wherever the Scan & Grade code keeps it (defensive)."""
    for holder in holders:
        if isinstance(holder.get(qid), dict):
            return holder[qid]
    if isinstance(given, dict) and isinstance(given.get("ai_draft"), dict):
        return given["ai_draft"]
    if isinstance(grade, dict) and isinstance(grade.get("ai_draft"), dict):
        return grade["ai_draft"]
    return None


def answer_mistakes(row: dict, grade: dict | None, draft: dict | None) -> list[tuple[str, str]]:
    """(text, kind) mistake notes for one answer that lost points. Teacher-approved data
    wins over the AI draft so an approved draft is not counted twice."""
    max_pts = float(row.get("max_points") or 0)
    if row.get("pending"):
        # Not graded yet: only an AI draft that already says points were lost counts.
        if not draft:
            return []
        try:
            if float(draft.get("points", max_pts)) >= max_pts:
                return []
        except (TypeError, ValueError):
            return []
    elif float(row.get("points_awarded") or 0) >= max_pts - 1e-9:
        return []
    out: list[tuple[str, str]] = []
    tags = _tags((grade or {}).get("mistake_tags")) or _tags((draft or {}).get("mistake_tags"))
    out += [(t, "tag") for t in tags]
    teacher_comment = ((grade or {}).get("comment") or "").strip() if grade else ""
    if teacher_comment:
        out.append((teacher_comment, "teacher"))
    elif draft and isinstance(draft.get("comment"), str) and draft["comment"].strip():
        c = draft["comment"].strip()
        if not c.lower().startswith("ai is off"):
            out.append((c, "ai"))
    return out


# ---------------------------------------------------------------- loading
async def teacher_exams(session: AsyncSession, teacher_id: int) -> list[Exam]:
    rows = (await session.execute(
        select(Exam).where(Exam.teacher_id == teacher_id, Exam.status != "draft")
        .order_by(Exam.created_at, Exam.id))).scalars().all()
    return [e for e in rows if not getattr(e, "formative", False)]


def exam_infos(exams: Iterable[Exam]) -> dict[str, ExamInfo]:
    out = {}
    for e in exams:
        qs = exam_questions(e)
        out[e.id] = ExamInfo(id=e.id, title=e.title, course=e.course or "", date=exam_date(e),
                             delivery=delivery_of(e), total_points=float(e.total_points or 0),
                             questions=qs, index={q["id"]: i for i, q in enumerate(qs)},
                             topics=[question_topics(q, e.course or "") for q in qs])
    return out


async def load_facts(session: AsyncSession, exams: list[Exam],
                     student_id: int | None = None, with_mistakes: bool = True) -> Facts:
    """Two queries in total, whatever the number of exams/attempts."""
    infos = exam_infos(exams)
    by_id = {e.id: e for e in exams}
    res = Facts(exams=infos, attempts=[], facts=[], mistakes=[], students={})
    if not infos:
        return res
    q = (select(Attempt, User.username, User.name)
         .join(User, User.id == Attempt.student_id)
         .where(Attempt.exam_id.in_(list(infos)), Attempt.status == "submitted"))
    if student_id is not None:
        q = q.where(Attempt.student_id == student_id)
    rows = (await session.execute(q)).all()
    # CPU work off the event loop so live exams on this worker stay responsive.
    await asyncio.to_thread(_build_facts, res, rows, by_id, with_mistakes)
    return res


def _build_facts(res: Facts, rows: list, by_id: dict[str, Exam], with_mistakes: bool) -> None:
    infos = res.exams
    for a, username, name in rows:
        info = infos[a.exam_id]
        e = by_id[a.exam_id]
        grades = a.grades or {}
        answers = a.answers or {}
        score, pending, rows = grade_attempt(info.questions, answers, grades)
        res.students[a.student_id] = (username, name)
        res.attempts.append(AttemptInfo(
            id=a.id, exam_id=a.exam_id, student_id=a.student_id, username=username, name=name,
            score=score, pending=pending, percent=percent(score, info.total_points),
            submitted_at=a.submitted_at, time_on_task_min=time_on_task_min(e, a)))
        holders = _draft_holders(a) if with_mistakes else ()
        for i, (qd, row) in enumerate(zip(info.questions, rows)):
            topics = info.topics[i]
            qid = qd["id"]
            if not row["pending"]:
                res.facts.append(Fact(info.id, a.id, a.student_id, qid, topics,
                                      float(row["points_awarded"]), float(row["max_points"] or 0)))
            if not with_mistakes or (not row["pending"] and row["correct"]):
                continue
            g = grades.get(qid) if isinstance(grades, dict) else None
            draft = _draft_for(holders, qid, answers.get(qid) if isinstance(answers, dict) else None, g)
            for text, kind in answer_mistakes(row, g, draft):
                res.mistakes.append(MistakeItem(text, kind, info.id, a.id, qid, a.student_id, topics))


# ---------------------------------------------------------------- topic aggregates
class _Acc:
    __slots__ = ("got", "max", "n", "students")

    def __init__(self):
        self.got = 0.0
        self.max = 0.0
        self.n = 0
        self.students: set[int] = set()

    def add(self, f: Fact) -> None:
        self.got += f.awarded
        self.max += f.max_points
        self.n += 1
        self.students.add(f.student_id)

    @property
    def pct(self) -> float | None:
        return r1(self.got / self.max * 100) if self.max > 0 else None


def _chrono(exams: dict[str, ExamInfo]) -> list[ExamInfo]:
    return sorted(exams.values(), key=lambda e: (e.date, e.id))


def topic_rows(f: Facts) -> list[dict]:
    """Per topic: avg over all graded answers + per-exam breakdown (heatmap cells) +
    trend = last exam's avg minus the average of the earlier exams' avgs."""
    total: dict[str, _Acc] = defaultdict(_Acc)
    per_exam: dict[str, dict[str, _Acc]] = defaultdict(lambda: defaultdict(_Acc))
    for fact in f.facts:
        for t in fact.topics:
            total[t].add(fact)
            per_exam[t][fact.exam_id].add(fact)
    order = _chrono(f.exams)
    out = []
    for t, acc in total.items():
        if acc.pct is None:
            continue
        cells = [(e, per_exam[t][e.id]) for e in order if e.id in per_exam[t]]
        cells = [(e, c) for e, c in cells if c.pct is not None]
        trend = None
        if len(cells) >= 2:
            earlier = [c.pct for _, c in cells[:-1]]
            trend = r1(cells[-1][1].pct - sum(earlier) / len(earlier))
        out.append({"topic": t, "avg_percent": acc.pct, "n_answers": acc.n,
                    "n_students": len(acc.students), "trend": trend, "untagged": t == UNTAGGED,
                    "by_exam": [{"exam_id": e.id, "avg_percent": c.pct, "n_answers": c.n}
                                for e, c in cells]})
    out.sort(key=lambda r: (r["untagged"], r["avg_percent"], r["topic"]))
    return out


def hardest_questions(f: Facts, limit: int = HARDEST_LIMIT) -> list[dict]:
    acc: dict[tuple[str, str], _Acc] = defaultdict(_Acc)
    for fact in f.facts:
        acc[(fact.exam_id, fact.question_id)].add(fact)
    rows = []
    for (eid, qid), a in acc.items():
        if a.max <= 0 or a.got >= a.max:
            continue
        info = f.exams[eid]
        i = info.index.get(qid, 0)
        q = info.questions[i]
        if q.get("type") == "external":  # the website score is not a question to reteach
            continue
        rows.append({"exam_id": eid, "exam_title": info.title, "course": info.course,
                     "question_id": qid, "question_index": i, "type": q.get("type"),
                     "prompt": str(q.get("prompt") or "")[:300],
                     "topics": question_topics(q, info.course),
                     "avg_percent": a.pct, "lost_points": r1(a.max - a.got), "n_answers": a.n})
    rows.sort(key=lambda r: (-r["lost_points"], r["avg_percent"], r["exam_id"], r["question_index"]))
    return rows[:limit]


# ---------------------------------------------------------------- mistake clustering
_STOP = frozenset("""a an the and or but of to in on at for with by from as is are was were be been
being it its this that these those there their they them he she his her i you your we our not no
did does do done has have had into than then so very too also just only all any some can could
should would will may might must one more most less over under up down out about after before
again same such student answer answers question""".split())
_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> frozenset[str]:
    out = set()
    for t in _TOKEN.findall(text.lower()):
        if t in _STOP:  # single letters stay: "+C", "x", "n" carry meaning in math
            continue
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.add(t)
        if len(out) >= 40:
            break
    return frozenset(out)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def cluster_mistakes(f: Facts, limit: int = MISTAKE_LIMIT,
                     threshold: float = JACCARD_MIN) -> list[dict]:
    """Greedy clustering on normalized-token Jaccard. Identical token sets are merged first;
    then each distinct set (most frequent first, tags before comments) joins the most
    similar existing cluster (compared with its seed set, found via an inverted index) or
    starts a new one."""
    groups: dict[frozenset, list[MistakeItem]] = defaultdict(list)
    cache: dict[str, frozenset] = {}
    for m in f.mistakes:
        tk = cache.get(m.text)
        if tk is None:
            tk = cache[m.text] = tokens(m.text)
        if tk:
            groups[tk].append(m)
    kind_rank = {"tag": 0, "teacher": 1, "ai": 2}

    def key_order(item):
        tk, items = item
        return (-len(items), min(kind_rank.get(i.kind, 3) for i in items), sorted(tk))

    seeds: list[frozenset] = []
    members: list[list[MistakeItem]] = []
    index: dict[str, list[int]] = defaultdict(list)
    for tk, items in sorted(groups.items(), key=key_order):
        cand = set()
        for t in tk:
            cand.update(index.get(t, ()))
        best, best_s = None, threshold
        for c in cand:
            s = jaccard(tk, seeds[c])
            if s >= best_s and (best is None or s > best_s or c < best):
                best, best_s = c, s
        if best is None:
            best = len(seeds)
            seeds.append(tk)
            members.append([])
            for t in tk:
                index[t].append(best)
        members[best].extend(items)

    out = []
    for items in members:
        answers = {}
        for m in items:
            answers.setdefault((m.attempt_id, m.question_id), m)
        # Label: a mistake tag if any, else a teacher's comment, else the AI's wording.
        texts = (Counter(m.text for m in items if m.kind == "tag")
                 or Counter(m.text for m in items if m.kind == "teacher")
                 or Counter(m.text for m in items))
        label = sorted(texts.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0]
        topic_c = Counter(t for m in answers.values() for t in m.topics if t != UNTAGGED)
        examples = []
        for m in list(answers.values())[:MISTAKE_EXAMPLES]:
            info = f.exams[m.exam_id]
            username, name = f.students.get(m.student_id, ("", ""))
            examples.append({"exam_id": m.exam_id, "exam_title": info.title,
                             "attempt_id": m.attempt_id, "question_id": m.question_id,
                             "question_index": info.index.get(m.question_id, 0),
                             "username": username, "name": name,
                             "text": m.text[:300], "source": m.kind})
        kinds = Counter(m.kind for m in items)
        out.append({"label": label[:120], "count": len(answers), "examples": examples,
                    "topics": [t for t, _ in topic_c.most_common(3)],
                    "sources": {"tag": kinds.get("tag", 0), "teacher": kinds.get("teacher", 0),
                                "ai": kinds.get("ai", 0)}})
    out.sort(key=lambda m: (-m["count"], m["label"]))
    return out[:limit]


# ---------------------------------------------------------------- reports
def insights_report(f: Facts) -> dict:
    return {
        "exams": [{"exam_id": e.id, "title": e.title, "course": e.course, "date": iso(e.date),
                   "delivery": e.delivery} for e in _chrono(f.exams)],
        "topics": topic_rows(f),
        "hardest": hardest_questions(f),
        "mistakes": cluster_mistakes(f),
        "attempt_count": len(f.attempts),
    }


def _student_topic_accs(f: Facts) -> dict[int, dict[str, _Acc]]:
    out: dict[int, dict[str, _Acc]] = defaultdict(lambda: defaultdict(_Acc))
    for fact in f.facts:
        for t in fact.topics:
            out[fact.student_id][t].add(fact)
    return out


def weakest_topic(accs: dict[str, _Acc]) -> dict | None:
    best = None
    for t, a in accs.items():
        if t == UNTAGGED or a.n < MIN_ANSWERS or a.pct is None:
            continue
        if best is None or (a.pct, t) < (best["avg_percent"], best["topic"]):
            best = {"topic": t, "avg_percent": a.pct}
    return best


def students_report(f: Facts, extra_students: dict[int, tuple[str, str]]) -> list[dict]:
    by_student: dict[int, list[AttemptInfo]] = defaultdict(list)
    for a in f.attempts:
        by_student[a.student_id].append(a)
    accs = _student_topic_accs(f)
    everyone = dict(extra_students)
    everyone.update(f.students)
    out = []
    for sid, (username, name) in everyone.items():
        atts = by_student.get(sid, [])
        graded = [a.percent for a in atts if a.pending == 0]
        last = max((a.submitted_at for a in atts if a.submitted_at), default=None)
        out.append({"username": username, "name": name, "exam_count": len(atts),
                    "average_percent": r1(sum(graded) / len(graded)) if graded else None,
                    "weakest_topic": weakest_topic(accs.get(sid, {})),
                    "last_exam_at": iso(last)})
    out.sort(key=lambda s: (s["name"].lower(), s["username"]))
    return out


def profile_report(f: Facts, student: dict) -> dict:
    atts = sorted(f.attempts, key=lambda a: (a.submitted_at or f.exams[a.exam_id].date, a.exam_id))
    when = {a.exam_id: (a.submitted_at or f.exams[a.exam_id].date) for a in atts}
    total: dict[str, _Acc] = defaultdict(_Acc)
    per_exam: dict[str, dict[str, _Acc]] = defaultdict(lambda: defaultdict(_Acc))
    for fact in f.facts:
        for t in fact.topics:
            total[t].add(fact)
            per_exam[t][fact.exam_id].add(fact)
    topics = []
    for t, acc in total.items():
        if acc.pct is None:
            continue
        points = sorted(((when.get(eid) or f.exams[eid].date, eid, c) for eid, c in per_exam[t].items()
                         if c.pct is not None), key=lambda x: (x[0], x[1]))
        topics.append({"topic": t, "avg_percent": acc.pct, "n": acc.n, "untagged": t == UNTAGGED,
                       "trend": [{"date": iso(d), "percent": c.pct, "exam_id": eid,
                                  "title": f.exams[eid].title} for d, eid, c in points]})
    topics.sort(key=lambda r: (r["untagged"], r["avg_percent"], r["topic"]))
    eligible = [r for r in topics if not r["untagged"] and r["n"] >= MIN_ANSWERS]
    strengths = [r["topic"] for r in sorted(eligible, key=lambda r: (-r["avg_percent"], r["topic"]))
                 if r["avg_percent"] >= STRENGTH_MIN]
    weak = [r["topic"] for r in eligible if r["avg_percent"] < WEAK_BELOW]
    exams = []
    for a in atts:
        info = f.exams[a.exam_id]
        exams.append({"exam_id": a.exam_id, "attempt_id": a.id, "title": info.title,
                      "course": info.course, "delivery": info.delivery,
                      "date": iso(a.submitted_at or info.date),
                      "score": r1(a.score) if a.score is not None else None,
                      "total_points": info.total_points,
                      "percent": a.percent, "state": "needs_grading" if a.pending else "graded",
                      "time_on_task_min": a.time_on_task_min})
    graded = [a.percent for a in atts if a.pending == 0]
    return {"student": student, "topics": topics, "strengths": strengths, "weak": weak,
            "exams": exams,
            "average_percent": r1(sum(graded) / len(graded)) if graded else None,
            "thresholds": {"strength_min": STRENGTH_MIN, "weak_below": WEAK_BELOW,
                           "min_answers": MIN_ANSWERS}}
