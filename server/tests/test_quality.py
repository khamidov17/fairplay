"""Phase 3 — grading quality: metrics math, routing rules, benchmark (consent, import, runs),
live production metrics, blind second marking, moderation, permissions."""
import io
import json
import time
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app import ai
from app.quality import metrics, routing
from app.quality.importer import parse_csv
from app.quality.metrics import Pair

from .conftest import login


# ---------------------------------------------------------------- helpers
def make_user(client, username, role="teacher", name=None):
    from app.db import get_database
    from app.models import User
    from app.security import hash_password

    async def go():
        async with get_database().sessionmaker() as s:
            s.add(User(username=username, name=name or username.title(), role=role,
                       password_hash=hash_password(f"{username}123", 1000)))
            await s.commit()
    client.portal.call(go)
    return login(client, username)


def user_id(client, username):
    from sqlalchemy import select

    from app.db import get_database
    from app.models import User

    async def go():
        async with get_database().sessionmaker() as s:
            return (await s.execute(select(User.id).where(User.username == username))).scalar()
    return client.portal.call(go)


def add_attempt(client, exam_id, username, answers, grades=None, drafts=None):
    from app.db import get_database
    from app.grading import exam_questions, grade_attempt
    from app.models import Attempt, Exam
    from app.services import new_id
    from app.timeutil import utcnow

    sid = user_id(client, username)

    async def go():
        async with get_database().sessionmaker() as s:
            e = await s.get(Exam, exam_id)
            now = utcnow()
            score, pending, _ = grade_attempt(exam_questions(e), answers, grades or {})
            a = Attempt(id=new_id("a"), exam_id=exam_id, student_id=sid, status="submitted", started_at=now,
                        deadline_at=now + timedelta(hours=1), submitted_at=now, answers=answers, client_seq=1,
                        receipt="AAAA-BBBB", grades=grades or {}, overall_comment="", ai_drafts=drafts or {},
                        score=score, needs_grading=pending)
            s.add(a)
            await s.commit()
            return a.id
    return client.portal.call(go)


ESSAY_EXAM = {
    "title": "Essays", "duration_min": 30, "course": "HIST 101",
    "questions": [
        {"type": "essay", "prompt": "Why did the empire fall?", "points": 10, "topics": ["empires"],
         "rubric": [{"label": "Two causes", "points": 6}, {"label": "Evidence", "points": 4}]},
        {"type": "essay", "prompt": "Define feudalism.", "points": 4, "topics": ["feudalism"]},
        {"type": "mcq", "prompt": "Pick B", "points": 2, "options": ["A", "B"], "correct_index": 1,
         "topics": ["misc"]},
    ],
}


@pytest.fixture
def exam(client, teacher):
    e = client.post("/api/exams", json=ESSAY_EXAM, headers=teacher)
    assert e.status_code == 201, e.text
    e = e.json()
    assert client.post(f"/api/exams/{e['id']}/open", headers=teacher).status_code == 200
    e = client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    assert e.status_code == 200, e.text
    e = e.json()
    return e


def g(points, **kw):
    return {"points": points, "comment": kw.pop("comment", ""), "rubric_scores": None,
            "graded_at": "2026-10-01T10:00:00.000Z", **kw}


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


# ---------------------------------------------------------------- metrics math
def test_metrics_hand_checked():
    pairs = [Pair(0, 0, 2, "essay"), Pair(1, 2, 2, "essay"), Pair(2, 2, 2, "short"), Pair(2, 1, 2, "short")]
    m = metrics.summarize(pairs)
    assert m["n"] == 4
    assert m["exact"] == 0.5
    assert m["within_1"] == 1.0
    assert m["within_10pct"] == 0.5          # 10 % of 2 = 0.2 points
    assert m["mae"] == 0.5
    assert m["bias"] == 0.0
    # QWK by hand: sum(W*O) = 0.5, sum(W*E) = 1.375 -> 1 - 0.5/1.375 = 7/11
    assert m["kappa"] == round(7 / 11, 4)
    assert m["severe"] == 2 and m["severe_rate"] == 0.5   # |diff| 1 >= 25 % of 2
    by = {r["key"]: r for r in metrics.breakdown(pairs, "question_type")}
    assert by["essay"]["n"] == 2 and by["essay"]["exact"] == 0.5 and by["essay"]["bias"] == -0.5
    assert by["short"]["bias"] == 0.5


def test_kappa_edge_cases():
    qwk = metrics.quadratic_weighted_kappa
    assert qwk([0, 1, 2, 3], [0, 1, 2, 3]) == 1.0
    assert qwk([0, 3], [3, 0]) < 0                     # systematic disagreement
    assert qwk([2, 2], [2, 2]) is None                 # one category: undefined
    assert qwk([1], [1]) is None
    # unequal max points: integer marks share one 0..max scale; half points round up
    m = metrics.summarize([Pair(2.5, 3, 4), Pair(0, 0, 10), Pair(10, 9, 10)])
    assert m["exact"] == round(1 / 3, 4) and m["kappa"] is not None and m["kappa"] > 0.9
    assert m["severe"] == 0
    assert metrics.summarize([])["kappa"] is None


