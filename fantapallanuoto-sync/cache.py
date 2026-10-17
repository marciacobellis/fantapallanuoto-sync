import threading
import time


class TTLCache:
    """Thread-safe in-memory cache with per-entry expiry."""

    def __init__(self):
        self._lock = threading.RLock()
        self._data = {}

    def get(self, key, max_age=None):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, value = entry
            if max_age is not None and (time.time() - ts) > max_age:
                return None
            return value

    def get_fresh(self, key):
        """Return (value, age_seconds) even if stale, or (None, None)."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None, None
            ts, value = entry
            return value, (time.time() - ts)

    def set(self, key, value):
        with self._lock:
            self._data[key] = (time.time(), value)
        return value

    def age(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            return time.time() - entry[0]


CACHE = TTLCache()
