"""Phase 2 (b): step-level AI suggestions, record shapes, approve provenance, no-overwrite,
bounded concurrency and retries. AI calls go through a fake httpx transport."""
import asyncio
import json
import time

import httpx
import pytest

from app import ai
from app.scan import ai_grade

from .test_scan import PAPER_EXAM, _attempt, _scan_class, _wait  # noqa: F401  (fixtures below)

Q = {"id": "q1", "type": "essay", "prompt": "Integrate x e^x.", "points": 4}
CRIT = [{"id": "c1", "label": "Setup", "points": 1, "description": ""},
        {"id": "c2", "label": "Integration", "points": 2, "description": ""},
        {"id": "c3", "label": "Constant", "points": 1, "description": ""}]
CTX = {"subject": "math", "criteria": CRIT, "solution": "x e^x - e^x + C"}


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


# ---------------------------------------------------------------- parsing (unit)
def test_parse_good_json_with_steps_and_criteria():
    out = {"legibility": "clear", "transcript": "u=x ...",
           "steps": [{"text": "u = x, dv = e^x", "status": "correct", "note": ""},
                     {"text": "x e^x + ∫ e^x dx", "status": "wrong", "note": "sign"},
                     {"text": "+ C", "status": "omitted"}],
           "first_error_step": 2,
           "criteria": [{"id": "c1", "points": 1}, {"id": "c2", "points": 1, "note": "sign error"},
                        {"id": "c3", "points": 0}],
           "points": 2, "explanation": "Check the sign when you integrate by parts.",
           "teacher_note": "Sign slip in step 2.", "misconception": {"tag": "Sign Error", "text": "minus lost"},
           "confidence": 0.9}
    d = ai_grade.parse_suggestion(out, Q, CTX)
    assert d["points"] == 2 and d["max_points"] == 4 and d["model_points"] == 2
    assert [s["status"] for s in d["steps"]] == ["correct", "error", "missing"]
    assert [s["n"] for s in d["steps"]] == [1, 2, 3]
    assert d["first_error_step"] == 2
    assert [(c["id"], c["points"], c["max_points"], c["met"]) for c in d["criteria"]] == [
        ("c1", 1, 1, True), ("c2", 1, 2, False), ("c3", 0, 1, False)]
    assert d["explanation"] == d["comment"] == "Check the sign when you integrate by parts."
    assert d["misconception"] == {"tag": "sign error", "text": "minus lost"}
    assert d["mistake_tags"] == ["sign error"]
    assert d["confidence"] == 0.9 and d["low_confidence"] is False
    assert d["needs_review"] is False and d["review_reasons"] == [] and d["legibility"] == "clear"
    assert d["source"] == "ai" and d["prompt_version"] == ai_grade.PROMPT_VERSION
    for k in ("provider", "model", "created_at", "teacher_note"):
        assert k in d


@pytest.mark.parametrize("junk", [None, [], "text", {"points": "lots"}, {"steps": "nope", "criteria": 7}])
def test_parse_junk_never_raises(junk):
    d = ai_grade.parse_suggestion(junk, Q, CTX)
    assert d["points"] == 0 and d["needs_review"] is True
    assert "missing_criteria" in d["review_reasons"] and "no_steps" in d["review_reasons"]
    assert all(c["points"] == 0 for c in d["criteria"])


def test_parse_out_of_range_and_missing_criteria():
    # no rubric: model total is clamped and flagged
    d = ai_grade.parse_suggestion({"points": 9, "transcript": "x", "confidence": 0.95}, Q, {"subject": "math"})
    assert d["points"] == 4 and "points_out_of_range" in d["review_reasons"] and d["needs_review"]
    d = ai_grade.parse_suggestion({"points": -3, "transcript": "x", "confidence": 0.95}, Q, {})
    assert d["points"] == 0 and "points_out_of_range" in d["review_reasons"]
    # criteria: one over its max (clamped), one missing, label-matched one
    out = {"criteria": [{"id": "c2", "points": 5}, {"label": "constant", "points": 1}], "points": 3,
           "transcript": "x", "confidence": 0.9}
    d = ai_grade.parse_suggestion(out, Q, CTX)
    assert [c["points"] for c in d["criteria"]] == [0, 2, 1]
    assert d["points"] == 3 and "missing_criteria" in d["review_reasons"]
    assert d["criteria"][0]["note"] == "Not assessed by AI"
    # total disagreeing with criteria: criteria win, flagged
    out = {"criteria": [{"id": "c1", "points": 1}, {"id": "c2", "points": 2}, {"id": "c3", "points": 1}],
           "points": 2, "transcript": "x", "confidence": 0.9}
    d = ai_grade.parse_suggestion(out, Q, CTX)
    assert d["points"] == 4 and d["review_reasons"] == ["total_mismatch"]


