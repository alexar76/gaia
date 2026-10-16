"""Day-ahead electricity prices from SMARD (Bundesnetzagentur) — CC BY 4.0, no key.

One relay per bidding zone: the exchange clearing price (SDAC / EPEX day-ahead) for
every quarter-hour of today and, from about 13:00 CET, tomorrow. That is the signal an
agent scheduling a battery, an EV charge, a heat pump or a compute job needs: what a
MWh costs now, and when in the published horizon it is cheapest.

**Why SMARD.** It is the source of record and licenses all fifteen zones below under
CC BY 4.0. Energy-Charts relays more zones, but marks several of them (SE1-SE3, FI, EE,
LT, LV, ES, IT-South, checked 2026-10-16) "private and internal use only", and limits a
client to a few requests. Those zones are not offered here.

**Which series is which zone** was established by value, not from documentation: each
SMARD filter below matched Energy-Charts' series for its zone at all 96 quarter-hours
of 2026-10-16 (to the cent). CH clears hourly; SMARD repeats each hour across its four
quarter-hours.

**Not a tariff.** A wholesale price excludes grid fees, levies and VAT; a household
pays several times it. The source text in every reading says so.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices._live_base import LiveDevice
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_power")

SMARD_BASE = "https://www.smard.de/app/chart_data"

#: zone → (SMARD filter id, name, pin latitude, pin longitude). Pins sit near each
#: zone's centre for the map; a price belongs to the whole zone, not to the pin.
SMARD_ZONES: dict[str, tuple[int, str, float, float]] = {
    "DE-LU": (4169, "Germany / Luxembourg", 51.17, 10.45),
    "AT": (4170, "Austria", 47.59, 14.14),
    "BE": (4996, "Belgium", 50.64, 4.67),
    "NO2": (4997, "Norway NO2 (south-west)", 58.97, 6.90),
    "DK1": (252, "Denmark DK1 (west)", 56.20, 9.00),
    "DK2": (253, "Denmark DK2 (east)", 55.50, 11.90),
    "FR": (254, "France", 46.60, 2.40),
    "IT-North": (255, "Italy North", 45.50, 9.80),
    "NL": (256, "Netherlands", 52.20, 5.50),
    "PL": (257, "Poland", 52.00, 19.40),
    "SE4": (258, "Sweden SE4 (south)", 56.00, 13.60),
    "CH": (259, "Switzerland", 46.80, 8.20),
    "SI": (260, "Slovenia", 46.10, 14.80),
    "CZ": (261, "Czechia", 49.80, 15.50),
    "HU": (262, "Hungary", 47.20, 19.40),
}

#: Countries that are one bidding zone, so a country (or a city in it) names the zone.
#: Denmark, Norway, Sweden and Italy are split and need the zone itself.
COUNTRY_ZONE: dict[str, str] = {
    "DE": "DE-LU", "LU": "DE-LU", "AT": "AT", "BE": "BE", "FR": "FR", "NL": "NL",
    "PL": "PL", "CH": "CH", "SI": "SI", "CZ": "CZ", "HU": "HU",
}
SPLIT_COUNTRIES: dict[str, tuple[str, ...]] = {
    "DK": ("DK1", "DK2"), "NO": ("NO2",), "SE": ("SE4",), "IT": ("IT-North",),
}

#: Zones whose public series are NOT licensed for commercial reuse (Energy-Charts marks
#: them "private and internal use only", 2026-10-16). Named so a caller asking for one
#: learns why it is refused, instead of being told the zone does not exist.
RESTRICTED_ZONES: frozenset[str] = frozenset({
    "SE1", "SE2", "SE3", "FI", "EE", "LT", "LV", "ES", "PT", "IT-SOUTH", "IT-CENTRE-NORTH",
    "IT-CENTRE-SOUTH", "IT-SICILY", "IT-SARDINIA", "IT-CALABRIA",
})

#: SMARD publishes once a day; fifteen minutes keeps every zone well inside their limits.
CACHE_TTL_S = 900.0
#: The latest published interval may not end before now, or the series is stale.
HORIZON_GRACE_S = 0.0

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()


def device_id_for(zone: str) -> str:
    return f"smard-price-{zone.lower()}"


def zone_for(text: str) -> str | None:
    """A zone code in any case ("de-lu", "IT-NORTH"), else None."""
    key = str(text or "").strip().upper()
    for zone in SMARD_ZONES:
        if zone.upper() == key:
            return zone
    return None


@dataclass(frozen=True)
class PriceSeries:
    """The quarter-hour prices of one zone, as published, with where they came from."""

    zone: str
    points: tuple[tuple[int, float], ...]  # (interval start, unix s; EUR/MWh), ascending
    step_s: int
    sources: tuple[str, ...]

    @property
    def horizon_end(self) -> int:
        return self.points[-1][0] + self.step_s

    def digest(self) -> str:
        """SHA-256 of the canonical series, so a buyer can recompute from the same data."""
        body = json.dumps([[t, p] for t, p in self.points], separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    def at(self, ts: float) -> tuple[int, float] | None:
        for start, price in self.points:
            if start <= ts < start + self.step_s:
                return start, price
        return None

    def ahead(self, ts: float, seconds: float) -> list[tuple[int, float]]:
        cur = self.at(ts)
        first = cur[0] if cur else int(ts)
        return [(t, p) for t, p in self.points if first <= t < ts + seconds]


def _cached(key: str, loader: Any) -> Any:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    payload = loader()
    with _CACHE_LOCK:
        _CACHE[key] = (now, payload)
    return payload


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


class SmardDayAheadPrice(LiveDevice):
    """Day-ahead price relay for one bidding zone (SMARD, CC BY 4.0)."""

    model = "GAIA-POWER-PRICE (SMARD day-ahead)"
    policy_id = "smard"
    timeout = 15.0
    fields = {
        "price_eur_mwh": "EUR/MWh",
        "price_min_24h_eur_mwh": "EUR/MWh",
        "price_mean_24h_eur_mwh": "EUR/MWh",
        "price_max_24h_eur_mwh": "EUR/MWh",
        "negative_intervals_24h": "count",
        "interval_start_unix": "s",
        "horizon_end_unix": "s",
        "resolution_min": "min",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, zone: str, **kw: Any):
        if zone not in SMARD_ZONES:
            raise ValueError(f"unknown bidding zone {zone!r}")
        super().__init__(device_id, clock, **kw)
        self.zone = zone
        self.filter_id, self.zone_name, lat, lon = SMARD_ZONES[zone]
        self.latitude, self.longitude = float(lat), float(lon)
        self.url = f"{SMARD_BASE}/{self.filter_id}/DE/index_quarterhour.json"
        self.source = (
            f"SMARD.de (Bundesnetzagentur) day-ahead price, bidding zone {zone} "
            f"({self.zone_name}), series {self.filter_id} · CC BY 4.0. Exchange clearing "
            "price (SDAC/EPEX), EUR/MWh — not a retail tariff: excludes grid fees, levies "
            "and VAT."
            + (" CH clears hourly; each hour repeats across its quarter-hours."
               if zone == "CH" else "")
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def _chunk_url(self, ts_ms: int) -> str:
        return (f"{SMARD_BASE}/{self.filter_id}/DE/"
                f"{self.filter_id}_DE_quarterhour_{int(ts_ms)}.json")

    def series(self) -> PriceSeries:
        """The last two weekly chunks, merged — today and tomorrow across a week boundary."""
        index = _cached(self.url, lambda: self._fetch(self.url))
        stamps = index.get("timestamps") if isinstance(index, dict) else None
        if not isinstance(stamps, list) or not stamps:
            raise DeviceOffline(f"{self.device_id}: SMARD index empty")
        chunks = sorted(int(s) for s in stamps if isinstance(s, (int, float)))[-2:]
        merged: dict[int, float] = {}
        sources: list[str] = []
        for ts_ms in chunks:
            url = self._chunk_url(ts_ms)
            payload = _cached(url, lambda u=url: self._fetch(u))
            rows = payload.get("series") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                continue
            sources.append(url)
            for row in rows:
                if (isinstance(row, list) and len(row) == 2 and isinstance(row[0], (int, float))
                        and isinstance(row[1], (int, float)) and not isinstance(row[1], bool)):
                    merged[int(row[0]) // 1000] = float(row[1])
        if len(merged) < 2:
            raise DeviceOffline(f"{self.device_id}: SMARD series has no published prices")
        points = tuple(sorted(merged.items()))
        steps = {b[0] - a[0] for a, b in zip(points, points[1:])}
        step = min(s for s in steps if s > 0) if steps else 900
        return PriceSeries(self.zone, points, int(step), tuple(sources))

    def sample(self) -> dict[str, float]:
        s = self.series()
        now = self.clock.now()
        if s.horizon_end + HORIZON_GRACE_S <= now:
            raise DeviceOffline(f"{self.device_id}: SMARD prices end before now (stale)")
        current = s.at(now)
        if current is None:
            raise DeviceOffline(f"{self.device_id}: no published price for the current interval")
        ahead = [p for _t, p in s.ahead(now, 86_400)]
        return {
            "price_eur_mwh": current[1],
            "price_min_24h_eur_mwh": min(ahead),
            "price_mean_24h_eur_mwh": sum(ahead) / len(ahead),
            "price_max_24h_eur_mwh": max(ahead),
            "negative_intervals_24h": float(sum(1 for p in ahead if p < 0)),
            "interval_start_unix": float(current[0]),
            "horizon_end_unix": float(s.horizon_end),
            "resolution_min": s.step_s / 60.0,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


def enabled_zones() -> list[str]:
    """GAIA_SMARD_ZONES (comma list) or every zone; GAIA_SMARD_ENABLED=0 turns all off."""
    if os.environ.get("GAIA_SMARD_ENABLED", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return []
    raw = os.environ.get("GAIA_SMARD_ZONES", "").strip()
    if not raw:
        return list(SMARD_ZONES)
    zones = []
    for part in raw.split(","):
        z = zone_for(part)
        if z is None:
            log.warning("GAIA_SMARD_ZONES: unknown zone %r skipped", part.strip())
        elif z not in zones:
            zones.append(z)
    return zones


def register_power_prices(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    n = 0
    for zone in enabled_zones():
        try:
            fleet.add(SmardDayAheadPrice(device_id_for(zone), clock, zone=zone,
                                         site=f"power-zone-{zone.lower()}", key_dir=key_dir))
            n += 1
        except ValueError as exc:
            log.warning("SMARD %s skipped: %s", zone, exc)
    return n


def atlas_rows() -> list[tuple[str, float, float, str, str, str, str]]:
    return [
        (device_id_for(zone), lat, lon, f"{name} ({zone})", "power_price",
         "gaia.power.price.read@v1", "SMARD day-ahead · CC BY 4.0; wholesale, not a tariff")
        for zone, (_fid, name, lat, lon) in SMARD_ZONES.items()
    ]


__all__ = [
    "SMARD_ZONES", "RESTRICTED_ZONES", "COUNTRY_ZONE", "SPLIT_COUNTRIES", "PriceSeries", "SmardDayAheadPrice",
    "device_id_for", "zone_for", "enabled_zones", "register_power_prices", "atlas_rows",
    "clear_cache",
]
