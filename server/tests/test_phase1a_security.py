"""Phase 1a: production settings, CLI accounts, CORS, security headers, sign-in limits,
token revocation, per-exam invigilator exit code, attempt-session write rules."""
import base64
import io
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import hardening
from app.config import DEV_SECRET, ConfigError, Settings
from app.exitcode import normalize, verify_check
from app.main import create_app
from app.security import hash_password
from app.timeutil import utcnow

from .conftest import SAMPLE_EXAM, login

PROD_SECRET = "p" * 48


def _settings(tmp_path, static_dir=None, **kw):
    base = dict(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", redis_url=None,
                secret="test-secret", pbkdf2_iterations=1000, seed_loadtest_users=0,
                static_dir=str(static_dir or tmp_path), uploads_dir=str(tmp_path / "uploads"))
    base.update(kw)
    return Settings(**base)


def add_user(tmp_path, username, role, password=None, name=None):
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (username, name, role, password_hash, created_at, token_version)"
                       " VALUES (:u, :n, :r, :p, :t, 0)"),
                  {"u": username, "n": name or username, "r": role, "t": utcnow(),
                   "p": hash_password(password or f"{username}123", 1000)})
    eng.dispose()


def sql(tmp_path, stmt, **params):
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with eng.begin() as c:
        c.execute(text(stmt), params)
    eng.dispose()


def start(client, h, exam, code=None):
    return client.post(f"/api/student/exams/{exam['id']}/start",
                       json={"start_code": code or exam["start_code"]}, headers=h)


# ---------------------------------------------------------------- production settings
@pytest.mark.parametrize("secret", [None, DEV_SECRET, "short-secret"])
def test_production_refuses_unsafe_secret(tmp_path, secret):
    s = _settings(tmp_path, env="production", secret=secret or "")
    with pytest.raises(ConfigError):
        create_app(s)


def test_production_refuses_wildcard_cors(tmp_path):
    with pytest.raises(ConfigError):
        create_app(_settings(tmp_path, env="production", secret=PROD_SECRET, cors_origins=["*"]))


