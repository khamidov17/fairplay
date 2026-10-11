"""Practice item generation: variations of the teacher's own questions.

Rules (CONTRACT slice 6 H):
* numeric — numbers in the prompt are detected and perturbed; the new answer is computed by
  SymPy from a formula (AI-provided, read from an arithmetic expression in the prompt, or a
  small template search). A formula is only trusted when it reproduces the teacher's own
  answer for the original numbers. A formula that does NOT reproduce it is dropped; an AI
  rewrite whose answer cannot be checked becomes "needs_answer" for the teacher.
* mcq — AI rewrite that keeps the correct option text, else shuffled options.
* short — AI rewrite when AI is on (math answers must pass a SymPy check), else the original.
* essay — AI rewrite when AI is on, else the original as a review item.
Nothing reaches students before the teacher approves the set.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from .. import ai, mathcheck
from ..grading import normalize_short
from ..mathcheck import MathError
from .weak import question_topics

log = logging.getLogger("fairplay.practice")

MAX_PROMPT = 5000
MAX_OPTION = 500
MAX_EXPLANATION = 2000
MAX_NUMBERS = 6
AI_CONCURRENCY = 4

SYSTEM = ("You help a teacher make practice variations of their own exam questions for one student. "
          "Keep the same skill and difficulty. Reply with ONE JSON object only, no prose.")


def new_item_id() -> str:
    return "pi_" + secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")


# ---------------------------------------------------------------- numbers in text
@dataclass
class Num:
    start: int
    end: int
    text: str
    value: float
    decimals: int


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def find_numbers(prompt: str) -> list[Num]:
    """Numbers that may be changed: not part of a word ("x2", "H2O"), not an exponent ("^2")."""
    out: list[Num] = []
    for m in _NUM_RE.finditer(prompt):
        s, e = m.start(), m.end()
        before = prompt[s - 1] if s > 0 else ""
        after = prompt[e] if e < len(prompt) else ""
        if before.isalpha() or before in "_^." or after == "." and prompt[e + 1:e + 2].isdigit():
            continue
        if prompt[max(0, s - 2):s] == "**":
            continue
        if after.isalpha() and prompt[e:e + 2].lower() in ("st", "nd", "rd", "th"):
            continue  # ordinals ("3rd")
        if before == "-" and (s < 2 or prompt[s - 2] in " (=,\t\n"):
            s -= 1  # attached minus sign
        text = prompt[s:e]
        dec = len(m.group(0).split(".")[1]) if "." in m.group(0) else 0
        out.append(Num(s, e, text, float(text), dec))
        if len(out) >= MAX_NUMBERS:
            break
    return out


def fmt_value(v: float, decimals: int) -> str:
    if decimals <= 0:
        return str(int(round(v)))
    return f"{v:.{decimals}f}"


def replace_numbers(prompt: str, nums: list[Num], values: list[float]) -> str:
    out, pos = [], 0
    for n, v in zip(nums, values):
        out.append(prompt[pos:n.start])
        out.append(fmt_value(v, n.decimals))
        pos = n.end
    out.append(prompt[pos:])
    return "".join(out)


def _perturb_one(n: Num, rng: random.Random) -> float:
    scale = 10 ** n.decimals
    iv = int(round(n.value * scale))
    if abs(n.value) <= 1 and n.decimals == 0:
        return n.value  # 0 and 1 are usually structural
    d = max(1, int(round(abs(iv) * 0.4)))
    for _ in range(20):
        cand = iv + rng.randint(-d, d)
        if cand == iv:
            continue
        if iv > 0 and cand <= (scale if n.decimals == 0 else 0):
            continue
        if iv < 0 and cand >= 0:
            continue
        return cand / scale
    return n.value + (1 if n.value >= 0 else -1)


def answer_decimals(v: float) -> int:
    for d in range(0, 7):
        if abs(round(v, d) - v) <= 1e-9 * max(1.0, abs(v)):
            return d
    return 7


# ---------------------------------------------------------------- formulas
def _template_candidates(k: int) -> list[str]:
    n = [f"n{i}" for i in range(1, k + 1)]
    if k == 0:
        return []
    if k == 1:
        a = n[0]
        return [f"{a}^2", f"sqrt({a})", f"{a}^3", f"2*{a}", f"{a}/2", f"pi*{a}^2", f"2*pi*{a}", f"{a}*({a}+1)/2"]
    c = [" + ".join(n), " * ".join(n), "(" + " + ".join(n) + f")/{k}"]
    if k == 2:
        a, b = n
        c += [f"{a} - {b}", f"{b} - {a}", f"{a}/{b}", f"{b}/{a}", f"{a}^{b}", f"{a}*{b}/2",
              f"2*({a} + {b})", f"sqrt({a}^2 + {b}^2)", f"sqrt({a}^2 - {b}^2)", f"sqrt({b}^2 - {a}^2)",
              f"{a}*{b}/100", f"{a}*(1 + {b}/100)", f"{a}*(1 - {b}/100)", f"pi*{a}^2*{b}", f"{a}^2*{b}"]
    elif k == 3:
        a, b, d = n
        c += [f"{a}*{b}/{d}", f"({a} + {b})*{d}", f"{a}*({b} + {d})", f"{a} + {b} - {d}", f"{a} - {b} - {d}",
              f"({a} + {b})*{d}/2", f"{a}*{b} + {d}", f"{a}*{b} - {d}", f"{a}*{b}*{d}/2"]
    return c


_EXPR_CHUNK = re.compile(r"[-(]?\d[\d\s.+\-*/^×÷−()]*[\d)]")


def _formula_from_prompt(prompt: str, nums: list[Num]) -> str | None:
    """An arithmetic expression written in the prompt ("What is 12 × 7 + 3?")."""
    best = None
    for m in _EXPR_CHUNK.finditer(prompt):
        chunk = m.group(0)
        if not re.search(r"\d\s*[+\-*/^×÷−]\s*[\d(]", chunk):
            continue
        inside = [(i, n) for i, n in enumerate(nums) if n.start >= m.start() and n.end <= m.end()]
        if not inside:
            continue
        out, pos = [], m.start()
        for i, n in inside:
            out.append(prompt[pos:n.start])
            out.append(f"(n{i + 1})")
            pos = n.end
        out.append(prompt[pos:m.end()])
        f = "".join(out).strip()
        if best is None or len(f) > len(best):
            best = f
    return best


def _values_map(values: list[float], nums: list[Num]) -> dict[str, str]:
    return {f"n{i + 1}": fmt_value(v, n.decimals) for i, (n, v) in enumerate(zip(nums, values))}


def _eval(formula: str, values: list[float], nums: list[Num]) -> float | None:
    try:
        v = mathcheck.evaluate_formula(formula, _values_map(values, nums))
    except MathError:
        return None
    return v if math.isfinite(v) else None


def _matches(formula: str, nums: list[Num], answer: float, tol: float) -> bool:
    v = _eval(formula, [n.value for n in nums], nums)
    return v is not None and abs(v - answer) <= max(tol, 1e-6 * max(1.0, abs(answer)))


def _pretty_formula(formula: str, values: dict[str, str]) -> str:
    s = formula
    for k in sorted(values, key=len, reverse=True):
        s = s.replace(k, values[k])
    s = s.replace("(" + "", "(").replace("*", " × ").replace("  ", " ")
    return re.sub(r"\((-?[\d.]+)\)", r"\1", s)


# ---------------------------------------------------------------- AI helpers
def _clip(s: Any, n: int) -> str:
    return str(s if s is not None else "").strip()[:n]


async def _ai(prompt: str) -> dict | None:
    if not ai.enabled():
        return None
    try:
        return await ai.complete_json(SYSTEM, prompt)
    except ai.AIUnavailable as e:
        log.info("practice AI unavailable: %s", e)
        return None


@dataclass
class GenContext:
    rng: random.Random
    ai_on: bool
    sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(AI_CONCURRENCY))
    ai_cache: dict = field(default_factory=dict)
    dropped: list = field(default_factory=list)   # [{"question_id","reason"}]

    async def ai_once(self, key: str, prompt: str) -> dict | None:
        """One AI call per (question, kind) per generate request."""
        if not self.ai_on:
            return None
        if key not in self.ai_cache:
            async def run():
                async with self.sem:
                    return await _ai(prompt)
            self.ai_cache[key] = asyncio.ensure_future(run())
        return await self.ai_cache[key]


# ---------------------------------------------------------------- items
def base_item(src: dict, q: dict, kind: str, status: str) -> dict:
    return {
        "id": new_item_id(), "type": q.get("type"), "prompt": q.get("prompt", ""),
        "options": list(q.get("options") or []), "correct_index": q.get("correct_index"),
        "answer_number": q.get("answer_number"), "tolerance": q.get("tolerance"),
        "accepted_answers": list(q.get("accepted_answers") or []),
        "math": False, "rubric": [dict(r) for r in q.get("rubric") or []],
        "explanation": _clip(q.get("explanation"), MAX_EXPLANATION),
        "topics": question_topics(q, src.get("exam")), "images": list(q.get("images") or [])[:6],
        "status": status, "kind": kind, "verification": None, "note": "",
        "source": {"exam_id": src.get("exam_id"), "exam_title": src.get("exam_title", ""),
                   "question_id": q.get("id")},
    }


def original_item(src: dict) -> dict:
    """The teacher's own question, unchanged (their answer key)."""
    q = src["q"]
    it = base_item(src, q, "original", "ready")
    if q.get("type") == "short":
        it["math"] = _all_math(it["accepted_answers"])
    if q.get("type") == "numeric" and q.get("answer_number") is None:
        it["status"] = "needs_answer"
    if q.get("type") == "mcq" and not isinstance(q.get("correct_index"), int):
        it["status"] = "needs_answer"
    return it


