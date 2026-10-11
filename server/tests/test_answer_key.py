"""Phase 2 (a): answer key + marking rubric per exam, AI-drafted rubric."""
import json

import httpx
import pymupdf
import pytest

from app import ai
from app.scan import answer_key as ak

EXAM = {
    "title": "Calc quiz", "duration_min": 30, "course": "MATH 101", "delivery": "paper",
    "questions": [
        {"type": "essay", "prompt": "Integrate x e^x.", "points": 4, "topics": ["integration"],
         "rubric": [{"label": "Parts set up", "points": 2}, {"label": "Final answer", "points": 2}]},
        {"type": "numeric", "prompt": "Derivative of x^2 at 3?", "points": 2, "answer_number": 6,
         "tolerance": 0, "topics": ["derivatives"]},
    ],
}


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


@pytest.fixture
def exam(client, teacher):
    r = client.post("/api/exams", json=EXAM, headers=teacher)
    assert r.status_code == 201, r.text
    return r.json()


def test_answer_key_crud_and_validation(client, teacher, exam, bob):
    eid = exam["id"]
    q0, q1 = [q["id"] for q in exam["questions"]]
    r = client.get(f"/api/exams/{eid}/answer-key", headers=teacher)
    assert r.status_code == 200, r.text
    k = r.json()
    assert k["subject"] == "general" and k["text"] == "" and k["files"] == []
    assert k["ai_enabled"] is False
    # essay rubric is the default grading criteria
    kq0 = k["questions"][0]["key"]
    assert kq0["criteria_source"] == "question"
    assert [c["id"] for c in kq0["effective_criteria"]] == ["r1", "r2"]
    assert k["questions"][1]["correct_answer"] == "6"

    r = client.put(f"/api/exams/{eid}/answer-key", json={"subject": "math", "text": "Q1: x e^x - e^x + C"},
                   headers=teacher)
    assert r.status_code == 200 and r.json()["subject"] == "math"
    assert "sign error" in r.json()["misconceptions"]
    assert client.put(f"/api/exams/{eid}/answer-key", json={"subject": "astrology"}, headers=teacher).status_code == 422

    body = {"solution": "u = x, dv = e^x dx ...", "partial_credit": "Half credit for correct setup",
            "criteria": [{"label": "Chooses u and dv", "points": 1}, {"label": "Integrates", "points": 2},
                         {"label": "Constant", "points": 1, "description": "+C"}],
            "common_mistakes": [{"label": "Forgets +C", "tag": "Missing Step", "deduction": 1}]}
    r = client.put(f"/api/exams/{eid}/answer-key/questions/{q0}", json=body, headers=teacher)
    assert r.status_code == 200, r.text
    got = r.json()["key"]
    assert [c["id"] for c in got["criteria"]] == ["c1", "c2", "c3"]
    assert got["criteria_source"] == "key" and got["updated_by"] == "teacher"
    assert got["common_mistakes"][0] == {"label": "Forgets +C", "tag": "missing step", "deduction": 1}
    # points must add up to the question's points
    bad = dict(body, criteria=[{"label": "All", "points": 3}])
    r = client.put(f"/api/exams/{eid}/answer-key/questions/{q0}", json=bad, headers=teacher)
    assert r.status_code == 422 and "add up to 4" in r.json()["detail"]
    r = client.put(f"/api/exams/{eid}/answer-key/questions/{q0}", json=dict(body, criteria=[{"label": "", "points": 4}]),
                   headers=teacher)
    assert r.status_code == 422
    # empty criteria allowed (falls back to the question rubric / none)
    r = client.put(f"/api/exams/{eid}/answer-key/questions/{q1}", json={"solution": "2x -> 6"}, headers=teacher)
    assert r.status_code == 200 and r.json()["key"]["criteria_source"] == "none"
    assert client.put(f"/api/exams/{eid}/answer-key/questions/nope", json={}, headers=teacher).status_code == 404
    # persisted
    k = client.get(f"/api/exams/{eid}/answer-key", headers=teacher).json()
    assert k["questions"][0]["key"]["solution"].startswith("u = x")
    # not the teacher's exam / students
    assert client.get(f"/api/exams/{eid}/answer-key", headers=bob).status_code == 403
    assert client.get("/api/exams/e_missing/answer-key", headers=teacher).status_code == 404


