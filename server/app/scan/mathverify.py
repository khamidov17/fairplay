"""Step verification for math / physics answers: check the AI's step marks with SymPy (+ pint units).

The AI transcribes every step of the student's work as LaTeX (prompt `fp-step-grade-v2`, added
to the v1 prompt by `extend_prompt`). This module parses those lines and checks, without
trusting the model:

* each step against the previous one — expressions are algebraically equal; equations have the
  same solutions (both sides transformed alike, solution sets, formula rearrangements);
  `= ...` continuation lines; derivative / integral claims (`\\frac{d}{dx}(x^3) = 3x^2`,
  `\\int_0^1 x^2 dx = 1/3`, `\\int 2x dx = x^2 + C`, `f'(x)` / `y'` / `dy/dx` against an earlier
  `f(x) = ...`); arithmetic (`3 \\cdot 4 = 12`); units and dimensions (`72 km/h = 20 m/s`,
  adding m and s); numeric rounding of written decimals;
* error carried forward: after a wrong step, later steps that correctly follow from the wrong
  value are `ecf` ("correct given the earlier error");
* the final answer against the teacher's key (exact / equivalent forms / tolerance / rounding,
  solution sets, vectors as (x, y) or i/j, units converted, significant figures when asked).

Then `reconcile` merges the verdicts into the AI draft: the verifier wins on algebra and
arithmetic, criteria judged on the checked steps are re-scored (ECF method marks only where the
rubric allows it), disagreements that change points lower the confidence and set needs_review
(`verifier_disagrees`), and full agreement with the key raises the confidence.

Record (inside attempt.ai_drafts[qid], documented in CONTRACT.md "Step verification"):
  steps[i] += {"latex", "verify": "verified_ok"|"verified_error"|"unverifiable", "verify_reason",
               "ecf": bool, "model_status"?, "verifier_disagrees"?}
  criteria[i] += {"steps": [n...], "ecf"?: bool, "model_points"?, "verifier_note"?}
  final_answer = {"latex", "units", "text"} | null
  verification = {"engine", "version", "steps": [{"n","status","reason","ecf","latex"}],
                  "final": {...}|null, "units": {...}|null, "sig_figs": {...}|null,
                  "summary": {"verified_ok","verified_error","unverifiable"},
                  "disagreements": [{"n"|"final", "model", "verifier", "reason", "changes_points"}],
                  "agrees": bool, "confidence_before", "points_before", "seconds"}

Everything here is synchronous and CPU-bound; `apply_async` runs it in a worker thread with
a time budget. Nothing raises: anything we cannot read is "unverifiable".
"""
from __future__ import annotations

import asyncio
import copy
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import sympy
from sympy.core.function import AppliedUndef

from .. import mathcheck
from . import units as U

ENGINE = "sympy"
VERSION = "fp-verify-v1"
PROMPT_VERSION = "fp-step-grade-v2"
OK, ERR, UNK = "verified_ok", "verified_error", "unverifiable"
CALL_BUDGET = 1.5      # seconds for one heavy SymPy call (solve / simplify / doit)
TOTAL_BUDGET = 10.0    # seconds for one answer
MAX_LATEX = 400

try:  # needs antlr4-python3-runtime==4.11 (requirements.txt); else a small built-in converter
    from sympy.parsing.latex import parse_latex as _antlr_parse_latex
    import antlr4  # noqa: F401
    _HAVE_ANTLR = True
except Exception:  # pragma: no cover - depends on the environment
    _antlr_parse_latex = None
    _HAVE_ANTLR = False
_parse_lock = threading.Lock()

REVIEW_TEXT = {"verifier_disagrees": "The math check disagrees with the AI"}


def enabled() -> bool:
    return os.environ.get("FAIRPLAY_STEP_VERIFY", "1").strip().lower() not in ("0", "false", "off", "no")


class Unparseable(ValueError):
    pass


# ====================================================================== LaTeX cleaning
_SPACES = [r"\displaystyle", r"\textstyle", r"\qquad", r"\quad", r"\!", r"\,", r"\;", r"\:", r"\ ", "&", r"\\",
           r"\limits", r"\nolimits"]
_UNICODE = [("−", "-"), ("–", "-"), ("×", r"\times "), ("·", r"\cdot "), ("⋅", r"\cdot "), ("÷", "/"),
            ("π", r"\pi "), ("θ", r"\theta "), ("≤", r"\le "), ("≥", r"\ge "), ("≠", r"\neq "),
            ("²", "^{2}"), ("³", "^{3}"), ("±", r"\pm "), ("∞", r"\infty "), ("Δ", r"\Delta "),
            ("√", r"\sqrt"), ("∫", r"\int "), ("→", r"\to "), ("⇒", r"\Rightarrow ")]
_ARROWS = r"(?:\\Rightarrow|\\implies|\\Longrightarrow|\\therefore|\\to|=>|->|\\rightarrow|\\iff|\\Leftrightarrow)"
_WRAPS = ("vec", "mathbf", "boldsymbol", "bm", "overrightarrow", "mathit", "mathsf", "overline", "underline",
          "boxed", "mathbb", "pmb")
_FUNC_WORDS = ("arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "sin", "cos", "tan", "sec", "csc", "cot",
               "log", "ln", "exp")
_LEAKED = {"mathrm", "text", "vec", "mathbf", "hat", "quad", "left", "right", "operatorname", "frac", "dfrac",
           "displaystyle", "cdot", "times", "textrm", "mbox", "boxed", "pm", "mp", "or", "and", "infty", "cdots",
           "ldots", "dots", "circ", "approx", "sim", "equiv", "propto", "therefore", "implies"}
_OR = "\x00OR\x00"


