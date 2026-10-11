"""Step verification (math/physics): LaTeX steps checked with SymPy + pint, ECF, final answer vs key,
reconciliation with the AI's marks. AI calls go through a fake httpx transport."""
import asyncio
import json
import time

import httpx
import pytest

from app import ai
from app.scan import ai_grade, mathverify as mv, units as U

from .test_scan import PAPER_EXAM, _attempt, _scan_class, _wait  # noqa: F401


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    monkeypatch.delenv("FAIRPLAY_STEP_VERIFY", raising=False)
    yield
    ai.set_transport(None)


def chain(lines, physics=False):
    res, _ = mv.verify_steps([(i + 1, ln, "correct") for i, ln in enumerate(lines)], physics=physics)
    return [(r["status"], r["ecf"]) for r in res], res


OK, ERR, UNK = mv.OK, mv.ERR, mv.UNK

# (lines, physics, expected [(status, ecf)] per line, substring expected in some reason)
CHAINS = [
    # algebra: solving
    ([r"2x + 3 = 7", r"2x = 4", r"x = 2"], False, [(UNK, False), (OK, False), (OK, False)], "follows"),
    # sign error (moved +3 without changing sign), then ECF
    ([r"2x + 3 = 7", r"2x = 7 + 3", r"x = 5"], False, [(UNK, False), (ERR, False), (OK, True)], "solution changes"),
    # dropped term
    ([r"x^2 + 3x = 10", r"x^2 = 10"], False, [(UNK, False), (ERR, False)], "does not follow"),
    # both sides transformed alike (multiply by 6)
    ([r"\frac{x}{2} + \frac{x}{3} = 5", r"3x + 2x = 30", r"5x = 30", r"x = 6"], False,
     [(UNK, False), (OK, False), (OK, False), (OK, False)], "transformed alike"),
    # brackets expanded wrong -> error, later lines follow the wrong value (ECF)
    ([r"3(x - 2) = 2x + 1", r"3x - 2 = 2x + 1", r"x = 3"], False, [(UNK, False), (ERR, False), (OK, True)],
     "correct given the error in step 2"),
    # factorising and solution sets
    ([r"x^2 - 5x + 6 = 0", r"(x-2)(x-3) = 0", r"x = 2 \text{ or } x = 3"], False,
     [(UNK, False), (OK, False), (OK, False)], "same solutions"),
    ([r"x^2 = 4", r"x = 2"], False, [(UNK, False), (ERR, False)], "loses solution"),
    ([r"x^2 = 4", r"x = \pm 2"], False, [(UNK, False), (OK, False)], "same solutions"),
    # physics keeps the positive root
    ([r"t^2 = 4", r"t = 2"], True, [(UNK, False), (OK, False)], "physical"),
    # fractions, "=" continuation lines
    ([r"\frac{1}{2} + \frac{1}{3}", r"= \frac{3}{6} + \frac{2}{6}", r"= \frac{5}{6}"], False,
     [(UNK, False), (OK, False), (OK, False)], "follows"),
    ([r"\frac{1}{2} + \frac{1}{3}", r"= \frac{2}{5}"], False, [(UNK, False), (ERR, False)], "previous line"),
    # algebraic simplification of expressions
    ([r"(x+1)^2 - (x-1)^2", r"= x^2 + 2x + 1 - x^2 + 2x - 1", r"= 4x"], False,
     [(UNK, False), (OK, False), (OK, False)], "follows"),
    ([r"(x+1)^2", r"x^2 + 1"], False, [(UNK, False), (ERR, False)], "not equal"),
    # derivative / integral claims
    ([r"\frac{d}{dx}\left(x^3 + 2x\right) = 3x^2 + 2"], False, [(OK, False)], "checks"),
    ([r"\frac{d}{dx}(x^3) = 3x"], False, [(ERR, False)], "derivative is wrong"),
    ([r"f(x) = x^3 - 2x", r"f'(x) = 3x^2 - 2", r"f'(2) = 10"], False, [(UNK, False), (OK, False), (OK, False)], ""),
    ([r"f(x) = x^3 - 2x", r"f'(x) = 3x^2 - 2x"], False, [(UNK, False), (ERR, False)], "derivative"),
    ([r"y = \sin x \cos x", r"\frac{dy}{dx} = \cos^2 x - \sin^2 x"], False, [(UNK, False), (OK, False)], ""),
    ([r"\int_0^1 x^2\,dx = \frac{1}{3}"], False, [(OK, False)], ""),
    ([r"\int_0^2 3x^2\,dx = 6"], False, [(ERR, False)], "integral"),
    ([r"\int 2x\,dx = x^2 + C"], False, [(OK, False)], "antiderivative"),
    ([r"\int 2x\,dx = 2x^2 + C"], False, [(ERR, False)], "integrand"),
    # arithmetic + numeric rounding of written decimals
    ([r"2 \times 3 + 4 = 10"], False, [(OK, False)], ""),
    ([r"\frac{10}{3}", r"= 3.33"], False, [(UNK, False), (OK, False)], ""),
    ([r"\frac{10}{3}", r"= 3.5"], False, [(UNK, False), (ERR, False)], ""),
    # physics: formula rearranged, values substituted (not checkable), units added
    ([r"v = u + at", r"a = \frac{v - u}{t}"], True, [(UNK, False), (OK, False)], "solved for a"),
    ([r"v = u + at", r"a = \frac{v + u}{t}"], True, [(UNK, False), (ERR, False)], ""),
    ([r"v = u + at", r"v = 0 + 9.8 \cdot 2", r"v = 19.6 \text{ m/s}"], True,
     [(UNK, False), (UNK, False), (OK, False)], "substitutes values"),
    # unit conversion km/h -> m/s, wrong conversion, dimension error
    ([r"72 \text{ km/h} = 20 \text{ m/s}"], True, [(OK, False)], ""),
    ([r"72 \text{ km/h} = 72000 \text{ m/s}"], True, [(ERR, False)], ""),
    ([r"d = 5 \text{ m} + 3 \text{ s}"], True, [(ERR, False)], "adds"),
    ([r"F = 2\,\mathrm{kg} \cdot 3\,\mathrm{m\,s^{-2}} = 6 \text{ N}"], True, [(OK, False)], ""),
    ([r"F = 2\,\mathrm{kg} \cdot 3\,\mathrm{m\,s^{-2}} = 6 \text{ J}"], True, [(ERR, False)], "units"),
    # vectors
    ([r"\left(3, 4\right)", r"3\hat{i} + 4\hat{j}"], True, [(UNK, False), (OK, False)], "same vector"),
    ([r"\left(3, 4\right)", r"3\hat{i} - 4\hat{j}"], True, [(UNK, False), (ERR, False)], "component"),
    # prose, chemistry, broken LaTeX -> unverifiable, never a crash
    (["count H and O atoms", "2H2 + O2 -> 2H2O", "check both sides"], False, [(UNK, False)] * 3, "could not read"),
    ([r"\frac{x^{2}", r"x \le 3", r"\sum_{i=1}^{n} i = 7", r"9^{9^{9^{9}}}", "", "$$", r"\unknowncmd{x}"],
     False, [(UNK, False)] * 7, ""),
    # v1 transcripts (plain text, no LaTeX) still verify
    (["2*x + 3 = 7", "2*x = 4", "x = 2"], False, [(UNK, False), (OK, False), (OK, False)], ""),
    (["sqrt(16) = 4", "x^(2) = 9"], False, [(OK, False), (UNK, False)], ""),
]


