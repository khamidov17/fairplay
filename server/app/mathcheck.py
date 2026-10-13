"""Safe math parsing and answer checking with SymPy (slice 6, practice assignments).

* `parse(text)` turns student/teacher math ("2x+2", "1/2", "sqrt(8)", "sin^2(x)", "3·π",
  "x = 4") into a SymPy expression. Input is length-limited and character-whitelisted,
  identifiers are resolved against a fixed table of functions/constants, and the parser
  only sees a curated global namespace (no builtins), so arbitrary Python never runs.
  Expressions are first parsed unevaluated and checked for size (huge powers such as
  9^9^9^9 are rejected before SymPy tries to compute them).
* `equivalent(a, b)` decides whether two answers mean the same thing:
  numbers compare with a tolerance; expressions use simplify(a-b) == 0 under a time
  budget with random numeric sampling as fallback.
* `evaluate_formula(formula, values)` computes a practice answer from a stored formula
  (variables n1..n9 stand for the numbers in a question's text).

All heavy SymPy work runs on a small worker pool with a timeout; callers on the event
loop should use the `*_async` helpers.
"""
from __future__ import annotations

import asyncio
import cmath
import concurrent.futures
import math
import os
import queue
import random
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import sympy
from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application,
                                        parse_expr, standard_transformations)

MAX_LEN = 200            # characters of user input
MAX_OPS_LEN = 400        # AI-provided check expressions (may call expand/diff/...)
MAX_EXPONENT = 1000      # |constant exponent|
MAX_LOG10 = 3000         # estimated decimal digits of any constant sub-expression
MAX_NODES = 400          # nodes in the expression tree
MAX_TERMS = 5000         # audit L-02: terms an expand() of the expression could produce
MAX_SYMBOLIC_POWER = 12  # audit L-02: (a+b)^n with symbols, n above this is refused
DEFAULT_TIMEOUT = 2.0    # seconds per check

_TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)


class MathError(ValueError):
    """The text is not a math expression we accept (or it is too big / too slow)."""


# ---------------------------------------------------------------- namespace
_FUNCS: dict[str, Any] = {
    "sin": sympy.sin, "cos": sympy.cos, "tan": sympy.tan, "sec": sympy.sec, "csc": sympy.csc,
    "cot": sympy.cot, "asin": sympy.asin, "acos": sympy.acos, "atan": sympy.atan,
    "arcsin": sympy.asin, "arccos": sympy.acos, "arctan": sympy.atan,
    "sinh": sympy.sinh, "cosh": sympy.cosh, "tanh": sympy.tanh,
    "sqrt": sympy.sqrt, "exp": sympy.exp, "log": sympy.log, "ln": sympy.log, "abs": sympy.Abs,
}
_CONSTS: dict[str, Any] = {"pi": sympy.pi, "e": sympy.E, "E": sympy.E}
# Only for formulas/checks written by the AI or a template, never for student answers.
_OPS: dict[str, Any] = {"expand": sympy.expand, "factor": sympy.factor, "simplify": sympy.simplify,
                        "diff": sympy.diff, "integrate": sympy.integrate, "Rational": sympy.Rational}
_FORMULA_VARS = {f"n{i}": sympy.Symbol(f"n{i}") for i in range(1, 10)}

# What the transformed code may reference. No builtins, no import machinery.
_GLOBALS: dict[str, Any] = {
    "Integer": sympy.Integer, "Float": sympy.Float, "Rational": sympy.Rational,
    "Number": sympy.Number, "Symbol": sympy.Symbol, "Function": sympy.Function,
    "Pow": sympy.Pow, "Mul": sympy.Mul, "Add": sympy.Add, "I": sympy.I,
}

_ALLOWED_CHARS = re.compile(r"^[0-9A-Za-z+\-*/^().=\s]*$")
_ALLOWED_CHARS_OPS = re.compile(r"^[0-9A-Za-z+\-*/^().=,\s]*$")
_UNICODE = [("×", "*"), ("⋅", "*"), ("·", "*"), ("÷", "/"), ("−", "-"), ("–", "-"),
            ("π", "pi"), ("²", "^2"), ("³", "^3"), ("√", "sqrt"), ("**", "^")]
