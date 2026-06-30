"""
Cache backends.

The funnel engine caches day-by-day log fetches (potentially large), so the
default day-log cache should be Redis or in-memory. Firestore is offered too but
is only safe for small values (it has a 1 MB per-document limit).

Interface: `cache.get(key)` / `cache.set(key, value, timeout)`.
"""
import pickle
import time
from threading import Lock
from typing import Any, Optional

from app import settings


class BaseCache:
    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError

    def set(self, key: str, value: Any, timeout: int) -> None:
        raise NotImplementedError


class MemoryCache(BaseCache):
    """Per-instance dict cache. Good for dev and single-instance Cloud Run."""

    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()

    def get(self, key):
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at < time.time():
                del self._store[key]
                return None
            return value

    def set(self, key, value, timeout):
        with self._lock:
            self._store[key] = (time.time() + timeout, value)


class RedisCache(BaseCache):
    """Recommended for production. Stores pickled blobs; no size limit issues."""

    def __init__(self, url: str):
        import redis  # lazy import so the dep is optional

        self._client = redis.Redis.from_url(url)

    def get(self, key):
        raw = self._client.get(key)
        return pickle.loads(raw) if raw is not None else None

    def set(self, key, value, timeout):
        self._client.set(key, pickle.dumps(value), ex=timeout)


class FirestoreCache(BaseCache):
    """Serverless cache. WARNING: only safe for small values (<1 MB pickled)."""

    def __init__(self, collection: str):
        from google.cloud import firestore

        self._col = firestore.Client().collection(collection)

    def get(self, key):
        doc = self._col.document(key).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        if data["expires_at"] < time.time():
            self._col.document(key).delete()
            return None
        return pickle.loads(data["value"])

    def set(self, key, value, timeout):
        self._col.document(key).set(
            {"value": pickle.dumps(value), "expires_at": time.time() + timeout}
        )


def build_cache() -> BaseCache:
    backend = settings.CACHE_BACKEND
    if backend == "redis":
        return RedisCache(settings.REDIS_URL)
    if backend == "firestore":
        return FirestoreCache(settings.CACHE_COLLECTION)
    return MemoryCache()


# Single shared instance used by data sources.
cache = build_cache()
