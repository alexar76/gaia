"""NDW DATEX II v3 road safety notices and temporary closures."""

from __future__ import annotations

import gzip
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Any
from zlib import error as ZlibError

import httpx

from gaia.devices._live_base import LiveDevice, _UA
from gaia.devices._policy import _assert_url_allowed
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.live_transport_events import _geometry, _utc
from gaia.source_policy import require_approved_source

_NS = {
    "msg": "http://datex2.eu/schema/3/messageContainer",
    "com": "http://datex2.eu/schema/3/common",
    "sit": "http://datex2.eu/schema/3/situation",
    "loc": "http://datex2.eu/schema/3/locationReferencing",
}
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
_MAX_GZIP_BYTES = 1024 * 1024
_MAX_XML_BYTES = 8 * 1024 * 1024
_SNAPSHOT_AGE = timedelta(minutes=5)
_RECORD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RECORD_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")
_CLOSURE_LABELS = {
    "carriagewayClosures": "Carriageway closure",
    "laneClosures": "Lane closure",
    "roadClosed": "Road closure",
    "bridgeSwingInOperation": "Bridge opening",
}


def _date_interval(
    node: ET.Element, start_name: str, end_name: str,
) -> tuple[datetime, datetime | None] | None:
    starts = node.findall(start_name, _NS)
    ends = node.findall(end_name, _NS)
    if len(starts) != 1 or len(ends) > 1:
        return None
    start = _utc(starts[0].text)
    end = _utc(ends[0].text) if ends else None
    if start is None or (ends and (end is None or end <= start)):
        return None
    return start, end


def _active_interval(
    record: ET.Element, now: datetime,
) -> tuple[datetime, datetime | None] | None:
    validity = record.find("sit:validity", _NS)
    if validity is None or validity.findtext("com:validityStatus", namespaces=_NS) != "definedByValidityTimeSpec":
        return None
    spec = validity.find("com:validityTimeSpecification", _NS)
    if spec is None:
        return None
    overall_names = ("com:overallStartTime", "com:overallEndTime")
    period_name = "com:validPeriod"
    periods = spec.findall(period_name, _NS)
    if len(spec) != len(periods) + sum(len(spec.findall(name, _NS)) for name in overall_names):
        # Exceptions, recurrences and extensions outside the supported profile
        # can constrain a closure further. Never widen their effective window.
        return None
    overall = _date_interval(spec, *overall_names)
    if overall is None:
        return None
    start, end = overall
    if now < start or (end is not None and now >= end):
        return None
    if not periods:
        return overall

    active: list[tuple[datetime, datetime | None]] = []
    period_names = ("com:startOfPeriod", "com:endOfPeriod")
    for period in periods:
        if len(period) != sum(len(period.findall(name, _NS)) for name in period_names):
            return None
        window = _date_interval(period, *period_names)
        if window is None:
            return None
        period_start, period_end = window
        effective_start = max(start, period_start)
        effective_end = min((bound for bound in (end, period_end) if bound is not None), default=None)
        if effective_start <= now and (effective_end is None or now < effective_end):
            active.append((effective_start, effective_end))
    # Pick the active window with the latest end. The provider may publish
    # several windows, but a single map object cannot represent gaps.
    return max(active, key=lambda interval: interval[1] or datetime.max.replace(tzinfo=UTC), default=None)


def _record_geometry(record: ET.Element) -> dict[str, Any] | None:
    location = record.find("sit:locationReference", _NS)
    if location is None:
        return None
    lines = location.findall(".//loc:gmlLineString", _NS)
    if lines:
        paths = []
        for line in lines:
            if line.get("srsName") != "WGS 84":
                return None
            positions = (line.findtext("loc:posList", namespaces=_NS) or "").split()
            if len(positions) < 4 or len(positions) % 2:
                return None
            # NDW's WGS 84 posList is latitude, longitude. GeoJSON is lon, lat.
            paths.append([[lon, lat] for lat, lon in zip(positions[::2], positions[1::2])])
        if len(paths) == 1:
            return _geometry({"type": "LineString", "coordinates": paths[0]})
        return _geometry({"type": "MultiLineString", "coordinates": paths})

    points = location.findall(".//loc:pointByCoordinates/loc:pointCoordinates", _NS)
    if len(points) != 1:
        return None
    lon = points[0].findtext("loc:longitude", namespaces=_NS)
    lat = points[0].findtext("loc:latitude", namespaces=_NS)
    return _geometry({"type": "Point", "coordinates": [lon, lat]})


