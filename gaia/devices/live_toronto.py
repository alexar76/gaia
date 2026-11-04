"""City of Toronto road restrictions and licensed GBFS v1 station availability."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from gaia.devices._live_base import LiveDevice, _UA, _num
from gaia.devices._policy import _assert_url_allowed
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.live_transport_events import _geometry
from gaia.source_policy import require_approved_source

_TORONTO_LICENSE = "https://open.toronto.ca/open-data-licence/"
_TORONTO_CREDIT = "City of Toronto · Contains information licensed under the Open Government Licence – Toronto."
_TORONTO_ZONE = ZoneInfo("America/Toronto")
_ROAD_SNAPSHOT_AGE = timedelta(minutes=15)
_BIKE_AGE = timedelta(minutes=5)
_MAX_HOTSPOT_BYTES = 950_000  # leave room for the attestation and Hub envelope
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEKDAYS = range(5)
_ROAD_DAYS = {
    "Daily": range(7), "Weekdays": _WEEKDAYS, "Weekends": range(5, 7),
    "Monday To Saturday": range(6), "Activity Schedule": range(7),
}
_ROAD_TYPES = {
    "ROAD_CLOSED": "Road closure", "CONSTRUCTION": "Road works",
    "ACCIDENT": "Road incident", "HAZARD": "Road hazard",
}
_ROAD_IMPACT = {1: "Low", 2: "Medium", 3: "High", "Low": "Low", "Medium": "Medium", "High": "High"}
_ROAD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STATION_ID = re.compile(r"[A-Za-z0-9_-]{1,24}")
_HOUR_RANGE = re.compile(r"(\d\d):(\d\d)-(\d\d):(\d\d)")


def _epoch(value: Any, *, milliseconds: bool = False) -> datetime | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 16:
        value = int(value)
    if type(value) is not int or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000 if milliseconds else value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _toronto_coordinate(lon_value: Any, lat_value: Any) -> tuple[float, float] | None:
    lon, lat = _num(lon_value), _num(lat_value)
    if lon is None or lat is None or not (-80 <= lon <= -79 and 43 <= lat <= 44):
        return None
    return lon, lat


def _road_shape(record: dict[str, Any]) -> dict[str, Any] | None:
    raw = record.get("geoPolyline")
    if raw in (None, ""):
        point = _toronto_coordinate(record.get("longitude"), record.get("latitude"))
        return _geometry({"type": "Point", "coordinates": list(point)}) if point else None
    if not isinstance(raw, str) or len(raw) > 40_000:
        return None
    try:
        coordinates = json.loads(raw if raw.startswith("[[") else f"[{raw}]")
    except (ValueError, RecursionError):
        return None
    if not coordinates or not isinstance(coordinates, list):
        return None
    line = []
    order = None
    for pair in coordinates:
        if not isinstance(pair, list) or len(pair) != 2:
            return None
        # The V3 JSON is lon/lat. The City's readme also describes lat/lon;
        # only one ordering is possible within the stated Toronto bounds.
        point = _toronto_coordinate(*pair)
        lon_first = point is not None
        if point is None:
            point = _toronto_coordinate(pair[1], pair[0])
        if point is None or (order is not None and lon_first != order):
            return None
        order = lon_first
        if not line or line[-1] != list(point):
            line.append(list(point))
    if len(line) == 1:
        return _geometry({"type": "Point", "coordinates": line[0]})
    return _geometry({"type": "LineString", "coordinates": line})


def _local_boundary(day: date, minute: int) -> datetime | None:
    wall = datetime.combine(day, time()) + timedelta(minutes=minute)
    earlier = wall.replace(tzinfo=_TORONTO_ZONE, fold=0).astimezone(UTC)
    later = wall.replace(tzinfo=_TORONTO_ZONE, fold=1).astimezone(UTC)
    # Ambiguous or nonexistent wall clock times lack an explicit offset.
    return earlier if earlier == later else None


def _daily_intervals(day: date, schedule: Any) -> list[tuple[datetime, datetime]]:
    if not isinstance(schedule, str):
        return []
    windows = []
    for part in schedule.split(","):
        match = _HOUR_RANGE.fullmatch(part.strip())
        if match is None:
            return []
        sh, sm, eh, em = (int(value) for value in match.groups())
        if sh > 23 or sm > 59 or eh > 24 or em > 59 or (eh == 24 and em != 0):
            return []
        start_minute, end_minute = sh * 60 + sm, eh * 60 + em
        if end_minute == start_minute:
            return []
        if end_minute < start_minute:
            end_minute += 24 * 60
        start, end = _local_boundary(day, start_minute), _local_boundary(day, end_minute)
        if start is not None and end is not None:
            windows.append((start, end))
    return windows


def _road_interval(
    record: dict[str, Any], now: datetime, published: datetime,
) -> tuple[datetime, datetime] | None:
    start = _epoch(record.get("startTime"), milliseconds=True)
    end = _epoch(record.get("endTime"), milliseconds=True)
    if start is None or end is None or not start <= published <= now < end:
        return None
    period = record.get("workPeriod")
    if period == "Continuous":
        return start, end
    days = _ROAD_DAYS.get(period) if isinstance(period, str) else None
    if days is None:
        return None
    local_day = now.astimezone(_TORONTO_ZONE).date()
    intervals = []
    for day in (local_day, local_day - timedelta(days=1)):
        if day.weekday() not in days:
            continue
        day_key = f"schedule{_DAY_NAMES[day.weekday()]}"
        schedule = record.get(day_key) or record.get("scheduleEveryday")
        for lower, upper in _daily_intervals(day, schedule):
            lower, upper = max(start, lower), min(end, upper)
            if lower <= published <= now < upper:
                intervals.append((lower, upper))
    return max(intervals, key=lambda window: window[1], default=None)


class TorontoRoadRestrictions(LiveDevice):
    """Currently active restrictions in the City's published V3 response."""

    model = "GAIA-ROAD-NOTICES (Toronto)"
    retain_hotspots_in_history = False
    policy_id = "toronto_road_restrictions"
    url = "https://secure.toronto.ca/opendata/cart/road_restrictions/v3?format=json"
    attribution = _TORONTO_CREDIT
    source = (
        f"{url} (City of Toronto Road Restrictions V3. {_TORONTO_CREDIT} {_TORONTO_LICENSE}. "
        "Filtered to active scheduled windows and normalized to WGS84 geometry.)"
    )
    fields = {"latitude": "deg", "longitude": "deg", "transport_notice_count": "count",
              "source_published_unix": "s", "source_expires_unix": "s"}

    def _snapshot(self) -> tuple[Any, datetime]:
        require_approved_source(self.policy_id).require_endpoint(self.url)
        url = _assert_url_allowed(self.url)
        headers = {"User-Agent": _UA, "Accept": "application/json", "Cache-Control": "no-cache"}
        data = bytearray()
        try:
            with httpx.stream("GET", url, headers=headers, timeout=self.timeout, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise DeviceOffline(f"{self.device_id}: upstream HTTP {response.status_code}")
                published = parsedate_to_datetime(response.headers.get("Date", ""))
                if published.tzinfo is None:
                    raise ValueError("Date missing offset")
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > 8 * 1024 * 1024:
                        raise DeviceOffline(f"{self.device_id}: Toronto response exceeds 8 MiB")
                    data.extend(chunk)
            return json.loads(data), published.astimezone(UTC)
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise DeviceOffline(f"{self.device_id}: invalid Toronto road snapshot or HTTP Date") from exc

    def read(self) -> dict[str, Any]:
        payload, published = self._snapshot()
        now = datetime.now(UTC)
        expires = published + _ROAD_SNAPSHOT_AGE
        if not published <= now < expires:
            raise DeviceOffline(f"{self.device_id}: Toronto response Date outside 15-minute window")
        source_times = {"source_published_at": published.isoformat(), "source_expires_at": expires.isoformat()}
        records = payload.get("Closure") if isinstance(payload, dict) else None
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise DeviceOffline(f"{self.device_id}: missing Toronto Closure array")
        latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
        for record in records:
            event_id = record.get("id")
            issue = _epoch(record.get("lastUpdated"), milliseconds=True)
            if not isinstance(event_id, str) or not _ROAD_ID.fullmatch(event_id) or issue is None:
                raise DeviceOffline(f"{self.device_id}: malformed Toronto road identity or update time")
            previous = latest.get(event_id)
            if previous is None or previous[0] < issue:
                latest[event_id] = issue, record
            elif previous[0] == issue and previous[1] != record:
                raise DeviceOffline(f"{self.device_id}: conflicting Toronto road record versions")
        rows = []
        for event_id, (issue, record) in sorted(latest.items()):
            if issue > published:
                raise DeviceOffline(f"{self.device_id}: Toronto record is newer than its snapshot")
            raw_impact = record.get("currImpact")
            impact = _ROAD_IMPACT.get(raw_impact) if type(raw_impact) in (int, str) else None
            raw_type = record.get("type")
            kind = _ROAD_TYPES.get(raw_type) if isinstance(raw_type, str) else None
            if record.get("expired") != 0 or impact is None or kind is None:
                continue
            timing = _road_interval(record, now, published)
            if timing is None:
                continue
            shape = _road_shape(record)
            if shape is None:
                raise DeviceOffline(f"{self.device_id}: active Toronto restriction has invalid geometry")
            start, end = timing
            road = record.get("road")
            title = f"{kind} · {road.strip()[:120]}" if isinstance(road, str) and road.strip() else kind
            direction = record.get("directionsAffected")
            direction_text = {
                "ONE_DIRECTION": "One direction affected.", "BOTH_DIRECTIONS": "Both directions affected.",
            }.get(direction) if isinstance(direction, str) else None
            rows.append({
                **shape, **source_times, "event_id": event_id, "title": title, "notice_type": kind,
                "notice_subtype": raw_type, "severity": impact, "notice_text": direction_text,
                "issue_at": issue.isoformat(), "valid_from": start.isoformat(),
                "valid_until": end.isoformat(), "claim_class": "published_road_notice",
            })
        if len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()) > _MAX_HOTSPOT_BYTES:
            raise DeviceOffline(f"{self.device_id}: Toronto event geometry exceeds the response budget")
        return signed_cluster_read(
            self, rows, numeric_keys=tuple(self.fields), structured_keys=("geometry", "event_bbox"),
            meta_keys=("event_id", "title", "notice_type", "notice_subtype", "severity", "notice_text",
                       "issue_at", "valid_from", "valid_until", "claim_class", *source_times),
            reading_meta={**source_times, "source_read_at": now.isoformat()},
            empty_values={"transport_notice_count": 0.0, "source_published_unix": published.timestamp(),
                          "source_expires_unix": expires.timestamp()},
        )


