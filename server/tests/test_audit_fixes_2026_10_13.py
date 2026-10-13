"""Security audit 2026-10-13 (Cloudflare security-audit skill, student side): fixes.

- wrong-code lockouts reserve the try before comparing (start code, invigilator code)
- signed invigilator-code reply
- event cap reserved atomically
- submit does not overwrite a newer PUT (client_seq guard)
- answers only for the exam's own questions; per-attempt answer write budget
- challenge budget; hand-in session header checked; API explorer off outside dev
- offline package v3 (600k iterations in the package), one package key per run,
  single-flight package build
- practice numeric answers: huge numbers are "not a number", not a 500
"""
import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest

from app import routes_student as rs
from app.app_client import exit_message, sign
from app.store import MemoryStore

from .conftest import SAMPLE_EXAM, login
from .test_app_session import KEY, app_client, exam, start  # noqa: F401  (fixtures)


# ---------------------------------------------------------------- lockouts
def test_parallel_wrong_codes_compare_at_most_the_limit():
    """The audit replay: 200 parallel guesses with an await between the lock check and the
    compare all got compared. Now at most MAX of them reach the compare."""
    store = MemoryStore()
    compared = 0

    async def guess():
        nonlocal compared
        try:
            n = await rs.take_code_try(store, "lock", "count", 5, 600, "wait")
        except Exception:   # noqa: BLE001  429
            return
        await asyncio.sleep(0)          # the DB read in exit_code
        await asyncio.sleep(0)
        compared += 1
        await rs.wrong_code(store, "lock", "count", n, 5, 300)

    async def go():
        await asyncio.gather(*(guess() for _ in range(200)))
    asyncio.run(go())
    assert compared == 5
    assert asyncio.run(store.get("lock")) == "1"


def test_lockout_holds_after_the_limit():
    store = MemoryStore()

    async def go():
        for i in range(5):
            n = await rs.take_code_try(store, "L", "C", 5, 600, "wait")
            await rs.wrong_code(store, "L", "C", n, 5, 300)
        with pytest.raises(Exception) as e:
            await rs.take_code_try(store, "L", "C", 5, 600, "wait")
        assert getattr(e.value, "status_code", None) == 429
        assert await store.get("L") == "1"
    asyncio.run(go())


def test_same_code_is_constant_time_and_safe():
    assert rs.same_code(" 123456 ", "123456")
    assert not rs.same_code("123457", "123456")
    assert not rs.same_code("ünï", "123456")       # non-ASCII: no exception
    assert not rs.same_code("", "")                 # no code set: never matches


def test_start_code_lockout_still_works(app_client, exam):
    h = login(app_client, "alice")
    from .test_app_session import proof
    url = f"/api/student/exams/{exam['id']}/start"
    for _ in range(5):
        r = app_client.post(url, headers=h, json={"start_code": "000000" if exam["start_code"] != "000000"
                                                  else "111111", "client": proof(app_client, h, exam["id"], "device-aaaa-1111")})
        assert r.status_code == 403
    r = app_client.post(url, headers=h, json={"start_code": exam["start_code"],
                                              "client": proof(app_client, h, exam["id"], "device-aaaa-1111")})
    assert r.status_code == 429


