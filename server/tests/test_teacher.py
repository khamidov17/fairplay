import copy

from .conftest import SAMPLE_EXAM


def test_create_get_list_edit(client, teacher):
    r = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher)
    assert r.status_code == 201
    e = r.json()
    assert e["id"].startswith("e_") and e["status"] == "draft" and e["start_code"] is None
    assert e["question_count"] == 3 and e["total_points"] == 10
    assert e["created_at"].endswith("Z")
    q0, q1, q2 = e["questions"]
    assert all(q["id"].startswith("q_") for q in e["questions"])
    assert q0["correct_index"] == 1 and q0["answer_number"] is None and q0["accepted_answers"] == []
    assert q1["options"] == [] and q1["correct_index"] is None and q1["tolerance"] == 0.01
    assert q2["accepted_answers"] == ["Paris", "  Paris  City "] and q2["tolerance"] is None

    assert client.get(f"/api/exams/{e['id']}", headers=teacher).json()["id"] == e["id"]
    assert client.get("/api/exams/e_missing", headers=teacher).status_code == 404

    body = copy.deepcopy(SAMPLE_EXAM)
    body["title"] = "Midterm v2"
    body["questions"][0]["id"] = q0["id"]  # existing ids are preserved
    r = client.put(f"/api/exams/{e['id']}", json=body, headers=teacher)
    assert r.status_code == 200
    assert r.json()["title"] == "Midterm v2" and r.json()["questions"][0]["id"] == q0["id"]

    e2 = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    ids = [x["id"] for x in client.get("/api/exams", headers=teacher).json()]
    assert ids[:2] == [e2["id"], e["id"]]  # newest first


def test_validation(client, teacher):
    def bad(mutate):
        body = copy.deepcopy(SAMPLE_EXAM)
        mutate(body)
        r = client.post("/api/exams", json=body, headers=teacher)
        assert r.status_code == 422, (body, r.text)

    bad(lambda b: b.update(title="   "))
    bad(lambda b: b.update(duration_min=0))
    bad(lambda b: b.update(questions=[]))
    bad(lambda b: b["questions"][0].update(options=["only one"]))
    bad(lambda b: b["questions"][0].update(options=[str(i) for i in range(9)]))
    bad(lambda b: b["questions"][0].update(correct_index=7))
    bad(lambda b: b["questions"][0].update(correct_index=None))
    bad(lambda b: b["questions"][1].update(answer_number=None))
    bad(lambda b: b["questions"][1].update(tolerance=-1))
    bad(lambda b: b["questions"][2].update(accepted_answers=[]))
    bad(lambda b: b["questions"][2].update(type="truefalse"))
    bad(lambda b: b["questions"][2].update(prompt=""))


def test_edit_only_draft_and_delete_rules(client, teacher):
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    opened = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    assert opened["status"] == "live"
    code = opened["start_code"]
    assert isinstance(code, str) and len(code) == 6 and code.isdigit()
    r = client.put(f"/api/exams/{e['id']}", json=SAMPLE_EXAM, headers=teacher)
    assert r.status_code == 409 and r.json() == {"detail": "Exam is not a draft"}
    assert client.delete(f"/api/exams/{e['id']}", headers=teacher).status_code == 409
    closed = client.post(f"/api/exams/{e['id']}/close", headers=teacher).json()
    assert closed["status"] == "closed" and closed["start_code"] is None
    reopened = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    assert reopened["status"] == "live" and len(reopened["start_code"]) == 6
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    assert client.delete(f"/api/exams/{e['id']}", headers=teacher).status_code == 204
    assert client.get(f"/api/exams/{e['id']}", headers=teacher).status_code == 404
    d = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    assert client.delete(f"/api/exams/{d['id']}", headers=teacher).status_code == 204