def _match_brace(s: str, i: int, open_: str = "{", close: str = "}") -> int:
    """Index of the bracket closing s[i] (which is `open_`), or -1."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == open_:
            depth += 1
        elif s[j] == close:
            depth -= 1
            if depth == 0:
                return j
    return -1


def _paren_to_brace_after(s: str, token: str) -> str:
    """`^(x+1)` -> `^{x+1}` / `sqrt(x)` -> `\\sqrt{x}` (plain-text math written by the model)."""
    out, i = [], 0
    while i < len(s):
        if s.startswith(token, i) and i + len(token) < len(s) and s[i + len(token)] == "(":
            j = _match_brace(s, i + len(token), "(", ")")
            if j > 0:
                out.append(token + "{" + s[i + len(token) + 1:j] + "}")
                i = j + 1
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def clean_latex(s: Any) -> str:
    t = str(s or "").strip()
    if len(t) > MAX_LATEX:
        raise Unparseable("line is too long")
    t = t.strip("$").strip()
    for a in (r"\(", r"\)", r"\[", r"\]"):
        if t.startswith(a) or t.endswith(a):
            t = t.replace(a, " ")
    for a, b in _UNICODE:
        t = t.replace(a, b)
    t = re.sub(r"\\(?:left|right|bigl|bigr|Bigl|Bigr|biggl|biggr|big|Big|bigg|Bigg)\s*(?=[()\[\]|.]|\\[{}|])", "", t)
    t = t.replace(r"\left.", "").replace(r"\right.", "").replace(r"\{", "\x01").replace(r"\}", "\x02")
    for sp in _SPACES:
        t = t.replace(sp, " ")
    t = re.sub(r"\\[dt]frac", r"\\frac", t)
    t = t.replace(r"\div", "/").replace(r"\ast", "*").replace(r"\leq", r"\le").replace(r"\geq", r"\ge")
    t = t.replace(r"\ne ", r"\neq ").replace("**", "^")
    t = re.sub(r"\\operatorname\s*\{\s*([A-Za-z]+)\s*\}", r"\\\1", t)
    t = re.sub(r"\\mathrm\s*\{\s*d\s*\}", "d", t)
    t = re.sub(r"\\mathrm\s*\{\s*e\s*\}", "e", t)
    t = re.sub(r"^\s*" + _ARROWS + r"\s*", "", t)
    t = re.sub(r"\s*" + _ARROWS + r"\s*$", "", t)
    t = re.sub(r"(?<![\\A-Za-z])sqrt\s*(?=\()", r"\\sqrt", t)
    t = _paren_to_brace_after(t, r"\sqrt")
    t = _paren_to_brace_after(t, "^")
    t = re.sub(r"(?<![\\A-Za-z])(" + "|".join(_FUNC_WORDS) + r")(?![A-Za-z])", r"\\\1", t)
    t = re.sub(r"(\d)\s*[eE]([+-]?\d+)(?![\d.A-Za-z])", r"\1\\times 10^{\2}", t)   # 1.5e3
    t = re.sub(r"(?<=\d)\s*\*\s*(?=\d)", r" \\cdot ", t)
    t = t.replace("*", r" \cdot ")
    return " ".join(t.split())


def _sub_cmd(t: str, cmds: tuple[str, ...], fn) -> str:
    """Replace \\cmd{...} (nested braces allowed) by fn(match-like object with group(1))."""
    pat = re.compile(r"\\(?:" + "|".join(cmds) + r")\s*\{")
    out, i = [], 0
    while True:
        m = pat.search(t, i)
        if not m:
            out.append(t[i:])
            return "".join(out)
        j = _match_brace(t, m.end() - 1)
        if j < 0:
            raise Unparseable("unbalanced braces")
        out.append(t[i:m.start()])
        inner = t[m.end():j]
        out.append(fn(type("M", (), {"group": staticmethod(lambda k=0, _s=inner: _s)})()))
        i = j + 1


def _extract_markup(t: str, unit_map: dict[str, U.UnitInfo]) -> str:
    """\\text{..}/\\mathrm{..}: units -> placeholder symbols, "or"/"and" -> separators, other words dropped.
    \\hat{i}/\\mathbf{j}... -> unit-vector placeholders; \\vec{v} etc. -> v."""
    t = re.sub(r"\\(?:hat|mathbf|vec|boldsymbol|bm)\s*\{\s*\\?(?:i|imath)\s*\}", r" \\Psi_{1} ", t)
    t = re.sub(r"\\(?:hat|mathbf|vec|boldsymbol|bm)\s*\{\s*\\?(?:j|jmath)\s*\}", r" \\Psi_{2} ", t)
    t = re.sub(r"\\(?:hat|mathbf|vec|boldsymbol|bm)\s*\{\s*k\s*\}", r" \\Psi_{3} ", t)
    t = re.sub(r"\\hat\s*([ijk])(?![A-Za-z])", lambda m: r" \Psi_{" + str("ijk".index(m.group(1)) + 1) + "} ", t)

    def unit_or_word(m: re.Match) -> str:
        inner = m.group(1).strip()
        if inner.lower().strip(" ,.") in ("or", "and", "or else", ","):
            return f" {_OR} "
        info = U.parse_unit(inner)
        if info is not None:
            k = str(len(unit_map) + 1)
            unit_map[k] = info
            return r" \Xi_{" + k + "} "
        if re.fullmatch(r"[A-Za-z]", inner):
            return " " + inner + " "
        return " "

    t = _sub_cmd(t, ("text", "mathrm", "textrm", "mbox", "rm", "textit", "textbf"), unit_or_word)
    for w in _WRAPS:
        t = re.sub(r"\\" + w + r"\s*\{([^{}]*)\}", r" \1 ", t)
        t = re.sub(r"\\" + w + r"\s*([A-Za-z])", r" \1 ", t)
    t = re.sub(r"(?<![A-Za-z\\])(or|and)(?![A-Za-z])", f" {_OR} ", t)
    return " ".join(t.split())


# ====================================================================== parsing one expression
_SAFE_FUNCS = {"sin": sympy.sin, "cos": sympy.cos, "tan": sympy.tan, "sec": sympy.sec, "csc": sympy.csc,
               "cot": sympy.cot, "arcsin": sympy.asin, "arccos": sympy.acos, "arctan": sympy.atan,
               "sinh": sympy.sinh, "cosh": sympy.cosh, "tanh": sympy.tanh, "sqrt": sympy.sqrt,
               "exp": sympy.exp, "log": sympy.log, "ln": sympy.log, "Abs": sympy.Abs,
               "Derivative": sympy.Derivative, "Integral": sympy.Integral, "root": sympy.root}
_GREEK = ("alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta", "theta", "vartheta", "iota",
          "kappa", "lambda", "mu", "nu", "xi", "rho", "sigma", "tau", "upsilon", "phi", "varphi", "chi", "psi",
          "omega", "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Phi", "Psi", "Omega")


def _brace_arg(s: str, i: int) -> tuple[str, int]:
    """Argument of a LaTeX command at s[i:]: {..} or one char. Returns (arg, next index)."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i < len(s) and s[i] == "{":
        j = _match_brace(s, i)
        if j < 0:
            raise Unparseable("unbalanced braces")
        return s[i + 1:j], j + 1
    if i < len(s):
        if s[i] == "\\":
            m = re.match(r"\\[A-Za-z]+", s[i:])
            if m:
                return m.group(0), i + len(m.group(0))
        return s[i], i + 1
    raise Unparseable("missing argument")


def _latex_to_py(s: str) -> str:
    """Small LaTeX -> Python-math converter (used when the antlr parser is missing or fails)."""
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+)", s[i:])
            if not m:
                i += 1
                continue
            name = m.group(1)
            i += len(m.group(0))
            if name == "frac":
                a, i = _brace_arg(s, i)
                b, i = _brace_arg(s, i)
                if a.strip() == "d" and re.fullmatch(r"\s*d\s*([A-Za-z])\s*", b):
                    var = re.fullmatch(r"\s*d\s*([A-Za-z])\s*", b).group(1)
                    rest = s[i:].strip()
                    out.append(f"Derivative({_latex_to_py(rest)}, {var})")
                    i = len(s)
                    continue
                out.append(f"(({_latex_to_py(a)})/({_latex_to_py(b)}))")
            elif name == "sqrt":
                n = None
                if i < len(s) and s[i] == "[":
                    j = s.index("]", i)
                    n, i = s[i + 1:j], j + 1
                a, i = _brace_arg(s, i)
                out.append(f"sqrt({_latex_to_py(a)})" if n is None else
                           f"root({_latex_to_py(a)}, {_latex_to_py(n)})")
            elif name in ("cdot", "times"):
                out.append("*")
            elif name in ("pi",):
                out.append(" pi ")
            elif name == "int":
                m2 = re.match(r"\s*_\s*(\{[^{}]*\}|\S)\s*\^\s*(\{[^{}]*\}|\S)(.*)d([A-Za-z])\s*$", s[i:])
                m3 = re.match(r"(.*)d([A-Za-z])\s*$", s[i:])
                if m2:
                    lo, hi = m2.group(1).strip("{}"), m2.group(2).strip("{}")
                    out.append(f"Integral({_latex_to_py(m2.group(3))}, ({m2.group(4)}, {_latex_to_py(lo)}, "
                               f"{_latex_to_py(hi)}))")
                elif m3:
                    out.append(f"Integral({_latex_to_py(m3.group(1))}, {m3.group(2)})")
                else:
                    raise Unparseable("integral without dx")
                i = len(s)
            elif name in _SAFE_FUNCS or name in _GREEK or name in ("ln",):
                out.append(f" {name}" + ("" if s[i:i + 1] == "_" else " "))
            elif name in ("le", "ge", "lt", "gt", "neq", "in", "pm", "mp"):
                raise Unparseable("relation")
            else:
                raise Unparseable(f"unknown command \\{name}")
        elif c == "{":
            out.append("(")
            i += 1
        elif c == "}":
            out.append(")")
            i += 1
        elif c == "^":
            out.append("**")
            i += 1
        elif c == "_":
            a, i = _brace_arg(s, i + 1)
            if not re.fullmatch(r"[A-Za-z0-9]+", a.strip()):
                raise Unparseable("subscript")
            out.append("_" + a.strip())
        elif c == "|":
            j = s.find("|", i + 1)
            if j < 0:
                raise Unparseable("unbalanced |")
            out.append(f"Abs({_latex_to_py(s[i + 1:j])})")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _fallback_parse(s: str) -> sympy.Basic:
    py = _latex_to_py(s)
    if not re.fullmatch(r"[0-9A-Za-z_+\-*/().,\s]*", py):
        raise Unparseable("unsupported characters")
    names: dict[str, Any] = dict(_SAFE_FUNCS)
    names.update({"pi": sympy.pi, "e": sympy.E})
    for word in set(re.findall(r"[A-Za-z][A-Za-z0-9_]*", py)):
        if word in names:
            continue
        if word in _GREEK or re.fullmatch(r"[A-Za-z](?:_[A-Za-z0-9]+)?", word) or re.fullmatch(r"(Xi|Psi)_\d+", word):
            names[word] = sympy.Symbol(word)
            continue
        if re.fullmatch(r"[A-Za-z]{2,3}", word):  # "xy" written without operator: implicit product
            continue
        raise Unparseable(f"unknown word {word}")
    from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application, parse_expr,
                                            standard_transformations, split_symbols)
    tr = standard_transformations + (split_symbols, implicit_multiplication_application, convert_xor)
    try:
        e = parse_expr(py, local_dict=names, global_dict=dict(mathcheck._GLOBALS), transformations=tr,
                       evaluate=False)
    except Exception as ex:  # noqa: BLE001
        raise Unparseable("could not read the math") from ex
    # names the fallback produced for placeholders: Xi_1 -> Xi_{1} like the antlr parser
    ren = {sym: sympy.Symbol(re.sub(r"^(\w+?)_(\w+)$", r"\1_{\2}", sym.name))
           for sym in getattr(e, "free_symbols", set()) if "_" in sym.name}
    return e.xreplace(ren) if ren else e


def _check_words(s: str) -> None:
    """Prose ("count H and O atoms") is not math: the parsers would read it as a product."""
    bare = re.sub(r"_\{[^{}]*\}", " ", s)
    bare = re.sub(r"\\[A-Za-z]+", " ", bare)
    for w in re.findall(r"[A-Za-z]{3,}", bare):
        raise Unparseable(f"text “{w}” is not math")


def _guard(e: sympy.Basic) -> None:
    for node in sympy.preorder_traversal(e):
        if isinstance(node, (sympy.factorial, sympy.Sum, sympy.Product, sympy.Limit)):
            if isinstance(node, sympy.factorial) and node.args[0].is_number and abs(float(node.args[0])) <= 200:
                continue
            raise Unparseable("sums / limits / large factorials are not checked")
    try:
        mathcheck._check_size(e)
    except mathcheck.MathError as ex:
        raise Unparseable(str(ex)) from ex


_VARIABLE_LETTERS = set("xyzabcdkmnpqrstuvwlh")


