"""Pluggable graders for benchmark runs.

A grader is `async def grader(item: dict) -> dict`. It receives one benchmark item:

    {"id", "question": {"type","prompt","points","rubric","options",  # exam-question shape
                        "answer_number","tolerance","accepted_answers","correct_index"},
     "subject", "answer_text", "answer_image": url|None, "image": bytes|None}

and returns at least `{"points": number}`; optional `confidence` (0..1), `explanation`,
`needs_review` (bool). Raise `app.ai.AIUnavailable` (or anything) to record a failed item.
The human mark is never passed to a grader.

Phase 2 registers its step-level grader with `register_grader("phase2", fn,
prompt_version="...")` and can make it the default with `set_default_grader("phase2")`
(or env FAIRPLAY_BENCHMARK_GRADER).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from .. import ai
from ..grading import num

GraderFn = Callable[[dict], Awaitable[dict]]


@dataclass
class Grader:
    name: str
    fn: GraderFn
    prompt_version: str = ""
    needs_ai: bool = True  # default grader needs the AI provider to be on


_GRADERS: dict[str, Grader] = {}
_default_name = "default"


def register_grader(name: str, fn: GraderFn, prompt_version: str = "", needs_ai: bool = True) -> None:
    _GRADERS[name] = Grader(name, fn, prompt_version, needs_ai)


def unregister_grader(name: str) -> None:
    if name != "default":
        _GRADERS.pop(name, None)


def set_default_grader(name: str) -> None:
    global _default_name
    _default_name = name


def default_grader_name() -> str:
    env = (os.environ.get("FAIRPLAY_BENCHMARK_GRADER") or "").strip()
    if env and env in _GRADERS:
        return env
    return _default_name if _default_name in _GRADERS else "default"


def get_grader(name: str | None) -> Grader | None:
    return _GRADERS.get(name or default_grader_name())


def grader_names() -> list[str]:
    return sorted(_GRADERS)


# ---------------------------------------------------------------- default grader
PROMPT_VERSION = "bench-default-1"

SYSTEM = (
    "You are an experienced university examiner marking ONE student answer. Mark it against "
    "the question, the answer key and the rubric exactly as a careful professor would. "
    "Award partial credit only where the rubric allows it. If the handwriting is hard to read "
    "or you are unsure, lower your confidence and set needs_review. Reply with ONE JSON object only."
)

SCHEMA = ('{"points": number, "confidence": number between 0 and 1, "explanation": "one or two '
          'sentences", "needs_review": true|false}')


def _key_text(q: dict) -> str:
    t = q.get("type")
    if t == "numeric" and q.get("answer_number") is not None:
        return f"Correct answer: {q['answer_number']}" + (f" (± {q['tolerance']})" if q.get("tolerance") else "")
    if t == "short" and q.get("accepted_answers"):
        return "Accepted answers: " + "; ".join(q["accepted_answers"])
    if t == "mcq" and isinstance(q.get("correct_index"), int):
        opts = q.get("options") or []
        ci = q["correct_index"]
        return f"Correct option: {opts[ci] if 0 <= ci < len(opts) else ci}"
    return ""


def build_prompt(item: dict) -> str:
    q = item["question"]
    parts = [f"Question type: {q.get('type')}", f"Question: {q.get('prompt', '')}",
             f"Maximum points: {num(float(q.get('points', 1)))}"]
    key = _key_text(q)
    if key:
        parts.append(key)
    rubric = q.get("rubric") or []
    if rubric:
        parts.append("Rubric: " + "; ".join(f"{r.get('label', '')} ({num(float(r.get('points', 0)))} pts)"
                                           for r in rubric if isinstance(r, dict)))
    if item.get("answer_text"):
        parts.append(f"Student answer (typed or transcribed):\n{item['answer_text'][:8000]}")
    if item.get("image") is not None:
        parts.append("The image shows the student's handwritten answer.")
    parts.append(f"Return JSON exactly like: {SCHEMA}")
    return "\n".join(parts)


async def default_grader(item: dict) -> dict:
    images = [item["image"]] if item.get("image") is not None else []
    out = await ai.complete_json(SYSTEM, build_prompt(item), images=images, max_tokens=500)
    return {"points": out.get("points"), "confidence": out.get("confidence"),
            "explanation": str(out.get("explanation") or out.get("comment") or ""),
            "needs_review": bool(out.get("needs_review"))}


register_grader("default", default_grader, PROMPT_VERSION, needs_ai=True)