_POW_FUNC = re.compile(r"\b(sin|cos|tan|sec|csc|cot|sinh|cosh|tanh)\s*\^\s*(\d+)\s*\(")


def _rewrite_func_powers(s: str) -> str:
    """sin^2(x) -> (sin(x))^2 (textbook notation)."""
    for _ in range(10):
        m = _POW_FUNC.search(s)
        if not m:
            break
        depth, j = 0, m.end() - 1
        while j < len(s):
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            raise MathError("Unbalanced parentheses")
        inner = s[m.end():j]
        s = f"{s[:m.start()]}({m.group(1)}({inner}))^{m.group(2)}{s[j + 1:]}"
    return s


def _prepare(text: str, allow_ops: bool) -> str:
    if not isinstance(text, str):
        text = str(text)
    s = text.strip()
    if not s:
        raise MathError("Empty answer")
    if len(s) > (MAX_OPS_LEN if allow_ops else MAX_LEN):
        raise MathError("Answer is too long")
    for a, b in _UNICODE:
        s = s.replace(a, b)
    s = re.sub(r"sqrt\s*(\d+(?:\.\d+)?|[A-Za-z])(?![A-Za-z(])", r"sqrt(\1)", s)  # √8 -> sqrt(8)
    s = re.sub(r"\|([^|]+)\|", r"abs(\1)", s)
    if not (_ALLOWED_CHARS_OPS if allow_ops else _ALLOWED_CHARS).match(s):
        raise MathError("Only numbers, letters and + - * / ^ ( ) are allowed")
    if re.search(r"\.\s*[A-Za-z(]", s) or ".." in s:
        raise MathError("Unexpected '.'")
    # "x = 4" (answer written as an assignment) -> "4"; other equations are not answers.
    if "=" in s:
        parts = s.split("=")
        if len(parts) == 2 and re.fullmatch(r"\s*[A-Za-z]\s*", parts[0]):
            s = parts[1]
        else:
            raise MathError("Write just the value or expression, not an equation")
    s = _rewrite_func_powers(s)
    for word in re.findall(r"[A-Za-z]+\d*", s):
        if word in _FUNCS or word in _CONSTS or (allow_ops and (word in _OPS or word in _FORMULA_VARS)):
            continue
        if word in _FORMULA_VARS:
            continue
        # Single letters / letter pairs are variables ("2xy"); longer unknown words are text.
        if len(word) > (12 if allow_ops else 2):
            raise MathError(f"Unknown word “{word}” — use numbers, variables and functions like sqrt, sin, log")
    if s.count("(") != s.count(")"):
        raise MathError("Unbalanced parentheses")
    return s


# ---------------------------------------------------------------- size guard
def _log10_mag(e: sympy.Basic) -> float | None:
    """Estimated decimal digits of a constant sub-expression (None if it has symbols).
    Raises MathError when something would be astronomically large."""
    if e.is_Integer:
        return float(len(str(abs(int(e)))))
    if e.is_Rational:
        return float(max(len(str(abs(e.p))), len(str(e.q))))
    if e.is_Float:
        f = abs(float(e))
        return math.log10(f) if f > 1 else 0.0
    if e.is_Symbol:
        return None
    if e.is_Number or e in (sympy.pi, sympy.E, sympy.I):
        return 1.0
    mags = [_log10_mag(a) for a in e.args]
    if e.is_Pow:
        base, ex = e.args
        mb, mx = mags
        if mx is not None:  # constant exponent: bound it before evaluating
            if mx > 5:
                raise MathError("Number is too large")
            xv = complex(sympy.N(ex, 15))
            if abs(xv) > MAX_EXPONENT and base not in (sympy.Integer(1), sympy.Integer(-1), sympy.Integer(0)):
                raise MathError("Exponent is too large")
            if mb is not None:
                est = abs(xv.real) * max(mb, 1.0)
                if est > MAX_LOG10:
                    raise MathError("Number is too large")
                return est
        return None
    if any(m is None for m in mags):
        for m in mags:
            if m is not None and m > MAX_LOG10:
                raise MathError("Number is too large")
        return None
    if e.is_Mul:
        est = sum(mags)
    elif e.is_Add:
        est = max(mags, default=0.0) + 1
    else:
        est = max(mags, default=0.0)
    if est > MAX_LOG10:
        raise MathError("Number is too large")
    return est


