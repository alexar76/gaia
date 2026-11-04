"""DOT-NL charge point availability and British Columbia wildfire observations."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import urlencode
from zlib import error as ZlibError

import httpx

from gaia.devices._live_base import LiveDevice, _UA, _num
from gaia.devices._policy import _assert_url_allowed
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.live_transport_events import _geometry, _utc
from gaia.source_policy import require_approved_source

_BBox = tuple[float, float, float, float]
_NDW_PILOT: _BBox = (5.1364, 52.082, 5.1729, 52.0976)
_NDW_SNAPSHOT_AGE = timedelta(minutes=4)
_NDW_NATIVE_LIMIT = 1_000
_MAX_NDW_AREA = 4.0
_MAX_PAGE = 500
_MAX_PAGE_BYTES = 215_000  # space for a GAIA envelope under the Hub's 256 KiB budget
_LOCATION_ID = re.compile(r"NL-[A-Za-z0-9_.:*~-]{1,124}")
_PIN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CONNECTOR_CODE = re.compile(r"[A-Z0-9_]{1,40}")
_FIRE_ID = re.compile(r"[A-Z][0-9]{5}")
_BC_BOUNDS: _BBox = (-140.5, 47.5, -113.5, 61.0)
_BC_SNAPSHOT_AGE = timedelta(hours=1)
_BC_LAYERS = "https://services6.arcgis.com/ubm4tcTYICKBpist/arcgis/rest/services/"
_BC_STATUSES = ("Fire of Note", "New", "Out of Control", "Being Held", "Under Control")
_TIMING_FIELDS = {
    "source_published_unix": "s",
    "source_expires_unix": "s",
    "inventory_total": "count",
    "page_offset": "count",
    "next_offset": "count",
    "coverage_complete": "bool",
    "bbox_west_deg": "deg",
    "bbox_south_deg": "deg",
    "bbox_east_deg": "deg",
    "bbox_north_deg": "deg",
}


def _strict_bbox(west: float, south: float, east: float, north: float) -> _BBox:
    values = west, south, east, north
    if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
        raise ValueError("bbox requires four finite numeric coordinates")
    west, south, east, north = (round(v, 4) for v in values)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox must be ordered west, south, east, north within WGS84")
    return west, south, east, north


def _inside(lon: float, lat: float, bbox: _BBox) -> bool:
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north


def _intersects(feature_bbox: list[float], bbox: _BBox) -> bool:
    a, b, c, d = feature_bbox
    west, south, east, north = bbox
    return a <= east and c >= west and b <= north and d >= south


def _text(raw: Any, length: int = 100) -> str | None:
    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split())
    return value[:length] or None


def _timestamp_ms(value: Any) -> datetime | None:
    if type(value) is not int or not 0 < value < 4_102_444_800_000:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _header_time(headers: httpx.Headers, key: str) -> datetime:
    date = parsedate_to_datetime(headers.get(key, ""))
    if date.tzinfo is None:
        raise ValueError(f"unqualified {key} date")
    return date.astimezone(UTC)


def _snapshot_valid(published: datetime, age: timedelta) -> bool:
    now = datetime.now(UTC)
    return now - age < published <= now + timedelta(seconds=5)


def _feature_collection(payload: Any) -> list[dict[str, Any]]:
    features = payload.get("features") if isinstance(payload, dict) and payload.get("type") == "FeatureCollection" else None
    if not isinstance(features, list) or not all(isinstance(row, dict) for row in features):
        raise ValueError("expected a GeoJSON FeatureCollection")
    return features


class _PublicInventory(LiveDevice):
    retain_hotspots_in_history = False
    timeout = 15.0
    meta_keys: tuple[str, ...] = ()
    structured_keys: tuple[str, ...] = ()
    age = timedelta(minutes=10)

    def _body(self, url: str, *, max_bytes: int) -> tuple[bytes, httpx.Headers]:
        require_approved_source(self.policy_id).require_endpoint(url)
        data = bytearray()
        try:
            with httpx.stream(
                "GET", _assert_url_allowed(url),
                headers={"User-Agent": _UA, "Cache-Control": "no-cache"},
                follow_redirects=False, timeout=self.timeout,
            ) as response:
                if response.status_code != 200:
                    raise DeviceOffline(f"{self.device_id}: upstream HTTP {response.status_code}")
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > max_bytes:
                        raise DeviceOffline(f"{self.device_id}: upstream payload exceeds byte limit")
                    data.extend(chunk)
                return bytes(data), response.headers
        except httpx.HTTPError as exc:
            raise DeviceOffline(f"{self.device_id}: upstream {type(exc).__name__}") from exc

    def _json(self, url: str, *, max_bytes: int = 4_000_000) -> tuple[Any, httpx.Headers]:
        body, headers = self._body(url, max_bytes=max_bytes)
        return json.loads(body), headers

    def _page(
        self, rows: list[dict[str, Any]], published: datetime, bbox: _BBox, *,
        offset: int, limit: int, snapshot_unix: float | None, count_key: str,
    ) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= len(rows):
            raise ValueError("offset must be within this snapshot")
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE:
            raise ValueError(f"limit must be between 1 and {_MAX_PAGE}")
        if snapshot_unix is not None and (
            type(snapshot_unix) not in (int, float)
            or round(published.timestamp(), 3) != snapshot_unix
        ):
            raise ValueError("source snapshot changed; restart pagination")
        if offset and snapshot_unix is None:
            raise ValueError("snapshot_unix is required after the first page")
        if not _snapshot_valid(published, self.age):
            raise DeviceOffline(f"{self.device_id}: source publication outside freshness window")
        expires = published + self.age
        page = []
        used_bytes = 0
        for row in rows[offset:offset + limit]:
            row = {**row, "valid_until": expires.isoformat()}
            used_bytes += len(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()) + 1
            if used_bytes > _MAX_PAGE_BYTES:
                if not page:
                    raise DeviceOffline(f"{self.device_id}: one feature exceeds the response budget")
                break
            page.append(row)
        end = offset + len(page)
        next_offset = end if end < len(rows) else None
        scope = {
            "source_published_unix": round(published.timestamp(), 3),
            "source_expires_unix": round(expires.timestamp(), 3),
            count_key: len(rows),
            "inventory_total": len(rows),
            "page_offset": offset,
            "next_offset": next_offset if next_offset is not None else -1,
            "coverage_complete": int(offset == 0 and next_offset is None),
            **dict(zip(("bbox_west_deg", "bbox_south_deg", "bbox_east_deg", "bbox_north_deg"), bbox)),
        }
        meta = {
            **scope,
            "snapshot_unix": scope["source_published_unix"],
            "source_published_at": published.isoformat(),
            "source_expires_at": expires.isoformat(),
            "next_offset": next_offset,
            "coverage_complete": bool(scope["coverage_complete"]),
        }
        return signed_cluster_read(
            self, page, numeric_keys=tuple(self.fields), meta_keys=(*self.meta_keys, "valid_until"),
            structured_keys=self.structured_keys, reading_meta=meta, headline_values=scope,
        )


class NdwEvCharging(_PublicInventory):
    """Bounded availability snapshots from NDW's complete Netherlands GeoJSON."""

    model = "GAIA-EV-CHARGING (NDW DOT-NL)"
    policy_id = "ndw_ev_charging"
    url = "https://opendata.ndw.nu/charging_point_locations.geojson.gz"
    api_url = "https://dotnl.ndw.nu/api/rest/geojson/dynamic-road-status/charge-point-data/v1/features"
    attribution = "NDW DOT-NL · CC0 1.0"
    source = (
        f"{url} (NDW DOT-NL public charging availability, CC0 1.0. "
        "Connector availability is the published operator status. Location change time differs from feed publication.)"
    )
    fields = {
        "latitude": "deg", "longitude": "deg", "connector_count": "count", "available_count": "count",
        "max_power_kw": "kW", "observation_unix": "s", "charging_station_count": "count", **_TIMING_FIELDS,
    }
    meta_keys = ("station_id", "station_key", "provider", "title", "observed_at")
    structured_keys = ("connectors",)
    age = _NDW_SNAPSHOT_AGE

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._snapshot_lock = Lock()
        self._cached_bulk: tuple[datetime, list[dict[str, Any]]] | None = None
        self._bulk_until = 0.0

    def _bulk(self) -> tuple[datetime, list[dict[str, Any]]]:
        with self._snapshot_lock:
            if self._cached_bulk and monotonic() < self._bulk_until:
                return self._cached_bulk
            raw, headers = self._body(self.url, max_bytes=8_000_000)
            published = _header_time(headers, "Last-Modified")
            if not _snapshot_valid(published, self.age):
                raise DeviceOffline(f"{self.device_id}: stale NDW bulk publication")
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source:
                data = source.read(64_000_001)
            if len(data) > 64_000_000:
                raise DeviceOffline(f"{self.device_id}: NDW bulk exceeds uncompressed byte limit")
            rows = self._normalize(_feature_collection(json.loads(data)), published)
            self._cached_bulk = published, rows
            self._bulk_until = monotonic() + 35.0
            return self._cached_bulk

    def _native_pilot(self) -> tuple[datetime, list[dict[str, Any]]] | None:
        url = f"{self.api_url}?bbox=" + ",".join(str(v) for v in _NDW_PILOT)
        payload, headers = self._json(url, max_bytes=800_000)
        rows = _feature_collection(payload)
        if headers.get("Link") or len(rows) >= _NDW_NATIVE_LIMIT:
            return None
        total = headers.get("X-Total-Count", "")
        if not total.isdecimal() or int(total) != len(rows):
            return None
        published = _header_time(headers, "Date")
        if headers.get("Age", "0") != "0" or not _snapshot_valid(published, self.age):
            return None
        return published, self._normalize(rows, published)

    def _normalize(self, features: list[dict[str, Any]], published: datetime) -> list[dict[str, Any]]:
        if len(features) > 120_000:
            raise ValueError("NDW feature count exceeds supported inventory")
        rows: dict[str, dict[str, Any]] = {}
        for feature in features:
            properties = feature.get("properties")
            station_id = feature.get("id")
            geometry = feature.get("geometry")
            if (not isinstance(station_id, str) or not _LOCATION_ID.fullmatch(station_id)
                    or not isinstance(properties, dict) or properties.get("country") != "NLD"
                    or not isinstance(geometry, dict) or geometry.get("type") != "Point"):
                continue
            coords = geometry.get("coordinates")
            if not isinstance(coords, list) or len(coords) != 2:
                continue
            lon, lat = map(_num, coords)
            if lon is None or lat is None or not _inside(lon, lat, (2.5, 50.5, 8.0, 54.0)):
                continue
            observed = _utc(properties.get("last_updated"))
            if observed is None or observed > published + timedelta(minutes=1):
                continue
            available = properties.get("availabilities")
            if not isinstance(available, list) or not 0 < len(available) <= 64:
                continue
            connectors = []
            for item in available:
                if not isinstance(item, dict):
                    break
                code, fmt, power_type = (item.get(key) for key in ("connector_type", "connector_format", "power_type"))
                total, free = item.get("total"), item.get("available")
                power = _num(item.get("power_max"))
                if (not all(isinstance(v, str) and _CONNECTOR_CODE.fullmatch(v) for v in (code, fmt, power_type))
                        or type(total) is not int or type(free) is not int or not 0 <= free <= total <= 2000
                        or power is None or not 0 <= power <= 5_000_000):
                    break
                connectors.append({"type": code, "format": fmt, "power_type": power_type, "total": total,
                                   "available": free, "power_kw": round(power / 1000, 3)})
            if len(connectors) != len(available):
                continue
            provider = _text(properties.get("operator_name")) or _text(properties.get("cpo_id"), 24)
            row = {
                "station_id": station_id, "title": _text(properties.get("address")) or "Public charging location",
                "station_key": station_id if _PIN_ID.fullmatch(station_id) else hashlib.sha256(station_id.encode()).hexdigest()[:32],
                "provider": provider, "latitude": lat, "longitude": lon,
                "connector_count": sum(c["total"] for c in connectors),
                "available_count": sum(c["available"] for c in connectors),
                "max_power_kw": max(c["power_kw"] for c in connectors),
                "observation_unix": observed.timestamp(), "observed_at": observed.isoformat(),
                "connectors": connectors,
            }
            previous = rows.get(station_id)
            if previous is None or previous["observation_unix"] < row["observation_unix"]:
                rows[station_id] = row
            elif previous["observation_unix"] == row["observation_unix"] and previous != row:
                raise ValueError("conflicting NDW records at the same location version")
        if features and not rows:
            raise ValueError("NDW feature collection has no usable availability schema")
        return list(rows.values())

    def read(self) -> dict[str, Any]:
        try:
            pilot = self._native_pilot()
        except (DeviceOffline, ValueError, RecursionError):
            pilot = None
        if pilot is None:
            return self.read_bbox(*_NDW_PILOT)
        published, rows = pilot
        return self._serve(rows, published, _NDW_PILOT, offset=0, limit=_MAX_PAGE, snapshot_unix=None)

    def _serve(
        self, rows: list[dict[str, Any]], published: datetime, bbox: _BBox, *,
        offset: int, limit: int, snapshot_unix: float | None,
    ) -> dict[str, Any]:
        scoped = [row for row in rows if _inside(row["longitude"], row["latitude"], bbox)]
        scoped.sort(key=lambda row: row["station_id"])
        return self._page(scoped, published, bbox, offset=offset, limit=limit,
                          snapshot_unix=snapshot_unix, count_key="charging_station_count")

    def read_bbox(
        self, west: float, south: float, east: float, north: float, *,
        offset: int = 0, limit: int = _MAX_PAGE, snapshot_unix: float | None = None,
    ) -> dict[str, Any]:
        bbox = _strict_bbox(west, south, east, north)
        if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > _MAX_NDW_AREA:
            raise ValueError("NDW bbox is limited to 4 square degrees")
        try:
            published, rows = self._bulk()
        except (ValueError, EOFError, OSError, RecursionError, ZlibError) as exc:
            raise DeviceOffline(f"{self.device_id}: invalid NDW snapshot") from exc
        return self._serve(rows, published, bbox, offset=offset, limit=limit, snapshot_unix=snapshot_unix)