def _post(e: sympy.Basic, unit_map: dict[str, U.UnitInfo]) -> sympy.Basic:
    names = {s.name for s in e.free_symbols}
    if names & _LEAKED:
        raise Unparseable("could not read part of the LaTeX")
    rep: dict = {}
    for s in e.free_symbols:
        m = re.fullmatch(r"Xi_\{(\d+)\}", s.name)
        if m:
            info = unit_map.get(m.group(1))
            if info is None:
                raise Unparseable("unit")
            rep[s] = info.expr()
        elif s.name == "e":
            rep[s] = sympy.E
        elif s.name == "pi":
            rep[s] = sympy.pi
    if rep:
        e = e.xreplace(rep)
    # x(x+1) read as a function call -> product
    apps = [a for a in e.atoms(AppliedUndef) if a.func.__name__ in _VARIABLE_LETTERS and len(a.args) == 1]
    for a in apps:
        e = e.xreplace({a: sympy.Mul(sympy.Symbol(a.func.__name__), a.args[0], evaluate=False)})
    return e


def parse_expr_latex(s: str, unit_map: dict[str, U.UnitInfo] | None = None) -> sympy.Basic:
    """One expression / equation part (no top-level '=' handling) -> SymPy. Raises Unparseable."""
    unit_map = unit_map if unit_map is not None else {}
    s = s.strip()
    if not s:
        raise Unparseable("empty")
    _check_words(s)
    e = None
    if _HAVE_ANTLR:
        try:
            with _parse_lock:
                e = _antlr_parse_latex(s, strict=True)
        except Exception:  # noqa: BLE001
            e = None
    if e is None:
        e = _fallback_parse(s)
    if isinstance(e, sympy.Basic) and isinstance(e, sympy.Rel) and not isinstance(e, sympy.Eq):
        raise Unparseable("inequality")
    if not isinstance(e, sympy.Basic):
        raise Unparseable("not math")
    e = _post(e, unit_map)
    _guard(e)
    return e


# ====================================================================== one step
@dataclass
class Step:
    n: int
    src: str
    kind: str = "other"                  # expr | eq | solset | vector | other
    parts: list = field(default_factory=list)   # expr: [e]; eq/chain: [p0, p1, ...] (None = unread)
    leading: bool = False                # line starts with "=": continues the previous value
    sols: list = field(default_factory=list)    # solset: [{name: value}], name None if unnamed
    vec: list = field(default_factory=list)
    has_units: bool = False
    floats_tol: float | None = None      # half unit of the least precise written decimal
    has_float: bool = False

    @property
    def value(self):
        if self.kind == "expr":
            return self.parts[0]
        if self.kind == "eq" and self.parts and self.parts[-1] is not None:
            return self.parts[-1]
        return None