def _terms(e: sympy.Basic) -> int:
    """Upper bound on the number of terms expand(e) produces (audit L-02):
    (a+b+c+d+f+g)^999 parses in milliseconds but expands to ~10^12 terms."""
    if not e.free_symbols:
        return 1
    if e.is_Add:
        return sum(_terms(a) for a in e.args)
    if e.is_Mul:
        n = 1
        for a in e.args:
            n *= _terms(a)
            if n > MAX_TERMS:
                return n
        return n
    if e.is_Pow:
        base, ex = e.args
        t = _terms(base)
        if ex.is_Integer and t > 1:
            k = abs(int(ex))
            if k > MAX_SYMBOLIC_POWER:
                return MAX_TERMS + 1
            return math.comb(k + t - 1, t - 1)
        return t
    return max((_terms(a) for a in e.args), default=1)


def _check_size(e: sympy.Basic) -> None:
    n = 0
    for _ in sympy.preorder_traversal(e):
        n += 1
        if n > MAX_NODES:
            raise MathError("Expression is too long")
    _log10_mag(e)
    if _terms(e) > MAX_TERMS:
        raise MathError("Expression is too big to check")


def _parse_sync(text: str, allow_ops: bool = False) -> sympy.Expr:
    s = _prepare(text, allow_ops)
    local: dict[str, Any] = {**_FUNCS, **_CONSTS, **_FORMULA_VARS}
    if allow_ops:
        local.update(_OPS)
    try:
        raw = parse_expr(s, local_dict=dict(local), global_dict={"__builtins__": {}, **_GLOBALS},
                         transformations=_TRANSFORMS, evaluate=False)
    except MathError:
        raise
    except Exception as e:  # SyntaxError, TypeError, TokenError, NameError ...
        raise MathError("Could not read this as math") from e
    if not isinstance(raw, sympy.Expr):
        raise MathError("Could not read this as math")
    _check_size(raw)
    try:
        expr = parse_expr(s, local_dict=dict(local), global_dict={"__builtins__": {}, **_GLOBALS},
                          transformations=_TRANSFORMS, evaluate=True)
    except Exception as e:
        raise MathError("Could not read this as math") from e
    if not isinstance(expr, sympy.Expr):
        raise MathError("Could not read this as math")
    if expr.has(sympy.zoo, sympy.nan):
        raise MathError("Undefined value (division by zero?)")
    return expr


# ---------------------------------------------------------------- time limits
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="mathcheck")
# simplify() is started from inside a _pool task: separate pool so nesting cannot deadlock.
_inner_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="mathcheck-s")

# Audit L-02: a thread that runs away can't be stopped, and four of them used to block every
# math check on the server. By default the SymPy work runs in worker PROCESSES with memory
# and CPU limits; one that is still busy at its deadline is killed and replaced.
# FAIRPLAY_MATH_PROCESSES=0 keeps everything in threads (tests that monkeypatch internals).
# Per server process, started on first use (~100 MB each with SymPy loaded).
PROCESS_WORKERS = max(1, int(os.environ.get("FAIRPLAY_MATH_WORKERS", "2") or 2))
WORKER_MEMORY_BYTES = 1024 * 1024 * 1024
WORKER_CPU_SECONDS = 600            # whole life of a worker; it is replaced when it dies


def _use_processes() -> bool:
    return os.environ.get("FAIRPLAY_MATH_PROCESSES", "1") != "0"


def _worker_main(conn) -> None:  # pragma: no cover - runs in the child process
    try:
        import resource
        for lim, val in ((getattr(resource, "RLIMIT_AS", None), WORKER_MEMORY_BYTES),
                         (getattr(resource, "RLIMIT_CPU", None), WORKER_CPU_SECONDS)):
            if lim is not None:
                try:
                    resource.setrlimit(lim, (val, val))
                except (ValueError, OSError):
                    pass            # macOS ignores RLIMIT_AS; the kill on timeout still holds
    except ImportError:
        pass
    os.environ["FAIRPLAY_MATH_PROCESSES"] = "0"     # inside a worker: plain threads
    while True:
        try:
            name, args = conn.recv()
        except (EOFError, OSError):
            return
        try:
            conn.send((True, _SYNC[name](*args)))
        except MathError as e:
            conn.send((False, str(e)))
        except MemoryError:
            conn.send((False, "This took too much memory to check"))
        except Exception as e:
            conn.send((False, f"Could not check this ({type(e).__name__})"))