def test_severe_threshold_configurable():
    pairs = [Pair(7, 10, 10), Pair(8, 10, 10)]
    assert metrics.summarize(pairs)["severe"] == 1               # 3 >= 2.5
    assert metrics.summarize(pairs, severe_fraction=0.15)["severe"] == 2
    assert metrics.summarize(pairs, severe_fraction=0.5)["severe"] == 0


def test_correction_and_timing_stats():
    sp = metrics.suggested_points
    assert sp({"points": 3, "ai_suggested_points": 4}, {"points": 2}) == 4
    assert sp({"points": 3}, {"points": 2, "source": "ai"}) == 2
    assert sp({"points": 3}, {"points": 0, "source": "fallback"}) is None
    assert sp({"points": 3}, None) is None
    assert metrics.grade_source({"points": 3}, 3) == "ai_accepted"
    assert metrics.grade_source({"points": 2}, 3) == "ai_edited"
    assert metrics.grade_source({"points": 2, "source": "manual"}, 3) == "manual"
    assert metrics.grade_source({"points": 2}, None) == "manual"
    items = [{"suggested": 3, "final": 3, "max_points": 10, "source": "ai_accepted"},
             {"suggested": 3, "final": 5, "max_points": 10, "source": "ai_edited"},
             {"suggested": 3, "final": 3, "max_points": 10, "source": "ai_edited"},  # comment-only edit
             {"suggested": None, "final": 1, "max_points": 10, "source": "manual"}]
    c = metrics.correction_stats(items)
    assert c["ai_suggested"] == 3 and c["edited"] == 2 and c["correction_rate"] == round(2 / 3, 4)
    assert c["mean_edit"] == 1.0 and c["points_changed"] == 1 and c["mean_edit_pct"] == 10.0
    t = metrics.timing_stats([{"seconds": 20, "assisted": True, "batch": "b1"},
                              {"seconds": 40, "assisted": True, "batch": "b1"},
                              {"seconds": 90, "assisted": False, "batch": "b2"},
                              {"seconds": None, "assisted": False, "batch": "b2"}])
    assert t["ai_assisted"] == {"n": 2, "mean": 30.0, "median": 30.0, "total": 60.0}
    assert t["manual"]["n"] == 1 and t["manual"]["mean"] == 90.0
    assert t["time_saved_fraction"] == round(1 - 30 / 90, 4)
    assert [b["batch"] for b in t["batches"]] == ["b1", "b2"] and t["batches"][0]["total_seconds"] == 60.0


# ---------------------------------------------------------------- routing rules
def _ex(qs, total=None, course="HIST 101"):
    return SimpleNamespace(questions=qs, mode="questions", course=course,
                           total_points=total if total is not None else sum(q["points"] for q in qs))


def _att(drafts, grades=None, answers=None):
    return SimpleNamespace(ai_drafts=drafts, grades=grades or {}, answers=answers or {})


Q1 = {"id": "q1", "type": "essay", "points": 10, "prompt": "p", "topics": ["empires"],
      "rubric": [{"label": "a", "points": 6}, {"label": "b", "points": 4}]}
Q2 = {"id": "q2", "type": "essay", "points": 10, "prompt": "p2", "topics": ["feudalism"]}


def codes(r):
    return [x["code"] for x in r["reasons"]]


