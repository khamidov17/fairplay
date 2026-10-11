"""Graph / chart / diagram questions: checklist specs, prompt, strict parsing, OpenCV checks on
synthetic hand-drawn graphs, and the full scan -> AI -> review -> student feedback flow with a
fake AI transport."""
import asyncio
import base64
import json
import time

import cv2
import httpx
import numpy as np
import pytest

from app import ai
from app.scan import ai_grade, diagram_cv, diagram_spec, diagrams
from app.scan.print import build_layout
from app.schemas import QuestionIn
from app.services import student_question

from .test_scan import S, _boxes, _render, _to_pdf, _wait


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


# ---------------------------------------------------------------- synthetic drawings
EXPECTED = [(1, 2), (2, 4), (3, 5), (4, 7), (6, 9)]


def draw_graph(w=800, h=600, points=EXPECTED, parabola=True, ticks=True, axes=True, uneven=False, seed=1,
               ox=100, oy=None, sx=60, sy=40, canvas=None, at=(0, 0)):
    """A 'hand-drawn' graph: slightly slanted axes, tick marks, a wobbly parabola and filled dots.
    Returns (image, value -> pixel function)."""
    rng = np.random.default_rng(seed)
    img = canvas if canvas is not None else np.full((h, w, 3), 245, np.uint8)
    X, Y = at
    oy = oy if oy is not None else h - 100

    def P(x, y, jitter=1.0):
        return (int(X + ox + x * sx + rng.normal(0, jitter)), int(Y + oy - y * sy + rng.normal(0, jitter)))
    ink = (40, 40, 40)
    if axes:
        cv2.line(img, (X + ox - 40, Y + oy), (X + w - 40, Y + oy + 3), ink, 3)
        cv2.line(img, (X + ox, Y + oy + 60), (X + ox - 2, Y + 40), ink, 3)
    if ticks and axes:
        xs = [1, 2, 3, 3.4, 5, 6, 8.5, 9, 10] if uneven else range(1, 11)
        for i in xs:
            x = int(X + ox + i * sx)
            cv2.line(img, (x, Y + oy - 8), (x, Y + oy + 8), ink, 2)
        for j in range(1, 11):
            y = Y + oy - j * sy
            cv2.line(img, (X + ox - 8, y), (X + ox + 8, y), ink, 2)
    if parabola:
        pts = [P(x / 10, 0.32 * ((x / 10) - 5) ** 2 + 0.5) for x in range(0, 101)]
        cv2.polylines(img, [np.array(pts)], False, (60, 30, 30), 2)
    for x, y in points:
        cv2.circle(img, P(x, y), 7, (30, 30, 30), -1)
    return img, (lambda x, y: ((X + ox + x * sx) / img.shape[1], (Y + oy - y * sy) / img.shape[0]))


def png(img) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


# ---------------------------------------------------------------- specs + defaults
@pytest.mark.parametrize("kind", list(diagram_spec.KINDS))
@pytest.mark.parametrize("total", [1, 3, 4, 7, 10, 25])
def test_default_checklists_add_up(kind, total):
    feats = diagram_spec.default_features(kind, total)
    assert len(feats) >= 4
    assert abs(sum(float(f["points"]) for f in feats) - total) < 1e-9
    assert len({f["id"] for f in feats}) == len(feats)
    assert all(f["label"] and float(f["points"]) >= 0 and f["key"] for f in feats)
    for f in feats:
        if f["key"] in diagram_spec.POINT_KEYS:
            assert f["points_xy"] == [] and f["tolerance"] == 0.5


def test_default_checklist_content():
    labels = lambda k: " | ".join(f["label"].lower() for f in diagram_spec.default_features(k, 10))  # noqa: E731
    fg = labels("function_graph")
    for word in ("axes", "intercepts", "turning points", "asymptotes", "end behaviour", "domain", "key points",
                 "scale"):
        assert word in fg
    dc = labels("data_chart")
    for word in ("chart type", "units", "scale", "points plotted", "best fit", "title"):
        assert word in dc
    fbd = labels("physics_fbd")
    for word in ("gravity", "normal", "friction", "tension", "no extra", "start on the object", "lengths"):
        assert word in fbd
    ci = labels("physics_circuit")
    for word in ("components", "series / parallel", "symbols"):
        assert word in ci
    assert {c["kind"] for c in diagram_spec.catalog()} == set(diagram_spec.KINDS)