def _gbfs_stations(payload: Any) -> tuple[datetime, dict[str, dict[str, Any]]] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None
    published = _epoch(payload.get("last_updated"))
    stations = payload["data"].get("stations")
    if published is None or not isinstance(stations, list) or len(stations) > 5000:
        return None
    rows = {}
    for station in stations:
        station_id = station.get("station_id") if isinstance(station, dict) else None
        if not isinstance(station_id, str) or not _STATION_ID.fullmatch(station_id) or station_id in rows:
            return None
        rows[station_id] = station
    return published, rows


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 10000 else None


class TorontoBikeShare(LiveDevice):
    """Availability and coordinates from the City's linked GBFS v1 feeds."""

    model = "GAIA-BIKESHARE (Toronto)"
    retain_hotspots_in_history = False
    policy_id = "toronto_bikeshare"
    url = "https://tor.publicbikesystem.net/ube/gbfs/v1/en/station_status"
    info_url = "https://tor.publicbikesystem.net/ube/gbfs/v1/en/station_information"
    attribution = _TORONTO_CREDIT
    source = (
        f"{url} + {info_url} (Bike Share Toronto, City-linked GBFS v1. {_TORONTO_CREDIT} {_TORONTO_LICENSE}. "
        "Joined station coordinates and live availability.)"
    )
    fields = {
        "latitude": "deg", "longitude": "deg", "observation_unix": "s", "bikes_available": "count",
        "ebikes_available": "count", "docks_available": "count", "is_renting": "bool", "is_returning": "bool",
        "bikeshare_station_count": "count", "source_published_unix": "s", "source_expires_unix": "s",
    }

    def read(self) -> dict[str, Any]:
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(self.info_url)
        policy.require_endpoint(self.url)
        info = _gbfs_stations(self._fetch(self.info_url))
        status = _gbfs_stations(self._fetch(self.url))
        now = datetime.now(UTC)
        if info is None or status is None:
            raise DeviceOffline(f"{self.device_id}: invalid Toronto GBFS station payload")
        info_published, metadata = info
        published, reports = status
        expires = published + _BIKE_AGE
        if not info_published <= now or not published <= now < expires:
            raise DeviceOffline(f"{self.device_id}: stale or future Toronto GBFS snapshot")
        if reports and not (reports.keys() & metadata.keys()):
            raise DeviceOffline(f"{self.device_id}: no matching Toronto GBFS station metadata")
        rows = []
        for station_id in sorted(reports.keys() & metadata.keys()):
            report, station = reports[station_id], metadata[station_id]
            observed = _epoch(report.get("last_reported"))
            if observed is None or observed > published or now >= observed + _BIKE_AGE:
                continue
            point = _toronto_coordinate(station.get("lon"), station.get("lat"))
            if point is None or report.get("is_installed") not in (1, True):
                continue
            bikes = _count(report.get("num_bikes_available"))
            docks = _count(report.get("num_docks_available"))
            renting, returning = report.get("is_renting"), report.get("is_returning")
            if bikes is None or docks is None or renting not in (0, 1) or returning not in (0, 1):
                raise DeviceOffline(f"{self.device_id}: invalid Toronto GBFS counts or service flags")
            types = report.get("num_bikes_available_types")
            ebikes = _count(types.get("ebike")) if isinstance(types, dict) else None
            lon, lat = point
            name = station.get("name")
            rows.append({
                "station_id": station_id, "title": name.strip()[:140] if isinstance(name, str) and name.strip() else station_id,
                "latitude": lat, "longitude": lon, "bikes_available": bikes, "docks_available": docks,
                "ebikes_available": ebikes if ebikes is not None and ebikes <= bikes else None,
                "is_renting": int(renting), "is_returning": int(returning),
                "observed_at": observed.isoformat(), "observation_unix": observed.timestamp(),
                "valid_until": min(expires, observed + _BIKE_AGE).isoformat(),
            })
        if len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()) > _MAX_HOTSPOT_BYTES:
            raise DeviceOffline(f"{self.device_id}: Toronto GBFS stations exceed the response budget")
        return signed_cluster_read(
            self, rows, numeric_keys=tuple(self.fields), meta_keys=("station_id", "title", "observed_at", "valid_until"),
            reading_meta={"source_published_at": published.isoformat(), "source_expires_at": expires.isoformat(),
                          "source_read_at": now.isoformat()},
            empty_values={"bikeshare_station_count": 0.0, "source_published_unix": published.timestamp(),
                          "source_expires_unix": expires.timestamp()},
        )