def test_route_item_rules():
    ex = _ex([Q1, Q2], total=20)
    pol = {"grade_boundaries": [50], "boundary_margin": 1}
    ok = {"points": 3, "confidence": 0.95, "source": "ai"}
    # q2 graded 3 -> total 6 of 20: far from the 10-point boundary
    base = {"q2": g(3)}
    r = routing.route_item(ex, _att({"q1": ok}, base), "q1", pol)
    assert r == {"review": False, "reasons": []}
    assert codes(routing.route_item(ex, _att({}, base), "q1", pol)) == ["no_ai_suggestion"]
    assert codes(routing.route_item(ex, _att({"q1": {"points": 0, "source": "fallback", "confidence": 0}}),
                                     "q1", pol)) == ["no_ai_suggestion"]
    assert codes(routing.route_item(ex, _att({"q1": {"points": 3, "source": "ai"}}, base), "q1", pol)) == \
        ["confidence_unknown"]
    r = routing.route_item(ex, _att({"q1": dict(ok, confidence=0.5)}, base), "q1", pol)
    assert codes(r) == ["low_confidence"] and "0.50" in r["reasons"][0]["message"]
    assert codes(routing.route_item(ex, _att({"q1": dict(ok, confidence=0.5)}, base), "q1",
                                    dict(pol, confidence_threshold=0.4))) == []
    # illegible handwriting (Phase 2 needs_review + reason) and a generic AI flag
    ill = dict(ok, needs_review=True, needs_review_reason="Illegible handwriting in line 3")
    assert codes(routing.route_item(ex, _att({"q1": ill}, base), "q1", pol)) == ["illegible_handwriting"]
    flag = dict(ok, needs_review=True, needs_review_reason="Answer uses a method not in the rubric")
    r = routing.route_item(ex, _att({"q1": flag}, base), "q1", pol)
    assert codes(r) == ["ai_flagged"] and "not in the rubric" in r["reasons"][0]["message"]
    # criteria inconsistent with total / above the rubric cap
    bad = dict(ok, criteria=[{"points": 2}, {"points": 2}])
    assert codes(routing.route_item(ex, _att({"q1": bad}, base), "q1", pol)) == ["criteria_inconsistent"]
    cap = dict(ok, points=8, criterion_points=[7, 1])  # criterion 1 max 6
    assert codes(routing.route_item(ex, _att({"q1": cap}, {"q2": g(9)}), "q1", pol)) == ["criteria_inconsistent"]
    good = dict(ok, criteria=[{"points": 2, "max_points": 6}, {"points": 1, "max_points": 4}])
    assert codes(routing.route_item(ex, _att({"q1": good}, base), "q1", pol)) == []
    # near the 50 % boundary: q2 graded 6 + AI 3 = 9 of 20, within 1 point of 10
    r = routing.route_item(ex, _att({"q1": ok}, {"q2": g(6)}), "q1", pol)
    assert codes(r) == ["near_boundary"]
    # another answer's AI draft counts in the projected total too
    r = routing.route_item(ex, _att({"q1": ok, "q2": {"points": 7, "confidence": 0.9}}), "q1", pol)
    assert codes(r) == ["near_boundary"]
    assert codes(routing.route_item(ex, _att({"q1": ok}, {"q2": g(6)}), "q1", dict(pol, boundary_margin=0))) == []
    # weak areas from the benchmark
    weak = {"types": {"essay": 0.41}, "subjects": {"hist 101": 0.5}}
    r = routing.route_item(ex, _att({"q1": ok}, base), "q1", pol, weak)
    assert codes(r) == ["weak_question_type", "weak_subject"]
    assert codes(routing.route_item(ex, _att({"q1": ok}, base), "q1", pol, {"subjects": {"empires": 0.3}})) == \
        ["weak_subject"]
    # out of range points
    assert "points_out_of_range" in codes(routing.route_item(ex, _att({"q1": dict(ok, points=12)}, base), "q1", pol))
    # unknown question -> nothing to route
    assert routing.route_item(ex, _att({}), "nope") == {"review": False, "reasons": []}


def test_routing_endpoint_and_policy(client, teacher, exam):
    eid = exam["id"]
    q1, q2, q3 = [q["id"] for q in exam["questions"]]
    a1 = add_attempt(client, eid, "alice", {q1: "Long essay", q2: "Short", q3: 1},
                     drafts={q1: {"points": 7, "confidence": 0.95, "source": "ai"},
                             q2: {"points": 2, "confidence": 0.4, "source": "ai"}})
    add_attempt(client, eid, "bob", {q1: "Essay", q2: "x", q3: 0},
                drafts={q1: {"points": 2, "confidence": 0.9, "needs_review": True,
                             "needs_review_reason": "handwriting is illegible"}})
    r = client.get(f"/api/exams/{eid}/quality/routing", headers=teacher)
    assert r.status_code == 200, r.text
    body = r.json()
    items = {(i["attempt_id"], i["question_id"]): i for i in body["items"]}
    assert len(items) == 4  # 2 attempts x 2 essays; the auto-graded mcq is not routed
    assert not items[(a1, q1)]["review"] or codes(items[(a1, q1)]) == ["near_boundary"]
    assert "low_confidence" in codes(items[(a1, q2)])
    assert body["summary"]["routed"] >= 3 and body["summary"]["by_reason"]["low_confidence"] == 1
    # exam policy override: lower threshold, no boundaries
    r = client.put(f"/api/exams/{eid}/quality/routing-policy",
                   json={"confidence_threshold": 0.3, "grade_boundaries": []}, headers=teacher)
    assert r.status_code == 200 and r.json()["policy"]["confidence_threshold"] == 0.3
    assert r.json()["inherits"] is False
    body = client.get(f"/api/exams/{eid}/quality/routing", headers=teacher).json()
    items = {(i["attempt_id"], i["question_id"]): i for i in body["items"]}
    assert items[(a1, q1)]["review"] is False and items[(a1, q2)]["review"] is False
    one = client.get(f"/api/exams/{eid}/quality/routing/{a1}/{q2}", headers=teacher).json()
    assert one["review"] is False
    # teacher default applies when the exam override is removed
    client.put("/api/quality/routing-policy", json={"confidence_threshold": 0.5}, headers=teacher)
    r = client.delete(f"/api/exams/{eid}/quality/routing-policy", headers=teacher)
    assert r.json()["inherits"] is True and r.json()["policy"]["confidence_threshold"] == 0.5
    assert client.get(f"/api/exams/{eid}/quality/routing/{a1}/{q2}", headers=teacher).json()["review"] is True
    r = client.put("/api/quality/routing-policy", json={"grade_boundaries": [120]}, headers=teacher)
    assert r.status_code == 422