def _split_top(s: str, seps: tuple[str, ...]) -> list[str]:
    """Split at top-level separators (outside (), {}, [])."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c in "({[\x01":
            depth += 1
        elif c in ")}]\x02":
            depth -= 1
        if depth == 0:
            hit = next((x for x in seps if s.startswith(x, i)), None)
            if hit is not None:
                out.append("".join(cur))
                cur = []
                i += len(hit)
                continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def _lone_name(e: Any) -> Any:
    """y / v_{0} / f(x) used as a name on the left of '='."""
    if isinstance(e, sympy.Symbol) and not U.is_base_symbol(e):
        return e
    if isinstance(e, AppliedUndef) and all(isinstance(a, sympy.Symbol) for a in e.args):
        return e
    return None


def _decimals_tol(src: str) -> tuple[bool, float | None]:
    nums = re.findall(r"(?<![\w.])\d+\.(\d+)", src)
    if not nums:
        return False, None
    return True, 0.5 * 10 ** (-min(len(d) for d in nums))


def parse_step(n: int, latex: Any) -> Step | None:
    """Parse one written line. None when nothing in it can be read as math."""
    try:
        t = clean_latex(latex)
    except Unparseable:
        return None
    if not t:
        return None
    unit_map: dict[str, U.UnitInfo] = {}
    try:
        t = _extract_markup(t, unit_map)
    except Exception:  # noqa: BLE001
        return None
    st = Step(n=n, src=t, has_units=bool(unit_map))
    st.has_float, st.floats_tol = _decimals_tol(t)
    if re.search(r"\\(?:le|ge|lt|gt|neq)(?![A-Za-z])|[<>]", t):
        return None
    # x = \pm 2  -> two solutions
    if r"\pm" in t or r"\mp" in t:
        if t.count(r"\pm") + t.count(r"\mp") != 1:
            return None
        a, b = t.replace(r"\mp", r"\pm").split(r"\pm")
        t = f"{a} + {b} {_OR} {a} - {b}" if "=" not in b else t
        if "=" in a:
            name, _, first = a.partition("=")
            t = f"{name} = {first} + ({b}) {_OR} {name} = {first} - ({b})"
    # \{2, 3\}  /  x \in \{2,3\}
    m = re.fullmatch(r"(?:([A-Za-z](?:_\{?\w+\}?)?)\s*\\in\s*)?\x01(.*)\x02", t.strip())
    if m:
        t = _OR.join(f"{m.group(1)} = {v}" if m.group(1) else v for v in _split_top(m.group(2), (",",)))
    t = t.replace("\x01", "(").replace("\x02", ")")
    # vector: (a, b) / \langle a, b \rangle
    vm = re.fullmatch(r"(?:([A-Za-z](?:_\{?\w+\}?)?)\s*=\s*)?(?:\(|\\langle)(.*)(?:\)|\\rangle)", t.strip())
    if vm and len(_split_top(vm.group(2), (",",))) in (2, 3):
        comps = []
        for c in _split_top(vm.group(2), (",",)):
            try:
                comps.append(parse_expr_latex(c, unit_map))
            except Unparseable:
                return None
        st.kind, st.vec = "vector", comps
        return st
    pieces = [p for p in _split_top(t, (_OR, ",", ";")) if p.strip()]
    if len(pieces) > 1:
        sols = _parse_solset(pieces, unit_map)
        if sols is None:
            return None
        st.kind, st.sols = "solset", sols
        return st
    s = pieces[0] if pieces else ""
    leading = s.lstrip().startswith("=")
    parts_src = _split_top(s.lstrip().lstrip("=") if leading else s, ("=",))
    parts = []
    for p in parts_src:
        try:
            parts.append(parse_expr_latex(p, unit_map) if p.strip() else None)
        except Unparseable:
            parts.append(None)
    if all(p is None for p in parts):
        return None
    st.leading = leading
    if leading:
        st.kind, st.parts = "eq", [None] + parts
    elif len(parts) == 1:
        st.kind, st.parts = "expr", parts
    else:
        st.kind, st.parts = "eq", parts
    # a vector written with i/j unit vectors
    if st.kind == "expr" and any(re.fullmatch(r"Psi_\{\d\}", x.name) for x in st.parts[0].free_symbols):
        st.kind, st.vec = "vector", _ij_components(st.parts[0])
        if st.vec is None:
            return None
    elif st.kind == "eq" and st.parts[-1] is not None and \
            any(re.fullmatch(r"Psi_\{\d\}", x.name) for x in st.parts[-1].free_symbols):
        comps = _ij_components(st.parts[-1])
        if comps is None:
            return None
        st.kind, st.vec, st.parts = "vector", comps, []
    return st


def _ij_components(e: sympy.Expr) -> list | None:
    ps = [sympy.Symbol("Psi_{%d}" % k) for k in (1, 2, 3)]
    try:
        ex = sympy.expand(e)
        comps = [ex.coeff(p) for p in ps]
        rest = sympy.expand(ex - sum(c * p for c, p in zip(comps, ps)))
    except Exception:  # noqa: BLE001
        return None
    if rest != 0 or any(c.has(*ps) for c in comps):
        return None
    return comps if comps[2] != 0 else comps[:2]


def _parse_solset(pieces: list[str], unit_map: dict) -> list[dict] | None:
    named: list[tuple[Any, Any]] = []
    last_name = None
    for p in pieces:
        bits = _split_top(p, ("=",))
        try:
            if len(bits) == 2:
                lhs = parse_expr_latex(bits[0], unit_map)
                name = _lone_name(lhs)
                if name is None:
                    return None
                last_name = name
                named.append((name, parse_expr_latex(bits[1], unit_map)))
            elif len(bits) == 1:
                named.append((last_name, parse_expr_latex(bits[0], unit_map)))
            else:
                return None
        except Unparseable:
            return None
    names = [nm for nm, _ in named]
    if len(set(names)) == len(names) and len(names) > 1 and None not in names:
        return [dict(named)]                       # x = 1, y = 2: one joint solution
    return [{nm: v} for nm, v in named]            # x = 2 or x = 3 / 2, 3


# ====================================================================== SymPy helpers (time-limited)
class _Clock:
    def __init__(self, budget: float):
        self.end = time.monotonic() + budget

    def left(self) -> float:
        return self.end - time.monotonic()

    def call(self, fn, *a, t: float = CALL_BUDGET):
        lim = min(t, self.left())
        if lim <= 0.05:
            return None
        try:
            return mathcheck.run_limited(fn, *a, timeout=lim)
        except Exception:  # noqa: BLE001 - MathError (timeout) or SymPy failures
            return None


def _derived_name(e: Any) -> tuple[str, Any] | None:
    """f'(x) / y' -> ("f", arg) / ("y", None)."""
    if isinstance(e, AppliedUndef) and e.func.__name__.endswith("'") and len(e.args) == 1:
        return e.func.__name__.rstrip("'"), e.args[0]
    if isinstance(e, sympy.Symbol) and e.name.endswith("'") and e.name.count("'") == 1:
        return e.name[:-1], None
    return None


class Ctx:
    """Definitions seen so far (f(x) = ..., y = ...) + time budget."""

    def __init__(self, clock: _Clock, physics: bool):
        self.defs: dict[str, tuple[tuple, Any]] = {}
        self.clock = clock
        self.physics = physics

    def learn(self, st: Step) -> None:
        if st.kind != "eq" or len(st.parts) < 2 or st.parts[0] is None or st.parts[-1] is None:
            return
        name = _lone_name(st.parts[0])
        if name is None:
            return
        rhs = st.parts[-1]
        if isinstance(name, AppliedUndef):
            self.defs[name.func.__name__] = (tuple(name.args), rhs)
        elif rhs.free_symbols - {name}:
            self.defs[name.name] = ((), rhs)


def _has_calc(e: Any) -> bool:
    return isinstance(e, sympy.Basic) and (e.has(sympy.Derivative) or e.has(sympy.Integral)
                                           or bool(_derived_names_in(e)))


def _derived_names_in(e: sympy.Basic) -> list:
    out = [a for a in e.atoms(AppliedUndef) if a.func.__name__.endswith("'")]
    out += [s for s in e.free_symbols if s.name.endswith("'")]
    return out


def _resolve(e: Any, cx: Ctx) -> Any:
    """Substitute definitions into derivative names / function calls and evaluate d/dx and integrals."""
    if not isinstance(e, sympy.Basic):
        return None
    rep = {}
    for node in _derived_names_in(e):
        dn = _derived_name(node)
        if dn is None or dn[0] not in cx.defs:
            return None
        params, body = cx.defs[dn[0]]
        if params:
            var = params[0]
            val = sympy.diff(body, var)
            rep[node] = val.subs(var, dn[1]) if dn[1] is not None else val
        else:
            syms = [s for s in body.free_symbols if not U.is_base_symbol(s)]
            if len(syms) != 1:
                return None
            rep[node] = sympy.diff(body, syms[0])
    for node in e.atoms(AppliedUndef):
        nm = node.func.__name__
        if node in rep or nm.endswith("'"):
            continue
        if nm in cx.defs and cx.defs[nm][0] and len(cx.defs[nm][0]) == len(node.args):
            params, body = cx.defs[nm]
            if node.args != params:
                rep[node] = body.subs(dict(zip(params, node.args)))
    # dy/dx with y defined earlier
    for d in e.atoms(sympy.Derivative):
        if isinstance(d.expr, sympy.Symbol) and d.expr.name in cx.defs and not cx.defs[d.expr.name][0]:
            rep[d] = sympy.Derivative(cx.defs[d.expr.name][1], *d.variables)
    if rep:
        e = e.xreplace(rep)
    if e.has(sympy.Derivative) or e.has(sympy.Integral):
        def run(x):
            return x.replace(lambda n: isinstance(n, (sympy.Derivative, sympy.Integral)) and not
                             _indefinite(n), lambda n: n.doit())
        e2 = cx.clock.call(run, e)
        if e2 is None or (e2.has(sympy.Integral) and not any(_indefinite(i) for i in e2.atoms(sympy.Integral))):
            return None
        e = e2
    return e


def _indefinite(n: Any) -> bool:
    return isinstance(n, sympy.Integral) and any(len(lim) == 1 for lim in n.limits)


def _freeze(e: sympy.Basic) -> sympy.Basic:
    """Function calls left unresolved (f(x)) become plain symbols for algebra."""
    apps = e.atoms(AppliedUndef)
    return e.xreplace({a: sympy.Symbol(str(a)) for a in apps}) if apps else e


def _syms(e: sympy.Basic) -> set:
    return {s for s in e.free_symbols if not U.is_base_symbol(s)}


def same(a: Any, b: Any, cx: Ctx, *, abs_tol: float | None = None, rel_tol: float = 1e-9) -> tuple[bool | None, str]:
    """Is b the same value as a? (True / False / None = cannot tell, with a short reason)."""
    if a is None or b is None:
        return None, "could not read one side"
    ra, rb = _resolve(a, cx), _resolve(b, cx)
    if ra is None or rb is None:
        return None, "could not evaluate the calculus"
    ra, rb = _freeze(ra), _freeze(rb)
    if U.has_units(ra) != U.has_units(rb) and not (U.dimension_problems(ra) or U.dimension_problems(rb)):
        ra, rb = U.strip_units(ra), U.strip_units(rb)   # units written on one side only
    indefinite = [i for i in (ra.atoms(sympy.Integral) | rb.atoms(sympy.Integral)) if _indefinite(i)]
    if indefinite:
        var = indefinite[0].limits[0][0]
        da = cx.clock.call(lambda: sympy.diff(ra, var).doit())
        db = cx.clock.call(lambda: sympy.diff(rb, var).doit())
        if da is None or db is None:
            return None, "could not check the integral"
        v = mathcheck.check(da, db, rel_tol=rel_tol, timeout=max(0.2, min(CALL_BUDGET, cx.clock.left())))
        if v.equal is True:
            consts = {s.name for s in (_syms(ra) | _syms(rb))} & {"C", "c", "K", "C_{1}", "k"}
            return True, "antiderivative checks" + ("" if consts else " (no + C)")
        if v.equal is False:
            return False, "the derivative of the result is not the integrand"
        return None, "could not check the integral"
    du = U.dimension_problems(ra) + U.dimension_problems(rb)
    if du:
        return False, "units: " + du[0]
    if U.has_units(ra) or U.has_units(rb):
        da_, db_ = U.expr_dims(ra), U.expr_dims(rb)
        if da_ is not U.UNKNOWN and db_ is not U.UNKNOWN and da_ != db_:
            return False, f"units do not match ({U.dims_text(da_)} vs {U.dims_text(db_)})"
    if cx.clock.left() <= 0.1:
        return None, "time limit"
    v = mathcheck.check(ra, rb, abs_tol=abs_tol, rel_tol=rel_tol,
                        timeout=max(0.2, min(CALL_BUDGET, cx.clock.left())))
    if v.equal is True:
        return True, "equal" if v.method != "numeric" else "same value"
    if v.equal is False:
        sa, sb = _syms(ra), _syms(rb)
        if sa != sb:
            return None, "substitutes values" if (sb < sa or sa < sb) else "different variables"
        return False, "not equal"
    return None, "could not decide"


def _tol(st: Step) -> tuple[float | None, float]:
    """Tolerance for comparing against a line with written decimals."""
    if st.has_float:
        return st.floats_tol, 5e-3
    return None, 1e-9


def _num_close(a: Any, b: Any, abs_tol: float | None, rel_tol: float) -> bool | None:
    try:
        ca, cb = complex(sympy.N(a, 20)), complex(sympy.N(b, 20))
    except Exception:  # noqa: BLE001
        return None
    d = abs(ca - cb)
    return (abs_tol is not None and d <= abs_tol + 1e-12) or d <= rel_tol * max(1.0, abs(ca), abs(cb))


def _real_solutions(D: sympy.Expr, var: sympy.Symbol, cx: Ctx) -> list | None:
    if D.has(sympy.sin, sympy.cos, sympy.tan, sympy.exp, sympy.log) and D.free_symbols == {var}:
        # periodic / transcendental: solve() only gives principal values
        if D.has(sympy.sin, sympy.cos, sympy.tan):
            return None
    sols = cx.clock.call(lambda: sympy.solve(D, var, dict=False))
    if sols is None or not isinstance(sols, list):
        return None
    out = []
    for s in sols:
        try:
            c = complex(sympy.N(s, 20)) if s.is_number else None
        except Exception:  # noqa: BLE001
            c = None
        if c is not None and abs(c.imag) > 1e-9 * max(1.0, abs(c)):
            continue
        out.append(s)
    return out


def _set_compare(A: list, B: list, abs_tol: float | None, rel_tol: float) -> tuple[list, list]:
    """(in A not in B, in B not in A) with numeric tolerance."""
    def has(xs, y):
        return any(_num_close(x, y, abs_tol, rel_tol) for x in xs)
    return [a for a in A if not has(B, a)], [b for b in B if not has(A, b)]


def _fmt(v: Any) -> str:
    try:
        s = sympy.latex(sympy.nsimplify(v) if isinstance(v, sympy.Float) else v)
    except Exception:  # noqa: BLE001
        s = str(v)
    return s[:40]


def eq_equivalent(p: Step, c: Step, cx: Ctx) -> tuple[bool | None, str]:
    """Do two equations say the same thing?"""
    pa = [_resolve(x, cx) for x in (p.parts[0], p.parts[-1])]
    ca = [_resolve(x, cx) for x in (c.parts[0], c.parts[-1])]
    if any(x is None for x in pa + ca):
        return None, "could not read both equations"
    D1 = _freeze(pa[0] - pa[1])
    D2 = _freeze(ca[0] - ca[1])
    if U.has_units(D1) != U.has_units(D2):
        # units written on one line only: compare the SI magnitudes
        D1, D2 = U.strip_units(D1), U.strip_units(D2)
    dp = U.dimension_problems(D2)
    if dp:
        return False, "units: " + dp[0]
    abs_tol, rel_tol = _tol(c)
    if D1 == 0 or D2 == 0:
        return None, "identity"
    # 1) same equation up to a constant factor (both sides transformed alike)
    r = cx.clock.call(lambda: sympy.cancel(sympy.together(D1 / D2)))
    if r is not None and r.is_number and r != 0 and r.is_finite:
        return True, "same equation" if r in (1, -1) else "both sides transformed alike"
    syms = sorted(_syms(D1) | _syms(D2), key=lambda s: s.name)
    if not syms:
        return None, "no unknowns"
    if _syms(D1) != _syms(D2):
        return None, "substitutes values" if (_syms(D2) < _syms(D1) or _syms(D1) < _syms(D2)) else \
            "different letters"
    if len(syms) == 1:
        var = syms[0]
        s1, s2 = _real_solutions(D1, var, cx), _real_solutions(D2, var, cx)
        if s1 is None or s2 is None:
            return None, "could not solve"
        if not s1 and not s2:
            return None, "no real solutions"
        lost, gained = _set_compare(s1, s2, abs_tol, max(rel_tol, 1e-9))
        if not lost and not gained:
            return True, "same solutions"
        v = var.name
        if gained and lost:
            return False, f"solution changes ({v} = {_fmt(s1[0])} becomes {v} = {_fmt(s2[0])})"
        if gained:
            return False, f"adds solution {v} = {_fmt(gained[0])}"
        if cx.physics and all((sympy.N(x) < 0) == True for x in lost):  # noqa: E712
            return True, "keeps the physical (positive) solution"
        return False, f"loses solution {v} = {_fmt(lost[0])}"
    # 2) several letters (formula rearranged): solve for a shared letter both ways
    target = _lone_name(c.parts[0]) if c.parts[0] is not None else None
    order = ([target] if isinstance(target, sympy.Symbol) and target in syms else []) + \
        [s for s in syms if s is not target]
    for var in order[:3]:
        s1, s2 = _real_solutions(D1, var, cx), _real_solutions(D2, var, cx)
        if not s1 or not s2:
            continue

        def holds(D, sols):
            res = []
            for s in sols:
                v = mathcheck.check(D.subs(var, s), sympy.Integer(0), abs_tol=abs_tol, rel_tol=max(rel_tol, 1e-9),
                                    timeout=max(0.2, min(CALL_BUDGET, cx.clock.left())))
                res.append(v.equal)
            return res
        fwd = holds(D1, s2)   # every solution of the new line solves the old one
        back = holds(D2, s1)
        if all(x is True for x in fwd) and all(x is True for x in back):
            return True, f"same equation solved for {var.name}"
        if all(x is True for x in fwd) and any(x is True for x in back):
            return True, f"solved for {var.name} (keeps one root)"
        if any(x is False for x in fwd) and not any(x is True for x in fwd):
            return False, "does not follow"
    return None, "could not compare the equations"


def _values_of(st: Step, cx: Ctx) -> list | None:
    """Solution values a line states: x = 2 / x = 2 or 3 / 2x = 4 (one unknown) / bare value."""
    if st.kind == "solset":
        return [list(d.values())[0] for d in st.sols if len(d) == 1]
    if st.kind == "expr":
        return [st.parts[0]] if st.parts[0] is not None and not _syms(st.parts[0]) else None
    if st.kind == "eq" and len(st.parts) >= 2 and st.parts[0] is not None and st.parts[-1] is not None:
        if _lone_name(st.parts[0]) is not None and not _syms(st.parts[-1]):
            return [st.parts[-1]]
        D = _freeze(st.parts[0] - st.parts[-1])
        syms = _syms(D)
        if len(syms) == 1:
            return _real_solutions(D, next(iter(syms)), cx)
    return None


def internal(st: Step, cx: Ctx) -> tuple[str | None, str]:
    """Claims inside one line: 3·4 = 12, d/dx(...) = ..., ∫... = ..., a = b = c chains."""
    if st.kind == "vector" or st.kind == "solset":
        return None, ""
    for p in st.parts:
        if p is not None:
            dp = U.dimension_problems(p)
            if dp:
                return ERR, "units: " + dp[0]
    if st.kind != "eq" or len(st.parts) < 2:
        return None, ""
    parts = st.parts
    checked, ok_reason = 0, ""
    start = 1 if (len(parts) >= 3 and _lone_name(parts[0]) is not None) or st.leading else 0
    for i in range(start, len(parts) - 1):
        a, b = parts[i], parts[i + 1]
        if a is None or b is None:
            continue
        calc = _has_calc(a) or _has_calc(b)
        const = not _syms(_freeze(a)) and not _syms(_freeze(b))
        chain_mid = len(parts) >= 3 and i >= start and not (i + 1 == len(parts) - 1 and not _syms(b) and _syms(a))
        if not (calc or const or chain_mid):
            continue
        abs_tol, rel_tol = _tol(st)
        res, why = same(a, b, cx, abs_tol=abs_tol if not _syms(b) else None, rel_tol=rel_tol)
        if res is False:
            what = "derivative" if a.has(sympy.Derivative) or _derived_names_in(a) else \
                "integral" if a.has(sympy.Integral) else "calculation"
            return ERR, f"{what} is wrong: {_fmt(a)} ≠ {_fmt(b)}" if why == "not equal" else why
        if res is True:
            checked += 1
            ok_reason = why
    if checked:
        return OK, ("calculation checks" if ok_reason in ("equal", "same value") else ok_reason)
    return None, ""


def relate(p: Step, c: Step, cx: Ctx) -> tuple[bool | None, str]:
    """Does line c follow from line p?"""
    abs_tol, rel_tol = _tol(c)
    if c.leading:
        pv = p.value if p.kind in ("expr", "eq") else None
        first = next((x for x in c.parts[1:] if x is not None), None)
        res, why = same(pv, first, cx, abs_tol=abs_tol, rel_tol=rel_tol)
        return res, ("does not equal the previous line" if res is False and why == "not equal" else why)
    if c.kind == "vector" or p.kind == "vector":
        if c.kind == "vector" and p.kind == "vector" and len(c.vec) == len(p.vec):
            rs = [same(a, b, cx, abs_tol=abs_tol, rel_tol=rel_tol)[0] for a, b in zip(p.vec, c.vec)]
            if all(r is True for r in rs):
                return True, "same vector"
            if any(r is False for r in rs):
                return False, "a component changes"
        return None, ""
    if c.kind == "solset" or p.kind == "solset":
        A, B = _values_of(p, cx), _values_of(c, cx)
        if not A or not B:
            return None, "could not compare the solutions"
        lost, gained = _set_compare(A, B, abs_tol, rel_tol)
        if not lost and not gained:
            return True, "same solutions"
        if gained:
            return False, f"solution {_fmt(gained[0])} does not solve the previous line"
        if cx.physics and all((sympy.N(x) < 0) == True for x in lost):  # noqa: E712
            return True, "keeps the physical (positive) solution"
        return False, f"loses solution {_fmt(lost[0])}"
    if p.kind == "expr" and c.kind == "expr":
        res, why = same(p.parts[0], c.parts[0], cx, abs_tol=abs_tol, rel_tol=rel_tol)
        return res, ("not equal to the previous line" if res is False and why == "not equal" else why)
    if p.kind == "eq" and c.kind == "eq":
        if c.parts[0] is not None and (_derived_names_in(c.parts[0]) or c.parts[0].has(sympy.Derivative)):
            return None, "new claim"   # f'(x) = ... is checked inside the line
        if any(x is None for x in (p.parts[0], p.parts[-1], c.parts[0], c.parts[-1])):
            return None, "could not read both equations"
        if _lone_name(c.parts[0]) is not None and _lone_name(c.parts[0]) != _lone_name(p.parts[0]) and \
                not (_syms(c.parts[0]) & (_syms(p.parts[0]) | _syms(p.parts[-1]))):
            return None, "new quantity"
        return eq_equivalent(p, c, cx)
    if p.kind == "eq" and c.kind == "expr":
        cands = [x for x in (p.parts[-1], p.parts[0]) if x is not None]
        verdicts = [same(x, c.parts[0], cx, abs_tol=abs_tol, rel_tol=rel_tol) for x in cands]
        for res, why in verdicts:
            if res is True:
                return True, why
        if verdicts and verdicts[0][0] is False and not _syms(_freeze(cands[0])) and not _syms(_freeze(c.parts[0])):
            return False, "not equal to the previous result"
        return None, verdicts[0][1] if verdicts else ""
    if p.kind == "expr" and c.kind == "eq":
        if c.parts[0] is not None:
            res, why = same(p.parts[0], c.parts[0], cx, abs_tol=abs_tol, rel_tol=rel_tol)
            if res is True:
                return True, "continues the previous expression"
        if _lone_name(c.parts[0]) is not None and c.parts[-1] is not None:
            res, why = same(p.parts[0], c.parts[-1], cx, abs_tol=abs_tol, rel_tol=rel_tol)
            if res is not None:
                return res, why
        return None, ""
    return None, ""


# ====================================================================== whole chain
def verify_steps(lines: list[tuple[int, Any, str]], *, physics: bool = False,
                 clock: _Clock | None = None) -> tuple[list[dict], dict]:
    """lines = [(n, latex_or_text, model_status)] -> (per-step results, state).

    state: {"parsed": {n: Step}, "last": Step|None, "tainted": bool, "taint_from": n|None, "cx": Ctx}"""
    clock = clock or _Clock(TOTAL_BUDGET)
    cx = Ctx(clock, physics)
    out: list[dict] = []
    last: Step | None = None
    clean: Step | None = None
    tainted, taint_from = False, None
    parsed: dict[int, Step] = {}
    for n, src, model_status in lines:
        row = {"n": n, "status": UNK, "reason": "", "ecf": False}
        out.append(row)
        if model_status == "missing" and not str(src or "").strip():
            row["reason"] = "not written"
            continue
        if clock.left() <= 0.1:
            row["reason"] = "time limit"
            continue
        st = parse_step(n, src)
        if st is None:
            row["reason"] = "could not read this line as math"
            continue
        parsed[n] = st
        istat, ireason = internal(st, cx)
        if istat == ERR:
            row.update(status=ERR, reason=ireason)
            if not tainted:
                taint_from = n
            tainted = True
            last = st
            cx.learn(st)
            continue
        if last is None:
            if istat == OK:
                row.update(status=OK, reason=ireason)
            else:
                row["reason"] = "first line (nothing to compare with)"
            last = clean = st
            cx.learn(st)
            continue
        res, why = relate(last, st, cx)
        if res is True:
            if tainted:
                back = relate(clean, st, cx)[0] if clean is not None else None
                if back is True:
                    tainted, taint_from = False, None
                    row.update(status=OK, reason=f"follows from step {last.n}" + (f" ({why})" if why else ""))
                else:
                    row.update(status=OK, ecf=True,
                               reason=f"correct given the error in step {taint_from}" + (f" ({why})" if why else ""))
            else:
                row.update(status=OK, reason=f"follows from step {last.n}" + (f" ({why})" if why else ""))
        elif res is False:
            back = relate(clean, st, cx)[0] if (tainted and clean is not None) else None
            if back is True:
                tainted, taint_from = False, None
                row.update(status=OK, reason="back on track")
            else:
                row.update(status=ERR, reason=f"does not follow from step {last.n}: {why}")
                if not tainted:
                    taint_from = n
                tainted = True
        else:
            if istat == OK:
                row.update(status=OK, reason=ireason)
            else:
                row["reason"] = why or "cannot check this step"
            if tainted and clean is not None and relate(clean, st, cx)[0] is True:
                tainted, taint_from = False, None
        last = st
        if not tainted:
            clean = st
        cx.learn(st)
    return out, {"parsed": parsed, "last": last, "tainted": tainted, "taint_from": taint_from, "cx": cx}


# ====================================================================== final answer vs key
def _quantity(latex: Any, unit_text: Any) -> tuple[Step | None, U.UnitInfo | None]:
    st = parse_step(0, latex)
    uinfo = U.parse_unit(unit_text) if str(unit_text or "").strip() else None
    return st, uinfo


def _final_values(st: Step, uinfo: U.UnitInfo | None, cx: Ctx) -> tuple[str, list] | None:
    """("vec", comps) or ("set", values) with units multiplied in."""
    if st is None:
        return None
    if st.kind == "vector":
        comps = st.vec
        return ("vec", [c * uinfo.expr() for c in comps] if uinfo else comps)
    vals = _values_of(st, cx)
    if vals is None and st.kind == "expr" and st.parts[0] is not None:
        vals = [st.parts[0]]
    if vals is None and st.kind == "eq" and st.parts[-1] is not None:
        vals = [st.parts[-1]]
    if not vals:
        return None
    if uinfo is not None:
        vals = [v * uinfo.expr() if not U.has_units(v) else v for v in vals]
    return ("set", vals)


def _number_text(latex: str) -> str:
    t = clean_latex(latex)
    t = re.sub(r"\\(?:text|mathrm)\s*\{[^{}]*\}", "", t)
    t = re.sub(r"^[^=]*=", "", t).strip()
    m = re.match(r"^[+-]?\d[\d.]*(?:\s*\\(?:times|cdot)\s*10\^\{?-?\d+\}?)?", t)
    return m.group(0) if m else ""


def check_final(student_latex: str, student_units: str, key: dict, cx: Ctx,
                q_tolerance: float | None = None) -> tuple[dict | None, dict | None, dict | None]:
    """-> (final, units, sig_figs) records (any may be None)."""
    key_latex = str(key.get("key_final_latex") or "").strip()
    key_units = str(key.get("key_units") or "").strip()
    if not student_latex:
        return None, None, None
    s_step, s_unit = _quantity(student_latex, student_units)
    final: dict = {"status": UNK, "reason": "", "student": student_latex[:200], "key": key_latex[:200] or None,
                   "ecf": False}
    units_rec = None
    sig = None
    req = U.required_sig_figs(key)
    if req is not None:
        given = U.sig_figs(_number_text(student_latex))
        sig = {"required": req, "given": None if given is None else (given[0] if given[0] == given[1] else list(given)),
               "ok": None if given is None else (given[0] <= req <= given[1])}
    if s_step is None:
        final["reason"] = "could not read the final answer"
        return final, units_rec, sig
    if not key_latex:
        final["reason"] = "no final answer in the key"
        return final, units_rec, sig
    k_step, k_unit = _quantity(key_latex, key_units)
    if k_step is None:
        final["reason"] = "could not read the key's answer"
        return final, units_rec, sig
    sv, kv = _final_values(s_step, s_unit, cx), _final_values(k_step, k_unit, cx)
    if sv is None or kv is None:
        final["reason"] = "could not compare with the key"
        return final, units_rec, sig
    # units
    key_has_units = any(U.has_units(v) for v in kv[1])
    stu_has_units = any(U.has_units(v) for v in sv[1])
    if key_has_units:
        kd = U.expr_dims(kv[1][0])
        if not stu_has_units:
            units_rec = {"status": ERR, "reason": f"no units (expected {key_units or U.dims_text(kd)})",
                         "student": None, "key": key_units or None}
            # read the bare number in the key's units
            factor = (k_unit.expr() if k_unit else 1)
            sv = (sv[0], [v * factor for v in sv[1]])
        else:
            sd = U.expr_dims(sv[1][0])
            if sd is not U.UNKNOWN and kd is not U.UNKNOWN and sd != kd:
                units_rec = {"status": ERR, "reason": f"wrong units: {U.dims_text(sd)} instead of {U.dims_text(kd)}",
                             "student": student_units or None, "key": key_units or None}
                final.update(status=ERR, reason="units do not match the key")
                return final, units_rec, sig
            units_rec = {"status": OK, "reason": "units match" + (" after conversion" if (s_unit and k_unit and
                         s_unit.text != k_unit.text) else ""), "student": student_units or None,
                         "key": key_units or None}
    # tolerance: teacher's tolerance, or rounding of the decimals the student wrote
    abs_tol = q_tolerance if q_tolerance else None
    rt = U.rounding_tolerance(_number_text(student_latex))
    rel_tol = 1e-9
    if rt is not None and s_step.has_float:
        # accept rounding of the written decimals, but only to about 3 significant figures or better
        mags = []
        for v in kv[1] if kv[0] == "set" else kv[1]:
            try:
                mags.append(abs(complex(sympy.N(U.strip_units(_freeze(v))))))
            except Exception:  # noqa: BLE001
                pass
        if mags and all(rt <= 0.005 * m + 1e-12 for m in mags if m):
            abs_tol = max(abs_tol or 0.0, rt)
    kt = U.rounding_tolerance(_number_text(key_latex))
    if kt is not None and k_step.has_float:
        abs_tol = max(abs_tol or 0.0, kt)   # the key itself is a rounded decimal
    if sv[0] != kv[0] or len(sv[1]) != len(kv[1]):
        final.update(status=ERR, reason=(f"gives {len(sv[1])} answer(s), the key has {len(kv[1])}"
                                          if sv[0] == kv[0] else "different kind of answer than the key"))
        return final, units_rec, sig

    def close(a, b):
        sa, sb = U.strip_units(_freeze(a)), U.strip_units(_freeze(b))
        scale = 1.0
        if U.has_units(b) and k_unit is not None:
            try:
                scale = float(k_unit.factor)
            except Exception:  # noqa: BLE001
                scale = 1.0
        if not _syms(sa) and not _syms(sb):
            return _num_close(sa, sb, None if abs_tol is None else abs_tol * scale, rel_tol)
        return same(sa, sb, cx, rel_tol=rel_tol)[0]

    if sv[0] == "vec":
        rs = [close(a, b) for a, b in zip(sv[1], kv[1])]
    else:
        remaining = list(kv[1])
        rs = []
        for a in sv[1]:
            hit = next((b for b in remaining if close(a, b) is True), None)
            if hit is not None:
                remaining.remove(hit)
                rs.append(True)
            else:
                rs.append(False if all(close(a, b) is False for b in remaining) else None)
    if all(r is True for r in rs):
        tol_backup = abs_tol
        abs_tol = q_tolerance or None
        exact = all(close(a, b) is True for a, b in zip(sv[1], kv[1])) if sv[0] == "vec" else \
            all(any(close(a, b) is True for b in kv[1]) for a in sv[1])
        abs_tol = tol_backup
        final.update(status=OK, reason="matches the key" + ("" if exact else " (rounded)"))
    elif any(r is False for r in rs):
        final.update(status=ERR, reason="does not match the key")
    else:
        final["reason"] = "could not compare with the key"
    return final, units_rec, sig


# ====================================================================== AI output -> lines
def _raw_steps(out: Any) -> list[dict]:
    """The model's steps aligned with ai_grade.parse_steps (same skipping rules)."""
    from .ai_grade import _STATUS
    res: list[dict] = []
    raw = out.get("steps") if isinstance(out, dict) else None
    if not isinstance(raw, list):
        return res
    for s in raw[:40]:
        if isinstance(s, str):
            s = {"text": s, "status": "correct"}
        if not isinstance(s, dict):
            continue
        text = str(s.get("text") or s.get("step") or "").strip()[:500]
        status = _STATUS.get(str(s.get("status") or "").strip().lower())
        if status is None:
            status = "correct" if s.get("correct") is True else "error"
        if not text and status != "missing":
            continue
        res.append(s)
    return res


