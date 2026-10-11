"""Benchmark runs: ask a grader for a mark per benchmark item and store the comparison."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sqlalchemy import select, update

from .. import ai
from ..db import get_database
from ..grading import num
from ..scan.storage import read_url
from ..timeutil import utcnow
from . import metrics
from .graders import get_grader
from .models import BenchmarkItem, BenchmarkRun, BenchmarkRunItem

log = logging.getLogger("fairplay.quality")

CONCURRENCY = 4
_tasks: set[asyncio.Task] = set()


def item_question(i: BenchmarkItem) -> dict:
    """Benchmark item -> exam-question shaped dict (what graders expect)."""
    key = (i.answer_key or "").strip()
    q: dict[str, Any] = {"id": i.id, "type": i.question_type, "prompt": i.question_prompt,
                         "points": num(float(i.max_points)), "rubric": list(i.rubric or []),
                         "options": [], "correct_index": None, "answer_number": None, "tolerance": None,
                         "accepted_answers": [], "answer_key": key}
    if key and i.question_type == "numeric":
        try:
            q["answer_number"] = float(key)
        except ValueError:
            pass
    elif key and i.question_type == "short":
        q["accepted_answers"] = [s.strip() for s in key.split("|") if s.strip()]
    return q


def _clamp(v: Any, max_points: float) -> float | None:
    f = metrics._num(v)
    if f is None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
    return max(0.0, min(float(max_points), f))


def spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def compute_metrics(rows: list[BenchmarkRunItem], items: dict[str, BenchmarkItem], severe: float) -> dict:
    out = metrics.report(metrics_pairs(rows), severe)
    out["failed"] = sum(1 for r in rows if r.ai_points is None)
    # Human vs human (first two marks) where an item has several: the ceiling to compare with.
    hh = []
    for r in rows:
        it = items.get(r.item_id)
        marks = [metrics._num(m) for m in (it.human_marks if it else []) or []]
        marks = [m for m in marks if m is not None]
        if len(marks) >= 2:
            hh.append(metrics.Pair(ai=marks[1], human=marks[0], max_points=float(r.max_points),
                                   question_type=r.question_type, subject=r.subject))
    out["human_vs_human"] = metrics.summarize(hh, severe) if hh else None
    return out


def metrics_pairs(rows: list[BenchmarkRunItem]) -> list[metrics.Pair]:
    return [metrics.Pair(ai=float(r.ai_points), human=float(r.human_points), max_points=float(r.max_points),
                         question_type=r.question_type or "", subject=(r.subject or "").strip().lower(),
                         ref={"item_id": r.item_id})
            for r in rows if r.ai_points is not None]


async def execute_run(run_id: str) -> None:
    db = get_database()
    try:
        async with db.sessionmaker() as s:
            run = await s.get(BenchmarkRun, run_id)
            if run is None:
                return
            items = list((await s.execute(select(BenchmarkItem).where(BenchmarkItem.set_id == run.set_id)
                                          .order_by(BenchmarkItem.created_at, BenchmarkItem.id))).scalars())
            grader = get_grader(run.grader)
            severe = run.severe_fraction
        if grader is None:
            raise RuntimeError(f"Unknown grader {run.grader}")
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(it: BenchmarkItem) -> BenchmarkRunItem:
            async with sem:
                row = BenchmarkRunItem(run_id=run_id, item_id=it.id, question_type=it.question_type,
                                       subject=it.subject or "", max_points=it.max_points,
                                       human_points=it.human_points, explanation="", needs_review=False)
                try:
                    img = await asyncio.to_thread(read_url, it.answer_image) if it.answer_image else None
                    out = await grader.fn({"id": it.id, "question": item_question(it), "subject": it.subject,
                                           "answer_text": it.answer_text or "", "answer_image": it.answer_image,
                                           "image": img})
                    pts = _clamp((out or {}).get("points"), it.max_points)
                    if pts is None:
                        raise ValueError("grader returned no points")
                    row.ai_points = pts
                    conf = metrics._num(out.get("confidence"))
                    row.confidence = None if conf is None else max(0.0, min(1.0, conf))
                    row.explanation = str(out.get("explanation") or "")[:2000]
                    row.needs_review = bool(out.get("needs_review"))
                except Exception as e:  # noqa: BLE001 - one bad item never stops a run
                    row.ai_points = None
                    row.error = str(e)[:500] or e.__class__.__name__
                async with db.sessionmaker() as s2:
                    await s2.execute(update(BenchmarkRun).where(BenchmarkRun.id == run_id)
                                     .values(done_count=BenchmarkRun.done_count + 1))
                    await s2.commit()
                return row

        rows = await asyncio.gather(*(one(it) for it in items))
        async with db.sessionmaker() as s:
            s.add_all(rows)
            run = await s.get(BenchmarkRun, run_id)
            run.metrics = compute_metrics(list(rows), {i.id: i for i in items}, severe)
            run.status = "done"
            run.done_count = len(rows)
            run.finished_at = utcnow()
            await s.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("benchmark run %s failed", run_id)
        async with db.sessionmaker() as s:
            run = await s.get(BenchmarkRun, run_id)
            if run is not None:
                run.status = "failed"
                run.error = str(e)[:500]
                run.finished_at = utcnow()
                await s.commit()


def provider_info() -> tuple[str, str]:
    prov = ai.provider() if ai.enabled() else "none"
    return prov, os.environ.get("FAIRPLAY_AI_MODEL") or ""