def test_answer_key_files(client, teacher, exam):
    eid = exam["id"]
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Answer 1: x e^x - e^x + C")
    pdf = doc.tobytes()
    r = client.post(f"/api/exams/{eid}/answer-key/files", headers=teacher,
                    files={"file": ("key.pdf", pdf, "application/pdf")})
    assert r.status_code == 201, r.text
    k = r.json()
    assert len(k["files"]) == 1 and k["files"][0]["kind"] == "pdf"
    assert k["files"][0]["pages"][0].startswith("/api/uploads/")
    assert "x e^x - e^x + C" in k["text"]
    r = client.post(f"/api/exams/{eid}/answer-key/files", headers=teacher,
                    files={"file": ("key.txt", b"Q2: 6", "text/plain")})
    assert r.status_code == 201 and "Q2: 6" in r.json()["text"]
    r = client.post(f"/api/exams/{eid}/answer-key/files", headers=teacher,
                    files={"file": ("x.bin", b"\x00\x01garbage", "application/octet-stream")})
    assert r.status_code == 422
    fid = k["files"][0]["id"]
    r = client.delete(f"/api/exams/{eid}/answer-key/files/{fid}", headers=teacher)
    assert r.status_code == 200 and [f["kind"] for f in r.json()["files"]] == ["text"]
    assert client.delete(f"/api/exams/{eid}/answer-key/files/{fid}", headers=teacher).status_code == 404


def test_ai_drafts_rubric(client, teacher, exam, monkeypatch):
    eid = exam["id"]
    assert client.post(f"/api/exams/{eid}/answer-key/ai-draft", json={}, headers=teacher).status_code == 409
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        out = {"solution": "Use parts.", "partial_credit": "half for setup",
               "criteria": [{"label": "Setup", "points": 3}, {"label": "Answer", "points": 3}],  # adds to 6, not 4
               "common_mistakes": [{"label": "sign", "tag": "sign error", "deduction": 1}, "forgot C"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})

    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    ai.set_transport(httpx.MockTransport(handler))
    q0 = exam["questions"][0]["id"]
    r = client.post(f"/api/exams/{eid}/answer-key/ai-draft", json={"question_ids": [q0]}, headers=teacher)
    assert r.status_code == 200, r.text
    assert r.json()["drafted"] == 1 and len(calls) == 1
    sug = r.json()["questions"][0]["ai_suggestion"]
    assert sug["solution"] == "Use parts." and sug["prompt_version"] == ak.KEY_PROMPT_VERSION
    assert sum(c["points"] for c in sug["criteria"]) == 4  # rescaled to the question's points
    assert [m["label"] for m in sug["common_mistakes"]] == ["sign", "forgot C"]
    # a draft is never applied
    assert r.json()["questions"][0]["key"]["criteria"] == []
    assert r.json()["questions"][1]["ai_suggestion"] is None


def test_clean_criteria_unit():
    assert ak.clean_criteria([], 3) == []
    with pytest.raises(ValueError):
        ak.clean_criteria([{"label": "a", "points": -1}], 3)
    with pytest.raises(ValueError):
        ak.clean_criteria([{"label": "a", "points": 1}], 3)
    out = ak.clean_criteria([{"id": "x", "label": " a  b ", "points": 1.5}, {"id": "x", "label": "c", "points": 1.5}], 3)
    assert out[0]["id"] == "x" and out[1]["id"] != "x" and out[0]["label"] == "a b"
