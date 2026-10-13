"""A small cache-backed mutex for read-modify-write of a cached value."""
import time

from django.core.cache import cache


class cache_mutex:
    """Hold ``key`` in the cache for the duration of a ``with`` block.

    ``cache.add`` is atomic on every backend we use (locmem, Redis), so only one
    holder at a time gets in. A crashed holder's lock expires after ``ttl``
    seconds; a waiter gives up after ``wait`` seconds and breaks the lock so a
    stale entry can never deadlock the feature.
    """

    def __init__(self, key, ttl=10, wait=2.0):
        self.key = key
        self.ttl = ttl
        self.wait = wait

    def __enter__(self):
        deadline = time.monotonic() + self.wait
        while not cache.add(self.key, 1, self.ttl):
            if time.monotonic() >= deadline:
                cache.delete(self.key)
                break
            time.sleep(0.01)
        return self

    def __exit__(self, *exc):
        cache.delete(self.key)
        return False
