"""Operational environmental layers added after the P3 relay set."""

from __future__ import annotations

import html
import csv
import gzip
import hashlib
from io import BytesIO
import json
import re
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.p4_networks import DART_STATIONS, RADNET_STATIONS
from gaia.devices._policy import _assert_url_allowed
from gaia.devices._policy import _om_apikey_suffix, _om_auth_headers, _om_origin, _om_source
from gaia.source_policy import require_approved_source


_HMS_FIELD = re.compile(r"(?:^|<br\s*/?>)\s*([^:<]+):\s*([^<]+)", re.I)
_HMS_SCORE = {"light": 30.0, "medium": 60.0, "heavy": 90.0}


class CoordinateQueryable:
    """Temporary paid-invoke coordinate override guarded by the capability handler."""

    query_gate: threading.Lock
    latitude: float
    longitude: float

    def _coordinate_ok(self, latitude: float, longitude: float) -> bool:
        return -90 <= latitude <= 90 and -180 <= longitude <= 180

    def _init_coordinate(self, latitude: float, longitude: float) -> None:
        if not self._coordinate_ok(float(latitude), float(longitude)):
            raise ValueError("coordinate out of source range")
        self.latitude, self.longitude = float(latitude), float(longitude)
        self._default_coordinate = (self.latitude, self.longitude)
        self.query_gate = threading.Lock()
        self._coordinate_changed()

    def set_coordinate(self, latitude: float, longitude: float) -> None:
        if not self._coordinate_ok(float(latitude), float(longitude)):
            raise ValueError("coordinate out of source range")
        self.latitude, self.longitude = float(latitude), float(longitude)
        self._coordinate_changed()

    def clear_coordinate(self) -> None:
        self.latitude, self.longitude = self._default_coordinate
        self._coordinate_changed()

    def _coordinate_changed(self) -> None:
        pass


def _hms_ring(boundary: ET.Element) -> list[list[float]]:
    coord_text = next(
        (el.text or "" for el in boundary.iter() if el.tag.endswith("coordinates")),
        "",
    )
    ring: list[list[float]] = []
    for token in coord_text.split():
        bits = token.split(",")
        if len(bits) < 2:
            continue
        lon, lat = _num(bits[0]), _num(bits[1])
        if lat is None or lon is None or not -90 <= lat <= 90:
            continue
        wrapped_lon = ((float(lon) + 180.0) % 360.0) - 180.0
        point = [round(wrapped_lon, 6), round(float(lat), 6)]
        if not ring or point != ring[-1]:
            ring.append(point)
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring if len(ring) >= 4 else []


def _hms_bbox(ring: list[list[float]]) -> list[float]:
    """RFC-7946-ish bbox; west > east denotes an antimeridian crossing."""
    lats = [point[1] for point in ring]
    lons = sorted(set(point[0] for point in ring))
    if len(lons) < 2:
        return [lons[0], min(lats), lons[0], max(lats)]
    circular = lons + [lons[0] + 360.0]
    gap_i = max(range(len(lons)), key=lambda i: circular[i + 1] - circular[i])
    west = circular[gap_i + 1]
    east = circular[gap_i]
    if west > 180:
        west -= 360
    return [round(west, 6), min(lats), round(east, 6), max(lats)]


