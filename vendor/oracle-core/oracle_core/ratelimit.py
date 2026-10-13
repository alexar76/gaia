"""Tiny fixed-window rate limiter — bounds DoS / cost on open endpoints.

Keyed per client (typically the real client IP supplied by the reverse proxy)
so a single noisy client cannot exhaust the budget for everyone. An unkeyed
``allow()`` falls back to a shared bucket for backwards compatibility.

Requests may carry a **cost**. ``allow(key)`` costs 1, so a limiter constructed
with ``RateLimiter(120)`` is the plain "120 requests per minute" it always was.
Passing a larger cost rations something other than request count:
``RateLimiter(20_000)`` fed ``cost=6800`` is a budget of 20 CPU-seconds per
minute that admits three max-difficulty VDF evaluations, or two thousand cheap
ones.

That distinction is not decoration. Two capabilities in this family price
themselves in sequential squarings, so one call may cost microseconds and the
next may cost 36 seconds of un-parallelisable CPU. A limiter that counts calls
has to pick which of those to be wrong about: set it for the expensive call and
ordinary exploration is refused after two requests (this is not hypothetical —
it broke Aestus's own test suite), set it for the cheap call and a loop of
expensive ones melts the box. Charging each request what it actually costs is
the only setting that is right for both.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    """Fixed-window budget over a rolling ``window_s``, keyed per client.

    ``limit`` is a budget in whatever unit callers pass as ``cost``. With the
    default cost of 1 that unit is "requests"; oracle_core also uses milliseconds
    of expected CPU (see :mod:`oracle_core.tiers`).
    """

    def __init__(self, limit: float, window_s: float = 60.0, *, max_keys: int = 8192) -> None:
        self.limit = limit
        self.window = window_s
        self.max_keys = max_keys
        # (timestamp, cost) pairs, oldest first.
        self._buckets: dict[str, deque[tuple[float, float]]] = defaultdict(deque)

    def allow(self, key: str = "*", cost: float = 1.0) -> bool:
        """Charge ``cost`` to ``key``; return False (and charge nothing) if over budget.

        A single request costing more than the whole budget is refused rather than
        admitted-and-then-blocking: otherwise one oversized call would be let through
        every window no matter how large it was, which is precisely the case the
        budget exists to stop.
        """
        now = time.monotonic()
        # Opportunistic eviction so idle/one-shot keys (e.g. spoofed source IPs)
        # cannot grow the bucket map without bound.
        if len(self._buckets) > self.max_keys:
            self._evict_stale(now)
        hits = self._buckets[key]
        while hits and now - hits[0][0] > self.window:
            hits.popleft()
        if sum(c for _, c in hits) + cost > self.limit:
            return False
        hits.append((now, cost))
        return True

    def would_allow(self, key: str = "*", cost: float = 1.0) -> bool:
        """Would ``allow(key, cost)`` succeed? Charges nothing.

        Lets a caller that must satisfy several budgets at once (per-client AND
        aggregate) check them all before charging any, so a request refused by the
        second budget does not leave the first one debited for work never performed.
        """
        return self.spent(key) + cost <= self.limit

    def spent(self, key: str = "*") -> float:
        """Budget consumed by ``key`` in the current window — for diagnostics/tests."""
        now = time.monotonic()
        hits = self._buckets.get(key)
        if not hits:
            return 0.0
        return sum(c for t, c in hits if now - t <= self.window)

    def _evict_stale(self, now: float) -> None:
        stale = [k for k, h in self._buckets.items() if not h or now - h[-1][0] > self.window]
        for k in stale:
            del self._buckets[k]

    def reset(self) -> None:
        self._buckets.clear()
