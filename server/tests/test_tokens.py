"""Audit 2026-10-12 S-15: tokens can be revoked, carry a key id, students live shorter."""
import base64
import time

from app.security import key_id, make_token, parse_token

from .conftest import login, make_class

SECRET = "test-secret-0123456789abcdef"


def _payload(tok):
    p = tok.split(".")[0]
    return base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode().split(":")


def test_token_has_version_and_key_id():
    t = make_token(SECRET, 7, "student", 60, version=3)
    uid, role, exp, ver, kid = _payload(t)
    assert (uid, role, ver, kid) == ("7", "student", "3", key_id(SECRET))
    assert parse_token(SECRET, t) == (7, "student", 3)
    assert parse_token("another-secret-0123456789", t) is None


def test_old_format_and_expired_tokens_are_refused():
    import hashlib
    import hmac
    payload = base64.urlsafe_b64encode(f"7:student:{int(time.time()) + 60}".encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert parse_token(SECRET, f"{payload}.{sig}") is None
    assert parse_token(SECRET, make_token(SECRET, 7, "student", -1)) is None


def test_logout_everywhere_revokes_all_tokens(client):
    a1 = login(client, "alice")
    a2 = login(client, "alice")
    assert client.get("/api/me", headers=a1).status_code == 200
    assert client.post("/api/auth/logout", headers=a1).json() == {"ok": True}
    for h in (a1, a2):
        r = client.get("/api/me", headers=h)
        assert r.status_code == 401 and "sign in again" in r.json()["detail"]
    assert client.get("/api/me", headers=login(client, "alice")).status_code == 200


def test_teacher_signs_a_class_member_out(client):
    t = login(client, "teacher")
    k = client.get("/api/classes", headers=t).json()[0]          # the demo class (conftest)
    bob = login(client, "bob")
    assert client.post(f"/api/classes/{k['id']}/members/bob/sign-out", headers=t).json() == {"ok": True}
    assert client.get("/api/me", headers=bob).status_code == 401
    other = make_class(client, t, "Empty", members=[])
    assert client.post(f"/api/classes/{other['id']}/members/alice/sign-out", headers=t).status_code == 404


def test_student_tokens_live_shorter_than_teacher_tokens(client):
    def ttl(h):
        exp = int(_payload(h["Authorization"].split(" ")[1])[2])
        return exp - time.time()
    assert ttl(login(client, "alice")) < ttl(login(client, "teacher"))
    assert 7.5 * 3600 < ttl(login(client, "alice")) <= 8 * 3600
