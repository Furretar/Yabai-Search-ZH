"""A tiny in-memory failure counter for the login form.

It is per process, so it slows password guessing down rather than stopping a
distributed attack; Cloudflare rate-limiting rules in front of /accounts/login/
are the stronger layer.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class FailureThrottle:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._failures: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        hits = self._failures[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(len(self._trim(key, now)) >= self.limit for key in keys)

    def fail(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                self._trim(key, now).append(now)
            if len(self._failures) > 10000:
                for key in [k for k, v in self._failures.items() if not v][:2000]:
                    del self._failures[key]

    def clear(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


# 8 wrong passwords in 15 minutes, counted per address and per username.
login_throttle = FailureThrottle(limit=8, window_seconds=15 * 60)
