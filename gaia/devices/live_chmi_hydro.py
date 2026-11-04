"""Preliminary measured river levels and discharge from the Czech CHMI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_water_ext import _utc_time
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

CHMI_GAUGES = {
    "0-203-1-042000": (50.0946613, 15.8067603, "Němčice"),
    "0-203-1-101800": (50.2486875, 14.7753034, "Předměřice"),
    "0-203-1-200100": (50.0274827, 14.3968736, "Praha–Chuchle"),
}
_MAX_AGE = timedelta(hours=2)
# H_F and Q_F are forecasts and never enter the observation contract.
_ELEMENTS = {
    "H": ("gage_height_m", "CM", 0.01, -5.0, 200.0),
    "Q": ("discharge_m3s", "M3_S", 1.0, 0.0, 200_000.0),
    "TH": ("water_temperature_c", "0C", 1.0, -2.0, 45.0),
}


class ChmiRiverGauge(LiveDevice):
    """Same-time observed stage and discharge from a fixed Czech river gauge."""

    model = "GAIA-RIVER (Czech Hydrometeorological Institute)"
    policy_id = "chmi_hydrometry"
    fields = {
        "gage_height_m": "m",
        "discharge_m3s": "m3/s",
        "water_temperature_c": "cel",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, station_id: str, **kw: Any):
        if station_id not in CHMI_GAUGES:
            raise ValueError("unsupported CHMI pilot station")
        super().__init__(device_id, clock, **kw)
        self.station_id = station_id
        self.latitude, self.longitude, _ = CHMI_GAUGES[station_id]
        self.url = f"https://opendata.chmi.cz/hydrology/now/data/{station_id}.json"
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.source = (
            f"{self.url} (Czech Hydrometeorological Institute, CC BY 4.0; preliminary "
            "observed hydrometry. Stage in metres above local gauge datum, converted from cm. "
            "Observations and forecasts are separate series.)"
        )

    def map(self, payload: Any) -> dict[str, float]:
        objects = payload.get("objList") if isinstance(payload, dict) else None
        if not isinstance(objects, list) or len(objects) != 1 or not isinstance(objects[0], dict):
            raise DeviceOffline(f"{self.device_id}: invalid CHMI station response")
        station = objects[0]
        if station.get("objID") != self.station_id or not isinstance(station.get("tsList"), list):
            raise DeviceOffline(f"{self.device_id}: CHMI station identity or series mismatch")

        now = datetime.now(UTC)
        series: dict[str, dict[datetime, float]] = {}
        for entry in station["tsList"]:
            if not isinstance(entry, dict):
                continue
            code = entry.get("tsConID")
            if not isinstance(code, str) or code not in _ELEMENTS:
                continue
            field, unit, scale, minimum, maximum = _ELEMENTS[code]
            if entry.get("unit") != unit or not isinstance(entry.get("tsData"), list):
                continue
            if field in series:
                raise DeviceOffline(f"{self.device_id}: duplicate CHMI {code} series")
            values: dict[datetime, float] = {}
            for row in entry["tsData"]:
                if not isinstance(row, dict):
                    continue
                stamp = _utc_time(row.get("dt"))
                raw = _num(row.get("value"))
                if stamp is None or not timedelta(0) <= now - stamp <= _MAX_AGE or raw is None:
                    continue
                value = raw * scale
                if minimum <= value <= maximum and values.setdefault(stamp, value) != value:
                    raise DeviceOffline(f"{self.device_id}: conflicting CHMI {code} observations")
            series[field] = values

        paired = series.get("gage_height_m", {}).keys() & series.get("discharge_m3s", {}).keys()
        if not paired:
            raise DeviceOffline(f"{self.device_id}: no paired CHMI observations from the last 2 hours")
        measured = max(paired)
        return {
            **{field: values[measured] for field, values in series.items() if measured in values},
            "observation_unix": measured.timestamp(),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
