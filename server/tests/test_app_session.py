"""Audit 2026-10-12: app-only start, one session per attempt, signed app beats,
server-checked invigilator exit, startup refusal of public secrets."""
import time

import pytest
from fastapi.testclient import TestClient

from app.app_client import beat_message, sign, start_message
from app.config import DEV_SECRET, Settings
from app.main import create_app

from .conftest import SAMPLE_EXAM, login

KEY = "unit-test-app-key-0123456789"
DEV_A = "device-aaaa-1111"
DEV_B = "device-bbbb-2222"


@pytest.fixture
def app_client(tmp_path, static_dir):
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}", redis_url=None,
                 secret="test-secret-0123456789abcdef", pbkdf2_iterations=1000,
                 static_dir=str(static_dir), uploads_dir=str(tmp_path / "up"),
                 require_app=True, app_keys=[KEY], dev=False, seed_loadtest_users=0, seed_demo=True, single_process=True)
    with TestClient(create_app(s)) as c:
        yield c


@pytest.fixture
def exam(app_client):
    t = login(app_client, "teacher")
    e = app_client.post("/api/exams", json=SAMPLE_EXAM, headers=t).json()
    return app_client.post(f"/api/exams/{e['id']}/open", headers=t).json()


def proof(c, h, exam_id, device, key=KEY):
    nonce = c.get(f"/api/student/exams/{exam_id}/challenge", headers=h).json()["nonce"]
    return {"device_id": device, "nonce": nonce,
            "signature": sign(key, start_message(nonce, device)), "version": "0.5"}


def start(c, h, exam, device=DEV_A, key=KEY):
    return c.post(f"/api/student/exams/{exam['id']}/start", headers=h,
                  json={"start_code": exam["start_code"], "client": proof(c, h, exam["id"], device, key)})


def events(c, teacher, exam):
    res = c.get(f"/api/exams/{exam['id']}/results", headers=teacher).json()
    return [ev["type"] for a in res["attempts"] for ev in a.get("events", [])]


def live_student(c, teacher, exam):
    return c.get(f"/api/exams/{exam['id']}/live", headers=teacher).json()["students"][0]


def test_codes_are_six_digits(exam):
    assert len(exam["start_code"]) == 6 and exam["start_code"].isdigit()
    assert len(exam["invigilator_code"]) == 6


def test_browser_cannot_start(app_client, exam):
    h = login(app_client, "alice")
    r = app_client.post(f"/api/student/exams/{exam['id']}/start", headers=h,
                        json={"start_code": exam["start_code"]})
    assert r.status_code == 403 and "FairPlay app" in r.json()["detail"]


def test_bad_or_replayed_proof_refused(app_client, exam):
    h = login(app_client, "alice")
    assert start(app_client, h, exam, key="wrong-key").status_code == 403
    p = proof(app_client, h, exam["id"], DEV_A)
    body = {"start_code": exam["start_code"], "client": p}
    assert app_client.post(f"/api/student/exams/{exam['id']}/start", headers=h, json=body).status_code == 200
    # nonce is single use
    assert app_client.post(f"/api/student/exams/{exam['id']}/start", headers=h, json=body).status_code == 403
    # a nonce for another student does not work for alice
    bob = login(app_client, "bob")
    p2 = proof(app_client, bob, exam["id"], DEV_B)
    r = app_client.post(f"/api/student/exams/{exam['id']}/start", headers=h,
                        json={"start_code": exam["start_code"], "client": p2})
    assert r.status_code == 403


def test_session_binding_and_second_device(app_client, exam):
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    aid, sess = a["attempt_id"], a["session"]
    good = {**h, "X-FairPlay-Session": sess}
    qid = a["questions"][0]["id"]
    put = lambda hdr, seq: app_client.put(f"/api/student/attempts/{aid}/answers", headers=hdr,
                                          json={"answers": {qid: 1}, "client_seq": seq})
    assert put(good, 0).json()["saved"] is True
    # same login elsewhere (helper in a browser): no session -> refused, and GET too
    h2 = login(app_client, "alice")
    assert put(h2, 1).status_code == 409
    assert app_client.get(f"/api/student/attempts/{aid}", headers=h2).status_code == 409
    assert app_client.post(f"/api/student/attempts/{aid}/submit", headers=h2, json={}).status_code == 409
    # helper with another Mac running FairPlay: refused and flagged
    r = start(app_client, h2, exam, device=DEV_B)
    assert r.status_code == 409 and "another computer" in r.json()["detail"]
    ev = events(app_client, t, exam)
    assert "foreign_session" in ev and "second_device" in ev
    # the real session still works
    assert put(good, 2).json()["saved"] is True


