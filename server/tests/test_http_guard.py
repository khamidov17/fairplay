"""Audit 2026-10-12 L-04 / S-16."""
from app.http_guard import DEFAULT_LIMIT, limit_for

from .conftest import login


def test_limits_per_path():
    assert limit_for("/api/exams/e_1/scans") > 200 * 1024 * 1024
    assert limit_for("/api/uploads") < 10 * 1024 * 1024
    assert limit_for("/api/student/attempts/a/answers") == DEFAULT_LIMIT


def test_declared_oversize_is_refused_before_reading(client):
    t = login(client, "teacher")
    r = client.post("/api/uploads", headers={**t, "Content-Length": str(50 * 1024 * 1024),
                                            "Content-Type": "multipart/form-data; boundary=x"}, content=b"x")
    assert r.status_code == 413 and "too large" in r.json()["detail"]


def test_streamed_oversize_is_stopped(client):
    alice = login(client, "alice")

    def chunks():
        for _ in range(20):
            yield b"x" * (1024 * 1024)
    r = client.put("/api/student/attempts/a_x/answers", headers={**alice, "Content-Type": "application/json"},
                   content=chunks())
    assert r.status_code == 413


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    page = client.get("/teacher")
    assert "frame-ancestors 'none'" in page.headers.get("content-security-policy", "")
    assert "strict-transport-security" not in page.headers          # plain http in tests


def test_csv_cells_cannot_be_formulas(client):
    from app.routes_gradebook import csv_cell
    assert csv_cell('=HYPERLINK("http://x","y")') == "'=HYPERLINK(\"http://x\",\"y\")"
    assert csv_cell("+1") == "'+1" and csv_cell("@SUM(A1)") == "'@SUM(A1)" and csv_cell("-x") == "'-x"
    assert csv_cell("Midterm") == "Midterm" and csv_cell(-3.5) == -3.5
    t = login(client, "teacher")
    client.post("/api/exams", json={"title": "=cmd|' /C calc'!A0", "duration_min": 5,
                                   "questions": [{"type": "mcq", "prompt": "?", "points": 1, "options": ["a", "b"],
                                                  "correct_index": 0, "topics": ["x"]}]}, headers=t)
    e = client.get("/api/exams", headers=t).json()
    eid = [x for x in e if x["title"].startswith("=")][0]["id"]
    client.post(f"/api/exams/{eid}/open", headers=t)
    client.post(f"/api/exams/{eid}/close", headers=t)
    body = client.get("/api/gradebook.csv", headers=t).text
    assert "'=cmd" in body and ",=cmd" not in body


def test_naive_schedule_times_are_refused(client):
    t = login(client, "teacher")
    from .conftest import SAMPLE_EXAM
    r = client.post("/api/exams", json={**SAMPLE_EXAM, "scheduled_at": "2026-10-12T09:00:00"}, headers=t)
    assert r.status_code == 422
    r = client.post("/api/exams", json={**SAMPLE_EXAM, "scheduled_at": "2026-10-12T09:00:00+08:00"}, headers=t)
    assert r.status_code == 201 and r.json()["scheduled_at"].startswith("2026-10-12T01:00:00")


def test_pool_budget_is_checked(tmp_path, static_dir):
    import pytest
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'p.db'}", redis_url="redis://unused", dev=False,
                 secret="test-secret-0123456789abcdef", app_keys=["k"], require_app=False,
                 static_dir=str(static_dir), uploads_dir=str(tmp_path / "up"),
                 db_pool_size=10, db_max_overflow=20, workers=4, replicas=5, db_max_connections=500)
    with pytest.raises(RuntimeError, match="Database pools can open 600"):
        with TestClient(create_app(s)):
            pass