def parse_hms_smoke_kml(text: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Parse every NOAA HMS Polygon, preserving outer rings and holes.

    The centroid is retained only as the one-coordinate ATLAS display anchor.
    Commercial containment uses the signed ``geometry`` member.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("invalid HMS KML") from exc
    rows: list[dict[str, Any]] = []
    for placemark in root.iter():
        if not placemark.tag.endswith("Placemark"):
            continue
        desc = next((el.text or "" for el in placemark if el.tag.endswith("description")), "")
        desc = re.sub(r"^\s*<div[^>]*>", "", desc, flags=re.I)
        fields = {
            key.strip().lower(): html.unescape(value).strip()
            for key, value in _HMS_FIELD.findall(desc)
        }
        density = fields.get("density", "").lower()
        score = _HMS_SCORE.get(density)
        if score is None:
            continue
        for polygon in (el for el in placemark.iter() if el.tag.endswith("Polygon")):
            outers = [el for el in polygon if el.tag.endswith("outerBoundaryIs")]
            if not outers:
                continue
            outer = _hms_ring(outers[0])
            if not outer:
                continue
            holes = [
                ring for boundary in polygon
                if boundary.tag.endswith("innerBoundaryIs")
                for ring in [_hms_ring(boundary)] if ring
            ]
            geometry = {"type": "Polygon", "coordinates": [outer, *holes]}
            canonical = json.dumps(
                geometry, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            anchor = outer[:-1] if outer[0] == outer[-1] else outer
            # A mean-vertex centre is only the single map coordinate. It is not
            # used for coverage and is not an in-situ PM measurement.
            lat = sum(point[1] for point in anchor) / len(anchor)
            lon = sum(point[0] for point in anchor) / len(anchor)
            identity = "|".join((
                digest, density, fields.get("start time", ""), fields.get("end time", "")
            ))
            rows.append({
                "severity_score": score,
                "latitude": lat,
                "longitude": lon,
                "density": density,
                "satellite": fields.get("satellite", ""),
                "start_time": fields.get("start time", ""),
                "end_time": fields.get("end time", ""),
                "geometry_type": "Polygon centroid",
                "polygon_id": f"hms-{hashlib.sha256(identity.encode()).hexdigest()[:20]}",
                "geometry_digest": digest,
                "vertex_count": sum(len(ring) for ring in geometry["coordinates"]),
                "bbox": _hms_bbox(outer),
                "geometry": geometry,
            })
    rows.sort(key=lambda row: float(row["severity_score"]), reverse=True)
    if limit is None:
        return rows
    return rows[: max(1, min(int(limit), 10_000))]


class NoaaHmsSmoke(LiveDevice):
    """NOAA HMS qualitative smoke polygons over North America."""

    model = "GAIA-SMOKE (NOAA HMS)"
    policy_id = "noaa_hms_smoke"
    fields = {"severity_score": "score", "latitude": "deg", "longitude": "deg"}
    source = (
        "https://www.ospo.noaa.gov/Products/land/hms.html "
        "(NOAA/NESDIS Hazard Mapping System smoke polygons; U.S. Government "
        "public domain. Qualitative light/medium/heavy analysis, not PM2.5.)"
    )
    attribution = "NOAA/NESDIS Hazard Mapping System (HMS)"
    _base = "https://www.ospo.noaa.gov/data/spl/kmlfiles/fire/hms_smoke"
    # HMS publishes one KML per UTC day and only writes it once the first daytime
    # analysis exists. Asking for today alone left the layer — and the paid
    # containment SKU on top of it — dark for most of every UTC day: at 06:00Z the
    # current day's file 404s while the previous day's is complete. Fall back one
    # dated product at a time and carry the analysis date so nothing downstream can
    # call yesterday's analysis "current".
    _MAX_LOOKBACK_DAYS = 2

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        self.url = self._url_for(datetime.now(timezone.utc))
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.product_date: str | None = None

    @classmethod
    def _url_for(cls, when: datetime) -> str:
        return f"{cls._base}{when:%Y%m%d}.kml"

    def collect_hotspots(self, text: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = parse_hms_smoke_kml(text, limit=limit)
        if not rows:
            raise DeviceOffline(f"{self.device_id}: HMS has no smoke polygons yet")
        return rows

    def _fetch_dated_product(self) -> tuple[list[dict[str, Any]], datetime]:
        """Newest dated HMS product that actually parses, within the lookback window."""
        now = datetime.now(timezone.utc)
        policy = require_approved_source(self.policy_id)
        last: DeviceOffline | None = None
        for offset in range(self._MAX_LOOKBACK_DAYS + 1):
            when = now - timedelta(days=offset)
            url = self._url_for(when)
            policy.require_endpoint(url)
            try:
                text = self._fetch_text(url, max_chars=8_000_000)
                hotspots = self.collect_hotspots(text)
            except DeviceOffline as exc:
                last = exc
                continue
            except ValueError as exc:  # invalid KML for that day
                last = DeviceOffline(f"{self.device_id}: {exc}")
                continue
            self.url = url
            return hotspots, when
        raise last or DeviceOffline(
            f"{self.device_id}: no HMS smoke product in the last "
            f"{self._MAX_LOOKBACK_DAYS + 1} UTC day(s)"
        )

    def read(self) -> dict[str, Any]:
        hotspots, when = self._fetch_dated_product()
        product_date = f"{when:%Y-%m-%d}"
        self.product_date = product_date
        age_hours = round(
            (datetime.now(timezone.utc) - when.replace(
                hour=0, minute=0, second=0, microsecond=0
            )).total_seconds() / 3600.0,
            2,
        )
        for row in hotspots:
            # Per-polygon so the date survives ATLAS's hotspot fan-out to map pins
            # and reaches the containment product with the geometry it describes.
            row["product_date"] = product_date
            row["product_age_hours"] = age_hours
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("severity_score", "latitude", "longitude"),
            meta_keys=(
                "density", "satellite", "start_time", "end_time", "geometry_type",
                "polygon_id", "geometry_digest", "vertex_count",
                "product_date", "product_age_hours",
            ),
            structured_keys=("bbox", "geometry"),
            reading_meta={
                "inventory_total": len(hotspots),
                "inventory_complete": True,
                "product_date": product_date,
                "product_age_hours": age_hours,
            },
        )


class UsgsWaterQuality(LiveDevice):
    """Latest USGS water quality for one station or every station in a bbox."""

    model = "GAIA-WQ (USGS continuous)"
    policy_id = "usgs_water_quality"
    fields = {
        "water_temperature_c": "cel",
        "ph": "pH",
        "dissolved_oxygen_mg_l": "mg/L",
        "specific_conductance_us_cm": "uS/cm",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous "
        "(USGS Water Data for the Nation automated continuous observations; "
        "U.S. Government public domain. Values may be provisional.)"
    )
    attribution = "U.S. Geological Survey Water Data for the Nation"
    _field_by_code = {
        "00010": "water_temperature_c",
        "00400": "ph",
        "00300": "dissolved_oxygen_mg_l",
        "00095": "specific_conductance_us_cm",
    }
    _code_by_field = {field: code for code, field in _field_by_code.items()}

    def __init__(self, device_id: str, clock: SimClock, *, station: str = "01480065", **kw):
        if not re.fullmatch(r"[0-9]{8,15}", station):
            raise ValueError(f"invalid USGS station id: {station!r}")
        super().__init__(device_id, clock, **kw)
        self.station = station
        self.query_gate = threading.Lock()
        self._query_bbox: tuple[float, float, float, float] | None = None
        self._query_limit = 10_000
        self._query_codes = tuple(self._field_by_code)
        self._query_require_all = False
        self._query_max_age_hours = 48.0
        self.url = (
            "https://api.waterdata.usgs.gov/ogcapi/v0/collections/"
            f"latest-continuous/items?f=json&monitoring_location_id=USGS-{station}&limit=100"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    @classmethod
    def resolve_parameter_codes(cls, requested: Any) -> tuple[str, ...]:
        if requested is None:
            return tuple(cls._field_by_code)
        if not isinstance(requested, list) or not requested:
            raise ValueError("parameters must be a non-empty array")
        codes: list[str] = []
        for raw in requested:
            token = str(raw or "").strip()
            code = token if token in cls._field_by_code else cls._code_by_field.get(token)
            if code is None:
                raise ValueError(f"unsupported water-quality parameter: {token}")
            if code not in codes:
                codes.append(code)
        return tuple(codes)

    def set_query(
        self,
        *,
        bbox: tuple[float, float, float, float],
        limit: int = 10_000,
        parameters: Any = None,
        require_all: bool = False,
        max_age_hours: float = 48.0,
    ) -> None:
        west, south, east, north = bbox
        if not (-180 <= west <= 180 and -180 <= east <= 180 and
                -90 <= south < north <= 90):
            raise ValueError("invalid water-quality bbox")
        self._query_bbox = (float(west), float(south), float(east), float(north))
        self._query_limit = max(1, min(int(limit), 10_000))
        self._query_codes = self.resolve_parameter_codes(parameters)
        self._query_require_all = bool(require_all)
        self._query_max_age_hours = max(1.0, min(float(max_age_hours), 720.0))

    def clear_query(self) -> None:
        self._query_bbox = None
        self._query_limit = 10_000
        self._query_codes = tuple(self._field_by_code)
        self._query_require_all = False
        self._query_max_age_hours = 48.0

    def _observation_is_fresh(self, observed: str) -> bool:
        try:
            parsed = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        age_hours = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600
        return -1.0 <= age_hours <= self._query_max_age_hours

    def _ogc_features(self, first_url: str) -> list[dict[str, Any]]:
        """Drain an OGC feature collection; never sell a silently truncated registry."""
        features: list[dict[str, Any]] = []
        url = first_url
        visited: set[str] = set()
        for _page in range(100):
            if url in visited:
                raise DeviceOffline(f"{self.device_id}: USGS pagination loop")
            visited.add(url)
            require_approved_source(self.policy_id).require_endpoint(url)
            payload = self._fetch(url)
            page = payload.get("features") if isinstance(payload, dict) else None
            if not isinstance(page, list):
                raise DeviceOffline(f"{self.device_id}: invalid USGS feature page")
            features.extend(feature for feature in page if isinstance(feature, dict))
            next_url = ""
            for link in payload.get("links") or []:
                if isinstance(link, dict) and str(link.get("rel") or "").lower() == "next":
                    next_url = str(link.get("href") or "")
                    break
            if not next_url:
                matched_raw = payload.get("numberMatched")
                try:
                    matched = int(matched_raw) if matched_raw is not None else None
                except (TypeError, ValueError):
                    matched = None
                if matched is not None and len(features) < matched:
                    raise DeviceOffline(
                        f"{self.device_id}: USGS registry truncated ({len(features)}/{matched})"
                    )
                # OGC numberMatched may legally be null when counting is costly.
                # A full page without a next link is then ambiguous, so fail closed.
                limit_match = re.search(r"(?:[?&])limit=(\d+)", url)
                page_limit = int(limit_match.group(1)) if limit_match else None
                if matched is None and page_limit is not None and len(page) >= page_limit:
                    raise DeviceOffline(
                        f"{self.device_id}: USGS registry completeness is unprovable"
                    )
                return features
            url = urljoin(url, next_url)
        raise DeviceOffline(f"{self.device_id}: USGS registry exceeded pagination safety bound")

    def _network_rows(self) -> list[dict[str, Any]]:
        if self._query_bbox is None:
            return []
        west, south, east, north = self._query_bbox
        bounds = (
            ((west, south, east, north),)
            if west <= east
            else ((west, south, 180.0, north), (-180.0, south, east, north))
        )
        by_station: dict[str, dict[str, Any]] = {}
        for code in self._query_codes:
            field = self._field_by_code[code]
            for query_west, query_south, query_east, query_north in bounds:
                bbox_text = (
                    f"{query_west:.6f},{query_south:.6f},"
                    f"{query_east:.6f},{query_north:.6f}"
                )
                url = (
                    "https://api.waterdata.usgs.gov/ogcapi/v0/collections/"
                    "latest-continuous/items?f=json"
                    f"&bbox={bbox_text}&parameter_code={code}&limit={self._query_limit}"
                )
                for feature in self._ogc_features(url):
                    if not isinstance(feature, dict):
                        continue
                    props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
                    value = _num(props.get("value"))
                    observed = str(props.get("time") or "")
                    station_id = str(props.get("monitoring_location_id") or "").removeprefix("USGS-")
                    geom = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
                    coords = geom.get("coordinates") if isinstance(geom, dict) else None
                    if (value is None or not self._observation_is_fresh(observed)
                            or not station_id or not isinstance(coords, list) or len(coords) < 2):
                        continue
                    lon, lat = _num(coords[0]), _num(coords[1])
                    if lat is None or lon is None:
                        continue
                    row = by_station.setdefault(station_id, {
                        "station_id": station_id,
                        "name": str(props.get("monitoring_location_name") or station_id),
                        "latitude": float(lat), "longitude": float(lon),
                        "observed_at": observed,
                        "site_type": str(props.get("site_type") or ""),
                        "state_name": str(props.get("state_name") or ""),
                        "hydrologic_unit_code": str(props.get("hydrologic_unit_code") or ""),
                        "observation_metadata": {},
                    })
                    previous_time = str(row.get(f"{field}_observed_at") or "")
                    if observed >= previous_time:
                        approval_status = str(props.get("approval_status") or "").strip().title()
                        qualifier = str(props.get("qualifier") or "").strip()
                        row[field] = float(value)
                        row[f"{field}_observed_at"] = observed
                        row["observation_metadata"][field] = {
                            "parameter_code": code,
                            "observed_at": observed,
                            "approval_status": approval_status,
                            "qualifier": qualifier,
                            "unit_of_measure": str(props.get("unit_of_measure") or ""),
                            "time_series_id": str(props.get("time_series_id") or ""),
                        }
                    if observed > str(row.get("observed_at") or ""):
                        row["observed_at"] = observed
        rows = []
        required_fields = {self._field_by_code[code] for code in self._query_codes}
        for row in by_station.values():
            available = sorted(required_fields.intersection(row))
            if not available or (self._query_require_all and set(available) != required_fields):
                continue
            metadata = row.get("observation_metadata") or {}
            statuses = {
                str(item.get("approval_status") or "")
                for item in metadata.values() if isinstance(item, dict)
            }
            qualifiers = sorted({
                str(item.get("qualifier") or "")
                for item in metadata.values()
                if isinstance(item, dict) and str(item.get("qualifier") or "")
            })
            row["available_parameters"] = available
            row["parameter_codes"] = [self._code_by_field[field] for field in available]
            row["approval_status"] = (
                "Provisional" if "Provisional" in statuses
                else "Approved" if statuses == {"Approved"}
                else "Unknown"
            )
            row["qualifiers"] = qualifiers
            row["qualifier"] = "; ".join(qualifiers)
            rows.append(row)
        registry = self._station_registry([str(row["station_id"]) for row in rows])
        missing = sorted(str(row["station_id"]) for row in rows if str(row["station_id"]) not in registry)
        if missing:
            preview = ",".join(missing[:5])
            raise DeviceOffline(
                f"{self.device_id}: USGS station registry missing {len(missing)} active site(s): {preview}"
            )
        for row in rows:
            meta = registry[str(row["station_id"])]
            row.update(meta)
        rows.sort(key=lambda row: str(row["station_id"]))
        return rows

    def _station_registry(self, station_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-join active readings to the official monitoring-locations registry."""
        unique = sorted(set(station_ids))
        if not unique:
            return {}
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", station_id) for station_id in unique):
            raise DeviceOffline(f"{self.device_id}: invalid USGS registry station id")
        registry: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(unique), 100):
            batch = unique[offset:offset + 100]
            cql = "id IN (" + ",".join(f"'USGS-{station_id}'" for station_id in batch) + ")"
            url = (
                "https://api.waterdata.usgs.gov/ogcapi/v0/collections/"
                "monitoring-locations/items?f=json&filter-lang=cql2-text"
                f"&filter={quote(cql, safe='')}&limit=1000"
            )
            for feature in self._ogc_features(url):
                props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
                registry_id = str(feature.get("id") or props.get("id") or "")
                station_id = registry_id.removeprefix("USGS-")
                if station_id not in batch:
                    continue
                geom = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
                coords = geom.get("coordinates") if isinstance(geom, dict) else None
                coordinates: dict[str, float] = {}
                if isinstance(coords, list) and len(coords) >= 2:
                    lon, lat = _num(coords[0]), _num(coords[1])
                    if lon is not None and lat is not None:
                        coordinates = {"latitude": float(lat), "longitude": float(lon)}
                registry[station_id] = {
                    "registry_id": registry_id,
                    "name": str(props.get("monitoring_location_name") or station_id),
                    "agency_code": str(props.get("agency_code") or "USGS"),
                    "site_type": str(props.get("site_type") or ""),
                    "state_name": str(props.get("state_name") or ""),
                    "county_name": str(props.get("county_name") or ""),
                    "country_name": str(props.get("country_name") or ""),
                    "hydrologic_unit_code": str(props.get("hydrologic_unit_code") or ""),
                    "registry_revision_modified": str(props.get("revision_modified") or ""),
                    **coordinates,
                }
        return registry

    def map(self, payload: Any) -> dict[str, float | None]:
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            return {}
        # A station can have retired and current time series for the same pcode.
        # Select by observation time, never by API response order.
        latest: dict[str, tuple[str, float]] = {}
        lat = lon = None
        for feature in features:
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
            code = str(props.get("parameter_code") or "")
            field = self._field_by_code.get(code)
            value = _num(props.get("value"))
            observed = str(props.get("time") or "")
            if field and value is not None and observed >= latest.get(field, ("", 0.0))[0]:
                latest[field] = (observed, float(value))
            geom = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
            coords = geom.get("coordinates") if isinstance(geom, dict) else None
            if isinstance(coords, list) and len(coords) >= 2:
                lon, lat = _num(coords[0]), _num(coords[1])
        out: dict[str, float | None] = {field: row[1] for field, row in latest.items()}
        out.update({"latitude": lat, "longitude": lon})
        return out

    def read(self) -> dict[str, Any]:
        if self._query_bbox is None:
            return super().read()
        rows = self._network_rows()
        if not rows:
            raise DeviceOffline(f"{self.device_id}: no USGS water-quality stations in bbox")
        return signed_cluster_read(
            self, rows,
            numeric_keys=(
                "water_temperature_c", "ph", "dissolved_oxygen_mg_l",
                "specific_conductance_us_cm", "latitude", "longitude",
            ),
            meta_keys=(
                "station_id", "name", "observed_at", "approval_status", "qualifier",
                "registry_id", "agency_code", "site_type", "state_name", "county_name",
                "country_name", "hydrologic_unit_code", "registry_revision_modified",
            ),
            structured_keys=(
                "available_parameters", "parameter_codes", "qualifiers", "observation_metadata",
            ),
            reading_meta={
                "inventory_total": len(rows),
                "inventory_complete": True,
                "registry_source": "USGS latest-continuous OGC",
                "parameter_codes": ",".join(self._query_codes),
                "require_all_parameters": self._query_require_all,
                "max_observation_age_hours": self._query_max_age_hours,
            },
        )


def parse_ndbc_dart(text: str) -> dict[str, float | None]:
    """Return newest valid row from NDBC realtime ``.dart`` text."""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 8:
            continue
        height = _num(parts[7])
        kind = _num(parts[6])
        if height is None or kind not in (1.0, 2.0, 3.0) or height >= 9999:
            continue
        return {"water_column_height_m": float(height), "measurement_type": float(kind)}
    return {}


class NoaaDartGauge(LiveDevice):
    """NOAA NDBC DART deep-ocean water-column height gauge."""

    model = "GAIA-DART (NOAA NDBC)"
    policy_id = "noaa_dart"
    fields = {"water_column_height_m": "m", "measurement_type": "code"}
    source = (
        "https://www.ndbc.noaa.gov/data/realtime2/ (NOAA/NDBC DART real-time "
        "water-column height; U.S. Government public domain. Gross-error checked "
        "only; this is a gauge, not a tsunami warning.)"
    )
    attribution = "NOAA National Data Buoy Center DART®"

    def __init__(self, device_id: str, clock: SimClock, *, station: str = "46407", **kw):
        if not re.fullmatch(r"[0-9]{5}", station):
            raise ValueError(f"invalid DART station id: {station!r}")
        super().__init__(device_id, clock, **kw)
        self.station = station
        self.url = f"https://www.ndbc.noaa.gov/data/realtime2/{station}.dart"
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def sample(self) -> dict[str, float]:
        values = parse_ndbc_dart(self._fetch_text(self.url, max_chars=2_000_000))
        if not values:
            raise DeviceOffline(f"{self.device_id}: DART feed has no valid observations")
        return {key: float(value) for key, value in values.items() if value is not None}


_IMERG_POINTS = (
    ("ottawa", 45.4215, -75.6972),
    ("berlin", 52.5200, 13.4050),
    ("delhi", 28.6139, 77.2090),
    ("tokyo", 35.6762, 139.6503),
    ("nairobi", -1.2921, 36.8219),
    ("sao-paulo", -23.5505, -46.6333),
)


def parse_imerg_hdf5(data: bytes, points=_IMERG_POINTS) -> list[dict[str, Any]]:
    """Extract one numeric IMERG grid cell for each requested coordinate."""
    try:
        import h5py
    except ImportError as exc:  # pragma: no cover - dependency is in GAIA runtime
        raise ValueError("h5py is required for IMERG") from exc
    try:
        with h5py.File(BytesIO(data), "r") as doc:
            grid = doc["Grid"]
            lats = grid["lat"][:]
            lons = grid["lon"][:]
            precip = grid["precipitation"]
            rows: list[dict[str, Any]] = []
            for name, wanted_lat, wanted_lon in points:
                iy = int(abs(lats - wanted_lat).argmin())
                ix = int(abs(lons - wanted_lon).argmin())
                # IMERG V07 convention is [time, lon, lat]. Tolerate the more
                # common [time, lat, lon] layout without guessing silently.
                if precip.shape[-2:] == (len(lons), len(lats)):
                    value = _num(precip[0, ix, iy])
                elif precip.shape[-2:] == (len(lats), len(lons)):
                    value = _num(precip[0, iy, ix])
                else:
                    raise ValueError(f"unexpected IMERG grid shape {precip.shape}")
                if value is None or value < 0:
                    continue
                rows.append({
                    "precipitation_mm_h": float(value),
                    "latitude": float(lats[iy]),
                    "longitude": float(lons[ix]),
                    "anchor": name,
                    "product": "GPM_3IMERGHHE.07 Early Run",
                })
            return rows
    except (KeyError, OSError) as exc:
        raise ValueError("invalid IMERG HDF5") from exc


class NasaImergPrecipitation(LiveDevice):
    """NASA GPM IMERG Early Run half-hour precipitation grid samples."""

    model = "GAIA-PRECIP (NASA IMERG)"
    policy_id = "nasa_imerg"
    fields = {"precipitation_mm_h": "mm/h", "latitude": "deg", "longitude": "deg"}
    source = (
        "https://disc.gsfc.nasa.gov/datasets/GPM_3IMERGHHE_07 "
        "(NASA GPM IMERG Early Run V07 half-hour precipitation; NASA open data. "
        "Free Earthdata Login token required; Early Run is near-real-time/preliminary.)"
    )
    attribution = "NASA GPM IMERG Early Run V07"
    url = (
        "https://cmr.earthdata.nasa.gov/search/granules.json?short_name="
        "GPM_3IMERGHHE&version=07&page_size=1&sort_key=-start_date"
    )

    def __init__(self, device_id: str, clock: SimClock, *, token: str, **kw):
        if not token.strip():
            raise ValueError("Earthdata token is required for IMERG")
        super().__init__(device_id, clock, **kw)
        self.token = token.strip()
        self.query_gate = threading.Lock()
        self._query_points: tuple[tuple[str, float, float], ...] | None = None
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def set_coordinate(self, latitude: float, longitude: float) -> None:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("IMERG coordinate out of range")
        self._query_points = (("query", float(latitude), float(longitude)),)

    def clear_coordinate(self) -> None:
        self._query_points = None

    @staticmethod
    def _granule(payload: Any) -> tuple[str, str]:
        entries = ((payload or {}).get("feed") or {}).get("entry") or []
        if not entries or not isinstance(entries[0], dict):
            raise DeviceOffline("IMERG CMR returned no granule")
        entry = entries[0]
        for link in entry.get("links") or []:
            href = str(link.get("href") or "") if isinstance(link, dict) else ""
            if href.startswith("https://data.gesdisc.earthdata.nasa.gov/"):
                return href, str(entry.get("time_start") or "")
        raise DeviceOffline("IMERG granule has no approved download URL")

    def _download(self, url: str) -> bytes:
        _assert_url_allowed(url)
        try:
            response = httpx.get(
                url, headers={"Authorization": f"Bearer {self.token}"},
                timeout=30.0, follow_redirects=False,
            )
            if response.status_code != 200 or len(response.content) > 16 * 1024 * 1024:
                raise DeviceOffline(f"{self.device_id}: IMERG download HTTP {response.status_code}")
            return response.content
        except httpx.HTTPError as exc:
            raise DeviceOffline(f"{self.device_id}: IMERG download unreachable") from exc

    def read(self) -> dict[str, Any]:
        granule_url, observed = self._granule(self._fetch(self.url))
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(granule_url)
        rows = parse_imerg_hdf5(
            self._download(granule_url), points=self._query_points or _IMERG_POINTS,
        )
        if not rows:
            raise DeviceOffline(f"{self.device_id}: IMERG had no valid configured cells")
        for row in rows:
            row["observed_at"] = observed
        return signed_cluster_read(
            self, rows,
            numeric_keys=("precipitation_mm_h", "latitude", "longitude"),
            meta_keys=("anchor", "product", "observed_at"),
        )


class NoaaNexradStatus(LiveDevice):
    """NWS WSR-88D station health/latency — one reading per radar coordinate."""

    model = "GAIA-RADAR (NEXRAD status)"
    policy_id = "noaa_nexrad_status"
    fields = {
        "radar_latency_s": "s",
        "reflectivity_calibration_db": "dB",
        "transmitter_power_w": "W",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.weather.gov/radar/stations (NWS NEXRAD WSR-88D station "
        "status; U.S. Government public domain. Operational health/latency, "
        "not reflectivity pixels.)"
    )
    attribution = "NOAA/NWS NEXRAD Radar Operations Center"
    url = "https://api.weather.gov/radar/stations?host=ldm2&stationType=WSR-88D"

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def collect_hotspots(self, payload: Any, *, limit: int = 250) -> list[dict[str, Any]]:
        features = payload.get("features") if isinstance(payload, dict) else None
        rows: list[dict[str, Any]] = []
        for feature in features or []:
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
            if props.get("stationType") != "WSR-88D":
                continue
            geom = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
            coords = geom.get("coordinates") if isinstance(geom, dict) else None
            if not isinstance(coords, list) or len(coords) < 2:
                continue
            lon, lat = _num(coords[0]), _num(coords[1])
            latency = props.get("latency") if isinstance(props.get("latency"), dict) else {}
            current = latency.get("current") if isinstance(latency.get("current"), dict) else {}
            rda = props.get("rda") if isinstance(props.get("rda"), dict) else {}
            rda_props = rda.get("properties") if isinstance(rda.get("properties"), dict) else {}
            cal = rda_props.get("reflectivityCalibrationCorrection")
            power = rda_props.get("averageTransmitterPower")
            cal = cal if isinstance(cal, dict) else {}
            power = power if isinstance(power, dict) else {}
            if lat is None or lon is None or _num(current.get("value")) is None:
                continue
            row: dict[str, Any] = {
                "radar_latency_s": float(_num(current.get("value")) or 0.0),
                "reflectivity_calibration_db": float(_num(cal.get("value")) or 0.0),
                "transmitter_power_w": float(_num(power.get("value")) or 0.0),
                "latitude": float(lat), "longitude": float(lon),
                "radar_id": str(props.get("id") or "")[:8],
                "name": str(props.get("name") or "")[:100],
                "status": str(rda_props.get("status") or "unknown")[:30],
                "operability": str(rda_props.get("operabilityStatus") or "unknown")[:60],
                "vcp": str(rda_props.get("volumeCoveragePattern") or "")[:16],
            }
            rows.append(row)
        rows.sort(key=lambda row: str(row.get("radar_id")))
        if not rows:
            raise DeviceOffline(f"{self.device_id}: NEXRAD status returned no active radars")
        return rows[: max(1, min(int(limit), 300))]

    def read(self) -> dict[str, Any]:
        return signed_cluster_read(
            self, self.collect_hotspots(self._fetch(self.url)),
            numeric_keys=("radar_latency_s", "reflectivity_calibration_db", "transmitter_power_w", "latitude", "longitude"),
            meta_keys=("radar_id", "name", "status", "operability", "vcp"),
        )


class CamsAirComposition(CoordinateQueryable, LiveDevice):
    """CAMS-derived aerosol/dust/pollen at an arbitrary requested coordinate."""

    model = "GAIA-CAMS (Open-Meteo relay)"
    fields = {
        "aerosol_optical_depth": "AOD",
        "dust_ugm3": "ug/m3",
        "alder_pollen_grains_m3": "grains/m3",
        "birch_pollen_grains_m3": "grains/m3",
        "grass_pollen_grains_m3": "grains/m3",
        "latitude": "deg", "longitude": "deg",
    }
    source = (
        "https://open-meteo.com (Open-Meteo Air Quality API using CAMS European/"
        "global atmospheric composition forecasts; data CC BY 4.0. Hosted free "
        "API is non-commercial; paid customer endpoint or self-host is required for sales.)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, latitude: float, longitude: float, **kw):
        super().__init__(device_id, clock, **kw)
        self._origin = _om_origin("air_quality")
        self.source = _om_source(CamsAirComposition.source, self._origin)
        self.headers = {**self.headers, **_om_auth_headers(self._origin)}
        self._init_coordinate(latitude, longitude)

    def _coordinate_changed(self) -> None:
        self.url = (
            f"{self._origin}/v1/air-quality?latitude={self.latitude:.5f}&longitude={self.longitude:.5f}"
            "&current=aerosol_optical_depth,dust,alder_pollen,birch_pollen,grass_pollen"
            f"{_om_apikey_suffix()}"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        cur = payload.get("current") if isinstance(payload, dict) else {}
        cur = cur if isinstance(cur, dict) else {}
        latitude = _num(payload.get("latitude")) if isinstance(payload, dict) else None
        longitude = _num(payload.get("longitude")) if isinstance(payload, dict) else None
        return {
            "aerosol_optical_depth": _num(cur.get("aerosol_optical_depth")),
            "dust_ugm3": _num(cur.get("dust")),
            "alder_pollen_grains_m3": _num(cur.get("alder_pollen")),
            "birch_pollen_grains_m3": _num(cur.get("birch_pollen")),
            "grass_pollen_grains_m3": _num(cur.get("grass_pollen")),
            "latitude": self.latitude if latitude is None else latitude,
            "longitude": self.longitude if longitude is None else longitude,
        }


class EpaRadNetStation(LiveDevice):
    """EPA RadNet approved hourly radiation at one fixed monitor coordinate."""

    model = "GAIA-RADNET (EPA)"
    policy_id = "epa_radnet"
    fields = {
        "dose_equivalent_nsv_h": "nSv/h",
        "gamma_count_total_cpm": "cpm",
        "latitude": "deg", "longitude": "deg",
    }
    source = (
        "https://radnet.epa.gov/cdx-radnet-rest/api/rest/csv/ "
        "(U.S. EPA RadNet approved near-real-time hourly gamma monitoring; "
        "U.S. Government public data. Total CPM is the sum of channels R02–R09.)"
    )
    attribution = "U.S. EPA RadNet"

    def __init__(self, device_id: str, clock: SimClock, *, state: str, city: str,
                 latitude: float, longitude: float, **kw):
        if not re.fullmatch(r"[A-Z]{2}", state) or not re.fullmatch(r"[A-Z0-9 .'-]+", city):
            raise ValueError("invalid RadNet station path")
        super().__init__(device_id, clock, **kw)
        self.latitude, self.longitude = float(latitude), float(longitude)
        self.state, self.city = state, city
        self.cache_ttl_s = max(300.0, float(_env("GAIA_RADNET_CACHE_TTL_S", "3600")))
        self._cached_csv = ""
        self._cached_at = 0.0
        self.url = (
            "https://radnet.epa.gov/cdx-radnet-rest/api/rest/csv/"
            f"{datetime.now(timezone.utc).year}/fixed/{state}/{quote(city, safe='')}"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map_csv(self, text: str) -> dict[str, float | None]:
        latest: dict[str, str] | None = None
        for row in csv.DictReader(text.splitlines()):
            if str(row.get("STATUS") or "").upper() != "APPROVED":
                continue
            if _num(row.get("DOSE EQUIVALENT RATE (nSv/h)")) is not None:
                latest = row
        if latest is None:
            return {}
        channels = [
            _num(latest.get(f"GAMMA COUNT RATE R0{i} (CPM)"))
            for i in range(2, 10)
        ]
        valid = [float(value) for value in channels if value is not None]
        return {
            "dose_equivalent_nsv_h": _num(latest.get("DOSE EQUIVALENT RATE (nSv/h)")),
            "gamma_count_total_cpm": sum(valid) if valid else None,
            "latitude": self.latitude, "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        now = time.monotonic()
        if not self._cached_csv or now - self._cached_at >= self.cache_ttl_s:
            self.url = (
                "https://radnet.epa.gov/cdx-radnet-rest/api/rest/csv/"
                f"{datetime.now(timezone.utc).year}/fixed/{self.state}/{quote(self.city, safe='')}"
            )
            self._cached_csv = self._fetch_text(self.url, max_chars=12_000_000)
            self._cached_at = now
        values = self.map_csv(self._cached_csv)
        if not values:
            raise DeviceOffline(f"{self.device_id}: RadNet has no approved dose row")
        return {key: float(value) for key, value in values.items() if value is not None}


def parse_sentinel_statistics(payload: Any) -> float | None:
    """Extract the newest valid B0 mean from Sentinel Hub Statistical API."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    for row in reversed(rows or []):
        outputs = row.get("outputs") if isinstance(row, dict) else None
        data = outputs.get("data") if isinstance(outputs, dict) else None
        bands = data.get("bands") if isinstance(data, dict) else None
        b0 = bands.get("B0") if isinstance(bands, dict) else None
        stats = b0.get("stats") if isinstance(b0, dict) else None
        value = _num(stats.get("mean")) if isinstance(stats, dict) else None
        if value is not None:
            return float(value)
    return None


def parse_sentinel_statistics_bands(payload: Any) -> dict[str, float]:
    """Extract the newest valid B0/B1 means from Sentinel Statistical API."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    for row in reversed(rows or []):
        outputs = row.get("outputs") if isinstance(row, dict) else None
        data = outputs.get("data") if isinstance(outputs, dict) else None
        bands = data.get("bands") if isinstance(data, dict) else None
        if not isinstance(bands, dict):
            continue
        out: dict[str, float] = {}
        for name in ("B0", "B1"):
            band = bands.get(name)
            stats = band.get("stats") if isinstance(band, dict) else None
            value = _num(stats.get("mean")) if isinstance(stats, dict) else None
            if value is not None:
                out[name] = float(value)
        if "B0" in out:
            return out
    return {}


class CopernicusSoilWaterIndex(CoordinateQueryable, LiveDevice):
    """CLMS global daily SWI020 at an arbitrary requested coordinate."""

    model = "GAIA-SOIL (Copernicus CLMS SWI)"
    policy_id = "copernicus_clms_swi"
    fields = {"soil_water_index_pct": "percent", "latitude": "deg", "longitude": "deg"}
    source = (
        "https://land.copernicus.eu/en/products/soil-moisture "
        "(Copernicus Land Monitoring Service global Soil Water Index 12.5 km "
        "daily v3, SWI020; free for any purpose with Copernicus attribution. "
        "Free CDSE OAuth client required.)"
    )
    attribution = (
        "Contains modified Copernicus Land Monitoring Service information"
    )
    _token_url = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
        "protocol/openid-connect/token"
    )
    url = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
    _collection = "byoc-f2278442-eb7f-4926-93e9-7a382f567fb4"

    def __init__(self, device_id: str, clock: SimClock, *, client_id: str,
                 client_secret: str, latitude: float, longitude: float, **kw):
        if not client_id or not client_secret:
            raise ValueError("CDSE OAuth client id/secret required")
        super().__init__(device_id, clock, **kw)
        self.client_id, self.client_secret = client_id, client_secret
        self._init_coordinate(latitude, longitude)
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(self.url)
        policy.require_endpoint(self._token_url)

    def _token(self) -> str:
        try:
            response = httpx.post(
                self._token_url,
                data={"grant_type": "client_credentials", "client_id": self.client_id,
                      "client_secret": self.client_secret},
                timeout=20.0, follow_redirects=False,
            )
            if response.status_code != 200:
                raise DeviceOffline(f"{self.device_id}: CDSE OAuth HTTP {response.status_code}")
            token = str(response.json().get("access_token") or "")
            if not token:
                raise DeviceOffline(f"{self.device_id}: CDSE OAuth returned no token")
            return token
        except (httpx.HTTPError, ValueError) as exc:
            raise DeviceOffline(f"{self.device_id}: CDSE OAuth unreachable") from exc

    def sample(self) -> dict[str, float]:
        now = datetime.now(timezone.utc)
        start = now.timestamp() - 10 * 86400
        start_iso = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        d = 0.01
        body = {
            "input": {"bounds": {"bbox": [self.longitude-d, self.latitude-d, self.longitude+d, self.latitude+d]},
                      "data": [{"type": self._collection,
                                "dataFilter": {"timeRange": {"from": start_iso, "to": end_iso},
                                               "mosaickingOrder": "mostRecent"}}]},
            "aggregation": {"timeRange": {"from": start_iso, "to": end_iso},
                            "aggregationInterval": {"of": "P10D"}, "width": 1, "height": 1,
                            "evalscript": "//VERSION=3\nfunction setup(){return {input:[\"SWI020\",\"dataMask\"],output:[{id:\"data\",bands:1},{id:\"dataMask\",bands:1}]};}function evaluatePixel(s){return {data:[s.SWI020/2],dataMask:[s.dataMask]};}"},
        }
        try:
            response = httpx.post(
                self.url, json=body, headers={"Authorization": f"Bearer {self._token()}"},
                timeout=30.0, follow_redirects=False,
            )
            if response.status_code != 200:
                raise DeviceOffline(f"{self.device_id}: CLMS statistics HTTP {response.status_code}")
            value = parse_sentinel_statistics(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise DeviceOffline(f"{self.device_id}: CLMS statistics unreachable") from exc
        if value is None:
            raise DeviceOffline(f"{self.device_id}: CLMS SWI returned no valid cell")
        return {"soil_water_index_pct": value, "latitude": self.latitude, "longitude": self.longitude}


class CopernicusSentinel3LandTemperature(CopernicusSoilWaterIndex):
    """Sentinel-3 SLSTR L2 land-surface temperature at one coordinate."""

    model = "GAIA-LST (Copernicus Sentinel-3 SLSTR L2)"
    policy_id = "copernicus_s3_lst"
    fields = {
        "land_surface_temperature_c": "cel",
        "land_surface_temperature_uncertainty_k": "K",
        "latitude": "deg", "longitude": "deg",
    }
    source = (
        "https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/"
        "S3SLSTRL2.html (Copernicus Sentinel-3 SLSTR Level-2 LST, 1-km thermal "
        "infrared retrieval; free/full/open Copernicus data. CDSE OAuth client required.)"
    )
    attribution = "Contains modified Copernicus Sentinel-3 SLSTR Level-2 information"

    def sample(self) -> dict[str, float]:
        now = datetime.now(timezone.utc)
        start_iso = datetime.fromtimestamp(
            now.timestamp() - 10 * 86400, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        # About one native SLSTR LST cell. The resulting Atlas point remains the
        # configured cell coordinate rather than a generic regional centroid.
        d = 0.0045
        body = {
            "input": {"bounds": {"bbox": [self.longitude-d, self.latitude-d,
                                             self.longitude+d, self.latitude+d]},
                      "data": [{"type": "sentinel-3-slstr-l2",
                                "dataFilter": {"timeRange": {"from": start_iso, "to": end_iso},
                                               "mosaickingOrder": "mostRecent",
                                               "maxCloudCoverage": 80}}]},
            "aggregation": {"timeRange": {"from": start_iso, "to": end_iso},
                            "aggregationInterval": {"of": "P10D"}, "width": 1, "height": 1,
                            "evalscript": "//VERSION=3\nfunction setup(){return {input:[{bands:[\"LST\",\"LST_uncertainty\",\"dataMask\"],units:[\"KELVIN\",\"KELVIN\",\"DN\"]}],output:[{id:\"data\",bands:2},{id:\"dataMask\",bands:1}]};}function evaluatePixel(s){return {data:[s.LST-273.15,s.LST_uncertainty],dataMask:[s.dataMask&&s.LST>0]};}"},
        }
        try:
            response = httpx.post(
                self.url, json=body, headers={"Authorization": f"Bearer {self._token()}"},
                timeout=30.0, follow_redirects=False,
            )
            if response.status_code != 200:
                raise DeviceOffline(f"{self.device_id}: Sentinel-3 statistics HTTP {response.status_code}")
            stats = parse_sentinel_statistics_bands(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise DeviceOffline(f"{self.device_id}: Sentinel-3 statistics unreachable") from exc
        if "B0" not in stats:
            raise DeviceOffline(f"{self.device_id}: Sentinel-3 returned no valid LST cell")
        values = {
            "land_surface_temperature_c": stats["B0"],
            "latitude": self.latitude, "longitude": self.longitude,
        }
        if "B1" in stats:
            values["land_surface_temperature_uncertainty_k"] = stats["B1"]
        return values


def parse_nasa_power_daily(payload: Any) -> dict[str, float]:
    """Return the newest published POWER day, ignoring the documented -999 fill."""
    props = payload.get("properties") if isinstance(payload, dict) else None
    params = props.get("parameter") if isinstance(props, dict) else None
    all_sky = params.get("ALLSKY_SFC_SW_DWN") if isinstance(params, dict) else None
    clear_sky = params.get("CLRSKY_SFC_SW_DWN") if isinstance(params, dict) else None
    if not isinstance(all_sky, dict):
        return {}
    for day in sorted(all_sky, reverse=True):
        value = _num(all_sky.get(day))
        if value is None or value <= -900:
            continue
        out = {
            "solar_irradiation_kwh_m2_day": float(value),
            "solar_observation_yyyymmdd": float(day),
        }
        clear = _num(clear_sky.get(day)) if isinstance(clear_sky, dict) else None
        if clear is not None and clear > -900:
            out["clear_sky_irradiation_kwh_m2_day"] = float(clear)
        return out
    return {}


class NasaPowerSolar(CoordinateQueryable, LiveDevice):
    """NASA POWER daily solar irradiation at an arbitrary requested coordinate."""

    model = "GAIA-SOLAR (NASA POWER)"
    policy_id = "nasa_power_solar"
    fields = {
        "solar_irradiation_kwh_m2_day": "kWh/m2/day",
        "clear_sky_irradiation_kwh_m2_day": "kWh/m2/day",
        "solar_observation_yyyymmdd": "date",
        "latitude": "deg", "longitude": "deg",
    }
    source = (
        "https://power.larc.nasa.gov/api/temporal/daily/point "
        "(NASA POWER source-resolution daily all-sky and clear-sky surface "
        "irradiation; NASA open data/CC0 unless marked otherwise. Cite NASA POWER; "
        "do not imply NASA endorsement.)"
    )
    attribution = "NASA POWER"
    _base = "https://power.larc.nasa.gov/api/temporal/daily/point"

    def __init__(self, device_id: str, clock: SimClock, *, latitude: float,
                 longitude: float, **kw):
        super().__init__(device_id, clock, **kw)
        self._init_coordinate(latitude, longitude)
        self.url = self._url_for(datetime.now(timezone.utc))
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def _url_for(self, now: datetime) -> str:
        # POWER is near-real-time rather than instantaneous. A long rolling window
        # also makes the relay robust when the runtime clock is ahead of publication.
        start = datetime.fromtimestamp(now.timestamp() - 400 * 86400, timezone.utc)
        return (
            f"{self._base}?parameters=ALLSKY_SFC_SW_DWN%2CCLRSKY_SFC_SW_DWN"
            f"&community=RE&longitude={self.longitude:.5f}&latitude={self.latitude:.5f}"
            f"&start={start:%Y%m%d}&end={now:%Y%m%d}&format=JSON&time-standard=UTC"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        values: dict[str, float | None] = parse_nasa_power_daily(payload)
        geometry = payload.get("geometry") if isinstance(payload, dict) else None
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        lon = _num(coords[0]) if isinstance(coords, (list, tuple)) and len(coords) >= 2 else None
        lat = _num(coords[1]) if isinstance(coords, (list, tuple)) and len(coords) >= 2 else None
        values.update({
            "latitude": self.latitude if lat is None else lat,
            "longitude": self.longitude if lon is None else lon,
        })
        return values

    def sample(self) -> dict[str, float]:
        self.url = self._url_for(datetime.now(timezone.utc))
        values = self.map(self._fetch(self.url))
        if "solar_irradiation_kwh_m2_day" not in values:
            raise DeviceOffline(f"{self.device_id}: NASA POWER has no published solar day")
        return {key: float(value) for key, value in values.items() if value is not None}


def parse_snodas_tar(data: bytes, points: Any) -> list[dict[str, float | str]]:
    """Sample documented metre/1000 SNODAS depth and SWE binary grid cells."""
    try:
        import numpy as np
        with tarfile.open(fileobj=BytesIO(data), mode="r:") as archive:
            members = {member.name: member for member in archive.getmembers()}
            grids: dict[str, Any] = {}
            meta: dict[str, float] = {}
            for code, field in (("11036", "snow_depth_cm"),
                                ("11034", "snow_water_equivalent_cm")):
                dat = next(m for name, m in members.items()
                           if code in name and name.endswith(".dat.gz"))
                txt = next(m for name, m in members.items()
                           if code in name and name.endswith(".txt.gz"))
                header = gzip.decompress(archive.extractfile(txt).read()).decode("utf-8")
                props = {
                    key.strip(): value.strip()
                    for key, value in (line.split(":", 1) for line in header.splitlines()
                                       if ":" in line)
                }
                rows = int(props["Number of rows"])
                cols = int(props["Number of columns"])
                raster = np.frombuffer(
                    gzip.decompress(archive.extractfile(dat).read()), dtype=">i2"
                )
                if raster.size != rows * cols:
                    raise ValueError("SNODAS raster dimensions do not match metadata")
                grids[field] = raster.reshape((rows, cols))
                meta = {
                    "rows": rows, "cols": cols,
                    "x0": float(props["Benchmark x-axis coordinate"]),
                    "y0": float(props["Benchmark y-axis coordinate"]),
                    "dx": float(props["X-axis resolution"]),
                    "dy": float(props["Y-axis resolution"]),
                }
    except (KeyError, StopIteration, tarfile.TarError, gzip.BadGzipFile, OSError,
            UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid SNODAS daily archive") from exc

    results: list[dict[str, float | str]] = []
    for anchor, wanted_lat, wanted_lon in points:
        col = int(round((wanted_lon - meta["x0"]) / meta["dx"]))
        row = int(round((meta["y0"] - wanted_lat) / meta["dy"]))
        if not (0 <= row < meta["rows"] and 0 <= col < meta["cols"]):
            continue
        depth_raw = int(grids["snow_depth_cm"][row, col])
        swe_raw = int(grids["snow_water_equivalent_cm"][row, col])
        # 32767 is the documented saturated/error maximum seen at some glacier
        # cells; it is not a sellable physical observation.
        if depth_raw < 0 or swe_raw < 0 or depth_raw >= 32767 or swe_raw >= 32767:
            continue
        # Both products are metres with scale factor 1000. raw / 1000 m = raw / 10 cm.
        results.append({
            "snow_depth_cm": depth_raw / 10.0,
            "snow_water_equivalent_cm": swe_raw / 10.0,
            "latitude": meta["y0"] - row * meta["dy"],
            "longitude": meta["x0"] + col * meta["dx"],
            "anchor": str(anchor),
        })
    return results


class NoaaNohrscSnow(CoordinateQueryable, LiveDevice):
    """NOAA NOHRSC/SNODAS snow analysis at one configured CONUS grid cell."""

    model = "GAIA-SNOW (NOAA NOHRSC)"
    policy_id = "noaa_nohrsc_snow"
    fields = {
        "snow_depth_cm": "cm", "snow_water_equivalent_cm": "cm",
        "snow_observation_yyyymmdd": "date", "latitude": "deg", "longitude": "deg",
    }
    source = (
        "https://noaadata.apps.nsidc.org/NOAA/G02158/masked/ "
        "(NOAA/NWS NOHRSC SNODAS daily binary archive at NOAA@NSIDC; "
        "U.S. Government public domain. Assimilated SNODAS model grid, provisional, "
        "not an in-situ sensor.)"
    )
    attribution = "NOAA/NWS NOHRSC National Snow Analysis (SNODAS)"
    _base = "https://noaadata.apps.nsidc.org/NOAA/G02158/masked"

    def __init__(self, device_id: str, clock: SimClock, *, latitude: float,
                 longitude: float, **kw):
        super().__init__(device_id, clock, **kw)
        self._init_coordinate(latitude, longitude)
        self.url = self._index_url(datetime.now(timezone.utc))
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def _coordinate_ok(self, latitude: float, longitude: float) -> bool:
        return 24.95 <= latitude <= 52.875 and -124.734 <= longitude <= -66.94

    @classmethod
    def _index_url(cls, when: datetime) -> str:
        return f"{cls._base}/{when:%Y}/{when:%m_%b}/"

    @staticmethod
    def _latest_name(index_html: str) -> tuple[str, str]:
        days = re.findall(r"SNODAS_(\d{8})\.tar", index_html)
        if not days:
            raise DeviceOffline("SNODAS directory contains no daily archive")
        day = max(days)
        return f"SNODAS_{day}.tar", day

    def sample(self) -> dict[str, float]:
        now = datetime.now(timezone.utc)
        try:
            index_url = self._index_url(now)
            name, day = self._latest_name(self._fetch_text(index_url, max_chars=500_000))
        except DeviceOffline:
            previous = datetime.fromtimestamp(now.timestamp() - 32 * 86400, timezone.utc)
            index_url = self._index_url(previous)
            name, day = self._latest_name(self._fetch_text(index_url, max_chars=500_000))
        archive_url = f"{index_url}{name}"
        require_approved_source(self.policy_id).require_endpoint(archive_url)
        rows = parse_snodas_tar(
            self._fetch_bytes(archive_url, max_bytes=8_000_000),
            ((self.device_id, self.latitude, self.longitude),),
        )
        if not rows:
            raise DeviceOffline(f"{self.device_id}: NOHRSC returned no snow grid cell")
        values = rows[0]
        values["snow_observation_yyyymmdd"] = float(day)
        return {key: float(value) for key, value in values.items()
                if key != "anchor" and value is not None}


_ARCTIC_ICE_POINTS = (
    ("beaufort", 74.0, -145.0),
    ("chukchi", 72.0, -170.0),
    ("laptev", 78.0, 125.0),
    ("kara", 77.0, 75.0),
    ("greenland", 78.0, -5.0),
)


def parse_nsidc_ice_geotiff(data: bytes, points=_ARCTIC_ICE_POINTS) -> list[dict[str, Any]]:
    """Sample exact Sea Ice Index grid cells and return their cell-centre coordinates."""
    try:
        import tifffile
        from pyproj import Transformer
        with tifffile.TiffFile(BytesIO(data)) as doc:
            page = doc.pages[0]
            raster = page.asarray()
            scale = page.tags[33550].value  # ModelPixelScaleTag
            tie = page.tags[33922].value  # ModelTiepointTag
    except (KeyError, ValueError, OSError) as exc:
        raise ValueError("invalid NSIDC Sea Ice Index GeoTIFF") from exc
    to_grid = Transformer.from_crs(4326, 3411, always_xy=True)
    to_geo = Transformer.from_crs(3411, 4326, always_xy=True)
    rows: list[dict[str, Any]] = []
    for anchor, wanted_lat, wanted_lon in points:
        x, y = to_grid.transform(wanted_lon, wanted_lat)
        col = int((x - float(tie[3])) / float(scale[0]))
        row = int((float(tie[4]) - y) / float(scale[1]))
        if not (0 <= row < raster.shape[0] and 0 <= col < raster.shape[1]):
            continue
        raw = int(raster[row, col])
        # GeoTIFF concentration is percent * 10. Values above 1000 are land,
        # coast, pole-hole or missing-data flags and must never become readings.
        if not 0 <= raw <= 1000:
            continue
        cell_x = float(tie[3]) + (col + 0.5) * float(scale[0])
        cell_y = float(tie[4]) - (row + 0.5) * float(scale[1])
        cell_lon, cell_lat = to_geo.transform(cell_x, cell_y)
        rows.append({
            "sea_ice_concentration_pct": raw / 10.0,
            "latitude": float(cell_lat), "longitude": float(cell_lon),
            "anchor": anchor, "product": "NOAA/NSIDC Sea Ice Index v4",
        })
    return rows


class NoaaNsidcSeaIceIndex(LiveDevice):
    """Current NOAA/NSIDC Sea Ice Index v4 concentration grid samples."""

    model = "GAIA-ICE (NOAA/NSIDC Sea Ice Index)"
    policy_id = "noaa_nsidc_sea_ice"
    fields = {
        "sea_ice_concentration_pct": "percent", "latitude": "deg", "longitude": "deg",
        "sea_ice_observation_yyyymmdd": "date",
    }
    source = (
        "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/geotiff/ "
        "(NOAA/NSIDC Sea Ice Index v4 daily 25-km concentration GeoTIFF; U.S. "
        "Government public data with required dataset citation. Not for navigation.)"
    )
    attribution = (
        "Fetterer et al. (2025), NOAA/NSIDC Sea Ice Index v4, doi:10.7265/a98x-0f50"
    )
    _base = "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/geotiff"

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        self.query_gate = threading.Lock()
        self._query_points: tuple[tuple[str, float, float], ...] | None = None
        self.url = self._index_url(datetime.now(timezone.utc))
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def set_coordinate(self, latitude: float, longitude: float) -> None:
        if not (40 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Sea Ice Index coordinate must be in the Northern Hemisphere domain")
        self._query_points = (("query", float(latitude), float(longitude)),)

    def clear_coordinate(self) -> None:
        self._query_points = None

    @classmethod
    def _index_url(cls, when: datetime) -> str:
        return f"{cls._base}/{when:%Y}/{when:%m_%b}/"

    @staticmethod
    def _latest_name(index_html: str) -> tuple[str, str]:
        names = re.findall(r"N_(\d{8})_concentration_v4\.0\.tif", index_html)
        if not names:
            raise DeviceOffline("Sea Ice Index directory contains no daily GeoTIFF")
        day = max(names)
        return f"N_{day}_concentration_v4.0.tif", day

    def read(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        try:
            index_url = self._index_url(now)
            index = self._fetch_text(index_url, max_chars=1_000_000)
            name, day = self._latest_name(index)
        except DeviceOffline:
            previous = datetime.fromtimestamp(now.timestamp() - 32 * 86400, timezone.utc)
            index_url = self._index_url(previous)
            index = self._fetch_text(index_url, max_chars=1_000_000)
            name, day = self._latest_name(index)
        tif_url = f"{index_url}{name}"
        require_approved_source(self.policy_id).require_endpoint(tif_url)
        rows = parse_nsidc_ice_geotiff(
            self._fetch_bytes(tif_url, max_bytes=2_000_000),
            points=self._query_points or _ARCTIC_ICE_POINTS,
        )
        if not rows:
            raise DeviceOffline(f"{self.device_id}: Sea Ice Index had no valid configured cells")
        for row in rows:
            row["sea_ice_observation_yyyymmdd"] = float(day)
        return signed_cluster_read(
            self, rows,
            numeric_keys=("sea_ice_concentration_pct", "latitude", "longitude",
                          "sea_ice_observation_yyyymmdd"),
            meta_keys=("anchor", "product"),
        )


def register_p4_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    enabled = lambda name: _env(name, "1").lower() in ("1", "true", "yes", "on")
    n = 0
    if enabled("GAIA_HMS_SMOKE_ENABLED"):
        fleet.add(NoaaHmsSmoke("hms-smoke-01", clock, site="live-smoke-hms", key_dir=key_dir))
        n += 1
    if enabled("GAIA_USGS_WQ_ENABLED"):
        fleet.add(UsgsWaterQuality(
            "usgs-wq-01", clock,
            station=_env("GAIA_USGS_WQ_STATION", "01480065"),
            site="live-water-quality-usgs", key_dir=key_dir,
        ))
        n += 1
    if enabled("GAIA_DART_ENABLED"):
        for row in DART_STATIONS:
            fleet.add(NoaaDartGauge(
                str(row["device_id"]), clock, station=str(row["station_id"]),
                site=f"live-dart-{row['station_id']}", key_dir=key_dir,
            ))
            n += 1
    if enabled("GAIA_IMERG_ENABLED"):
        token = _env("GAIA_EARTHDATA_TOKEN")
        if token:
            fleet.add(NasaImergPrecipitation(
                "imerg-01", clock, token=token,
                site="live-precip-imerg", key_dir=key_dir,
            ))
            n += 1
    if enabled("GAIA_NEXRAD_STATUS_ENABLED"):
        fleet.add(NoaaNexradStatus(
            "nexrad-status-01", clock, site="live-radar-nexrad", key_dir=key_dir,
        ))
        n += 1
    if enabled("GAIA_CAMS_ENABLED"):
        for name, lat, lon in _IMERG_POINTS:
            fleet.add(CamsAirComposition(
                f"cams-{name}", clock, latitude=lat, longitude=lon,
                site=f"live-cams-{name}", key_dir=key_dir,
            ))
            n += 1
    if enabled("GAIA_RADNET_ENABLED"):
        for row in RADNET_STATIONS:
            fleet.add(EpaRadNetStation(
                str(row["device_id"]), clock,
                state=str(row["state"]), city=str(row["city_path"]),
                latitude=float(row["latitude"]), longitude=float(row["longitude"]),
                site=f"live-{row['device_id']}", key_dir=key_dir,
            ))
            n += 1
    if enabled("GAIA_COPERNICUS_SOIL_ENABLED"):
        client_id = _env("GAIA_CDSE_CLIENT_ID")
        client_secret = _env("GAIA_CDSE_CLIENT_SECRET")
        if client_id and client_secret:
            for name, lat, lon in _IMERG_POINTS:
                fleet.add(CopernicusSoilWaterIndex(
                    f"soil-{name}", clock, client_id=client_id, client_secret=client_secret,
                    latitude=lat, longitude=lon, site=f"live-soil-{name}", key_dir=key_dir,
                ))
                n += 1
    if enabled("GAIA_POWER_SOLAR_ENABLED"):
        for name, lat, lon in _IMERG_POINTS:
            fleet.add(NasaPowerSolar(
                f"solar-{name}", clock, latitude=lat, longitude=lon,
                site=f"live-solar-{name}", key_dir=key_dir,
            ))
            n += 1
    if enabled("GAIA_NOHRSC_SNOW_ENABLED"):
        for name, lat, lon in (
            ("rainier", 46.7860, -121.7360),
            ("tahoe", 39.1700, -120.1400),
            ("mammoth", 37.6400, -119.0000),
            ("rockies", 40.3428, -105.6836),
            ("teton", 43.7904, -110.6818),
            ("washington", 44.2706, -71.3033),
        ):
            fleet.add(NoaaNohrscSnow(
                f"snow-{name}", clock, latitude=lat, longitude=lon,
                site=f"live-snow-{name}", key_dir=key_dir,
            ))
            n += 1
    if enabled("GAIA_NSIDC_ICE_ENABLED"):
        fleet.add(NoaaNsidcSeaIceIndex(
            "nsidc-ice-01", clock, site="live-sea-ice-index", key_dir=key_dir,
        ))
        n += 1
    if enabled("GAIA_SENTINEL3_LST_ENABLED"):
        client_id = _env("GAIA_CDSE_CLIENT_ID")
        client_secret = _env("GAIA_CDSE_CLIENT_SECRET")
        if client_id and client_secret:
            for name, lat, lon in _IMERG_POINTS:
                fleet.add(CopernicusSentinel3LandTemperature(
                    f"lst-{name}", clock, client_id=client_id, client_secret=client_secret,
                    latitude=lat, longitude=lon, site=f"live-lst-{name}", key_dir=key_dir,
                ))
                n += 1
    return n


__all__ = [
    "NoaaHmsSmoke", "UsgsWaterQuality", "NoaaDartGauge", "parse_hms_smoke_kml",
    "NasaImergPrecipitation", "parse_ndbc_dart", "parse_imerg_hdf5", "register_p4_relays",
    "NoaaNexradStatus",
    "CamsAirComposition",
    "EpaRadNetStation",
    "CopernicusSoilWaterIndex", "parse_sentinel_statistics",
    "NasaPowerSolar", "parse_nasa_power_daily",
    "NoaaNohrscSnow", "parse_snodas_tar",
    "NoaaNsidcSeaIceIndex", "parse_nsidc_ice_geotiff",
    "CopernicusSentinel3LandTemperature", "parse_sentinel_statistics_bands",
]