def test_clean_diagram_validation():
    d = diagram_spec.clean_diagram({"kind": "data_chart"}, 6)
    assert d["kind"] == "data_chart" and sum(float(f["points"]) for f in d["features"]) == 6
    assert d["reference_image"] is None and d["notes"] == ""
    with pytest.raises(ValueError, match="kind"):
        diagram_spec.clean_diagram({"kind": "pie"}, 6)
    feats = [{"label": "Axes", "points": 2}, {"label": "Points", "points": 3, "key": "points",
                                               "points_xy": [[1, 2], {"x": 2, "y": 4}], "tolerance": 0.25}]
    with pytest.raises(ValueError, match="add up to 6"):
        diagram_spec.clean_diagram({"kind": "data_chart", "features": feats}, 6)
    d = diagram_spec.clean_diagram({"kind": "data_chart", "features": feats}, 5)
    assert [f["id"] for f in d["features"]] == ["f1", "f2"]
    assert d["features"][1]["points_xy"] == [[1, 2], [2, 4]] and d["features"][1]["tolerance"] == 0.25
    d = diagram_spec.clean_diagram({"kind": "data_chart", "features": feats}, 10, strict=False)  # rescaled
    assert sum(float(f["points"]) for f in d["features"]) == 10 and d["features"][1]["points"] == 6
    with pytest.raises(ValueError, match="uploaded image"):
        diagram_spec.clean_diagram({"kind": "geometry", "reference_image": "https://evil/x.png"}, 4)
    with pytest.raises(ValueError):
        diagram_spec.clean_diagram({"kind": "geometry", "features": [{"label": "x", "points": 4,
                                                                      "points_xy": [["a", 1]]}]}, 4)
    with pytest.raises(ValueError, match="label"):
        diagram_spec.clean_diagram({"kind": "geometry", "features": [{"label": " ", "points": 4}]}, 4)


def test_question_schema_backward_compatible_and_hidden_from_students():
    q = QuestionIn(type="essay", prompt="Sketch y = x^2 - 4", points=5,
                   rubric=[{"label": "a", "points": 5}], diagram={"kind": "function_graph"})
    d = q.to_dict()
    assert d["diagram"]["kind"] == "function_graph" and d["rubric"] == []  # the checklist replaces the rubric
    assert sum(float(f["points"]) for f in d["diagram"]["features"]) == 5
    plain = QuestionIn(type="essay", prompt="Explain", points=5).to_dict()
    assert "diagram" not in plain  # no type = today's behaviour
    num_q = QuestionIn(type="numeric", prompt="2+2", points=1, answer_number=4, diagram={"kind": "geometry"})
    assert "diagram" not in num_q.to_dict()
    with pytest.raises(ValueError):
        QuestionIn(type="essay", prompt="x", points=5, diagram={"kind": "nope"})
    stored = dict(d, id="q_1")
    assert "diagram" not in student_question(stored)
    assert diagram_spec.effective(stored, None)["kind"] == "function_graph"
    over = {"diagram": {"kind": "physics_fbd"}}
    assert diagram_spec.effective(stored, over)["kind"] == "physics_fbd"  # answer-key override wins
    assert diagram_spec.effective(dict(stored, type="short"), over) is None


# ---------------------------------------------------------------- prompt + parsing
SPEC = diagram_spec.clean_diagram({"kind": "data_chart", "notes": "Plot the table.", "features": [
    {"id": "f1", "key": "chart_type", "label": "Correct chart type", "points": 1, "expected": "scatter"},
    {"id": "f2", "key": "axis_labels", "label": "Axis labels with units", "points": 1},
    {"id": "f3", "key": "scale", "label": "Even scale", "points": 1},
    {"id": "f4", "key": "points", "label": "Points plotted correctly", "points": 3,
     "points_xy": [list(p) for p in EXPECTED], "tolerance": 0.3},
]}, 6)
Q = {"id": "q1", "type": "essay", "prompt": "Plot the data from the table.", "points": 6, "diagram": SPEC}
CTX = {"subject": "physics", "criteria": diagram_spec.feature_criteria(SPEC), "diagram": SPEC}


