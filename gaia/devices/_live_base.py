"""Shared machinery for live relay devices.

Split out of ``live.py``, which had grown to hold the base class, twelve device
classes and the fleet factory in one file. Nothing here knows about a specific
upstream: this is the contract every relay implements plus the unit vocabulary and
the two coercion helpers.

WHAT THE KEY ATTESTS is documented on :class:`LiveDevice` and is the reason this
module exists separately — a relay's signature is a chain-of-custody claim over
"this is the payload host X served me at ts", never "this gateway measured it".

Access policy (SSRF allowlist, Open-Meteo origin and licence gate) lives in
``_policy.py``; keeping it out of here is what lets a device be read without also
reading the licence rules.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline, VirtualDevice
from gaia.devices._policy import _assert_url_allowed

log = logging.getLogger("gaia.live")

_FIELD_UNITS: dict[str, str] = {
    "temperature_c": "cel",
    "humidity_pct": "percent",
    "pressure_hpa": "hPa",
    "wind_mps": "m/s",
    "pm2_5_ugm3": "ug/m3",
    "pm10_ugm3": "ug/m3",
    "co2_ppm": "ppm",
    "voc_index": "index",
    "carbon_intensity_gco2_kwh": "gCO2/kWh",
    "magnitude": "Mw",
    "depth_km": "km",
    "latitude": "deg",
    "longitude": "deg",
    "water_level_m": "m",
    "discharge_m3s": "m3/s",
    "gage_height_m": "m",
    "wave_height_m": "m",
    "sst_c": "cel",
    # Free-to-commercialize open relays + own edge feeders
    "brightness_k": "K",
    "confidence": "pct",
    "cpm": "cpm",
    "severity_score": "score",
    "radius_km": "km",
    "altitude_m": "m",
    "speed_mps": "m/s",
    "sog_knots": "kn",
    "cog_deg": "deg",
    "intensity_kn": "kn",
    # P0 event / in-situ extras (EONET, SWPC, GLM, CAP, Argo, USGS geomag)
    "kp_index": "Kp",
    "aurora_pct": "pct",
    "energy_j": "J",
    "energy_fj": "fJ",
    "field_nt": "nT",
    "salinity_psu": "PSU",
    "pressure_dbar": "dbar",
    "demand_mw": "MW",
    "air_quality_index": "DAQI",
    "water_temperature_c": "cel",
    "ph": "pH",
    "dissolved_oxygen_mg_l": "mg/L",
    "specific_conductance_us_cm": "uS/cm",
    "water_column_height_m": "m",
    "measurement_type": "code",
    "precipitation_mm_h": "mm/h",
    "radar_latency_s": "s",
    "reflectivity_calibration_db": "dB",
    "transmitter_power_w": "W",
    "aerosol_optical_depth": "AOD",
    "dust_ugm3": "ug/m3",
    "alder_pollen_grains_m3": "grains/m3",
    "birch_pollen_grains_m3": "grains/m3",
    "grass_pollen_grains_m3": "grains/m3",
    "dose_equivalent_nsv_h": "nSv/h",
    "gamma_count_total_cpm": "cpm",
    "soil_water_index_pct": "percent",
    "solar_irradiation_kwh_m2_day": "kWh/m2/day",
    "clear_sky_irradiation_kwh_m2_day": "kWh/m2/day",
    "solar_observation_yyyymmdd": "date",
    "snow_depth_cm": "cm",
    "snow_water_equivalent_cm": "cm",
    "snow_observation_yyyymmdd": "date",
    "sea_ice_concentration_pct": "percent",
    "sea_ice_observation_yyyymmdd": "date",
    "land_surface_temperature_c": "cel",
    "land_surface_temperature_uncertainty_k": "K",
}

_UA = "GAIA-oracle/0.1 (+https://iot.modelmarket.dev; contact@modelmarket.dev)"
_STA_DEFAULT_BASE = "https://airquality-frost.k8s.ilt-dmz.iosb.fraunhofer.de/v1.1"

_SAFE_STATION = re.compile(r"^[A-Za-z0-9]{3,12}$")
_SAFE_BOX = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_SAFE_DS = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SAFE_NOAA = re.compile(r"^[0-9]{5,8}$")
_SAFE_OPENAQ = re.compile(r"^[0-9]{1,12}$")
_SAFE_USGS_SITE = re.compile(r"^[0-9]{8,15}$")
_SAFE_NDBC = re.compile(r"^[0-9]{4,6}[a-z]?$", re.I)

_FT3S_TO_M3S = 0.028316846592
_FT_TO_M = 0.3048


def _num(value: Any) -> float | None:
    """Coerce an upstream scalar to float, or None if absent/non-numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return float(value)
    # numpy scalars from NetCDF / HDF5 (np.float32 is not a Python float)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = item()
        except (ValueError, TypeError):
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None
    return None


