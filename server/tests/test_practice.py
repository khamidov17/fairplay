"""Slice 6 H: practice assignments — weak topics, verified variations, draft → approve → send,
student answering with server-side checks, no effect on grades, AI fake transport + AI off."""
import json
import re

import httpx
import pytest

from app import ai, mathcheck

from .conftest import login

PRACTICE_EXAM = {
    "title": "Unit 3 quiz",
    "duration_min": 30,
    "course": "MATH 101",
    "questions": [
        {"type": "numeric", "prompt": "A rectangle is 3 cm by 4 cm. What is its area in cm²?", "points": 2,
         "answer_number": 12, "tolerance": 0, "topics": ["Area"]},
        {"type": "numeric", "prompt": "A garden is 5 m by 6 m. What is its area in m²?", "points": 2,
         "answer_number": 30, "tolerance": 0, "topics": ["area"]},
        {"type": "numeric", "prompt": "What is 12 × 7 + 3?", "points": 1, "answer_number": 87,
         "topics": ["arithmetic"]},
        {"type": "numeric", "prompt": "What is 15 + 9 - 4?", "points": 1, "answer_number": 20,
         "topics": ["arithmetic"]},
        {"type": "numeric", "prompt": "A shop sells pens at 3 for 2 dollars. How much do 12 pens cost?",
         "points": 1, "answer_number": 8, "topics": ["arithmetic"]},
        {"type": "mcq", "prompt": "Which number is prime?", "points": 1, "options": ["4", "6", "7", "9"],
         "correct_index": 2, "topics": ["primes"]},
        {"type": "mcq", "prompt": "Which number is NOT prime?", "points": 1, "options": ["2", "3", "15"],
         "correct_index": 2, "topics": ["primes"]},
        {"type": "short", "prompt": "Simplify 2(x+1).", "points": 1, "accepted_answers": ["2x+2"],
         "topics": ["algebra"]},
        {"type": "short", "prompt": "Capital of France?", "points": 1, "accepted_answers": ["Paris"],
         "topics": ["geography"]},
        {"type": "short", "prompt": "Capital of Italy?", "points": 1, "accepted_answers": ["Rome"],
         "topics": ["geography"]},
    ],
}
ALICE = {0: 10, 1: 11, 2: 1, 3: 1, 5: 0, 6: 0, 7: "x", 8: "Paris", 9: "Rome"}   # weak: area, arithmetic, primes
BOB = {0: 12, 1: 30, 2: 87, 3: 20, 4: 8, 5: 2, 6: 2, 7: "2x+3", 8: "Paris", 9: "Rome"}  # only 1 algebra answer


@pytest.fixture(autouse=True)
def _ai_off(monkeypatch):
    monkeypatch.delenv("FAIRPLAY_AI_PROVIDER", raising=False)
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


def setup_graded(client, teacher):
    e = client.post("/api/exams", json=PRACTICE_EXAM, headers=teacher)
    assert e.status_code == 201, e.text
    e = client.post(f"/api/exams/{e.json()['id']}/open", headers=teacher).json()
    qids = [q["id"] for q in e["questions"]]
    for user, ans in (("alice", ALICE), ("bob", BOB)):
        h = login(client, user)
        a = client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]}, headers=h)
        assert a.status_code == 200, a.text
        r = client.post(f"/api/student/attempts/{a.json()['attempt_id']}/submit",
                        json={"answers": {qids[i]: v for i, v in ans.items()}, "client_seq": 1}, headers=h)
        assert r.status_code == 200, r.text
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    return e


def nums_in(text):
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]


def generate(client, teacher, **body):
    r = client.post("/api/practice/generate", json=body, headers=teacher)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- weak topics
def test_students_weak_topics(client, teacher):
    setup_graded(client, teacher)
    r = client.get("/api/practice/students", headers=teacher)
    assert r.status_code == 200, r.text
    by = {s["username"]: s for s in r.json()["students"]}
    weak = {w["topic"] for w in by["alice"]["weak"]}
    assert weak == {"area", "arithmetic", "primes"}
    assert by["alice"]["weak_is_fallback"] is False
    assert all(w["avg_percent"] < 60 and w["n"] >= 2 for w in by["alice"]["weak"])
    # Bob has no topic < 60 % with >= 2 answers: fall back to his lowest topics.
    assert by["bob"]["weak_is_fallback"] is True
    assert by["bob"]["weak"][0]["topic"] == "algebra" and len(by["bob"]["weak"]) == 2
    assert by["carol"]["weak"] == [] and by["carol"]["topics"] == []
    topics = {t["topic"] for t in r.json()["topics"]}
    assert {"area", "arithmetic", "primes", "algebra", "geography"} <= topics


