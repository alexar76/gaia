"""Simulated clock — deterministic, steerable time for the whole gateway.

Every device samples against ONE shared clock so co-located simulators stay
physically consistent (the two weather stations see the same afternoon).
Tests and demos advance it explicitly; a live deployment can run it in
real-time mode where ``now()`` follows the wall clock from a fixed anchor.
"""

from __future__ import annotations

import time


class SimClock:
    """Epoch-seconds clock, either frozen-and-stepped (default) or real-time.

    Frozen mode is what tests and the demo use: ``advance()`` moves time, so a
    day of weather can pass in a millisecond and every reading is reproducible.
    """

    def __init__(self, start_epoch: float = 1_767_225_600.0, realtime: bool = False):
        # Default anchor: 2026-01-01T00:00:00Z — a stable, obviously-simulated origin.
        self._start = float(start_epoch)
        self._elapsed = 0.0
        self._realtime = realtime
        self._rt_anchor = time.time()

    def now(self) -> float:
        if self._realtime:
            return self._start + (time.time() - self._rt_anchor)
        return self._start + self._elapsed

    def advance(self, seconds: float) -> float:
        """Step frozen time forward. No-op guard: negative steps are rejected."""
        if seconds < 0:
            raise ValueError("time only moves forward")
        self._elapsed += float(seconds)
        return self.now()

    def iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now()))

    def hour_of_day(self) -> float:
        """Local-ish hour in [0, 24) — the sims treat the site as UTC."""
        t = time.gmtime(self.now())
        return t.tm_hour + t.tm_min / 60.0 + t.tm_sec / 3600.0

    def day_of_week(self) -> int:
        """0=Monday … 6=Sunday (drives weekday/weekend load patterns)."""
        return time.gmtime(self.now()).tm_wday
