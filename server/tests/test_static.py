def test_root_redirects(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/teacher"


def test_spa_fallback_and_files(client):
    for path in ("/teacher", "/teacher/exams/e_123", "/student/exam/a_1", "/student/login"):
        r = client.get(path)
        assert r.status_code == 200 and "<title>SPA</title>" in r.text
    r = client.get("/assets/app.js")
    assert r.status_code == 200 and "console.log" in r.text
    r = client.get("/assets/missing.js")
    assert r.status_code == 200 and "SPA" in r.text  # SPA fallback
    r = client.get("/../../etc/passwd")
    assert "root:" not in r.text


def test_api_404_is_json(client):
    for method in ("get", "post", "put", "delete"):
        r = getattr(client, method)("/api/does/not/exist")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json() == {"detail": "Not Found"}
    assert client.get("/api/health").json() == {"ok": True, "demo_logins": True, "features": ["scan", "lms", "practice"]}
