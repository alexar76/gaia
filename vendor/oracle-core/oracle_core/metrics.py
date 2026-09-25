"""Rolling per-capability metrics — real measured latency and success rate."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Metrics:
    def __init__(self, window: int = 200) -> None:
        self._window = window
        self._latency: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))
        self._success: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))

    def record(self, capability_id: str, latency_ms: float, success: bool) -> None:
        self._latency[capability_id].append(float(latency_ms))
        self._success[capability_id].append(1 if success else 0)

    def count(self, capability_id: str) -> int:
        return len(self._success.get(capability_id, ()))

    def p50_latency_ms(self, capability_id: str) -> Optional[float]:
        samples = self._latency.get(capability_id)
        if not samples:
            return None
        ordered = sorted(samples)
        return ordered[len(ordered) // 2]

    def success_rate(self, capability_id: str) -> Optional[float]:
        samples = self._success.get(capability_id)
        if not samples:
            return None
        return sum(samples) / len(samples)

    def reset(self) -> None:
        self._latency.clear()
        self._success.clear()