# ---------------------------------------------------------------- signed exit reply
def test_exit_code_reply_is_signed(app_client, exam):
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    aid, sess = a["attempt_id"], a["session"]
    hs = {**h, "X-FairPlay-Session": sess}
    url = f"/api/student/attempts/{aid}/exit-code"
    assert app_client.post(url, headers=hs, json={"code": "1", "nonce": "a|b|c|d|e"}).status_code == 422
    r = app_client.post(url, headers=hs, json={"code": exam["invigilator_code"], "nonce": "exit-nonce-1234"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["nonce"] == "exit-nonce-1234"
    assert body["proofs"] == [sign(KEY, exit_message(aid, sess, "exit-nonce-1234"))]
    # a wrong code gets no proof at all
    r = app_client.post(url, headers=hs, json={"code": "999999" if exam["invigilator_code"] != "999999"
                                               else "888888", "nonce": "exit-nonce-5678"})
    assert r.status_code == 403 and "proofs" not in r.json()


# ---------------------------------------------------------------- events cap
def test_event_cap_is_reserved(client, live_exam, alice, teacher, monkeypatch):
    monkeypatch.setattr(rs, "MAX_EVENTS_PER_ATTEMPT", 5)
    a = client.post(f"/api/student/exams/{live_exam['id']}/start", headers=alice,
                    json={"start_code": live_exam["start_code"]}).json()
    hs = {**alice, "X-FairPlay-Session": a["session"]}
    url = f"/api/student/attempts/{a['attempt_id']}/events"
    batch = {"events": [{"type": "focus_lost", "detail": "x"}] * 3}
    assert client.post(url, headers=hs, json=batch).json()["stored"] == 3
    assert client.post(url, headers=hs, json=batch).json()["stored"] == 2
    assert client.post(url, headers=hs, json=batch).json()["stored"] == 0
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    types = [ev["type"] for x in res["attempts"] for ev in x.get("events", [])]
    assert types.count("focus_lost") == 5 and types.count("event_limit_reached") == 1


# ---------------------------------------------------------------- answers
def _start(client, exam, h):
    a = client.post(f"/api/student/exams/{exam['id']}/start", headers=h,
                    json={"start_code": exam["start_code"]}).json()
    return a, {**h, "X-FairPlay-Session": a["session"]}


def test_unknown_answer_keys_are_dropped(client, live_exam, alice):
    a, hs = _start(client, live_exam, alice)
    q0 = live_exam["questions"][0]["id"]
    r = client.put(f"/api/student/attempts/{a['attempt_id']}/answers", headers=hs,
                   json={"answers": {q0: 1, "made-up-1": "x", "made-up-2": "y"}, "client_seq": 1})
    assert r.json()["saved"] is True
    got = client.get(f"/api/student/attempts/{a['attempt_id']}", headers=hs).json()
    assert got["answers"] == {q0: 1}
    s = client.post(f"/api/student/attempts/{a['attempt_id']}/submit", headers=hs,
                    json={"answers": {q0: 1, "made-up-3": "z"}, "client_seq": 2})
    assert s.status_code == 200


def test_answer_write_budget(client, live_exam, alice, monkeypatch):
    monkeypatch.setattr(rs, "ANSWER_WRITES_PER_MIN", 2)
    a, hs = _start(client, live_exam, alice)
    url = f"/api/student/attempts/{a['attempt_id']}/answers"
    for seq in (1, 2):
        assert client.put(url, headers=hs, json={"answers": {}, "client_seq": seq}).status_code == 200
    assert client.put(url, headers=hs, json={"answers": {}, "client_seq": 3}).status_code == 429


@pytest.mark.skipif(bool(os.environ.get("PROCTORA_TEST_DATABASE_URL")), reason="pokes the SQLite file")
def test_submit_does_not_overwrite_a_newer_put(client, live_exam, alice, tmp_path, monkeypatch):
    """A PUT that lands between submit's read and its UPDATE used to be overwritten by the
    (older) submit body. Now the UPDATE needs the same client_seq and submit re-reads."""
    a, hs = _start(client, live_exam, alice)
    aid = a["attempt_id"]
    q0 = live_exam["questions"][0]["id"]
    client.put(f"/api/student/attempts/{aid}/answers", headers=hs,
               json={"answers": {q0: 0}, "client_seq": 1})
    real = rs.grade_attempt
    calls = []

    def racing_grade(*args, **kw):
        if not calls:   # first pass: a newer PUT (seq 9, answer 1) commits right now
            con = sqlite3.connect(tmp_path / "test.db")
            con.execute("UPDATE attempts SET answers = ?, client_seq = 9 WHERE id = ?",
                        ('{"%s": 1}' % q0, aid))
            con.commit()
            con.close()
        calls.append(1)
        return real(*args, **kw)
    monkeypatch.setattr(rs, "grade_attempt", racing_grade)
    r = client.post(f"/api/student/attempts/{aid}/submit", headers=hs,
                    json={"answers": {q0: 2}, "client_seq": 5})
    assert r.status_code == 200
    assert len(calls) == 2
    got = client.get(f"/api/student/attempts/{aid}", headers=hs).json()
    assert got["answers"] == {q0: 1}          # the newer PUT won, not the seq-5 body


# ---------------------------------------------------------------- small budgets / checks
def test_challenge_budget(client, live_exam, alice, monkeypatch):
    monkeypatch.setattr(rs, "CHALLENGES_PER_MIN", 2)
    url = f"/api/student/exams/{live_exam['id']}/challenge"
    assert client.get(url, headers=alice).status_code == 200
    assert client.get(url, headers=alice).status_code == 200
    assert client.get(url, headers=alice).status_code == 429


def test_handin_session_header_checked(app_client, exam):
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    r = app_client.get(f"/api/student/attempts/{a['attempt_id']}/handin",
                       params={"nonce": "nonce-12345678"},
                       headers={**h, "X-FairPlay-Session": "abc|submitted|x"})
    assert r.status_code == 422


def test_api_explorer_off_outside_dev(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 404 or "openapi" not in r.text.lower(), path


def test_container_does_not_trust_forwarded_headers_by_default():
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "FORWARDED_ALLOW_IPS:-127.0.0.1" in text and "FORWARDED_ALLOW_IPS:-*" not in text


def test_requirements_are_pinned():
    lines = [x.strip() for x in (Path(__file__).resolve().parents[1] / "requirements.txt")
             .read_text().splitlines() if x.strip() and not x.startswith("#")]
    assert lines and all("<" in x for x in lines)        # an upper bound on every line


# ---------------------------------------------------------------- offline package
def test_package_v3_iterations_and_legacy():
    from app.offline import LEGACY_ITERATIONS, decrypt_package, encrypt_package
    pkg = encrypt_package({"a": 1}, "123456", "e_1", iterations=2000, package_key="k")
    assert pkg["version"] == 3 and pkg["iterations"] == 2000
    assert decrypt_package(pkg, "123456", package_key="k") == {"a": 1}     # count from the package
    old = encrypt_package({"b": 2}, "123456", "e_1", iterations=LEGACY_ITERATIONS, package_key="k")
    del old["iterations"]
    assert decrypt_package(old, "123456", package_key="k") == {"b": 2}     # v2: 100k
    with pytest.raises(ValueError):
        decrypt_package({**pkg, "iterations": 10 ** 9}, "123456", package_key="k")


def test_package_secret_set_once(client, live_exam):
    """Two first requests: the stale one must return the key that was really stored."""
    from app import db as dbmod
    from app.models import Exam
    from app.offline import _package_secret
    from sqlalchemy import update

    async def go():
        async with dbmod._db.sessionmaker() as s1, dbmod._db.sessionmaker() as s2:
            await s1.execute(update(Exam).where(Exam.id == live_exam["id"]).values(package_secret=None))
            await s1.commit()
            e1 = await s1.get(Exam, live_exam["id"])       # s1 has seen "no key yet"
            assert e1.package_secret is None
            k2 = await _package_secret(s2, live_exam["id"])  # the other request makes one
            k1 = await _package_secret(s1, live_exam["id"])
            return k1, k2
    k1, k2 = client.portal.call(go)
    assert k1 == k2 and len(k1) == 64


def test_package_build_is_single_flight(client, live_exam, alice, bob, monkeypatch):
    from app import offline
    builds = []
    real = offline._build

    def slow_build(exam, key):
        builds.append(threading.get_ident())
        time.sleep(0.6)
        return real(exam, key)
    monkeypatch.setattr(offline, "_build", slow_build)
    url = f"/api/student/exams/{live_exam['id']}/package"

    async def go():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            rs_ = await asyncio.gather(*(ac.get(url, headers=h) for h in (alice, bob, alice, bob)))
        return [r.status_code for r in rs_], [r.json() for r in rs_]
    codes, bodies = client.portal.call(go)
    assert codes == [200] * 4
    assert len(builds) == 1
    assert all(b == bodies[0] for b in bodies)


# ---------------------------------------------------------------- practice numbers
def test_huge_numbers_are_not_a_500():
    from app import mathcheck
    for bad in (10 ** 400, "9e999", "-9e999"):
        with pytest.raises(mathcheck.MathError):
            mathcheck.parse_number(bad)
    assert mathcheck.numbers_equal(10 ** 400, 1.0) is False


def test_moved_needs_another_computer(app_client, exam):
    """A made-up session on the locked Mac itself must not read as "moved" (= unlock)."""
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    url = f"/api/student/attempts/{a['attempt_id']}/handin"
    fake = {**h, "X-FairPlay-Session": "made-up-session-1"}
    same = app_client.get(url, params={"nonce": "nonce-12345678", "device": "device-aaaa-1111"}, headers=fake)
    assert same.json()["status"] == "in_progress"
    other = app_client.get(url, params={"nonce": "nonce-12345678", "device": "device-zzzz-9999"}, headers=fake)
    assert other.json()["status"] == "moved"
    legacy = app_client.get(url, params={"nonce": "nonce-12345678"}, headers=fake)
    assert legacy.json()["status"] == "moved"           # old apps: unchanged
    bad = app_client.get(url, params={"nonce": "nonce-12345678", "device": "a|b"}, headers=fake)
    assert bad.status_code == 422


# ---------------------------------------------------------------- Windows app formats
def test_windows_signed_verdicts(app_client, exam):
    """fairplay-windows RequireSignedVerdicts: one `proof` on exit-code and offline-key."""
    from app.app_client import exit_verdict_message, offkey_response_message, offline_key_message
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    aid, sess = a["attempt_id"], a["session"]
    hs = {**h, "X-FairPlay-Session": sess}
    r = app_client.post(f"/api/student/attempts/{aid}/exit-code", headers=hs,
                        json={"code": exam["invigilator_code"], "nonce": "0123456789abcdef0123456789abcdef"})
    assert r.json()["proof"] == sign(KEY, exit_verdict_message(aid, sess, "0123456789abcdef0123456789abcdef"))
    body = {"session": sess, "device_id": "device-aaaa-1111", "ts": 7,
            "signature": sign(KEY, offline_key_message(aid, sess, "device-aaaa-1111", 7))}
    k = app_client.post(f"/api/student/attempts/{aid}/offline-key", headers=hs, json=body).json()
    assert k["proof"] == sign(KEY, offkey_response_message(aid, sess, k["key"], 7))


def test_made_up_session_on_same_device_is_not_moved(app_client, exam):
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    url = f"/api/student/attempts/{a['attempt_id']}/handin"
    fake = {**h, "X-FairPlay-Session": "made-up-session-1"}
    same = app_client.get(url, params={"nonce": "nonce-12345678", "device": "device-aaaa-1111"}, headers=fake)
    assert same.json()["status"] == "in_progress"
    other = app_client.get(url, params={"nonce": "nonce-12345678", "device": "device-zzzz-9999"}, headers=fake)
    assert other.json()["status"] == "moved"
    assert app_client.get(url, params={"nonce": "nonce-12345678", "device": "a|b"}, headers=fake).status_code == 422


def test_app_signal_from_new_network_is_flagged(app_client, exam):
    """M-07 server half: one session's signed beats from two addresses -> one note."""
    from app.store import get_store
    from .test_app_session import beat
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    aid, sess = a["attempt_id"], a["session"]
    assert beat(app_client, h, aid, sess, 1).status_code == 200
    assert beat(app_client, h, aid, sess, 2).status_code == 200      # same address: nothing

    def types():
        res = app_client.get(f"/api/exams/{exam['id']}/results", headers=t).json()
        return [ev["type"] for x in res["attempts"] for ev in x.get("events", [])]
    assert "network_changed" not in types()
    # the session's last beat came from somewhere else
    app_client.portal.call(get_store().set, f"abip:{aid}:{sess}", "10.9.9.9", 3600)
    assert beat(app_client, h, aid, sess, 3).status_code == 200
    assert beat(app_client, h, aid, sess, 4).status_code == 200
    assert types().count("network_changed") == 1


def test_background_app_event_is_a_client_type():
    from app.app_client import client_event_type
    assert client_event_type("app_in_background") == "app_in_background"
