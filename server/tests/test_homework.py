"""Homework mode (paper assignments) + progress dashboard (CONTRACT.md "Homework mode and progress").
AI calls go through a fake httpx transport."""
import json
import time
from datetime import datetime, timedelta

import cv2
import httpx
import numpy as np
import pytest

from app import ai
from app.homework import progress, segment_adapter
from app.homework.progress import Item, Student

from .conftest import login
from .test_classes import make_user

QUESTIONS = [
    {"type": "numeric", "prompt": "Solve 2x + 3 = 11.", "points": 2, "answer_number": 4, "topics": ["linear equations"]},
    {"type": "essay", "prompt": "Factor x^2 - 5x + 6 and show your work.", "points": 4,
     "topics": ["factoring"]},
]


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


def _class(client, teacher, members="alice"):
    c = client.post("/api/classes", json={"name": "Algebra 1"}, headers=teacher).json()
    if members:
        r = client.post(f"/api/classes/{c['id']}/members", json={"text": members}, headers=teacher)
        assert r.status_code == 200, r.text
    return c


def _due(days=2):
    return (datetime.utcnow() + timedelta(days=days)).replace(microsecond=0).isoformat() + "Z"


def _hw(client, teacher, cid, **kw):
    body = {"title": "Week 3 homework", "due_at": _due(), "class_ids": [cid], "questions": QUESTIONS, **kw}
    r = client.post("/api/homework", json=body, headers=teacher)
    assert r.status_code == 201, r.text
    return r.json()


