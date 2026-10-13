"""2026-10-13 simplification: optional parts off by default, class cards (term, teacher),
a class's marks, and exams saved without a class."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from .conftest import SAMPLE_EXAM, login

pytestmark = pytest.mark.no_default_class
BODY = {k: v for k, v in SAMPLE_EXAM.items() if k != "class_id"}


def _settings(tmp_path, static_dir, features):
    return Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'f.db'}", redis_url=None, dev=True,
                    pbkdf2_iterations=1000, static_dir=str(static_dir), uploads_dir=str(tmp_path / "up"),
                    features=features)


def test_optional_parts_are_off_unless_listed(tmp_path, static_dir, monkeypatch):
    monkeypatch.setenv("FAIRPLAY_FEATURES", "")
    assert Settings().features == []
    monkeypatch.setenv("FAIRPLAY_FEATURES", "scan, nonsense")
    assert Settings().features == ["scan"]
    with TestClient(create_app(_settings(tmp_path, static_dir, []))) as c:
        assert c.get("/api/health").json()["features"] == []
        t = login(c, "teacher")
        for path in ("/api/lms/connections", "/api/practice/students", "/api/scan/exams"):
            assert c.get(path, headers=t).status_code == 404, path
        r = c.post("/api/exams", json={**BODY, "delivery": "paper"}, headers=t)
        assert r.status_code == 400 and "switched off" in r.json()["detail"]
        assert c.post("/api/exams", json=BODY, headers=t).status_code == 201


def test_class_card_has_term_and_teacher(client):
    t = login(client, "teacher")
    k = client.post("/api/classes", json={"name": "Calculus I", "term": "  Fall 2026 ·  Section 2 "}, headers=t).json()
    assert k["term"] == "Fall 2026 · Section 2" and k["teacher_name"] == "Demo Teacher"
    assert client.get("/api/classes", headers=t).json()[0]["term"] == "Fall 2026 · Section 2"
    r = client.put(f"/api/classes/{k['id']}", json={"name": "Calculus I", "term": ""}, headers=t).json()
    assert r["term"] == ""
    r = client.put(f"/api/classes/{k['id']}", json={"name": "Calc I"}, headers=t).json()   # term kept
    assert r["name"] == "Calc I" and r["term"] == ""
    alice = login(client, "alice")
    client.put(f"/api/classes/{k['id']}", json={"name": "Calc I", "term": "Spring"}, headers=t)
    client.post("/api/student/classes/join", json={"code": k["join_code"]}, headers=alice)
    assert client.get("/api/student/classes", headers=alice).json()[0]["term"] == "Spring"


def test_class_marks_list_members_and_only_that_class(client):
    t = login(client, "teacher")
    a = client.post("/api/classes", json={"name": "A"}, headers=t).json()
    b = client.post("/api/classes", json={"name": "B"}, headers=t).json()
    client.post(f"/api/classes/{a['id']}/members", json={"usernames": ["alice", "bob"]}, headers=t)
    client.post(f"/api/classes/{b['id']}/members", json={"usernames": ["carol"]}, headers=t)
    ea = client.post("/api/exams", json={**BODY, "class_id": a["id"]}, headers=t).json()
    eb = client.post("/api/exams", json={**BODY, "class_id": b["id"]}, headers=t).json()
    for e in (ea, eb):
        assert client.post(f"/api/exams/{e['id']}/open", headers=t).status_code == 200
    g = client.get(f"/api/gradebook?class_id={a['id']}", headers=t).json()
    assert [x["exam_id"] for x in g["exams"]] == [ea["id"]]
    assert sorted(s["username"] for s in g["students"]) == ["alice", "bob"]
    # a class with no open exam still lists its students
    c = client.post("/api/classes", json={"name": "C"}, headers=t).json()
    client.post(f"/api/classes/{c['id']}/members", json={"usernames": ["carol"]}, headers=t)
    g = client.get(f"/api/gradebook?class_id={c['id']}", headers=t).json()
    assert g["exams"] == [] and [s["username"] for s in g["students"]] == ["carol"]
    # someone else's class: not found
    assert client.get("/api/gradebook?class_id=999999", headers=t).status_code == 404


def test_exam_saves_without_class_but_needs_one_to_open(client):
    t = login(client, "teacher")
    e = client.post("/api/exams", json=BODY, headers=t)
    assert e.status_code == 201 and e.json()["class_id"] is None
    r = client.post(f"/api/exams/{e.json()['id']}/open", headers=t)
    assert r.status_code in (400, 409)
    k = client.post("/api/classes", json={"name": "K"}, headers=t).json()
    assert client.put(f"/api/exams/{e.json()['id']}", json={**BODY, "class_id": k["id"]}, headers=t).status_code == 200
    assert client.post(f"/api/exams/{e.json()['id']}/open", headers=t).status_code == 200