def test_reentry_rotates_session_and_is_flagged(app_client, exam):
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    again = start(app_client, h, exam).json()            # double click: same session
    assert again["session"] == a["session"]
    # simulate the double-click window passing (in-memory store in tests)
    import asyncio
    from app.store import get_store
    asyncio.run(get_store().delete(f"entry:{a['attempt_id']}"))
    third = start(app_client, h, exam).json()           # app killed and reopened
    assert third["session"] != a["session"]
    old = {**h, "X-FairPlay-Session": a["session"]}
    assert app_client.post(f"/api/student/attempts/{a['attempt_id']}/heartbeat", headers=old,
                           json={"current_question": 0}).status_code == 409
    assert "re_entry" in events(app_client, t, exam)
    assert live_student(app_client, t, exam)["entries"] == 2


def beat(c, h, aid, sess, ts, locked=True, device=DEV_A, key=KEY, checks="vm=0,displays=1"):
    return c.post(f"/api/student/attempts/{aid}/app-beat", headers=h, json={
        "session": sess, "device_id": device, "ts": ts, "locked": locked, "checks": checks,
        "signature": sign(key, beat_message(aid, sess, device, ts, locked, checks))})


def test_locked_only_from_signed_app_beats(app_client, exam):
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    aid, sess = a["attempt_id"], a["session"]
    hs = {**h, "X-FairPlay-Session": sess}
    app_client.post(f"/api/student/attempts/{aid}/heartbeat", headers=hs,
                    json={"current_question": 1, "locked": True})
    assert live_student(app_client, t, exam)["locked"] is False        # self-report ignored
    assert beat(app_client, h, aid, sess, 1000, key="forged").status_code == 403
    assert beat(app_client, h, aid, sess, 1000).status_code == 200
    s = live_student(app_client, t, exam)
    assert s["locked"] is True and s["app_online"] is True and s["app_checks"] == "vm=0,displays=1"
    assert beat(app_client, h, aid, sess, 1000).status_code == 409    # replay
    assert beat(app_client, h, aid, "x" * 20, 2000).status_code == 409  # wrong session
    assert beat(app_client, h, aid, sess, 2000, device=DEV_B).status_code == 409
    assert beat(app_client, h, aid, sess, 3000, locked=False).status_code == 200
    assert "unlocked_in_exam" in events(app_client, t, exam)


def test_app_gap_flagged(app_client, exam, monkeypatch):
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    aid, sess = a["attempt_id"], a["session"]
    assert beat(app_client, h, aid, sess, 1000).status_code == 200
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 60)
    assert beat(app_client, h, aid, sess, 2000).status_code == 200
    assert "app_gap" in events(app_client, t, exam)
    # page alive while the app stays quiet -> no_app_signal
    monkeypatch.setattr(time, "time", lambda: real() + 200)
    app_client.post(f"/api/student/attempts/{aid}/heartbeat",
                    headers={**h, "X-FairPlay-Session": sess}, json={"current_question": 0})
    assert "no_app_signal" in events(app_client, t, exam)


def test_invigilator_exit_checked_by_server(app_client, exam):
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    hs = {**h, "X-FairPlay-Session": a["session"]}
    url = f"/api/student/attempts/{a['attempt_id']}/exit-code"
    assert app_client.post(url, headers=hs, json={"code": "1234"}).status_code == 403
    for _ in range(4):
        app_client.post(url, headers=hs, json={"code": "000000"})
    # locked out now, even with the right code
    assert app_client.post(url, headers=hs, json={"code": exam["invigilator_code"]}).status_code == 429
    ev = events(app_client, t, exam)
    assert ev.count("emergency_exit_rejected") == 5


def test_invigilator_exit_ok_and_release_device(app_client, exam):
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    hs = {**h, "X-FairPlay-Session": a["session"]}
    r = app_client.post(f"/api/student/attempts/{a['attempt_id']}/exit-code", headers=hs,
                        json={"code": exam["invigilator_code"]})
    assert r.json() == {"ok": True}
    # broken laptop: teacher releases, the old session dies, another Mac can continue
    assert app_client.post(f"/api/exams/{exam['id']}/attempts/{a['attempt_id']}/release-device",
                           headers=t).json() == {"ok": True}
    assert app_client.get(f"/api/student/attempts/{a['attempt_id']}", headers=hs).status_code == 409
    b = start(app_client, h, exam, device=DEV_B)
    assert b.status_code == 200 and b.json()["attempt_id"] == a["attempt_id"]
    ev = events(app_client, t, exam)
    assert "emergency_exit" in ev and "device_released" in ev and "device_bound" in ev