def test_teacher_only(client, teacher, alice):
    assert client.get("/api/practice/students", headers=alice).status_code == 403
    assert client.post("/api/practice/generate", json={"username": "alice"}, headers=alice).status_code == 403
    assert client.get("/api/student/practice", headers=teacher).status_code == 403


# ---------------------------------------------------------------- generation (AI off)
def test_generate_from_weak_topics_verified(client, teacher):
    setup_graded(client, teacher)
    out = generate(client, teacher, usernames=["alice", "carol", "nobody"], count=8)
    skipped = {s["username"]: s["reason"] for s in out["skipped"]}
    assert "carol" in skipped and "nobody" in skipped
    assert len(out["sets"]) == 1
    s = out["sets"][0]
    assert s["username"] == "alice" and s["status"] == "draft" and s["formative"] is True
    assert set(s["topics"]) == {"area", "arithmetic", "primes"}
    assert 1 <= len(s["items"]) <= 8
    for it in s["items"]:
        assert set(it["topics"]) & {"area", "arithmetic", "primes"}
        if it["type"] == "numeric" and it["kind"] == "perturbed":
            assert it["status"] == "verified"
            assert it["verification"]["method"] == "sympy"
            n = nums_in(it["prompt"])
            if "area" in it["topics"]:
                assert it["answer_number"] == pytest.approx(n[0] * n[1])
            elif "×" in it["prompt"]:
                assert it["answer_number"] == pytest.approx(n[0] * n[1] + n[2])
            else:
                assert it["answer_number"] == pytest.approx(n[0] + n[1] - n[2])
            assert it["prompt"] not in [q["prompt"] for q in PRACTICE_EXAM["questions"]]
        if it["type"] == "mcq":
            assert it["options"][it["correct_index"]] in ("7", "15")
    assert any(it["kind"] == "perturbed" for it in s["items"])
    assert s["needs_answer"] == 0


def test_generate_with_explicit_topics_and_ai_off_fallback(client, teacher):
    setup_graded(client, teacher)
    s = generate(client, teacher, username="bob", topics=["Algebra", "geography"], count=5)["sets"][0]
    assert set(s["topics"]) == {"algebra", "geography"}
    kinds = {it["source"]["question_id"]: it for it in s["items"]}
    assert len(kinds) == 3  # one reused original per short question (no AI to rewrite them)
    for it in s["items"]:
        assert it["kind"] == "original" and it["status"] == "ready"
    alg = next(it for it in s["items"] if "algebra" in it["topics"])
    assert alg["math"] is True and alg["accepted_answers"] == ["2x+2"]


def test_template_ambiguity_is_not_trusted():
    from app.practice import generator
    # 2 and 2: sum == product; perturbed numbers would disagree -> no verified variation.
    nums = generator.find_numbers("What is 2 and 2 combined?")
    cands = [c for c in generator._template_candidates(2) if generator._matches(c, nums, 4.0, 0)]
    assert len(cands) > 1


def test_find_numbers_skips_exponents_and_words():
    from app.practice import generator
    found = [n.text for n in generator.find_numbers("Find x^2 when x = 3, for H2O and the 3rd item, -4 too")]
    assert found == ["3", "-4"]


