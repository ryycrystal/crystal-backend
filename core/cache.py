from __future__ import annotations
from typing import Any, Dict, Tuple
import time
import threading


class LocalCache:
    def __init__(self, max_items: int = 10000) -> None:
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self._max_items = max_items

    def get(self, key: str) -> Any | None:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None

            expires_at, value = item
            if expires_at < now:
                self._data.pop(key, None)
                return None

            return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return

        expires_at = time.time() + ttl_seconds

        with self._lock:
            if len(self._data) >= self._max_items:
                try:
                    oldest_key = next(iter(self._data))
                    self._data.pop(oldest_key, None)
                except StopIteration:
                    pass

            self._data[key] = (expires_at, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_cache = LocalCache()


def get(key: str) -> Any | None:
    return _cache.get(key)


def set(key: str, value: Any, ttl_seconds: float) -> None:
    _cache.set(key, value, ttl_seconds)


def clear() -> None:
    _cache.clear()