@pytest.mark.parametrize("lines,physics,expected,why", CHAINS, ids=[c[0][0][:30] or "empty" for c in CHAINS])
def test_step_chains(lines, physics, expected, why):
    got, res = chain(lines, physics)
    assert got == expected, [(r["status"], r["reason"]) for r in res]
    if why:
        assert any(why in r["reason"] for r in res), [r["reason"] for r in res]


def test_fallback_parser_without_antlr(monkeypatch):
    monkeypatch.setattr(mv, "_HAVE_ANTLR", False)
    got, res = chain([r"2x + 3 = 7", r"2x = 10", r"x = 5"])
    assert got == [(UNK, False), (ERR, False), (OK, True)], [r["reason"] for r in res]
    got, _ = chain([r"\frac{1}{2} + \frac{1}{3}", r"= \frac{5}{6}"])
    assert got == [(UNK, False), (OK, False)]
    got, _ = chain([r"\frac{d}{dx}\left(x^{3}\right) = 3x^{2}", r"\sqrt{16} = 4", r"\int_{0}^{1} x^{2} dx = 0.5"])
    assert got == [(OK, False), (OK, False), (ERR, False)]
    got, _ = chain([r"72 \text{ km/h} = 20 \text{ m/s}", "count the atoms"], physics=True)
    assert got == [(OK, False), (UNK, False)]