# ---------------------------------------------------------------- full flow
def test_draft_approve_send_and_student_answers(client, teacher, alice, bob):
    setup_graded(client, teacher)
    gb_before = client.get("/api/gradebook", headers=teacher).json()
    grades_before = client.get("/api/student/grades", headers=alice).json()
    s = generate(client, teacher, username="alice", count=4)["sets"][0]
    sid = s["id"]

    # Students never see drafts.
    assert client.get("/api/student/practice", headers=alice).json() == []
    assert client.get(f"/api/student/practice/{sid}", headers=alice).status_code == 404
    assert client.post(f"/api/practice/sets/{sid}/send", headers=teacher).status_code == 409

    # Edit, remove, add from the bank.
    first = s["items"][0]
    r = client.put(f"/api/practice/sets/{sid}/items/{first['id']}", json={"explanation": "Think it through."},
                   headers=teacher)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["explanation"] == "Think it through."
    victim = s["items"][-1]["id"]
    r = client.delete(f"/api/practice/sets/{sid}/items/{victim}", headers=teacher)
    assert r.status_code == 200 and victim not in [i["id"] for i in r.json()["items"]]
    bank = client.get("/api/practice/bank?topic=geography", headers=teacher).json()
    assert len(bank) == 2
    r = client.post(f"/api/practice/sets/{sid}/items",
                    json={"exam_id": bank[0]["exam_id"], "question_id": bank[0]["question_id"]}, headers=teacher)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[-1]["kind"] == "bank" and items[-1]["accepted_answers"]

    # Approve & send.
    r = client.post(f"/api/practice/sets/{sid}/approve", headers=teacher)
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    r = client.post(f"/api/practice/sets/{sid}/send", headers=teacher)
    assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["sent_at"]
    assert client.put(f"/api/practice/sets/{sid}/items/{first['id']}", json={"prompt": "x"},
                      headers=teacher).status_code == 409

    # Student side.
    lst = client.get("/api/student/practice", headers=alice).json()
    assert len(lst) == 1 and lst[0]["formative"] is True and lst[0]["item_count"] == len(items)
    assert client.get("/api/student/practice", headers=bob).json() == []
    assert client.get(f"/api/student/practice/{sid}", headers=bob).status_code == 404
    view = client.get(f"/api/student/practice/{sid}", headers=alice).json()
    for it in view["items"]:
        for secret in ("correct_index", "answer_number", "accepted_answers", "tolerance", "explanation",
                       "verification", "source"):
            assert secret not in it
        assert it["result"] is None
    full = {it["id"]: it for it in items}

    def right(it):
        if it["type"] == "mcq":
            return it["correct_index"]
        if it["type"] == "numeric":
            return str(it["answer_number"])
        return it["accepted_answers"][0]

    def wrong(it):
        if it["type"] == "mcq":
            return (it["correct_index"] + 1) % len(it["options"])
        if it["type"] == "numeric":
            return float(it["answer_number"]) + 1000
        return "definitely wrong"

    ids = [it["id"] for it in view["items"]]
    url = f"/api/student/practice/{sid}/items/{{}}/answer"
    # wrong once -> no answer revealed yet; wrong twice -> answer revealed
    r = client.post(url.format(ids[0]), json={"answer": wrong(full[ids[0]])}, headers=alice).json()
    assert r["correct"] is False and r["correct_answer"] is None and r["tries"] == 1
    r = client.post(url.format(ids[0]), json={"answer": wrong(full[ids[0]])}, headers=alice).json()
    assert r["correct"] is False and r["correct_answer"] is not None and "The answer is" in r["feedback"]
    r = client.post(url.format(ids[1]), json={"answer": right(full[ids[1]])}, headers=alice).json()
    assert r["correct"] is True and r["progress"]["correct"] == 1 and r["progress"]["answered"] == 2
    # unreadable numeric input doesn't use up a try
    num = next((i for i in ids[2:] if full[i]["type"] == "numeric"), None)
    if num:
        r = client.post(url.format(num), json={"answer": "__import__('os')"}, headers=alice).json()
        assert r["readable"] is False and r["correct"] is False
        # equivalent forms are accepted: e.g. "12" as "24/2"
        v = full[num]["answer_number"]
        r = client.post(url.format(num), json={"answer": f"{v * 2}/2"}, headers=alice).json()
        assert r["correct"] is True, r

    view = client.get(f"/api/student/practice/{sid}", headers=alice).json()
    assert view["items"][0]["result"]["tries"] == 2
    lst = client.get("/api/student/practice", headers=alice).json()
    assert lst[0]["answered"] >= 2

    # Teacher sees progress.
    sets = client.get("/api/practice/sets", headers=teacher).json()
    assert sets[0]["progress"]["answered"] >= 2 and sets[0]["username"] == "alice"
    one = client.get(f"/api/practice/sets/{sid}", headers=teacher).json()
    assert one["items"][1]["student_result"]["correct"] is True

    # Nothing in grades changed.
    assert client.get("/api/gradebook", headers=teacher).json() == gb_before
    assert client.get("/api/student/grades", headers=alice).json() == grades_before


