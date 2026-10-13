"""Slice 6 H: SymPy-based math parsing and equivalence (app.mathcheck)."""
import time

import pytest

from app import mathcheck as m


@pytest.mark.parametrize("a,b", [
    ("2x+2", "2(x+1)"),
    ("1/2", "0.5"),
    ("sqrt(8)", "2*sqrt(2)"),
    ("√8", "2√2"),
    ("sin^2(x)+cos^2(x)", "1"),
    ("sin(x)^2 + cos(x)^2", "1"),
    ("x^2-1", "(x-1)(x+1)"),
    ("0.5x", "x/2"),
    ("3·π", "3pi"),
    ("x = 4", "4"),
    ("e^(ln 3)", "3"),
    ("2^10", "1024"),
    ("(a+b)^2", "a^2 + 2ab + b^2"),
    ("1/3", "0.333333333333333333"),
])
def test_equivalent(a, b):
    assert m.equivalent(a, b), m.check(a, b)


@pytest.mark.parametrize("a,b", [
    ("2x", "x+2"),
    ("1/2", "0.51"),
    ("sqrt(8)", "3"),
    ("x^2", "x^3"),
    ("sin(x)", "cos(x)"),
])
def test_not_equivalent(a, b):
    assert m.check(a, b).equal is False


def test_float_tolerance():
    assert m.equivalent("3.14", "pi", abs_tol=0.01)
    assert not m.equivalent("3.14", "pi")
    assert m.numbers_equal("3.141", 3.14159, 0.01)
    assert m.numbers_equal("1/2", 0.5)
    assert m.numbers_equal("2sqrt(2)", 2.8284271247461903)
    assert not m.numbers_equal("abc", 1)


def test_parse_number_forms():
    assert m.parse_number("0.5") == 0.5
    assert m.parse_number("1/4") == 0.25
    assert m.parse_number("-3") == -3
    assert m.parse_number("1,000") == 1000
    assert m.parse_number("50%") == 0.5
    assert m.parse_number(7) == 7
    with pytest.raises(m.MathError):
        m.parse_number("x+1")


@pytest.mark.parametrize("bad", [
    "__import__('os')",
    "__import__('os').system('echo hacked')",
    "().__class__.__bases__",
    "lambda: 1",
    "open('/etc/passwd')",
    "exec(1)",
    "eval(1)",
    "x.__class__",
    "1; import os",
    "[1, 2]",
    "a" * 500,
])
def test_rejects_malicious_or_odd_input(bad):
    with pytest.raises(m.MathError):
        m.parse(bad)
    assert m.check(bad, "1").equal is None


@pytest.mark.parametrize("huge", ["9^9^9^9", "2^(10^10)", "x^100000", "((2^500)^500)^500", "10^10^10"])
def test_huge_exponents_fail_fast(huge):
    t = time.monotonic()
    with pytest.raises(m.MathError):
        m.parse(huge)
    assert m.check(huge, "1").equal is None
    assert time.monotonic() - t < 2


def test_timeout_is_safe(monkeypatch):
    def slow(*a, **k):
        time.sleep(5)

    t = time.monotonic()
    with pytest.raises(m.MathError):
        m.run_limited(slow, timeout=0.2)
    assert time.monotonic() - t < 1
    monkeypatch.setattr(m, "_equivalent_sync", slow)
    v = m.check("x", "x", timeout=0.2)
    assert v.equal is None and v.method == "timeout"


def test_looks_like_math():
    for s in ["2x+2", "1/2", "sqrt(8)", "x", "x^2-1"]:
        assert m.looks_like_math(s), s
    for s in ["paris", "New York", "photosynthesis", ""]:
        assert not m.looks_like_math(s), s


def test_formulas():
    assert m.evaluate_formula("n1*n2/2", {"n1": 3, "n2": 5}) == 7.5
    assert m.evaluate_formula("sqrt(n1^2+n2^2)", {"n1": "3", "n2": "4"}) == 5
    assert m.evaluate_formula("n1*(1 + n2/100)", {"n1": "80", "n2": "2.5"}) == pytest.approx(82)
    with pytest.raises(m.MathError):
        m.evaluate_formula("n1 + n3", {"n1": 1})
    with pytest.raises(m.MathError):
        m.evaluate_formula("n1/n2", {"n1": 1, "n2": 0})
    assert m.equivalent(m.evaluate_check("expand((x+1)^2)"), "x^2+2x+1")
    assert m.equivalent(m.evaluate_check("diff(x^3, x)"), "3x^2")


# ---------------------------------------------------------------- audit L-02
@pytest.mark.parametrize("bad", ["(a+b+c+d+f+g)^999", "(x+1)^13", "((x+y)^12)^12", "(a+b)^12*(c+d)^12*(e+f)^12*(g+h)^12"])
def test_symbolic_blowups_are_refused_before_sympy_expands(bad):
    import time as _t
    t0 = _t.monotonic()
    with pytest.raises(m.MathError):
        m.parse(bad)
    assert _t.monotonic() - t0 < 5
    v = m.check(bad, "1")
    assert v.equal is None


def test_small_powers_still_work():
    assert m.equivalent("(x+1)^2", "x^2+2x+1")
    assert m.equivalent("(a+b)^12", "(b+a)^12")
    assert m.equivalent("x^50", "x^50")              # one term, any power


def test_checks_run_in_killable_worker_processes(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_MATH_PROCESSES", "1")
    assert m.parse_number("1/2") == 0.5
    assert m.equivalent("2x+2", "2(x+1)")
    assert m._procs._started and m._procs.size == m.PROCESS_WORKERS


def test_a_runaway_worker_is_killed_and_replaced(monkeypatch):
    import time as _t
    monkeypatch.setenv("FAIRPLAY_MATH_SELFTEST", "1")
    pool = m._ProcessPool(1)
    try:
        pool.run("_parse_number_sync", ("2",), 30)        # worker up (spawn imports sympy)
        t0 = _t.monotonic()
        with pytest.raises(m.MathError, match="too long"):
            pool.run("_selftest_sleep", (3600,), 0.5)
        assert _t.monotonic() - t0 < 3 and pool.killed == 1
        assert pool.run("_parse_number_sync", ("1/4",), 30) == 0.25   # replaced, still works
        with pytest.raises(m.MathError):
            pool.run("_no_such_entry", (), 5)              # unknown names are refused
    finally:
        while not pool._idle.empty():
            pool._idle.get().kill()


def test_parse_has_no_builtins():
    with pytest.raises(m.MathError):
        m.parse("__import__('os')")