# ---------------------------------------------------------------- benchmark: consent + no names
def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, "PNG")
    return buf.getvalue()


def test_benchmark_consent_and_no_student_name(client, teacher, exam):
    eid = exam["id"]
    q1 = exam["questions"][0]["id"]
    aid = add_attempt(client, eid, "alice", {q1: "Rome fell because of debt and invasions."},
                      grades={q1: g(7, comment="Good causes, thin evidence")})
    s = client.post("/api/quality/benchmarks", json={"name": "History essays", "subject": "History",
                                                     "consent_note": "Opt-in form, Fall 2026",
                                                     "consent_basis": "opt-in"}, headers=teacher)
    assert s.status_code == 201, s.text
    sid = s.json()["id"]
    body = {"exam_id": eid, "attempt_id": aid, "question_id": q1}
    r = client.post(f"/api/quality/benchmarks/{sid}/items/from-grade", json=body, headers=teacher)
    assert r.status_code == 422 and "consent" in r.json()["detail"]
    r = client.post(f"/api/quality/benchmarks/{sid}/items/from-grade", json={**body, "consent": False},
                    headers=teacher)
    assert r.status_code == 422
    r = client.post(f"/api/quality/benchmarks/{sid}/items/from-grade", json={**body, "consent": True},
                    headers=teacher)
    assert r.status_code == 201, r.text
    it = r.json()
    assert it["human_points"] == 7 and it["human_comment"] == "Good causes, thin evidence"
    assert it["answer_text"].startswith("Rome fell") and it["max_points"] == 10 and len(it["rubric"]) == 2
    assert it["consent"] is True and it["consent_at"] and it["source"]["attempt_id"] == aid
    dumped = json.dumps(client.get(f"/api/quality/benchmarks/{sid}", headers=teacher).json())
    assert "Alice" not in dumped and "alice" not in dumped
    # the table has no column that could hold a name
    from app.quality.models import BenchmarkItem
    cols = {c.name for c in BenchmarkItem.__table__.columns}
    assert not cols & {"name", "student_name", "username", "student_id", "student"}
    # duplicate refused; ungraded answer refused
    r = client.post(f"/api/quality/benchmarks/{sid}/items/from-grade", json={**body, "consent": True},
                    headers=teacher)
    assert r.status_code == 409
    q2 = exam["questions"][1]["id"]
    r = client.post(f"/api/quality/benchmarks/{sid}/items/from-grade",
                    json={**body, "question_id": q2, "consent": True}, headers=teacher)
    assert r.status_code == 409
    # manual item: consent required, marks in range
    item = {"question_prompt": "Explain inflation", "question_type": "essay", "max_points": 5,
            "answer_text": "Prices go up", "human_marks": [3, 4]}
    assert client.post(f"/api/quality/benchmarks/{sid}/items", json=item, headers=teacher).status_code == 422
    assert client.post(f"/api/quality/benchmarks/{sid}/items", json={**item, "consent": True, "human_marks": [6]},
                       headers=teacher).status_code == 422
    r = client.post(f"/api/quality/benchmarks/{sid}/items", json={**item, "consent": True}, headers=teacher)
    assert r.status_code == 201 and r.json()["human_points"] == 3 and r.json()["human_marks"] == [3, 4]
    assert r.json()["subject"] == "History"
    lst = client.get("/api/quality/benchmarks", headers=teacher).json()
    assert lst[0]["item_count"] == 2


def test_benchmark_csv_import(client, teacher):
    sid = client.post("/api/quality/benchmarks", json={"name": "Calc"}, headers=teacher).json()["id"]
    csv_text = ("prompt,type,max_points,rubric,answer_key,answer_text,image,human_mark,comment,Student Name\n"
                "Integrate x,essay,4,method:2;answer:2,,x^2/2 + C,,4,Perfect,Alice Chen\n"
                "Derivative of x^2,numeric,2,,2x,,ans1.png,1|2,,Bob Karimov\n"
                "Bad row,essay,3,,,text,,5,,Carol\n"
                "Missing pic,essay,3,,,,nothere.png,1,,Dan\n")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("scans/ans1.png", _png())
    files = {"file": ("bench.csv", csv_text.encode(), "text/csv"),
             "images": ("imgs.zip", zbuf.getvalue(), "application/zip")}
    r = client.post(f"/api/quality/benchmarks/{sid}/import", files=files, headers=teacher)
    assert r.status_code == 422  # no consent
    r = client.post(f"/api/quality/benchmarks/{sid}/import", files=files, data={"consent": "true"},
                    headers=teacher)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["imported"] == 2
    assert [e["row"] for e in out["errors"]] == [4, 5]
    assert out["dropped_personal_columns"] == ["Student Name"]
    full = client.get(f"/api/quality/benchmarks/{sid}", headers=teacher).json()
    assert "Alice" not in json.dumps(full) and "Bob" not in json.dumps(full)
    pic = [i for i in full["items"] if i["answer_image"]][0]
    assert pic["human_marks"] == [1, 2] and pic["question_type"] == "numeric"
    assert client.get(pic["answer_image"]).status_code == 200
    rows, errors, _ = parse_csv(b"prompt,max_points\nx,1\n")
    assert rows == [] and "human_marks" in errors[0]["error"]