class NdwRoadNotices(LiveDevice):
    """Fresh Dutch publisher snapshots of safety notices or temporary closures."""

    model = "GAIA-ROAD-NOTICES (NDW)"
    policy_id = "ndw_road_events"
    attribution = "Nationaal Dataportaal Wegverkeer (NDW) · CC0 1.0"
    fields = {"latitude": "deg", "longitude": "deg", "transport_notice_count": "count"}

    def __init__(self, *args: Any, closures: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.closures = closures
        self.notice_type = "temporary road measure" if closures else "road safety notice"
        filename = ("tijdelijke_verkeersmaatregelen_afsluitingen" if closures
                    else "veiligheidsgerelateerde_informatie_srti")
        self.url = f"https://opendata.ndw.nu/{filename}.xml.gz"
        self.source = (
            f"{self.url} (Nationaal Dataportaal Wegverkeer, CC0 1.0; publisher snapshot. "
            "Geometry coordinates rounded to five decimals.)"
        )

    def _fetch_xml(self) -> ET.Element:
        require_approved_source(self.policy_id).require_endpoint(self.url)
        url = _assert_url_allowed(self.url)
        headers = {"User-Agent": _UA, "Accept": "application/gzip", "Accept-Encoding": "identity"}
        compressed = bytearray()
        try:
            with httpx.stream("GET", url, headers=headers, timeout=self.timeout, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise DeviceOffline(f"{self.device_id}: upstream HTTP {response.status_code}")
                for chunk in response.iter_raw(chunk_size=64 * 1024):
                    if len(compressed) + len(chunk) > _MAX_GZIP_BYTES:
                        raise DeviceOffline(f"{self.device_id}: compressed NDW body exceeds limit")
                    compressed.extend(chunk)
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                document = stream.read(_MAX_XML_BYTES + 1)
            if len(document) > _MAX_XML_BYTES:
                raise DeviceOffline(f"{self.device_id}: NDW XML body exceeds limit")
            xml = document.decode("utf-8-sig")
            if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
                raise DeviceOffline(f"{self.device_id}: NDW XML declarations are unsupported")
            return ET.fromstring(xml)
        except (httpx.HTTPError, OSError, EOFError, ZlibError, UnicodeError, ET.ParseError) as exc:
            raise DeviceOffline(f"{self.device_id}: invalid or unavailable NDW snapshot") from exc

    def _label(self, record: ET.Element) -> tuple[str, str, str | None] | None:
        if self.closures:
            subtype = (record.findtext("sit:roadOrCarriagewayOrLaneManagementType", namespaces=_NS)
                       or record.findtext("sit:generalNetworkManagementType", namespaces=_NS) or "")
            status = record.findtext("sit:operatorActionStatus", namespaces=_NS)
            if subtype not in _CLOSURE_LABELS or status not in {"approved", "implemented"}:
                return None
            return _CLOSURE_LABELS[subtype], subtype, f"NDW action status: {status}."
        subtype = record.get(_XSI_TYPE, "").rsplit(":", 1)[-1]
        if not _RECORD_TYPE.fullmatch(subtype):
            return None
        words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", subtype)
        return words, subtype, None

    def read(self) -> dict[str, Any]:
        root = self._fetch_xml()
        now = datetime.now(UTC)
        payloads = root.findall("msg:payload", _NS)
        if root.tag != f"{{{_NS['msg']}}}messageContainer" or len(payloads) != 1:
            raise DeviceOffline(f"{self.device_id}: unexpected NDW message container")
        payload = payloads[0]
        if payload.get(_XSI_TYPE, "").rsplit(":", 1)[-1] != "SituationPublication":
            raise DeviceOffline(f"{self.device_id}: unexpected NDW publication type")
        publications = payload.findall("com:publicationTime", _NS)
        published = _utc(publications[0].text) if len(publications) == 1 else None
        if published is None or not published <= now < published + _SNAPSHOT_AGE:
            raise DeviceOffline(f"{self.device_id}: NDW publication time outside 5-minute window")

        candidates: dict[str, tuple[datetime, ET.Element]] = {}
        for record in payload.findall("sit:situation/sit:situationRecord", _NS):
            event_id = record.get("id", "")
            version = _utc(record.findtext("sit:situationRecordVersionTime", namespaces=_NS))
            if not _RECORD_ID.fullmatch(event_id) or version is None:
                continue
            previous = candidates.get(event_id)
            if previous is None or previous[0] < version:
                candidates[event_id] = version, record

        published_at = published.isoformat()
        expires_at = (published + _SNAPSHOT_AGE).isoformat()
        source_meta = {"source_published_at": published_at, "source_expires_at": expires_at}
        rows = []
        for event_id, (version, record) in sorted(candidates.items()):
            if version > published:
                continue
            timing = _active_interval(record, now)
            shape = _record_geometry(record) if timing is not None else None
            label = self._label(record) if shape is not None else None
            if timing is None or shape is None or label is None:
                continue
            start, end = timing
            title, subtype, message = label
            rows.append({
                **shape, **source_meta, "event_id": event_id, "title": title,
                "notice_text": message, "notice_type": self.notice_type, "notice_subtype": subtype,
                "issue_at": version.isoformat(), "valid_from": start.isoformat(),
                "valid_until": end.isoformat() if end is not None else None,
                "claim_class": "published_road_notice",
                "evidence_boundary": (
                    "Published NDW road notice within its stated validity. "
                    "Check the road operator for current conditions."
                ),
            })
        if len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()) > 3_000_000:
            raise DeviceOffline(f"{self.device_id}: event geometry exceeds the response budget")
        return signed_cluster_read(
            self, rows, numeric_keys=tuple(self.fields), structured_keys=("geometry", "event_bbox"),
            meta_keys=("event_id", "title", "notice_text", "notice_type", "notice_subtype",
                       "issue_at", "valid_from", "valid_until", "claim_class", "evidence_boundary",
                       "source_published_at", "source_expires_at"),
            reading_meta={**source_meta, "source_read_at": now.isoformat()},
            empty_values={"transport_notice_count": 0.0},
        )
