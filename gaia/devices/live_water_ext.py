"""NOAA river forecasts and Environment Agency measured coastal water levels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any

from gaia.devices._live_base import _FT_TO_M, LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

NWPS_GAUGES = {
    "PTTP1": (40.439444444444, -80.010833333333, "Pittsburgh"),
    "RMDV2": (37.563055555556, -77.547222222222, "Richmond-Westham"),
    "VCKM6": (32.315, -90.905833, "Vicksburg"),
    "DLRT2": (29.330833333333, -100.92888888889, "Del Rio"),
}

EA_TIDE_GAUGES = {
    "E70039": (52.473075, 1.750085, "Lowestoft"),
    "E72639": (51.49999, -2.728468, "Avonmouth Portbury"),
    "E70539": (53.31394, -4.62043, "Holyhead"),
    "E70739": (57.14406, -2.08013, "Aberdeen"),
    "E73839": (54.66518, -5.67045, "Bangor"),
}


def _utc_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


class NwsRiverForecast(LiveDevice):
    """Highest published upcoming forecast stage in the next 72 hours."""

    model = "GAIA-RIVER-FORECAST (NOAA/NWS NWPS)"
    policy_id = "noaa_nwps_forecast"
    fields = {
        "forecast_stage_m": "m above local gage datum",
        "forecast_issued_unix": "s",
        "forecast_valid_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, gauge: str, **kw: Any):
        if gauge not in NWPS_GAUGES:
            raise ValueError("unsupported NWPS pilot gage")
        super().__init__(device_id, clock, **kw)
        self.latitude, self.longitude, _ = NWPS_GAUGES[gauge]
        self.url = f"https://api.water.noaa.gov/nwps/v1/gauges/{gauge}/stageflow/forecast"
        self.source = (
            f"{self.url} (NOAA/NWS NWPS official river forecast; U.S. public domain. "
            "Peak of available upcoming samples within 72 hours, on the local gage datum.)"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float]:
        if (not isinstance(payload, dict) or payload.get("pedts") != "HGIFF"
                or payload.get("primaryName") != "Stage" or payload.get("primaryUnits") != "ft"):
            raise DeviceOffline(f"{self.device_id}: NWPS stage forecast unavailable")
        now = datetime.now(UTC)
        issued = _utc_time(payload.get("issuedTime"))
        if issued is None or not timedelta(0) <= now - issued <= timedelta(hours=24):
            raise DeviceOffline(f"{self.device_id}: NWPS forecast issue time outside 24-hour window")
        samples = payload.get("data")
        if not isinstance(samples, list):
            raise DeviceOffline(f"{self.device_id}: invalid NWPS forecast sample list")
        peak: tuple[float, datetime] | None = None
        for row in samples:
            if not isinstance(row, dict):
                continue
            valid = _utc_time(row.get("validTime"))
            stage = _num(row.get("primary"))
            # Reject source missing-value sentinels and avoid counting expired forecast samples.
            if (valid is None or stage is None or not isfinite(stage) or not -100 <= stage <= 1000
                    or not now <= valid <= now + timedelta(hours=72)):
                continue
            if peak is None or stage > peak[0] or (stage == peak[0] and valid < peak[1]):
                peak = stage, valid
        if peak is None:
            raise DeviceOffline(f"{self.device_id}: NWPS has no valid upcoming forecast samples")
        return {
            "forecast_stage_m": peak[0] * _FT_TO_M,
            "forecast_issued_unix": issued.timestamp(),
            "forecast_valid_unix": peak[1].timestamp(),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


class EaTideGauge(LiveDevice):
    """Fifteen-minute measured coastal mean, on the Ordnance Datum at Newlyn."""

    model = "GAIA-TIDE (UK Environment Agency)"
    policy_id = "ea_tide_gauge"
    fields = {
        "water_level_maod": "mAOD",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, gauge: str, **kw: Any):
        if gauge not in EA_TIDE_GAUGES:
            raise ValueError("unsupported Environment Agency pilot tide gauge")
        super().__init__(device_id, clock, **kw)
        self.gauge = gauge
        self.latitude, self.longitude, _ = EA_TIDE_GAUGES[gauge]
        self.measure = f"{gauge}-level-tidal_level-Mean-15_min-mAOD"
        self.url = f"https://environment.data.gov.uk/flood-monitoring/id/measures/{self.measure}"
        self.source = (
            f"{self.url} (Environment Agency tide gauge real-time data API (Beta); "
            "Open Government Licence v3. Measured 15-minute mean relative to Ordnance Datum at Newlyn.)"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float]:
        measure = payload.get("items") if isinstance(payload, dict) else None
        if (not isinstance(measure, dict) or measure.get("notation") != self.measure
                or measure.get("stationReference") != self.gauge or measure.get("unitName") != "mAOD"
                or measure.get("period") != 900
                or str(measure.get("datumType")).rsplit("/", 1)[-1] != "datumAOD"):
            raise DeviceOffline(f"{self.device_id}: Environment Agency mAOD measure unavailable")
        reading = measure.get("latestReading")
        if not isinstance(reading, dict) or str(reading.get("measure")).rsplit("/", 1)[-1] != self.measure:
            raise DeviceOffline(f"{self.device_id}: Environment Agency tide reading unavailable")
        observed = _utc_time(reading.get("dateTime"))
        level = _num(reading.get("value"))
        if (observed is None or not timedelta(0) <= datetime.now(UTC) - observed <= timedelta(hours=2)
                or level is None or not -20 <= level <= 20):
            raise DeviceOffline(f"{self.device_id}: Environment Agency tide reading stale or invalid")
        return {
            "water_level_maod": level,
            "observation_unix": observed.timestamp(),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
