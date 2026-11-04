"""Preliminary bridge air-gap observations from NOAA NOS CO-OPS PORTS."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

NOAA_AIR_GAP_PILOTS = {
    "noaa-airgap-8726371": (27.620583, -82.65581, "Sunshine Skyway Bridge"),
    "noaa-airgap-8545556": (39.952778, -75.13583, "Ben Franklin Bridge"),
    "noaa-airgap-8575432": (38.993195, -76.382195, "Bay Bridge (Center Channel)"),
}
_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
_MAX_AGE = timedelta(minutes=18)
_FLAGS = re.compile(r"([0-9]{1,3}),([01]),([01]),([01])")


class NoaaAirGap(LiveDevice):
    """Vertical separation between the water and NOAA's fixed bridge reference."""

    model = "GAIA-BRIDGE-CLEARANCE (NOAA PORTS Air Gap)"
    policy_id = "noaa_ports_air_gap"
    attribution = "NOAA NOS CO-OPS · U.S. Government public domain"
    fields = {
        "air_gap_m": "m",
        "air_gap_sigma_m": "m",
        "air_gap_outlier_count": "count",
        "air_gap_flat_flag": "flag",
        "air_gap_rate_flag": "flag",
        "air_gap_limit_flag": "flag",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, **kwargs: Any) -> None:
        if device_id not in NOAA_AIR_GAP_PILOTS:
            raise ValueError("unsupported NOAA Air Gap bridge pilot")
        self.latitude, self.longitude, _ = NOAA_AIR_GAP_PILOTS[device_id]
        self.station_id = device_id.removeprefix("noaa-airgap-")
        self.url = f"{_API}?" + urlencode({
            "station": self.station_id, "product": "air_gap", "date": "latest", "time_zone": "gmt",
            "units": "metric", "application": "GAIA", "format": "json",
        })
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.source = (
            f"{self.url} (NOAA NOS CO-OPS PORTS, U.S. Government public domain. Preliminary "
            "observed vertical distance from the water surface to the fixed bridge reference. "
            "For situational awareness. For navigation consult official NOAA PORTS and charts.)"
        )
        super().__init__(device_id, clock, **kwargs)

    def map(self, payload: Any) -> dict[str, float]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: invalid NOAA Air Gap response")
        metadata, rows = payload.get("metadata"), payload.get("data")
        if not isinstance(metadata, dict) or metadata.get("id") != self.station_id:
            raise DeviceOffline(f"{self.device_id}: NOAA Air Gap station unavailable or mismatched")
        latitude, longitude = _num(metadata.get("lat")), _num(metadata.get("lon"))
        if (latitude is None or not abs(latitude - self.latitude) < 0.001
                or longitude is None or not abs(longitude - self.longitude) < 0.001):
            raise DeviceOffline(f"{self.device_id}: NOAA Air Gap station coordinates changed")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise DeviceOffline(f"{self.device_id}: NOAA Air Gap latest observation unavailable")
        row = rows[0]
        try:
            observed = datetime.strptime(row.get("t"), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            raise DeviceOffline(f"{self.device_id}: invalid NOAA Air Gap observation time") from exc
        if not timedelta(0) <= datetime.now(UTC) - observed <= _MAX_AGE:
            raise DeviceOffline(f"{self.device_id}: NOAA Air Gap observation is not current")
        raw_flags = row.get("f")
        flags = _FLAGS.fullmatch(raw_flags) if isinstance(raw_flags, str) else None
        if not flags:
            raise DeviceOffline(f"{self.device_id}: invalid NOAA Air Gap flags")
        outliers, flat, rate, limit = map(int, flags.groups())
        if outliers > 360 or flat or rate or limit:
            raise DeviceOffline(f"{self.device_id}: NOAA Air Gap observation failed quality checks")
        gap, sigma = _num(row.get("v")), _num(row.get("s"))
        if gap is None or not 0 < gap <= 150 or sigma is None or not 0 <= sigma <= gap:
            raise DeviceOffline(f"{self.device_id}: invalid NOAA Air Gap value or sigma")
        return {
            "air_gap_m": gap,
            "air_gap_sigma_m": sigma,
            "air_gap_outlier_count": float(outliers),
            "air_gap_flat_flag": float(flat),
            "air_gap_rate_flag": float(rate),
            "air_gap_limit_flag": float(limit),
            "observation_unix": observed.timestamp(),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