# ---------------------------------------------------------------- benchmark run with a fake AI
def _fake_ai(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = "".join(c.get("text", "") for c in body["messages"][0]["content"] if c["type"] == "text")
        calls.append(text)
        if "Q-A" in text:
            out = {"points": 4, "confidence": 0.9, "explanation": "Full marks"}
        elif "Q-B" in text:
            out = {"points": 1, "confidence": 0.6, "explanation": "Partly right", "needs_review": True}
        elif "Q-C" in text:
            out = {"points": 0, "confidence": 0.8}
        else:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(out)}]})
    return httpx.MockTransport(handler)


def _wait_run(client, headers, run_id):
    for _ in range(200):
        r = client.get(f"/api/quality/runs/{run_id}", headers=headers).json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_benchmark_run_with_fake_ai(client, teacher, monkeypatch):
    sid = client.post("/api/quality/benchmarks", json={"name": "Set", "subject": "Math"}, headers=teacher).json()["id"]
    items = [("Q-A", "essay", 4, [4]), ("Q-B", "essay", 4, [3, 2]), ("Q-C", "short", 2, [2]), ("Q-D", "short", 2, [0])]
    for prompt, t, mx, marks in items:
        r = client.post(f"/api/quality/benchmarks/{sid}/items", json={
            "question_prompt": prompt, "question_type": t, "max_points": mx, "answer_text": "answer",
            "human_marks": marks, "consent": True}, headers=teacher)
        assert r.status_code == 201
    r = client.post(f"/api/quality/benchmarks/{sid}/runs", json={}, headers=teacher)
    assert r.status_code == 409 and "AI is off" in r.json()["detail"]
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "anthropic")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    calls: list[str] = []
    ai.set_transport(_fake_ai(calls))
    r = client.post(f"/api/quality/benchmarks/{sid}/runs", json={"severe_fraction": 0.25}, headers=teacher)
    assert r.status_code == 202, r.text
    run = _wait_run(client, teacher, r.json()["id"])
    assert run["status"] == "done" and run["grader"] == "default" and run["prompt_version"]
    assert run["provider"] == "anthropic"
    assert len(calls) == 4
    m = run["metrics"]
    # pairs (ai, human, max): (4,4,4) (1,3,4) (0,2,2); Q-D failed (AI error)
    assert m["failed"] == 1
    o = m["overall"]
    assert o["n"] == 3 and o["exact"] == round(1 / 3, 4) and o["mae"] == round(4 / 3, 4)
    assert o["bias"] == round((5 - 9) / 3, 4) and o["severe"] == 2
    types = {x["key"]: x for x in m["by_type"]}
    assert types["short"]["n"] == 1 and types["short"]["severe_rate"] == 1.0
    assert m["by_subject"][0]["key"] == "math"
    assert m["human_vs_human"]["n"] == 1 and m["human_vs_human"]["mae"] == 1.0
    worst = run["items"][0]
    assert worst["prompt"] == "Q-C" and worst["severe"] is True
    failed = [i for i in run["items"] if i["error"]]
    assert len(failed) == 1 and failed[0]["ai_points"] is None
    assert any(i["needs_review"] for i in run["items"])
    # a pluggable grader (what Phase 2 drops in), no AI needed
    from app.quality import graders

    async def strict(item):
        assert "human_points" not in item and "human_marks" not in item
        return {"points": 0, "confidence": 1}
    graders.register_grader("strict", strict, "v9", needs_ai=False)
    try:
        r = client.post(f"/api/quality/benchmarks/{sid}/runs", json={"grader": "strict"}, headers=teacher)
        run2 = _wait_run(client, teacher, r.json()["id"])
        assert run2["grader"] == "strict" and run2["prompt_version"] == "v9"
        assert run2["metrics"]["overall"]["n"] == 4 and run2["metrics"]["failed"] == 0
        assert client.post(f"/api/quality/benchmarks/{sid}/runs", json={"grader": "nope"},
                           headers=teacher).status_code == 422
    finally:
        graders.unregister_grader("strict")
    runs = client.get(f"/api/quality/benchmarks/{sid}/runs", headers=teacher).json()
    assert len(runs) == 2


