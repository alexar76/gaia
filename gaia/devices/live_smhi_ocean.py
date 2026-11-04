"""SMHI Oceanographic Observations from fixed Swedish coastal sensors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock


@dataclass(frozen=True, slots=True)
class OceanPilot:
    station: str
    depth: float
    latitude: float
    longitude: float
    name: str
    owner: str
    parameters: tuple[str, ...]


SMHI_OCEAN_PILOTS = {
    "smhi-ocean-flinten16-8m": OceanPilot("35136", 8.0, 55.561, 12.8095, "Flinten 16", "Sjöfartsverket", ("4", "15")),
    "smhi-ocean-flinten16-2m": OceanPilot("35136", 2.0, 55.561, 12.8095, "Flinten 16", "Sjöfartsverket", ("3", "2")),
    "smhi-ocean-flinten7-8m": OceanPilot("33047", 8.0, 55.5894, 12.8445, "Flinten 7 Bottensystem", "SMHI", ("4",)),
    "smhi-ocean-kalmar2-0m": OceanPilot("35105", 0.0, 56.6589, 16.3782, "Kalmar 2", "Sjöfartsverket", ("3", "2")),
}
_BASE_URL = "https://opendata-download-ocobs.smhi.se/api/version/1.0/parameter"
_MAX_AGE = timedelta(hours=3).total_seconds()


class _Parameter(NamedTuple):
    field: str
    source_unit: str
    unit: str
    scale: float
    maximum: float


_PARAMETERS = {
    "2": _Parameter("current_direction_deg", "°", "deg", 1.0, 360.0),
    "3": _Parameter("current_speed_mps", "cm/s", "m/s", 0.01, 2000.0),
    "4": _Parameter("salinity_psu", "PSU", "PSU", 1.0, 50.0),
    "15": _Parameter("dissolved_oxygen_ml_l", "ml/l", "ml/L", 1.0, 30.0),
}


class SmhiOcean(LiveDevice):
    """Pair validated observations at the same fixed station, depth and time."""

    model = "GAIA-OCEAN (SMHI Oceanographic Observations)"
    policy_id = "smhi_ocean_observations"

    def __init__(self, device_id: str, clock: SimClock, **kw: Any):
        if device_id not in SMHI_OCEAN_PILOTS:
            raise ValueError("unsupported SMHI ocean observation pilot")
        self.pilot = SMHI_OCEAN_PILOTS[device_id]
        self.latitude, self.longitude = self.pilot.latitude, self.pilot.longitude
        self.url = self._series_url(self.pilot.parameters[0])
        require_approved_source(self.policy_id).require_endpoint(self.url)
        super().__init__(device_id, clock, **kw)
        self.fields = {
            **{_PARAMETERS[code].field: _PARAMETERS[code].unit for code in self.pilot.parameters},
            "measurement_depth_m": "m",
            "observation_unix": "s",
            "latitude": "deg",
            "longitude": "deg",
        }
        self.source = (
            f"{self.url} (SMHI Oceanographic Observations, CC BY 4.0; "
            f"station operator {self.pilot.owner}, measurement depth {self.pilot.depth:g} m.)"
        )

    @property
    def attribution(self) -> str:
        return f"SMHI Open Data · CC BY 4.0 · station operator {self.pilot.owner}"

    def _series_url(self, code: str) -> str:
        return f"{_BASE_URL}/{code}/station/{self.pilot.station}/period/latest-day/data.json"

    def sample(self) -> dict[str, float]:
        now = datetime.now(UTC).timestamp()
        series = {}
        for code in self.pilot.parameters:
            source = self._series_url(code)
            require_approved_source(self.policy_id).require_endpoint(source)
            series[_PARAMETERS[code].field] = self._observations(self._fetch(source), code, now)
        common = set.intersection(*(set(rows) for rows in series.values()))
        if not common:
            raise DeviceOffline(f"{self.device_id}: no fresh same-depth SMHI observations")
        measured = max(common)
        measurements = {field: rows[measured] for field, rows in series.items()}
        if measurements.get("current_speed_mps") == 0:
            measurements.pop("current_direction_deg", None)
        return {
            **measurements,
            "measurement_depth_m": self.pilot.depth,
            "observation_unix": measured,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def _observations(self, data: Any, code: str, now: float) -> dict[float, float]:
        if not isinstance(data, dict):
            raise DeviceOffline(f"{self.device_id}: invalid SMHI station response")
        station = data.get("station")
        parameter = data.get("parameter")
        spec = _PARAMETERS[code]
        if (not isinstance(station, dict) or station.get("key") != self.pilot.station
                or station.get("owner") != self.pilot.owner or not isinstance(parameter, dict)
                or parameter.get("key") != code or parameter.get("unit") != spec.source_unit):
            raise DeviceOffline(f"{self.device_id}: SMHI station identity or unit mismatch")
        positions = data.get("position")
        if not isinstance(positions, list) or len(positions) != 1 or not isinstance(positions[0], dict):
            raise DeviceOffline(f"{self.device_id}: SMHI fixed station coordinates unavailable")
        lat = _num(positions[0].get("latitude"))
        lon = _num(positions[0].get("longitude"))
        if lat is None or lon is None or not abs(lat - self.latitude) < 0.002 or not abs(lon - self.longitude) < 0.002:
            raise DeviceOffline(f"{self.device_id}: SMHI fixed station coordinates changed")
        period, rows = data.get("period"), data.get("value")
        if not isinstance(period, dict) or period.get("key") != "latest-day" or not isinstance(rows, list):
            raise DeviceOffline(f"{self.device_id}: SMHI daily observation response unavailable")

        results = {}
        for row in rows:
            if (not isinstance(row, dict) or row.get("quality") != "G"
                    or _num(row.get("depth")) != self.pilot.depth):
                continue
            date, value = row.get("date"), _num(row.get("value"))
            if (not isinstance(date, int) or not 0 <= now - date / 1000 <= _MAX_AGE
                    or value is None or not 0 <= value <= spec.maximum):
                continue
            observed = date / 1000
            scaled = value * spec.scale
            if results.setdefault(observed, scaled) != scaled:
                raise DeviceOffline(f"{self.device_id}: conflicting SMHI observations at one time and depth")
        return results
