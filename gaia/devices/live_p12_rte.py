"""P12 RTE éCO2mix France national production-only CO₂ — Etalab OL 2.0, no key."""

from __future__ import annotations

import logging
from typing import Any

from gaia.clock import SimClock
from gaia.devices.live_p12 import RTE_MESH, RteGrid, _env

log = logging.getLogger("gaia.devices.live_p12_rte")

P12_RTE_COUNT = len(RTE_MESH)


def atlas_rows() -> list[tuple[str, float, float, str, str, str, str]]:
    rows: list[tuple[str, float, float, str, str, str, str]] = []
    for device_id, lat, lon, place in RTE_MESH:
        rows.append((
            device_id, lat, lon, place, "grid", "gaia.grid.read@v1",
            "RTE éCO2mix · Etalab OL 2.0; production-only CO₂",
        ))
    return rows


def register_p12_rte(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    if _env("GAIA_RTE_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        return 0
    n = 0
    for device_id, lat, lon, _place in RTE_MESH:
        try:
            fleet.add(RteGrid(
                device_id, clock, latitude=lat, longitude=lon,
                site=f"live-grid-{device_id}", key_dir=key_dir,
            ))
            n += 1
        except ValueError as exc:
            log.warning("RTE %s skipped: %s", device_id, exc)
    return n


__all__ = ["RTE_MESH", "P12_RTE_COUNT", "atlas_rows", "register_p12_rte"]