def test_weak_benchmark_areas_route_to_review(client, teacher, exam, monkeypatch):
    """A question type the AI handles badly on the benchmark (kappa < 0.6) is routed."""
    from app.quality import graders
    sid = client.post("/api/quality/benchmarks", json={"name": "E"}, headers=teacher).json()["id"]
    for k in range(6):
        client.post(f"/api/quality/benchmarks/{sid}/items", json={
            "question_prompt": f"essay {k}", "question_type": "essay", "max_points": 4,
            "answer_text": "a", "human_marks": [k % 5], "consent": True}, headers=teacher)

    async def contrarian(item):
        return {"points": 4 - int(item["question"]["prompt"][-1]) % 5}
    graders.register_grader("contrarian", contrarian, needs_ai=False)
    try:
        r = client.post(f"/api/quality/benchmarks/{sid}/runs", json={"grader": "contrarian"}, headers=teacher)
        run = _wait_run(client, teacher, r.json()["id"])
    finally:
        graders.unregister_grader("contrarian")
    assert run["metrics"]["overall"]["kappa"] < 0.6
    q1 = exam["questions"][0]["id"]
    add_attempt(client, exam["id"], "alice", {q1: "x"},
                drafts={q1: {"points": 2, "confidence": 0.99, "source": "ai"}})
    body = client.get(f"/api/exams/{exam['id']}/quality/routing", headers=teacher).json()
    assert "essay" in body["weak_areas"]["types"]
    it = [i for i in body["items"] if i["question_id"] == q1][0]
    assert "weak_question_type" in codes(it)


# ---------------------------------------------------------------- live production metrics
def test_live_quality_metrics(client, teacher, exam):
    eid = exam["id"]
    q1, q2, _ = [q["id"] for q in exam["questions"]]
    add_attempt(client, eid, "alice", {q1: "a", q2: "b"},
                grades={q1: g(8, ai_suggested_points=8, source="ai_accepted", grading_seconds=20, graded_by="teacher"),
                        q2: g(1, ai_suggested_points=4, source="ai_edited", grading_seconds=40)})
    add_attempt(client, eid, "bob", {q1: "a", q2: "b"},
                grades={q1: g(5, source="manual", grading_seconds=120),
                        # pre-Phase 2 grade: no ai_suggested_points/source -> taken from the draft
                        q2: g(3)},
                drafts={q2: {"points": 3, "confidence": 0.9, "source": "ai"}})
    add_attempt(client, eid, "carol", {q1: "a"}, grades={q1: g(4, ai_suggested_points=7)})
    r = client.get(f"/api/exams/{eid}/quality", headers=teacher)
    assert r.status_code == 200, r.text
    d = r.json()
    a = d["agreement"]
    # pairs: (8,8,10) (4,1,4) (3,3,4) (7,4,10)
    assert a["n"] == 4 and a["exact"] == 0.5 and a["mae"] == 1.5 and a["severe"] == 2
    assert d["graded"] == 5
    c = d["correction"]
    assert c["ai_suggested"] == 4 and c["edited"] == 2 and c["correction_rate"] == 0.5 and c["mean_edit"] == 3.0
    t = d["timing"]
    assert t["ai_assisted"]["n"] == 2 and t["ai_assisted"]["mean"] == 30.0 and t["manual"]["mean"] == 120.0
    assert t["batches"][0]["batch"] == eid
    w = d["worst"]
    assert w[0]["question_id"] == q2 and w[0]["ai_points"] == 4 and w[0]["final_points"] == 1 and w[0]["severe"]
    assert w[0]["anon_id"].startswith("#") and "username" not in w[0]
    assert {x["question_id"] for x in d["by_question"]} == {q1, q2}
    ov = client.get("/api/quality/overview", headers=teacher).json()
    assert ov["agreement"]["n"] == 4 and ov["exams"][0]["exam_id"] == eid and ov["exams"][0]["correction_rate"] == 0.5
    assert client.get("/api/quality/overview?course=NOPE", headers=teacher).json()["agreement"]["n"] == 0
    graded = client.get(f"/api/exams/{eid}/quality/graded", headers=teacher).json()
    assert len(graded) == 5


