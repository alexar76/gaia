"""P12 Ireland OPW waterlevel.ie — CC BY 4.0, no key."""

from __future__ import annotations

import logging
from typing import Any

from gaia.clock import SimClock
from gaia.devices.live_p12 import OPW_MESH, OpwRiver, _env

log = logging.getLogger("gaia.devices.live_p12_opw")

P12_OPW_COUNT = len(OPW_MESH)


def atlas_rows() -> list[tuple[str, float, float, str, str, str, str]]:
    rows: list[tuple[str, float, float, str, str, str, str]] = []
    for device_id, station, lat, lon, place in OPW_MESH:
        rows.append((
            device_id, lat, lon, place, "river", "gaia.river.read@v1",
            "OPW waterlevel.ie · CC BY 4.0",
        ))
    return rows


def register_p12_opw(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    if _env("GAIA_OPW_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        return 0
    n = 0
    for device_id, station, lat, lon, _place in OPW_MESH:
        try:
            fleet.add(OpwRiver(
                device_id, clock, station=station, latitude=lat, longitude=lon,
                site=f"live-river-{device_id}", key_dir=key_dir,
            ))
            n += 1
        except ValueError as exc:
            log.warning("OPW %s skipped: %s", device_id, exc)
    return n


__all__ = ["OPW_MESH", "P12_OPW_COUNT", "atlas_rows", "register_p12_opw"]
