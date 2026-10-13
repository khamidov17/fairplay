"""Audit 2026-10-12 T-03: the Redis store behaves like the in-memory one where the app
relies on it (single-use nonces, counters that keep their first TTL, replay guard,
hashes). Runs against fakeredis; set PROCTORA_TEST_REDIS_URL to use a real server."""
import asyncio
import os

import pytest

fakeredis = pytest.importorskip("fakeredis")

from app.store import MemoryStore, RedisStore  # noqa: E402


def _redis_store() -> RedisStore:
    url = os.environ.get("PROCTORA_TEST_REDIS_URL")
    if url:
        return RedisStore(url)
    s = RedisStore.__new__(RedisStore)
    s.r = fakeredis.FakeAsyncRedis(decode_responses=True)
    return s


@pytest.fixture(params=["memory", "redis"])
def store(request):
    return MemoryStore() if request.param == "memory" else _redis_store()


def run(coro):
    return asyncio.run(coro)


def test_getdel_is_single_use(store):
    async def go():
        await store.set("chal:x", "1:e", 120)
        return await store.getdel("chal:x"), await store.getdel("chal:x")
    assert run(go()) == ("1:e", None)


def test_incr_keeps_the_first_ttl(store):
    async def go():
        a = await store.incr("cnt", 60)
        b = await store.incr("cnt", 600)          # a later TTL must not extend the window
        t = await store.ttl("cnt")
        return a, b, t
    a, b, t = run(go())
    assert (a, b) == (1, 2) and 0 < t <= 60


def test_incrby_and_hashes(store):
    async def go():
        n = await store.incrby("evtotal:a", 5, 60)
        n = await store.incrby("evtotal:a", 3, 60)
        await store.hset("ab:e", "a1", "x", 60)
        await store.hset("ab:e", "a2", "y", 60)
        await store.hdel("ab:e", "a1")
        h = await store.hgetall("ab:e")
        c = await store.hincrby("evc:e", "a2", 2, 60)
        return n, h, c
    assert run(go()) == (8, {"a2": "y"}, 2)


def test_app_session_flow_on_redis(tmp_path, static_dir, monkeypatch):
    """The app-session tests' core paths with the Redis store: one proof starts once,
    beats replay-guarded per session."""
    from fastapi.testclient import TestClient
    from app import main as main_mod
    from app.app_client import beat_message, sign, start_message
    from app.config import Settings
    from .conftest import SAMPLE_EXAM, login, make_class

    shared = _redis_store()
    monkeypatch.setattr(main_mod, "make_store", lambda url: shared)
    key = "unit-test-app-key-0123456789"
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}", redis_url="redis://fake",
                 secret="test-secret-0123456789abcdef", pbkdf2_iterations=1000, static_dir=str(static_dir),
                 uploads_dir=str(tmp_path / "up"), require_app=True, app_keys=[key], dev=False,
                 seed_demo=True)
    with TestClient(main_mod.create_app(s)) as c:
        t = login(c, "teacher")
        k = make_class(c, t)
        e = c.post("/api/exams", json={**SAMPLE_EXAM, "class_id": k["id"]}, headers=t).json()
        e = c.post(f"/api/exams/{e['id']}/open", headers=t).json()
        h = login(c, "alice")
        dev = "device-aaaa-1111"
        nonce = c.get(f"/api/student/exams/{e['id']}/challenge", headers=h).json()["nonce"]
        body = {"start_code": e["start_code"], "client": {
            "device_id": dev, "nonce": nonce, "signature": sign(key, start_message(nonce, dev)), "version": "0.5"}}
        a = c.post(f"/api/student/exams/{e['id']}/start", headers=h, json=body)
        assert a.status_code == 200, a.text
        assert c.post(f"/api/student/exams/{e['id']}/start", headers=h, json=body).status_code == 403
        a = a.json()

        def beat(ts):
            return c.post(f"/api/student/attempts/{a['attempt_id']}/app-beat", headers=h, json={
                "session": a["session"], "device_id": dev, "ts": ts, "locked": True, "checks": "console=1",
                "signature": sign(key, beat_message(a["attempt_id"], a["session"], dev, ts, True, "console=1"))})
        assert beat(1000).status_code == 200
        assert beat(1000).status_code == 409
        assert beat(2000).status_code == 200