# ---------------------------------------------------------------- second marking + moderation
def test_blind_second_marking_and_moderation(client, teacher, exam):
    t2 = make_user(client, "marker2", name="Second Marker")
    eid = exam["id"]
    q1, q2, q3 = [q["id"] for q in exam["questions"]]
    a1 = add_attempt(client, eid, "alice", {q1: "Alice's essay", q2: "def", q3: 1},
                     grades={q1: g(8, comment="FIRST-COMMENT", ai_suggested_points=9, source="ai_edited",
                                   rubric_scores=[5, 3]), q2: g(2)},
                     drafts={q1: {"points": 9, "comment": "AI-COMMENT", "confidence": 0.8, "source": "ai"}})
    a2 = add_attempt(client, eid, "bob", {q1: "Bob's essay", q2: "def"}, grades={q1: g(6), q2: g(1)})
    # validation
    r = client.post(f"/api/exams/{eid}/quality/second-marking", json={"marker_username": "teacher"},
                    headers=teacher)
    assert r.status_code == 422
    r = client.post(f"/api/exams/{eid}/quality/second-marking", json={"marker_username": "alice"}, headers=teacher)
    assert r.status_code == 404
    r = client.post(f"/api/exams/{eid}/quality/second-marking",
                    json={"marker_username": "marker2", "question_ids": [q1], "tolerance": 1}, headers=teacher)
    assert r.status_code == 201, r.text
    assert r.json()["created"] == 2
    # assigning again skips existing ones
    r = client.post(f"/api/exams/{eid}/quality/second-marking",
                    json={"marker_username": "marker2", "question_ids": [q1]}, headers=teacher)
    assert r.json() == {**r.json(), "created": 0, "skipped": 2}
    # the marker sees the answers blind
    q = client.get("/api/quality/second-marking/queue", headers=t2)
    assert q.status_code == 200
    raw = q.text
    for leak in ("FIRST-COMMENT", "AI-COMMENT", "Alice Chen", "Bob Karimov", "alice",
                 "ai_suggested", "confidence"):
        assert leak not in raw, leak
    for i in q.json()["items"]:
        assert set(i) == {"id", "exam_id", "exam_title", "course", "anon_id", "question", "answer", "status",
                          "my_mark"}
        assert set(i["answer"]) == {"text", "image"}
        assert set(i["question"]) == {"question_id", "index", "type", "prompt", "points", "rubric", "options",
                                      "images"}
    items = {i["answer"]["text"]: i for i in q.json()["items"]}
    assert set(items) == {"Alice's essay", "Bob's essay"} and q.json()["to_mark"] == 2
    it1 = items["Alice's essay"]
    assert it1["status"] == "to_mark" and it1["question"]["rubric"] and it1["my_mark"] is None
    assert it1["anon_id"].startswith("#")
    # the marker cannot use the lead's exam endpoints
    assert client.get(f"/api/exams/{eid}/quality/second-marking", headers=t2).status_code == 404
    assert client.get(f"/api/exams/{eid}/grading", headers=t2).status_code == 404
    assert client.get(f"/api/exams/{eid}/quality", headers=t2).status_code == 404
    # lead cannot mark as second marker; points validated
    assert client.put(f"/api/quality/second-marking/{it1['id']}/mark", json={"points": 3}, headers=teacher).status_code == 404
    assert client.put(f"/api/quality/second-marking/{it1['id']}/mark", json={"points": 11}, headers=t2).status_code == 422
    r = client.put(f"/api/quality/second-marking/{it1['id']}/mark", json={"points": 4, "comment": "SECOND"}, headers=t2)
    assert r.status_code == 200
    assert r.json()["status"] == "marked" and r.json()["my_mark"]["points"] == 4
    assert "FIRST-COMMENT" not in r.text and "needs_moderation" not in r.text and "agreed" not in r.text
    it2 = items["Bob's essay"]
    client.put(f"/api/quality/second-marking/{it2['id']}/mark", json={"points": 6.5}, headers=t2)
    # lead view: alice differs by 4 > 1 -> moderation; bob within tolerance -> agreed
    lead = client.get(f"/api/exams/{eid}/quality/second-marking", headers=teacher).json()
    rows = {x["attempt_id"]: x for x in lead["items"]}
    assert rows[a1]["status"] == "needs_moderation" and rows[a2]["status"] == "agreed"
    assert rows[a1]["first"]["points"] == 8 and rows[a1]["second"]["points"] == 4
    assert rows[a1]["ai"]["points"] == 9 and rows[a1]["username"] == "alice"
    assert lead["open"] == 1
    # release warns while moderation is open
    r = client.post(f"/api/exams/{eid}/release", json={}, headers=teacher)
    assert r.status_code == 409 and "moderation" in r.json()["detail"]
    assert client.get(f"/api/exams/{eid}/quality/release-check", headers=teacher).json()["moderation_open"] == 1
    # only the lead moderates; only items that need it
    assert client.post(f"/api/exams/{eid}/quality/second-marking/{rows[a1]['id']}/moderate",
                       json={"points": 6}, headers=t2).status_code == 404
    assert client.post(f"/api/exams/{eid}/quality/second-marking/{rows[a2]['id']}/moderate",
                       json={"points": 6}, headers=teacher).status_code == 409
    r = client.post(f"/api/exams/{eid}/quality/second-marking/{rows[a1]['id']}/moderate",
                    json={"points": 6, "comment": "Agreed after discussion"}, headers=teacher)
    assert r.status_code == 200, r.text
    grade = r.json()["grade"]
    assert grade["points"] == 6 and grade["moderated"] is True and grade["graded_by"] == "teacher"
    assert grade["moderation"]["first_points"] == 8 and grade["moderation"]["second_points"] == 4
    assert grade["moderation"]["moderated_points"] == 6
    assert grade["source"] == "ai_edited" and grade["ai_suggested_points"] == 9 and grade["rubric_scores"] is None
    # the final grade is the moderated mark
    res = client.get(f"/api/exams/{eid}/results", headers=teacher).json()
    alice = [x for x in res["attempts"] if x["attempt_id"] == a1][0]
    assert [x for x in alice["answers"] if x["question_id"] == q1][0]["points_awarded"] == 6
    lead = client.get(f"/api/exams/{eid}/quality/second-marking", headers=teacher).json()
    row = [x for x in lead["items"] if x["attempt_id"] == a1][0]
    assert row["status"] == "moderated" and row["first"]["points"] == 8 and row["moderated"]["points"] == 6
    assert row["moderated"]["comment"] == "Agreed after discussion" and row["moderated"]["at"]
    assert lead["open"] == 0
    # marker sees it closed and can no longer change it
    q = client.get("/api/quality/second-marking/queue", headers=t2).json()
    assert {i["status"] for i in q["items"]} == {"closed", "marked"}
    assert client.put(f"/api/quality/second-marking/{rows[a1]['id']}/mark", json={"points": 1},
                      headers=t2).status_code == 409
    assert client.post(f"/api/exams/{eid}/release", json={}, headers=teacher).status_code == 200


