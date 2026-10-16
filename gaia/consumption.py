"""A plug's consumption statistics: what its meter saw, in hourly buckets, kept durably.

Every ``sample_s`` (60 s) the recorder reads each actuator's meter — the same attested reading
a command's evidence carries — and adds it to the UTC hour it falls in: the energy (from the
plug's own energy register, or power × time when the register is missing or was reset), the
mean and peak power, and how long the relay was on. Buckets live in the oracle's store
(oracle_core.storage: SQLite by default, Postgres when ORACLE_DATABASE_URL says so).

They are read two ways:

* the owner, free: gaia.owner.consumption@v1 with the owner's EIP-712 signature;
* anyone else, paid through a hub, only where the owner set ``consumption.sale: true``:
  gaia.consumption.read@v1, no finer than the owner's ``resolution`` (hour or day) and no
  further back than ``history_days``.

A series is signed by the plug's device key (the key its readings carry) over its canonical
JSON, so a buyer can show where the numbers came from. Switching is never for sale.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("gaia.consumption")

HOUR = 3600
DAY = 86400
#: One socket at 230 V × 16 A: an energy step above this over the gap is a register reset.
_SOCKET_MAX_W = 3680.0
#: A gap longer than this between two samples is a pause (GAIA down), not consumption.
_MAX_GAP_S = 600.0
MAX_BUCKETS = {"hour": 24 * 31, "day": 366}

_DDL = (
    """CREATE TABLE IF NOT EXISTS gaia_consumption (
        actuator_id TEXT NOT NULL,
        hour BIGINT NOT NULL,
        energy_wh DOUBLE PRECISION NOT NULL DEFAULT 0,
        power_sum_w DOUBLE PRECISION NOT NULL DEFAULT 0,
        max_w DOUBLE PRECISION NOT NULL DEFAULT 0,
        on_s DOUBLE PRECISION NOT NULL DEFAULT 0,
        samples INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (actuator_id, hour))""",
)


def series_canonical(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class _Last:
    ts: float
    energy_wh: float | None


class ConsumptionStore:
    def __init__(self, store: Any):
        self.store = store
        store.script(_DDL)

    def add(self, actuator_id: str, ts: float, *, energy_wh: float, power_w: float,
            on_s: float) -> None:
        hour = int(ts // HOUR) * HOUR
        greatest = "GREATEST" if self.store.dialect == "postgres" else "MAX"
        self.store.execute(
            f"""INSERT INTO gaia_consumption (actuator_id, hour, energy_wh, power_sum_w, max_w, on_s, samples)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (actuator_id, hour) DO UPDATE SET
                  energy_wh = gaia_consumption.energy_wh + excluded.energy_wh,
                  power_sum_w = gaia_consumption.power_sum_w + excluded.power_sum_w,
                  max_w = {greatest}(gaia_consumption.max_w, excluded.max_w),
                  on_s = gaia_consumption.on_s + excluded.on_s,
                  samples = gaia_consumption.samples + 1""",
            (actuator_id, hour, max(0.0, energy_wh), max(0.0, power_w), max(0.0, power_w),
             max(0.0, on_s)))

    def series(self, actuator_id: str, from_ts: int, to_ts: int, resolution: str) -> list[dict[str, Any]]:
        rows = self.store.query(
            "SELECT hour, energy_wh, power_sum_w, max_w, on_s, samples FROM gaia_consumption "
            "WHERE actuator_id = ? AND hour >= ? AND hour < ? ORDER BY hour",
            (actuator_id, int(from_ts) // HOUR * HOUR, int(to_ts)))
        width = HOUR if resolution == "hour" else DAY
        out: dict[int, dict[str, float]] = {}
        for hour, energy, psum, peak, on_s, samples in rows:
            start = int(hour) // width * width
            b = out.setdefault(start, {"energy_wh": 0.0, "power_sum_w": 0.0, "max_w": 0.0,
                                       "on_s": 0.0, "samples": 0})
            b["energy_wh"] += float(energy)
            b["power_sum_w"] += float(psum)
            b["max_w"] = max(b["max_w"], float(peak))
            b["on_s"] += float(on_s)
            b["samples"] += int(samples)
        return [{
            "start": _iso(start), "end": _iso(start + width),
            "energy_wh": round(b["energy_wh"], 3),
            "mean_w": round(b["power_sum_w"] / b["samples"], 2) if b["samples"] else 0.0,
            "peak_w": round(b["max_w"], 2),
            "on_minutes": round(b["on_s"] / 60.0, 2),
            "samples": b["samples"],
        } for start, b in sorted(out.items())]

    def purge(self, older_than_ts: float) -> None:
        self.store.execute("DELETE FROM gaia_consumption WHERE hour < ?", (int(older_than_ts),))


class ConsumptionRecorder:
    """Samples every actuator's meter on a timer and adds each sample to its hour."""

    def __init__(self, store: ConsumptionStore, read: Callable[[str], dict[str, Any]],
                 actuator_ids: list[str], *, sample_s: float = 60.0,
                 retention_days: int = 400, now: Callable[[], float] = time.time):
        self.store = store
        self.read = read
        self.actuator_ids = list(actuator_ids)
        self.sample_s = sample_s
        self.retention_days = retention_days
        self.now = now
        self._last: dict[str, _Last] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_purge = 0.0

    def sample_once(self) -> None:
        for aid in self.actuator_ids:
            try:
                reading = self.read(aid)
            except Exception as exc:  # noqa: BLE001 — a plug offline is a gap, not a crash
                log.info("consumption: %s not read: %s", aid, exc)
                self._last.pop(aid, None)
                continue
            values = (reading.get("reading") or {}).get("values") or {}
            ts = self.now()
            power = _num(values.get("power_w"))
            energy = _num(values.get("energy_wh"))
            relay_on = (_num(values.get("relay_on")) or 0.0) >= 0.5
            last = self._last.get(aid)
            self._last[aid] = _Last(ts, energy)
            if last is None or power is None:
                continue  # the first sample only sets the baseline
            dt = ts - last.ts
            if not 0 < dt <= _MAX_GAP_S:
                continue
            step = None
            if energy is not None and last.energy_wh is not None:
                step = energy - last.energy_wh
                if step < 0 or step > _SOCKET_MAX_W * dt / 3600.0 + 1.0:
                    step = None  # a reset or a glitch: fall back to power × time
            if step is None:
                step = power * dt / 3600.0
            self.store.add(aid, ts, energy_wh=step, power_w=power, on_s=dt if relay_on else 0.0)
        if self.now() - self._last_purge > DAY:
            self._last_purge = self.now()
            try:
                self.store.purge(self.now() - self.retention_days * DAY)
            except Exception as exc:  # noqa: BLE001
                log.warning("consumption purge failed: %s", exc)

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.sample_s):
                try:
                    self.sample_once()
                except Exception as exc:  # noqa: BLE001 — keep sampling
                    log.warning("consumption sample failed: %s", exc)
        self.sample_once()
        self._thread = threading.Thread(target=loop, name="gaia-consumption", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def parse_time(value: Any, default: float) -> int:
    """Unix seconds or an ISO 8601 UTC time (2026-10-16T00:00:00Z); empty → default."""
    if value in (None, ""):
        return int(default)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{text[:40]!r} is not a time: use unix seconds or ISO 8601 (…Z)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def recorder_from_env(store: ConsumptionStore, read: Callable[[str], dict[str, Any]],
                      actuator_ids: list[str]) -> ConsumptionRecorder:
    return ConsumptionRecorder(
        store, read, actuator_ids,
        sample_s=float(os.environ.get("GAIA_CONSUMPTION_SAMPLE_S", "60") or 60),
        retention_days=int(os.environ.get("GAIA_CONSUMPTION_RETENTION_DAYS", "400") or 400))


__all__ = ["ConsumptionRecorder", "ConsumptionStore", "MAX_BUCKETS", "parse_time",
           "recorder_from_env", "series_canonical"]