def test_prompt_has_checklist_only_for_diagram_questions():
    p = ai_grade.build_prompt(Q, CTX)
    assert "GRAPH / DIAGRAM QUESTION" in p and diagrams.DIAGRAM_PROMPT_VERSION in p
    assert "id f4: Points plotted correctly (3 pts)" in p and "(1, 2), (2, 4)" in p and "±0.3" in p
    assert "expected: scatter" in p and "Teacher's notes: Plot the table." in p
    assert '"features"' in p and "LAST image" not in p
    assert "LAST image is the teacher's correct drawing" in ai_grade.build_prompt(Q, dict(CTX, diagram_reference=True))
    plain = ai_grade.build_prompt({"id": "q", "type": "essay", "prompt": "Explain", "points": 4}, {"subject": "math"})
    assert "GRAPH" not in plain and plain.rstrip().endswith("}")


def good_out(**over):
    out = {"legibility": "clear", "transcript": "scatter graph of the table",
           "features": [
               {"id": "f1", "status": "present", "points": 1, "note": "scatter", "box": [0.1, 0.1, 0.9, 0.9]},
               {"id": "f2", "status": "missing", "points": 0, "note": "no units", "point": [50, 95]},
               {"id": "f3", "status": "present", "note": "", "box": [0.9, 0.2, 0.1, 0.8]},
               {"id": "f4", "status": "wrong", "points": 2, "note": "(3, 5) is off", "box": [0.2, 0.2, 0.6, 0.7]},
               {"id": "zz", "status": "present", "points": 5, "note": "made up"}],
           "readings": [{"label": "A", "x": 1, "y": 2, "at": [0.2, 0.7]}, {"x": 2, "y": 4, "at": [0.275, 0.567]},
                        {"x": 3, "y": 6}, {"x": 4, "y": 7}, [6, 9], {"x": "nan?"}],
           "points": 4, "explanation": "Label your axes with units.", "teacher_note": "Units missing",
           "criteria": [], "confidence": 0.9}
    out.update(over)
    return out


def test_parse_features_strict():
    d = ai_grade.parse_suggestion(good_out(), Q, CTX)
    dg = d["diagram"]
    rows = {r["id"]: r for r in dg["features"]}
    assert set(rows) == {"f1", "f2", "f3", "f4"}  # unknown feature dropped
    assert [(r["status"], r["points"]) for r in dg["features"]] == [
        ("present", 1), ("missing", 0), ("present", 1), ("wrong", 2)]
    assert rows["f2"]["point"] == [0.5, 0.95]  # percentages -> fractions
    assert rows["f3"]["box"] == [0.1, 0.2, 0.9, 0.8]  # corners sorted
    assert d["points"] == 4 and d["max_points"] == 6
    assert [(c["id"], c["points"], c["max_points"], c["met"]) for c in d["criteria"]] == [
        ("f1", 1, 1, True), ("f2", 0, 1, False), ("f3", 1, 1, True), ("f4", 2, 3, False)]
    assert len(dg["readings"]) == 5 and dg["readings"][4] == {"label": "", "x": 6, "y": 9, "at": None}
    assert dg["kind"] == "data_chart" and dg["prompt_version"] == diagrams.DIAGRAM_PROMPT_VERSION
    # the checklist replaces the step grader's rubric / steps complaints
    assert "missing_criteria" not in d["review_reasons"] and "no_steps" not in d["review_reasons"]
    assert d["needs_review"] is False and d["confidence"] == 0.9


def test_parse_missing_unknown_and_inconsistent_features():
    out = good_out(features=[{"id": "f1", "status": "present", "points": 9},          # clamped to 1
                             {"id": "f2", "status": "missing", "points": 1},          # missing with points
                             {"id": "f3", "status": "bogus"}])                        # f4 skipped
    d = ai_grade.parse_suggestion(out, Q, CTX)
    rows = {r["id"]: r for r in d["diagram"]["features"]}
    assert rows["f1"]["points"] == 1 and rows["f2"]["points"] == 0
    assert rows["f3"]["status"] == "not_assessed" and rows["f4"]["status"] == "not_assessed"
    assert rows["f4"]["points"] == 0 and rows["f4"]["note"] == "Not checked by AI"
    assert d["points"] == 1 and d["needs_review"]
    assert "missing_features" in d["review_reasons"] and "features_inconsistent" in d["review_reasons"]
    assert "AI skipped some checklist features" in d["review_reason"]