class BcWildfireService(_PublicInventory):
    """Active BC incidents, joined to the most recent published perimeter when present."""

    model = "GAIA-WILDFIRE-PERIMETERS (BC Wildfire Service)"
    policy_id = "bc_wildfire"
    url = _BC_LAYERS + "BCWS_ActiveFires_PublicView/FeatureServer/0"
    perimeter_url = _BC_LAYERS + "BCWS_FirePerimeters_PublicView/FeatureServer/0"
    attribution = "Contains information licensed under the Open Government Licence – British Columbia."
    source = (
        f"{url} and {perimeter_url} (BC Wildfire Service. {attribution} "
        "Active incident status and the last published perimeter geometry, generalized by the provider for display.)"
    )
    fields = {
        "latitude": "deg", "longitude": "deg", "area_ha": "ha", "perimeter_area_ha": "ha",
        "ignition_unix": "s", "perimeter_observation_unix": "s", "perimeter_published_unix": "s",
        "wildfire_incident_count": "count", **_TIMING_FIELDS,
    }
    meta_keys = ("event_id", "title", "status", "ignition_at", "perimeter_observed_at", "claim_class")
    structured_keys = ("geometry", "event_bbox")
    age = _BC_SNAPSHOT_AGE

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._snapshot_lock = Lock()
        self._cached_snapshot: tuple[datetime, list[dict[str, Any]]] | None = None
        self._snapshot_until = 0.0

    def _layer_publication(self, layer: str) -> datetime:
        payload, _ = self._json(layer + "?f=json", max_bytes=1_000_000)
        edits = payload.get("editingInfo") if isinstance(payload, dict) else None
        published = _timestamp_ms(edits.get("dataLastEditDate")) if isinstance(edits, dict) else None
        if published is None or not _snapshot_valid(published, self.age):
            raise DeviceOffline(f"{self.device_id}: missing or stale BC source publication")
        return published

    def _layer_features(self, layer: str, *, where: str, fields: str, perimeters: bool) -> list[dict[str, Any]]:
        params = {
            "where": where, "outFields": fields, "f": "geojson", "outSR": "4326",
            "orderByFields": "OBJECTID ASC", "resultRecordCount": "200",
        }
        if perimeters:
            params.update(maxAllowableOffset="0.00025", geometryPrecision="5")
        rows = []
        object_ids: set[int] = set()
        for offset in range(0, 2_000, 200):
            params["resultOffset"] = str(offset)
            payload, _ = self._json(f"{layer}/query?" + urlencode(params))
            features = _feature_collection(payload)
            for feature in features:
                props = feature.get("properties")
                oid = props.get("OBJECTID") if isinstance(props, dict) else None
                if type(oid) is not int or oid in object_ids:
                    raise ValueError("invalid or duplicate ArcGIS object ID")
                object_ids.add(oid)
            rows.extend(features)
            props = payload.get("properties")
            exceeded = payload.get("exceededTransferLimit") or (
                isinstance(props, dict) and props.get("exceededTransferLimit")
            )
            if not exceeded and len(features) < 200:
                return rows
            if not features:
                raise ValueError("empty ArcGIS page before transfer completed")
        raise DeviceOffline(f"{self.device_id}: BC query exceeds 2,000 active source records")

    def _snapshot(self) -> tuple[datetime, list[dict[str, Any]]]:
        with self._snapshot_lock:
            if self._cached_snapshot and monotonic() < self._snapshot_until:
                return self._cached_snapshot
            for _ in range(2):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    p = pool.submit(self._read_layer, False)
                    g = pool.submit(self._read_layer, True)
                    point_stamp, points = p.result()
                    perimeter_stamp, perimeters = g.result()
                if point_stamp is not None and perimeter_stamp is not None:
                    rows = self._join(points, perimeters, point_stamp, perimeter_stamp)
                    self._cached_snapshot = min(point_stamp, perimeter_stamp), rows
                    self._snapshot_until = monotonic() + 45.0
                    return self._cached_snapshot
        raise DeviceOffline(f"{self.device_id}: BC layer changed while paging")

    def _read_layer(self, perimeters: bool) -> tuple[datetime | None, list[dict[str, Any]]]:
        layer = self.perimeter_url if perimeters else self.url
        fields = (
            "OBJECTID,FIRE_NUMBER,FIRE_YEAR,VERSION_NUMBER,TRACK_DATE,FIRE_SIZE_HECTARES"
            if perimeters else "OBJECTID,FIRE_NUMBER,FIRE_YEAR,IGNITION_DATE,FIRE_STATUS,FIRE_OUT_DATE,CURRENT_SIZE,INCIDENT_NAME"
        )
        published = self._layer_publication(layer)
        statuses = ",".join(f"'{status}'" for status in _BC_STATUSES)
        where = f"FIRE_STATUS IN ({statuses})"
        if not perimeters:
            where += " AND FIRE_OUT_DATE IS NULL"
        rows = self._layer_features(layer, where=where, fields=fields, perimeters=perimeters)
        return (published if published == self._layer_publication(layer) else None), rows

    def _join(
        self, points: list[dict[str, Any]], perimeters: list[dict[str, Any]],
        point_stamp: datetime, perimeter_stamp: datetime,
    ) -> list[dict[str, Any]]:
        polygons: dict[tuple[int, str], tuple[datetime, int, float | None, dict[str, Any]]] = {}
        for item in perimeters:
            props = item["properties"]
            fire_id, year, version = (props.get(key) for key in ("FIRE_NUMBER", "FIRE_YEAR", "VERSION_NUMBER"))
            tracked = _timestamp_ms(props.get("TRACK_DATE"))
            if (not isinstance(fire_id, str) or not _FIRE_ID.fullmatch(fire_id)
                    or type(year) is not int or type(version) is not int
                    or tracked is None or tracked > perimeter_stamp):
                continue
            raw = item.get("geometry")
            if not isinstance(raw, dict) or raw.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            shape = _geometry(raw)
            if shape is None or not _inside(shape["longitude"], shape["latitude"], _BC_BOUNDS):
                continue
            key = year, fire_id
            prior = polygons.get(key)
            if prior is None or prior[:2] < (tracked, version):
                area = _num(props.get("FIRE_SIZE_HECTARES"))
                polygons[key] = tracked, version, area if area is not None and 0 <= area <= 2_000_000 else None, shape
            elif prior[:2] == (tracked, version) and prior[3] != shape:
                raise ValueError("conflicting BC perimeter geometry versions")
        rows = []
        seen = set()
        for item in points:
            props = item["properties"]
            fire_id, year, status = (props.get(key) for key in ("FIRE_NUMBER", "FIRE_YEAR", "FIRE_STATUS"))
            ignition = _timestamp_ms(props.get("IGNITION_DATE"))
            if (not isinstance(fire_id, str) or not _FIRE_ID.fullmatch(fire_id) or type(year) is not int
                    or not 2000 <= year <= point_stamp.year or status not in _BC_STATUSES
                    or props.get("FIRE_OUT_DATE") is not None or ignition is None or ignition > point_stamp):
                continue
            raw = item.get("geometry")
            if not isinstance(raw, dict) or raw.get("type") != "Point":
                continue
            anchor = _geometry(raw)
            if anchor is None or not _inside(anchor["longitude"], anchor["latitude"], _BC_BOUNDS):
                continue
            key = year, fire_id
            if key in seen:
                raise ValueError("duplicate BC active wildfire identity")
            seen.add(key)
            area = _num(props.get("CURRENT_SIZE"))
            row = {
                **anchor, "event_id": f"{year}-{fire_id}", "title": _text(props.get("INCIDENT_NAME")) or fire_id,
                "status": status, "ignition_at": ignition.isoformat(), "ignition_unix": ignition.timestamp(),
                "area_ha": area if area is not None and 0 <= area <= 2_000_000 else None,
                "claim_class": "published_wildfire_incident",
            }
            if key in polygons:
                tracked, _, area, shape = polygons[key]
                row.update(geometry=shape["geometry"], event_bbox=shape["event_bbox"],
                           perimeter_observed_at=tracked.isoformat(), perimeter_observation_unix=tracked.timestamp(),
                           perimeter_published_unix=perimeter_stamp.timestamp(), perimeter_area_ha=area)
            rows.append(row)
        rows.sort(key=lambda row: row["event_id"])
        return rows

    def read(self) -> dict[str, Any]:
        return self.read_bbox(*_BC_BOUNDS)

    def read_bbox(
        self, west: float, south: float, east: float, north: float, *,
        offset: int = 0, limit: int = _MAX_PAGE, snapshot_unix: float | None = None,
    ) -> dict[str, Any]:
        bbox = _strict_bbox(west, south, east, north)
        try:
            published, rows = self._snapshot()
        except (ValueError, RecursionError) as exc:
            raise DeviceOffline(f"{self.device_id}: invalid BC wildfire snapshot") from exc
        scoped = [row for row in rows if _intersects(row["event_bbox"], bbox)
                  or _inside(row["longitude"], row["latitude"], bbox)]
        return self._page(scoped, published, bbox, offset=offset, limit=limit,
                          snapshot_unix=snapshot_unix, count_key="wildfire_incident_count")