class _Worker:
    def __init__(self) -> None:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_worker_main, args=(child,), daemon=True,
                                name="fairplay-mathcheck")
        self.proc.start()
        child.close()

    def kill(self) -> None:
        try:
            self.proc.kill()
            self.proc.join(1)
        except Exception:  # pragma: no cover
            pass
        try:
            self.conn.close()
        except Exception:  # pragma: no cover
            pass


class _ProcessPool:
    def __init__(self, size: int) -> None:
        self.size = size
        self._idle: "queue.Queue[_Worker]" = queue.Queue()
        self._lock = threading.Lock()
        self._started = False
        self.killed = 0             # for tests and logs

    def _start(self) -> None:
        with self._lock:
            if not self._started:
                for _ in range(self.size):
                    self._idle.put(_Worker())
                self._started = True

    def run(self, name: str, args: tuple, timeout: float) -> Any:
        self._start()
        try:
            w = self._idle.get(timeout=max(timeout, 5.0))   # all busy: wait a little
        except queue.Empty:
            raise MathError("The server is busy checking math. Try again.") from None
        try:
            w.conn.send((name, args))
            if not w.conn.poll(timeout):
                raise TimeoutError
            ok, value = w.conn.recv()
        except (TimeoutError, EOFError, OSError, BrokenPipeError):
            w.kill()
            self.killed += 1
            self._idle.put(_Worker())
            raise MathError("This took too long to check") from None
        self._idle.put(w)
        if ok:
            return value
        raise MathError(value)


_procs = _ProcessPool(PROCESS_WORKERS)


def run_limited(fn: Callable[..., Any], *args: Any, timeout: float = DEFAULT_TIMEOUT,
                pool: concurrent.futures.ThreadPoolExecutor | None = None) -> Any:
    """Run fn with a deadline; too slow -> MathError. Registered SymPy entry points go to a
    killable worker process (see above); anything else runs on the bounded thread pool."""
    name = getattr(fn, "__name__", "")
    if pool is None and _use_processes() and _SYNC.get(name) is fn:
        return _procs.run(name, args, timeout)
    fut = (pool or _pool).submit(fn, *args)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError as e:
        fut.cancel()
        raise MathError("This took too long to check") from e


def parse(text: str, *, allow_ops: bool = False, timeout: float = DEFAULT_TIMEOUT) -> sympy.Expr:
    return run_limited(_parse_sync, text, allow_ops, timeout=timeout)


def try_parse(text: Any, **kw: Any) -> sympy.Expr | None:
    try:
        return parse(str(text), **kw)
    except MathError:
        return None


def _to_complex(e: sympy.Expr) -> complex:
    v = complex(sympy.N(e, 30))
    if not (cmath.isfinite(v)):
        raise MathError("Not a finite number")
    return v


def parse_number(text: Any, *, timeout: float = DEFAULT_TIMEOUT) -> float:
    """A real number from "0.5", "1/2", "2sqrt(2)", "3.5e2"... (no variables)."""
    if isinstance(text, bool):
        raise MathError("Not a number")
    if isinstance(text, (int, float)):
        try:
            f = float(text)
        except OverflowError as e:      # audit 2026-10-13: a huge JSON integer
            raise MathError("Not a finite number") from e
        if not math.isfinite(f):
            raise MathError("Not a finite number")
        return f
    s = str(text).strip().replace(",", "") if re.fullmatch(r"\s*-?\d{1,3}(,\d{3})+(\.\d+)?\s*", str(text)) \
        else str(text).strip()
    m = re.fullmatch(r"([-+]?\d*\.?\d+)(?:[eE]([-+]?\d{1,3}))?\s*(%?)", s)
    if m:
        f = float(m.group(1) + (f"e{m.group(2)}" if m.group(2) else ""))
        if not math.isfinite(f):        # "9e999" -> inf
            raise MathError("Not a finite number")
        return f / 100 if m.group(3) else f

    return run_limited(_parse_number_sync, s, timeout=timeout)