def test_short_math_answer_checked_with_sympy(client, teacher, bob):
    setup_graded(client, teacher)
    s = generate(client, teacher, username="bob", topics=["algebra"], count=1)["sets"][0]
    sid = s["id"]
    client.post(f"/api/practice/sets/{sid}/approve", headers=teacher)
    client.post(f"/api/practice/sets/{sid}/send", headers=teacher)
    iid = s["items"][0]["id"]
    r = client.post(f"/api/student/practice/{sid}/items/{iid}/answer", json={"answer": "2(x+1)"}, headers=bob)
    assert r.json()["correct"] is True
    r = client.post(f"/api/student/practice/{sid}/items/{iid}/answer", json={"answer": "2x+1"}, headers=bob)
    assert r.json()["correct"] is False


def test_generate_requires_data_or_topics(client, teacher):
    setup_graded(client, teacher)
    out = generate(client, teacher, username="carol", count=3)
    assert out["sets"] == [] and out["skipped"][0]["username"] == "carol"
    out = generate(client, teacher, username="carol", topics=["area"], count=3)
    assert len(out["sets"]) == 1
    assert client.post("/api/practice/generate", json={"count": 3}, headers=teacher).status_code == 422
    assert client.post("/api/practice/generate", json={"username": "alice", "count": 50},
                       headers=teacher).status_code == 422


# ---------------------------------------------------------------- AI (fake transport)
def _fake_ai(monkeypatch, responder):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "anthropic")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "test-key")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"][-1]["text"]
        calls.append(prompt)
        data = responder(prompt)
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(data)}]})

    ai.set_transport(httpx.MockTransport(handler))
    return calls


def test_ai_variations_are_verified_and_wrong_ones_dropped(client, teacher, monkeypatch):
    setup_graded(client, teacher)

    def responder(prompt):
        if "rectangle" in prompt or "garden" in prompt:
            return {"formula": "n1 + n2", "template": "A field is {n1} by {n2}. Area?", "explanation": "Add."}
        if "12 × 7 + 3" in prompt:
            return {"formula": "n1*n2 + n3",
                    "template": "A box has {n1} rows of {n2} eggs and {n3} loose eggs. How many eggs?",
                    "explanation": "Multiply rows by eggs per row, then add the loose eggs."}
        if "pens" in prompt:  # no formula: SymPy cannot check it, and no template formula fits
            return {"template": "Pencils cost {n2} dollars for {n1}. How much do {n3} pencils cost?"}
        if "Which number is prime" in prompt:
            return {"prompt": "Pick the prime.", "options": ["8", "11", "12"], "correct_index": 1}
        if "NOT prime" in prompt:
            return {"prompt": "Which one is composite?", "options": ["5", "15", "13"], "correct_index": 1,
                    "explanation": "15 = 3 × 5."}
        return {}

    calls = _fake_ai(monkeypatch, responder)
    out = generate(client, teacher, username="alice", count=12)
    assert calls, "AI was not called"
    s = out["sets"][0]
    dropped = " ".join(d["reason"] for d in out["sets"][0]["dropped"])
    assert "AI formula n1 + n2" in dropped              # wrong area formula rejected by SymPy
    assert "changed the correct option" in dropped      # "Pick the prime" lost the original "7"
    for it in s["items"]:
        if it["type"] == "numeric" and it["status"] == "verified":
            n = nums_in(it["prompt"])
            if "area" in it["topics"]:
                assert it["answer_number"] == pytest.approx(n[0] * n[1])     # template search, not AI
                assert it["verification"]["how"] == "template"
            elif "eggs" in it["prompt"]:
                assert it["answer_number"] == pytest.approx(n[0] * n[1] + n[2])
                assert it["verification"]["how"] == "ai_formula"
                assert "Multiply rows" in it["explanation"]
        if it["type"] == "mcq":
            assert it["options"][it["correct_index"]] in ("7", "15")
    assert any("eggs" in it["prompt"] for it in s["items"])
    composite = [it for it in s["items"] if it["prompt"] == "Which one is composite?"]
    assert composite and composite[0]["kind"] == "ai_rewrite"
    # The pens rewrite came without a formula -> needs the teacher's answer
    sam = [it for it in s["items"] if it["prompt"].startswith("Pencils cost")]
    assert sam and sam[0]["status"] == "needs_answer" and sam[0]["answer_number"] is None
    r = client.post(f"/api/practice/sets/{s['id']}/approve", headers=teacher)
    assert r.status_code == 409 and "need" in r.json()["detail"]
    n = nums_in(sam[0]["prompt"])
    r = client.put(f"/api/practice/sets/{s['id']}/items/{sam[0]['id']}",
                   json={"answer_number": n[2] * n[0] / n[1]}, headers=teacher)
    assert r.status_code == 200
    fixed = next(i for i in r.json()["items"] if i["id"] == sam[0]["id"])
    assert fixed["status"] == "ready" and fixed["verification"] == {"method": "teacher"}
    while True:  # every other "needs_answer" item (if any) gets removed
        cur = client.get(f"/api/practice/sets/{s['id']}", headers=teacher).json()
        todo = [i for i in cur["items"] if i["status"] == "needs_answer"]
        if not todo:
            break
        client.delete(f"/api/practice/sets/{s['id']}/items/{todo[0]['id']}", headers=teacher)
    assert client.post(f"/api/practice/sets/{s['id']}/approve", headers=teacher).status_code == 200