def test_parse_step_shapes():
    assert mv.parse_step(1, r"x = 2 \text{ or } x = 3").kind == "solset"
    assert mv.parse_step(1, r"x \in \{2, 3\}").kind == "solset"
    assert len(mv.parse_step(1, r"x = \pm 2").sols) == 2
    v = mv.parse_step(1, r"\vec{F} = (3, 4)")
    assert v.kind == "vector" and [int(c) for c in v.vec] == [3, 4]
    assert mv.parse_step(1, r"3\mathbf{i} + 4\mathbf{j}").kind == "vector"
    st = mv.parse_step(1, r"$$v = 20\,\text{m/s}$$")
    assert st.kind == "eq" and st.has_units
    assert mv.parse_step(1, "the answer is") is None
    assert mv.parse_step(1, "x" * 500) is None


# ---------------------------------------------------------------- final answer vs key
CX = None


def final(student, units="", key=None, tol=None):
    cx = mv.Ctx(mv._Clock(5), True)
    return mv.check_final(student, units, key or {}, cx, tol)


@pytest.mark.parametrize("student,units,key,tol,status,why", [
    ("19.6", "m/s", {"key_final_latex": "19.6", "key_units": "m/s"}, None, OK, "matches the key"),
    ("70.56", "km/h", {"key_final_latex": "19.6", "key_units": "m/s"}, None, OK, "matches"),        # conversion
    (r"v = 19.6\,\mathrm{m\,s^{-1}}", "", {"key_final_latex": "19.6", "key_units": "m/s"}, None, OK, ""),
    ("19.62", "m/s", {"key_final_latex": "19.6", "key_units": "m/s"}, None, OK, "rounded"),          # key is rounded
    ("20", "m/s", {"key_final_latex": "19.6", "key_units": "m/s"}, None, ERR, "does not match"),
    ("3.14", "", {"key_final_latex": r"\pi"}, None, OK, "rounded"),
    ("3.1", "", {"key_final_latex": r"\pi"}, None, ERR, ""),                                        # too coarse
    ("3.1", "", {"key_final_latex": r"\pi"}, 0.05, OK, ""),                                         # teacher tolerance
    ("0.333", "", {"key_final_latex": r"\frac{1}{3}"}, None, OK, "rounded"),
    (r"\frac{2}{4}", "", {"key_final_latex": "0.5"}, None, OK, ""),
    (r"\sqrt{8}", "", {"key_final_latex": r"2\sqrt{2}"}, None, OK, ""),
    (r"x^2 + 2x", "", {"key_final_latex": r"x(x+2)"}, None, OK, ""),
    (r"x = 3 \text{ or } x = 2", "", {"key_final_latex": r"x = 2, x = 3"}, None, OK, ""),
    (r"x = 2", "", {"key_final_latex": r"x = 2, x = 3"}, None, ERR, "the key has 2"),
    (r"(3, 4)", "N", {"key_final_latex": r"3\hat{i} + 4\hat{j}", "key_units": "N"}, None, OK, ""),
    (r"(3, 5)", "N", {"key_final_latex": r"3\hat{i} + 4\hat{j}", "key_units": "N"}, None, ERR, ""),
    ("19.6", "m", {"key_final_latex": "19.6", "key_units": "m/s"}, None, ERR, "units"),
    (r"\frac{x^{", "", {"key_final_latex": "2"}, None, UNK, "could not read"),
    ("7", "", {}, None, UNK, "no final answer in the key"),
])
def test_final_answer(student, units, key, tol, status, why):
    f, _, _ = final(student, units, key, tol)
    assert f["status"] == status, f
    assert why in f["reason"]


