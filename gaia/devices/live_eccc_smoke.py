"""ECCC RAQDPS near-surface PM2.5 forecast within wildfire smoke plumes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock


ECCC_SMOKE_PILOTS = {
    "eccc-smoke-seattle": (47.610, -122.340, "Seattle"),
    "eccc-smoke-toronto": (43.650, -79.380, "Toronto"),
    "eccc-smoke-calgary": (51.050, -114.070, "Calgary"),
}
_LAYER = "RAQDPS.Sfc_PM2.5-WildfireSmokePlume"
_MAX_RUN_AGE = timedelta(hours=18)
_MAX_FORECAST_LEAD = timedelta(hours=72)


def _utc_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


class EcccSmokeForecast(LiveDevice):
    """Sign the next hour of the latest available model run at a pilot grid cell."""

    model = "GAIA-SMOKE-FORECAST (ECCC RAQDPS)"
    policy_id = "eccc_raqdps_smoke"
    url = "https://geo.weather.gc.ca/geomet"
    fields = {
        "forecast_smoke_pm2_5_ugm3": "ug/m3",
        "forecast_run_unix": "s",
        "forecast_valid_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, **kw: Any):
        if device_id not in ECCC_SMOKE_PILOTS:
            raise ValueError("unsupported ECCC smoke forecast pilot")
        require_approved_source(self.policy_id).require_endpoint(self.url)
        super().__init__(device_id, clock, **kw)
        self.latitude, self.longitude, _ = ECCC_SMOKE_PILOTS[device_id]
        self.source = (
            f"{self.url}?service=WMS&version=1.3.0&request=GetCapabilities&layer={_LAYER} "
            "(Environment and Climate Change Canada, Meteorological Service of Canada, RAQDPS. "
            "Forecast of total near-surface PM2.5 within a modelled wildfire smoke plume. "
            "10 km model cell, converted from kg/m³ to µg/m³.)"
        )

    def sample(self) -> dict[str, float]:
        now = datetime.now(UTC)
        valid = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        lat, lon = self.latitude, self.longitude
        # WMS 1.3 EPSG:4326 uses latitude before longitude in BBOX. The center
        # pixel selects the pilot, but GeoMet returns its actual model cell.
        query = urlencode({
            "service": "WMS", "version": "1.3.0", "request": "GetFeatureInfo",
            "layers": _LAYER, "query_layers": _LAYER, "crs": "EPSG:4326",
            "bbox": f"{lat - 0.01:.4f},{lon - 0.01:.4f},{lat + 0.01:.4f},{lon + 0.01:.4f}",
            "width": 3, "height": 3, "i": 1, "j": 1, "info_format": "application/json",
            "time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        return self._forecast(self._fetch(f"{self.url}?{query}"), valid, datetime.now(UTC))

    def _forecast(self, payload: Any, wanted: datetime, now: datetime) -> dict[str, float]:
        if (not isinstance(payload, dict) or payload.get("type") != "FeatureCollection"
                or payload.get("layer") != _LAYER):
            raise DeviceOffline(f"{self.device_id}: unexpected ECCC forecast layer")
        features = payload.get("features")
        if not isinstance(features, list) or len(features) != 1 or not isinstance(features[0], dict):
            raise DeviceOffline(f"{self.device_id}: ECCC forecast cell unavailable")
        feature = features[0]
        geometry = feature.get("geometry")
        coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if (feature.get("type") != "Feature" or not isinstance(coordinates, list)
                or len(coordinates) != 2 or geometry.get("type") != "Point"):
            raise DeviceOffline(f"{self.device_id}: invalid ECCC model cell geometry")
        lon, lat = map(_num, coordinates)
        # For these three pilots a 10 km cell cannot be more than 0.15 degrees
        # away on either axis. Refuse unrelated, out-of-domain or swapped cells.
        if (lat is None or lon is None or not abs(lat - self.latitude) <= 0.15
                or not abs(lon - self.longitude) <= 0.15):
            raise DeviceOffline(f"{self.device_id}: ECCC model cell is outside the pilot area")

        properties = feature.get("properties")
        title = properties.get("title_en") if isinstance(properties, dict) else None
        if not isinstance(title, str) or not title.endswith("PM2.5 [kg/m³]"):
            raise DeviceOffline(f"{self.device_id}: ECCC PM2.5 unit unavailable")
        concentration = _num(properties.get("value"))
        run = _utc_time(properties.get("dim_reference_time"))
        valid = _utc_time(properties.get("time"))
        if (run is None or run.hour not in (0, 12) or run != run.replace(minute=0, second=0, microsecond=0)
                or not timedelta(0) <= now - run <= _MAX_RUN_AGE
                or valid != wanted or not now < wanted <= run + _MAX_FORECAST_LEAD):
            raise DeviceOffline(f"{self.device_id}: stale or mismatched ECCC forecast times")
        ugm3 = concentration * 1_000_000_000 if concentration is not None else None
        if ugm3 is None or not isfinite(ugm3) or ugm3 < 0:
            raise DeviceOffline(f"{self.device_id}: invalid ECCC PM2.5 concentration")
        return {
            "forecast_smoke_pm2_5_ugm3": ugm3,
            "forecast_run_unix": run.timestamp(),
            "forecast_valid_unix": wanted.timestamp(),
            "latitude": lat,
            "longitude": lon,
        }