def _all_math(answers: list) -> bool:
    return bool(answers) and all(mathcheck.looks_like_math(a) for a in answers)


async def numeric_variation(src: dict, ctx: GenContext, slot: int) -> dict | None:
    q = src["q"]
    prompt = q.get("prompt", "")
    ans = q.get("answer_number")
    if ans is None or q.get("images"):
        return None  # no key, or a figure that would no longer match changed numbers
    ans = float(ans)
    tol = float(q.get("tolerance") or 0)
    nums = find_numbers(prompt)
    if not nums:
        return None
    k = len(nums)

    labels = ", ".join(f"n{i + 1} = {n.text}" for i, n in enumerate(nums))
    data = await ctx.ai_once(f"num:{src['exam_id']}:{q.get('id')}", (
        f"Question: {prompt}\nCorrect answer: {mathcheck.format_number(ans)}\n"
        f"The numbers in the question are labelled: {labels}.\n"
        "Return JSON {\"formula\": \"expression using n1..n%d that computes the correct answer, e.g. n1*n2/2\", "
        "\"template\": \"the question reworded (a new everyday context is welcome) with placeholders "
        "{n1}, {n2} ... where the numbers go\", \"explanation\": \"one or two sentences on the method, "
        "using the same placeholders\"}" % k))
    return await asyncio.to_thread(_numeric_finish, src, ctx, slot, data, nums, ans, tol)