def test_second_marking_sample_and_routed(client, teacher, exam):
    make_user(client, "marker3")
    eid = exam["id"]
    q1, q2, _ = [q["id"] for q in exam["questions"]]
    for u in ("alice", "bob", "carol"):
        add_attempt(client, eid, u, {q1: "e", q2: "f"},
                    drafts={q1: {"points": 5, "confidence": 0.2 if u == "bob" else 0.99, "source": "ai"}})
    client.put(f"/api/exams/{eid}/quality/routing-policy",
               json={"grade_boundaries": [], "review_without_ai": False}, headers=teacher)
    r = client.post(f"/api/exams/{eid}/quality/second-marking",
                    json={"marker_username": "marker3", "routed_only": True}, headers=teacher)
    assert r.json()["created"] == 1  # only bob's low-confidence answer
    r = client.post(f"/api/exams/{eid}/quality/second-marking",
                    json={"marker_username": "marker3", "sample_percent": 50, "seed": 1}, headers=teacher)
    assert r.json()["created"] + r.json()["skipped"] == 3  # ceil(6 * 50 %)
    lst = client.get(f"/api/exams/{eid}/quality/second-marking", headers=teacher).json()
    assert {x["reason"] for x in lst["items"]} <= {"routed", "sample"}
    sm = lst["items"][0]["id"]
    assert client.delete(f"/api/exams/{eid}/quality/second-marking/{sm}", headers=teacher).status_code == 204


# ---------------------------------------------------------------- permissions
def test_other_teacher_cannot_read_sets_runs_or_exams(client, teacher, exam):
    other = make_user(client, "teacher2")
    sid = client.post("/api/quality/benchmarks", json={"name": "Private"}, headers=teacher).json()["id"]
    client.post(f"/api/quality/benchmarks/{sid}/items", json={
        "question_prompt": "p", "max_points": 1, "answer_text": "a", "human_marks": [1], "consent": True},
        headers=teacher)

    from app.quality import graders

    async def zero(item):
        return {"points": 0}
    graders.register_grader("zero", zero, needs_ai=False)
    try:
        run = client.post(f"/api/quality/benchmarks/{sid}/runs", json={"grader": "zero"}, headers=teacher).json()
        _wait_run(client, teacher, run["id"])
    finally:
        graders.unregister_grader("zero")
    assert client.get("/api/quality/benchmarks", headers=other).json() == []
    for method, url in [("get", f"/api/quality/benchmarks/{sid}"), ("get", f"/api/quality/benchmarks/{sid}/runs"),
                        ("get", f"/api/quality/runs/{run['id']}"), ("delete", f"/api/quality/benchmarks/{sid}"),
                        ("get", f"/api/exams/{exam['id']}/quality"),
                        ("get", f"/api/exams/{exam['id']}/quality/routing"),
                        ("get", f"/api/exams/{exam['id']}/quality/graded")]:
        assert getattr(client, method)(url, headers=other).status_code == 404, url
    r = client.post(f"/api/quality/benchmarks/{sid}/items", json={
        "question_prompt": "p", "max_points": 1, "answer_text": "a", "human_marks": [1], "consent": True},
        headers=other)
    assert r.status_code == 404
    assert client.get("/api/quality/second-marking/queue", headers=other).json()["items"] == []
    alice = login(client, "alice")
    assert client.get("/api/quality/benchmarks", headers=alice).status_code == 403
    assert client.get("/api/quality/second-marking/queue", headers=alice).status_code == 403
    teachers = client.get("/api/quality/second-marking/teachers", headers=teacher).json()
    assert [t["username"] for t in teachers] == ["teacher2"]