@pytest.mark.parametrize("junk", [None, [], "text", {"features": "x"}, {"features": [1, None]},
                                  {"features": [{"id": "f1", "box": "big", "point": [1e9, 2]}]}])
def test_parse_junk_never_raises(junk):
    d = ai_grade.parse_suggestion(junk, Q, CTX)
    assert d["points"] <= 1 and d["needs_review"] is True
    assert len(d["diagram"]["features"]) == 4 and all(r["box"] is None for r in d["diagram"]["features"])


def test_parse_accepts_rubric_format_answers():
    out = {"criteria": [{"id": "f1", "points": 1}, {"id": "f2", "points": 1}, {"id": "f3", "points": 0},
                        {"id": "f4", "points": 3}], "points": 5, "confidence": 0.8, "transcript": "graph"}
    d = ai_grade.parse_suggestion(out, Q, CTX)
    assert [r["status"] for r in d["diagram"]["features"]] == ["present", "present", "wrong", "present"]
    assert d["points"] == 5


def test_readings_disagreeing_with_points_lower_confidence():
    # the model gives full points for the plotted points but its own readings miss 3 of 5
    out = good_out(features=[{"id": "f1", "status": "present"}, {"id": "f2", "status": "present"},
                             {"id": "f3", "status": "present"}, {"id": "f4", "status": "present", "points": 3}],
                   readings=[{"x": 1, "y": 2}, {"x": 2, "y": 4}, {"x": 3, "y": 8}, {"x": 5, "y": 1}, {"x": 6, "y": 1}])
    d = ai_grade.parse_suggestion(out, Q, CTX)
    assert d["points"] == 6
    assert "diagram_check_disagrees" in d["review_reasons"] and d["confidence"] <= diagrams.DISAGREE_CONFIDENCE
    assert d["diagram"]["check"]["agrees"] is False and "2 of 5" in d["diagram"]["check"]["issues"][0]


# ---------------------------------------------------------------- OpenCV
def test_cv_detects_axes_ticks_and_dots_on_synthetic_graph():
    img, to_px = draw_graph()
    r = diagram_cv.analyze(png(img))
    assert r["ok"]
    ax = r["axes"]
    assert abs(ax["x_axis"]["y"] - 500 / 600) < 0.01 and abs(ax["y_axis"]["x"] - 100 / 800) < 0.01
    assert ax["x_axis"]["x1"] - ax["x_axis"]["x0"] > 0.8 and ax["y_axis"]["y1"] - ax["y_axis"]["y0"] > 0.8
    assert len(r["ticks"]["x"]) == 10 and len(r["ticks"]["y"]) == 10
    assert r["ticks_even"] == {"x": True, "y": True}
    want = [to_px(x, y) for x, y in EXPECTED]
    assert len(r["dots"]) == len(EXPECTED)  # the parabola and the tick marks are not dots
    for wx, wy in want:
        assert any(abs(wx - d[0]) < 0.01 and abs(wy - d[1]) < 0.01 for d in r["dots"])