def _numeric_finish(src: dict, ctx: GenContext, slot: int, data: dict | None, nums: list[Num],
                    ans: float, tol: float) -> dict | None:
    """CPU part (SymPy) of a numeric variation — runs in a worker thread."""
    q = src["q"]
    prompt = q.get("prompt", "")
    k = len(nums)
    formula, method, template, explanation_t = None, None, None, ""
    ai_unverifiable = False
    if data:
        f = _clip(data.get("formula"), 300)
        if f:
            v = _eval(f, [n.value for n in nums], nums)
            if v is None:
                ai_unverifiable = True
            elif abs(v - ans) <= max(tol, 1e-6 * max(1.0, abs(ans))):
                formula, method = f, "ai_formula"
                t = _clip(data.get("template"), MAX_PROMPT)
                used = set(re.findall(r"n\d", f))
                placeholders = set(re.findall(r"\{(n\d)\}", t))
                if t and used <= placeholders and placeholders <= {f"n{i}" for i in range(1, k + 1)}:
                    template = t
                explanation_t = _clip(data.get("explanation"), MAX_EXPLANATION)
            else:
                ctx.dropped.append({"question_id": q.get("id"),
                                    "reason": f"AI formula {f} gives {mathcheck.format_number(v)}, "
                                              f"not {mathcheck.format_number(ans)}"})
        else:
            ai_unverifiable = True

    candidates: list[str] = []
    if formula is None:
        f = _formula_from_prompt(prompt, nums)
        if f and _matches(f, nums, ans, tol):
            formula, method = f, "prompt_expression"
    if formula is None:
        candidates = [c for c in _template_candidates(k) if _matches(c, nums, ans, tol)]
        if candidates:
            formula, method = candidates[0], "template"

    rng = random.Random(ctx.rng.random() + slot)
    if formula is None:
        if ai_unverifiable and data and _clip(data.get("template"), MAX_PROMPT):
            # The AI rewrote the question but gave no usable formula: the teacher must answer it.
            values = [_perturb_one(n, rng) for n in nums]
            t = _clip(data.get("template"), MAX_PROMPT)
            vm = _values_map(values, nums)
            filled = re.sub(r"\{(n\d)\}", lambda m: vm.get(m.group(1), m.group(0)), t)
            it = base_item(src, q, "ai_rewrite", "needs_answer")
            it.update(prompt=filled, answer_number=None,
                      note="The AI could not give a formula SymPy can check — please enter the answer.")
            return it
        return None

    best = None
    for _ in range(40):
        values = [_perturb_one(n, rng) for n in nums]
        if values == [n.value for n in nums]:
            continue
        v = _eval(formula, values, nums)
        if v is None or (ans > 0 and v <= 0) or (ans < 0 and v >= 0):
            continue
        if abs(v - ans) <= 1e-9 * max(1.0, abs(ans)):
            continue  # same answer as the original (e.g. 3×4 -> 4×3): not a real variation
        if method == "template" and len(candidates) > 1:
            others = {round(x, 9) for x in (_eval(c, values, nums) for c in candidates[1:]) if x is not None}
            if others - {round(v, 9)}:
                continue  # two formulas fit the original numbers but disagree here: ambiguous
        dec = answer_decimals(v)
        if best is None or dec < best[2]:
            best = (values, v, dec)
        if dec <= max(2, answer_decimals(ans)):
            break
    if best is None:
        if method == "template" and len(candidates) > 1:
            ctx.dropped.append({"question_id": q.get("id"),
                                "reason": "Several formulas fit the original numbers; could not tell which"})
        return None
    values, v, dec = best
    vm = _values_map(values, nums)
    new_prompt = (re.sub(r"\{(n\d)\}", lambda m: vm.get(m.group(1), m.group(0)), template) if template
                  else replace_numbers(prompt, nums, values))
    new_tol = tol
    if dec > 2:
        v = round(v, 2)
        new_tol = max(tol, 0.01)
        new_prompt = new_prompt.rstrip() + " (Round to 2 decimal places.)"
    # Independent re-check: the stored answer equals the formula's value for the new numbers.
    recheck = _eval(formula, values, nums)
    if recheck is None or abs(recheck - v) > max(new_tol, 1e-9 * max(1.0, abs(v))) + 1e-9:
        ctx.dropped.append({"question_id": q.get("id"), "reason": "Answer failed the SymPy re-check"})
        return None
    it = base_item(src, q, "perturbed", "verified")
    explanation = ""
    if explanation_t:
        explanation = re.sub(r"\{(n\d)\}", lambda m: vm.get(m.group(1), m.group(0)), explanation_t)
    working = f"{_pretty_formula(formula, vm)} = {mathcheck.format_number(v)}"
    it.update(prompt=new_prompt[:MAX_PROMPT], answer_number=float(v) if not float(v).is_integer() else int(v),
              tolerance=new_tol, explanation=(explanation + (" " if explanation else "") + "Working: " + working)[
                  :MAX_EXPLANATION],
              verification={"method": "sympy", "how": method, "formula": formula, "values": vm,
                            "original_answer": mathcheck.format_number(ans)})
    return it


