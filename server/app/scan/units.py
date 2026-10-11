"""Physical units for step verification (math/physics papers).

Units are only taken from explicit unit markup — `\\text{m/s}`, `\\mathrm{km\\,h^{-1}}`, the
`units` field of a final answer or of the teacher's key — never from bare letters (an `m` in
an equation is a mass as often as a metre).

A unit string is read with pint and turned into a SymPy expression over *base-unit symbols*
(`[meter]`, `[second]`, `[kilogram]`, ...) times its exact SI factor, e.g.
`km/h -> 5/18 * [meter] * [second]**-1`. Substituting these into a parsed step makes unit
conversions plain algebra (72 km/h - 20 m/s == 0) and dimension errors visible as an Add whose
terms have different base-unit signatures (5 [meter] + 3 [second]).

Also here: significant-figure counting and the "how many sig figs does the rubric want" rule.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

import sympy

BASE_PREFIX = "["  # base-unit symbols are named "[meter]" — no student symbol can look like that
_ureg = None
_lock = threading.Lock()

# LaTeX/Unicode spellings -> pint spellings
_REPL = [
    (r"\Omega", "ohm"), ("Ω", "ohm"), (r"\mu ", "u"), (r"\mu", "u"), ("µ", "u"), ("μ", "u"),
    (r"^\circ", "deg"), (r"^{\circ}", "deg"), ("°", "deg"), (r"\%", "percent"), ("%", "percent"),
    (r"\cdot", "*"), (r"\times", "*"), ("·", "*"), ("⋅", "*"), (r"\,", " "), (r"\;", " "), (r"\ ", " "),
    ("~", " "),
]
_OFFSET = {"degc", "degf", "celsius", "fahrenheit", "degree_celsius", "degree_fahrenheit", "°c", "°f"}
# Words that are not units even though pint knows them (or they are plain English).
_NOT_UNITS = {"or", "and", "if", "so", "then", "the", "a", "an", "is", "for", "at", "to", "in", "of", "by",
              "when", "where", "with", "and so", "hence", "therefore", "thus", "since", "let"}


def ureg():
    global _ureg
    if _ureg is None:
        with _lock:
            if _ureg is None:
                import pint
                _ureg = pint.UnitRegistry()
    return _ureg


@dataclass(frozen=True)
class UnitInfo:
    text: str                      # cleaned pint spelling, e.g. "km/h"
    factor: Any                    # SymPy number: value of 1 <unit> in SI base units
    dims: tuple                    # (("meter", 1), ("second", -1)) sorted, radians dropped

    def expr(self) -> sympy.Expr:
        e = self.factor
        for name, p in self.dims:
            e = e * base_symbol(name) ** p
        return e

    @property
    def dims_text(self) -> str:
        return dims_text(dict(self.dims))


def base_symbol(name: str) -> sympy.Symbol:
    return sympy.Symbol(f"[{name}]", positive=True)


def is_base_symbol(s: Any) -> bool:
    return isinstance(s, sympy.Symbol) and s.name.startswith(BASE_PREFIX)


def _unwrap(t: str) -> str:
    """\\mathrm{m\\,s^{-1}} -> m\\,s^{-1} (nested braces allowed)."""
    for _ in range(10):
        m = re.search(r"\\(?:text|mathrm|textrm|operatorname|rm|mbox)\s*\{", t)
        if not m:
            return t
        depth, j = 0, None
        for k in range(m.end() - 1, len(t)):
            depth += {"{": 1, "}": -1}.get(t[k], 0)
            if depth == 0:
                j = k
                break
        if j is None:
            return t
        t = t[:m.start()] + t[m.end():j] + t[j + 1:]
    return t


def _to_pint_text(s: str) -> str:
    t = str(s or "").strip()
    t = _unwrap(t)
    t = t.replace(r"\rm", "")
    for a, b in _REPL:
        t = t.replace(a, b)
    t = t.replace("{", "(").replace("}", ")")
    t = re.sub(r"\^\s*\(([^()]*)\)", r"**(\1)", t)
    t = re.sub(r"\^\s*(-?\d+(?:\.\d+)?)", r"**\1", t)
    t = re.sub(r"\s*\*\*\s*", "**", t)
    t = re.sub(r"\s*/\s*", "/", t)
    t = re.sub(r"\s*\*\s*", "*", t)
    t = " ".join(t.split())
    t = re.sub(r"(?<=[A-Za-z0-9)])\s+(?=[A-Za-z(])", "*", t)   # "kg m" -> "kg*m"
    return t.strip()


def _factor(mag: float) -> Any:
    if mag == 1:
        return sympy.Integer(1)
    if 1e-6 <= abs(mag) <= 1e9:
        r = sympy.Rational(mag).limit_denominator(10 ** 6)
        if abs(float(r) - mag) <= 1e-12 * abs(mag):
            return r
    return sympy.Float(mag, 15)


def parse_unit(s: Any) -> UnitInfo | None:
    """'km/h', '\\mathrm{m\\,s^{-1}}', 'N', 'kg m/s^2' -> UnitInfo; None if not a unit we can use
    (empty, plain words, offset temperatures, unknown)."""
    t = _to_pint_text(s)
    if not t or len(t) > 60 or t.lower() in _NOT_UNITS:
        return None
    if not re.fullmatch(r"[A-Za-z0-9*/()\-. ]+", t) or not re.search(r"[A-Za-z]", t):
        return None
    if any(w.lower() in _OFFSET for w in re.findall(r"[A-Za-z_]+", t)) or t.lower() in ("degc", "degf"):
        return None
    try:
        reg = ureg()
        q = reg.Quantity(1, t).to_base_units()
        mag = float(q.magnitude)
        items = dict(q.unit_items()) if hasattr(q, "unit_items") else dict(q.units._units)
    except Exception:  # noqa: BLE001 - undefined unit, syntax, offset units ...
        return None
    dims = []
    for name, p in items.items():
        if name == "radian":
            continue
        pf = float(p)
        if not pf.is_integer():
            return None
        dims.append((str(name), int(pf)))
    if mag != mag or mag == 0:
        return None
    return UnitInfo(text=t, factor=_factor(mag), dims=tuple(sorted(dims)))


def dims_text(d: dict) -> str:
    if not d:
        return "no unit"
    short = {"meter": "m", "second": "s", "kilogram": "kg", "ampere": "A", "kelvin": "K", "mole": "mol",
             "candela": "cd"}
    parts = []
    for k in sorted(d):
        p = d[k]
        if p == 0:
            continue
        parts.append(short.get(k, k) + ("" if p == 1 else f"^{p}"))
    return "·".join(parts) or "no unit"


# ---------------------------------------------------------------- dimensions of SymPy expressions
UNKNOWN = object()  # a term with a non-unit symbol: its dimension is not known


def _term_dims(t: sympy.Expr) -> Any:
    """dict of base-unit powers of a product term; UNKNOWN when it has other symbols."""
    d: dict[str, int] = {}
    for f in sympy.Mul.make_args(t):
        base, ex = (f.base, f.exp) if isinstance(f, sympy.Pow) else (f, sympy.Integer(1))
        if is_base_symbol(base):
            if not ex.is_number:
                return UNKNOWN
            name = base.name[1:-1]
            d[name] = d.get(name, 0) + int(ex)
            if d[name] == 0:
                d.pop(name)
        elif f.free_symbols - {s for s in f.free_symbols if is_base_symbol(s)}:
            return UNKNOWN
        elif any(is_base_symbol(s) for s in f.free_symbols):
            return UNKNOWN  # unit inside a function or sum: handled by the recursive check
    return d


def has_units(e: Any) -> bool:
    return isinstance(e, sympy.Basic) and any(is_base_symbol(s) for s in e.free_symbols)


def expr_dims(e: sympy.Expr) -> Any:
    """Base-unit signature of a whole expression (dict), UNKNOWN when not determinable."""
    try:
        e = sympy.expand(e)
    except Exception:  # noqa: BLE001
        return UNKNOWN
    sigs = [_term_dims(t) for t in sympy.Add.make_args(e)]
    if any(s is UNKNOWN for s in sigs) or not sigs:
        return UNKNOWN
    first = sigs[0]
    return first if all(s == first for s in sigs) else UNKNOWN


def dimension_problems(e: Any) -> list[str]:
    """Sums whose unit-carrying terms have different dimensions ("adds m and s")."""
    out: list[str] = []
    if not isinstance(e, sympy.Basic) or not has_units(e):
        return out
    for node in sympy.preorder_traversal(e):
        if isinstance(node, sympy.Add):
            sigs = []
            for t in node.args:
                s = _term_dims(t)
                if s is UNKNOWN or not has_units(t):
                    continue
                sigs.append(s)
            distinct = []
            for s in sigs:
                if s not in distinct:
                    distinct.append(s)
            if len(distinct) > 1:
                out.append("adds " + " and ".join(dims_text(s) for s in distinct[:3]))
        elif isinstance(node, sympy.Function) and not isinstance(node, sympy.Abs):
            for a in node.args:
                if isinstance(a, sympy.Basic) and has_units(a):
                    s = expr_dims(a)
                    if s is not UNKNOWN and s:
                        out.append(f"takes {type(node).__name__}() of a quantity in {dims_text(s)}")
    return list(dict.fromkeys(out))


def strip_units(e: sympy.Expr) -> sympy.Expr:
    """Value in SI base units with the unit symbols removed (for numeric comparisons)."""
    return e.subs({s: 1 for s in e.free_symbols if is_base_symbol(s)})


# ---------------------------------------------------------------- significant figures
_NUM = re.compile(r"^[+-]?(\d*)(?:\.(\d*))?$")


def sig_figs(text: Any) -> tuple[int, int] | None:
    """(min, max) significant figures of a written number ("19.6" -> (3,3), "1200" -> (2,4),
    "0.0250" -> (3,3), "2.50 \\times 10^{3}" -> (3,3)). None if it is not a plain number."""
    s = str(text or "").strip().replace(" ", "").replace(r"\,", "").replace("{", "").replace("}", "")
    s = s.replace(r"\times", "x").replace(r"\cdot", "x").replace("×", "x").replace("·", "x")
    m = re.match(r"^(.*?)(?:x10\^?(-?\d+)|[eE](-?\d+))$", s)
    if m:
        s = m.group(1)
    m = _NUM.match(s)
    if not m or not (m.group(1) or m.group(2)):
        return None
    ip, fp = m.group(1) or "", m.group(2)
    if fp is None:  # integer
        digits = ip.lstrip("0")
        if not digits:
            return (1, 1)
        core = digits.rstrip("0")
        return (len(core), len(digits))
    allds = (ip + fp).lstrip("0")
    if not allds:
        return (max(1, len(fp)), max(1, len(fp)))
    return (len(allds), len(allds))


def required_sig_figs(ctx: dict) -> int | None:
    """Teacher's key `sig_figs`, else a rubric criterion like "answer to 3 significant figures"."""
    v = ctx.get("sig_figs")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= int(v) <= 15:
        return int(v)
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    for c in ctx.get("criteria") or []:
        text = f"{c.get('label', '')} {c.get('description', '')}".lower()
        m = re.search(r"(\d+|one|two|three|four|five|six)\s*(?:significant\s*fig|sig\.?\s*figs?|s\.?\s*f\b)", text)
        if m:
            g = m.group(1)
            return int(g) if g.isdigit() else words[g]
    return None


def rounding_tolerance(text: Any) -> float | None:
    """Half a unit in the last written decimal place ("19.6" -> 0.05, "3.33e2" -> 0.5)."""
    s = str(text or "").strip().replace(" ", "").replace("{", "").replace("}", "")
    s = s.replace(r"\times", "x").replace(r"\cdot", "x").replace("×", "x")
    exp = 0
    m = re.match(r"^(.*?)(?:x10\^?(-?\d+)|[eE](-?\d+))$", s)
    if m:
        s, exp = m.group(1), int(m.group(2) or m.group(3))
    m = _NUM.match(s)
    if not m or not (m.group(1) or m.group(2)):
        return None
    decimals = len(m.group(2) or "")
    return 0.5 * 10 ** (exp - decimals)
