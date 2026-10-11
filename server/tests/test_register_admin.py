"""Self-registration, login by email, account status, and the superadmin dashboard API."""
import pytest

from app import routes_register

from .conftest import SAMPLE_EXAM, login


def reg(client, **over):
    body = {"name": "Dana Teach", "email": "dana@uni.edu", "username": "dana",
            "password": "s3cret-pass", "role": "teacher", **over}
    return client.post("/api/auth/register", json=body)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(client):
    return login(client, "admin")


# ------------------------------------------------------------------ registration

def test_register_teacher_and_student_happy_path(client):
    r = reg(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "active" and body["role"] == "teacher" and body["token"]
    me = client.get("/api/me", headers=auth(body["token"])).json()
    assert me == {"username": "dana", "name": "Dana Teach", "role": "teacher"}
    # The token works on teacher endpoints.
    assert client.get("/api/exams", headers=auth(body["token"])).status_code == 200

    r = reg(client, name="  Sam   Student ", email="Sam@Uni.EDU", username="Sam_1",
            role="student")
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "student" and r.json()["name"] == "Sam Student"
    # Username/email are stored lowercase; login by either, any case.
    for ident in ("sam_1", "SAM_1", "sam@uni.edu", "  SAM@uni.edu "):
        r = client.post("/api/auth/login", json={"username": ident, "password": "s3cret-pass"})
        assert r.status_code == 200, ident
        assert r.json()["role"] == "student"


def test_register_duplicates_case_insensitive(client):
    assert reg(client).status_code == 201
    r = reg(client, username="DANA", email="other@uni.edu")
    assert r.status_code == 409 and "username" in r.json()["detail"]
    r = reg(client, username="dana2", email="DANA@uni.edu")
    assert r.status_code == 409 and "email" in r.json()["detail"]
    # Seeded usernames are taken too.
    assert reg(client, username="Teacher", email="t2@uni.edu").status_code == 409


@pytest.mark.parametrize("password", ["short1", "allletters", "12345678", "dana-is-me1",
                                      "aaaa1111"])
def test_register_weak_password(client, password):
    r = reg(client, password=password)
    assert r.status_code == 422, password
    assert "assword" in r.json()["detail"]


@pytest.mark.parametrize("field,value", [("name", "D"), ("name", "x" * 101),
                                         ("email", "not-an-email"), ("username", "ab"),
                                         ("username", "has space"), ("username", "a@b.c"),
                                         ("role", "superadmin")])
def test_register_invalid_fields(client, field, value):
    assert reg(client, **{field: value}).status_code == 422


def test_teacher_signup_approval_mode(client, admin):
    client.app.state.settings.teacher_signup = "approval"
    r = reg(client)
    assert r.status_code == 201
    assert r.json()["status"] == "pending" and r.json()["token"] is None
    r = client.post("/api/auth/login", json={"username": "dana", "password": "s3cret-pass"})
    assert r.status_code == 403 and "approval" in r.json()["detail"]
    # A wrong password still says "invalid" (no status leak).
    r = client.post("/api/auth/login", json={"username": "dana", "password": "wrong-pass1"})
    assert r.status_code == 401
    # Students are not affected by the teacher setting.
    assert reg(client, username="stu", email="stu@uni.edu", role="student").json()["status"] == "active"

    teachers = client.get("/api/admin/teachers", headers=admin).json()
    assert teachers[0]["username"] == "dana" and teachers[0]["status"] == "pending"
    r = client.post(f"/api/admin/teachers/{teachers[0]['id']}/approve", headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "active"
    r = client.post("/api/auth/login", json={"username": "dana@uni.edu", "password": "s3cret-pass"})
    assert r.status_code == 200 and r.json()["role"] == "teacher"


def test_teacher_signup_closed(client):
    client.app.state.settings.teacher_signup = "closed"
    assert reg(client).status_code == 403
    assert reg(client, role="student").status_code == 201


def test_register_throttle(client, monkeypatch):
    monkeypatch.setattr(routes_register, "REGISTER_LIMIT", 2)
    assert reg(client, username="user1", email="user1@x.org").status_code == 201
    assert reg(client, username="user2", email="user2@x.org").status_code == 201
    assert reg(client, username="user3", email="user3@x.org").status_code == 429


def test_disabled_login_and_last_login(client, admin):
    tid = next(t["id"] for t in client.get("/api/admin/teachers", headers=admin).json()
               if t["username"] == "teacher")
    login(client, "teacher")
    t = next(t for t in client.get("/api/admin/teachers", headers=admin).json() if t["id"] == tid)
    assert t["last_login_at"] is not None
    r = client.post(f"/api/admin/teachers/{tid}/disable", headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    r = client.post("/api/auth/login", json={"username": "teacher", "password": "teacher123"})
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    assert client.post(f"/api/admin/teachers/{tid}/approve", headers=admin).status_code == 409
    r = client.post(f"/api/admin/teachers/{tid}/enable", headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "active"
    login(client, "teacher")
    assert client.post(f"/api/admin/teachers/{tid}/enable", headers=admin).status_code == 409


# ------------------------------------------------------------------ superadmin

def test_superadmin_login_and_roles(client, admin, teacher, alice):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.json()["role"] == "superadmin"
    assert client.get("/api/me", headers=admin).json()["role"] == "superadmin"
    paths = [("GET", "/api/admin/stats"), ("GET", "/api/admin/teachers"),
             ("POST", "/api/admin/teachers/1/approve"), ("POST", "/api/admin/teachers/1/disable"),
             ("POST", "/api/admin/teachers/1/enable")]
    for method, path in paths:
        for who in (teacher, alice):
            assert client.request(method, path, headers=who).status_code == 403, path
        assert client.request(method, path).status_code == 401, path
    # The superadmin is not a teacher or a student.
    assert client.get("/api/exams", headers=admin).status_code == 403
    assert client.get("/api/student/exams", headers=admin).status_code == 403
    # Only teachers can be managed here.
    alice_id = 2  # seed order: teacher, alice, ...
    assert client.post(f"/api/admin/teachers/{alice_id}/disable", headers=admin).status_code == 404
    assert client.post("/api/admin/teachers/99999/disable", headers=admin).status_code == 404


def test_admin_stats_known_fixture(client, admin, teacher, alice, bob):
    # Second teacher (registered) with one draft exam; a pending third teacher.
    t2 = auth(reg(client).json()["token"])
    client.post("/api/exams", json=SAMPLE_EXAM, headers=t2)
    client.app.state.settings.teacher_signup = "approval"
    reg(client, username="pend", email="pend@uni.edu")
    reg(client, username="newstu", email="newstu@uni.edu", role="student")

    # Demo teacher: one exam held, alice and bob submit; one draft never opened.
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher)
    e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    for who in (alice, bob):
        a = client.post(f"/api/student/exams/{e['id']}/start",
                        json={"start_code": e["start_code"]}, headers=who).json()
        r = client.post(f"/api/student/attempts/{a['attempt_id']}/submit",
                        json={"answers": {}, "client_seq": 1}, headers=who)
        assert r.status_code == 200, r.text

    s = client.get("/api/admin/stats", headers=admin).json()
    assert s["teachers"] == {"active": 2, "pending": 1, "disabled": 0, "total": 3}
    assert s["students"] == 4  # alice, bob, carol, newstu
    assert s["exams_created"] == 3
    assert s["exams_held"] == 1
    assert s["attempts_submitted"] == 2
    assert s["active_teachers_7d"] == 2 and s["active_teachers_30d"] == 2

    rows = {t["username"]: t for t in client.get("/api/admin/teachers", headers=admin).json()}
    assert set(rows) == {"teacher", "dana", "pend"}
    assert next(iter(client.get("/api/admin/teachers", headers=admin).json()))["username"] == "pend"
    assert rows["teacher"]["exams"] == 2 and rows["teacher"]["students_reached"] == 2
    assert rows["dana"]["exams"] == 1 and rows["dana"]["students_reached"] == 0
    assert rows["dana"]["email"] == "dana@uni.edu" and rows["teacher"]["email"] is None
    assert rows["pend"]["status"] == "pending" and rows["pend"]["last_login_at"] is None


def test_active_teacher_window(client, admin, teacher):
    """Activity older than the window does not count; grading after submit does."""
    from datetime import timedelta

    from sqlalchemy import update

    from app.db import get_database
    from app.models import Exam
    from app.timeutil import utcnow

    client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher)

    async def age(days):
        async with get_database().sessionmaker() as s:
            old = utcnow() - timedelta(days=days)
            await s.execute(update(Exam).values(created_at=old, updated_at=old))
            await s.commit()

    client.portal.call(age, 10)
    s = client.get("/api/admin/stats", headers=admin).json()
    assert s["active_teachers_7d"] == 0 and s["active_teachers_30d"] == 1
    client.portal.call(age, 40)
    s = client.get("/api/admin/stats", headers=admin).json()
    assert s["active_teachers_30d"] == 0