def _photo(text="x = 4", seed=0) -> bytes:
    """A phone-like photo: a white sheet with handwriting on a dark desk, slightly rotated."""
    img = np.full((1400, 1100, 3), 60, np.uint8)
    pts = np.array([[150, 120], [960, 160], [930, 1300], [120, 1260]], np.int32)
    cv2.fillPoly(img, [pts], (245, 245, 245))
    for i, line in enumerate([text, "page %d" % seed, "work shown"]):
        cv2.putText(img, line, (220, 320 + i * 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (20, 20, 20), 4)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def _submit(client, student, eid, n=2):
    files = [("files", (f"p{i}.jpg", _photo(seed=i), "image/jpeg")) for i in range(n)]
    return client.post(f"/api/student/homework/{eid}/submit", files=files, headers=student)


def _wait_done(client, student, eid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/student/homework/{eid}", headers=student).json()
        if d["state"] not in ("processing",):
            return d
        time.sleep(0.1)
    raise AssertionError("homework processing did not finish")


def _wait_status(client, teacher, eid, username, statuses, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/homework/{eid}", headers=teacher).json()
        row = next(s for s in d["students"] if s["username"] == username)
        if row["status"] in statuses and row["processing"] not in ("processing", "grading"):
            return d, row
        time.sleep(0.1)
    raise AssertionError(f"{username} never reached {statuses}: {row}")


def _fake_ai(stats):
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = body["messages"][1]["content"]
        text = "".join(c.get("text", "") for c in content if c["type"] == "text")
        stats["calls"] += 1
        stats["images"].append(sum(1 for c in content if c["type"] != "text"))
        stats["prompts"].append(text)
        if "Factor" in text:
            out = {"legibility": "clear", "transcript": "(x-2)(x-3)",
                   "steps": [{"text": "x^2 - 5x + 6", "status": "correct"},
                             {"text": "(x+2)(x+3)", "status": "error", "note": "signs"}],
                   "points": 2, "explanation": "Check the signs: -2 and -3 multiply to +6 and add to -5.",
                   "misconception": {"tag": "sign error", "text": ""}, "confidence": 0.9}
        else:
            out = {"legibility": "clear", "transcript": "x = 4", "steps": [{"text": "2x = 8", "status": "correct"},
                                                                          {"text": "x = 4", "status": "correct"}],
                   "points": 2, "explanation": "Correct.", "confidence": 0.95}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})
    return httpx.MockTransport(handler)


def _ai_on(monkeypatch):
    stats = {"calls": 0, "images": [], "prompts": []}
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    ai.set_transport(_fake_ai(stats))
    return stats


# ---------------------------------------------------------------- CRUD
def test_homework_crud_and_validation(client, teacher, alice):
    c = _class(client, teacher)
    base = {"title": "HW", "due_at": _due(), "questions": QUESTIONS}
    assert client.post("/api/homework", json={**base, "class_ids": []}, headers=teacher).status_code == 422
    assert client.post("/api/homework", json={**base, "class_ids": [999]}, headers=teacher).status_code == 404
    bad = [{"type": "essay", "prompt": "No topic", "points": 1}]
    r = client.post("/api/homework", json={**base, "class_ids": [c["id"]], "questions": bad}, headers=teacher)
    assert r.status_code == 422 and "topic" in r.json()["detail"]
    assert client.post("/api/homework", json={**base, "class_ids": [c["id"]]}, headers=alice).status_code == 403

    h = _hw(client, teacher, c["id"], instructions="Show every step.", auto_release=True)
    assert h["classes"][0]["id"] == c["id"] and h["instructions"] == "Show every step."
    assert h["auto_release"] is True and h["allow_late"] is True and h["allow_resubmit"] is True
    assert [q["topics"] for q in h["questions"]] == [["linear equations"], ["factoring"]]
    assert h["counts"]["total"] == 1 and h["counts"]["not_submitted"] == 1
    assert h["students"][0]["username"] == "alice" and h["students"][0]["status"] == "not_submitted"
    # it is a paper exam underneath: Scan & Grade and the answer key work unchanged
    ex = client.get(f"/api/exams/{h['exam_id']}", headers=teacher).json()
    assert ex["delivery"] == "paper" and ex["status"] == "draft"
    assert client.get(f"/api/exams/{h['exam_id']}/answer-key", headers=teacher).status_code == 200
    assert h["exam_id"] not in [e["id"] for e in client.get("/api/student/exams", headers=alice).json()]

    lst = client.get("/api/homework", headers=teacher).json()
    assert [x["exam_id"] for x in lst] == [h["exam_id"]]
    assert client.get(f"/api/homework?class_id={c['id']}", headers=teacher).json()[0]["counts"]["total"] == 1

    r = client.patch(f"/api/homework/{h['exam_id']}", json={"allow_late": False, "title": "Week 3 (v2)",
                                                           "due_at": "2030-01-01T10:00:00Z"}, headers=teacher)
    assert r.status_code == 200 and r.json()["allow_late"] is False and r.json()["title"] == "Week 3 (v2)"
    assert r.json()["due_at"].startswith("2030-01-01T10:00:00")

    # another teacher sees nothing
    t2, _ = make_user(client, "teacher2", "teacher")
    assert client.get(f"/api/homework/{h['exam_id']}", headers=t2).status_code == 404
    assert client.get("/api/homework", headers=t2).json() == []
    assert client.patch(f"/api/homework/{h['exam_id']}", json={"title": "x"}, headers=t2).status_code == 404

    # worksheet: teacher uploads, class members download, others cannot
    r = client.put(f"/api/homework/{h['exam_id']}/worksheet", headers=teacher,
                   files={"file": ("sheet.pdf", b"%PDF-1.4 worksheet", "application/pdf")})
    assert r.status_code == 200 and r.json()["worksheet"] == {"name": "sheet.pdf", "type": "application/pdf"}
    assert client.put(f"/api/homework/{h['exam_id']}/worksheet", headers=teacher,
                      files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 422
    r = client.get(f"/api/student/homework/{h['exam_id']}/worksheet", headers=alice)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    bob = login(client, "bob")
    assert client.get(f"/api/student/homework/{h['exam_id']}/worksheet", headers=bob).status_code == 403

    assert client.delete(f"/api/homework/{h['exam_id']}", headers=teacher).status_code == 204
    assert client.get(f"/api/homework/{h['exam_id']}", headers=teacher).status_code == 404
    assert client.get(f"/api/exams/{h['exam_id']}", headers=teacher).status_code == 404


# ---------------------------------------------------------------- student upload rules
def test_student_upload_permissions_due_and_resubmit(client, teacher, alice, bob):
    c = _class(client, teacher, "alice")
    h = _hw(client, teacher, c["id"])
    eid = h["exam_id"]
    # bob is not in the class: cannot see or upload
    assert client.get(f"/api/student/homework/{eid}", headers=bob).status_code == 403
    assert _submit(client, bob, eid).status_code == 403
    assert all(x["exam_id"] != eid for x in client.get("/api/student/homework", headers=bob).json())
    # a teacher token is not a student
    assert _submit(client, teacher, eid).status_code == 403
    assert client.get("/api/student/homework/nope", headers=alice).status_code == 404
    # not a photo
    r = client.post(f"/api/student/homework/{eid}/submit", headers=alice,
                    files=[("files", ("a.txt", b"hello there", "text/plain"))])
    assert r.status_code == 422

    lst = client.get("/api/student/homework", headers=alice).json()
    assert lst[0]["exam_id"] == eid and lst[0]["state"] == "not_submitted" and lst[0]["can_submit"]
    assert lst[0]["class_names"] == ["Algebra 1"]

    r = _submit(client, alice, eid)
    assert r.status_code == 202, r.text
    assert r.json()["pages"] == 2 and r.json()["late"] is False
    d = _wait_done(client, alice, eid)
    assert d["state"] == "handed_in" and len(d["pages"]) == 2 and d["late"] is False
    assert all(u.startswith("/api/uploads/") for u in d["pages"])
    assert d["can_submit"] is True  # resubmit until due
    assert "correct_answer" not in json.dumps(d["questions"]) and "topics" not in d["questions"][0]
    # AI is off: answers wait for the teacher (fallback drafts), never graded
    _, row = _wait_status(client, teacher, eid, "alice", ("ai_graded",))
    assert row["source"] == "student" and len(row["pages"]) == 2 and row["needs_grading"] == 2
    # the paper attempt holds the whole page set for every question (no segmenter on this branch)
    q = client.get(f"/api/exams/{eid}/scan/queue", headers=teacher).json()
    it = q["items"][0]
    assert it["username"] == "alice" and it["image"] == d["pages"][0]
    assert it["draft"]["source"] == "fallback"
    # the exam is now "collected" (closed) so it can be released later
    assert client.get(f"/api/exams/{eid}", headers=teacher).json()["status"] == "closed"

    # resubmission replaces the pages
    assert _submit(client, alice, eid, n=1).status_code == 202
    d = _wait_done(client, alice, eid)
    assert len(d["pages"]) == 1

    # no resubmitting when the teacher turned it off
    client.patch(f"/api/homework/{eid}", json={"allow_resubmit": False}, headers=teacher)
    r = _submit(client, alice, eid)
    assert r.status_code == 409 and r.json()["detail"] == "You already handed this in"

    # past due: late flag when late work is allowed, refused otherwise
    carol = login(client, "carol")
    client.post(f"/api/classes/{c['id']}/members", json={"text": "carol"}, headers=teacher)
    client.patch(f"/api/homework/{eid}", json={"due_at": "2020-01-01T00:00:00Z", "allow_late": False},
                 headers=teacher)
    st = client.get(f"/api/student/homework/{eid}", headers=carol).json()
    assert st["overdue"] is True and st["can_submit"] is False and st["block_reason"] == "The due date has passed"
    assert _submit(client, carol, eid).status_code == 409
    client.patch(f"/api/homework/{eid}", json={"allow_late": True}, headers=teacher)
    r = _submit(client, carol, eid, n=1)
    assert r.status_code == 202 and r.json()["late"] is True
    _wait_done(client, carol, eid)
    _, row = _wait_status(client, teacher, eid, "carol", ("ai_graded",))
    assert row["late"] is True


def test_segment_adapter_fallback_and_shapes(monkeypatch):
    qs = [{"id": "q1"}, {"id": "q2"}]
    page = cv2.imencode(".png", np.full((100, 80, 3), 255, np.uint8))[1].tobytes()
    out, mode = segment_adapter.locate(qs, [page, page])
    assert mode == "whole_page" and out["q1"].whole_page and len(out["q2"].images) == 2

    calls = []

    def fake(questions, pages):
        calls.append(len(pages))
        return {"q1": [{"page": 1, "bbox": [0.1, 0.1, 0.9, 0.5]}], "q2": "junk"}
    monkeypatch.setattr(segment_adapter, "_segmenter", lambda: fake)
    out, mode = segment_adapter.locate(qs, [page, page])
    assert calls == [2] and mode == "partial"
    assert not out["q1"].whole_page and out["q1"].page == 2 and len(out["q1"].images) == 1
    assert out["q2"].whole_page

    monkeypatch.setattr(segment_adapter, "_segmenter", lambda: (lambda q, p: 1 / 0))
    out, mode = segment_adapter.locate(qs, [page])
    assert mode == "whole_page"


# ---------------------------------------------------------------- AI grading, review, release, notifications
def test_background_ai_grading_release_and_feedback(client, teacher, alice, bob, monkeypatch):
    stats = _ai_on(monkeypatch)
    c = _class(client, teacher, "alice\nbob")
    h = _hw(client, teacher, c["id"])
    eid = h["exam_id"]
    r = client.put(f"/api/exams/{eid}/answer-key", json={"subject": "math"}, headers=teacher)
    assert r.status_code == 200, r.text
    assert _submit(client, alice, eid, n=2).status_code == 202
    _wait_done(client, alice, eid)
    d, row = _wait_status(client, teacher, eid, "alice", ("ai_graded", "reviewed"))
    assert stats["calls"] == 2
    assert stats["images"] == [2, 2]  # whole page set for every question
    assert all("whole answer page" in p for p in stats["prompts"])
    q = client.get(f"/api/exams/{eid}/scan/queue", headers=teacher).json()
    drafts = {i["question_id"]: i["draft"] for i in q["items"]}
    qids = [x["question_id"] for x in h["questions"]]
    assert drafts[qids[0]]["source"] == "key" and drafts[qids[0]]["points"] == 2
    assert drafts[qids[1]]["misconception"] == {"tag": "sign error", "text": ""}
    assert row["status"] == "ai_graded" and d["counts"]["ai_graded"] == 1 and d["counts"]["not_submitted"] == 1

    # nothing is visible to the student before release
    assert client.get(f"/api/student/grades/{eid}", headers=alice).status_code == 403
    assert client.get("/api/student/notifications", headers=alice).json()["feedback_ready"] == 0
    assert client.get("/api/student/progress", headers=alice).json()["topics"] == []
    r = client.post(f"/api/homework/{eid}/release", json={}, headers=teacher)
    assert r.status_code == 409 and "still need grading" in r.json()["detail"]

    # teacher accepts both drafts in the existing review screen
    aid = q["items"][0]["attempt_id"]
    for qid in qids:
        r = client.post(f"/api/exams/{eid}/scan/answers/{aid}/{qid}/approve", json={}, headers=teacher)
        assert r.status_code == 200, r.text
    d, row = _wait_status(client, teacher, eid, "alice", ("reviewed",))
    assert row["percent"] == round(4 / 6 * 100, 1)
    r = client.post(f"/api/homework/{eid}/release", json={}, headers=teacher)
    assert r.status_code == 200 and r.json()["grades_released"] is True
    assert {x["username"]: x["status"] for x in r.json()["students"]} == {"alice": "released", "bob": "not_submitted"}

    # student: badge, feedback with step marks and the explanation, then the badge clears
    n = client.get("/api/student/notifications", headers=alice).json()
    assert n["feedback_ready"] == 1 and n["items"][0]["exam_id"] == eid
    lst = client.get("/api/student/homework", headers=alice).json()
    assert lst[0]["state"] == "feedback" and lst[0]["feedback_new"] is True and lst[0]["percent"] == 66.7
    fb = client.get(f"/api/student/grades/{eid}", headers=alice).json()
    q2 = fb["questions"][1]
    assert q2["points_awarded"] == 2 and "Check the signs" in q2["comment"]
    assert [s["status"] for s in q2["steps"]] == ["correct", "error"]
    det = client.get(f"/api/student/homework/{eid}", headers=alice).json()
    assert det["state"] == "feedback" and det["can_submit"] is False
    assert client.get("/api/student/notifications", headers=alice).json()["feedback_ready"] == 0
    # my progress: released work only
    pr = client.get("/api/student/progress", headers=alice).json()
    topics = {t["topic"]: t for t in pr["topics"]}
    assert topics["linear equations"]["mastery"] == 100.0 and topics["factoring"]["mastery"] == 50.0
    assert [t["topic"] for t in pr["work_on"]] == ["factoring"]
    assert pr["recent"][0]["exam_id"] == eid and pr["recent"][0]["homework"] is True
    assert pr["mistakes"][0]["label"] == "sign error"
    # bob gets nothing
    assert client.get("/api/student/notifications", headers=bob).json()["feedback_ready"] == 0
    assert client.get(f"/api/student/grades/{eid}", headers=bob).status_code == 404

    # teacher progress dashboard shows the topic and the misconception
    pg = client.get(f"/api/progress?class_id={c['id']}", headers=teacher).json()
    assert [t["topic"] for t in pg["topics"]] == ["factoring", "linear equations"]
    al = next(s for s in pg["students"] if s["username"] == "alice")
    assert al["cells"]["factoring"]["mastery"] == 50.0 and al["cells"]["factoring"]["level"] == "struggling"
    assert pg["misconceptions"][0]["label"] == "sign error" and pg["misconceptions"][0]["this_week"] == 1
    act = next(a for a in pg["actions"] if a["topic"] == "factoring")
    assert act["kind"] == "reteach" and act["students"] == ["alice"]
    sp = client.get("/api/progress/students/alice", headers=teacher).json()
    assert sp["mistakes"][0]["examples"][0]["image"].startswith("/api/uploads/")
    assert {t["topic"] for t in sp["topics"]} == {"factoring", "linear equations"}


def test_auto_release_when_everything_is_reviewed(client, teacher, alice, monkeypatch):
    _ai_on(monkeypatch)
    c = _class(client, teacher, "alice")
    h = _hw(client, teacher, c["id"], auto_release=True)
    eid = h["exam_id"]
    _submit(client, alice, eid)
    _wait_done(client, alice, eid)
    _wait_status(client, teacher, eid, "alice", ("ai_graded",))
    q = client.get(f"/api/exams/{eid}/scan/queue", headers=teacher).json()
    items = [{"attempt_id": i["attempt_id"], "question_id": i["question_id"]} for i in q["items"]]
    r = client.post(f"/api/exams/{eid}/scan/approve-bulk", json={"items": items}, headers=teacher)
    assert r.json()["approved"] == 2
    d = client.get(f"/api/homework/{eid}", headers=teacher).json()
    assert d["grades_released"] is True and d["auto_released_at"] is not None
    assert client.get("/api/student/notifications", headers=alice).json()["feedback_ready"] == 1
    # unrelease turns auto-release off so it does not come back by itself
    d = client.post(f"/api/homework/{eid}/unrelease", headers=teacher).json()
    assert d["grades_released"] is False and d["auto_release"] is False


def test_progress_permissions_and_notes(client, teacher, alice):
    c = _class(client, teacher, "alice")
    t2, _ = make_user(client, "teacher2", "teacher")
    assert client.get(f"/api/progress?class_id={c['id']}", headers=t2).status_code == 404
    assert client.get(f"/api/progress?class_id={c['id']}", headers=alice).status_code == 403
    assert client.get("/api/progress/students/alice", headers=t2).status_code == 404
    assert client.get("/api/progress/students/bob", headers=teacher).status_code == 404  # not my student
    r = client.post("/api/progress/students/alice/notes", json={"text": "Talked about signs."}, headers=teacher)
    assert r.status_code == 201
    nid = r.json()["id"]
    sp = client.get("/api/progress/students/alice", headers=teacher).json()
    assert sp["notes"][0]["text"] == "Talked about signs." and sp["classes"][0]["name"] == "Algebra 1"
    assert client.delete(f"/api/progress/notes/{nid}", headers=t2).status_code == 404
    assert client.delete(f"/api/progress/notes/{nid}", headers=teacher).status_code == 204
    empty = client.get(f"/api/progress?class_id={c['id']}", headers=teacher).json()
    assert empty["topics"] == [] and empty["students"][0]["trend_label"] == "new"


# ---------------------------------------------------------------- dashboard math on a fixed fixture
NOW = datetime(2026, 10, 12, 12, 0)


def _it(sid, exam, days_ago, topic, awarded, mx=10.0, label=None, approved=True):
    return Item(student_id=sid, exam_id=exam, exam_title=exam.upper(), question_id=f"{exam}-{topic}",
                topics=[topic], awarded=awarded, max_points=mx, date=NOW - timedelta(days=days_ago),
                label=label, approved=approved)


def test_progress_math_fixture():
    # weight halves every 21 days
    assert progress.weight(NOW - timedelta(days=21), NOW) == pytest.approx(0.5)
    # mastery: recent work counts more. 21 days ago 0/10 (w .5), today 10/10 (w 1) -> 10/15
    pct, n = progress.mastery([_it(1, "a", 21, "t", 0), _it(1, "b", 0, "t", 10)], NOW)
    assert pct == 66.7 and n == 2
    # pending answers are skipped
    assert progress.mastery([_it(1, "a", 0, "t", None)], NOW) == (None, 0)
    # trend: recent = last min(2, n//2), before = up to 3 before those
    assert progress.trend([50]) is None
    assert progress.trend([50, 80]) == 30.0
    assert progress.trend([90, 80, 70, 40, 30]) == -45.0   # recent [40,30]=35, before [90,80,70]=80
    assert progress.trend_label(-10) == "slipping" and progress.trend_label(9.9) == "steady"

    students = [Student(1, "ann", "Ann"), Student(2, "ben", "Ben"), Student(3, "cat", "Cat")]
    items = [
        # ann slips: 100% -> 90% -> 40% -> 30% in algebra
        _it(1, "e1", 30, "algebra", 10), _it(1, "e2", 20, "algebra", 9),
        _it(1, "e3", 10, "algebra", 4, label="sign error"), _it(1, "e4", 2, "algebra", 3, label="sign error"),
        # ben improves: 20% -> 90%
        _it(2, "e1", 30, "algebra", 2, label="sign error"), _it(2, "e4", 2, "algebra", 9),
        # cat: geometry only, last week + this week mistakes (one still an AI draft)
        _it(3, "e2", 9, "geometry", 4, label="wrong formula"), _it(3, "e4", 1, "geometry", None,
                                                                    label="wrong formula", approved=False),
        _it(3, "e4", 1, "geometry", 5, label="wrong formula"),
        # someone not in the class is ignored
        _it(9, "e4", 1, "algebra", 0, label="sign error"),
    ]
    d = progress.class_dashboard(items, students, NOW)
    assert [t["topic"] for t in d["topics"]] == ["algebra", "geometry"]
    rows = {r["username"]: r for r in d["students"]}
    assert rows["ann"]["trend"] == -60.0 and rows["ann"]["trend_label"] == "slipping"  # [40,30]=35 vs [100,90]=95
    assert rows["ben"]["trend"] == 70.0 and rows["ben"]["trend_label"] == "improving"
    assert rows["cat"]["trend"] == 10.0 and rows["cat"]["trend_label"] == "improving"
    assert [s["username"] for s in d["slipping"]] == ["ann"]
    assert [s["username"] for s in d["improving"]] == ["ben", "cat"]
    # ann's algebra mastery, weights .5^(30/21), .5^(20/21), .5^(10/21), .5^(2/21)
    w = [0.5 ** (x / 21) for x in (30, 20, 10, 2)]
    exp = round(sum(wi * g for wi, g in zip(w, (10, 9, 4, 3))) / (10 * sum(w)) * 100, 1)
    assert rows["ann"]["cells"]["algebra"]["mastery"] == exp
    assert rows["ann"]["cells"]["algebra"]["level"] == "struggling"
    assert "geometry" not in rows["ann"]["cells"]
    # misconceptions: this week [now-7d, now) vs last week [now-14d, now-7d)
    mis = {m["label"]: m for m in d["misconceptions"]}
    assert (mis["sign error"]["this_week"], mis["sign error"]["last_week"]) == (1, 1)  # ann e4 / ann e3
    assert (mis["wrong formula"]["this_week"], mis["wrong formula"]["last_week"]) == (2, 1)
    assert mis["wrong formula"]["approved"] == 1 and mis["wrong formula"]["change"] == 1
    assert [m["label"] for m in d["misconceptions"]] == ["wrong formula", "sign error"]
    assert [s["username"] for s in mis["sign error"]["students"]] == ["ann"]
    # actions: geometry (cat 45%) is a re-teach (class mastery < 60), algebra has 1 struggling of 2
    acts = {a["topic"]: a for a in d["actions"]}
    geo = d["topics"][1]
    assert geo["mastery"] < 60 and acts["geometry"]["kind"] == "reteach"
    assert acts["geometry"]["misconception"] == "wrong formula" and acts["geometry"]["students"] == ["cat"]
    alg = d["topics"][0]
    if alg["mastery"] < 70:
        assert acts["algebra"]["kind"] == "reteach"  # 1 of 2 students struggle = 50 % >= 40 %

    one = progress.student_detail([i for i in items if i.student_id == 1], NOW)
    assert one["mistakes"][0]["label"] == "sign error" and one["mistakes"][0]["count"] == 2
    assert one["mistakes"][0]["recurring"] is True
    assert [p["percent"] for p in one["topics"][0]["series"]] == [100.0, 90.0, 40.0, 30.0]
    view = progress.student_view([i for i in items if i.student_id == 2], NOW)
    assert [t["topic"] for t in view["improving"]] == ["algebra"] and view["work_on"] == []
