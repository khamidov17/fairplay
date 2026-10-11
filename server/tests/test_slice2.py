"""Slice 2: exam fields, essays, teacher grading, release, gradebook, student grades,
extend, flags/review, per-student shuffle and ownership."""
import copy
import csv
import hashlib
import io
from datetime import timedelta

import pytest

from app.timeutil import parse_iso

from .conftest import SAMPLE_EXAM, login

ESSAY_EXAM = {
    "title": "Final",
    "duration_min": 30,
    "course": "MATH 201",
    "room": "AB 1087",
    "scheduled_at": "2026-10-12T09:00:00Z",
    "questions": [
        {"type": "mcq", "prompt": "2+2?", "points": 2, "options": ["3", "4"], "correct_index": 1,
         "topics": ["arithmetic"]},
        {"type": "numeric", "prompt": "pi?", "points": 3, "answer_number": 3.14, "tolerance": 0.01,
         "topics": ["constants"]},
        {"type": "short", "prompt": "Capital of France?", "points": 5, "accepted_answers": ["Paris"],
         "topics": ["geography"]},
        {"type": "essay", "prompt": "Why?", "points": 10, "topics": ["writing"],
         "rubric": [{"label": "Two clear reasons", "points": 6}, {"label": "Grammar", "points": 4}],
         "min_words": 5, "max_words": 500},
    ],
}  # total 20


# ---------------------------------------------------------------- helpers
def make_exam(client, teacher, body=ESSAY_EXAM, open_=True):
    e = client.post("/api/exams", json=body, headers=teacher)
    assert e.status_code == 201, e.text
    e = e.json()
    if open_:
        e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    return e


