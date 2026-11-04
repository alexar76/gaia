"""NOAA storm reports and ash advisories, and Geoscience Australia hotspots."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.live_transport_events import _geometry, _utc
from gaia.source_policy import require_approved_source


class _HazardCluster(LiveDevice):
    retain_hotspots_in_history = False

    def _text(self, url: str, *, max_bytes: int) -> str:
        require_approved_source(self.policy_id).require_endpoint(url)
        try:
            return self._fetch_bytes(url, max_bytes=max_bytes).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DeviceOffline(f"{self.device_id}: invalid upstream text encoding") from exc

    def _sign(
        self, rows: list[dict], now: datetime, *, headline: dict[str, float],
        meta: tuple[str, ...] = (), geometry: bool = False,
    ) -> dict:
        if len(json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) > 820_000:
            raise DeviceOffline(f"{self.device_id}: snapshot exceeds the response budget")
        return signed_cluster_read(
            self, rows,
            numeric_keys=("latitude", "longitude", *self.row_fields),
            meta_keys=("event_id", "title", "claim_class", "valid_until", *meta),
            structured_keys=("geometry", "event_bbox") if geometry else (),
            headline_values={"snapshot_unix": now.timestamp(), **headline},
            reading_meta={"source_read_at": now.isoformat()},
        )


_SPC_HAZARDS = {"F_Scale": "Tornado", "Speed": "Wind", "Size": "Hail"}
_SPC_TAIL = ["Location", "County", "State", "Lat", "Lon", "Comments"]
_SPC_WINDOW = timedelta(hours=24)
_PLACE = re.compile(r"[A-Za-z0-9 .,'()/-]{1,100}")


def _spc_day(when: datetime) -> date:
    return (when - timedelta(hours=12)).date()


def _storm_rows(payload: str, day: date, now: datetime) -> list[dict]:
    rows = []
    seen_sections = []
    kind = ""
    for row in csv.reader(io.StringIO(payload)):
        if not row or not any(row):
            continue
        if row[0] == "Time":
            if len(row) != 8 or row[1] not in _SPC_HAZARDS or row[2:] != _SPC_TAIL:
                raise ValueError("unexpected SPC section header")
            seen_sections.append(row[1])
            kind = _SPC_HAZARDS[row[1]]
            continue
        if not kind or len(row) < 8 or not re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9]", row[0]):
            raise ValueError("unexpected SPC report record")
        hour, minute = int(row[0][:2]), int(row[0][2:])
        observed = datetime.combine(day + timedelta(days=hour < 12), time(hour, minute), tzinfo=UTC)
        if not now - _SPC_WINDOW < observed <= now:
            continue
        lat, lon = _num(row[5]), _num(row[6])
        if lat is None or lon is None or not (15 <= lat <= 75 and -180 <= lon <= -60):
            # Reports occasionally have missing coordinates; don't guess a location.
            continue
        place, state = row[2].strip(), row[4].strip()
        if not _PLACE.fullmatch(place) or not re.fullmatch(r"[A-Z]{2}", state):
            continue
        event_id = hashlib.sha256(f"{kind}|{observed.isoformat()}|{lat}|{lon}|{place}|{state}".encode()).hexdigest()[:24]
        fields = {}
        magnitude = _num(row[1])
        if magnitude is not None:
            if kind == "Wind" and 0 < magnitude <= 300:
                fields["wind_mps"] = magnitude * 0.44704  # SPC reports integer mph.
            elif kind == "Hail" and 0 < magnitude <= 1200:
                fields["hail_size_mm"] = magnitude * 0.254  # Hundredths of an inch.
        rows.append({
            "event_id": event_id, "title": f"{kind} · {place}, {state}",
            "claim_class": "preliminary_storm_report", "notice_type": kind,
            "latitude": lat, "longitude": lon, "observation_unix": observed.timestamp(),
            "observed_at": observed.isoformat(), "valid_until": (observed + _SPC_WINDOW).isoformat(),
            **fields,
        })
    if seen_sections != list(_SPC_HAZARDS):
        raise ValueError("incomplete SPC storm report")
    return rows


class SpcStormReports(_HazardCluster):
    """Preliminary tornado, severe wind and hail reports in the last 24 hours."""

    model = "GAIA-STORM-REPORTS (NOAA SPC)"
    policy_id = "noaa_spc_storm_reports"
    _root = "https://www.spc.noaa.gov/climo/reports/"
    url = _root + "today.csv"
    attribution = "NOAA/NWS Storm Prediction Center · U.S. Government public domain"
    source = (
        f"{url} (NOAA/NWS SPC. Structured preliminary local storm reports. "
        "Times are reconstructed from SPC's 12 UTC reporting day. Free-text comments are omitted.)"
    )
    row_fields = ("observation_unix", "wind_mps", "hail_size_mm")
    fields = {
        "latitude": "deg", "longitude": "deg", "observation_unix": "s", "wind_mps": "m/s",
        "hail_size_mm": "mm", "storm_report_count": "count", "snapshot_unix": "s",
    }

    def read(self) -> dict:
        now = datetime.now(UTC)
        day = _spc_day(now)
        previous = day - timedelta(days=1)
        archives = (
            (previous, self._root + previous.strftime("%y%m%d") + "_rpts.csv"),
            (day, self.url),
        )
        rows: dict[str, dict] = {}
        for report_day, url in archives:
            payload = self._text(url, max_bytes=1_500_000)
            try:
                rows.update((row["event_id"], row) for row in _storm_rows(payload, report_day, now))
            except (csv.Error, ValueError) as exc:
                raise DeviceOffline(f"{self.device_id}: malformed SPC reports") from exc
        if _spc_day(datetime.now(UTC)) != day:
            raise DeviceOffline(f"{self.device_id}: SPC day rolled over during retrieval")
        ordered = sorted(rows.values(), key=lambda row: (-row["observation_unix"], row["event_id"]))
        return self._sign(ordered, now, headline={"storm_report_count": len(ordered)},
                          meta=("notice_type", "observed_at"))


_VAAC_PATH = "/products/atmosphere/vaac/"
_VAAC_XML = re.compile(
    r"""href\s*=\s*["'](/products/atmosphere/vaac/volcanoes/xml_files/FVXX[0-9]{2}_([0-9]{8}_[0-9]{4})\.xml)["']""",
    re.IGNORECASE,
)
_VAAC_CENTRE = "WASHINGTON"
_VAAC_WINDOW = timedelta(hours=7)
_IWXXM = "{http://icao.int/iwxxm/3.0}"
_GML = "{http://www.opengis.net/gml/3.2}"
_AIXM = "{http://www.aixm.aero/schema/5.1.1}"
_METCE = "{http://def.wmo.int/metce/2013}"
_COLLECT = "{http://def.wmo.int/collect/2014}"
_CRS = "http://www.opengis.net/def/crs/EPSG/0/4326"


