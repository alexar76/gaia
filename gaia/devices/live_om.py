"""Open-Meteo relays — the three devices governed by the licence gate.

Grouped together because they share one thing no other relay does: their upstream
has TWO licences. The DATA is CC BY 4.0 and resellable; the HOSTED FREE endpoint is
non-commercial. Everything that follows from that — origin resolution, the
fail-closed commercial gate, the cross-host bearer, and the provenance string that
names an operator-run instance — lives in ``_policy.py`` and is applied here.

Keeping them in one module means the licence question has one place to be answered,
instead of being re-derived beside three unrelated device classes.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from gaia.clock import SimClock
from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices._policy import (
    _om_apikey_suffix,
    _om_auth_headers,
    _om_origin,
    _om_source,
)


# ── Open-Meteo weather (global twin to NWS) ────────────────────────────────────


class OpenMeteoWeather(LiveDevice):
    """Open-Meteo current weather — no API key, CC BY 4.0 / attribution."""

    model = "GAIA-WS1 (Open-Meteo relay)"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
    }
    source = "https://open-meteo.com (Open-Meteo Forecast API; CC BY 4.0 — attribution required)"

    def __init__(self, device_id: str, clock: SimClock, *,
                 latitude: float = 52.52, longitude: float = 13.41, **kw):
        super().__init__(device_id, clock, **kw)
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            raise ValueError("Open-Meteo lat/lon out of range")
        self.latitude = latitude
        self.longitude = longitude
        origin = _om_origin("weather")
        self.source = _om_source(OpenMeteoWeather.source, origin)
        self.headers = {**self.headers, **_om_auth_headers(origin)}
        self.url = (
            f"{origin}/v1/forecast"
            f"?latitude={latitude:.5f}&longitude={longitude:.5f}"
            "&current=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m"
            "&wind_speed_unit=ms"
            f"{_om_apikey_suffix()}"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        cur = (payload or {}).get("current") or {}
        return {
            "temperature_c": _num(cur.get("temperature_2m")),
            "humidity_pct": _num(cur.get("relative_humidity_2m")),
            "pressure_hpa": _num(cur.get("surface_pressure")),
            "wind_mps": _num(cur.get("wind_speed_10m")),
        }


# ── Open-Meteo air quality ────────────────────────────────────────────────────


class OpenMeteoAirQuality(LiveDevice):
    """Open-Meteo air-quality — no API key; PM + optional CO₂."""

    model = "GAIA-AQ1 (Open-Meteo AQ relay)"
    fields = {
        "pm2_5_ugm3": "ug/m3",
        "pm10_ugm3": "ug/m3",
        "co2_ppm": "ppm",
        "us_aqi": "US AQI",
        "european_aqi": "EAQI",
    }
    source = "https://open-meteo.com (Open-Meteo Air Quality API; CC BY 4.0 — attribution required)"

    def __init__(self, device_id: str, clock: SimClock, *,
                 latitude: float = 52.52, longitude: float = 13.41, **kw):
        super().__init__(device_id, clock, **kw)
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            raise ValueError("Open-Meteo AQ lat/lon out of range")
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self._default_coordinate = (self.latitude, self.longitude)
        self.query_gate = threading.Lock()
        self._origin = _om_origin("air_quality")
        self.source = _om_source(OpenMeteoAirQuality.source, self._origin)
        self.headers = {**self.headers, **_om_auth_headers(self._origin)}
        self._coordinate_changed()

    def _coordinate_changed(self) -> None:
        self.url = (
            f"{self._origin}/v1/air-quality"
            f"?latitude={self.latitude:.5f}&longitude={self.longitude:.5f}"
            "&current=pm2_5,pm10,carbon_dioxide,us_aqi,european_aqi"
            f"{_om_apikey_suffix()}"
        )

    def set_coordinate(self, latitude: float, longitude: float) -> None:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Open-Meteo AQ lat/lon out of range")
        self.latitude, self.longitude = float(latitude), float(longitude)
        self._coordinate_changed()

    def clear_coordinate(self) -> None:
        self.latitude, self.longitude = self._default_coordinate
        self._coordinate_changed()

    def map(self, payload: Any) -> dict[str, float | None]:
        cur = (payload or {}).get("current") or {}
        return {
            "pm2_5_ugm3": _num(cur.get("pm2_5")),
            "pm10_ugm3": _num(cur.get("pm10")),
            "co2_ppm": _num(cur.get("carbon_dioxide")),
            "us_aqi": _num(cur.get("us_aqi")),
            "european_aqi": _num(cur.get("european_aqi")),
        }


# ── Open-Meteo Marine ──────────────────────────────────────────────────────────


class OpenMeteoMarine(LiveDevice):
    """Open-Meteo Marine — wave height + SST at operator lat/lon (CC BY 4.0)."""

    model = "GAIA-MARINE (Open-Meteo)"
    fields = {
        "wave_height_m": "m",
        "sst_c": "cel",
    }
    source = (
        "https://open-meteo.com "
        "(Open-Meteo Marine API; CC BY 4.0 — attribution required)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        latitude: float = 40.70,
        longitude: float = -74.01,
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            raise ValueError("Open-Meteo Marine lat/lon out of range")
        self.latitude = latitude
        self.longitude = longitude
        origin = _om_origin("marine")
        self.source = _om_source(OpenMeteoMarine.source, origin)
        self.headers = {**self.headers, **_om_auth_headers(origin)}
        self.url = (
            f"{origin}/v1/marine"
            f"?latitude={latitude:.5f}&longitude={longitude:.5f}"
            "&current=wave_height,sea_surface_temperature"
            f"{_om_apikey_suffix()}"
        )
        # A self-hosted instance serves only the wave model it syncs, and its default
        # blend never picks that model: wave_height came back null everywhere while SST
        # (from the blend) answered. So on an operator-run origin the wave is asked of the
        # synced model by name, in a second request; a coast that model's 0.25° grid does
        # not reach keeps its SST and simply has no wave field.
        wave_model = os.environ.get("GAIA_OM_MARINE_WAVE_MODEL", "ncep_gfswave025").strip()
        hosted = origin.rstrip("/").lower().endswith("open-meteo.com")
        self._wave_url = (
            f"{origin}/v1/marine?latitude={latitude:.5f}&longitude={longitude:.5f}"
            f"&current=wave_height&models={wave_model}{_om_apikey_suffix()}"
            if wave_model and not hosted else ""
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        cur = (payload or {}).get("current") or {}
        return {
            "wave_height_m": _num(cur.get("wave_height")),
            "sst_c": _num(cur.get("sea_surface_temperature")),
        }

    def sample(self) -> dict[str, float]:
        values = {k: v for k, v in self.map(self._fetch(self.url)).items() if v is not None}
        if self._wave_url and "wave_height_m" not in values:
            try:
                wave = _num(((self._fetch(self._wave_url) or {}).get("current") or {}).get("wave_height"))
            except DeviceOffline:
                wave = None
            if wave is not None:
                values["wave_height_m"] = wave
        return values


__all__ = ["OpenMeteoWeather", "OpenMeteoAirQuality", "OpenMeteoMarine"]
