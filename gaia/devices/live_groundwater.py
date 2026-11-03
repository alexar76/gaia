"""USGS continuous depth-to-water observations for vetted pilot wells."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any
from urllib.parse import urlencode

from gaia.devices._live_base import LiveDevice, _FT_TO_M, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.source_policy import require_approved_source


# Coordinates verified against the official USGS monitoring-locations registry.
_WELLS = {
    "USGS-445227067520101": (44.874240312155, -67.8663921558273),
    "USGS-424452076081902": (42.7476944444444, -76.13825),
    "USGS-373715086300503": (37.6209416666667, -86.5012805555556),
}
_API = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/latest-continuous"
_PARAMETER = "72019"
_MAX_AGE = timedelta(hours=24)


class UsgsGroundwater(LiveDevice):
    """One depth below local land surface per USGS pilot well."""

    model = "GAIA-GROUNDWATER (USGS continuous)"
    policy_id = "usgs_groundwater"
    fields = {
        "groundwater_depth_m": "m",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        f"{_API} (USGS Water Data for the Nation; U.S. public domain. "
        "USGS-operated groundwater wells, parameter 72019. Values may be provisional.)"
    )
    attribution = "U.S. Geological Survey Water Data for the Nation"
    url = f"{_API}/items?" + urlencode({
        "f": "json",
        "limit": 50,
        "filter-lang": "cql2-text",
        "filter": f"parameter_code='{_PARAMETER}' AND monitoring_location_id IN ("
        + ",".join(f"'{station_id}'" for station_id in _WELLS)
        + ")",
    })

    def read(self) -> dict[str, Any]:
        require_approved_source(self.policy_id).require_endpoint(self.url)
        payload = self._fetch(self.url)
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            raise DeviceOffline(f"{self.device_id}: invalid USGS feature collection")
        if len(features) >= 50 or any(
            isinstance(link, dict) and link.get("rel") == "next"
            for link in (payload.get("links") or [])
        ):
            raise DeviceOffline(f"{self.device_id}: incomplete USGS pilot observations")

        now = datetime.now(timezone.utc)
        by_station: dict[str, dict[str, Any]] = {}
        for feature in features:
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict):
                continue
            station_id = str(props.get("monitoring_location_id") or "")
            if (station_id not in _WELLS or str(props.get("parameter_code")) != _PARAMETER
                    or props.get("statistic_id") != "00011"):
                continue
            qualifier = str(props.get("qualifier") or "")
            if "TEST" in qualifier.upper():
                continue
            depth_ft = _num(props.get("value"))
            unit = str(props.get("unit_of_measure") or "").strip().lower()
            if depth_ft is None or not isfinite(depth_ft) or unit not in {"ft", "feet"}:
                continue
            depth_m = depth_ft * _FT_TO_M
            if not -500 <= depth_m <= 12_000:
                continue
            try:
                observed = datetime.fromisoformat(str(props.get("time") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if observed.tzinfo is None or not timedelta(0) <= now - observed <= _MAX_AGE:
                continue
            observed_unix = observed.timestamp()
            if station_id in by_station and by_station[station_id]["observation_unix"] >= observed_unix:
                continue
            latitude, longitude = _WELLS[station_id]
            by_station[station_id] = {
                "station_id": station_id,
                "groundwater_depth_m": depth_m,
                "observation_unix": observed_unix,
                "observed_at": observed.astimezone(timezone.utc).isoformat(),
                "latitude": latitude,
                "longitude": longitude,
                "parameter_code": _PARAMETER,
                "reference": "Local land surface (positive downward)",
                "approval_status": props.get("approval_status") or "Unknown",
                "qualifier": qualifier,
                "time_series_id": props.get("time_series_id"),
            }
        if not by_station:
            raise DeviceOffline(f"{self.device_id}: no USGS pilot well measurements from the last 24 hours")
        return signed_cluster_read(
            self,
            [by_station[station_id] for station_id in sorted(by_station)],
            numeric_keys=tuple(self.fields),
            meta_keys=(
                "station_id", "observed_at", "parameter_code", "reference",
                "approval_status", "qualifier", "time_series_id",
            ),
        )
