"""Slice 6 F/G: class insights, students list and student profiles.

Data is written straight into the test database (sync SQLAlchemy on the same SQLite file)
so the tests do not depend on the exam-editor validation other slices are changing.
"""
import os
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.analytics import cluster_mistakes, jaccard, tokens
from app.models import Attempt, Exam, User
from app.security import hash_password, make_token

pytestmark = pytest.mark.skipif(bool(os.environ.get("PROCTORA_TEST_DATABASE_URL")),
                                reason="writes the SQLite test file directly")

D1 = datetime(2026, 9, 1, 9, 0)
D2 = datetime(2026, 9, 15, 9, 0)
D3 = datetime(2026, 9, 20, 9, 0)


def q(qid, type_, points, topics=None, **kw):
    base = {"id": qid, "type": type_, "prompt": f"Prompt {qid}", "points": points, "options": [],
            "correct_index": None, "answer_number": None, "tolerance": None,
            "accepted_answers": [], "rubric": [], "min_words": None, "max_words": None}
    if topics is not None:
        base["topics"] = topics
    base.update(kw)
    return base


E1_QS = [
    q("q1", "mcq", 2, ["algebra"], options=["3", "4"], correct_index=1),
    q("q2", "numeric", 4, ["Integration by parts"], answer_number=2.0, tolerance=0),
    q("q3", "essay", 10, ["integration by parts", "writing"]),
    q("q4", "short", 4, None, accepted_answers=["paris"]),  # old question: no topics
]
E2_QS = [
    q("q1", "numeric", 10, ["integration by parts"], answer_number=5.0),
    q("q2", "mcq", 2, ["algebra"], options=["a", "b"], correct_index=0),
]
EXT_QS = [q("q_external", "external", 100)]


def grade(points, comment="", tags=None):
    g = {"points": points, "comment": comment, "rubric_scores": None,
         "graded_at": "2026-09-02T00:00:00.000Z"}
    if tags is not None:
        g["mistake_tags"] = tags
    return g


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'test.db'}")


def _exam(s, eid, teacher_id, title, qs, when, course="MATH 201", mode="questions"):
    s.add(Exam(id=eid, teacher_id=teacher_id, title=title, duration_min=60, status="closed",
               questions=qs, question_count=len(qs), total_points=sum(x["points"] for x in qs),
               created_at=when - timedelta(days=5), updated_at=when, course=course,
               scheduled_at=when, closed_at=when + timedelta(hours=2), mode=mode,
               external_url="https://x.example.com" if mode == "external" else None))


def _att(s, aid, eid, sid, when, answers, grades=None, minutes=30, status="submitted"):
    s.add(Attempt(id=aid, exam_id=eid, student_id=sid, status=status, started_at=when,
                  deadline_at=when + timedelta(hours=1),
                  submitted_at=when + timedelta(minutes=minutes) if status == "submitted" else None,
                  answers=answers, client_seq=1, receipt="AAAA-BBBB", grades=grades or {},
                  overall_comment=""))