def test_parse_legibility_confidence_and_tags():
    d = ai_grade.parse_suggestion({"legibility": "illegible", "points": 1, "confidence": 0.95, "transcript": "?"}, Q, {})
    assert "illegible" in d["review_reasons"] and d["confidence"] <= 0.4 and d["low_confidence"]
    assert "Handwriting is hard to read" in d["review_reason"]
    d = ai_grade.parse_suggestion({"points": 1, "confidence": 85, "transcript": "x"}, Q, {})
    assert d["confidence"] == 0.85
    d = ai_grade.parse_suggestion({"points": 1, "confidence": "high", "transcript": "x"}, Q, {})
    assert d["confidence"] == 0.5 and d["low_confidence"]
    d = ai_grade.parse_suggestion({"points": 1, "transcript": "x", "misconception": {"tag": "forgot chain rule"}},
                                  Q, {"subject": "math"})
    assert d["misconception"] == {"tag": "other", "text": "forgot chain rule"}
    assert d["mistake_tags"] == ["forgot chain rule"]
    d = ai_grade.parse_suggestion({"points": 4, "transcript": "x", "misconception": {"tag": "none"}}, Q, {})
    assert d["misconception"] is None
    # steps: strings and bad first_error_step
    d = ai_grade.parse_suggestion({"steps": ["a", {"text": "b", "correct": False}, {"status": "x"}],
                                   "first_error_step": 1, "points": 1}, Q, {})
    assert [s["status"] for s in d["steps"]] == ["correct", "error"]
    assert d["first_error_step"] == 2 and d["transcript"] == "a\nb"
    # numeric answer that matches the key -> full marks from the key
    qn = {"id": "qn", "type": "numeric", "prompt": "pi", "points": 3, "answer_number": 3.14, "tolerance": 0.01}
    d = ai_grade.parse_suggestion({"transcript": "pi = 3.14", "points": 1, "confidence": 0.5}, qn, {})
    assert d["source"] == "key" and d["points"] == 3 and not d["needs_review"]


def test_suggest_retries_then_falls_back(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    calls = []

    def flaky(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"points": 2, "transcript": "x", "confidence": 0.8})}}]})

    ai.set_transport(httpx.MockTransport(flaky))
    d = asyncio.run(ai_grade.suggest(Q, {}, [b"\x89PNG\r\n\x1a\n"]))
    assert len(calls) == 2 and d["source"] == "ai" and d["points"] == 2
    ai.set_transport(httpx.MockTransport(lambda r: httpx.Response(500, json={})))
    calls.clear()
    d = asyncio.run(ai_grade.suggest(Q, {}, [b"img"]))
    assert d["source"] == "fallback" and d["review_reasons"] == ["ai_failed"] and d["needs_review"]
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    d = asyncio.run(ai_grade.suggest(Q, {}, [b"img"]))
    assert d["source"] == "fallback" and d["review_reasons"] == ["ai_off"]


# ---------------------------------------------------------------- end to end on a scanned class
@pytest.fixture
def paper(client, teacher):
    r = client.post("/api/exams", json=PAPER_EXAM, headers=teacher)
    assert r.status_code == 201, r.text
    e = r.json()
    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice", "bob"]}, headers=teacher)
    return e


STEP_OUT = {
    "legibility": "clear", "transcript": "x e^x - e^x + C",
    "steps": [{"text": "u = x", "status": "correct"}, {"text": "x e^x - e^x", "status": "correct"},
              {"text": "+C", "status": "missing", "note": "constant"}],
    "criteria": [{"id": "c1", "points": 1}, {"id": "c2", "points": 2}, {"id": "c3", "points": 0}],
    "points": 3, "explanation": "Nice work — remember the constant of integration.",
    "teacher_note": "Missing +C", "misconception": {"tag": "missing step", "text": "+C"}, "confidence": 0.92,
}


