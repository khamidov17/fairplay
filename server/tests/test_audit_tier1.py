"""Audit 2026-10-12 tier 1: S-01 demo logins, S-02 late submit, S-04 huge numbers,
S-05 sign-in limits."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.grading import _number
from app.main import create_app

from .conftest import SAMPLE_EXAM, login


def _settings(tmp_path, static_dir, **kw):
    base = dict(database_url=f"sqlite+aiosqlite:///{tmp_path / 'a.db'}", redis_url=None,
                secret="test-secret-0123456789abcdef", pbkdf2_iterations=1000,
                require_app=False, app_keys=["test-app-key"], dev=False, seed_loadtest_users=0, single_process=True,
                static_dir=str(static_dir), uploads_dir=str(tmp_path / "up"))
    base.update(kw)
    return Settings(**base)


# ---------------------------------------------------------------- S-01 demo logins
def test_real_server_has_no_demo_logins(tmp_path, static_dir):
    with TestClient(create_app(_settings(tmp_path, static_dir))) as c:
        assert c.get("/api/health").json()["demo_logins"] is False
        r = c.post("/api/auth/login", json={"username": "teacher", "password": "teacher123"})
        assert r.status_code == 401


def test_dev_server_has_demo_logins(tmp_path, static_dir):
    with TestClient(create_app(_settings(tmp_path, static_dir, dev=True, app_keys=[]))) as c:
        assert c.get("/api/health").json()["demo_logins"] is True
        login(c, "teacher")


def test_real_server_refuses_a_left_over_demo_password(tmp_path, static_dir):
    # A database first used as a demo (seeded), then started for real.
    with TestClient(create_app(_settings(tmp_path, static_dir, seed_demo=True))):
        pass
    # Checked directly (a startup error inside TestClient's lifespan can hang some
    # starlette versions instead of raising).
    import asyncio
    from app.db import Database
    from app.seed import refuse_public_demo_logins
    s = _settings(tmp_path, static_dir)

    async def go():
        db = Database(s)
        try:
            await refuse_public_demo_logins(db, s)
        finally:
            await db.dispose()
    with pytest.raises(RuntimeError, match="demo account '\w+' still has its public password"):
        asyncio.run(go())


# ---------------------------------------------------------------- S-02 late submit
def _late_attempt(client, teacher, student):
    """An attempt whose time ran out ~29 minutes ago (offline start, 1-minute exam)."""
    e = client.post("/api/exams", json={**SAMPLE_EXAM, "duration_min": 1}, headers=teacher).json()
    e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    a = client.post(f"/api/student/exams/{e['id']}/start", headers=student,
                    json={"start_code": e["start_code"],
                          "offline_started_at": started.replace(microsecond=0).isoformat() + "Z"}).json()
    return e, a


def test_submit_after_time_keeps_the_saved_answers(client, teacher, alice):
    e, a = _late_attempt(client, teacher, alice)
    q_mcq, q_num = e["questions"][0]["id"], e["questions"][1]["id"]
    aid = a["attempt_id"]
    # saving is closed after time; the submit carries new (correct) answers
    r = client.post(f"/api/student/attempts/{aid}/submit", headers=alice,
                    json={"answers": {q_mcq: 1, q_num: 3.14}, "client_seq": 9})
    assert r.status_code == 200 and r.json()["receipt"]
    res = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    att = next(x for x in res["attempts"] if x["attempt_id"] == aid)
    assert att["late"] is True
    assert att["score"] == 0                         # graded on what was saved in time (nothing)
    assert att["late_answers"] == {q_mcq: 1, q_num: 3.14}
    types = [ev["type"] for ev in att["events"]]
    assert "late_submission" in types
    detail = next(ev["detail"] for ev in att["events"] if ev["type"] == "late_submission")
    assert "2 answer(s) sent after time were not graded" in detail
    live = client.get(f"/api/exams/{e['id']}/live", headers=teacher).json()
    assert next(s for s in live["students"] if s["attempt_id"] == aid)["late"] is True
    gb = client.get("/api/gradebook", headers=teacher).json()
    cell = next(s for s in gb["students"] if s["username"] == "alice")["cells"][e["id"]]
    assert cell["late"] is True


def test_submit_in_time_still_uses_the_body(client, teacher, alice, live_exam):
    q = live_exam["questions"][0]["id"]
    a = client.post(f"/api/student/exams/{live_exam['id']}/start", headers=alice,
                    json={"start_code": live_exam["start_code"]}).json()
    client.post(f"/api/student/attempts/{a['attempt_id']}/submit", headers=alice,
                json={"answers": {q: 1}, "client_seq": 2})
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    att = res["attempts"][0]
    assert att["late"] is False and att["late_answers"] == {} and att["score"] == 2


# ---------------------------------------------------------------- S-04 huge numbers
def test_number_overflow_is_not_a_crash():
    assert _number(10 ** 400) is None
    assert _number("1e400") is None          # float("1e400") is inf -> None
    assert _number(3) == 3.0


@pytest.mark.parametrize("bad", ["10" * 200, "NaN", "Infinity"])
def test_answers_reject_non_finite_and_huge_numbers(client, alice, live_exam, bad):
    a = client.post(f"/api/student/exams/{live_exam['id']}/start", headers=alice,
                    json={"start_code": live_exam["start_code"]}).json()
    q = live_exam["questions"][1]["id"]
    raw = '{"answers": {"%s": %s}, "client_seq": 1}' % (q, bad)
    r = client.put(f"/api/student/attempts/{a['attempt_id']}/answers", headers={
        **alice, "Content-Type": "application/json"}, content=raw)
    assert r.status_code == 422


def test_huge_number_already_stored_does_not_break_close(client, teacher, alice, live_exam):
    """Rows saved before the fix: grading must still work."""
    from sqlalchemy import update
    from app import db as dbmod
    from app.models import Attempt
    a = client.post(f"/api/student/exams/{live_exam['id']}/start", headers=alice,
                    json={"start_code": live_exam["start_code"]}).json()
    q = live_exam["questions"][1]["id"]

    async def poke():
        async with dbmod._db.sessionmaker() as s:
            await s.execute(update(Attempt).where(Attempt.id == a["attempt_id"])
                            .values(answers={q: 10 ** 400}))
            await s.commit()
    client.portal.call(poke)
    assert client.post(f"/api/exams/{live_exam['id']}/close", headers=teacher).status_code == 200
    assert client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).status_code == 200
    assert client.get("/api/gradebook", headers=teacher).status_code == 200


# ---------------------------------------------------------------- S-05 sign-in limits
def test_wrong_passwords_for_one_account_are_throttled(client):
    for _ in range(10):
        r = client.post("/api/auth/login", json={"username": "alice", "password": "nope"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "alice", "password": "alice123"})
    assert r.status_code == 429
    login(client, "bob")                     # other accounts are not affected


def test_unknown_user_is_throttled_like_a_real_one(client):
    for _ in range(10):
        assert client.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code == 429


def test_per_ip_limit(tmp_path, static_dir):
    with TestClient(create_app(_settings(tmp_path, static_dir, seed_demo=True, login_ip_limit=3))) as c:
        for _ in range(3):
            login(c, "alice")
        assert c.post("/api/auth/login", json={"username": "bob", "password": "bob123"}).status_code == 429
