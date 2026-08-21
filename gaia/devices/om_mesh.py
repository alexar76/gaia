"""Global Open-Meteo mesh — operator-anchored city relays (no buyer lat/lon).

Canonical city list: ``gaia/config/om_mesh_cities.yaml``
(mirrored to ``atlas/config/om_mesh_cities.yaml`` — keep identical via
``scripts/sync_om_mesh_catalog.sh``).

Each city becomes two device_ids when enabled:

    om-wx-{slug}  → OpenMeteoWeather
    om-aq-{slug}  → OpenMeteoAirQuality

Berlin stays on legacy ids ``om-wx-01`` / ``om-aq-01`` (env ``GAIA_OM_LAT/LON``).

Developer onboarding: ``docs/add-gaia-atlas-sensor.md``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict

import yaml


class OmCity(TypedDict, total=False):
    slug: str
    place: str
    lat: float
    lon: float
    aliases: list[str]


def _config_path() -> Path:
    # gaia/gaia/devices/om_mesh.py → gaia/config/om_mesh_cities.yaml
    return Path(__file__).resolve().parents[2] / "config" / "om_mesh_cities.yaml"


@lru_cache(maxsize=1)
def _load_cities() -> tuple[OmCity, ...]:
    path = _config_path()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cities = raw.get("cities") or []
    out: list[OmCity] = []
    for row in cities:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        out.append(
            {
                "slug": slug,
                "place": str(row.get("place") or slug),
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "aliases": [str(a) for a in (row.get("aliases") or []) if str(a).strip()],
            }
        )
    if not out:
        raise RuntimeError(f"om_mesh_cities.yaml empty or missing cities: {path}")
    return tuple(out)


# Public constant — same shape as before (lazy via property-like load).
OM_MESH_CITIES: tuple[OmCity, ...] = _load_cities()


def mesh_device_ids(*, weather: bool = True, aq: bool = True) -> list[str]:
    ids: list[str] = []
    for city in OM_MESH_CITIES:
        slug = city["slug"]
        if weather:
            ids.append(f"om-wx-{slug}")
        if aq:
            ids.append(f"om-aq-{slug}")
    return ids


def atlas_catalog_entries() -> dict[str, dict[str, Any]]:
    """ATLAS pin catalog slice for the Open-Meteo mesh (mirror of GAIA devices)."""
    out: dict[str, dict[str, Any]] = {}
    for city in OM_MESH_CITIES:
        slug = city["slug"]
        place = city["place"]
        lat = float(city["lat"])
        lon = float(city["lon"])
        out[f"om-wx-{slug}"] = {
            "layer": "weather",
            "label": f"Open-Meteo Weather · {place}",
            "capability": "gaia.weather.read@v1",
            "lat": lat,
            "lon": lon,
            "place": place,
            "kind": "point",
            "mode": "live",
        }
        out[f"om-aq-{slug}"] = {
            "layer": "air",
            "label": f"Open-Meteo Air · {place}",
            "capability": "gaia.air.read@v1",
            "lat": lat,
            "lon": lon,
            "place": place,
            "kind": "point",
            "mode": "live",
        }
    return out