def _final_from(out: Any) -> dict | None:
    if not isinstance(out, dict):
        return None
    fa = out.get("final_answer")
    if isinstance(fa, dict):
        latex = str(fa.get("latex") or fa.get("value") or "").strip()[:300]
        unit = str(fa.get("units") or fa.get("unit") or "").strip()[:60]
        text = str(fa.get("text") or "").strip()[:300]
        if latex or text:
            return {"latex": latex or text, "units": unit, "text": text}
    elif isinstance(fa, (str, int, float)) and str(fa).strip():
        return {"latex": str(fa).strip()[:300], "units": str(out.get("final_units") or "")[:60], "text": ""}
    return None


def _round_half(x: float) -> float:
    return float(int(x * 2 + 1e-9)) / 2


def reconcile(d: dict, out: Any, q: dict, ctx: dict, results: list[dict], state: dict,
              final: dict | None, units_rec: dict | None, sig: dict | None) -> tuple[dict, list[str]]:
    """Merge verifier verdicts into the draft. Returns (draft, extra review reason codes)."""
    steps = d.get("steps") or []
    by_n = {r["n"]: r for r in results}
    disagreements: list[dict] = []
    max_points = float(q.get("points", 1))
    model_points = float(d.get("points") or 0)
    raw = _raw_steps(out)
    model_ecf = {i + 1 for i, s in enumerate(raw) if isinstance(s, dict) and s.get("ecf") is True}
    any_ecf_criterion = any(c.get("ecf") for c in (ctx.get("criteria") or []))
    for s in steps:
        r = by_n.get(s["n"])
        if r is None:
            continue
        s["verify"], s["verify_reason"], s["ecf"] = r["status"], r["reason"], bool(r["ecf"]) or \
            (r["status"] != ERR and s["n"] in model_ecf and s["status"] == "correct")
        if r["status"] == OK and s["status"] == "error":
            changes = not r["ecf"] or any_ecf_criterion
            s["model_status"], s["status"] = "error", "correct"
            s["verifier_disagrees"] = ("SymPy: this step follows correctly from the earlier error (ECF); the AI "
                                       "marked it wrong") if r["ecf"] else \
                ("SymPy proves this step follows from the previous one; the AI marked it wrong")
            disagreements.append({"n": s["n"], "model": "error", "verifier": OK, "reason": s["verifier_disagrees"],
                                  "changes_points": changes, "ecf": bool(r["ecf"])})
        elif r["status"] == ERR and s["status"] == "correct":
            s["model_status"], s["status"] = "correct", "error"
            s["verifier_disagrees"] = f"SymPy finds a mistake here ({r['reason']}); the AI marked it correct"
            disagreements.append({"n": s["n"], "model": "correct", "verifier": ERR, "reason": s["verifier_disagrees"],
                                  "changes_points": True, "ecf": False})
    d["first_error_step"] = next((s["n"] for s in steps if s["status"] != "correct"), None)

    # final answer vs the model's points
    if final is not None and final["status"] != UNK and d.get("source") != "key":
        if final["status"] == ERR and state.get("tainted") and state.get("last") is not None:
            final["ecf"] = True
            final["reason"] += f" (follows from the error in step {state.get('taint_from')})"
        full = model_points >= max_points - 1e-9
        if final["status"] == ERR and full:
            disagreements.append({"n": "final", "model": "full marks", "verifier": ERR,
                                  "reason": f"SymPy: the final answer {final['reason']}, but the AI gave full marks",
                                  "changes_points": True})
        elif final["status"] == OK and model_points <= 1e-9 and not any(
                r["status"] == ERR for r in results):
            disagreements.append({"n": "final", "model": "no marks", "verifier": OK,
                                  "reason": "SymPy: the final answer matches the key, but the AI gave 0 points",
                                  "changes_points": True})
    if units_rec and units_rec["status"] == ERR and model_points >= max_points - 1e-9 and d.get("source") != "key":
        disagreements.append({"n": "units", "model": "full marks", "verifier": ERR,
                              "reason": f"Units: {units_rec['reason']}, but the AI gave full marks",
                              "changes_points": True})
    if sig and sig.get("ok") is False and model_points >= max_points - 1e-9:
        disagreements.append({"n": "sig_figs", "model": "full marks", "verifier": ERR,
                              "reason": f"Answer has {sig['given']} significant figures, the rubric asks for "
                                        f"{sig['required']}", "changes_points": True})

    # criteria judged on specific steps: re-score from the verified steps
    raw_crit = out.get("criteria") if isinstance(out, dict) and isinstance(out.get("criteria"), list) else []
    crit_steps: dict[str, list[int]] = {}
    for i, rc in enumerate(raw_crit):
        if not isinstance(rc, dict):
            continue
        ns = rc.get("steps")
        if isinstance(ns, list):
            cid = str(rc.get("id") or "")
            if not cid and i < len(d.get("criteria") or []):
                cid = d["criteria"][i]["id"]
            crit_steps[cid] = [int(x) for x in ns if isinstance(x, (int, float)) and not isinstance(x, bool)
                               and 1 <= int(x) <= len(steps)]
    key_crit = {c["id"]: c for c in (ctx.get("criteria") or [])}
    changed_points = False
    if d.get("source") != "key":
        for c in d.get("criteria") or []:
            if key_crit.get(c["id"], {}).get("ecf"):
                c["ecf"] = True
            ns = crit_steps.get(c["id"])
            if not ns:
                continue
            c["steps"] = ns
            rows = [by_n.get(n) for n in ns]
            if any(r is None or r["status"] == UNK for r in rows):
                continue
            allow_ecf = bool(c.get("ecf"))
            good = sum(1 for r in rows if r["status"] == OK and (not r["ecf"] or allow_ecf))
            mx = float(c["max_points"])
            new = mx if good == len(rows) else _round_half(mx * good / len(rows))
            old = float(c["points"])
            if abs(new - old) > 1e-9:
                c["model_points"] = c["points"]
                c["points"] = int(new) if float(new).is_integer() else new
                c["met"] = abs(new - mx) < 1e-9 and mx > 0
                c["verifier_note"] = ("method marks under ECF: steps " if any(r["ecf"] for r in rows) and allow_ecf
                                      else "re-scored from checked steps ") + ", ".join(map(str, ns))
                changed_points = True
        if changed_points:
            total = sum(float(c["points"]) for c in d["criteria"])
            d["points"] = max(0.0, min(max_points, total))
            disagreements.append({"n": "criteria", "model": model_points, "verifier": d["points"],
                                  "reason": f"Points re-scored from the checked steps: {model_points:g} → "
                                            f"{float(d['points']):g}", "changes_points": True})

    reasons: list[str] = []
    conf = float(d.get("confidence") or 0)
    checked = [r for r in results if r["status"] != UNK]
    if any(x.get("changes_points") for x in disagreements):
        reasons.append("verifier_disagrees")
        conf = min(conf, 0.5)
    elif disagreements:
        conf = max(0.0, conf - 0.1)
    elif checked and (final is None or final["status"] == OK or (final["status"] == ERR and model_points < max_points)) \
            and d.get("legibility") not in ("partly", "illegible") and \
            (final is not None and final["status"] != UNK or len(checked) >= max(1, len(results) // 2)):
        conf = min(0.97, max(conf, min(conf + 0.15, 0.95) if final is None else 0.9))
    d["confidence"] = round(conf, 2)
    return d, reasons, disagreements


def _lines_for(d: dict, out: Any) -> list[tuple[int, Any, str]]:
    raw = _raw_steps(out)
    lines = []
    for i, s in enumerate(d.get("steps") or []):
        rs = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
        latex = str(rs.get("latex") or "").strip()[:MAX_LATEX]
        if latex:
            s["latex"] = latex
        lines.append((s["n"], latex or s.get("text") or "", s.get("status", "correct")))
    return lines


def _is_physics(ctx: dict) -> bool:
    return str(ctx.get("subject") or "").lower() in ("physics", "chemistry")


def verify_draft(d: dict, out: Any, q: dict, ctx: dict, budget: float = TOTAL_BUDGET) -> dict:
    """The whole check on a parsed draft (synchronous). Returns a new dict."""
    t0 = time.monotonic()
    d = copy.deepcopy(d)
    if d.get("source") not in ("ai", "key") or not d.get("steps") and not _final_from(out):
        return d
    clock = _Clock(budget)
    lines = _lines_for(d, out)
    results, state = verify_steps(lines, physics=_is_physics(ctx), clock=clock)
    fa = _final_from(out)
    d["final_answer"] = fa
    student_final = (fa or {}).get("latex") or ""
    student_units = (fa or {}).get("units") or ""
    if not student_final and lines:
        # last readable line stands for the final answer
        last = state.get("last")
        if last is not None:
            student_final = next((ln[1] for ln in lines if ln[0] == last.n), "")
    tol = None
    if q.get("type") == "numeric":
        try:
            tol = float(q.get("tolerance") or 0) or None
        except (TypeError, ValueError):
            tol = None
    final, units_rec, sig = (None, None, None)
    if student_final:
        try:
            final, units_rec, sig = check_final(student_final, student_units, ctx, state["cx"], tol)
        except Exception as ex:  # noqa: BLE001 - never break grading over the check
            final = {"status": UNK, "reason": f"check failed ({type(ex).__name__})", "student": student_final[:200],
                     "key": ctx.get("key_final_latex"), "ecf": False}
    conf_before, pts_before = d.get("confidence"), d.get("points")
    d, reasons, disagreements = reconcile(d, out, q, ctx, results, state, final, units_rec, sig)
    rows = []
    by_n = {s["n"]: s for s in d.get("steps") or []}
    for r in results:
        rows.append({"n": r["n"], "status": r["status"], "reason": r["reason"], "ecf": bool(r["ecf"]),
                     "latex": (by_n.get(r["n"]) or {}).get("latex") or None})
    summary = {OK: 0, ERR: 0, UNK: 0}
    for r in results:
        summary[r["status"]] += 1
    checked_any = bool(summary[OK] or summary[ERR] or (final and final["status"] != UNK) or units_rec)
    if not checked_any:
        # nothing was math (prose, chemistry equations ...): no per-step badges, the record stays for metrics
        for s in d.get("steps") or []:
            s.pop("verify", None)
            s.pop("verify_reason", None)
    d["verification"] = {
        "engine": ENGINE + ("+pint" if units_rec else "") + ("" if _HAVE_ANTLR else " (built-in LaTeX reader)"),
        "version": VERSION, "steps": rows, "final": final, "units": units_rec, "sig_figs": sig,
        "summary": summary, "disagreements": disagreements,
        "checked": checked_any,
        "agrees": not disagreements and checked_any,
        "confidence_before": conf_before, "points_before": pts_before,
        "seconds": round(time.monotonic() - t0, 3),
    }
    d["_verify_reasons"] = reasons
    return d


# ====================================================================== hook entry points
def _register_review_text() -> None:
    from . import ai_grade
    for k, v in REVIEW_TEXT.items():
        ai_grade.REVIEW_TEXT.setdefault(k, v)


def apply(d: dict, out: Any, q: dict, ctx: dict, budget: float = TOTAL_BUDGET) -> dict:
    """Hook called by ai_grade.suggest after parse_suggestion. Never raises."""
    if not enabled() or not isinstance(d, dict):
        return d
    from . import ai_grade
    _register_review_text()
    try:
        v = verify_draft(d, out, q, ctx, budget)
    except Exception as ex:  # noqa: BLE001
        d = dict(d)
        d["verification"] = {"engine": ENGINE, "version": VERSION, "error": f"{type(ex).__name__}: {ex}"[:200],
                             "steps": [], "final": None, "units": None, "sig_figs": None, "disagreements": []}
        return d
    if "verification" not in v:
        return v
    reasons = v.pop("_verify_reasons", [])
    v["prompt_version"] = PROMPT_VERSION if _final_from(out) or any(s.get("latex") for s in v.get("steps") or []) \
        else v.get("prompt_version")
    # confidence may have moved: drop a stale low_confidence reason and re-finalize
    v["review_reasons"] = [r for r in v.get("review_reasons") or [] if r != "low_confidence"]
    return ai_grade.finalize(v, q, reasons)


async def apply_async(d: dict, out: Any, q: dict, ctx: dict) -> dict:
    if not enabled():
        return d
    try:
        return await asyncio.wait_for(asyncio.to_thread(apply, d, out, q, ctx), TOTAL_BUDGET + 5)
    except Exception:  # noqa: BLE001 - timeout: keep the model's draft
        return d


# ====================================================================== prompt extension (v2)
PROMPT_EXTRA = (
    "Math transcription (prompt fp-step-grade-v2): for EVERY step also give \"latex\": the student's line "
    "exactly as written, as LaTeX (keep their mistakes; one expression or equation per step; write units "
    "as \\text{m/s}; continue a calculation with a line starting with '='). Also return \"final_answer\": "
    "{\"latex\": \"the student's final answer as LaTeX\", \"units\": \"unit or empty\"}. For each rubric "
    "criterion add \"steps\": [the step numbers it is judged on]. A step that correctly follows from an "
    "earlier mistake is error carried forward: mark it \"correct\" with \"ecf\": true."
)
SCHEMA_EXTRA = ('{"steps": [{"text": "...", "latex": "2x + 3 = 7", "status": "correct|error|missing", '
                '"ecf": false, "note": "..."}], "final_answer": {"latex": "x = 2", "units": ""}, '
                '"criteria": [{"id": "...", "points": 1, "steps": [1, 2], "note": "..."}]}')


def extend_prompt(prompt: str, q: dict, ctx: dict) -> str:
    if not enabled():
        return prompt
    prompt = re.sub(r"^Prompt version: (\S+)", rf"Prompt version: {PROMPT_VERSION} (= \1 + LaTeX steps)",
                    prompt, count=1, flags=re.M)
    extra = [PROMPT_EXTRA, f"Extra fields: {SCHEMA_EXTRA}"]
    if ctx.get("key_final_latex"):
        extra.append(f"Teacher's final answer (LaTeX): {ctx['key_final_latex']}"
                     + (f" [{ctx['key_units']}]" if ctx.get("key_units") else ""))
    if ctx.get("key_solution_latex"):
        extra.append("Teacher's worked solution (LaTeX lines):\n" + "\n".join(ctx["key_solution_latex"][:30]))
    ecf = [c["id"] for c in ctx.get("criteria") or [] if c.get("ecf")]
    if ecf:
        extra.append("Criteria that allow error carried forward (award them when the method is right on a wrong "
                     "earlier value): " + ", ".join(ecf))
    if ctx.get("sig_figs"):
        extra.append(f"The final answer must have {ctx['sig_figs']} significant figures.")
    return prompt + "\n" + "\n".join(extra)


# ====================================================================== answer key LaTeX
def key_latex_context(q: dict, entry: dict | None) -> dict:
    """Fields the verifier reads from the answer key (merged into answer_key.key_context)."""
    entry = entry or {}
    final = str(entry.get("final_latex") or "").strip()
    if not final and q.get("type") == "numeric" and q.get("answer_number") is not None:
        try:
            v = float(q["answer_number"])
            final = str(int(v)) if v.is_integer() else repr(v)
        except (TypeError, ValueError):
            final = ""
    sol = entry.get("solution_latex") or []
    if isinstance(sol, str):
        sol = [x for x in sol.splitlines() if x.strip()]
    return {"key_final_latex": final, "key_units": str(entry.get("units") or "").strip(),
            "key_solution_latex": [str(x)[:MAX_LATEX] for x in sol][:40],
            "sig_figs": entry.get("sig_figs")}


def clean_key_latex(raw: dict) -> dict:
    """Teacher / AI input for the key's LaTeX fields -> stored shape."""
    sol = raw.get("solution_latex") or []
    if isinstance(sol, str):
        sol = sol.splitlines()
    sol = [str(x).strip()[:MAX_LATEX] for x in sol if str(x).strip()][:40] if isinstance(sol, list) else []
    sf = raw.get("sig_figs")
    try:
        sf = None if sf in (None, "") else int(sf)
        if sf is not None and not 1 <= sf <= 15:
            sf = None
    except (TypeError, ValueError):
        sf = None
    return {"final_latex": str(raw.get("final_latex") or "").strip()[:MAX_LATEX],
            "units": str(raw.get("units") or "").strip()[:60], "solution_latex": sol, "sig_figs": sf}


def check_key_latex(fields: dict) -> list[str]:
    """Warnings for the teacher when the key's LaTeX cannot be read."""
    warn = []
    if fields.get("final_latex") and parse_step(0, fields["final_latex"]) is None:
        warn.append("The final answer LaTeX could not be read — the math check will skip it")
    if fields.get("units") and U.parse_unit(fields["units"]) is None:
        warn.append(f"Unknown unit “{fields['units']}”")
    bad = [i + 1 for i, ln in enumerate(fields.get("solution_latex") or []) if parse_step(i + 1, ln) is None]
    if bad:
        warn.append("Solution lines that could not be read: " + ", ".join(map(str, bad[:10])))
    return warn


KEY_LATEX_SYSTEM = ("You convert a teacher's answer key into LaTeX for an automatic math checker. Keep the math "
                    "exactly; one equation or expression per line; units as \\text{...}. Reply with ONE JSON "
                    "object only.")
KEY_LATEX_SCHEMA = ('{"final_latex": "final answer, e.g. x = 2 or 19.6", "units": "unit of the final answer or '
                    'empty", "solution_latex": ["first line", "second line"]}')


def key_latex_prompt(q: dict, entry: dict, correct_answer: str) -> str:
    parts = [f"Question: {q.get('prompt', '')}"]
    if correct_answer:
        parts.append(f"Correct answer: {correct_answer}")
    if entry.get("solution"):
        parts.append(f"Teacher's worked solution:\n{entry['solution']}")
    parts.append(f"Return JSON exactly like: {KEY_LATEX_SCHEMA}")
    return "\n".join(parts)
