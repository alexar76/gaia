"""Verified Fintraffic road notices and NOAA G-AIRMET forecast geometry."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.source_policy import require_approved_source


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _coordinates(raw: Any, limits: tuple[int, ...]) -> Any:
    if not isinstance(raw, list):
        raise ValueError("invalid coordinates")
    if limits:
        if len(raw) < limits[0]:
            raise ValueError("incomplete geometry")
        return [_coordinates(part, limits[1:]) for part in raw]
    if len(raw) < 2:
        raise ValueError("missing coordinate")
    lon, lat = _num(raw[0]), _num(raw[1])
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("invalid coordinate")
    # About one metre of coordinate precision keeps detailed road networks
    # below Hub response limits while preserving each source path and ring.
    return [round(lon, 5), round(lat, 5)]


_GEOMETRIES = {
    "Point": (),
    "LineString": (2,),
    "MultiLineString": (1, 2),
    "Polygon": (1, 4),
    "MultiPolygon": (1, 1, 4),
}


def _geometry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    if not isinstance(kind, str) or kind not in _GEOMETRIES:
        return None
    try:
        coords = _coordinates(raw.get("coordinates"), _GEOMETRIES[kind])
    except ValueError:
        return None
    polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    if any(ring[0] != ring[-1] for polygon in polygons for ring in polygon):
        return None
    anchor = coords
    while isinstance(anchor[0], list):
        anchor = anchor[0]
    lon, lat = anchor

    def vertices(parts):
        if isinstance(parts[0], list):
            for part in parts:
                yield from vertices(part)
        else:
            yield parts

    xs, ys = zip(*vertices(coords))
    return {
        "geometry": {"type": kind, "coordinates": coords},
        "event_bbox": [min(xs), min(ys), max(xs), max(ys)],
        "latitude": lat,
        "longitude": lon,
    }


class _TransportEvents(LiveDevice):
    fields = {"latitude": "deg", "longitude": "deg", "transport_notice_count": "count"}

    def _features(self) -> list[dict[str, Any]]:
        require_approved_source(self.policy_id).require_endpoint(self.url)
        payload = self._fetch(self.url)
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or any(not isinstance(feature, dict) for feature in features):
            raise DeviceOffline(f"{self.device_id}: invalid GeoJSON feature collection")
        return features

    def _sign(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()) > 3_000_000:
            raise DeviceOffline(f"{self.device_id}: event geometry exceeds the response budget")
        return signed_cluster_read(
            self, rows, numeric_keys=tuple(self.fields), structured_keys=("geometry", "event_bbox"),
            meta_keys=("event_id", "title", "notice_text", "notice_type", "notice_subtype", "issue_at",
                       "valid_from", "valid_until", "valid_at", "claim_class", "evidence_boundary",
                       "forecast_hour", "base_ft", "top_ft", "level_ft", "severity"),
            reading_meta={"source_read_at": datetime.now(UTC).isoformat()},
            empty_values={"transport_notice_count": 0.0},
        )


class DigitrafficRoadNotices(_TransportEvents):
    """Notices whose published validity interval includes the current time."""

    model = "GAIA-ROAD-NOTICES (Fintraffic)"
    policy_id = "digitraffic_road_events"
    headers = {"Accept-Encoding": "gzip", "Digitraffic-User": "aicom-gaia"}
    attribution = "Fintraffic Digitraffic · CC BY 4.0"
    _base = "https://tie.digitraffic.fi/api/traffic-message/v2/"

    def __init__(self, *args: Any, roadworks: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.notice_type = "road work" if roadworks else "traffic announcement"
        self.url = self._base + ("roadworks" if roadworks else "traffic-announcements")
        self.source = (
            f"{self.url} (Fintraffic Digitraffic, CC BY 4.0; credit Fintraffic Digitraffic. "
            "Geometry coordinates rounded to five decimals.)"
        )

    def read(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        rows = {}
        for feature in self._features():
            props = feature.get("properties")
            if not isinstance(props, dict) or props.get("situationType") != self.notice_type:
                continue
            event_id = str(props.get("situationId") or "")
            issued = _utc(props.get("versionTime") or props.get("releaseTime"))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", event_id) or not issued or issued > now:
                continue
            old = rows.get(event_id)
            if old and old["issue_at"] >= issued.isoformat():
                continue
            geometry = _geometry(feature.get("geometry"))
            announcements = props.get("announcements")
            if geometry is None or not isinstance(announcements, list):
                continue
            for announcement in announcements:
                timing = announcement.get("timeAndDuration") if isinstance(announcement, dict) else None
                if not isinstance(timing, dict):
                    continue
                start, end = _utc(timing.get("startTime")), _utc(timing.get("endTime"))
                if not start or start > now or (timing.get("endTime") and not end) or (end and end <= now):
                    continue
                rows[event_id] = {
                    **geometry, "event_id": event_id, "title": announcement.get("title") or self.notice_type,
                    "notice_text": announcement.get("comment"),
                    "notice_type": self.notice_type, "notice_subtype": props.get("trafficAnnouncementType"),
                    "issue_at": issued.isoformat(), "valid_from": start.isoformat(),
                    "valid_until": end.isoformat() if end else None,
                    "claim_class": "published_road_notice",
                    "evidence_boundary": "Published Finnish road notice within its stated validity; check Fintraffic for current restrictions and work schedules.",
                }
                break
        return self._sign(list(rows.values()))


class AwcGairmet(_TransportEvents):
    """Nearest still-valid NOAA/NWS graphical aviation advisory snapshot."""

    model = "GAIA-G-AIRMET (NOAA/NWS AWC)"
    policy_id = "awc_gairmet"
    url = "https://aviationweather.gov/api/data/gairmet?format=geojson&fore=all"
    source = url + " (NOAA/NWS Aviation Weather Center; U.S. public domain; forecast snapshot.)"
    attribution = "NOAA/NWS Aviation Weather Center"

    @staticmethod
    def _altitude(value: Any) -> int | None:
        if value == "SFC":
            return 0
        text = str(value or "")
        return int(text) * 100 if re.fullmatch(r"[0-9]{1,3}", text) else None

    def read(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        rows: dict[str, dict[str, Any]] = {}
        for feature in self._features():
            props = feature.get("properties")
            if not isinstance(props, dict):
                continue
            issued, valid = _utc(props.get("issueTime")), _utc(props.get("validTime"))
            if (not issued or not valid or not now - timedelta(hours=7) <= issued <= now
                    or not now <= valid <= now + timedelta(hours=3) or valid < issued):
                continue
            geometry = _geometry(feature.get("geometry"))
            hazard = str(props.get("hazard") or "").strip()
            tag = str(props.get("tag") or "").strip()
            product = str(props.get("product") or "").strip()
            if not geometry or not hazard or not tag or not product:
                continue
            key_parts = [
                product, hazard, tag, valid.isoformat(),
                props.get("base"), props.get("top"), props.get("level"),
                geometry["geometry"],
            ]
            key = hashlib.sha256(json.dumps(key_parts, separators=(",", ":")).encode()).hexdigest()[:24]
            old = rows.get(key)
            if old and old["issue_at"] >= issued.isoformat():
                continue
            rows[key] = {
                **geometry, "event_id": key, "title": f"G-AIRMET {hazard}", "notice_type": hazard,
                "notice_text": props.get("dueTo"), "severity": props.get("severity"),
                "notice_subtype": product, "issue_at": issued.isoformat(),
                "valid_at": valid.isoformat(), "forecast_hour": props.get("forecast"),
                "base_ft": self._altitude(props.get("base")), "top_ft": self._altitude(props.get("top")),
                "level_ft": self._altitude(props.get("level")),
                "claim_class": "aviation_forecast_snapshot",
                "evidence_boundary": "Forecast at the stated UTC snapshot, for situational awareness. Obtain an official aviation weather briefing for flight decisions.",
            }
        first_snapshot = min((row["valid_at"] for row in rows.values()), default=None)
        return self._sign([row for row in rows.values() if row["valid_at"] == first_snapshot])
