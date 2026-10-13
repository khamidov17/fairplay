"""Slice 3: external-website exams (SEB-style)."""
import copy
import csv
import io

import pytest

from .conftest import SAMPLE_EXAM, login

EXTERNAL_EXAM = {
    "title": "Canvas Quiz",
    "duration_min": 45,
    "course": "MATH 201",
    "mode": "external",
    "external_url": "https://Canvas.DKU.edu/courses/12/quizzes/3",
    "allowed_domains": ["login.microsoftonline.com", "  DKU.edu ", "", "dku.edu"],
    "finish_url_contains": "  submissions ",
    "questions": [],
}


def make(client, teacher, body=EXTERNAL_EXAM, open_=True):
    r = client.post("/api/exams", json=body, headers=teacher)
    assert r.status_code == 201, r.text
    e = r.json()
    if open_:
        e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    return e


def start(client, h, exam):
    r = client.post(f"/api/student/exams/{exam['id']}/start",
                    json={"start_code": exam["start_code"]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def submit(client, h, aid):
    r = client.post(f"/api/student/attempts/{aid}/submit",
                    json={"answers": {}, "client_seq": 0}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- validation
def test_create_external_normalises_fields(client, teacher):
    e = make(client, teacher, open_=False)
    assert e["mode"] == "external"
    assert e["external_url"] == "https://Canvas.DKU.edu/courses/12/quizzes/3"
    assert e["allowed_domains"] == ["login.microsoftonline.com", "dku.edu"]
    assert e["finish_url_contains"] == "submissions"
    # slice 4: one hidden grading item, default 100 points
    assert [q["type"] for q in e["questions"]] == ["external"]
    assert e["question_count"] == 1 and e["total_points"] == 100
    got = client.get(f"/api/exams/{e['id']}", headers=teacher).json()
    assert got["allowed_domains"] == ["login.microsoftonline.com", "dku.edu"]


def test_questions_mode_defaults_and_clears_external_fields(client, teacher):
    e = make(client, teacher, SAMPLE_EXAM, open_=False)
    assert e["mode"] == "questions"
    assert e["external_url"] is None and e["allowed_domains"] == []
    assert e["finish_url_contains"] is None
    body = {**SAMPLE_EXAM, "mode": "questions", "external_url": "https://x.org",
            "allowed_domains": ["x.org"], "finish_url_contains": "done"}
    r = client.put(f"/api/exams/{e['id']}", json=body, headers=teacher)
    assert r.status_code == 200, r.text
    assert r.json()["external_url"] is None and r.json()["allowed_domains"] == []
    # switch the draft to external and back
    r = client.put(f"/api/exams/{e['id']}", json=EXTERNAL_EXAM, headers=teacher)
    assert r.status_code == 200 and r.json()["mode"] == "external"
    assert r.json()["question_count"] == 1
    r = client.put(f"/api/exams/{e['id']}", json=SAMPLE_EXAM, headers=teacher)
    assert r.json()["mode"] == "questions" and r.json()["external_url"] is None


@pytest.mark.parametrize("patch", [
    {"external_url": None},
    {"external_url": ""},
    {"external_url": "   "},
    {"external_url": "ftp://files.dku.edu/quiz"},
    {"external_url": "javascript:alert(1)"},
    {"external_url": "canvas.dku.edu/quiz"},
    {"external_url": "https://" + "a" * 2000 + ".com"},
    {"questions": SAMPLE_EXAM["questions"]},
    {"allowed_domains": ["canvas.dku.edu/pa th"]},
    {"allowed_domains": ["*."]},
    {"allowed_domains": ["canvas"]},
    {"allowed_domains": ["bad_host.com"]},
    {"allowed_domains": ["a..b.com"]},
    {"allowed_domains": [".dku.edu"]},
    {"allowed_domains": [f"d{i}.edu" for i in range(31)]},
    {"finish_url_contains": "x" * 201},
    {"mode": "website"},
])
def test_external_validation_422(client, teacher, patch):
    body = {**copy.deepcopy(EXTERNAL_EXAM), **patch}
    r = client.post("/api/exams", json=body, headers=teacher)
    assert r.status_code == 422, (patch, r.text)


def test_questions_mode_still_needs_a_question(client, teacher):
    r = client.post("/api/exams", json={"title": "x", "duration_min": 5, "questions": []},
                    headers=teacher)
    assert r.status_code == 422


def test_localhost_and_ports_allowed(client, teacher):
    body = {**EXTERNAL_EXAM, "external_url": "http://localhost:3000/form",
            "allowed_domains": ["localhost"], "finish_url_contains": None}
    e = make(client, teacher, body, open_=False)
    assert e["allowed_domains"] == ["localhost"] and e["finish_url_contains"] is None


# ---------------------------------------------------------------- student flow
def test_student_list_start_resume_and_submit(client, teacher, alice):
    e = make(client, teacher)
    items = client.get("/api/student/exams", headers=alice).json()
    item = next(x for x in items if x["id"] == e["id"])
    assert item["mode"] == "external"

    a = start(client, alice, e)
    assert a["mode"] == "external"
    assert a["external_url"] == "https://Canvas.DKU.edu/courses/12/quizzes/3"
    # the external_url host is always allowed (first), stored entries follow
    assert a["allowed_domains"] == ["canvas.dku.edu", "login.microsoftonline.com", "dku.edu"]
    assert a["finish_url_contains"] == "submissions"
    assert a["questions"] == [] and a["answers"] == {} and a["status"] == "in_progress"

    again = client.get(f"/api/student/attempts/{a['attempt_id']}", headers=alice).json()
    assert again["mode"] == "external" and again["allowed_domains"] == a["allowed_domains"]

    hb = client.post(f"/api/student/attempts/{a['attempt_id']}/heartbeat",
                     json={"current_question": 0, "locked": True}, headers=alice)
    assert hb.status_code == 200 and hb.json()["status"] == "in_progress"
    ev = client.post(f"/api/student/attempts/{a['attempt_id']}/events",
                     json={"events": [{"type": "blocked_navigation", "detail": "https://x.com/",
                                       "client_time": "2026-10-11T10:00:00Z"}]}, headers=alice)
    assert ev.json() == {"ok": True, "stored": 1}

    s1 = submit(client, alice, a["attempt_id"])
    assert s1["status"] == "submitted" and len(s1["receipt"]) == 9
    s2 = submit(client, alice, a["attempt_id"])
    assert s2["receipt"] == s1["receipt"]  # idempotent


def test_questions_exam_view_has_null_external_fields(client, alice, live_exam):
    a = start(client, alice, live_exam)
    assert a["mode"] == "questions" and a["external_url"] is None
    assert a["allowed_domains"] == [] and a["finish_url_contains"] is None
    assert len(a["questions"]) == 3


def test_close_submits_and_heartbeat_reports_submitted(client, teacher, alice):
    e = make(client, teacher)
    a = start(client, alice, e)
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    hb = client.post(f"/api/student/attempts/{a['attempt_id']}/heartbeat",
                     json={"current_question": 0, "locked": True}, headers=alice).json()
    assert hb["status"] == "submitted"


# ---------------------------------------------------------------- teacher views (slice 4)
def test_external_points_and_teacher_scores(client, teacher, alice, bob):
    e = make(client, teacher, {**EXTERNAL_EXAM, "external_points": 20})
    assert e["total_points"] == 20
    qid = e["questions"][0]["id"]
    a = start(client, alice, e)
    submit(client, alice, a["attempt_id"])
    start(client, bob, e)  # still writing
    r = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    assert r["pending_grading"] == 1 and r["submitted_count"] == 1 and r["attempt_count"] == 2
    got = next(x for x in r["attempts"] if x["username"] == "alice")
    assert got["needs_grading"] == 1 and got["answers"][0]["pending"] is True

    # grading queue lists the submitted student for the website score
    gq = client.get(f"/api/exams/{e['id']}/grading", headers=teacher).json()
    assert [q["type"] for q in gq["questions"]] == ["external"]

    g = client.put(f"/api/exams/{e['id']}/attempts/{a['attempt_id']}/grades/{qid}",
                   json={"points": 17, "comment": "Good work", "rubric_scores": None}, headers=teacher)
    assert g.status_code == 200, g.text
    assert g.json()["score"] == 17 and g.json()["percent"] == 85.0 and g.json()["needs_grading"] == 0
    too_much = client.put(f"/api/exams/{e['id']}/attempts/{a['attempt_id']}/grades/{qid}",
                          json={"points": 21, "comment": "", "rubric_scores": None}, headers=teacher)
    assert too_much.status_code == 422


def test_external_release_and_student_feedback(client, teacher, alice, bob):
    e = make(client, teacher, {**EXTERNAL_EXAM, "external_points": 10})
    qid = e["questions"][0]["id"]
    a = start(client, alice, e)
    submit(client, alice, a["attempt_id"])
    start(client, bob, e)
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)  # auto-submits bob
    client.put(f"/api/exams/{e['id']}/attempts/{a['attempt_id']}/grades/{qid}",
               json={"points": 8, "comment": "Nice", "rubric_scores": None}, headers=teacher)
    r = client.post(f"/api/exams/{e['id']}/release", json={"force": False}, headers=teacher)
    assert r.status_code == 409  # bob still needs a score
    r = client.post(f"/api/exams/{e['id']}/release", json={"force": True}, headers=teacher)
    assert r.status_code == 200 and r.json()["grades_released"] is True

    grades = client.get("/api/student/grades", headers=alice).json()
    mine = next(x for x in grades if x["exam_id"] == e["id"])
    assert mine["released"] is True and mine["score"] == 8 and mine["percent"] == 80.0
    fb = client.get(f"/api/student/grades/{e['id']}", headers=alice).json()
    assert fb["score"] == 8 and fb["questions"][0]["comment"] == "Nice"

    gb = client.get("/api/gradebook", headers=teacher).json()
    row = next(s for s in gb["students"] if s["username"] == "alice")
    assert row["cells"][e["id"]]["state"] == "graded" and row["cells"][e["id"]]["percent"] == 80.0
    assert client.post(f"/api/exams/{e['id']}/unrelease", headers=teacher).status_code == 200


def test_student_view_never_shows_grading_item(client, teacher, alice):
    e = make(client, teacher)
    a = start(client, alice, e)
    assert a["questions"] == [] and a["answers"] == {}


def test_other_student_cannot_read_external_attempt(client, teacher, alice):
    e = make(client, teacher)
    a = start(client, alice, e)
    bob = login(client, "bob")
    r = client.get(f"/api/student/attempts/{a['attempt_id']}", headers=bob)
    assert r.status_code == 404


def test_allowed_site_grammar(client, teacher):
    """Audit #10: exact host by default, "*." for subdomains, optional path prefix."""
    r = client.post("/api/exams", headers=teacher, json={
        **EXTERNAL_EXAM, "allowed_domains": ["https://Docs.Google.com/forms/d/e/AbC/viewform?x=1",
                                        "*.DKU.edu", "canvas.dku.edu/", "dku.edu"]})
    assert r.status_code == 201, r.text
    assert r.json()["allowed_domains"] == ["docs.google.com/forms/d/e/AbC/viewform",
                                           "*.dku.edu", "canvas.dku.edu", "dku.edu"]


def test_start_url_entry_is_narrow_on_broad_hosts():
    from app.services import start_url_entry
    assert start_url_entry("https://canvas.dku.edu/courses/1/quizzes/2") == "canvas.dku.edu"
    assert start_url_entry("https://docs.google.com/forms/d/e/1FAIpQL/viewform?usp=sf") == \
        "docs.google.com/forms/d/e/1FAIpQL/"
    assert start_url_entry("https://www.google.com/") == "www.google.com"
