"""Phase 1a: ownership checks. Another teacher must not read or change a teacher's exams,
attempts, grades, scans or timelines; a student must not touch another student's attempt.
Routes are enumerated from the app, so endpoints added later are covered too."""
import re

import pytest

from .conftest import SAMPLE_EXAM, login
from .test_phase1a_security import add_user

# Bodies for routes that validate a body before the ownership check runs.
BODIES = {
    ("PUT", "/api/exams/{exam_id}"): SAMPLE_EXAM,
    ("POST", "/api/exams/{exam_id}/extend"): {"minutes": 5},
    ("POST", "/api/exams/{exam_id}/events/{event_id}/review"): {"status": "dismissed"},
    ("PUT", "/api/exams/{exam_id}/attempts/{aid}/grades/{qid}"): {"points": 1, "comment": ""},
    ("PUT", "/api/exams/{exam_id}/attempts/{aid}/comment"): {"comment": "x"},
    ("PUT", "/api/exams/{exam_id}/lms"): {"course_link_id": None},
    ("PUT", "/api/exams/{exam_id}/roster"): {"usernames": []},
    ("PUT", "/api/exams/{exam_id}/print-settings"): {"answer_lines": {}},
    ("POST", "/api/exams/{exam_id}/scans/pages/{page_id}/assign"): {"student_id": 2, "page_no": 1},
    ("POST", "/api/exams/{exam_id}/scan/answers/{aid}/{qid}/approve"): {"points": 1},
    ("POST", "/api/exams/{exam_id}/scan/approve-bulk"): {"items": [{"attempt_id": "a_x", "question_id": "q"}]},
    ("PUT", "/api/student/attempts/{aid}/answers"): {"answers": {}, "client_seq": 10},
    ("POST", "/api/student/attempts/{aid}/heartbeat"): {"current_question": 0, "locked": False},
    ("POST", "/api/student/attempts/{aid}/events"): {"events": [{"type": "x", "detail": ""}]},
    ("POST", "/api/student/attempts/{aid}/submit"): {"answers": {}, "client_seq": 10},
    ("POST", "/api/student/attempts/{aid}/exit-code"): {"code": "ABCD-EFGH"},
}


@pytest.fixture
def world(client, teacher, alice, tmp_path):
    add_user(tmp_path, "teacher2", "teacher")
    t2 = login(client, "teacher2")
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    a = client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]},
                    headers=alice).json()
    aid = a["attempt_id"]
    client.post(f"/api/student/attempts/{aid}/events",
                json={"events": [{"type": "blocked_shortcut", "detail": "Cmd-Tab"}]}, headers=alice)
    client.put(f"/api/student/attempts/{aid}/answers",
               json={"answers": {e["questions"][0]["id"]: 1}, "client_seq": 1}, headers=alice)
    res = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    event_id = res["attempts"][0]["events"][0]["event_id"]
    return {"t2": t2, "exam": e, "aid": aid, "qid": e["questions"][0]["id"], "event_id": event_id}


def _routes(client, prefix_re):
    for path, ops in client.app.openapi()["paths"].items():
        if re.match(prefix_re, path):
            for m in sorted(ops):
                yield m.upper(), path


def _fill(path, w):
    return (path.replace("{exam_id}", w["exam"]["id"]).replace("{aid}", w["aid"])
            .replace("{attempt_id}", w["aid"]).replace("{qid}", w["qid"])
            .replace("{event_id}", str(w["event_id"])).replace("{page_id}", "1")
            .replace("{batch_id}", "b_x"))


def test_other_teacher_cannot_reach_any_exam_route(client, teacher, world):
    checked = 0
    for method, path in _routes(client, r"^/api/exams/\{exam_id\}"):
        body = BODIES.get((method, path))
        r = client.request(method, _fill(path, world), headers=world["t2"], json=body)
        if r.status_code == 422 and body is None:
            continue  # body validated before ownership; nothing was read or changed
        assert r.status_code == 404, (method, path, r.status_code, r.text[:200])
        checked += 1
    assert checked >= 25
    # and the owner still sees everything unchanged
    e = client.get(f"/api/exams/{world['exam']['id']}", headers=teacher).json()
    assert e["status"] == "live" and e["title"] == "Midterm"
    assert world["exam"]["id"] not in [x["id"] for x in client.get("/api/exams", headers=world["t2"]).json()]
    gb = client.get("/api/gradebook", headers=world["t2"]).json()
    assert world["exam"]["id"] not in str(gb)


def test_other_student_cannot_reach_attempt_routes(client, world):
    bob = login(client, "bob")
    checked = 0
    for method, path in _routes(client, r"^/api/student/attempts/\{aid\}"):
        r = client.request(method, _fill(path, world), headers=bob, json=BODIES.get((method, path)))
        assert r.status_code == 404, (method, path, r.status_code, r.text[:200])
        checked += 1
    assert checked >= 6
    assert client.get(f"/api/student/grades/{world['exam']['id']}", headers=bob).status_code == 404


def test_students_cannot_use_teacher_routes(client, world, alice):
    for method, path in _routes(client, r"^/api/exams/\{exam_id\}"):
        r = client.request(method, _fill(path, world), headers=alice, json=BODIES.get((method, path)))
        assert r.status_code in (403, 422), (method, path, r.status_code)
        if r.status_code == 422:
            assert BODIES.get((method, path)) is None


def test_teachers_cannot_use_student_routes(client, world, teacher):
    for method, path in _routes(client, r"^/api/student/attempts/\{aid\}"):
        r = client.request(method, _fill(path, world), headers=teacher, json=BODIES.get((method, path)))
        assert r.status_code == 403, (method, path, r.status_code)


def test_timeline_refused_for_other_teacher_and_students(client, teacher, alice, world):
    url = f"/api/exams/{world['exam']['id']}/attempts/{world['aid']}/timeline"
    assert client.get(url, headers=teacher).status_code == 200
    assert client.get(url, headers=world["t2"]).status_code == 404
    assert client.get(url, headers=alice).status_code == 403
    # an attempt id from another exam is not served under this exam
    e2 = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    assert client.get(f"/api/exams/{e2['id']}/attempts/{world['aid']}/timeline",
                      headers=teacher).status_code == 404


def test_uploads_need_a_teacher_and_are_not_listable(client, alice, world):
    import io
    r = client.post("/api/uploads", files={"file": ("a.png", io.BytesIO(b"x"), "image/png")}, headers=alice)
    assert r.status_code == 403
    assert client.get("/api/uploads", headers=world["t2"]).status_code in (404, 405)
    assert client.get("/api/uploads/" + "0" * 32).status_code == 404