def start(client, h, exam):
    r = client.post(f"/api/student/exams/{exam['id']}/start",
                    json={"start_code": exam["start_code"]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def submit(client, h, exam, by_index):
    """Start and submit; by_index maps ORIGINAL question index -> value."""
    a = start(client, h, exam)
    qids = [q["id"] for q in exam["questions"]]
    answers = {qids[i]: v for i, v in by_index.items()}
    r = client.post(f"/api/student/attempts/{a['attempt_id']}/submit",
                    json={"answers": answers, "client_seq": 1}, headers=h)
    assert r.status_code == 200, r.text
    return a


def close(client, teacher, exam):
    r = client.post(f"/api/exams/{exam['id']}/close", headers=teacher)
    assert r.status_code == 200
    return r.json()


def put_grade(client, teacher, exam, aid, qid, **body):
    return client.put(f"/api/exams/{exam['id']}/attempts/{aid}/grades/{qid}", json=body,
                      headers=teacher)


def make_user(client, username, role, name=None):
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


@pytest.fixture
def carol(client):
    return login(client, "carol")


@pytest.fixture
def graded_setup(client, teacher, alice, bob, carol):
    """Closed essay exam: alice mixed answers, bob blank essay; carol never started."""
    e = make_exam(client, teacher)
    qids = [q["id"] for q in e["questions"]]
    a = submit(client, alice, e, {0: 1, 1: 3.0, 2: "London",
                                  3: "Because one, and also because two reasons."})
    b = submit(client, bob, e, {0: 0, 3: "   "})
    close(client, teacher, e)
    return {"exam": e, "qids": qids, "alice": a["attempt_id"], "bob": b["attempt_id"]}


# ---------------------------------------------------------------- exam fields
def test_exam_new_fields_create_put_list(client, teacher):
    e = make_exam(client, teacher, open_=False)
    assert e["course"] == "MATH 201" and e["room"] == "AB 1087"
    assert e["scheduled_at"] == "2026-10-12T09:00:00.000Z"
    assert e["shuffle_questions"] is False and e["grades_released"] is False
    assert e["released_at"] is None and e["closed_at"] is None and e["pending_grading"] == 0
    assert e["total_points"] == 20

    body = copy.deepcopy(ESSAY_EXAM)
    for f in ("course", "room", "scheduled_at"):
        body.pop(f)
    body["title"] = "Final v2"
    r = client.put(f"/api/exams/{e['id']}", json=body, headers=teacher).json()
    assert r["title"] == "Final v2" and r["course"] == "MATH 201"  # omitted -> kept
    assert r["scheduled_at"] == "2026-10-12T09:00:00.000Z"
    body.update(course="  CS 101 ", room="", scheduled_at=None, shuffle_questions=True)
    r = client.put(f"/api/exams/{e['id']}", json=body, headers=teacher).json()
    assert r["course"] == "CS 101" and r["room"] == "" and r["scheduled_at"] is None
    assert r["shuffle_questions"] is True

    listed = client.get("/api/exams", headers=teacher).json()[0]
    for f in ("course", "room", "scheduled_at", "shuffle_questions", "grades_released",
              "released_at", "closed_at", "pending_grading"):
        assert f in listed
    plain = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    assert plain["course"] == "" and plain["room"] == "" and plain["scheduled_at"] is None

    for bad in ({"course": "x" * 65}, {"room": "y" * 65}, {"scheduled_at": "not a date"}):
        r = client.post("/api/exams", json={**ESSAY_EXAM, **bad}, headers=teacher)
        assert r.status_code == 422, bad


def test_closed_at_set_on_close_and_cleared_on_reopen(client, teacher):
    e = make_exam(client, teacher)
    c = close(client, teacher, e)
    assert c["closed_at"].endswith("Z") and c["status"] == "closed"
    r = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    assert r["closed_at"] is None


# ---------------------------------------------------------------- essay questions
def test_essay_validation(client, teacher):
    def post(mutate):
        body = copy.deepcopy(ESSAY_EXAM)
        mutate(body["questions"][3])
        return client.post("/api/exams", json=body, headers=teacher)

    assert post(lambda q: q.update(rubric=[{"label": "a", "points": 5}])).status_code == 422
    assert post(lambda q: q.update(rubric=[{"label": "a", "points": 6},
                                           {"label": "b", "points": 5}])).status_code == 422
    assert post(lambda q: q.update(min_words=600, max_words=500)).status_code == 422
    assert post(lambda q: q.update(min_words=0)).status_code == 422
    assert post(lambda q: q.update(rubric=[{"label": " ", "points": 10}])).status_code == 422
    assert post(lambda q: q.update(rubric=[{"label": "a", "points": -1},
                                           {"label": "b", "points": 11}])).status_code == 422
    # empty rubric is fine; fractional rubric points summing exactly are fine
    r = post(lambda q: q.update(rubric=[], min_words=None, max_words=None))
    assert r.status_code == 201
    essay = r.json()["questions"][3]
    assert essay["rubric"] == [] and essay["min_words"] is None and essay["max_words"] is None
    r = post(lambda q: q.update(rubric=[{"label": "a", "points": 2.5},
                                        {"label": "b", "points": 7.5}]))
    assert r.status_code == 201
    assert r.json()["questions"][3]["rubric"] == [{"label": "a", "points": 2.5},
                                                  {"label": "b", "points": 7.5}]
    # type-specific fields are normalised
    r = post(lambda q: q.update(options=["x", "y"], correct_index=0, accepted_answers=["z"],
                                answer_number=1))
    q = r.json()["questions"][3]
    assert q["options"] == [] and q["correct_index"] is None and q["accepted_answers"] == []
    assert q["answer_number"] is None and q["tolerance"] is None
    assert q["rubric"] == [{"label": "Two clear reasons", "points": 6},
                           {"label": "Grammar", "points": 4}]
    assert q["min_words"] == 5 and q["max_words"] == 500
    body = copy.deepcopy(ESSAY_EXAM)
    body["questions"][0].update(rubric=[{"label": "a", "points": 2}], min_words=3)
    mcq = client.post("/api/exams", json=body, headers=teacher).json()["questions"][0]
    assert mcq["rubric"] == [] and mcq["min_words"] is None and mcq["max_words"] is None


def test_essay_student_view_and_long_answers(client, teacher, alice):
    e = make_exam(client, teacher)
    a = start(client, alice, e)
    assert a["course"] == "MATH 201"
    essay = next(q for q in a["questions"] if q["type"] == "essay")
    assert essay["rubric"] == [{"label": "Two clear reasons", "points": 6},
                               {"label": "Grammar", "points": 4}]
    assert essay["min_words"] == 5 and essay["max_words"] == 500
    for q in a["questions"]:
        for secret in ("correct_index", "answer_number", "tolerance", "accepted_answers"):
            assert secret not in q
    url = f"/api/student/attempts/{a['attempt_id']}/answers"
    r = client.put(url, json={"answers": {essay["id"]: "w " * 25_000}, "client_seq": 1},
                   headers=alice)
    assert r.status_code == 200 and r.json()["saved"] is True
    r = client.put(url, json={"answers": {essay["id"]: "x" * 50_001}, "client_seq": 2},
                   headers=alice)
    assert r.status_code == 422


# ---------------------------------------------------------------- grading model
def test_results_pending_and_auto(client, teacher, graded_setup):
    e, qids = graded_setup["exam"], graded_setup["qids"]
    res = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    assert res["course"] == "MATH 201" and res["grades_released"] is False
    assert res["released_at"] is None
    assert res["pending_grading"] == 1 and res["submitted_count"] == 2 and res["attempt_count"] == 2
    by = {x["username"]: x for x in res["attempts"]}
    al, bo = by["alice"], by["bob"]
    assert al["needs_grading"] == 1 and bo["needs_grading"] == 0
    assert al["overall_comment"] == "" and al["flag_count"] == 0
    assert al["score"] == 2 and al["percent"] == 10.0  # mcq only (3.0 is outside tolerance)
    ess = al["answers"][3]
    assert ess["pending"] is True and ess["correct"] is False and ess["points_awarded"] == 0
    assert ess["max_points"] == 10 and ess["manual"] is False and ess["rubric_scores"] is None
    blank = bo["answers"][3]
    assert blank["pending"] is False and blank["given"] is None and blank["points_awarded"] == 0
    assert [r["correct"] for r in al["answers"]] == [True, False, False, False]
    qstats = res["questions"]
    assert [q["points"] for q in qstats] == [2, 3, 5, 10]
    assert qstats[0]["percent_correct"] == 50.0 and qstats[0]["avg_points"] == 1
    # essay: only bob's blank answer is graded (0 of 10)
    assert qstats[3]["percent_correct"] == 0.0 and qstats[3]["avg_points"] == 0
    assert client.get(f"/api/exams/{e['id']}", headers=teacher).json()["pending_grading"] == 1
    assert client.get("/api/exams", headers=teacher).json()[0]["pending_grading"] == 1


def test_results_aggregates_buckets_median(client, teacher, alice, bob, carol):
    e = make_exam(client, teacher, SAMPLE_EXAM)  # 10 points, no essay
    submit(client, alice, e, {0: 1, 1: 3.14, 2: "Paris"})  # 100
    submit(client, bob, e, {0: 1})                          # 20
    submit(client, carol, e, {0: 1, 1: 3.14})               # 50
    res = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    b = res["score_buckets"]
    assert len(b) == 10 and b[0]["label"] == "0–9" and b[-1]["label"] == "90–100"
    assert b[1]["label"] == "10–19"
    assert {x["label"]: x["count"] for x in b if x["count"]} == {
        "20–29": 1, "50–59": 1, "90–100": 1}
    assert res["median_percent"] == 50.0 and res["average_percent"] == round(170 / 3, 1)
    assert res["questions"][2]["percent_correct"] == round(100 / 3, 1)
    assert res["questions"][1]["avg_points"] == 2


def test_grading_queue(client, teacher, graded_setup):
    e, qids = graded_setup["exam"], graded_setup["qids"]
    g = client.get(f"/api/exams/{e['id']}/grading", headers=teacher).json()
    assert g["exam_id"] == e["id"] and g["title"] == "Final" and g["course"] == "MATH 201"
    assert g["status"] == "closed" and g["grades_released"] is False
    assert [(q["index"], q["type"], q["to_grade"], q["graded"]) for q in g["questions"]] == [
        (0, "mcq", 0, 0), (1, "numeric", 0, 0), (2, "short", 1, 0), (3, "essay", 1, 0)]
    assert g["questions"][3]["rubric"][0] == {"label": "Two clear reasons", "points": 6}
    items = g["items"]
    # short wrong (alice only: bob left it blank), then both essays ordered by started_at
    assert [(i["question_id"], i["username"]) for i in items] == [
        (qids[2], "alice"), (qids[3], "alice"), (qids[3], "bob")]
    short, ess_a, ess_b = items
    assert short["answer"] == "London" and short["auto_points"] == 0 and short["word_count"] is None
    assert ess_a["word_count"] == 7 and ess_a["auto_points"] is None and ess_a["grade"] is None
    assert ess_b["answer"] is None and ess_b["word_count"] == 0
    aid = graded_setup["alice"]
    assert ess_a["anon_id"] == "#" + hashlib.sha256(aid.encode()).hexdigest()[:4].upper()
    assert ess_a["name"] == "Alice Chen" and ess_a["flag_count"] == 0

    put_grade(client, teacher, e, aid, qids[3], points=8, comment="ok", rubric_scores=[5, 3])
    put_grade(client, teacher, e, aid, qids[2], points=2, comment="close enough")
    g = client.get(f"/api/exams/{e['id']}/grading", headers=teacher).json()
    assert [(q["to_grade"], q["graded"]) for q in g["questions"]] == [(0, 0), (0, 0), (0, 1), (0, 1)]
    assert g["items"][1]["grade"]["points"] == 8 and g["items"][1]["grade"]["rubric_scores"] == [5, 3]


def test_put_and_delete_grade(client, teacher, graded_setup):
    e, qids, aid = graded_setup["exam"], graded_setup["qids"], graded_setup["alice"]
    r = put_grade(client, teacher, e, aid, qids[3], points=8, comment="Good", rubric_scores=[5, 3])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attempt_id"] == aid and body["question_id"] == qids[3]
    assert body["grade"]["points"] == 8 and body["grade"]["comment"] == "Good"
    assert body["grade"]["rubric_scores"] == [5, 3] and body["grade"]["graded_at"].endswith("Z")
    assert body["score"] == 10 and body["total_points"] == 20 and body["percent"] == 50.0
    assert body["needs_grading"] == 0
    assert client.get(f"/api/exams/{e['id']}", headers=teacher).json()["pending_grading"] == 0

    # override an auto-graded question (any type)
    r = put_grade(client, teacher, e, aid, qids[2], points=5, comment="accepted")
    assert r.json()["score"] == 15
    res = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    al = next(x for x in res["attempts"] if x["username"] == "alice")
    short = al["answers"][2]
    assert short["manual"] is True and short["correct"] is True and short["points_awarded"] == 5
    assert short["comment"] == "accepted"
    ess = al["answers"][3]
    assert ess["pending"] is False and ess["manual"] and ess["rubric_scores"] == [5, 3]
    assert ess["correct"] is False and ess["points_awarded"] == 8
    assert res["questions"][3]["percent_correct"] == 40.0  # (8 + 0) / 2 / 10

    r = client.delete(f"/api/exams/{e['id']}/attempts/{aid}/grades/{qids[2]}", headers=teacher)
    assert r.status_code == 200
    assert r.json()["grade"] is None and r.json()["score"] == 10
    r = client.delete(f"/api/exams/{e['id']}/attempts/{aid}/grades/{qids[3]}", headers=teacher)
    assert r.json()["needs_grading"] == 1 and r.json()["score"] == 2

    # comment
    r = client.put(f"/api/exams/{e['id']}/attempts/{aid}/comment",
                   json={"comment": "Nice work"}, headers=teacher)
    assert r.json() == {"attempt_id": aid, "overall_comment": "Nice work"}
    r = client.put(f"/api/exams/{e['id']}/attempts/{aid}/comment",
                   json={"comment": "x" * 2001}, headers=teacher)
    assert r.status_code == 422


def test_grade_validation(client, teacher, alice, graded_setup):
    e, qids, aid = graded_setup["exam"], graded_setup["qids"], graded_setup["alice"]
    ess = qids[3]
    assert put_grade(client, teacher, e, aid, ess, points=11).status_code == 422
    assert put_grade(client, teacher, e, aid, ess, points=-1).status_code == 422
    assert put_grade(client, teacher, e, aid, qids[0], points=3).status_code == 422
    assert put_grade(client, teacher, e, aid, ess, points=5, rubric_scores=[5]).status_code == 422
    assert put_grade(client, teacher, e, aid, ess, points=7,
                     rubric_scores=[7, 0]).status_code == 422  # criterion max 6
    assert put_grade(client, teacher, e, aid, ess, points=7,
                     rubric_scores=[5, 1]).status_code == 422  # sum mismatch
    assert put_grade(client, teacher, e, aid, ess, points=7,
                     rubric_scores=[-1, 8]).status_code == 422
    # rubric_scores on a question without a rubric must be [] (length 0)
    assert put_grade(client, teacher, e, aid, qids[0], points=1,
                     rubric_scores=[1]).status_code == 422
    assert put_grade(client, teacher, e, aid, ess, points=6.5, comment="",
                     rubric_scores=[4.5, 2]).status_code == 200
    assert put_grade(client, teacher, e, aid, "q_nope", points=1).status_code == 404
    assert put_grade(client, teacher, e, "a_nope", ess, points=1).status_code == 404
    assert client.delete(f"/api/exams/{e['id']}/attempts/{aid}/grades/q_nope",
                         headers=teacher).status_code == 404
    # attempt of another exam is not found through this exam
    other = make_exam(client, teacher, SAMPLE_EXAM)
    oa = start(client, alice, other)
    r = put_grade(client, teacher, e, oa["attempt_id"], ess, points=1)
    assert r.status_code == 404
    # not submitted yet -> 409
    r = put_grade(client, teacher, other, oa["attempt_id"], other["questions"][0]["id"], points=1)
    assert r.status_code == 409


# ---------------------------------------------------------------- release
def test_release_rules(client, teacher, alice):
    e = make_exam(client, teacher)
    qids = [q["id"] for q in e["questions"]]
    a = submit(client, alice, e, {3: "Some essay words here please"})
    r = client.post(f"/api/exams/{e['id']}/release", json={"force": False}, headers=teacher)
    assert r.status_code == 409 and r.json() == {"detail": "Close the exam first"}
    close(client, teacher, e)
    r = client.post(f"/api/exams/{e['id']}/release", json={"force": False}, headers=teacher)
    assert r.status_code == 409 and r.json() == {"detail": "1 answers still need grading"}
    r = client.post(f"/api/exams/{e['id']}/release", headers=teacher)  # no body = not forced
    assert r.status_code == 409
    r = client.post(f"/api/exams/{e['id']}/release", json={"force": True}, headers=teacher)
    assert r.status_code == 200
    ex = r.json()
    assert ex["grades_released"] is True and ex["released_at"].endswith("Z")
    assert ex["pending_grading"] == 1
    r = client.post(f"/api/exams/{e['id']}/unrelease", headers=teacher)
    assert r.json()["grades_released"] is False and r.json()["released_at"] is None
    put_grade(client, teacher, e, a["attempt_id"], qids[3], points=10)
    r = client.post(f"/api/exams/{e['id']}/release", json={"force": False}, headers=teacher)
    assert r.status_code == 200 and r.json()["pending_grading"] == 0


def test_student_grades_before_and_after_release(client, teacher, alice, bob, carol, graded_setup):
    e, qids = graded_setup["exam"], graded_setup["qids"]
    lst = client.get("/api/student/grades", headers=alice).json()
    assert lst == [{"exam_id": e["id"], "title": "Final", "course": "MATH 201",
                    "submitted_at": lst[0]["submitted_at"], "released": False, "score": None,
                    "total_points": 20, "percent": None}]
    r = client.get(f"/api/student/grades/{e['id']}", headers=alice)
    assert r.status_code == 403 and r.json() == {"detail": "Grades not released yet"}
    assert client.get(f"/api/student/grades/{e['id']}", headers=carol).status_code == 404
    assert client.get("/api/student/grades/e_nope", headers=alice).status_code == 404
    assert client.get("/api/student/grades", headers=carol).json() == []
    item = client.get("/api/student/exams", headers=alice).json()[0]
    assert item["course"] == "MATH 201" and item["room"] == "AB 1087"
    assert item["scheduled_at"] == "2026-10-12T09:00:00.000Z"
    assert item["attempt"]["released"] is False and item["attempt"]["percent"] is None

    put_grade(client, teacher, e, graded_setup["alice"], qids[3], points=7, comment="Decent",
              rubric_scores=[4, 3])
    client.put(f"/api/exams/{e['id']}/attempts/{graded_setup['alice']}/comment",
               json={"comment": "Keep going"}, headers=teacher)
    assert client.post(f"/api/exams/{e['id']}/release", json={}, headers=teacher).status_code == 200

    lst = client.get("/api/student/grades", headers=alice).json()
    assert lst[0]["released"] is True and lst[0]["score"] == 9 and lst[0]["percent"] == 45.0
    item = client.get("/api/student/exams", headers=alice).json()[0]
    assert item["attempt"]["released"] is True and item["attempt"]["percent"] == 45.0

    d = client.get(f"/api/student/grades/{e['id']}", headers=alice).json()
    assert d["exam_id"] == e["id"] and d["title"] == "Final" and d["course"] == "MATH 201"
    assert d["score"] == 9 and d["total_points"] == 20 and d["percent"] == 45.0
    assert d["receipt"] and d["submitted_at"].endswith("Z")
    assert d["class_average_percent"] == 22.5  # alice 9, bob 0
    assert d["overall_comment"] == "Keep going"
    qs = d["questions"]
    assert [q["question_id"] for q in qs] == qids and [q["index"] for q in qs] == [0, 1, 2, 3]
    assert [q["status"] for q in qs] == ["correct", "wrong", "wrong", "partial"]
    assert qs[0]["correct_answer"] == 1 and qs[0]["options"] == ["3", "4"] and qs[0]["given"] == 1
    assert qs[1]["correct_answer"] == 3.14 and qs[1]["tolerance"] == 0.01 and qs[1]["given"] == 3.0
    assert qs[2]["correct_answer"] == "Paris" and qs[2]["tolerance"] is None
    assert qs[3]["correct_answer"] is None and qs[3]["points_awarded"] == 7
    assert qs[3]["comment"] == "Decent" and qs[3]["rubric_scores"] == [4, 3]
    assert qs[3]["rubric"][1] == {"label": "Grammar", "points": 4} and qs[3]["points"] == 10
    for q in qs:
        assert "accepted_answers" not in q and "answer_number" not in q

    bd = client.get(f"/api/student/grades/{e['id']}", headers=bob).json()
    assert [q["status"] for q in bd["questions"]] == ["wrong", "unanswered", "unanswered",
                                                      "unanswered"]
    assert bd["questions"][3]["rubric_scores"] is None and bd["overall_comment"] == ""

    client.post(f"/api/exams/{e['id']}/unrelease", headers=teacher)
    assert client.get(f"/api/student/grades/{e['id']}", headers=alice).status_code == 403
    assert client.get("/api/student/grades", headers=alice).json()[0]["score"] is None
    # role checks
    assert client.get("/api/student/grades", headers=teacher).status_code == 403


# ---------------------------------------------------------------- overviews
def test_grading_overview(client, teacher, alice, bob, graded_setup):
    make_exam(client, teacher, SAMPLE_EXAM)  # live, nobody submitted -> not listed
    e2 = make_exam(client, teacher, {**SAMPLE_EXAM, "title": "Quiz", "course": "CS 1"})
    submit(client, alice, e2, {0: 1})
    rows = client.get("/api/grading", headers=teacher).json()
    assert [r["exam_id"] for r in rows] == [e2["id"], graded_setup["exam"]["id"]]  # newest first
    assert rows[0] == {"exam_id": e2["id"], "title": "Quiz", "course": "CS 1", "status": "live",
                       "pending": 0, "submitted": 1, "grades_released": False}
    assert rows[1]["pending"] == 1 and rows[1]["submitted"] == 2 and rows[1]["status"] == "closed"
    assert client.get("/api/grading", headers=alice).status_code == 403


def test_gradebook_states_and_csv(client, teacher, alice, bob, carol, graded_setup):
    e1, qids = graded_setup["exam"], graded_setup["qids"]
    e2 = make_exam(client, teacher, {**SAMPLE_EXAM, "title": "Quiz, part 2", "course": "CS 1"})
    start(client, alice, e2)                     # in_progress
    submit(client, bob, e2, {0: 1, 1: 3.14})     # graded 50%
    make_exam(client, teacher, SAMPLE_EXAM, open_=False)  # draft: not in the gradebook

    gb = client.get("/api/gradebook", headers=teacher).json()
    assert gb["courses"] == ["CS 1", "MATH 201"]
    assert [x["exam_id"] for x in gb["exams"]] == [e1["id"], e2["id"]]  # oldest first
    assert gb["exams"][0] == {"exam_id": e1["id"], "title": "Final", "course": "MATH 201",
                              "total_points": 20, "status": "closed", "grades_released": False}
    st = {s["username"]: s for s in gb["students"]}
    assert [s["name"] for s in gb["students"]] == ["Alice Chen", "Bob Karimov", "Carol Wang"]
    assert st["alice"]["cells"][e1["id"]] == {"state": "needs_grading", "score": 2, "percent": 10.0}
    assert st["alice"]["cells"][e2["id"]] == {"state": "in_progress", "score": None,
                                             "percent": None}
    assert st["alice"]["average_percent"] is None
    assert st["bob"]["cells"][e1["id"]] == {"state": "graded", "score": 0, "percent": 0.0}
    assert st["bob"]["cells"][e2["id"]]["state"] == "graded"
    assert st["bob"]["average_percent"] == 25.0  # (0 + 50) / 2
    assert st["carol"]["cells"][e1["id"]]["state"] == "missing"
    assert st["carol"]["cells"][e2["id"]]["state"] == "not_taken"

    put_grade(client, teacher, e1, graded_setup["alice"], qids[3], points=10)
    gb = client.get("/api/gradebook?course=MATH%20201", headers=teacher).json()
    assert gb["courses"] == ["CS 1", "MATH 201"] and [x["exam_id"] for x in gb["exams"]] == [e1["id"]]
    alice_row = next(s for s in gb["students"] if s["username"] == "alice")
    assert alice_row["cells"][e1["id"]] == {"state": "graded", "score": 12, "percent": 60.0}
    assert alice_row["average_percent"] == 60.0 and list(alice_row["cells"]) == [e1["id"]]
    assert client.get("/api/gradebook?course=", headers=teacher).json()["exams"][1]["exam_id"] == e2["id"]

    r = client.get("/api/gradebook.csv", headers=teacher)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["Student", "Username", "Final", "Quiz, part 2", "Average %"]
    by = {row[1]: row for row in rows[1:]}
    assert by["alice"] == ["Alice Chen", "alice", "60.0", "in_progress", "60.0"]
    assert by["bob"] == ["Bob Karimov", "bob", "0.0", "50.0", "25.0"]
    assert by["carol"] == ["Carol Wang", "carol", "missing", "not_taken", ""]
    r = client.get("/api/gradebook.csv?course=CS%201", headers=teacher)
    assert next(csv.reader(io.StringIO(r.text))) == ["Student", "Username", "Quiz, part 2",
                                                     "Average %"]
    assert client.get("/api/gradebook.csv").status_code == 401  # not the SPA fallback
    assert client.get("/api/gradebook.csv", headers=alice).status_code == 403
    assert client.get("/api/gradebook", headers=alice).status_code == 403


# ---------------------------------------------------------------- live room
def test_extend_updates_deadlines(client, teacher, alice, bob):
    e = make_exam(client, teacher, SAMPLE_EXAM)
    a = start(client, alice, e)
    hb = client.post(f"/api/student/attempts/{a['attempt_id']}/heartbeat", json={},
                     headers=alice).json()  # caches attempt meta
    assert hb["deadline_at"] == a["deadline_at"]
    r = client.post(f"/api/exams/{e['id']}/extend", json={"minutes": 10}, headers=teacher)
    assert r.status_code == 200 and r.json()["duration_min"] == 40
    hb = client.post(f"/api/student/attempts/{a['attempt_id']}/heartbeat", json={},
                     headers=alice).json()
    assert parse_iso(hb["deadline_at"]) - parse_iso(a["deadline_at"]) == timedelta(minutes=10)
    g = client.get(f"/api/student/attempts/{a['attempt_id']}", headers=alice).json()
    assert g["deadline_at"] == hb["deadline_at"] and g["duration_min"] == 40
    live = client.get(f"/api/exams/{e['id']}/live", headers=teacher).json()
    assert live["duration_min"] == 40 and live["students"][0]["deadline_at"] == hb["deadline_at"]
    b = start(client, bob, e)  # new starts get the extended duration
    assert parse_iso(b["deadline_at"]) - parse_iso(b["started_at"]) == timedelta(minutes=40)
    for bad in (0, 121):
        assert client.post(f"/api/exams/{e['id']}/extend", json={"minutes": bad},
                           headers=teacher).status_code == 422
    close(client, teacher, e)
    r = client.post(f"/api/exams/{e['id']}/extend", json={"minutes": 5}, headers=teacher)
    assert r.status_code == 409
    d = make_exam(client, teacher, SAMPLE_EXAM, open_=False)
    assert client.post(f"/api/exams/{d['id']}/extend", json={"minutes": 5},
                       headers=teacher).status_code == 409


def test_flags_counts_and_review(client, teacher, alice, bob, carol):
    e = make_exam(client, teacher, SAMPLE_EXAM)
    a = start(client, alice, e)
    start(client, bob, e)                       # never heartbeats -> offline
    submit(client, carol, e, {0: 1})            # submitted
    aid = a["attempt_id"]
    client.post(f"/api/student/attempts/{aid}/heartbeat", json={"locked": True}, headers=alice)
    evs = [{"type": "lock_started", "detail": "", "client_time": "t"},
           {"type": "focus_lost", "detail": "", "client_time": "t"},
           {"type": "blocked_shortcut", "detail": "Cmd-key48", "client_time": "t"},
           {"type": "lock_ended", "detail": "", "client_time": "t"}]
    client.post(f"/api/student/attempts/{aid}/events", json={"events": evs}, headers=alice)

    live = client.get(f"/api/exams/{e['id']}/live", headers=teacher).json()
    flags = live["flags"]
    assert [f["type"] for f in flags] == ["blocked_shortcut", "focus_lost"]  # newest first
    f0 = flags[0]
    assert set(f0) == {"event_id", "attempt_id", "username", "name", "type", "detail",
                       "server_time"}
    assert f0["attempt_id"] == aid and f0["username"] == "alice" and f0["name"] == "Alice Chen"
    assert f0["detail"] == "Cmd-key48" and f0["server_time"].endswith("Z")
    st = {s["username"]: s for s in live["students"]}
    assert st["alice"]["needs_look"] is True and st["bob"]["needs_look"] is False
    assert live["counts"] == {"writing": 1, "submitted": 1, "offline": 1, "needs_look": 1}

    url = f"/api/exams/{e['id']}/events/{f0['event_id']}/review"
    r = client.post(url, json={"status": "noted", "note": "Talked to student"}, headers=teacher)
    assert r.json() == {"event_id": f0["event_id"], "review": "noted", "note": "Talked to student"}
    live = client.get(f"/api/exams/{e['id']}/live", headers=teacher).json()
    assert [f["type"] for f in live["flags"]] == ["focus_lost"]
    r = client.post(f"/api/exams/{e['id']}/events/{live['flags'][0]['event_id']}/review",
                    json={"status": "dismissed"}, headers=teacher)
    assert r.json()["review"] == "dismissed" and r.json()["note"] == ""
    live = client.get(f"/api/exams/{e['id']}/live", headers=teacher).json()
    assert live["flags"] == [] and live["counts"]["needs_look"] == 0
    assert all(s["needs_look"] is False for s in live["students"])

    assert client.post(url, json={"status": "open"}, headers=teacher).status_code == 422
    assert client.post(url, json={"status": "noted", "note": "x" * 501},
                       headers=teacher).status_code == 422
    other = make_exam(client, teacher, SAMPLE_EXAM)
    assert client.post(f"/api/exams/{other['id']}/events/{f0['event_id']}/review",
                       json={"status": "noted"}, headers=teacher).status_code == 404
    assert client.post(f"/api/exams/{e['id']}/events/999999/review",
                       json={"status": "noted"}, headers=teacher).status_code == 404

    res = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    assert res["flags_total"] == 2 and res["flags_open"] == 0
    al = next(x for x in res["attempts"] if x["username"] == "alice")
    assert al["flag_count"] == 2 and len(al["events"]) == 4
    noted = next(ev for ev in al["events"] if ev["event_id"] == f0["event_id"])
    assert noted["review"] == "noted" and noted["note"] == "Talked to student"
    assert next(ev for ev in al["events"] if ev["type"] == "lock_started")["review"] == "open"


def test_live_flags_capped_at_50(client, teacher, alice):
    e = make_exam(client, teacher, SAMPLE_EXAM)
    a = start(client, alice, e)
    evs = [{"type": "focus_lost", "detail": str(i), "client_time": "t"} for i in range(60)]
    client.post(f"/api/student/attempts/{a['attempt_id']}/events", json={"events": evs},
                headers=alice)
    flags = client.get(f"/api/exams/{e['id']}/live", headers=teacher).json()["flags"]
    assert len(flags) == 50 and flags[0]["detail"] == "59" and flags[-1]["detail"] == "10"


# ---------------------------------------------------------------- shuffle
def test_shuffle_stable_per_attempt_and_differs(client, teacher, alice, bob, carol):
    body = {"title": "Shuffled", "duration_min": 30, "shuffle_questions": True,
            "questions": [{"type": "short", "prompt": f"Q{i}", "points": 1, "topics": ["t"],
                           "accepted_answers": [str(i)]} for i in range(8)]}
    e = make_exam(client, teacher, body)
    assert e["shuffle_questions"] is True
    original = [q["id"] for q in e["questions"]]
    orders = []
    for h in (alice, bob, carol):
        a = start(client, h, e)
        order = [q["id"] for q in a["questions"]]
        assert sorted(order) == sorted(original)
        for _ in range(2):  # stable across reloads and resume-via-start
            g = client.get(f"/api/student/attempts/{a['attempt_id']}", headers=h).json()
            assert [q["id"] for q in g["questions"]] == order
        assert [q["id"] for q in start(client, h, e)["questions"]] == order
        orders.append(order)
    # 8! orderings: three identical orders (or all equal to the original) is ~impossible
    assert len({tuple(o) for o in orders}) > 1
    assert any(o != original for o in orders)
    # teacher views keep the original order
    res = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    assert [q["question_id"] for q in res["questions"]] == original


def test_shuffle_helper_deterministic():
    from app.services import shuffled_for
    items = list(range(20))
    assert shuffled_for("a_x", items) == shuffled_for("a_x", items)
    assert shuffled_for("a_x", items) != shuffled_for("a_y", items)
    assert items == list(range(20))  # input not mutated


def test_no_shuffle_keeps_order(client, teacher, alice):
    e = make_exam(client, teacher)
    a = start(client, alice, e)
    assert [q["id"] for q in a["questions"]] == [q["id"] for q in e["questions"]]


# ---------------------------------------------------------------- ownership
def test_other_teacher_cannot_see_exams(client, teacher, graded_setup):
    other = make_user(client, "teacher2", "teacher", "Other Teacher")
    e, aid, qids = graded_setup["exam"], graded_setup["alice"], graded_setup["qids"]
    assert client.get("/api/exams", headers=other).json() == []
    for path in ("", "/results", "/live", "/grading"):
        assert client.get(f"/api/exams/{e['id']}{path}", headers=other).status_code == 404
    assert put_grade(client, other, e, aid, qids[3], points=1).status_code == 404
    assert client.put(f"/api/exams/{e['id']}/attempts/{aid}/comment", json={"comment": "x"},
                      headers=other).status_code == 404
    for path in ("release", "unrelease"):
        assert client.post(f"/api/exams/{e['id']}/{path}", json={"force": True},
                           headers=other).status_code == 404
    assert client.post(f"/api/exams/{e['id']}/extend", json={"minutes": 5},
                       headers=other).status_code == 404
    assert client.get("/api/grading", headers=other).json() == []
    gb = client.get("/api/gradebook", headers=other).json()
    assert gb["exams"] == [] and gb["courses"] == []
    assert all(s["cells"] == {} for s in gb["students"])
    assert client.get("/api/grading", headers=teacher).json()[0]["exam_id"] == e["id"]


# ---------------------------------------------------------------- migration
def test_old_database_gets_new_columns(tmp_path):
    import asyncio
    import sqlite3

    from app.config import Settings
    from app.db import Database

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64) UNIQUE NOT NULL,
            name VARCHAR(200) NOT NULL, role VARCHAR(16) NOT NULL,
            password_hash VARCHAR(256) NOT NULL, created_at DATETIME NOT NULL);
        CREATE TABLE exams (id VARCHAR(32) PRIMARY KEY, teacher_id INTEGER NOT NULL,
            title VARCHAR(200) NOT NULL, duration_min INTEGER NOT NULL, status VARCHAR(16) NOT NULL,
            start_code VARCHAR(8), questions JSON NOT NULL, question_count INTEGER NOT NULL,
            total_points FLOAT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
        INSERT INTO exams VALUES ('e_old', 1, 'Old', 30, 'closed', NULL, '[]', 0, 0,
            '2026-01-01 00:00:00', '2026-01-01 00:00:00');
        CREATE TABLE attempts (id VARCHAR(32) PRIMARY KEY, exam_id VARCHAR(32) NOT NULL,
            student_id INTEGER NOT NULL, status VARCHAR(16) NOT NULL, started_at DATETIME NOT NULL,
            deadline_at DATETIME NOT NULL, submitted_at DATETIME, answers JSON NOT NULL,
            client_seq BIGINT NOT NULL, receipt VARCHAR(16), auto_submitted BOOLEAN NOT NULL,
            late BOOLEAN NOT NULL, score FLOAT, updated_at DATETIME NOT NULL);
        INSERT INTO attempts VALUES ('a_old', 'e_old', 2, 'submitted', '2026-01-01 00:00:00',
            '2026-01-01 00:30:00', '2026-01-01 00:10:00', '{}', 1, 'AAAA-BBBB', 0, 0, 0,
            '2026-01-01 00:10:00');
    """)
    con.commit()
    con.close()

    async def go():
        db = Database(Settings(database_url=f"sqlite+aiosqlite:///{path}"))
        await db.create_all()
        await db.create_all()  # idempotent
        await db.dispose()
    asyncio.run(go())
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(exams)")}
    assert {"course", "room", "scheduled_at", "shuffle_questions", "grades_released",
            "released_at", "closed_at"} <= cols
    row = con.execute("SELECT course, room, shuffle_questions, grades_released FROM exams").fetchone()
    assert row == ("", "", 0, 0)
    ev_cols = {r[1] for r in con.execute("PRAGMA table_info(integrity_events)")}
    assert {"review", "note"} <= ev_cols
    row = con.execute("SELECT grades, overall_comment, needs_grading FROM attempts").fetchone()
    assert row == ("{}", "", 0)
    idx = {r[1] for r in con.execute("PRAGMA index_list(integrity_events)")}
    assert "ix_events_exam_review" in idx
    con.close()