def _step_ai(stats: dict):
    async def handler(request: httpx.Request) -> httpx.Response:
        stats["inflight"] += 1
        stats["max"] = max(stats["max"], stats["inflight"])
        stats["calls"] += 1
        body = json.loads(request.content)
        text = "".join(c.get("text", "") for c in body["messages"][1]["content"] if c["type"] == "text")
        stats["prompts"].append(text)
        await asyncio.sleep(0.03)
        stats["inflight"] -= 1
        if "integrate x e^x" in text:
            out = STEP_OUT
        else:
            out = {"transcript": "something", "points": 1, "confidence": 0.5, "explanation": "Partly right.",
                   "steps": [{"text": "something", "status": "error"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})
    return httpx.MockTransport(handler)


def _run_ai(client, teacher, eid, **body):
    r = client.post(f"/api/exams/{eid}/scan/ai-grade", json=body, headers=teacher)
    assert r.status_code == 202, r.text
    end = time.time() + 60
    while time.time() < end:
        st = client.get(f"/api/exams/{eid}/scan/ai-grade", headers=teacher).json()
        if not st["running"]:
            return r.json(), st
        time.sleep(0.1)
    raise AssertionError("AI run did not finish")


def test_ai_run_records_approve_provenance_and_no_overwrite(client, teacher, paper, monkeypatch):
    e = paper
    q = [x["id"] for x in e["questions"]]
    _scan_class(client, teacher, e)
    _wait(client, teacher, e["id"])
    # rubric for the "show your work" essay (q4, 4 points)
    r = client.put(f"/api/exams/{e['id']}/answer-key/questions/{q[4]}", headers=teacher, json={
        "solution": "x e^x - e^x + C", "criteria": [{"id": "c1", "label": "Setup", "points": 1},
                                                    {"id": "c2", "label": "Integration", "points": 2},
                                                    {"id": "c3", "label": "Constant", "points": 1}]})
    assert r.status_code == 200, r.text
    client.put(f"/api/exams/{e['id']}/answer-key", json={"subject": "math"}, headers=teacher)

    # AI off -> 409
    assert client.post(f"/api/exams/{e['id']}/scan/ai-grade", json={}, headers=teacher).status_code == 409

    stats = {"inflight": 0, "max": 0, "calls": 0, "prompts": []}
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    monkeypatch.setenv("FAIRPLAY_AI_MODEL", "fake-vision-1")
    monkeypatch.setenv("FAIRPLAY_AI_CONCURRENCY", "2")
    ai.set_transport(_step_ai(stats))

    # the teacher grades alice's essay q3 by hand BEFORE the AI run: never overwritten
    al = _attempt(client, teacher, e["id"], "alice")
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[3]}/approve",
                    json={"points": 5, "comment": "Great", "grading_seconds": 42.5}, headers=teacher)
    assert r.status_code == 200
    manual = r.json()["grade"]
    assert manual["source"] == "manual" and manual["ai_suggested_points"] is None
    assert manual["graded_by"] == "teacher" and manual["grading_seconds"] == 42.5

    started, st = _run_ai(client, teacher, e["id"])
    # pending written answers with ink: alice q1,q2,q4 + bob q1,q3 (blank boxes use rules)
    assert started["queued"] == 5 and st["done"] == 5 and st["total"] == 5
    assert stats["calls"] == 5
    assert 1 < stats["max"] <= 2  # bounded concurrency
    assert any("Rubric criteria" in p and "id c2: Integration (2 pts)" in p for p in stats["prompts"])
    assert any("Allowed misconception tags" in p and "sign error" in p for p in stats["prompts"])

    queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    qq = {x["question_id"]: x for x in queue["questions"]}
    assert [c["id"] for c in qq[q[4]]["criteria"]] == ["c1", "c2", "c3"]
    assert qq[q[4]]["solution"] == "x e^x - e^x + C"
    items = {(i["username"], i["question_id"]): i for i in queue["items"]}
    d = items[("alice", q[4])]["draft"]
    assert d["points"] == 3 and d["max_points"] == 4 and d["source"] == "ai"
    assert [s["status"] for s in d["steps"]] == ["correct", "correct", "missing"] and d["first_error_step"] == 3
    assert [c["points"] for c in d["criteria"]] == [1, 2, 0]
    assert d["misconception"] == {"tag": "missing step", "text": "+C"}
    assert d["provider"] == "openai" and d["model"] == "fake-vision-1"
    assert d["prompt_version"] == ai_grade.PROMPT_VERSION and d["needs_review"] is False
    assert d["image"] == items[("alice", q[4])]["image"]
    assert items[("alice", q[3])]["grade"]["points"] == 5  # untouched
    assert items[("alice", q[3])]["draft"]["source"] == "fallback"  # old placeholder, not replaced
    low = items[("bob", q[3])]["draft"]
    assert low["needs_review"] and "low_confidence" in low["review_reasons"]

    # accept as-is -> ai_accepted
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[4]}/approve",
                    json={"grading_seconds": 6}, headers=teacher)
    assert r.status_code == 200, r.text
    g = r.json()["grade"]
    assert g["source"] == "ai_accepted" and g["ai_suggested_points"] == 3 and g["points"] == 3
    assert g["criteria"] == [{"id": "c1", "label": "Setup", "points": 1, "max_points": 1},
                             {"id": "c2", "label": "Integration", "points": 2, "max_points": 2},
                             {"id": "c3", "label": "Constant", "points": 0, "max_points": 1}]
    assert g["steps"][2]["status"] == "missing" and g["comment"].startswith("Nice work")
    assert g["misconception"]["tag"] == "missing step" and g["mistake_tags"] == ["missing step"]
    assert g["rubric_scores"] is None  # key criteria are not the essay rubric
    assert g["ai_model"] == "fake-vision-1" and g["ai_prompt_version"] == ai_grade.PROMPT_VERSION
    assert g["grading_seconds"] == 6 and g["feedback_edited"] is False

    # bob q4 is blank (rule draft). Edit alice q1 numeric via a changed points -> ai_edited
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[1]}/approve",
                    json={"points": 0, "comment": "Close", "grading_seconds": 12}, headers=teacher)
    g = r.json()["grade"]
    assert g["source"] == "ai_edited" and g["ai_suggested_points"] == 1 and g["feedback_edited"] is True
    # same points, only the feedback changed -> still ai_accepted (mark unchanged)
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[2]}/approve",
                    json={"points": 1, "comment": "Rewritten"}, headers=teacher)
    g = r.json()["grade"]
    assert g["source"] == "ai_accepted" and g["feedback_edited"] is True
    # criteria edits recompute points; must add up; unknown ids rejected
    bo = _attempt(client, teacher, e["id"], "bob")
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{bo['attempt_id']}/{q[1]}/approve",
                    json={"criteria": [{"id": "c1", "points": 1}]}, headers=teacher)
    assert r.status_code == 422  # numeric question has no criteria
    # re-approve alice q4: criteria edit recomputes the total, a flipped step, misconception cleared
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[4]}/approve", headers=teacher,
                    json={"criteria": [{"id": "c1", "points": 1}, {"id": "c2", "points": 2}, {"id": "c3", "points": 1}],
                          "steps": [{"n": 3, "status": "correct"}], "misconception": {"tag": ""}})
    assert r.status_code == 200, r.text
    g = r.json()["grade"]
    assert g["points"] == 4 and g["source"] == "ai_edited" and g["ai_suggested_points"] == 3
    assert g["steps"][2] == {"n": 3, "text": "+C", "status": "correct", "note": "constant", "teacher_flipped": True}
    assert g["first_error_step"] is None and g["misconception"] is None and g["mistake_tags"] == []
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[4]}/approve", headers=teacher,
                    json={"criteria": [{"id": "c1", "points": 1}], "points": 3})
    assert r.status_code == 422 and "sum of the criteria" in r.json()["detail"]
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[4]}/approve", headers=teacher,
                    json={"criteria": [{"id": "zz", "points": 1}]})
    assert r.status_code == 422
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[4]}/approve", headers=teacher,
                    json={"criteria": [{"id": "c2", "points": 3}]})
    assert r.status_code == 422
    r = client.post(f"/api/exams/{e['id']}/scan/answers/{al['attempt_id']}/{q[4]}/approve", headers=teacher,
                    json={"steps": [{"n": 9, "status": "error"}]})
    assert r.status_code == 422

    # redo run: graded answers are skipped, their grades and drafts stay
    before = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    graded = {(i["attempt_id"], i["question_id"]): i for i in before["items"] if i["grade"]}
    stats["calls"] = 0
    started, _ = _run_ai(client, teacher, e["id"], redo=True)
    after = {(i["attempt_id"], i["question_id"]): i
             for i in client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()["items"]}
    for k, it in graded.items():
        assert after[k]["grade"] == it["grade"] and after[k]["draft"] == it["draft"]
    assert started["queued"] == 5 and stats["calls"] == 2  # bob only (alice is fully graded); rules need no AI


def test_bulk_approve_records_teacher(client, teacher, paper, monkeypatch):
    e = paper
    q = [x["id"] for x in e["questions"]]
    _scan_class(client, teacher, e)
    _wait(client, teacher, e["id"])
    queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    blank = [{"attempt_id": i["attempt_id"], "question_id": i["question_id"]}
             for i in queue["items"] if i["draft"] and i["draft"]["source"] == "blank"]
    assert blank
    r = client.post(f"/api/exams/{e['id']}/scan/approve-bulk", json={"items": blank}, headers=teacher)
    assert r.json()["approved"] == len(blank)
    queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    g = next(i["grade"] for i in queue["items"] if i["question_id"] == q[2] and i["username"] == "bob")
    assert g["source"] == "ai_accepted" and g["suggestion_source"] == "blank" and g["graded_by"] == "teacher"
    assert g["grading_seconds"] is None and g["points"] == 0