def _parse_number_sync(s: str) -> float:
    e = _parse_sync(s)
    if e.free_symbols:
        raise MathError("Expected a number")
    v = _to_complex(e)
    if abs(v.imag) > 1e-12 * max(1.0, abs(v.real)):
        raise MathError("Expected a real number")
    return v.real


def is_number_text(text: Any) -> bool:
    try:
        parse_number(text)
        return True
    except MathError:
        return False


_WORD = re.compile(r"[A-Za-z]+")


def looks_like_math(text: Any) -> bool:
    """True for "2x+2", "1/2", "sqrt(8)", "x^2-1"; False for words like "paris"."""
    if not isinstance(text, (str, int, float)) or isinstance(text, bool):
        return False
    s = str(text).strip()
    if not s or len(s) > MAX_LEN:
        return False
    for w in _WORD.findall(s):
        if w in _FUNCS or w in _CONSTS:
            continue
        if len(w) >= 3:
            return False
    if not re.search(r"[\d+\-*/^()=√π²³]", s) and not re.fullmatch(r"[A-Za-z]{1,2}", s):
        return False
    return try_parse(s) is not None


# ---------------------------------------------------------------- equivalence
@dataclass
class Verdict:
    equal: bool | None          # None = could not decide
    method: str                 # "numeric" | "exact" | "simplify" | "sampling" | "error" | "timeout"
    error: str | None = None


def _close(a: complex, b: complex, abs_tol: float | None, rel_tol: float) -> bool:
    diff = abs(a - b)
    if abs_tol is not None and diff <= abs_tol + 1e-12:
        return True
    return diff <= rel_tol * max(1.0, abs(a), abs(b))


def _sample(ea: sympy.Expr, eb: sympy.Expr, syms: list, abs_tol: float | None,
            rel_tol: float) -> bool | None:
    rng = random.Random(20261011)
    good = 0
    for _ in range(24):
        subs = {s: sympy.Float(rng.choice([-1, 1]) * rng.uniform(0.3, 2.7), 20) for s in syms}
        try:
            va = complex(ea.evalf(20, subs=subs))
            vb = complex(eb.evalf(20, subs=subs))
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            continue
        if not (cmath.isfinite(va) and cmath.isfinite(vb)):
            continue
        if not _close(va, vb, abs_tol, max(rel_tol, 1e-9)):
            return False
        good += 1
        if good >= 8:
            return True
    return True if good >= 3 else None


def _equivalent_sync(a: Any, b: Any, abs_tol: float | None, rel_tol: float,
                     simplify_budget: float) -> Verdict:
    try:
        ea = a if isinstance(a, sympy.Basic) else _parse_sync(str(a))
        eb = b if isinstance(b, sympy.Basic) else _parse_sync(str(b))
    except MathError as e:
        return Verdict(None, "error", str(e))
    syms = sorted(ea.free_symbols | eb.free_symbols, key=lambda s: s.name)
    if not syms:
        try:
            return Verdict(_close(_to_complex(ea), _to_complex(eb), abs_tol, rel_tol), "numeric")
        except (MathError, TypeError, ValueError) as e:
            return Verdict(None, "error", str(e))
    try:
        if sympy.expand(ea - eb) == 0:
            return Verdict(True, "exact")
    except Exception:  # pragma: no cover - sympy internals
        pass
    # simplify(a-b) == 0 under its own small budget (nested pool call -> separate worker).
    try:
        d = run_limited(lambda: sympy.simplify(ea - eb), timeout=simplify_budget,
                        pool=_inner_pool)
        if d == 0:
            return Verdict(True, "simplify")
    except MathError:
        pass
    except Exception:  # pragma: no cover
        pass
    res = _sample(ea, eb, syms, abs_tol, rel_tol)
    return Verdict(res, "sampling")