def _shuffle_mcq(src: dict, rng: random.Random) -> dict | None:
    q = src["q"]
    opts = list(q.get("options") or [])
    ci = q.get("correct_index")
    if len(opts) < 2 or not isinstance(ci, int) or not 0 <= ci < len(opts):
        return None
    it = base_item(src, q, "shuffled", "ready")
    if [o.strip().lower() for o in opts] in (["true", "false"], ["false", "true"]):
        it["kind"] = "original"
        return it
    order = list(range(len(opts)))
    for _ in range(5):
        rng.shuffle(order)
        if order != list(range(len(opts))):
            break
    it["options"] = [opts[i] for i in order]
    it["correct_index"] = order.index(ci)
    return it


async def mcq_variation(src: dict, ctx: GenContext, slot: int) -> dict | None:
    q = src["q"]
    opts = list(q.get("options") or [])
    ci = q.get("correct_index")
    if not isinstance(ci, int) or not 0 <= ci < len(opts):
        return None
    correct = opts[ci]
    data = await ctx.ai_once(f"mcq:{src['exam_id']}:{q.get('id')}", (
        f"Question: {q.get('prompt', '')}\nOptions: {opts}\nCorrect option: {correct}\n"
        "Rewrite the question (new wording or context) and the wrong options. Keep the correct option "
        f"text EXACTLY \"{correct}\". Return JSON {{\"prompt\": str, \"options\": [str, ...], "
        "\"correct_index\": int, \"explanation\": \"why the correct option is right\"}"))
    if data:
        p = _clip(data.get("prompt"), MAX_PROMPT)
        new_opts = [_clip(o, MAX_OPTION) for o in (data.get("options") or []) if _clip(o, MAX_OPTION)]
        nci = data.get("correct_index")
        ok = (p and 2 <= len(new_opts) <= 8 and isinstance(nci, int) and 0 <= nci < len(new_opts)
              and normalize_short(new_opts[nci]) == normalize_short(correct)
              and len({normalize_short(o) for o in new_opts}) == len(new_opts))
        if ok:
            it = base_item(src, q, "ai_rewrite", "ready")
            it.update(prompt=p, options=new_opts, correct_index=nci,
                      explanation=_clip(data.get("explanation"), MAX_EXPLANATION) or it["explanation"])
            return it
        ctx.dropped.append({"question_id": q.get("id"), "reason": "AI rewrite changed the correct option"})
    return _shuffle_mcq(src, random.Random(ctx.rng.random() + slot))


