import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def static_dir(tmp_path):
    d = tmp_path / "static"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>SPA</title>")
    (d / "assets" / "app.js").write_text("console.log('hi')")
    return d


def _reset_external(db_url, redis_url):
    """Optional: run the suite against real Postgres/Redis (wipes them!)."""
    import asyncio

    async def go():
        if db_url:
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import create_async_engine
            eng = create_async_engine(db_url)
            async with eng.begin() as conn:
                await conn.execute(text("DROP SCHEMA public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
            await eng.dispose()
        if redis_url:
            import redis.asyncio as redis
            r = redis.from_url(redis_url)
            await r.flushdb()
            await r.aclose()

    asyncio.run(go())


@pytest.fixture
def client(tmp_path, static_dir):
    # Default: temp SQLite + in-memory store. Set PROCTORA_TEST_DATABASE_URL /
    # PROCTORA_TEST_REDIS_URL to run the same tests against scratch Postgres / Redis.
    db_url = os.environ.get("PROCTORA_TEST_DATABASE_URL")
    redis_url = os.environ.get("PROCTORA_TEST_REDIS_URL")
    _reset_external(db_url, redis_url)
    settings = Settings(database_url=db_url or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
                        redis_url=redis_url, secret="test-secret", pbkdf2_iterations=1000,
                        seed_loadtest_users=0, static_dir=str(static_dir),
                        uploads_dir=str(tmp_path / "uploads"))
    with TestClient(create_app(settings)) as c:
        yield c


def login(client, username, password=None):
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password or f"{username}123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def teacher(client):
    return login(client, "teacher")


@pytest.fixture
def alice(client):
    return login(client, "alice")


@pytest.fixture
def bob(client):
    return login(client, "bob")


SAMPLE_EXAM = {
    "title": "Midterm",
    "duration_min": 30,
    "questions": [
        {"type": "mcq", "prompt": "2+2?", "points": 2, "options": ["3", "4", "5"], "correct_index": 1,
         "topics": ["arithmetic"]},
        {"type": "numeric", "prompt": "pi?", "points": 3, "answer_number": 3.14, "tolerance": 0.01,
         "topics": ["constants"]},
        {"type": "short", "prompt": "Capital of France?", "points": 5, "topics": ["geography"],
         "accepted_answers": ["Paris", "  Paris  City "]},
    ],
}


@pytest.fixture
def live_exam(client, teacher):
    e = client.post("/api/exams", json=SAMPLE_EXAM, headers=teacher).json()
    e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    return e