@pytest.fixture
def world(client, tmp_path):
    eng = _engine(tmp_path)
    with Session(eng) as s:
        ids = dict(s.execute(select(User.username, User.id)).all())
        t2 = User(username="teacher2", name="Other Teacher", role="teacher",
                  password_hash=hash_password("x", 1000))
        s.add(t2)
        s.flush()
        t1, alice, bob, carol = ids["teacher"], ids["alice"], ids["bob"], ids["carol"]
        _exam(s, "e_1", t1, "Quiz 1", E1_QS, D1)
        _exam(s, "e_2", t1, "Quiz 2", E2_QS, D2)
        _exam(s, "e_3", t1, "Web test", EXT_QS, D3, mode="external")
        _exam(s, "e_other", t2.id, "Other quiz", E2_QS, D2, course="BIO 1")
        # Quiz 1
        _att(s, "a_1a", "e_1", alice, D1, {"q1": 1, "q2": 7, "q3": "long essay", "q4": "Paris"},
             {"q3": grade(5, "Forgot the boundary terms", ["sign error"])}, minutes=42)
        _att(s, "a_1b", "e_1", bob, D1,
             {"q1": 0, "q2": 2.0, "q4": "London",
              # paper-style answer with an AI draft, not graded yet -> pending
              "q3": {"image": "/api/uploads/x", "ai_draft": {
                  "points": 3, "comment": "Missing boundary terms", "confidence": 0.9,
                  "mistake_tags": []}}},
             {"q4": grade(1, "Sign error in the last step")}, minutes=55)
        # Quiz 2
        _att(s, "a_2a", "e_2", alice, D2, {"q1": 5, "q2": 0}, {"q1": grade(10)}, minutes=20)
        _att(s, "a_2b", "e_2", bob, D2, {"q1": 1, "q2": 1}, {"q1": grade(3, "", ["sign errors"])})
        # Website exam: alice graded 80, bob not graded yet (pending -> excluded)
        _att(s, "a_3a", "e_3", alice, D3, {}, {"q_external": grade(80)})
        _att(s, "a_3b", "e_3", bob, D3, {})
        # carol: still writing quiz 2 -> not counted anywhere
        _att(s, "a_2c", "e_2", carol, D2, {"q1": 5}, status="in_progress")
        # Other teacher's exam
        _att(s, "a_oa", "e_other", alice, D2, {"q1": 1, "q2": 1})
        s.commit()
        t2_id = t2.id
    eng.dispose()
    token = make_token("test-secret", t2_id, "teacher", 3600)
    return {"teacher2": {"Authorization": f"Bearer {token}"}}


def by_topic(data):
    return {t["topic"]: t for t in data["topics"]}


# ---------------------------------------------------------------- insights
def test_topic_averages_partial_credit_and_pending(client, teacher, world):
    r = client.get("/api/insights", headers=teacher)
    assert r.status_code == 200, r.text
    d = r.json()
    t = by_topic(d)
    # integration by parts: E1 alice q2 0/4, alice q3 5/10 (partial), bob q2 4/4 (bob q3 pending)
    # E2 alice 10/10, bob 3/10 -> 22/38
    ibp = t["integration by parts"]
    assert ibp["avg_percent"] == round(22 / 38 * 100, 1)
    assert ibp["n_answers"] == 5 and ibp["n_students"] == 2
    cells = {c["exam_id"]: c for c in ibp["by_exam"]}
    assert cells["e_1"]["avg_percent"] == 50.0 and cells["e_1"]["n_answers"] == 3
    assert cells["e_2"]["avg_percent"] == 65.0
    assert ibp["trend"] == 15.0  # last exam minus earlier average
    assert t["algebra"]["avg_percent"] == 50.0 and t["algebra"]["trend"] == 0.0
    assert t["writing"]["n_answers"] == 1 and t["writing"]["trend"] is None
    # old question without topics -> "untagged", flagged so the UI can hide it
    assert t["untagged"]["avg_percent"] == round(5 / 8 * 100, 1) and t["untagged"]["untagged"]
    # website exam: topic = course; bob's ungraded score is excluded
    assert t["math 201"]["n_answers"] == 1 and t["math 201"]["avg_percent"] == 80.0
    assert d["topics"][-1]["topic"] == "untagged"  # tagged topics first, lowest first
    assert [e["exam_id"] for e in d["exams"]] == ["e_1", "e_2", "e_3"]
    assert d["attempt_count"] == 6  # carol's in-progress attempt not counted
    assert d["courses"] == ["MATH 201"]


def test_hardest_ordering(client, teacher, world):
    d = client.get("/api/insights", headers=teacher).json()
    order = [(h["exam_id"], h["question_id"], h["lost_points"]) for h in d["hardest"]]
    assert order[:6] == [("e_2", "q1", 7.0), ("e_1", "q3", 5.0), ("e_1", "q2", 4.0),
                         ("e_1", "q4", 3.0), ("e_1", "q1", 2.0), ("e_2", "q2", 2.0)]
    top = d["hardest"][0]
    assert top["avg_percent"] == 65.0 and top["exam_title"] == "Quiz 2"
    assert top["topics"] == ["integration by parts"]
    # e_1 q1 (50%) and e_2 q2 (50%) tie on lost points (exam order breaks the tie);
    # the hidden website-score item is not a question, so it is never listed
    assert all(h["exam_id"] != "e_3" for h in d["hardest"])


