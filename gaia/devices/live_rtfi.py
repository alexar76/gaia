"""USGS surveyed flood impact thresholds checked against fresh gage data."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.source_policy import require_approved_source

_SERIES_ID = re.compile(r"[0-9a-f]{32}")
_GAUGE_ID = re.compile(r"[0-9]{8,15}")
_LEVEL_CODES = {"00065", "63160", "72279", "62614", "62615"}
_METRES_PER_UNIT = {"ft": 0.3048, "m": 1.0}
_MAX_AGE = timedelta(hours=2)


class UsgsFloodImpacts(LiveDevice):
    """Provisional threshold exceedances, at the surveyed reference points."""

    model = "GAIA-FLOOD (USGS RT-FI)"
    policy_id = "usgs_rtfi"
    url = "https://api.waterdata.usgs.gov/rtfi-api/referencepoints/flooding/geojson"
    _series_url = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/latest-continuous/items"
    source = (
        url + " (U.S. Geological Survey Real-Time Flood Impacts; U.S. public domain. "
        "USGS gage reading above surveyed threshold. Provisional; possible inundation.)"
    )
    attribution = "U.S. Geological Survey Real-Time Flood Impacts / Water Data for the Nation"
    fields = {
        "flood_gage_level_m": "m",
        "flood_threshold_m": "m",
        "flood_exceedance_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }

    def _features(self, url: str) -> list[dict[str, Any]]:
        require_approved_source(self.policy_id).require_endpoint(url)
        payload = self._fetch(url)
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or any(not isinstance(f, dict) for f in features):
            raise DeviceOffline(f"{self.device_id}: invalid USGS feature collection")
        return features

    def _gage_readings(self, series_ids: list[str]) -> dict[str, dict[str, Any]]:
        readings: dict[str, dict[str, Any]] = {}
        for start in range(0, len(series_ids), 50):
            ids = series_ids[start : start + 50]
            # latest-continuous supplies one observation per series.
            query = urlencode({
                "f": "json",
                "limit": len(ids),
                "filter-lang": "cql2-text",
                "filter": f"time_series_id IN ({','.join(repr(series) for series in ids)})",
            })
            for feature in self._features(f"{self._series_url}?{query}"):
                props = feature.get("properties")
                if isinstance(props, dict) and props.get("time_series_id") in ids:
                    readings[props["time_series_id"]] = props
        return readings

    def read(self) -> dict[str, Any]:
        candidates = []
        for feature in self._features(self.url):
            props = feature.get("properties")
            geometry = feature.get("geometry")
            if not isinstance(props, dict) or not isinstance(geometry, dict):
                continue
            if props.get("active") is not True or props.get("is_flooding") is not True:
                continue
            series = str(props.get("plot_series_id") or "")
            gage = str(props.get("nwis_id") or "")
            point_id = props.get("id")
            if not (_SERIES_ID.fullmatch(series) and _GAUGE_ID.fullmatch(gage)
                    and type(point_id) is int and point_id > 0):
                continue
            coords = geometry.get("coordinates")
            if geometry.get("type") != "Point" or not isinstance(coords, list) or len(coords) < 2:
                continue
            lon, lat = _num(coords[0]), _num(coords[1])
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            candidates.append((point_id, series, gage, props, lat, lon))

        readings = self._gage_readings(sorted({row[1] for row in candidates}))
        now = datetime.now(UTC)
        hotspots = []
        seen: set[int] = set()
        for point_id, series, gage, props, lat, lon in candidates:
            reading = readings.get(series, {})
            parameter = reading.get("parameter_code")
            if (point_id in seen or reading.get("monitoring_location_id") != f"USGS-{gage}"
                    or parameter not in _LEVEL_CODES or parameter != props.get("plot_param")
                    or reading.get("statistic_id") != "00011"
                    or reading.get("approval_status") not in ("Approved", "Provisional")
                    or reading.get("qualifier")):
                continue
            unit = str(props.get("unit") or "")
            factor = _METRES_PER_UNIT.get(unit)
            if factor is None or unit != reading.get("unit_of_measure"):
                continue
            level, threshold = _num(reading.get("value")), _num(props.get("rp_elevation"))
            if (level is None or threshold is None or not math.isfinite(level)
                    or not math.isfinite(threshold) or level <= threshold):
                continue
            try:
                observed_at = datetime.fromisoformat(str(reading.get("time")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if observed_at.tzinfo is None or not -timedelta(minutes=5) <= now - observed_at <= _MAX_AGE:
                continue
            hotspots.append({
                "rtfi_point_id": point_id,
                "latitude": lat,
                "longitude": lon,
                "flood_gage_level_m": level * factor,
                "flood_threshold_m": threshold * factor,
                "flood_exceedance_m": (level - threshold) * factor,
                "gage_id": gage,
                "gage_series_id": series,
                "gage_parameter_code": parameter,
                "observed_at": observed_at.isoformat(),
                "status": "Provisional threshold exceedance",
                "claim_class": "surveyed_flood_threshold_exceedance",
                "evidence_boundary": "Possible local inundation from a gage threshold. Confirm road closures separately.",
            })
            seen.add(point_id)

        hotspots.sort(key=lambda row: row["rtfi_point_id"])
        return signed_cluster_read(
            self, hotspots, numeric_keys=tuple(self.fields),
            meta_keys=("rtfi_point_id", "gage_id", "gage_series_id", "gage_parameter_code",
                       "observed_at", "status", "claim_class", "evidence_boundary"),
        )
