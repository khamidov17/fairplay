"""Phase 2 (g): student feedback shows approved explanation + step marks; class struggles
panel; misconception tags reach the course insights."""
import json
import time

import httpx
import pytest

from app import ai

from .test_scan import PAPER_EXAM, _attempt, _scan_class, _wait


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


def _fake(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    text = "".join(c.get("text", "") for c in body["messages"][1]["content"] if c["type"] == "text")
    if "integrate x e^x" in text:
        out = {"transcript": "x e^x - e^x", "steps": [{"text": "u = x", "status": "correct"},
                                                       {"text": "x e^x - e^x", "status": "correct"},
                                                       {"text": "", "status": "missing", "note": "+C"}],
               "points": 3, "explanation": "Add the constant of integration.", "confidence": 0.9,
               "misconception": {"tag": "missing step", "text": "+C"}}
    else:
        out = {"transcript": "something", "points": 0, "confidence": 0.9, "explanation": "Check the definition.",
               "steps": [{"text": "something", "status": "error"}],
               "misconception": {"tag": "conceptual misunderstanding", "text": ""}}
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})


def test_feedback_steps_struggles_and_insights(client, teacher, alice, monkeypatch):
    e = client.post("/api/exams", json=PAPER_EXAM, headers=teacher).json()
    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice", "bob"]}, headers=teacher)
    q = [x["id"] for x in e["questions"]]
    _scan_class(client, teacher, e)
    _wait(client, teacher, e["id"])
    client.put(f"/api/exams/{e['id']}/answer-key", json={"subject": "math"}, headers=teacher)
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    ai.set_transport(httpx.MockTransport(_fake))
    assert client.post(f"/api/exams/{e['id']}/scan/ai-grade", json={}, headers=teacher).status_code == 202
    end = time.time() + 30
    while client.get(f"/api/exams/{e['id']}/scan/ai-grade", headers=teacher).json()["running"] and time.time() < end:
        time.sleep(0.1)

    # before any approval: struggles count AI tags as suggestions only
    s = client.get(f"/api/exams/{e['id']}/scan/struggles", headers=teacher).json()
    sq = {x["question_id"]: x for x in s["questions"]}
    assert sq[q[3]]["graded"] == 0 and sq[q[3]]["avg_percent"] is None
    tag = sq[q[3]]["misconceptions"][0]
    assert tag["tag"] == "conceptual misunderstanding" and tag["count"] == 2 and tag["approved"] == 0

    al = _attempt(client, teacher, e["id"], "alice")
    bo = _attempt(client, teacher, e["id"], "bob")
    # alice q4: accept (missing step); bob q4 is blank; q3 for both: accept the AI (0 pts, conceptual)
    for aid, qid, body in [(al["attempt_id"], q[4], {"grading_seconds": 8}),
                           (al["attempt_id"], q[3], {"grading_seconds": 5}),
                           (bo["attempt_id"], q[3], {"points": 1, "comment": "Some idea.", "grading_seconds": 20})]:
        r = client.post(f"/api/exams/{e['id']}/scan/answers/{aid}/{qid}/approve", json=body, headers=teacher)
        assert r.status_code == 200, r.text

    s = client.get(f"/api/exams/{e['id']}/scan/struggles", headers=teacher).json()
    sq = {x["question_id"]: x for x in s["questions"]}
    assert sq[q[3]]["graded"] == 2 and sq[q[3]]["avg_points"] == 0.5 and sq[q[3]]["avg_percent"] == 10.0
    tag = sq[q[3]]["misconceptions"][0]
    assert tag == {"tag": "conceptual misunderstanding", "count": 2, "approved": 2,
                   "students": [{"username": "alice", "name": "Alice Chen"}, {"username": "bob", "name": "Bob Karimov"}]}
    assert sq[q[4]]["misconceptions"][0]["tag"] == "missing step"
    assert sq[q[0]]["graded"] == 1 and sq[q[0]]["avg_percent"] == 100.0  # alice's bubble; bob's double mark pending
    assert s["time"] == {"graded": 3, "seconds": 33.0, "estimate_manual_seconds": 360}

    # course insights pick the approved tags up
    ins = client.get("/api/insights", params={"course": "MATH 201"}, headers=teacher).json()
    labels = [m["label"] for m in ins["mistakes"]]
    assert "missing step" in labels and "conceptual misunderstanding" in labels

    # release -> alice sees the approved explanation and step marks (and never unapproved drafts)
    client.post(f"/api/exams/{e['id']}/release", json={"force": True}, headers=teacher)
    fb = client.get(f"/api/student/grades/{e['id']}", headers=alice).json()
    fq = {x["question_id"]: x for x in fb["questions"]}
    assert fq[q[4]]["comment"] == "Add the constant of integration."
    assert [st["status"] for st in fq[q[4]]["steps"]] == ["correct", "correct", "missing"]
    assert fq[q[4]]["first_error_step"] == 3 and fq[q[4]]["image"].startswith("/api/uploads/")
    assert "steps" not in fq[q[1]]  # numeric answer not approved: no draft leaks
    assert "steps" not in fq[q[0]]