def test_final_units_and_sig_figs():
    f, u, sf = final("19.6", "", {"key_final_latex": "19.6", "key_units": "m/s"})
    assert f["status"] == OK and u["status"] == ERR and "no units" in u["reason"]
    f, u, _ = final("19.6", "m", {"key_final_latex": "19.6", "key_units": "m/s"})
    assert u["status"] == ERR and "wrong units" in u["reason"]
    _, u, _ = final("70.56", "km/h", {"key_final_latex": "19.6", "key_units": "m/s"})
    assert u["status"] == OK and "after conversion" in u["reason"]
    key = {"key_final_latex": "19.6", "key_units": "m/s", "sig_figs": 3}
    assert final("19.6", "m/s", key)[2] == {"required": 3, "given": 3, "ok": True}
    assert final("19.600", "m/s", key)[2]["ok"] is False
    # the rubric asks for sig figs in words
    key = {"key_final_latex": "19.6", "criteria": [{"label": "Answer to three significant figures"}]}
    assert final("19.62", "", key)[2]["ok"] is False


@pytest.mark.parametrize("text,expected", [("19.6", (3, 3)), ("0.0250", (3, 3)), ("1200", (2, 4)), ("7", (1, 1)),
                                           (r"2.50 \times 10^{3}", (3, 3)), ("1.5e3", (2, 2)), ("abc", None)])
def test_sig_figs(text, expected):
    assert U.sig_figs(text) == expected


def test_units_parse():
    assert U.parse_unit("km/h").factor == pytest.approx(5 / 18) or float(U.parse_unit("km/h").factor) == pytest.approx(5 / 18)
    assert U.parse_unit(r"\mathrm{m\,s^{-2}}").dims == (("meter", 1), ("second", -2))
    assert U.parse_unit("N").dims == U.parse_unit("kg m/s^2").dims
    assert U.parse_unit(r"\mu F") is not None and U.parse_unit(r"\Omega") is not None
    for bad in ("", "or", "°C", "banana", "x+1"):
        assert U.parse_unit(bad) is None


# ---------------------------------------------------------------- reconcile with the model
Q = {"id": "q1", "type": "essay", "prompt": "Solve 2x + 3 = 7.", "points": 4}
CRIT = [{"id": "c1", "label": "Isolate the x term", "points": 2, "description": ""},
        {"id": "c2", "label": "Divide correctly", "points": 1, "description": "", "ecf": True},
        {"id": "c3", "label": "Final answer", "points": 1, "description": ""}]
CTX = {"subject": "math", "criteria": CRIT, "key_final_latex": "x = 2", "key_units": ""}


def model_out(statuses, latexes, crit_points, points, conf=0.8, final_latex=None, ecf=None):
    return {"legibility": "clear", "transcript": "; ".join(latexes),
            "steps": [{"text": t, "latex": t, "status": s, "ecf": bool(ecf and i + 1 in ecf)}
                      for i, (s, t) in enumerate(zip(statuses, latexes))],
            "final_answer": {"latex": final_latex or latexes[-1], "units": ""},
            "criteria": [{"id": "c1", "points": crit_points[0], "steps": [2]},
                         {"id": "c2", "points": crit_points[1], "steps": [3]},
                         {"id": "c3", "points": crit_points[2], "steps": [3]}],
            "points": points, "explanation": "...", "confidence": conf}