def test_mistake_clustering(client, teacher, world):
    d = client.get("/api/insights", headers=teacher).json()
    m = {x["label"]: x for x in d["mistakes"]}
    sign = m["sign error"]  # tag + "sign errors" tag + teacher comment "Sign error in ..."
    assert sign["count"] == 3
    assert sign["sources"] == {"tag": 2, "teacher": 1, "ai": 0}
    assert set(sign["topics"]) == {"integration by parts", "writing"}
    ex = sign["examples"][0]
    assert {"exam_id", "attempt_id", "question_id", "username", "name", "text"} <= set(ex)
    assert len(sign["examples"]) == 3
    boundary = m["Forgot the boundary terms"]  # teacher comment + AI draft comment (pending answer)
    assert boundary["count"] == 2
    assert boundary["sources"]["ai"] == 1
    assert [x["label"] for x in d["mistakes"]][0] == "sign error"


def test_insights_filters(client, teacher, world):
    d = client.get("/api/insights?exam_ids=e_2", headers=teacher).json()
    t = by_topic(d)
    assert t["integration by parts"]["avg_percent"] == 65.0
    assert t["integration by parts"]["trend"] is None
    assert "untagged" not in t
    assert [e["exam_id"] for e in d["exams"]] == ["e_2"]
    assert len(d["all_exams"]) == 3  # filter options keep every exam
    d = client.get("/api/insights?course=BIO%201", headers=teacher).json()
    assert d["topics"] == [] and d["hardest"] == [] and d["mistakes"] == []


def test_scoping_other_teacher(client, teacher, world, alice):
    t2 = world["teacher2"]
    d = client.get("/api/insights", headers=t2).json()
    assert [e["exam_id"] for e in d["exams"]] == ["e_other"]
    assert d["attempt_count"] == 1
    students = client.get("/api/students", headers=t2).json()
    assert [s["username"] for s in students] == ["alice"]
    assert students[0]["exam_count"] == 1
    assert client.get("/api/students/bob/profile", headers=t2).status_code == 404
    p = client.get("/api/students/alice/profile", headers=t2).json()
    assert [e["exam_id"] for e in p["exams"]] == ["e_other"]
    # teacher 1 does not see the other teacher's exam in alice's profile
    p1 = client.get("/api/students/alice/profile", headers=teacher).json()
    assert "e_other" not in [e["exam_id"] for e in p1["exams"]]
    # roles
    assert client.get("/api/insights", headers=alice).status_code == 403
    assert client.get("/api/students").status_code == 401
    assert client.get("/api/students/nobody/profile", headers=teacher).status_code == 404
    assert client.get("/api/students/teacher2/profile", headers=teacher).status_code == 404


# ---------------------------------------------------------------- students + profile
def test_students_list(client, teacher, world):
    rows = {s["username"]: s for s in client.get("/api/students", headers=teacher).json()}
    assert set(rows) == {"alice", "bob"}  # carol only has an in-progress attempt
    a, b = rows["alice"], rows["bob"]
    assert a["exam_count"] == 3 and b["exam_count"] == 3
    # alice: 11/20=55, 12/12=100, 80 -> 78.3; bob: only quiz 2 fully graded (3/12)
    assert a["average_percent"] == 78.3
    assert b["average_percent"] == 25.0
    assert a["weakest_topic"] == {"topic": "integration by parts", "avg_percent": 62.5}
    assert b["weakest_topic"]["topic"] == "algebra"


def test_profile_strengths_weak_trend(client, teacher, world):
    p = client.get("/api/students/bob/profile", headers=teacher).json()
    assert p["student"] == {"username": "bob", "name": "Bob Karimov"}
    t = by_topic(p)
    ibp = t["integration by parts"]
    assert ibp["avg_percent"] == 50.0 and ibp["n"] == 2
    assert [(x["exam_id"], x["percent"]) for x in ibp["trend"]] == [("e_1", 100.0), ("e_2", 30.0)]
    assert ibp["trend"][0]["date"] < ibp["trend"][1]["date"]
    assert p["weak"] == ["algebra", "integration by parts"]
    assert p["strengths"] == []
    assert t["untagged"]["n"] == 1  # never in strengths/weak
    pa = client.get("/api/students/alice/profile", headers=teacher).json()
    assert pa["strengths"] == ["algebra"]  # 4/4 over 2 answers
    assert "math 201" not in pa["strengths"]  # 80% but only one answer
    assert pa["weak"] == []
    assert pa["practice"] == [] or isinstance(pa["practice"], list)