def test_env_var_selects_mode(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_ENV", "production")
    monkeypatch.delenv("PROCTORA_SECRET", raising=False)
    s = Settings()
    assert s.production and s.allowed_origins() == []
    with pytest.raises(ConfigError):
        s.validate()
    monkeypatch.setenv("FAIRPLAY_ENV", "development")
    assert not Settings().production and Settings().allowed_origins() == ["*"]
    monkeypatch.setenv("FAIRPLAY_CORS_ORIGINS", "https://a.example, https://b.example")
    assert Settings().allowed_origins() == ["https://a.example", "https://b.example"]


def test_production_seeds_no_demo_or_loadtest_users(tmp_path):
    s = _settings(tmp_path, env="production", secret=PROD_SECRET, seed_loadtest_users=3)
    with TestClient(create_app(s)) as c:
        for u, pw in (("teacher", "teacher123"), ("alice", "alice123"), ("lt0001", "loadtest")):
            assert c.post("/api/auth/login", json={"username": u, "password": pw}).status_code == 401
    # explicit second switch: load-test users are created
    s2 = _settings(tmp_path, env="production", secret=PROD_SECRET, seed_loadtest_users=2,
                   allow_loadtest_users_in_production=True)
    with TestClient(create_app(s2)) as c:
        assert c.post("/api/auth/login", json={"username": "lt0002", "password": "loadtest"}).status_code == 200
        assert c.post("/api/auth/login", json={"username": "teacher", "password": "teacher123"}).status_code == 401


def test_cli_creates_first_account(tmp_path, monkeypatch):
    from app import manage
    s = _settings(tmp_path, env="production", secret=PROD_SECRET)
    monkeypatch.setattr("sys.stdin", io.StringIO("correct horse battery\n"))
    manage.main(["create-user", "--username", "jsmith", "--name", "Jane Smith", "--role", "teacher",
                 "--password-stdin"], settings=s)
    monkeypatch.setattr("sys.stdin", io.StringIO("x\n"))
    with pytest.raises(SystemExit):  # too short
        manage.main(["create-user", "--username", "x", "--role", "student", "--password-stdin"], settings=s)
    with pytest.raises(SystemExit):  # duplicate
        monkeypatch.setattr("sys.stdin", io.StringIO("correct horse battery\n"))
        manage.main(["create-user", "--username", "jsmith", "--role", "teacher", "--password-stdin"],
                    settings=s)
    with TestClient(create_app(s)) as c:
        r = c.post("/api/auth/login", json={"username": "jsmith", "password": "correct horse battery"})
        assert r.status_code == 200 and r.json()["role"] == "teacher" and r.json()["name"] == "Jane Smith"
        old = {"Authorization": f"Bearer {r.json()['token']}"}
        assert c.get("/api/exams", headers=old).status_code == 200
    # CLI sign-out-everywhere revokes the token issued above
    manage.main(["revoke-tokens", "--username", "jsmith"], settings=s)
    with TestClient(create_app(s)) as c:
        assert c.get("/api/exams", headers=old).status_code == 401


# ---------------------------------------------------------------- CORS + headers
def test_cors_dev_allows_all_production_same_origin(tmp_path, client):
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert r.headers.get("access-control-allow-origin") == "*"
    s = _settings(tmp_path, env="production", secret=PROD_SECRET)
    with TestClient(create_app(s)) as c:
        r = c.get("/api/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers
    s = _settings(tmp_path, env="production", secret=PROD_SECRET, cors_origins=["https://lms.example"])
    with TestClient(create_app(s)) as c:
        ok = c.get("/api/health", headers={"Origin": "https://lms.example"})
        assert ok.headers.get("access-control-allow-origin") == "https://lms.example"
        bad = c.get("/api/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in bad.headers


def test_security_headers(client, tmp_path):
    for path in ("/api/health", "/teacher", "/assets/app.js", "/api/nope"):
        r = client.get(path)
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "same-origin"
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "strict-transport-security" not in client.get("/api/health").headers
    with TestClient(create_app(_settings(tmp_path, env="production", secret=PROD_SECRET))) as c:
        assert c.get("/api/health").headers["strict-transport-security"].startswith("max-age=")


# ---------------------------------------------------------------- sign-in limits
def test_login_lockout_per_username(client):
    for _ in range(hardening.LOGIN_MAX_PER_USER - 1):
        assert client.post("/api/auth/login", json={"username": "alice", "password": "x"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "alice", "password": "x"})
    assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "alice", "password": "alice123"})
    assert r.status_code == 429 and "Too many failed sign-ins" in r.json()["detail"]
    # case / spacing variants share the counter; other users are not affected
    assert client.post("/api/auth/login", json={"username": " ALICE ", "password": "alice123"}).status_code == 429
    assert client.post("/api/auth/login", json={"username": "bob", "password": "bob123"}).status_code == 200


def test_login_success_resets_counter(client):
    for _ in range(hardening.LOGIN_MAX_PER_USER - 1):
        client.post("/api/auth/login", json={"username": "bob", "password": "x"})
    assert client.post("/api/auth/login", json={"username": "bob", "password": "bob123"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "bob", "password": "x"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "bob", "password": "bob123"}).status_code == 200


def test_login_lockout_per_ip(client, monkeypatch):
    monkeypatch.setattr(hardening, "LOGIN_MAX_PER_IP", 4)
    for i in range(4):  # different usernames, same IP
        client.post("/api/auth/login", json={"username": f"nobody{i}", "password": "x"})
    r = client.post("/api/auth/login", json={"username": "carol", "password": "carol123"})
    assert r.status_code == 429


# ---------------------------------------------------------------- token revocation
def test_password_change_revokes_old_tokens(client):
    old = login(client, "alice")
    r = client.post("/api/auth/password", json={"current_password": "nope", "new_password": "newpass123"},
                    headers=old)
    assert r.status_code == 403
    r = client.post("/api/auth/password", json={"current_password": "alice123", "new_password": "short"},
                    headers=old)
    assert r.status_code == 422
    r = client.post("/api/auth/password",
                    json={"current_password": "alice123", "new_password": "newpass123"}, headers=old)
    assert r.status_code == 200
    new = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/me", headers=old).status_code == 401
    assert client.get("/api/me", headers=new).status_code == 200
    assert client.post("/api/auth/login", json={"username": "alice", "password": "alice123"}).status_code == 401
    assert login(client, "alice", "newpass123")


def test_logout_everywhere(client):
    a1, a2 = login(client, "alice"), login(client, "alice")
    assert client.post("/api/auth/logout-all", headers=a1).status_code == 204
    assert client.get("/api/me", headers=a1).status_code == 401
    assert client.get("/api/me", headers=a2).status_code == 401
    a3 = login(client, "alice")
    assert client.get("/api/me", headers=a3).status_code == 200


def test_token_of_deleted_user_is_refused(client, tmp_path):
    add_user(tmp_path, "temp", "student")
    h = login(client, "temp")
    assert client.get("/api/me", headers=h).status_code == 200
    from app.security import token_version_key
    sql(tmp_path, "DELETE FROM users WHERE username='temp'")
    # the cached version expires (simulate) -> the next request reads the DB
    from app.store import get_store
    uid = json.loads(base64.urlsafe_b64decode(
        h["Authorization"].split()[1].split(".")[0] + "==").decode().split(":")[0])
    client.portal.call(get_store().delete, token_version_key(uid))
    assert client.get("/api/me", headers=h).status_code == 401


# ---------------------------------------------------------------- invigilator exit code
def test_exit_code_teacher_only_and_verified(client, teacher, alice, bob):
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    assert e["exit_code"] is None  # draft: none yet
    e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    code = e["exit_code"]
    assert code and len(normalize(code)) == 8 and code != "1234"
    assert client.get(f"/api/exams/{e['id']}/live", headers=teacher).json()["exit_code"] == code
    a = start(client, alice, e).json()
    # students only ever get the verifier, never the code
    for body in (a, client.get(f"/api/student/attempts/{a['attempt_id']}", headers=alice).json(),
                 client.get("/api/student/exams", headers=alice).json()):
        assert normalize(code) not in json.dumps(body).upper().replace("-", "")
    assert verify_check(a["exit_check"], code)
    assert verify_check(a["exit_check"], code.lower().replace("-", " "))
    assert not verify_check(a["exit_check"], "1234")

    url = f"/api/student/attempts/{a['attempt_id']}/exit-code"
    assert client.post(url, json={"code": "1234"}, headers=alice).status_code == 403
    assert client.post(url, json={"code": code}, headers=alice).json() == {"ok": True}
    assert client.post(url, json={"code": code}, headers=bob).status_code == 404  # not bob's attempt
    assert client.post(url, json={"code": code}, headers=teacher).status_code == 403  # role
    for _ in range(5):
        client.post(url, json={"code": "WRONG"}, headers=alice)
    assert client.post(url, json={"code": code}, headers=alice).status_code == 429

    # a new opening issues a new code; the old one stops working
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    e2 = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    assert e2["exit_code"] != code


# ---------------------------------------------------------------- attempt-session write rules
def _setup_attempt(client, teacher, alice, live_exam):
    a = start(client, alice, live_exam).json()
    return a, f"/api/student/attempts/{a['attempt_id']}"


def test_writes_rejected_after_deadline(client, teacher, alice, live_exam, tmp_path):
    a, base = _setup_attempt(client, teacher, alice, live_exam)
    assert client.put(f"{base}/answers", json={"answers": {"x": 1}, "client_seq": 1}, headers=alice).json()["saved"]
    sql(tmp_path, "UPDATE attempts SET deadline_at=:d WHERE id=:a",
        d=utcnow() - timedelta(seconds=5), a=a["attempt_id"])
    r = client.put(f"{base}/answers", json={"answers": {"x": 2}, "client_seq": 2}, headers=alice)
    assert r.status_code == 409 and r.json()["detail"] == "Time is up"
    # submit after the deadline is still accepted (contract: offline work is never lost) but late
    sql(tmp_path, "UPDATE attempts SET deadline_at=:d WHERE id=:a",
        d=utcnow() - timedelta(minutes=5), a=a["attempt_id"])
    r = client.post(f"{base}/submit", json={"answers": {"x": 3}, "client_seq": 3}, headers=alice)
    assert r.status_code == 200
    late = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with eng.connect() as c:
        assert c.execute(text("SELECT late FROM attempts WHERE id=:a"), {"a": a["attempt_id"]}).scalar() in (1, True)
    eng.dispose()
    assert late["attempts"][0]["status"] == "submitted"


def test_writes_rejected_after_submit(client, teacher, alice, live_exam):
    a, base = _setup_attempt(client, teacher, alice, live_exam)
    rec = client.post(f"{base}/submit", json={"answers": {}, "client_seq": 0}, headers=alice).json()
    r = client.put(f"{base}/answers", json={"answers": {"x": 2}, "client_seq": 99}, headers=alice)
    assert r.status_code == 409 and r.json()["detail"] == "Attempt already submitted"
    # second submit with different answers: same receipt, answers unchanged
    again = client.post(f"{base}/submit", json={"answers": {"x": 5}, "client_seq": 100}, headers=alice).json()
    assert again["receipt"] == rec["receipt"]
    hb = client.post(f"{base}/heartbeat", json={"current_question": 0, "locked": True}, headers=alice).json()
    assert hb["status"] == "submitted"
    assert start(client, alice, live_exam).status_code == 409


def test_writes_rejected_after_close(client, teacher, alice, live_exam):
    a, base = _setup_attempt(client, teacher, alice, live_exam)
    client.post(f"/api/exams/{live_exam['id']}/close", headers=teacher)
    r = client.put(f"{base}/answers", json={"answers": {"x": 2}, "client_seq": 5}, headers=alice)
    assert r.status_code == 409
    hb = client.post(f"{base}/heartbeat", json={"current_question": 0, "locked": True}, headers=alice).json()
    assert hb["status"] == "submitted"
    sub = client.post(f"{base}/submit", json={"answers": {"x": 9}, "client_seq": 6}, headers=alice).json()
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    att = res["attempts"][0]
    assert att["auto_submitted"] and att["receipt"] == sub["receipt"]
    assert start(client, alice, live_exam, "0000").status_code == 409  # not open any more
