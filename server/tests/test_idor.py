"""Audit 2026-10-12 T-01: another teacher can't touch this teacher's exams, attempts,
grades, events, classes, scans, practice sets or LMS links (always 404/403, never 2xx)."""
import pytest
from sqlalchemy import create_engine, text

from app.security import hash_password

from .conftest import SAMPLE_EXAM, login


@pytest.fixture
def teacher2(client, tmp_path):
    from app.db import get_database
    url = str(get_database().engine.url).replace("sqlite+aiosqlite", "sqlite")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("insert into users (username, name, role, password_hash, created_at, token_version) "
                       "values ('teacher2', 'Other Teacher', 'teacher', :h, CURRENT_TIMESTAMP, 0)"),
                  {"h": hash_password("other-pass-123", 1000)})
    eng.dispose()
    return login(client, "teacher2", "other-pass-123")


def _world(client, teacher, alice):
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    a = client.post(f"/api/student/exams/{e['id']}/start", headers=alice,
                    json={"start_code": e["start_code"]}).json()
    client.post(f"/api/student/attempts/{a['attempt_id']}/events", headers=alice,
                json={"events": [{"type": "focus_lost", "detail": "", "client_time": "t"}]})
    res = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    ev = res["attempts"][0]["events"][0]["event_id"]
    k = client.get("/api/classes", headers=teacher).json()[0]
    return e, a["attempt_id"], ev, k


def test_other_teacher_gets_nothing(client, teacher, alice, teacher2):
    e, aid, ev, k = _world(client, teacher, alice)
    eid, q = e["id"], e["questions"][0]["id"]
    calls = [
        ("get", f"/api/exams/{eid}", None), ("put", f"/api/exams/{eid}", SAMPLE_EXAM),
        ("delete", f"/api/exams/{eid}", None), ("post", f"/api/exams/{eid}/open", None),
        ("post", f"/api/exams/{eid}/close", None), ("post", f"/api/exams/{eid}/rotate-code", None),
        ("post", f"/api/exams/{eid}/extend", {"minutes": 10}),
        ("get", f"/api/exams/{eid}/live", None), ("get", f"/api/exams/{eid}/results", None),
        ("get", f"/api/exams/{eid}/grading", None),
        ("post", f"/api/exams/{eid}/attempts/{aid}/release-device", None),
        ("post", f"/api/exams/{eid}/attempts/{aid}/offline-exit", {"mac_code": "K7QM-3XPA"}),
        ("get", f"/api/exams/{eid}/attempts/{aid}/events", None),
        ("post", f"/api/exams/{eid}/events/{ev}/review", {"status": "dismissed", "note": ""}),
        ("put", f"/api/exams/{eid}/attempts/{aid}/grades/{q}", {"points": 1, "comment": ""}),
        ("delete", f"/api/exams/{eid}/attempts/{aid}/grades/{q}", None),
        ("put", f"/api/exams/{eid}/attempts/{aid}/comment", {"comment": "x"}),
        ("post", f"/api/exams/{eid}/release", None), ("post", f"/api/exams/{eid}/unrelease", None),
        ("get", f"/api/exams/{eid}/roster", None), ("put", f"/api/exams/{eid}/roster", {"usernames": []}),
        ("get", f"/api/exams/{eid}/scans", None), ("get", f"/api/exams/{eid}/scan/queue", None),
        ("put", f"/api/exams/{eid}/lms", {"course_link_id": None}),
        ("get", f"/api/classes/{k['id']}", None), ("put", f"/api/classes/{k['id']}", {"name": "x"}),
        ("post", f"/api/classes/{k['id']}/join-code", None),
        ("post", f"/api/classes/{k['id']}/members", {"usernames": ["bob"]}),
        ("delete", f"/api/classes/{k['id']}/members/alice", None),
        ("post", f"/api/classes/{k['id']}/members/alice/sign-out", None),
        ("delete", f"/api/classes/{k['id']}", None),
    ]
    for method, url, body in calls:
        kw = {"json": body} if body is not None else {}
        r = getattr(client, method)(url, headers=teacher2, **kw)
        assert r.status_code in (403, 404, 422), (method, url, r.status_code, r.text[:200])
    # and nothing changed for the owner
    still = client.get(f"/api/exams/{eid}", headers=teacher).json()
    assert still["status"] == "live" and still["start_code"] == e["start_code"]
    assert client.get(f"/api/exams/{eid}/results", headers=teacher).json()["attempts"][0]["events"][0]["review"] == "open"
    assert [m["username"] for m in client.get(f"/api/classes/{k['id']}", headers=teacher).json()["members"]] \
        == ["alice", "bob", "carol"]
    # the other teacher's own lists don't include any of it
    assert client.get("/api/exams", headers=teacher2).json() == []
    assert client.get("/api/classes", headers=teacher2).json() == []


def test_students_cant_use_teacher_routes(client, teacher, alice):
    e, aid, ev, k = _world(client, teacher, alice)
    for url in (f"/api/exams/{e['id']}", f"/api/exams/{e['id']}/results", f"/api/classes/{k['id']}",
                "/api/gradebook", "/api/practice/sets"):
        assert client.get(url, headers=alice).status_code == 403, url