def test_rotate_start_code(app_client, exam):
    t = login(app_client, "teacher")
    new = app_client.post(f"/api/exams/{exam['id']}/rotate-code", headers=t).json()["start_code"]
    assert new != exam["start_code"] and len(new) == 6
    h = login(app_client, "alice")
    assert start(app_client, h, exam).status_code == 403          # old code
    assert start(app_client, h, {**exam, "start_code": new}).status_code == 200


@pytest.mark.parametrize("secret", ["", DEV_SECRET, "short"])
def test_server_refuses_public_or_missing_secret(tmp_path, static_dir, secret):
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", secret=secret,
                 static_dir=str(static_dir), uploads_dir=str(tmp_path / "up"), dev=False,
                 app_keys=[KEY], seed_loadtest_users=0)
    with pytest.raises(RuntimeError):
        with TestClient(create_app(s)):
            pass


def test_server_refuses_without_app_key(tmp_path, static_dir):
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}",
                 secret="test-secret-0123456789abcdef", static_dir=str(static_dir),
                 uploads_dir=str(tmp_path / "up"), dev=False, app_keys=[], require_app=True,
                 seed_loadtest_users=0)
    with pytest.raises(RuntimeError):
        with TestClient(create_app(s)):
            pass


def test_dev_mode_generates_local_secret(tmp_path, static_dir):
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", secret="",
                 static_dir=str(static_dir), uploads_dir=str(tmp_path / "data" / "up"),
                 dev=True, app_keys=[], seed_loadtest_users=0, pbkdf2_iterations=1000)
    with TestClient(create_app(s)) as c:
        assert login(c, "alice")
    assert len(s.secret) >= 32 and s.secret != DEV_SECRET
    assert (tmp_path / "data" / "secret.key").read_text() == s.secret