def test_cv_on_a_photo_like_jpeg_and_edge_cases():
    img, _ = draw_graph(uneven=True, seed=3)
    rng = np.random.default_rng(0)
    shade = np.linspace(0.8, 1.0, img.shape[1])[None, :, None]  # uneven lighting
    noisy = np.clip(img * shade + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
    noisy = cv2.GaussianBlur(noisy, (3, 3), 0)
    jpg = cv2.imencode(".jpg", noisy, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
    r = diagram_cv.analyze(jpg)
    assert r["ok"] and r["axes"] and r["axes"].get("origin")
    assert r["ticks_even"]["x"] is False  # unevenly spaced x ticks
    assert len(r["dots"]) == 5
    # printed answer-box border is not an axis; empty / junk input
    boxed = np.full((600, 800, 3), 250, np.uint8)
    cv2.rectangle(boxed, (8, 8), (791, 591), (50, 60, 80), 3)
    r = diagram_cv.analyze(png(boxed))
    assert r["ok"] and r["axes"] is None and r["dots"] == []
    assert diagram_cv.analyze(b"not an image") == {"ok": False}
    assert diagram_cv.analyze(None) == {"ok": False}


def _features(points_status="present", points=3, axes="present"):
    return [{"id": "f1", "key": "axes", "label": "Axes drawn", "points": 1, "max_points": 1,
             "status": axes},
            {"id": "f2", "key": "axis_labels", "label": "Axis labels", "points": 1, "max_points": 1,
             "status": "missing"},
            {"id": "f3", "key": "scale", "label": "Even scale", "points": 1, "max_points": 1, "status": "present"},
            {"id": "f4", "key": "points", "label": "Points plotted correctly", "points": points, "max_points": 3,
             "status": points_status}]


def test_cross_check_agrees_and_disagrees():
    img, to_px = draw_graph()
    cv = diagram_cv.analyze(png(img))
    readings = [{"x": x, "y": y, "at": list(to_px(x, y))} for x, y in EXPECTED]
    ok = diagram_cv.cross_check(SPEC, _features(), readings, cv)
    assert ok["agrees"] is True and ok["calibrated"] and ok["points_matched"] == 5 and ok["expected_points"] == 5
    assert ok["readings_on_dots"] == 1.0 and ok["axes_found"] is True and ok["ticks_even"] is True
    # student plotted different points; the model claims full marks and reads the expected values
    wrong, _ = draw_graph(points=[(1, 6), (2, 1), (3, 9), (5, 2), (7, 4)])
    cvw = diagram_cv.analyze(png(wrong))
    bad = diagram_cv.cross_check(SPEC, _features(), readings, cvw)
    assert bad["agrees"] is False and bad["points_matched"] is not None and bad["points_matched"] < 3
    # model says the axes are missing but two axes are there
    ax = diagram_cv.cross_check(SPEC, _features(axes="missing"), readings, cv)
    assert ax["agrees"] is False and any("axes" in i for i in ax["issues"])
    # no readings, no picture: nothing to compare
    assert diagram_cv.cross_check(SPEC, _features(), [], None)["agrees"] is None


# ---------------------------------------------------------------- end to end (fake AI transport)
DIAGRAM_EXAM = {
    "title": "Lab graphs", "duration_min": 30, "course": "PHYS 101", "delivery": "paper",
    "questions": [
        {"type": "essay", "prompt": "Plot the data from the table on a graph.", "points": 6, "answer_lines": 30,
         "topics": ["graphs"], "diagram": {"kind": "data_chart", "features": SPEC["features"]}},
        {"type": "short", "prompt": "Unit of force?", "points": 1, "accepted_answers": ["N"], "topics": ["units"]},
    ],
}


def _images(request: httpx.Request) -> list[bytes]:
    body = json.loads(request.content)
    out = []
    for c in body["messages"][1]["content"]:
        if c["type"] == "image_url":
            out.append(base64.b64decode(c["image_url"]["url"].split(",", 1)[1]))
    return out


def _fake_ai(stats):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = "".join(c.get("text", "") for c in body["messages"][1]["content"] if c["type"] == "text")
        imgs = _images(request)
        stats.append({"text": text, "images": len(imgs)})
        if "GRAPH / DIAGRAM QUESTION" in text:
            # a 'vision model' that reads the dots it sees: value grid from the drawn axes
            cv = diagram_cv.analyze(imgs[0])
            dots = sorted(cv.get("dots") or [])
            readings = [{"x": x, "y": y, "at": d} for (x, y), d in zip(EXPECTED, dots)]
            full = len(dots) == 5
            out = {"legibility": "clear", "transcript": "scatter graph",
                   "steps": [{"text": "axes drawn", "status": "correct"}],
                   "features": [{"id": "f1", "status": "present", "points": 1, "box": [0.1, 0.1, 0.9, 0.9]},
                                {"id": "f2", "status": "missing", "points": 0, "note": "No units on the axes",
                                 "point": [0.5, 0.95]},
                                {"id": "f3", "status": "present", "points": 1},
                                {"id": "f4", "status": "present", "points": 3, "box": [0.15, 0.2, 0.6, 0.75],
                                 "note": "All five points correct" if full else "Points look right"}],
                   "readings": readings, "points": 5, "explanation": "Good plot — add units to the axis labels.",
                   "teacher_note": "Units missing", "misconception": {"tag": "units", "text": ""},
                   "confidence": 0.9}
        else:
            out = {"transcript": "N", "points": 1, "confidence": 0.95, "explanation": "Correct."}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})
    return httpx.MockTransport(handler)