def verified(out, ctx=CTX, q=Q):
    return mv.apply(ai_grade.parse_suggestion(out, q, ctx), out, q, ctx)


def test_all_verified_raises_confidence():
    out = model_out(["correct"] * 3, ["2x + 3 = 7", "2x = 4", "x = 2"], [2, 1, 1], 4, conf=0.75)
    d = verified(out)
    assert d["points"] == 4 and d["needs_review"] is False
    assert d["confidence"] >= 0.9 and d["verification"]["agrees"] is True
    assert d["verification"]["final"]["status"] == OK
    assert [s["verify"] for s in d["steps"]] == [UNK, OK, OK]
    assert d["steps"][1]["latex"] == "2x = 4" and d["prompt_version"] == mv.PROMPT_VERSION
    v = d["verification"]
    assert v["engine"].startswith("sympy") and v["summary"] == {OK: 2, ERR: 0, UNK: 1}
    assert v["confidence_before"] == 0.75 and v["points_before"] == 4


def test_model_wrong_about_correct_step_is_overruled():
    # the AI calls a correct step wrong and deducts: SymPy proves it -> step flipped, points re-scored, review
    out = model_out(["correct", "error", "correct"], ["2x + 3 = 7", "2x = 4", "x = 2"], [0, 1, 1], 2, conf=0.9)
    d = verified(out)
    s2 = d["steps"][1]
    assert s2["status"] == "correct" and s2["model_status"] == "error" and s2["verify"] == OK
    assert "SymPy proves" in s2["verifier_disagrees"]
    assert d["criteria"][0]["points"] == 2 and d["criteria"][0]["model_points"] == 0
    assert d["points"] == 4 and d["model_points"] == 2
    assert d["needs_review"] is True and "verifier_disagrees" in d["review_reasons"]
    assert d["confidence"] <= 0.5 and "math check disagrees" in d["review_reason"]
    assert d["first_error_step"] is None


def test_model_misses_an_error():
    out = model_out(["correct"] * 3, ["2x + 3 = 7", "2x = 10", "x = 5"], [2, 1, 1], 4, conf=0.95)
    d = verified(out)
    assert d["steps"][1]["status"] == "error" and d["steps"][1]["model_status"] == "correct"
    assert d["steps"][2]["ecf"] is True and d["steps"][2]["status"] == "correct"
    assert d["verification"]["final"]["status"] == ERR and d["verification"]["final"]["ecf"] is True
    # c1 (step 2) wrong -> 0; c2 allows ECF -> kept; c3 (final, no ECF) -> 0
    assert [c["points"] for c in d["criteria"]] == [0, 1, 0]
    assert d["points"] == 1 and d["needs_review"] and d["confidence"] <= 0.5
    assert d["first_error_step"] == 2


def test_ecf_method_marks_only_where_rubric_allows():
    # AI marks every step after the slip wrong; SymPy: step 3 follows from the wrong value (ECF)
    out = model_out(["correct", "error", "error"], ["2x + 3 = 7", "2x = 10", "x = 5"], [0, 0, 0], 0, conf=0.9)
    d = verified(out)
    s3 = d["steps"][2]
    assert s3["ecf"] is True and s3["status"] == "correct" and "ECF" in s3["verifier_disagrees"]
    assert d["criteria"][1]["points"] == 1 and "ECF" in d["criteria"][1]["verifier_note"]
    assert d["criteria"][2]["points"] == 0           # final answer criterion has no ECF
    assert d["points"] == 1 and d["needs_review"]
    # without an ECF criterion the ECF step is not a point-changing disagreement
    crit = [dict(c, ecf=False) for c in CRIT]
    ctx = dict(CTX, criteria=crit)
    d2 = mv.apply(ai_grade.parse_suggestion(out, Q, ctx), out, Q, ctx)
    assert d2["points"] == 0 and "verifier_disagrees" not in d2["review_reasons"]