def test_no_wildcard_cors(app_client):
    r = app_client.options("/api/health", headers={"Origin": "https://evil.example",
                                                   "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------- offline invigilator exit
def test_offline_exit_code_matches_known_vector():
    from app.app_client import normalize_mac_code, offline_exit_code
    # Same vector must come out of mac/Sources/Proctora/OfflineExit.swift (dev key).
    assert offline_exit_code("fairplay-dev-app-key-not-secret", "K7QM3XPA") == "37496243"
    assert offline_exit_code(KEY, "K7QM3XPA") == "42780773"
    assert normalize_mac_code(" k7qm-3xpa ") == "K7QM3XPA"
    for bad in ("", "K7QM3XP", "K7QM3XPAA", "K7QM-3XP0", "K7QM-3XPI"):   # 0/I/O/1/L not used
        assert normalize_mac_code(bad) is None


def test_offline_key_vector():
    from app.app_client import offline_exit_code
    # mac/Sources/Proctora/OfflineExit.swift with the key bytes 00 01 .. 1f must agree.
    assert offline_exit_code(bytes(range(32)), "K7QM3XPA") == "06710479"


def _fetch_offline_key(c, h, a, device=DEV_A, key=KEY, ts=1):
    from app.app_client import offline_key_message
    msg = offline_key_message(a["attempt_id"], a["session"], device, ts)
    return c.post(f"/api/student/attempts/{a['attempt_id']}/offline-key",
                  headers={**h, "X-FairPlay-Session": a["session"]},
                  json={"session": a["session"], "device_id": device, "ts": ts,
                        "signature": sign(key, msg)})


def test_offline_exit_per_attempt_key(app_client, exam):
    from app.app_client import offline_exit_code
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    # the app (signed) gets the key once; a second ask is refused and flagged
    assert _fetch_offline_key(app_client, h, a, key="wrong-key").status_code == 403
    r = _fetch_offline_key(app_client, h, a)
    assert r.status_code == 200
    key = bytes.fromhex(r.json()["key"])
    assert len(key) == 32
    # review 2026-10-14: the same signed device may ask again (relaunch): same key, no flag
    again = _fetch_offline_key(app_client, h, a, ts=2)
    assert again.status_code == 200 and bytes.fromhex(again.json()["key"]) == key
    for ts in range(3, 13):          # ... but asking very often is flagged once
        _fetch_offline_key(app_client, h, a, ts=ts)
    # the teacher picks the student and types the Mac code
    url = f"/api/exams/{exam['id']}/attempts/{a['attempt_id']}/offline-exit"
    r = app_client.post(url, headers=t, json={"mac_code": "k7qm-3xpa"})
    assert r.status_code == 200 and r.json()["code"] == offline_exit_code(key, "K7QM3XPA")
    # not the app key: knowing it from the binary does not give the code
    assert r.json()["code"] != offline_exit_code(KEY, "K7QM3XPA")
    assert app_client.post(url, headers=t, json={"mac_code": "nope"}).status_code == 422
    assert app_client.post(url, headers=h, json={"mac_code": "K7QM3XPA"}).status_code in (401, 403)
    ev = events(app_client, t, exam)
    assert "offline_key_requested_twice" in ev and "offline_exit_code_issued" in ev
    # a new session (exam moved to another Mac) gets a new key; the old code stops matching
    app_client.post(f"/api/exams/{exam['id']}/attempts/{a['attempt_id']}/release-device", headers=t)
    b = start(app_client, h, exam, device=DEV_B).json()
    assert b["session"] != a["session"]
    r2 = _fetch_offline_key(app_client, h, b, device=DEV_B)
    assert r2.status_code == 200 and r2.json()["key"] != key.hex()
    assert app_client.post(url, headers=t, json={"mac_code": "K7QM3XPA"}).json()["code"] \
        == offline_exit_code(bytes.fromhex(r2.json()["key"]), "K7QM3XPA")


def test_offline_exit_cli(monkeypatch, capsys):
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "tools" / "offline_exit_code.py"
    spec = importlib.util.spec_from_file_location("offline_exit_code", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("FAIRPLAY_APP_KEY", KEY)
    assert mod.main(["x", "K7QM-3XPA"]) == 0
    assert capsys.readouterr().out.strip() == "42780773"


# ---------------------------------------------------------------- audit M-02 / M-04
def test_off_console_beat_counts_as_not_locked(app_client, exam):
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    aid, sess = a["attempt_id"], a["session"]
    assert beat(app_client, h, aid, sess, 1000, checks="vm=0,console=1,secinput=0").status_code == 200
    live = app_client.get(f"/api/exams/{exam['id']}/live", headers=t).json()
    assert live["students"][0]["locked"] is True
    assert beat(app_client, h, aid, sess, 2000, checks="vm=0,console=0,secinput=1").status_code == 200
    live = app_client.get(f"/api/exams/{exam['id']}/live", headers=t).json()
    assert live["students"][0]["locked"] is False
    ev = events(app_client, t, exam)
    assert "off_console" in ev and "secure_input_other_app" in ev and "unlocked_in_exam" in ev


def test_signed_handin_status(app_client, exam):
    from app.app_client import handin_message
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    aid, sess = a["attempt_id"], a["session"]
    hs = {**h, "X-FairPlay-Session": sess}

    def status(headers, nonce="nonce-12345678"):
        r = app_client.get(f"/api/student/attempts/{aid}/handin", params={"nonce": nonce}, headers=headers)
        assert r.status_code == 200, r.text
        return r.json()
    r = status(hs)
    assert r["status"] == "in_progress"
    assert r["proofs"] == [sign(KEY, handin_message(aid, sess, "in_progress", "nonce-12345678"))]
    app_client.post(f"/api/student/attempts/{aid}/submit", headers=hs, json={"answers": {}, "client_seq": 1})
    r = status(hs, "another-nonce-1")
    assert r["status"] == "submitted" and r["proofs"][0] == sign(KEY, handin_message(aid, sess, "submitted", "another-nonce-1"))
    # another session sees "moved"; someone else's attempt is an unsigned 404 (review
    # 2026-10-14: "gone" would end the lock), a deleted one is "gone"
    assert status({**h, "X-FairPlay-Session": "x" * 20})["status"] == "moved"
    bob = login(app_client, "bob")
    r404 = app_client.get(f"/api/student/attempts/{aid}/handin", params={"nonce": "nonce-12345678"}, headers=bob)
    assert r404.status_code == 404 and "proofs" not in r404.json()
    gone = app_client.get("/api/student/attempts/a_doesnotexist/handin", params={"nonce": "nonce-12345678"}, headers=h)
    assert gone.status_code == 200 and gone.json()["status"] == "gone"
    bad = app_client.get(f"/api/student/attempts/{aid}/handin", params={"nonce": "a|b"}, headers=hs)
    assert bad.status_code == 422
