"""Audit 2026-10-12 S-03: classes decide who sees, downloads and takes an exam."""
import pytest

from app import routes_classes

from .conftest import SAMPLE_EXAM, login, make_class

pytestmark = pytest.mark.no_default_class
BODY = {k: v for k, v in SAMPLE_EXAM.items() if k != "class_id"}


def _exam(c, t, class_id=None, open_=True):
    body = dict(BODY) if class_id is None else {**BODY, "class_id": class_id}
    e = c.post("/api/exams", json=body, headers=t)
    assert e.status_code == 201, e.text
    e = e.json()
    if open_:
        r = c.post(f"/api/exams/{e['id']}/open", headers=t)
        assert r.status_code == 200, r.text
        e = r.json()
    return e


def _code(k):
    return k["join_code"]


def test_teacher_creates_class_and_students_join(client):
    t = login(client, "teacher")
    k = client.post("/api/classes", json={"name": "  CS  101 "}, headers=t).json()
    assert k["name"] == "CS 101" and len(k["join_code"]) == 9 and k["join_code"][4] == "-"
    alice = login(client, "alice")
    # lower case and no dash work too
    r = client.post("/api/student/classes/join", json={"code": k["join_code"].replace("-", "").lower()}, headers=alice)
    assert r.status_code == 200 and r.json()["name"] == "CS 101" and r.json()["teacher"] == "Demo Teacher"
    assert client.post("/api/student/classes/join", json={"code": _code(k)}, headers=alice).status_code == 200  # again: fine
    assert [x["name"] for x in client.get("/api/student/classes", headers=alice).json()] == ["CS 101"]
    detail = client.get(f"/api/classes/{k['id']}", headers=t).json()
    assert [m["username"] for m in detail["members"]] == ["alice"] and detail["member_count"] == 1
    # students can't manage classes; another teacher can't see this one
    assert client.get("/api/classes", headers=alice).status_code in (401, 403)


def test_add_and_remove_members_by_username(client):
    t = login(client, "teacher")
    k = client.post("/api/classes", json={"name": "Math"}, headers=t).json()
    r = client.post(f"/api/classes/{k['id']}/members", json={"usernames": ["alice", "bob", "nobody", "teacher", "alice"]},
                    headers=t).json()
    assert r == {"added": 2, "already": 0, "unknown": ["nobody", "teacher"]}
    assert client.post(f"/api/classes/{k['id']}/members", json={"usernames": ["bob"]}, headers=t).json()["already"] == 1
    assert client.delete(f"/api/classes/{k['id']}/members/bob", headers=t).status_code == 204
    assert [m["username"] for m in client.get(f"/api/classes/{k['id']}", headers=t).json()["members"]] == ["alice"]


def test_new_join_code_stops_the_old_one(client):
    t = login(client, "teacher")
    k = client.post("/api/classes", json={"name": "Physics"}, headers=t).json()
    k2 = client.post(f"/api/classes/{k['id']}/join-code", headers=t).json()
    assert k2["join_code"] != k["join_code"]
    bob = login(client, "bob")
    assert client.post("/api/student/classes/join", json={"code": k["join_code"]}, headers=bob).status_code == 404
    assert client.post("/api/student/classes/join", json={"code": k2["join_code"]}, headers=bob).status_code == 200


def test_wrong_join_codes_are_limited(client, monkeypatch):
    monkeypatch.setattr(routes_classes, "MAX_WRONG_JOINS", 3)
    bob = login(client, "bob")
    codes = [client.post("/api/student/classes/join", json={"code": "AAAA-AAAA"}, headers=bob).status_code
             for _ in range(4)]
    assert codes == [404, 404, 404, 429]


def test_exam_needs_a_class_to_open(client):
    t = login(client, "teacher")
    e = _exam(client, t, open_=False)
    r = client.post(f"/api/exams/{e['id']}/open", headers=t)
    assert r.status_code == 409 and "class" in r.json()["detail"]
    k = make_class(client, t, members=[])
    assert client.put(f"/api/exams/{e['id']}", json={**BODY, "class_id": k["id"]}, headers=t).json()["class_id"] == k["id"]
    assert client.post(f"/api/exams/{e['id']}/open", headers=t).status_code == 200