def test_ai_short_math_check(client, teacher, monkeypatch):
    setup_graded(client, teacher)
    replies = {"r": {"prompt": "Simplify 3(x+2).", "answer": "3x+5", "check": "expand(3(x+2))"}}
    _fake_ai(monkeypatch, lambda prompt: replies["r"])
    out = generate(client, teacher, username="bob", topics=["algebra"], count=1)
    s = out["sets"][0]
    # Wrong AI answer is dropped; the teacher's original question is used instead.
    assert "does not match" in " ".join(d["reason"] for d in s["dropped"])
    assert s["items"][0]["kind"] == "original" and s["items"][0]["prompt"] == "Simplify 2(x+1)."

    replies["r"] = {"prompt": "Simplify 3(x+2).", "answer": "3x+6", "check": "expand(3(x+2))",
                    "explanation": "Distribute the 3."}
    s = generate(client, teacher, username="bob", topics=["algebra"], count=1)["sets"][0]
    it = s["items"][0]
    assert it["status"] == "verified" and it["accepted_answers"] == ["3x+6"] and it["math"] is True

    replies["r"] = {"prompt": "Simplify 4(x+1).", "answer": "4x+4"}   # no check -> teacher must confirm
    s = generate(client, teacher, username="bob", topics=["algebra"], count=1)["sets"][0]
    it = s["items"][0]
    assert it["status"] == "needs_answer" and it["suggested_answer"] == "4x+4"
    r = client.put(f"/api/practice/sets/{s['id']}/items/{it['id']}", json={"accepted_answers": ["4x+"]},
                   headers=teacher)
    assert r.status_code == 422
    r = client.put(f"/api/practice/sets/{s['id']}/items/{it['id']}", json={"accepted_answers": ["4x+4"]},
                   headers=teacher)
    assert r.status_code == 200 and r.json()["items"][0]["status"] == "ready"


def test_ai_failure_falls_back(client, teacher, monkeypatch):
    setup_graded(client, teacher)
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "anthropic")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "test-key")
    ai.set_transport(httpx.MockTransport(lambda req: httpx.Response(500, json={"error": "down"})))
    s = generate(client, teacher, username="alice", count=4)["sets"][0]
    assert s["items"] and all(it["status"] in ("verified", "ready") for it in s["items"])


def test_mathcheck_used_for_verification_is_consistent():
    # The verified answer must equal the formula value for the shown numbers.
    assert mathcheck.evaluate_formula("(n1)*(n2)+(n3)", {"n1": "9", "n2": "8", "n3": "2"}) == 74