def test_time_on_task_is_context_only(client, teacher, world):
    p = client.get("/api/students/alice/profile", headers=teacher).json()
    ex = {e["exam_id"]: e for e in p["exams"]}
    assert [e["exam_id"] for e in p["exams"]] == ["e_1", "e_2", "e_3"]
    assert ex["e_1"]["time_on_task_min"] == 42.0
    assert ex["e_2"]["time_on_task_min"] == 20.0
    assert ex["e_3"]["time_on_task_min"] is None  # website exam: not measured
    assert ex["e_1"]["percent"] == 55.0 and ex["e_1"]["state"] == "graded"
    for e in p["exams"]:
        assert not any(k for k in e if "flag" in k or "suspic" in k or "cheat" in k)
    pb = client.get("/api/students/bob/profile", headers=teacher).json()
    assert {e["exam_id"]: e["state"] for e in pb["exams"]}["e_1"] == "needs_grading"


# ---------------------------------------------------------------- pure helpers
def test_tokens_and_jaccard():
    assert tokens("Sign errors in the last step!") == frozenset({"sign", "error", "last", "step"})
    assert tokens("the a of") == frozenset()
    assert jaccard(tokens("sign error"), tokens("Sign errors")) == 1.0
    assert jaccard(frozenset({"a"}), frozenset()) == 0.0
    # "+C" keeps its letter, so it is not merged with an unrelated "missing" comment
    assert jaccard(tokens("missing +C"), tokens("Units missing")) < 0.5


def test_clustering_scales():
    from app.analytics import ExamInfo, Facts, MistakeItem
    info = ExamInfo("e", "E", "", D1, "computer", 10, [q("q1", "short", 1, ["t"])], {"q1": 0})
    words = ["chain", "rule", "sign", "limit", "domain", "units", "rounding", "graph", "slope",
             "area", "volume", "factor", "root", "proof", "angle"]
    items = [MistakeItem(f"{words[i % 15]} {words[(i * 7) % 15]} mistake {i % 400}", "teacher",
                         "e", f"a{i}", "q1", i, ["t"]) for i in range(20000)]
    f = Facts(exams={"e": info}, attempts=[], facts=[], mistakes=items, students={})
    t0 = time.perf_counter()
    out = cluster_mistakes(f)
    assert time.perf_counter() - t0 < 5
    assert 1 <= len(out) <= 20 and out[0]["count"] >= out[-1]["count"]


def test_profile_includes_sent_practice_only(client, teacher, world, tmp_path):
    pm = pytest.importorskip("app.practice.models")
    eng = _engine(tmp_path)
    with Session(eng) as s:
        ids = dict(s.execute(select(User.username, User.id)).all())
        items = [{"id": "i1"}, {"id": "i2"}]
        s.add(pm.PracticeSet(id="ps_1", teacher_id=ids["teacher"], student_id=ids["bob"], status="sent",
                             title="Algebra practice", topics=["algebra"], items=items, sent_at=D3))
        s.add(pm.PracticeSet(id="ps_2", teacher_id=ids["teacher"], student_id=ids["bob"], status="draft",
                             title="Draft", topics=[], items=items))
        s.add(pm.PracticeSet(id="ps_3", teacher_id=ids["teacher2"], student_id=ids["bob"], status="sent",
                             title="Other teacher", topics=[], items=items, sent_at=D3))
        s.add(pm.PracticeAttempt(set_id="ps_1", student_id=ids["bob"], item_id="i1", answer=1,
                                 correct=True, tries=1, ever_correct=True))
        s.commit()
    eng.dispose()
    p = client.get("/api/students/bob/profile", headers=teacher).json()
    assert [x["id"] for x in p["practice"]] == ["ps_1"]
    assert p["practice"][0]["correct"] == 1 and p["practice"][0]["total"] == 2
    # practice never changes grades-based numbers
    rows = {r["username"]: r for r in client.get("/api/students", headers=teacher).json()}
    assert rows["bob"]["average_percent"] == 25.0
