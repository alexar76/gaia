"""GeoNet Tilde coastal water height from a fixed pressure-sensor channel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_water_ext import _utc_time
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

GEONET_COASTAL_GAUGES = {
    "AUCT": (-36.8314, 174.7865, "Auckland"),
    "WLGT": (-41.2845, 174.7799, "Wellington"),
    "TAUT": (-37.6411, 176.1812, "Tauranga"),
    "GIST": (-38.6754, 178.0229, "Gisborne"),
    "JBTT": (-43.9737, 168.6157, "Jackson Bay"),
}
_MAX_AGE = timedelta(minutes=5)
_SENSOR_CODE = "40"


class GeonetCoastalGauge(LiveDevice):
    """Measured water height above the selected local pressure sensor."""

    model = "GAIA-TIDE (GeoNet Tilde coastal gauge)"
    policy_id = "geonet_coastal"
    headers = {"Accept": "application/json"}
    fields = {
        "coastal_water_height_m": "m above local pressure sensor",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
        "coastal_sensor_code": "code",
    }

    def __init__(self, device_id: str, clock: SimClock, *, gauge: str, **kwargs: Any) -> None:
        if gauge not in GEONET_COASTAL_GAUGES:
            raise ValueError("unsupported GeoNet coastal pilot gauge")
        super().__init__(device_id, clock, **kwargs)
        self.gauge = gauge
        self.latitude, self.longitude, _ = GEONET_COASTAL_GAUGES[gauge]
        self.url = (
            f"https://tilde.geonet.org.nz/v4/data/coastal/{gauge}"
            f"/water-height/{_SENSOR_CODE}/15s/nil/latest/6h"
        )
        self.source = (
            f"{self.url} (Earth Sciences New Zealand, GeoNet and programme sponsors "
            "NHC, LINZ, NEMA and MBIE; CC BY 3.0 NZ; doi:10.21420/EJ6W-RC96. "
            "Height in metres above local pressure sensor 40.)"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float]:
        rows = payload if isinstance(payload, list) else []
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise DeviceOffline(f"{self.device_id}: GeoNet coastal series unavailable or ambiguous")
        row = rows[0]
        series = row.get("series")
        expected = {
            "domain": "coastal", "station": self.gauge, "name": "water-height",
            "sensorCode": _SENSOR_CODE, "method": "15s", "aspect": "nil",
        }
        lat = _num(row.get("latitude"))
        lon = _num(row.get("longitude"))
        samples = row.get("data")
        if (
            not isinstance(series, dict)
            or any(series.get(key) != value for key, value in expected.items())
            or row.get("valueUnit") != "m"
            or row.get("datum") != "WGS84"
            or lat is None or not abs(lat - self.latitude) < 0.001
            or lon is None or not abs(lon - self.longitude) < 0.001
            or not isinstance(samples, list)
        ):
            raise DeviceOffline(f"{self.device_id}: GeoNet coastal series metadata do not match the pilot")

        now = datetime.now(UTC)
        latest: tuple[datetime, float] | None = None
        conflicting = False
        for sample in samples:
            if not isinstance(sample, dict) or sample.get("qc") != "":
                continue
            observed = _utc_time(sample.get("ts"))
            height = _num(sample.get("val"))
            if (
                observed is None or not timedelta(0) <= now - observed < _MAX_AGE
                or height is None or not 0 <= height <= 30
            ):
                continue
            if latest is None or observed > latest[0]:
                latest = (observed, height)
                conflicting = False
            elif observed == latest[0] and height != latest[1]:
                conflicting = True
        if latest is None:
            raise DeviceOffline(f"{self.device_id}: no fresh unflagged GeoNet observations")
        if conflicting:
            raise DeviceOffline(f"{self.device_id}: conflicting GeoNet observations")
        observed, height = latest
        return {
            "coastal_water_height_m": height,
            "observation_unix": observed.timestamp(),
            "latitude": lat,
            "longitude": lon,
            "coastal_sensor_code": float(_SENSOR_CODE),
        }
