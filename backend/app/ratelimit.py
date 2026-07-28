"""Sliding-window rate limiter for the login endpoint.

ponytail: in-process counters, so the limit is per API worker. Move the window
into Redis (already in docker-compose) the moment the API runs more than one
worker, or the effective limit multiplies by the worker count.
"""

import time
from collections import deque

# Stop an attacker from growing the dict without bound by rotating keys.
MAX_KEYS = 10_000


class SlidingWindow:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock=time.monotonic,  # noqa: ANN001
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit must be >= 1 and window_seconds > 0")
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record an attempt. False means the caller is over the limit."""
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > MAX_KEYS:
            self._evict(cutoff)
        return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until the oldest recorded attempt falls out of the window."""
        hits = self._hits.get(key)
        if not hits:
            return 0
        remaining = hits[0] + self.window_seconds - self.clock()
        return max(1, int(remaining) + 1) if remaining > 0 else 0

    def reset(self, key: str | None = None) -> None:
        """Clear one key, or all of them. For tests and admin unblocking."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)

    def _evict(self, cutoff: float) -> None:
        stale = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        if len(self._hits) > MAX_KEYS:
            # Still oversized: drop the least recently active keys.
            ordered = sorted(self._hits.items(), key=lambda kv: kv[1][-1])
            for key, _ in ordered[: len(self._hits) - MAX_KEYS]:
                del self._hits[key]


# 10 attempts per 5 minutes per client address. Keyed by address, not email:
# keying by email would let anyone lock a real user out, and would be trivially
# bypassed by rotating the submitted address.
login_limiter = SlidingWindow(limit=10, window_seconds=300)