def _lat_lon(lat_s: str, lon_s: str, *, default_lat: float, default_lon: float) -> tuple[float, float]:
    try:
        lat = float(lat_s) if lat_s else default_lat
        lon = float(lon_s) if lon_s else default_lon
    except ValueError as exc:
        raise ValueError("latitude/longitude must be numeric") from exc
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("latitude/longitude out of range")
    return lat, lon


class LiveDevice(VirtualDevice):
    """A VirtualDevice backed by a real HTTP API instead of a simulator."""

    model = "GAIA-LIVE"
    url: str = ""
    headers: dict[str, str] = {}
    source: str = ""
    timeout: float = 12.0

    def _fetch(self, url: str) -> Any:
        """GET ``url`` and return parsed JSON, or raise DeviceOffline."""
        url = _assert_url_allowed(url)
        headers = {"User-Agent": _UA, **(self.headers or {})}
        try:
            resp = httpx.get(url, headers=headers, timeout=self.timeout, follow_redirects=False)
            # Refuse redirects explicitly (SSRF via Location). Only a clean 200 JSON
            # body is accepted — raise_for_status alone would still parse a 302 body.
            if resp.status_code != 200:
                raise DeviceOffline(
                    f"{self.device_id}: upstream HTTP {resp.status_code}"
                )
            return resp.json()
        except DeviceOffline:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DeviceOffline(
                f"{self.device_id}: upstream unreachable ({type(exc).__name__})"
            ) from exc

    def _fetch_text(self, url: str, *, max_chars: int = 2_000_000) -> str:
        """GET ``url`` and return the body as text (METAR, S3 XML, NDBC)."""
        url = _assert_url_allowed(url)
        headers = {"User-Agent": _UA, **(self.headers or {})}
        try:
            resp = httpx.get(
                url, headers=headers, timeout=self.timeout, follow_redirects=False
            )
            if resp.status_code != 200:
                raise DeviceOffline(
                    f"{self.device_id}: upstream HTTP {resp.status_code}"
                )
            text = resp.text
            if len(text) > max_chars:
                raise DeviceOffline(f"{self.device_id}: upstream body too large")
            return text
        except DeviceOffline:
            raise
        except httpx.HTTPError as exc:
            raise DeviceOffline(
                f"{self.device_id}: upstream unreachable ({type(exc).__name__})"
            ) from exc

    def _fetch_bytes(self, url: str, *, max_bytes: int = 8 * 1024 * 1024) -> bytes:
        """GET ``url`` and return raw bytes (NetCDF / HDF5)."""
        url = _assert_url_allowed(url)
        headers = {"User-Agent": _UA, **(self.headers or {})}
        try:
            resp = httpx.get(
                url, headers=headers, timeout=self.timeout, follow_redirects=False
            )
            if resp.status_code != 200:
                raise DeviceOffline(
                    f"{self.device_id}: upstream HTTP {resp.status_code}"
                )
            data = resp.content
            if len(data) > max_bytes:
                raise DeviceOffline(f"{self.device_id}: upstream body too large")
            return data
        except DeviceOffline:
            raise
        except httpx.HTTPError as exc:
            raise DeviceOffline(
                f"{self.device_id}: upstream unreachable ({type(exc).__name__})"
            ) from exc

    def map(self, payload: Any) -> dict[str, float | None]:  # pragma: no cover - abstract
        raise NotImplementedError

    def sample(self) -> dict[str, float]:
        payload = self._fetch(self.url)
        return {k: v for k, v in self.map(payload).items() if v is not None}


__all__ = [
    "LiveDevice", "_FIELD_UNITS", "_num", "_lat_lon",
    # Identifier guards: every relay sanitises its path/query ids with these, so
    # they belong with the base rather than beside any one device.
    "_SAFE_STATION", "_SAFE_BOX", "_SAFE_DS", "_SAFE_NOAA", "_SAFE_OPENAQ",
    "_SAFE_USGS_SITE", "_SAFE_NDBC", "_UA", "_STA_DEFAULT_BASE",
    "_FT3S_TO_M3S", "_FT_TO_M",
]