async def short_variation(src: dict, ctx: GenContext, slot: int) -> dict | None:
    q = src["q"]
    accepted = [str(a) for a in (q.get("accepted_answers") or []) if str(a).strip()]
    if not accepted:
        return None
    if not ctx.ai_on:
        return None
    if await asyncio.to_thread(_all_math, accepted):
        data = await ctx.ai_once(f"short:{src['exam_id']}:{q.get('id')}", (
            f"Question: {q.get('prompt', '')}\nCorrect answer: {accepted[0]}\n"
            "Write a NEW similar question (different numbers or expression, same skill). Return JSON "
            "{\"prompt\": str, \"answer\": \"the answer as a math expression, e.g. 2x+2\", "
            "\"check\": \"a SymPy expression that computes the answer from the question, e.g. "
            "expand((x+1)^2) or diff(x^3, x)\", \"explanation\": \"short worked solution\"}"))
        if not data or not _clip(data.get("prompt"), MAX_PROMPT) or not _clip(data.get("answer"), 200):
            return None
        return await asyncio.to_thread(_short_math_finish, src, ctx, data)
    data = await ctx.ai_once(f"short:{src['exam_id']}:{q.get('id')}", (
        f"Question: {q.get('prompt', '')}\nAccepted answers: {accepted}\n"
        "Reword the question so it tests the same fact in a new way. Return JSON {\"prompt\": str, "
        "\"accepted_answers\": [str, ...], \"explanation\": str}"))
    if not data or not _clip(data.get("prompt"), MAX_PROMPT):
        return None
    acc = [_clip(a, 200) for a in (data.get("accepted_answers") or []) if _clip(a, 200)][:10]
    it = base_item(src, q, "ai_rewrite", "ready" if acc else "needs_answer")
    it.update(prompt=_clip(data.get("prompt"), MAX_PROMPT), accepted_answers=acc,
              explanation=_clip(data.get("explanation"), MAX_EXPLANATION))
    return it