def test_final_answer_disagreement_without_rubric():
    q = {"id": "q2", "type": "essay", "prompt": "Solve", "points": 2}
    ctx = {"subject": "math", "key_final_latex": "x = 2"}
    out = {"steps": [{"text": "x = 3", "latex": "x = 3", "status": "correct"}],
           "final_answer": {"latex": "x = 3"}, "points": 2, "confidence": 0.9, "legibility": "clear"}
    d = mv.apply(ai_grade.parse_suggestion(out, q, ctx), out, q, ctx)
    assert d["verification"]["final"]["status"] == ERR and d["needs_review"]
    assert any(x["n"] == "final" for x in d["verification"]["disagreements"])


def test_units_disagreement_flags_review():
    q = {"id": "q3", "type": "essay", "prompt": "Speed?", "points": 2}
    ctx = {"subject": "physics", "key_final_latex": "20", "key_units": "m/s"}
    out = {"steps": [{"text": "v = 72 km/h", "latex": r"v = 72 \text{ km/h}", "status": "correct"},
                     {"text": "v = 20 m", "latex": r"v = 20 \text{ m}", "status": "correct"}],
           "final_answer": {"latex": "20", "units": "m"}, "points": 2, "confidence": 0.9, "legibility": "clear"}
    d = mv.apply(ai_grade.parse_suggestion(out, q, ctx), out, q, ctx)
    assert d["verification"]["units"]["status"] == ERR and d["needs_review"]


def test_unparseable_and_legacy_outputs_never_crash():
    for out in [None, {}, {"steps": "x"}, {"steps": [{"text": "\\frac{", "latex": "\\frac{{{"}], "points": 1},
                {"steps": [{"text": "x", "status": "correct"}], "final_answer": "\\left("},
                {"steps": [{"text": "a" * 600, "latex": "b" * 600}], "final_answer": {"latex": 5}}]:
        d0 = ai_grade.parse_suggestion(out, Q, CTX)
        d = mv.apply(d0, out, Q, CTX)
        assert d["points"] == d0["points"]
    # v1 output (no latex fields): plain text steps are still checked, prompt version unchanged
    out = {"steps": [{"text": "2x + 3 = 7", "status": "correct"}, {"text": "2x = 4", "status": "correct"}],
           "points": 4, "confidence": 0.8, "criteria": [{"id": "c1", "points": 2}, {"id": "c2", "points": 1},
                                                         {"id": "c3", "points": 1}]}
    d = verified(out)
    assert d["steps"][1]["verify"] == OK and d["prompt_version"] == ai_grade.PROMPT_VERSION
    assert "latex" not in d["steps"][1]


def test_nothing_checkable_leaves_no_badges():
    q = {"id": "q9", "type": "essay", "prompt": "Balance H2 + O2", "points": 4}
    out = {"steps": [{"text": "count H and O atoms", "status": "correct"},
                     {"text": "2H2 + O2 -> 2H2O", "status": "correct"}], "points": 3, "confidence": 0.86}
    d = mv.apply(ai_grade.parse_suggestion(out, q, {"subject": "chemistry"}), out, q, {"subject": "chemistry"})
    assert d["verification"]["checked"] is False and d["verification"]["agrees"] is False
    assert all("verify" not in s for s in d["steps"])
    assert d["confidence"] == 0.86 and d["points"] == 3 and not d["needs_review"]


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_STEP_VERIFY", "0")
    out = model_out(["correct", "error", "correct"], ["2x + 3 = 7", "2x = 4", "x = 2"], [0, 1, 1], 2)
    d = verified(out)
    assert "verification" not in d and d["points"] == 2
    assert "LaTeX" not in mv.extend_prompt("Prompt version: fp-step-grade-v1\nQ", Q, CTX)


def test_prompt_extension():
    p = mv.extend_prompt(ai_grade.build_prompt(Q, CTX), Q, dict(CTX, sig_figs=3, key_solution_latex=["2x = 4"]))
    assert "Prompt version: fp-step-grade-v2 (= fp-step-grade-v1 + LaTeX steps)" in p
    assert '"latex"' in p and '"final_answer"' in p and "Teacher's final answer (LaTeX): x = 2" in p
    assert "error carried forward" in p and "c2" in p and "3 significant figures" in p and "2x = 4" in p