def test_diagram_question_end_to_end(client, teacher, alice, monkeypatch):
    r = client.get("/api/diagram-kinds", headers=teacher)
    assert r.status_code == 200 and len(r.json()["kinds"]) == len(diagram_spec.KINDS)
    assert client.get("/api/diagram-kinds", headers=alice).status_code == 403

    r = client.post("/api/exams", json=DIAGRAM_EXAM, headers=teacher)
    assert r.status_code == 201, r.text
    e = r.json()
    qd, qs = [x["id"] for x in e["questions"]]
    assert e["questions"][0]["diagram"]["kind"] == "data_chart"
    assert "diagram" not in e["questions"][1]
    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice", "bob"]}, headers=teacher)

    # reference drawing (teacher's correct graph) in the answer key step override
    ref, _ = draw_graph(seed=9)
    up = client.post("/api/uploads", files={"file": ("ref.png", png(ref), "image/png")}, headers=teacher)
    assert up.status_code in (200, 201), up.text
    ref_url = up.json()["url"]
    spec = dict(e["questions"][0]["diagram"], reference_image=ref_url)
    r = client.put(f"/api/exams/{e['id']}/diagrams/{qd}", json={"diagram": spec}, headers=teacher)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "key" and r.json()["diagram"]["reference_image"] == ref_url
    assert client.put(f"/api/exams/{e['id']}/diagrams/{qs}", json={"diagram": spec},
                      headers=teacher).status_code == 422
    bad = dict(spec, features=[dict(f, points=9) for f in spec["features"]])
    assert client.put(f"/api/exams/{e['id']}/diagrams/{qd}", json={"diagram": bad}, headers=teacher).status_code == 422
    lst = client.get(f"/api/exams/{e['id']}/diagrams", headers=teacher).json()
    assert [x["source"] for x in lst["questions"]] == ["key", None]

    # print, draw: alice plots the five points, bob only draws axes; scan
    pdf = client.get(f"/api/exams/{e['id']}/print.pdf", headers=teacher).content
    layout, _ = build_layout(e["id"], e["questions"])
    pages = _render(pdf)
    per = layout["page_count"]
    box = next(b for pg in layout["pages"] for b in pg["boxes"] if b["qid"] == qd)
    pno = next(i for i, pg in enumerate(layout["pages"]) if any(b["qid"] == qd for b in pg["boxes"]))
    x0, y0, x1, y1 = [int(v * S) for v in box["rect"]]
    bw, bh = x1 - x0, y1 - y0
    for k, pts in ((0, EXPECTED), (1, [])):
        page = pages[k * per + pno]
        draw_graph(w=bw - 40, h=bh - 40, points=pts, parabola=False, canvas=page, at=(x0 + 20, y0 + 20),
                   ox=90, oy=bh - 120, sx=min(110, (bw - 200) // 10), sy=min(90, (bh - 200) // 10))
    sbox = _boxes(layout, next(i + 1 for i, pg in enumerate(layout["pages"]) if any(b["qid"] == qs for b in pg["boxes"])))
    for k in (0, 1):
        sx0, sy0, _, _ = sbox[qs]["rect"]
        pi = next(i for i, pg in enumerate(layout["pages"]) if any(b["qid"] == qs for b in pg["boxes"]))
        cv2.putText(pages[k * per + pi], "N", (int(sx0 * S) + 25, int(sy0 * S) + 70), cv2.FONT_HERSHEY_SIMPLEX,
                    1.4, (30, 30, 60), 3)
    r = client.post(f"/api/exams/{e['id']}/scans", headers=teacher,
                    files=[("files", ("class.pdf", _to_pdf(pages), "application/pdf"))])
    assert r.status_code == 202, r.text
    _wait(client, teacher, e["id"])

    stats: list = []
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    ai.set_transport(_fake_ai(stats))
    r = client.post(f"/api/exams/{e['id']}/scan/ai-grade", json={}, headers=teacher)
    assert r.status_code == 202, r.text
    end = time.time() + 60
    while client.get(f"/api/exams/{e['id']}/scan/ai-grade", headers=teacher).json()["running"]:
        assert time.time() < end
        time.sleep(0.1)
    diag_calls = [s for s in stats if "GRAPH / DIAGRAM QUESTION" in s["text"]]
    assert len(diag_calls) == 2 and all(s["images"] == 2 for s in diag_calls)  # student + reference
    assert all("LAST image is the teacher's correct drawing" in s["text"] for s in diag_calls)
    assert not any("GRAPH" in s["text"] for s in stats if s not in diag_calls)

    queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    qq = {x["question_id"]: x for x in queue["questions"]}
    assert qq[qd]["diagram"]["kind"] == "data_chart" and qq[qs]["diagram"] is None
    assert [c["id"] for c in qq[qd]["criteria"]] == ["f1", "f2", "f3", "f4"]
    items = {(i["username"], i["question_id"]): i for i in queue["items"]}
    da = items[("alice", qd)]["draft"]
    assert da["points"] == 5 and da["diagram"]["check"]["agrees"] is True, da["diagram"]["check"]
    assert da["diagram"]["check"]["points_matched"] == 5 and da["needs_review"] is False
    assert da["diagram"]["reference_image"] == ref_url
    db = items[("bob", qd)]["draft"]  # model says the points are right, nothing is plotted
    assert db["needs_review"] and "diagram_check_disagrees" in db["review_reasons"]
    assert db["confidence"] <= diagrams.DISAGREE_CONFIDENCE

    # teacher ticks "Axis labels" for alice (criteria toggle) -> 6 points, ai_edited, statuses recomputed
    al = next(a for a in client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()["attempts"]
              if a["username"] == "alice")
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{qd}/approve", headers=teacher,
                    json={"criteria": [{"id": "f1", "points": 1}, {"id": "f2", "points": 1}, {"id": "f3", "points": 1},
                                       {"id": "f4", "points": 2}], "grading_seconds": 9})
    assert r.status_code == 200, r.text
    g = r.json()["grade"]
    assert g["points"] == 5 and g["source"] == "ai_edited" and g["rubric_scores"] is None
    assert [(f["id"], f["status"]) for f in g["diagram"]["features"]] == [
        ("f1", "present"), ("f2", "present"), ("f3", "present"), ("f4", "partial")]
    assert g["diagram"]["features"][3]["box"] == [0.15, 0.2, 0.6, 0.75]
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{qs}/approve", headers=teacher, json={})
    assert r.status_code == 200
    bo = next(a for a in client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()["attempts"]
              if a["username"] == "bob")
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{bo['attempt_id']}/{qd}/approve", headers=teacher,
                    json={"criteria": [{"id": "f1", "points": 1}, {"id": "f2", "points": 0}, {"id": "f3", "points": 1},
                                       {"id": "f4", "points": 0}]})
    assert r.status_code == 200
    gb = r.json()["grade"]
    assert gb["points"] == 2 and gb["diagram"]["features"][1]["status"] == "missing"
    assert gb["diagram"]["features"][3]["status"] == "wrong"  # AI said present, the teacher said no
    client.post(f"/api/exams/{e['id']}/scan/answers/{bo['attempt_id']}/{qs}/approve", headers=teacher, json={})

    # student feedback: approved checklist + marks, reference drawing
    r = client.post(f"/api/exams/{e['id']}/release", json={}, headers=teacher)
    assert r.status_code == 200, r.text
    fb = client.get(f"/api/student/grades/{e['id']}", headers=alice).json()
    fq = {x["question_id"]: x for x in fb["questions"]}
    dg = fq[qd]["diagram"]
    assert dg["kind"] == "data_chart" and [f["status"] for f in dg["features"]] == [
        "present", "present", "present", "partial"]
    assert dg["reference_image"] == ref_url and fq[qd]["image"].startswith("/api/uploads/")
    assert "diagram" not in fq[qs]
    # students never see the checklist while writing
    sv = client.get(f"/api/exams/{e['id']}", headers=teacher).json()
    assert sv["questions"][0]["diagram"]["kind"] == "data_chart"


def test_prepare_adds_reference_and_cv(tmp_path):
    img, _ = draw_graph()
    ctx, imgs = asyncio.run(diagrams.prepare({"diagram": SPEC}, [png(img)]))
    assert len(imgs) == 1 and ctx["diagram_cv"]["ok"] and "diagram_reference" not in ctx
    ctx, imgs = asyncio.run(diagrams.prepare({}, [png(img)]))
    assert ctx == {} and len(imgs) == 1
