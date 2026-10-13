from .conftest import login


def test_login_ok_and_me(client):
    r = client.post("/api/auth/login", json={"username": "teacher", "password": "teacher123"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "teacher" and body["name"] == "Demo Teacher" and body["token"]
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.json() == {"username": "teacher", "name": "Demo Teacher", "role": "teacher"}
    for u, n in [("alice", "Alice Chen"), ("bob", "Bob Karimov"), ("carol", "Carol Wang")]:
        r = client.post("/api/auth/login", json={"username": u, "password": f"{u}123"})
        assert r.json()["role"] == "student" and r.json()["name"] == n


def test_login_wrong(client):
    r = client.post("/api/auth/login", json={"username": "teacher", "password": "nope"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid username or password"}
    r = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401


def test_missing_and_tampered_token(client):
    assert client.get("/api/me").status_code == 401
    h = login(client, "alice")
    token = h["Authorization"].split()[1]
    payload, sig = token.split(".")
    # Forge the payload to claim the teacher role, keep the old signature.
    import base64
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    uid, _, exp, ver, kid = raw.split(":")
    forged = base64.urlsafe_b64encode(f"{uid}:teacher:{exp}:{ver}:{kid}".encode()).rstrip(b"=").decode()
    for bad in (f"{forged}.{sig}", token[:-2] + ("AA" if token[-2:] != "AA" else "BB"), "garbage"):
        r = client.get("/api/exams", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401, bad


def test_role_checks(client, teacher, alice):
    assert client.get("/api/exams", headers=alice).status_code == 403
    assert client.get("/api/student/exams", headers=teacher).status_code == 403
    assert client.get("/api/exams", headers=teacher).status_code == 200
    assert client.get("/api/student/exams", headers=alice).status_code == 200
