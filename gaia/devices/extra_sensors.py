"""Operator-added sensors from ``gaia/config/extra_sensors.yaml``.

Add via one command::

    python3 scripts/add_gaia_atlas_sensor.py ...

Only **known kinds** (existing LiveDevice / SIM classes) are supported.
Unknown upstream APIs still need a code change — see docs/add-gaia-atlas-sensor.md.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from gaia.clock import SimClock
    from gaia.fleet import Fleet

log = logging.getLogger("gaia.extra_sensors")

# kind → map layer / capability / live|sim + which factory builds the device
KIND_META: dict[str, dict[str, str]] = {
    "open-meteo-weather": {
        "layer": "weather",
        "capability": "gaia.weather.read@v1",
        "mode": "live",
    },
    "open-meteo-air": {
        "layer": "air",
        "capability": "gaia.air.read@v1",
        "mode": "live",
    },
    "nws": {
        "layer": "weather",
        "capability": "gaia.weather.read@v1",
        "mode": "live",
    },
    "opensensemap": {
        "layer": "air",
        "capability": "gaia.air.read@v1",
        "mode": "live",
    },
    "noaa-tide": {
        "layer": "tide",
        "capability": "gaia.tide.read@v1",
        "mode": "live",
    },
    "openaq": {
        "layer": "air",
        "capability": "gaia.air.read@v1",
        "mode": "live",
    },
    "uk-grid": {
        "layer": "grid",
        "capability": "gaia.grid.read@v1",
        "mode": "live",
    },
    "usgs-quake": {
        "layer": "quake",
        "capability": "gaia.quake.read@v1",
        "mode": "live",
    },
    "usgs-river": {
        "layer": "river",
        "capability": "gaia.river.read@v1",
        "mode": "live",
    },
    "ndbc-buoy": {
        "layer": "marine",
        "capability": "gaia.marine.read@v1",
        "mode": "live",
    },
    "open-meteo-marine": {
        "layer": "marine",
        "capability": "gaia.marine.read@v1",
        "mode": "live",
    },
    "firms-fire": {
        "layer": "fire",
        "capability": "gaia.fire.read@v1",
        "mode": "live",
    },
    "safecast": {
        "layer": "radiation",
        "capability": "gaia.radiation.read@v1",
        "mode": "live",
    },
    "cybernews-jamming": {
        "layer": "jamming",
        "capability": "gaia.jamming.read@v1",
        "mode": "live",
    },
    "sim-weather": {
        "layer": "weather",
        "capability": "gaia.weather.read@v1",
        "mode": "sim",
    },
    "sim-air": {
        "layer": "air",
        "capability": "gaia.air.read@v1",
        "mode": "sim",
    },
    "sim-energy": {
        "layer": "energy",
        "capability": "gaia.energy.read@v1",
        "mode": "sim",
    },
}


def _config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "extra_sensors.yaml"


@lru_cache(maxsize=1)
def load_sensors() -> tuple[dict[str, Any], ...]:
    path = _config_path()
    if not path.is_file():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = raw.get("sensors") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("enabled", True) is False:
            continue
        device_id = str(row.get("device_id") or "").strip()
        kind = str(row.get("kind") or "").strip()
        if not device_id or kind not in KIND_META:
            log.warning("extra_sensors: skip invalid row %r", row)
            continue
        out.append(row)
    return tuple(out)


def atlas_catalog_entries() -> dict[str, dict[str, Any]]:
    """ATLAS pin rows for every enabled extra sensor."""
    out: dict[str, dict[str, Any]] = {}
    for row in load_sensors():
        kind = str(row["kind"])
        meta = KIND_META[kind]
        device_id = str(row["device_id"])
        out[device_id] = {
            "layer": str(row.get("layer") or meta["layer"]),
            "label": str(row.get("label") or device_id),
            "capability": str(row.get("capability") or meta["capability"]),
            "lat": float(row.get("lat") or 0.0),
            "lon": float(row.get("lon") or 0.0),
            "place": str(row.get("place") or ""),
            "kind": str(row.get("pin_kind") or "point"),
            "mode": str(row.get("mode") or meta["mode"]),
        }
    return out


def place_targets() -> dict[str, dict[str, Any]]:
    """Analyst flyTo targets derived from extra sensors with aliases."""
    out: dict[str, dict[str, Any]] = {}
    for row in load_sensors():
        aliases = [str(a) for a in (row.get("aliases") or []) if str(a).strip()]
        place = str(row.get("place") or "").strip()
        device_id = str(row["device_id"])
        key = str(row.get("place_id") or device_id)
        if not aliases and place:
            aliases = [place.lower(), device_id]
        if not aliases:
            continue
        if key in out:
            # Merge station ids when several sensors share a place_id.
            prev = out[key]
            sids = list(prev.get("station_ids") or ())
            if device_id not in sids:
                sids.append(device_id)
            prev["station_ids"] = tuple(sids)
            continue
        out[key] = {
            "aliases": tuple(aliases),
            "station_ids": (device_id,),
            "lon": float(row.get("lon") or 0.0),
            "lat": float(row.get("lat") or 0.0),
            "zoom": float(row.get("zoom") or 9.5),
            "label": place or device_id,
        }
    return out


def register_live_extras(fleet: Fleet, clock: SimClock, key_dir: str = "data/devices") -> int:
    """Instantiate LIVE kinds into ``fleet``. Returns count added."""
    from gaia.devices.live import (
        NDBCBuoy,
        NOAATideStation,
        NWSStation,
        OpenAQLocation,
        OpenMeteoAirQuality,
        OpenMeteoMarine,
        OpenMeteoWeather,
        OpenSenseMapBox,
        UKCarbonIntensity,
        USGSEarthquake,
        USGSRiverGauge,
    )

    n = 0
    for row in load_sensors():
        kind = str(row["kind"])
        if KIND_META[kind]["mode"] != "live":
            continue
        device_id = str(row["device_id"])
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        site = str(row.get("site") or f"extra-{device_id}")
        try:
            if kind == "open-meteo-weather":
                fleet.add(
                    OpenMeteoWeather(
                        device_id,
                        clock,
                        latitude=float(row["lat"]),
                        longitude=float(row["lon"]),
                        site=site,
                        key_dir=key_dir,
                    )
                )
            elif kind == "open-meteo-air":
                fleet.add(
                    OpenMeteoAirQuality(
                        device_id,
                        clock,
                        latitude=float(row["lat"]),
                        longitude=float(row["lon"]),
                        site=site,
                        key_dir=key_dir,
                    )
                )
            elif kind == "open-meteo-marine":
                fleet.add(
                    OpenMeteoMarine(
                        device_id,
                        clock,
                        latitude=float(row["lat"]),
                        longitude=float(row["lon"]),
                        site=site,
                        key_dir=key_dir,
                    )
                )
            elif kind == "nws":
                station = str(params.get("station") or "KNYC")
                fleet.add(
                    NWSStation(
                        device_id, clock, station=station, site=site, key_dir=key_dir
                    )
                )
            elif kind == "opensensemap":
                box_id = str(params.get("box_id") or "")
                if not box_id:
                    log.warning("extra_sensors: %s opensensemap needs params.box_id", device_id)
                    continue
                fleet.add(
                    OpenSenseMapBox(
                        device_id, clock, box_id=box_id, site=site, key_dir=key_dir
                    )
                )
            elif kind == "noaa-tide":
                station = str(params.get("station") or "8518750")
                fleet.add(
                    NOAATideStation(
                        device_id, clock, station=station, site=site, key_dir=key_dir
                    )
                )
            elif kind == "usgs-river":
                usgs_site = str(params.get("usgs_site") or params.get("site") or "01646500")
                fleet.add(
                    USGSRiverGauge(
                        device_id, clock, usgs_site=usgs_site, site=site, key_dir=key_dir
                    )
                )
            elif kind == "ndbc-buoy":
                station = str(params.get("station") or "44025")
                fleet.add(
                    NDBCBuoy(
                        device_id, clock, station=station, site=site, key_dir=key_dir
                    )
                )
            elif kind == "openaq":
                import os

                key = (os.environ.get("GAIA_OPENAQ_API_KEY") or "").strip()
                if not key:
                    log.info("extra_sensors: skip %s (set GAIA_OPENAQ_API_KEY)", device_id)
                    continue
                loc = str(params.get("location_id") or "2178")
                fleet.add(
                    OpenAQLocation(
                        device_id,
                        clock,
                        location_id=loc,
                        api_key=key,
                        site=site,
                        key_dir=key_dir,
                    )
                )
            elif kind == "uk-grid":
                fleet.add(
                    UKCarbonIntensity(device_id, clock, site=site, key_dir=key_dir)
                )
            elif kind == "usgs-quake":
                fleet.add(
                    USGSEarthquake(device_id, clock, site=site, key_dir=key_dir)
                )
            elif kind == "firms-fire":
                from gaia.devices.live_open import FirmsFireHotspot

                fleet.add(
                    FirmsFireHotspot(
                        device_id,
                        clock,
                        map_key=str(params.get("map_key") or ""),
                        site=site,
                        key_dir=key_dir,
                    )
                )
            elif kind == "safecast":
                from gaia.devices.live import _env
                from gaia.devices.live_open import SafecastRadiation

                try:
                    max_age = int(_env("GAIA_SAFECAST_MAX_AGE_DAYS", "30"))
                except ValueError:
                    max_age = 30
                fleet.add(
                    SafecastRadiation(
                        device_id,
                        clock,
                        latitude=float(row.get("lat") or 37.42),
                        longitude=float(row.get("lon") or 141.03),
                        max_age_days=max_age,
                        site=site,
                        key_dir=key_dir,
                    )
                )
            elif kind == "cybernews-jamming":
                from gaia.devices.live_open import CyberNewsJamming

                fleet.add(
                    CyberNewsJamming(device_id, clock, site=site, key_dir=key_dir)
                )
            else:
                continue
            n += 1
        except Exception as exc:  # noqa: BLE001 — operator YAML must not crash gateway
            log.warning("extra_sensors: failed to add %s (%s): %s", device_id, kind, exc)
    return n


def register_sim_extras(
    fleet: Fleet,
    clock: SimClock,
    key_dir: str = "data/devices",
    *,
    seed: int = 100,
) -> int:
    """Instantiate SIM kinds into ``fleet``."""
    from gaia.devices.air_quality import AirQualitySim
    from gaia.devices.energy import EnergyMeterSim
    from gaia.devices.weather import SiteWeather, WeatherStationSim

    n = 0
    site = SiteWeather(clock, seed=seed + 50)
    for i, row in enumerate(load_sensors()):
        kind = str(row["kind"])
        if KIND_META[kind]["mode"] != "sim":
            continue
        device_id = str(row["device_id"])
        site_name = str(row.get("site") or "extra-sim")
        try:
            if kind == "sim-weather":
                fleet.add(
                    WeatherStationSim(
                        device_id,
                        clock,
                        site,
                        site=site_name,
                        seed=seed + i,
                        key_dir=key_dir,
                    )
                )
            elif kind == "sim-air":
                fleet.add(
                    AirQualitySim(
                        device_id,
                        clock,
                        site=site_name,
                        seed=seed + i,
                        key_dir=key_dir,
                    )
                )
            elif kind == "sim-energy":
                fleet.add(
                    EnergyMeterSim(
                        device_id,
                        clock,
                        site=site_name,
                        seed=seed + i,
                        key_dir=key_dir,
                    )
                )
            else:
                continue
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("extra_sensors: failed SIM %s: %s", device_id, exc)
    return n


__all__ = [
    "KIND_META",
    "atlas_catalog_entries",
    "load_sensors",
    "place_targets",
    "register_live_extras",
    "register_sim_extras",
]
