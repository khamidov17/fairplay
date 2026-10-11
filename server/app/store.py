"""Small key/value store for hot-path state: Redis in production, in-memory in dev.

Holds heartbeats / live-room state, event counters, wrong-code counters and lockouts,
cached student-view exam content and cached attempt metadata. Nothing here is the
source of truth for grades or answers (those live in the database).
"""
from __future__ import annotations

import time
from typing import Protocol


class Store(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    async def delete(self, *keys: str) -> None: ...
    async def incr(self, key: str, ttl: int) -> int: ...
    async def getset(self, key: str, value: str, ttl: int | None = None) -> str | None: ...
    async def ttl(self, key: str) -> int: ...
    async def hset(self, key: str, field: str, value: str, ttl: int) -> None: ...
    async def hgetall(self, key: str) -> dict[str, str]: ...
    async def hincrby(self, key: str, field: str, amount: int, ttl: int) -> int: ...
    async def live_snapshot(self, exam_id: str) -> tuple[dict, dict, dict]: ...
    async def close(self) -> None: ...


class MemoryStore:
    """Single-process fallback. Not shared between workers: dev and tests only."""

    def __init__(self):
        self._data: dict[str, object] = {}
        self._exp: dict[str, float] = {}

    def _alive(self, key: str) -> bool:
        exp = self._exp.get(key)
        if exp is not None and exp <= time.monotonic():
            self._data.pop(key, None)
            self._exp.pop(key, None)
            return False
        return key in self._data

    def _expire(self, key: str, ttl: int | None) -> None:
        if ttl:
            self._exp[key] = time.monotonic() + ttl
        else:
            self._exp.pop(key, None)

    async def get(self, key):
        if not self._alive(key):
            return None
        v = self._data[key]
        return v if isinstance(v, str) else None

    async def set(self, key, value, ttl=None):
        self._data[key] = value
        self._expire(key, ttl)

    async def delete(self, *keys):
        for k in keys:
            self._data.pop(k, None)
            self._exp.pop(k, None)

    async def getset(self, key, value, ttl=None):
        old = await self.get(key)
        await self.set(key, value, ttl)
        return old

    async def incr(self, key, ttl):
        n = (int(self._data[key]) if self._alive(key) else 0) + 1
        fresh = not self._alive(key)
        self._data[key] = str(n)
        if fresh:
            self._expire(key, ttl)
        return n

    async def ttl(self, key):
        if not self._alive(key):
            return -2
        exp = self._exp.get(key)
        if exp is None:
            return -1
        return max(0, int(round(exp - time.monotonic())))

    def _hash(self, key) -> dict:
        if not self._alive(key) or not isinstance(self._data.get(key), dict):
            self._data[key] = {}
        return self._data[key]  # type: ignore[return-value]

    async def hset(self, key, field, value, ttl):
        self._hash(key)[field] = value
        self._expire(key, ttl)

    async def hgetall(self, key):
        if not self._alive(key) or not isinstance(self._data.get(key), dict):
            return {}
        return dict(self._data[key])  # type: ignore[arg-type]

    async def hincrby(self, key, field, amount, ttl):
        h = self._hash(key)
        n = int(h.get(field, 0)) + amount
        h[field] = str(n)
        self._expire(key, ttl)
        return n

    async def live_snapshot(self, exam_id):
        return (await self.hgetall(f"hb:{exam_id}"), await self.hgetall(f"evc:{exam_id}"),
                await self.hgetall(f"evl:{exam_id}"))

    async def close(self):
        pass


class RedisStore:
    def __init__(self, url: str):
        import redis.asyncio as aioredis
        # Blocking pool: under a burst (end-of-exam submit storm) a request waits up to 10 s
        # for a free connection instead of failing with MaxConnectionsError (HTTP 500).
        pool = aioredis.BlockingConnectionPool.from_url(
            url, decode_responses=True, max_connections=200, timeout=10, socket_timeout=30)
        self.r = aioredis.Redis(connection_pool=pool)

    async def get(self, key):
        return await self.r.get(key)

    async def set(self, key, value, ttl=None):
        await self.r.set(key, value, ex=ttl)

    async def delete(self, *keys):
        if keys:
            await self.r.delete(*keys)

    async def getset(self, key, value, ttl=None):
        return await self.r.set(key, value, ex=ttl, get=True)

    async def incr(self, key, ttl):
        pipe = self.r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        n, _ = await pipe.execute()
        return int(n)

    async def ttl(self, key):
        return int(await self.r.ttl(key))

    async def hset(self, key, field, value, ttl):
        pipe = self.r.pipeline()
        pipe.hset(key, field, value)
        pipe.expire(key, ttl)
        await pipe.execute()

    async def hgetall(self, key):
        return await self.r.hgetall(key)

    async def hincrby(self, key, field, amount, ttl):
        pipe = self.r.pipeline()
        pipe.hincrby(key, field, amount)
        pipe.expire(key, ttl)
        n, _ = await pipe.execute()
        return int(n or 0)

    async def live_snapshot(self, exam_id):
        pipe = self.r.pipeline()
        pipe.hgetall(f"hb:{exam_id}")
        pipe.hgetall(f"evc:{exam_id}")
        pipe.hgetall(f"evl:{exam_id}")
        hb, evc, evl = await pipe.execute()
        return hb, evc, evl

    async def close(self):
        await self.r.aclose()


def make_store(redis_url: str | None) -> Store:
    return RedisStore(redis_url) if redis_url else MemoryStore()


_store: Store | None = None


def set_store(s: Store | None) -> None:
    global _store
    _store = s


def get_store() -> Store:
    if _store is None:
        raise RuntimeError("store not initialised")
    return _store