def _short_math_finish(src: dict, ctx: GenContext, data: dict) -> dict | None:
    """SymPy check of an AI-written math short answer — runs in a worker thread."""
    q = src["q"]
    answer = _clip(data.get("answer"), 200)
    it = base_item(src, q, "ai_rewrite", "needs_answer")
    it.update(prompt=_clip(data.get("prompt"), MAX_PROMPT), accepted_answers=[answer], math=True,
              explanation=_clip(data.get("explanation"), MAX_EXPLANATION))
    chk = _clip(data.get("check"), 300)
    if not mathcheck.looks_like_math(answer):
        it.update(accepted_answers=[], note=f"AI suggested “{answer}”, which SymPy cannot read.")
        return it
    if chk:
        try:
            expected = mathcheck.evaluate_check(chk)
        except MathError:
            expected = None
        if expected is not None:
            verdict = mathcheck.check(expected, answer)
            if verdict.equal is False:
                ctx.dropped.append({"question_id": q.get("id"),
                                    "reason": f"AI answer {answer} does not match {chk}"})
                return None
            if verdict.equal is True:
                it.update(status="verified",
                          verification={"method": "sympy", "how": "ai_check", "check": chk,
                                        "result": mathcheck.pretty(expected)})
                return it
    it["note"] = f"AI suggested “{answer}” but it could not be checked — confirm or correct it."
    it["accepted_answers"] = []
    it["suggested_answer"] = answer
    return it


async def essay_variation(src: dict, ctx: GenContext, slot: int) -> dict | None:
    q = src["q"]
    if not ctx.ai_on:
        return None
    data = await ctx.ai_once(f"essay:{src['exam_id']}:{q.get('id')}", (
        f"Essay question: {q.get('prompt', '')}\nRubric: {q.get('rubric') or []}\n"
        "Write a new essay prompt practising the same skill. Return JSON {\"prompt\": str, "
        "\"explanation\": \"what a strong answer includes\"}"))
    if not data or not _clip(data.get("prompt"), MAX_PROMPT):
        return None
    it = base_item(src, q, "ai_rewrite", "ready")
    it.update(prompt=_clip(data.get("prompt"), MAX_PROMPT),
              explanation=_clip(data.get("explanation"), MAX_EXPLANATION))
    return it


_VARIATORS = {"numeric": numeric_variation, "mcq": mcq_variation, "short": short_variation,
              "essay": essay_variation}


async def make_item(src: dict, ctx: GenContext, slot: int, use_original: bool) -> dict | None:
    fn = _VARIATORS.get(src["q"].get("type"))
    if fn is None:
        return None
    it = await fn(src, ctx, slot)
    if it is None and use_original:
        it = await asyncio.to_thread(original_item, src)
    return it


def plan_sources(sources: list[dict], count: int, topics: list[str], wrong: set) -> list[dict]:
    """Order the pool: questions in the weakest topics first, the ones the student got wrong first."""
    rank = {t: i for i, t in enumerate(topics)}

    def key(s: dict):
        ts = [rank[t] for t in s["topics"] if t in rank]
        return (min(ts) if ts else 99, 0 if (s["exam_id"], s["q"].get("id")) in wrong else 1)

    return sorted(sources, key=key)


async def generate_items(sources: list[dict], count: int, topics: list[str], wrong: set,
                         rng: random.Random | None = None) -> tuple[list[dict], list[dict]]:
    """Up to `count` items from `sources` ({"q","exam_id","exam_title","exam","topics"}).
    Returns (items, dropped)."""
    ctx = GenContext(rng=rng or random.Random(secrets.randbits(32)), ai_on=ai.enabled())
    pool = plan_sources(sources, count, topics, wrong)
    if not pool:
        return [], []
    # Round-robin over the pool. Numeric questions can be varied many times; others once each.
    slots: list[tuple[dict, int]] = []
    uses: dict[int, int] = {}
    while len(slots) < count:
        progressed = False
        for i, s in enumerate(pool):
            if len(slots) >= count:
                break
            n = uses.get(i, 0)
            if n > 0 and s["q"].get("type") != "numeric":
                continue
            if n >= 6:
                continue
            uses[i] = n + 1
            slots.append((s, n))
            progressed = True
        if not progressed:
            break

    async def one(idx: int, s: dict, n: int) -> dict | None:
        return await make_item(s, ctx, idx, use_original=(n == 0))

    results = await asyncio.gather(*(one(i, s, n) for i, (s, n) in enumerate(slots)))
    items: list[dict] = []
    seen: set[str] = set()
    for it in results:
        if it is None:
            continue
        sig = normalize_short(it["prompt"]) + "|" + "|".join(it.get("options") or [])
        if sig in seen:
            continue
        seen.add(sig)
        items.append(it)
    return items[:count], ctx.dropped