def check(a: Any, b: Any, *, abs_tol: float | None = None, rel_tol: float = 1e-9,
          timeout: float = DEFAULT_TIMEOUT) -> Verdict:
    """Full verdict for "is a the same as b?"."""
    try:
        return run_limited(_equivalent_sync, a, b, abs_tol, rel_tol, max(0.2, timeout * 0.6),
                           timeout=timeout)
    except MathError as e:
        return Verdict(None, "timeout", str(e))


def equivalent(a: Any, b: Any, **kw: Any) -> bool:
    return check(a, b, **kw).equal is True


def numbers_equal(given: Any, expected: float, tolerance: float | None = None) -> bool:
    """Student numeric answer vs key. Accepts "0.5", "1/2", "sqrt(2)" ..."""
    try:
        g = parse_number(given)
    except MathError:
        return False
    tol = float(tolerance or 0)
    return abs(g - float(expected)) <= tol + 1e-9 * max(1.0, abs(float(expected)))


# ---------------------------------------------------------------- formulas
def _exact(v: Any) -> sympy.Expr:
    """Exact SymPy number for a value found in text (3, 2.5, '1.25')."""
    try:
        return sympy.Rational(str(Decimal(str(v))))
    except (InvalidOperation, ValueError, TypeError) as e:
        raise MathError("Bad value") from e


def _evaluate_formula_sync(formula: str, values: dict[str, Any]) -> sympy.Expr:
    expr = _parse_sync(formula, allow_ops=True)
    unknown = {s.name for s in expr.free_symbols} - set(values)
    if unknown:
        raise MathError(f"Formula uses unknown values: {', '.join(sorted(unknown))}")
    subs = {sympy.Symbol(k): _exact(v) for k, v in values.items()}
    out = expr.subs(subs)
    out = sympy.nsimplify(out) if out.has(sympy.Float) else out
    if out.free_symbols:
        raise MathError("Formula did not produce a number")
    _to_complex(out)
    return out


def evaluate_formula(formula: str, values: dict[str, Any], *,
                     timeout: float = DEFAULT_TIMEOUT) -> float:
    """Real value of `formula` with n1..n9 replaced by `values`."""
    return run_limited(_formula_value_sync, formula, values, timeout=timeout)


def _formula_value_sync(formula: str, values: dict[str, Any]) -> float:
    v = _to_complex(_evaluate_formula_sync(formula, values))
    if abs(v.imag) > 1e-12 * max(1.0, abs(v.real)):
        raise MathError("Formula gave a complex number")
    return v.real


def evaluate_check(expression: str, *, timeout: float = DEFAULT_TIMEOUT) -> sympy.Expr:
    """Evaluate an AI-provided check like "expand((x+1)^2)" (expand/factor/diff/... allowed)."""
    return run_limited(_parse_ops_sync, expression, timeout=timeout)


def _parse_ops_sync(expression: str) -> sympy.Expr:
    return _parse_sync(expression, allow_ops=True)


def pretty(expr: Any) -> str:
    """Compact display form: "2*x + 2" -> "2x + 2" is left to the UI; we return sympy str."""
    if isinstance(expr, float):
        return format_number(expr)
    return str(expr).replace("**", "^")


def format_number(v: float) -> str:
    if float(v).is_integer() and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.10g}"


# ---------------------------------------------------------------- async helpers
async def check_async(a: Any, b: Any, **kw: Any) -> Verdict:
    return await asyncio.to_thread(check, a, b, **kw)


async def numbers_equal_async(given: Any, expected: float, tolerance: float | None = None) -> bool:
    return await asyncio.to_thread(numbers_equal, given, expected, tolerance)


# Entry points a worker process may run (by name: functions are looked up, never pickled).
_SYNC: dict[str, Callable[..., Any]] = {
    "_parse_sync": _parse_sync,
    "_parse_number_sync": _parse_number_sync,
    "_formula_value_sync": _formula_value_sync,
    "_parse_ops_sync": _parse_ops_sync,
    "_equivalent_sync": _equivalent_sync,
}
# Tests only: a slow entry point, to prove a stuck worker is killed (needs the env var
# in the parent before the pool starts; spawned workers inherit it).
if os.environ.get("FAIRPLAY_MATH_SELFTEST") == "1":
    import time as _time
    _SYNC["_selftest_sleep"] = _time.sleep