def test_class_must_be_the_teachers_own(client):
    t = login(client, "teacher")
    k = make_class(client, t)
    other = client.post("/api/auth/login", json={"username": "teacher", "password": "teacher123"})
    assert other.status_code == 200
    assert client.post("/api/exams", json={**BODY, "class_id": k["id"] + 999}, headers=t).status_code == 422


def test_only_class_members_see_download_and_start(client):
    t = login(client, "teacher")
    cs = make_class(client, t, "CS", members=["alice"])
    math = make_class(client, t, "Math", members=["bob"])
    e = _exam(client, t, cs["id"])
    alice, bob = login(client, "alice"), login(client, "bob")
    ids = lambda h: [x["id"] for x in client.get("/api/student/exams", headers=h).json()]  # noqa: E731
    assert e["id"] in ids(alice) and e["id"] not in ids(bob)
    assert client.get(f"/api/student/exams/{e['id']}/package", headers=alice).status_code == 200
    assert client.get(f"/api/student/exams/{e['id']}/package-key", headers=alice).status_code == 200
    for url in ("package", "package-key", "challenge"):
        r = client.get(f"/api/student/exams/{e['id']}/{url}", headers=bob)
        assert r.status_code == 403 and "CS students" in r.json()["detail"], url
    r = client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]}, headers=bob)
    assert r.status_code == 403
    # bob joins the class -> he sees it and can start
    client.post("/api/student/classes/join", json={"code": cs["join_code"]}, headers=bob)
    assert e["id"] in ids(bob)
    assert client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]},
                       headers=bob).status_code == 200
    # leaving later keeps his own attempt visible
    assert client.delete(f"/api/student/classes/{cs['id']}", headers=bob).status_code == 204
    assert e["id"] in ids(bob)
    assert math["id"] != cs["id"]


def test_package_key_only_while_live_and_reset_on_close(client):
    t = login(client, "teacher")
    k = make_class(client, t, members=["alice"])
    e = _exam(client, t, k["id"])
    alice = login(client, "alice")
    k1 = client.get(f"/api/student/exams/{e['id']}/package-key", headers=alice).json()["key"]
    assert client.get(f"/api/student/exams/{e['id']}/package-key", headers=alice).json()["key"] == k1
    client.post(f"/api/exams/{e['id']}/close", headers=t)
    assert client.get(f"/api/student/exams/{e['id']}/package-key", headers=alice).status_code == 409
    client.post(f"/api/exams/{e['id']}/open", headers=t)
    k2 = client.get(f"/api/student/exams/{e['id']}/package-key", headers=alice).json()["key"]
    assert k2 != k1


def test_delete_class_only_without_exams(client):
    t = login(client, "teacher")
    k = make_class(client, t)
    e = _exam(client, t, k["id"], open_=False)
    assert client.delete(f"/api/classes/{k['id']}", headers=t).status_code == 409
    client.delete(f"/api/exams/{e['id']}", headers=t)
    assert client.delete(f"/api/classes/{k['id']}", headers=t).status_code == 204
    assert client.get("/api/classes", headers=t).json() == []


def test_dev_server_gets_a_demo_class(tmp_path, static_dir):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'd.db'}", redis_url=None, dev=True,
                 pbkdf2_iterations=1000, static_dir=str(static_dir), uploads_dir=str(tmp_path / "up"))
    with TestClient(create_app(s)) as c:
        t = login(c, "teacher")
        e = c.post("/api/exams", json=BODY, headers=t).json()      # an exam from before classes
    with TestClient(create_app(s)) as c:                              # restart
        t = login(c, "teacher")
        ks = c.get("/api/classes", headers=t).json()
        assert [k["name"] for k in ks] == ["Demo class"] and ks[0]["member_count"] == 3
        assert c.get(f"/api/exams/{e['id']}", headers=t).json()["class_id"] == ks[0]["id"]