def _vaac_time(node: ET.Element, name: str) -> datetime | None:
    return _utc(node.findtext(_IWXXM + name + f"/{_GML}TimeInstant/{_GML}timePosition"))


def _vaac_altitude(element: ET.Element | None, reference: str | None) -> int | None:
    if element is None:
        return None
    raw = (element.text or "").strip()
    if raw in {"SFC", "GND"} and reference in {"SFC", None}:
        return 0
    if not re.fullmatch(r"[0-9]{1,5}", raw):
        return None
    if element.get("uom") == "FL" and reference == "STD":
        return int(raw) * 100 if int(raw) <= 800 else None
    # AIXM height in feet without an MSL datum isn't a flight level.
    return None


def _vaac_polygons(volume: ET.Element) -> list[dict]:
    shapes = []
    for surface in volume.findall(f"{_AIXM}horizontalProjection/{_AIXM}Surface"):
        if (surface.get("srsName") != _CRS or surface.get("srsDimension") != "2"
                or surface.get("axisLabels") != "Lat Long"):
            continue
        for patch in surface.findall(f"{_GML}patches/{_GML}PolygonPatch"):
            rings = []
            for outline in patch:
                if outline.tag not in {_GML + "exterior", _GML + "interior"}:
                    break
                pos = outline.find(f"{_GML}LinearRing/{_GML}posList")
                tokens = (pos.text or "").split() if pos is not None else []
                if not 8 <= len(tokens) <= 1000 or len(tokens) % 2:
                    break
                if pos is not None and pos.get("count") not in (None, str(len(tokens) // 2)):
                    break
                rings.append([[tokens[index + 1], tokens[index]] for index in range(0, len(tokens), 2)])
            else:
                if not rings or patch[0].tag != _GML + "exterior":
                    continue
                shape = _geometry({"type": "Polygon", "coordinates": rings})
                if shape is not None and all(len({tuple(pt) for pt in ring}) >= 3 for ring in shape["geometry"]["coordinates"]):
                    shapes.append(shape)
    return shapes


def _vaac_rows(
    advisory: ET.Element, volcano: str, name: str, issue: datetime, now: datetime,
) -> list[dict]:
    number = advisory.findtext(_IWXXM + "advisoryNumber", "")
    if not re.fullmatch(r"[0-9]{4}/[0-9]{1,6}", number):
        raise ValueError("invalid advisory number")
    next_advisory = _vaac_time(advisory, "nextAdvisoryTime")
    if not next_advisory or advisory.get("reportStatus") in {"CANCELLATION", "CANCELLED"}:
        # A final / cancelled bulletin replaces all shapes from that volcano's
        # prior bulletin, including any longer-range forecasts.
        return []
    expires = min(next_advisory, issue + _VAAC_WINDOW)
    if not issue <= now < expires:
        return []
    rows = []
    for kind, tag, eligible in (
        ("observation", "VolcanicAshObservedOrEstimatedConditions", "IDENTIFIABLE"),
        ("forecast", "VolcanicAshForecastConditions", "PROVIDED"),
    ):
        for condition in advisory.findall(f"{_IWXXM}{kind}/{_IWXXM}{tag}"):
            if condition.get("status") != eligible:
                continue
            valid = _vaac_time(condition, "phenomenonTime")
            if not valid:
                continue
            observed = kind == "observation"
            if observed and not issue - timedelta(hours=3) <= valid <= issue:
                continue
            if not observed and not max(now, issue) <= valid <= issue + timedelta(hours=20):
                continue
            claim = ("estimated_ash" if condition.get("isEstimated") != "false" else "observed_ash") if observed else "ash_forecast"
            title_kind = {"estimated_ash": "Estimated ash", "observed_ash": "Observed ash", "ash_forecast": "Ash forecast"}[claim]
            volumes = condition.findall(f".//{_IWXXM}ashCloudExtent/{_AIXM}AirspaceVolume")
            for vi, volume in enumerate(volumes):
                base = _vaac_altitude(volume.find(_AIXM + "lowerLimit"), volume.findtext(_AIXM + "lowerLimitReference"))
                top = _vaac_altitude(volume.find(_AIXM + "upperLimit"), volume.findtext(_AIXM + "upperLimitReference"))
                for pi, shape in enumerate(_vaac_polygons(volume)):
                    key = f"{volcano}|{number}|{claim}|{valid.isoformat()}|{vi}|{pi}"
                    row = {
                        **shape, "event_id": hashlib.sha256(key.encode()).hexdigest()[:24],
                        "title": f"{name} · {title_kind}", "claim_class": claim, "notice_type": title_kind,
                        "issue_at": issue.isoformat(), "valid_at": valid.isoformat(),
                        "valid_until": expires.isoformat(), "advisory_number": number,
                        "base_ft": base, "top_ft": top,
                    }
                    if observed:
                        row["observation_unix"] = valid.timestamp()
                        row["observed_at"] = valid.isoformat()
                    else:
                        row["forecast_hour"] = (valid - issue).total_seconds() / 3600
                    rows.append(row)
    return rows


def _vaac_advisory(raw: str) -> tuple[ET.Element, str, str, datetime]:
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", raw, re.IGNORECASE):
        raise ValueError("unexpected XML declaration")
    bulletin = ET.fromstring(raw)
    if bulletin.tag != _COLLECT + "MeteorologicalBulletin":
        raise ValueError("expected WMO meteorological bulletin")
    identifier = bulletin.findtext(_COLLECT + "bulletinIdentifier", "").strip()
    if not re.fullmatch(r"A_LUXX[0-9]{2}KNES[0-9]{6}(?:[A-Z]{3})?_C_KNES_[0-9]{14}\.xml", identifier):
        raise ValueError("expected NOAA KNES bulletin identifier")
    advisory = bulletin.find(f"{_COLLECT}meteorologicalInformation/{_IWXXM}VolcanicAshAdvisory")
    if advisory is None or advisory.get("permissibleUsage") != "OPERATIONAL":
        raise ValueError("expected operational IWXXM volcanic ash advisory")
    centre = advisory.find(f"{_IWXXM}issuingVolcanicAshAdvisoryCentre")
    if centre is None or centre.findtext(f".//{_AIXM}name", "").strip() != _VAAC_CENTRE:
        raise ValueError("unexpected volcanic ash advisory centre")
    label = advisory.findtext(
        f"{_IWXXM}volcano/{_METCE}EruptingVolcano/{_METCE}name", "",
    )
    match = re.fullmatch(r"([A-Z][A-Z0-9 .'-]{0,90}) ([0-9]{6})", label.strip())
    issue = _vaac_time(advisory, "issueTime")
    if not match or not issue:
        raise ValueError("volcano name, identity or issue time missing")
    return advisory, match[2], match[1].title(), issue


class WashingtonVaacAsh(_HazardCluster):
    """Latest NOAA Washington VAAC operational advisory for each volcano."""

    model = "GAIA-VOLCANIC-ASH (NOAA Washington VAAC)"
    policy_id = "noaa_washington_vaac"
    _root = "https://www.ospo.noaa.gov"
    url = _root + _VAAC_PATH + "messages.html"
    attribution = "NOAA/NESDIS Washington Volcanic Ash Advisory Center · U.S. Government public domain"
    source = (
        f"{url} (NOAA/NESDIS Washington VAAC IWXXM operational advisories. "
        "Observation, estimate and forecast geometry are distinguished. For situational awareness; "
        "consult the official VAAC advisory for flight decisions.)"
    )
    row_fields = ("observation_unix", "base_ft", "top_ft", "forecast_hour")
    fields = {
        "latitude": "deg", "longitude": "deg", "observation_unix": "s",
        "base_ft": "ft", "top_ft": "ft", "forecast_hour": "h",
        "ash_shape_count": "count", "snapshot_unix": "s",
    }

    def read(self) -> dict:
        now = datetime.now(UTC)
        index = self._text(self.url, max_bytes=600_000)
        if "Advisories from the past 24 hours" not in index:
            raise DeviceOffline(f"{self.device_id}: unrecognized Washington VAAC index")
        paths: dict[str, datetime] = {}
        for path, stamp in _VAAC_XML.findall(index):
            try:
                file_time = datetime.strptime(stamp, "%Y%m%d_%H%M").replace(tzinfo=UTC)
            except ValueError:
                continue
            if now - _VAAC_WINDOW < file_time <= now:
                paths[path] = file_time
        if len(paths) > 36:
            raise DeviceOffline(f"{self.device_id}: too many current advisories")
        latest: dict[str, tuple[datetime, str, ET.Element]] = {}

        def fetch(path: str):
            advisory, volcano, name, issue = _vaac_advisory(self._text(self._root + path, max_bytes=150_000))
            if issue.replace(second=0, microsecond=0) != paths[path]:
                raise ValueError("filename and issue time disagree")
            return advisory, volcano, name, issue

        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                for advisory, volcano, name, issue in pool.map(fetch, sorted(paths)):
                    prior = latest.get(volcano)
                    if prior is None or (issue, advisory.get("reportStatus") == "CORRECTION") > (
                        prior[0], prior[2].get("reportStatus") == "CORRECTION",
                    ):
                        latest[volcano] = (issue, name, advisory)
            rows = [
                row for volcano, (issue, name, advisory) in sorted(latest.items())
                for row in _vaac_rows(advisory, volcano, name, issue, now)
            ]
        except (ET.ParseError, ValueError) as exc:
            raise DeviceOffline(f"{self.device_id}: malformed Washington VAAC advisory") from exc
        return self._sign(
            rows, now, headline={"ash_shape_count": len(rows)}, geometry=True,
            meta=("issue_at", "valid_at", "notice_type", "advisory_number", "observed_at"),
        )


_DEA_AUSTRALIA = (110.0, -45.0, 155.0, -9.0)
_DEA_WINDOW = timedelta(hours=3)
_DEA_PAGE = 200
_DEA_MAX = 800
_DEA_TOKEN = re.compile(r"[A-Za-z0-9 ._+/-]{1,60}")


class DeaHotspots(_HazardCluster):
    """Bounded, latest-first DEA thermal detections within an Australian bbox."""

    model = "GAIA-HOTSPOTS (Digital Earth Australia)"
    policy_id = "dea_hotspots"
    url = "https://hotspots.dea.ga.gov.au/geoserver/ows"
    attribution = "Geoscience Australia, 2020. Digital Earth Australia Hotspots dataset. Geoscience Australia, Canberra. CC BY 4.0"
    source = (
        f"{url} (Digital Earth Australia Hotspots dataset, https://pid.geoscience.gov.au/dataset/ga/111881. "
        "Geoscience Australia, CC BY 4.0. Satellite thermal detections with a three-hour age limit.)"
    )
    row_fields = ("brightness_k", "confidence", "observation_unix")
    fields = {
        "latitude": "deg", "longitude": "deg", "brightness_k": "K", "confidence": "pct",
        "observation_unix": "s", "snapshot_unix": "s", "hotspot_matched": "count",
        "coverage_complete": "bool", "bbox_west_deg": "deg", "bbox_south_deg": "deg",
        "bbox_east_deg": "deg", "bbox_north_deg": "deg",
    }

    def _page(self, bbox: tuple[float, float, float, float], now: datetime, offset: int, size: int) -> dict:
        scope = ",".join(f"{value:.4f}" for value in bbox)
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (now - _DEA_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = self.url + "?" + urlencode({
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "public:hotspots_three_days", "outputFormat": "application/json",
            "count": size, "startIndex": offset, "sortBy": "datetime D,id A",
            "propertyName": "location,id,satellite,sensor,temp_kelvin,datetime,confidence",
            "CQL_FILTER": f"BBOX(geometry,{scope},'EPSG:4326') AND datetime AFTER {start}"
                          f" AND datetime BEFORE {end} AND load_dt BEFORE {end}",
        })
        try:
            data = json.loads(self._text(url, max_bytes=400_000))
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: malformed DEA hotspot page") from exc
        if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
            raise DeviceOffline(f"{self.device_id}: expected DEA feature collection")
        return data

    @staticmethod
    def _row(feature: object, bbox: tuple[float, float, float, float], now: datetime) -> dict | None:
        if not isinstance(feature, dict):
            return None
        props, geometry = feature.get("properties"), feature.get("geometry")
        if not isinstance(props, dict) or not isinstance(geometry, dict) or geometry.get("type") != "Point":
            return None
        point = _geometry(geometry)
        observed = _utc(props.get("datetime"))
        event = props.get("id")
        if not point or type(event) is not int or event <= 0 or not observed or not now - _DEA_WINDOW < observed <= now:
            return None
        lon, lat = point["longitude"], point["latitude"]
        if not bbox[0] - 0.00005 <= lon <= bbox[2] + 0.00005 or not bbox[1] - 0.00005 <= lat <= bbox[3] + 0.00005:
            return None
        satellite, sensor = props.get("satellite"), props.get("sensor")
        if not isinstance(satellite, str) or not _DEA_TOKEN.fullmatch(satellite):
            return None
        if not isinstance(sensor, str) or not _DEA_TOKEN.fullmatch(sensor):
            return None
        row = {
            "event_id": str(event), "title": f"Thermal detection · {satellite}",
            "claim_class": "satellite_thermal_detection", "satellite": satellite, "sensor": sensor,
            "latitude": lat, "longitude": lon, "observation_unix": observed.timestamp(),
            "observed_at": observed.isoformat(), "valid_until": (observed + _DEA_WINDOW).isoformat(),
        }
        brightness, confidence = _num(props.get("temp_kelvin")), _num(props.get("confidence"))
        if brightness is not None and 100 <= brightness <= 1500:
            row["brightness_k"] = brightness
        if confidence is not None and 0 <= confidence <= 100:
            row["confidence"] = confidence
        return row

    def read_bbox(
        self, west: float, south: float, east: float, north: float, *, limit: int = _DEA_MAX,
    ) -> dict:
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("invalid DEA bbox; split antimeridian bboxes")
        if type(limit) is not int or not 1 <= limit <= _DEA_MAX:
            raise ValueError(f"DEA limit must be 1–{_DEA_MAX}")
        now = datetime.now(UTC).replace(microsecond=0)
        bbox = tuple(round(value, 4) for value in (west, south, east, north))
        aw, as_, ae, an = _DEA_AUSTRALIA
        query_box = (max(aw, bbox[0]), max(as_, bbox[1]), min(ae, bbox[2]), min(an, bbox[3]))
        rows: dict[str, dict] = {}
        fetched = 0
        matched = 0
        if query_box[0] < query_box[2] and query_box[1] < query_box[3]:
            while fetched < limit:
                count = min(_DEA_PAGE, limit - fetched)
                data = self._page(query_box, now, fetched, count)
                features = data.get("features")
                reported = data.get("numberMatched")
                if (not isinstance(features, list) or not 0 <= len(features) <= count
                        or type(reported) is not int or reported < fetched + len(features)
                        or (fetched and reported != matched)):
                    raise DeviceOffline(f"{self.device_id}: inconsistent DEA pagination")
                matched = reported
                for feature in features:
                    row = self._row(feature, query_box, now)
                    if row is not None:
                        rows[row["event_id"]] = row
                fetched += len(features)
                if fetched == matched:
                    break
                if len(features) < count:
                    raise DeviceOffline(f"{self.device_id}: truncated DEA hotspot page")
        scope = {
            "bbox_west_deg": bbox[0], "bbox_south_deg": bbox[1],
            "bbox_east_deg": bbox[2], "bbox_north_deg": bbox[3],
        }
        return self._sign(
            list(rows.values()), now,
            headline={"coverage_complete": int(fetched == matched == len(rows)), "hotspot_matched": matched, **scope},
            meta=("observed_at", "satellite", "sensor"),
        )

    def read(self) -> dict:
        return self.read_bbox(*_DEA_AUSTRALIA)
