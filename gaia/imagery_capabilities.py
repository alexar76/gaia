"""gaia.imagery.read@v1 — the latest cloud-free Sentinel-2 view of a place, attested.

    gaia.imagery.read@v1  $0.005  NDVI / NDWI / NBR / NDMI and five surface
                                  reflectances at one coordinate (or a named place,
                                  through the gazetteer), from the newest scene whose
                                  pixel there is clear; with the scene id and date

The network part (STAC search, header and tile reads, ~3-6 s) runs outside the device
lock so concurrent callers do not queue behind each other; only signing the staged
values is serialised, under the device's query gate, with a site per coordinate so
plausibility history is this point's own.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from gaia.capabilities import GatewayRuntime

DEVICE_ID = "s2-l2a-01"
PRICE_USD = 0.005

IMAGERY_IN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "city": {"type": "string", "maxLength": 160,
                 "description": "a place name instead of coordinates (\"Kraków\", "
                                "\"Paris, Texas\"); resolved through the gazetteer"},
        "max_age_days": {"type": "number", "minimum": 1, "maximum": 120, "default": 30,
                         "description": "look back this far for a cloud-free view"},
    },
    "anyOf": [{"required": ["latitude", "longitude"]}, {"required": ["city"]},
              {"required": ["lat", "lon"]}, {"required": ["place"]},
              {"required": ["location"]}],
}
IMAGERY_OUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reading": {"type": "object", "description": "attested: ndvi, ndwi, nbr, ndmi, "
                    "reflectance_*, scene_class, observation_unix, observation_age_days"},
        "attestation": {"type": "object"},
        "scene": {"type": "object", "description": "Sentinel-2 scene id, time, MGRS tile, credit"},
        "resolved": {"type": "object"},
    },
}


def imagery_read_handler(runtime: "GatewayRuntime"):
    from gaia.capabilities import _place_source, _requested_location
    from gaia.devices.live_imagery import Sentinel2Point

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        device = runtime.fleet.get(DEVICE_ID)
        if not isinstance(device, Sentinel2Point):
            raise ValueError(f"{DEVICE_ID} is not the Sentinel-2 relay")
        where = _requested_location(raw, getattr(runtime, "gazetteer", None))
        if where is None:
            raise ValueError("gaia.imagery.read needs a place: latitude and longitude, or "
                             "city (\"Kraków\")")
        try:
            max_age = float(raw.get("max_age_days", 30))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_age_days must be a number") from exc
        if not (math.isfinite(max_age) and 1 <= max_age <= 120):
            raise ValueError("max_age_days must be from 1 to 120")
        lat, lon = float(where["lat"]), float(where["lon"])
        obs = device.observe(lat, lon, max_age)  # network; no shared state
        with device.query_gate:
            home = device.site
            device.site = f"{home}@{lat:.4f},{lon:.4f}"
            device.stage(obs["values"])
            try:
                out = runtime.fleet.read(DEVICE_ID)
            finally:
                device.unstage()
                device.site = home
        return {**out, "scene": obs["scene"], "resolved": {
            "requested": where["requested"], "matched_place": where.get("place"),
            **_place_source(where), "latitude": lat, "longitude": lon,
        }}

    return handler


__all__ = ["DEVICE_ID", "IMAGERY_IN", "IMAGERY_OUT", "PRICE_USD", "imagery_read_handler"]
