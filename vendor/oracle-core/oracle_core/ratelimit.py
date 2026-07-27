"""Tiny fixed-window rate limiter — bounds DoS / cost on open endpoints.

Keyed per client (typically the real client IP supplied by the reverse proxy)
so a single noisy client cannot exhaust the budget for everyone. An unkeyed
``allow()`` falls back to a shared bucket for backwards compatibility.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0, *, max_keys: int = 8192) -> None:
        self.limit = limit
        self.window = window_s
        self.max_keys = max_keys
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str = "*") -> bool:
        now = time.monotonic()
        # Opportunistic eviction so idle/one-shot keys (e.g. spoofed source IPs)
        # cannot grow the bucket map without bound.
        if len(self._buckets) > self.max_keys:
            self._evict_stale(now)
        hits = self._buckets[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def _evict_stale(self, now: float) -> None:
        stale = [k for k, h in self._buckets.items() if not h or now - h[-1] > self.window]
        for k in stale:
            del self._buckets[k]

    def reset(self) -> None:
        self._buckets.clear()
