"""P12 Lithuania LHMT Meteo.lt — CC BY-SA 4.0, no key."""

from __future__ import annotations

import logging
from typing import Any

from gaia.clock import SimClock
from gaia.devices.live_p12 import (
    LHMT_HYDRO_MESH,
    LHMT_WX_MESH,
    LhmtRiver,
    LhmtWeather,
    _env,
)

log = logging.getLogger("gaia.devices.live_p12_lt")

P12_LT_COUNT = len(LHMT_WX_MESH) + len(LHMT_HYDRO_MESH)


def atlas_rows() -> list[tuple[str, float, float, str, str, str, str]]:
    rows: list[tuple[str, float, float, str, str, str, str]] = []
    for device_id, _code, lat, lon, place in LHMT_WX_MESH:
        rows.append((
            device_id, lat, lon, place, "weather", "gaia.weather.read@v1",
            "LHMT Meteo.lt · CC BY-SA 4.0",
        ))
    for device_id, _code, lat, lon, place in LHMT_HYDRO_MESH:
        rows.append((
            device_id, lat, lon, place, "river", "gaia.river.read@v1",
            "LHMT hydro · CC BY-SA 4.0",
        ))
    return rows


def register_p12_lt(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    if _env("GAIA_LHMT_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        return 0
    n = 0
    for device_id, station, lat, lon, _place in LHMT_WX_MESH:
        try:
            fleet.add(LhmtWeather(
                device_id, clock, station=station, latitude=lat, longitude=lon,
                site=f"live-weather-{device_id}", key_dir=key_dir,
            ))
            n += 1
        except ValueError as exc:
            log.warning("LHMT weather %s skipped: %s", device_id, exc)
    for device_id, station, lat, lon, _place in LHMT_HYDRO_MESH:
        try:
            fleet.add(LhmtRiver(
                device_id, clock, station=station, latitude=lat, longitude=lon,
                site=f"live-river-{device_id}", key_dir=key_dir,
            ))
            n += 1
        except ValueError as exc:
            log.warning("LHMT hydro %s skipped: %s", device_id, exc)
    return n


__all__ = [
    "LHMT_WX_MESH",
    "LHMT_HYDRO_MESH",
    "P12_LT_COUNT",
    "atlas_rows",
    "register_p12_lt",
]
