"""P12 GeoSphere Austria TAWES — CC BY 4.0, no key."""

from __future__ import annotations

import logging
from typing import Any

from gaia.clock import SimClock
from gaia.devices.live_p12 import GEOSPHERE_MESH, GeosphereWeather, _env

log = logging.getLogger("gaia.devices.live_p12_at")

P12_AT_COUNT = len(GEOSPHERE_MESH)
_CAP = "gaia.weather.read@v1"


def atlas_rows() -> list[tuple[str, float, float, str, str, str, str]]:
    rows: list[tuple[str, float, float, str, str, str, str]] = []
    for device_id, station, lat, lon, place in GEOSPHERE_MESH:
        rows.append((
            device_id, lat, lon, place, "weather", _CAP,
            f"GeoSphere TAWES · CC BY 4.0 ({station})",
        ))
    return rows


def register_p12_at(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    if _env("GAIA_GEOSPHERE_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        return 0
    n = 0
    for device_id, station, lat, lon, _place in GEOSPHERE_MESH:
        try:
            fleet.add(GeosphereWeather(
                device_id, clock, station=station, latitude=lat, longitude=lon,
                site=f"live-weather-{device_id}", key_dir=key_dir,
            ))
            n += 1
        except ValueError as exc:
            log.warning("GeoSphere %s skipped: %s", device_id, exc)
    return n


__all__ = ["GEOSPHERE_MESH", "P12_AT_COUNT", "atlas_rows", "register_p12_at"]
