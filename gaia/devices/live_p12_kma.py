"""P12 KMA ASOS Korea — Public Nuri Type 1; register only when GAIA_KMA_SERVICE_KEY is set."""

from __future__ import annotations

import logging
from typing import Any

from gaia.clock import SimClock
from gaia.devices.live_p12 import KMA_MESH, KmaAsosWeather, _env

log = logging.getLogger("gaia.devices.live_p12_kma")

P12_KMA_COUNT = len(KMA_MESH)
_CAP = "gaia.weather.read@v1"


def atlas_rows() -> list[tuple[str, float, float, str, str, str, str]]:
    """Catalog pins always published (key-gated fleet registration)."""
    rows: list[tuple[str, float, float, str, str, str, str]] = []
    for device_id, station, lat, lon, place in KMA_MESH:
        rows.append((
            device_id, lat, lon, place, "weather", _CAP,
            "KMA ASOS · Public Nuri Type 1 (needs GAIA_KMA_SERVICE_KEY)",
        ))
    return rows


def register_p12_kma(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    kma_key = _env("GAIA_KMA_SERVICE_KEY")
    if not kma_key or _env("GAIA_KMA_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        if _env("GAIA_KMA_ENABLED", "1").lower() in ("1", "true", "yes", "on"):
            log.info("KMA ASOS skipped (set GAIA_KMA_SERVICE_KEY to enable)")
        return 0
    n = 0
    for device_id, station, lat, lon, _place in KMA_MESH:
        try:
            fleet.add(KmaAsosWeather(
                device_id, clock, station=station, api_key=kma_key,
                latitude=lat, longitude=lon,
                site=f"live-weather-{device_id}", key_dir=key_dir,
            ))
            n += 1
        except ValueError as exc:
            log.warning("KMA %s skipped: %s", device_id, exc)
    return n


__all__ = ["KMA_MESH", "P12_KMA_COUNT", "atlas_rows", "register_p12_kma"]