# ---------------------------------------------------------------- through the AI transport
def _fake(out_for):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = "".join(c.get("text", "") for c in body["messages"][1]["content"] if c.get("type") == "text")
        seen.append(text)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out_for(text))}}]})
    return httpx.MockTransport(handler), seen


def test_suggest_runs_the_verifier(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    wrong_call = model_out(["correct", "error", "correct"], ["2x + 3 = 7", "2x = 4", "x = 2"], [0, 1, 1], 2, conf=0.9)
    transport, seen = _fake(lambda _t: wrong_call)
    ai.set_transport(transport)
    d = asyncio.run(ai_grade.suggest(Q, CTX, [b"\x89PNG\r\n\x1a\n"]))
    assert "fp-step-grade-v2" in seen[0] and '"final_answer"' in seen[0]
    assert d["needs_review"] and "verifier_disagrees" in d["review_reasons"] and d["confidence"] <= 0.5
    assert d["points"] == 4 and d["verification"]["disagreements"]
    good = model_out(["correct"] * 3, ["2x + 3 = 7", "2x = 4", "x = 2"], [2, 1, 1], 4, conf=0.6)
    transport, _ = _fake(lambda _t: good)
    ai.set_transport(transport)
    d = asyncio.run(ai_grade.suggest(Q, CTX, [b"img"]))
    assert d["confidence"] >= 0.9 and not d["needs_review"] and d["low_confidence"] is False


# ---------------------------------------------------------------- answer key LaTeX (HTTP)
@pytest.fixture
def paper(client, teacher):
    r = client.post("/api/exams", json=PAPER_EXAM, headers=teacher)
    assert r.status_code == 201, r.text
    e = r.json()
    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice", "bob"]}, headers=teacher)
    return e


def test_key_latex_fields_and_ecf_criteria(client, teacher, paper):
    e = paper
    qid = e["questions"][4]["id"]
    body = {"solution": "x e^x - e^x + C",
            "criteria": [{"id": "c1", "label": "Setup", "points": 2},
                         {"id": "c2", "label": "Integration", "points": 2, "ecf": True}],
            "final_latex": r"x e^{x} - e^{x} + C", "solution_latex": [r"u = x", r"\int x e^x dx = x e^x - e^x + C"],
            "sig_figs": 3}
    r = client.put(f"/api/exams/{e['id']}/answer-key/questions/{qid}", json=body, headers=teacher)
    assert r.status_code == 200, r.text
    k = r.json()["key"]
    assert k["final_latex"] == r"x e^{x} - e^{x} + C" and k["solution_latex"][0] == "u = x"
    assert k["sig_figs"] == 3 and k["latex_source"] == "teacher" and r.json()["latex_warnings"] == []
    assert [c.get("ecf", False) for c in k["criteria"]] == [False, True]
    # an old client that does not send the LaTeX fields keeps them
    r = client.put(f"/api/exams/{e['id']}/answer-key/questions/{qid}", headers=teacher, json={
        "solution": "x e^x - e^x + C", "criteria": body["criteria"]})
    assert r.json()["key"]["final_latex"] == r"x e^{x} - e^{x} + C"
    # unreadable LaTeX -> warning (still stored)
    r = client.put(f"/api/exams/{e['id']}/answer-key/questions/{qid}", headers=teacher, json={
        "criteria": body["criteria"], "final_latex": r"\frac{1", "units": "bananas"})
    assert len(r.json()["latex_warnings"]) == 2
    # key_context feeds the verifier (numeric questions fall back to answer_number)
    from app.scan import answer_key
    key = client.get(f"/api/exams/{e['id']}/answer-key", headers=teacher).json()
    assert key["questions"][4]["key"]["units"] == "bananas"
    ctx = answer_key.key_context({"questions": {}}, {**e["questions"][1]})
    assert ctx["key_final_latex"] == "3.14" and ctx["sig_figs"] is None


def test_ai_converts_key_to_latex_once(client, teacher, paper, monkeypatch):
    e = paper
    qids = [q["id"] for q in e["questions"]]
    client.put(f"/api/exams/{e['id']}/answer-key/questions/{qids[4]}", headers=teacher,
               json={"solution": "x e^x - e^x + C"})
    assert client.post(f"/api/exams/{e['id']}/answer-key/latex", json={}, headers=teacher).status_code == 409
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    transport, seen = _fake(lambda t: {"final_latex": "x e^{x} - e^{x} + C" if "integrate" in t else "3.14",
                                       "units": "", "solution_latex": ["u = x", "= x e^{x} - e^{x} + C"]})
    ai.set_transport(transport)
    r = client.post(f"/api/exams/{e['id']}/answer-key/latex", json={}, headers=teacher)
    assert r.status_code == 200, r.text
    out = r.json()
    # numeric (has a correct answer), short (accepted answers) and the essay with a solution; not mcq / no-key essay
    assert out["converted"] == 3 and len(seen) == 3
    keys = {q["question_id"]: q["key"] for q in out["questions"]}
    assert keys[qids[4]]["final_latex"] == "x e^{x} - e^{x} + C" and keys[qids[4]]["latex_source"] == "ai"
    # second call: nothing left to convert (stored once)
    r = client.post(f"/api/exams/{e['id']}/answer-key/latex", json={}, headers=teacher)
    assert r.json()["converted"] == 0 and len(seen) == 3


def test_ai_run_records_verification(client, teacher, paper, monkeypatch):
    """Whole scan -> AI run with a v2 model: the stored draft carries the verification record."""
    e = paper
    q = [x["id"] for x in e["questions"]]
    _scan_class(client, teacher, e)
    _wait(client, teacher, e["id"])
    client.put(f"/api/exams/{e['id']}/answer-key", json={"subject": "math"}, headers=teacher)
    client.put(f"/api/exams/{e['id']}/answer-key/questions/{q[4]}", headers=teacher, json={
        "solution": "x e^x - e^x + C", "final_latex": r"x e^{x} - e^{x} + C",
        "criteria": [{"id": "c1", "label": "Antiderivative", "points": 4}]})
    out = {"legibility": "clear", "transcript": "...",
           "steps": [{"text": "∫x e^x dx = x e^x - ∫ e^x dx", "latex": r"\int x e^x dx = x e^x - \int e^x dx",
                      "status": "correct"},
                     {"text": "= x e^x + e^x + C", "latex": r"\int x e^x dx = x e^x + e^x + C", "status": "correct"}],
           "final_answer": {"latex": r"x e^x + e^x + C", "units": ""},
           "criteria": [{"id": "c1", "points": 4, "steps": [2]}], "points": 4, "confidence": 0.95,
           "explanation": "Great."}
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    transport, _ = _fake(lambda t: out if "integrate x e^x" in t else
                         {"transcript": "something", "points": 1, "confidence": 0.5, "steps": []})
    ai.set_transport(transport)
    r = client.post(f"/api/exams/{e['id']}/scan/ai-grade", json={"question_ids": [q[4]]}, headers=teacher)
    assert r.status_code == 202, r.text
    end = time.time() + 60
    while time.time() < end and client.get(f"/api/exams/{e['id']}/scan/ai-grade", headers=teacher).json()["running"]:
        time.sleep(0.1)
    queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    d = next(i["draft"] for i in queue["items"] if i["username"] == "alice" and i["question_id"] == q[4])
    v = d["verification"]
    assert [s["status"] for s in v["steps"]] == [OK, ERR] and v["final"]["status"] == ERR
    assert d["steps"][1]["verify"] == ERR and d["steps"][1]["model_status"] == "correct"
    assert d["points"] == 0 and d["needs_review"] and "verifier_disagrees" in d["review_reasons"]
