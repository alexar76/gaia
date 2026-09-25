"""P6 commercially-clear LIVE relays.

Environment Agency hydrology (OGL v3), Rijkswaterstaat WaterWebservices
(CC0), USACE CWMS (U.S. public domain), EPA UV forecasts (U.S. public
domain), and Met Éireann observations (CC BY 4.0).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from gaia.clock import SimClock
from gaia.devices._live_base import _UA
from gaia.devices._policy import _assert_url_allowed
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.devices.live_p0 import signed_cluster_read
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p6")

_SAFE_GUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
_SAFE_RWS = re.compile(r"^[a-z0-9][a-z0-9.-]{2,79}$", re.I)
_SAFE_CWMS = re.compile(r"^[A-Z0-9_-]{2,16}$")
_SAFE_ZIP = re.compile(r"^[0-9]{5}$")
_FT_TO_M = 0.3048
_ACFT_TO_M3 = 1233.4818375475


def _iso_epoch(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _items(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = [rows]
    return [r for r in (rows or []) if isinstance(r, dict)] if isinstance(rows, list) else []


class EaHydrologyRiver(LiveDevice):
    """Environment Agency Hydrology API point gauge — England, OGL v3."""

    model = "GAIA-RIVER (EA Hydrology)"
    policy_id = "ea_hydrology"
    fields = {
        "discharge_m3s": "m3/s",
        "gage_height_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Environment Agency Hydrology API · Open Government Licence v3. "
        "England hydrology observations only — not flood warnings, SEPA, or NRW."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        guid: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        if not _SAFE_GUID.fullmatch(guid or ""):
            raise ValueError(f"invalid EA station GUID: {guid!r}")
        self.guid = guid.lower()
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = f"https://environment.data.gov.uk/hydrology/id/stations/{self.guid}.json"
        require_approved_source(self.policy_id).require_endpoint(self.url)

    @staticmethod
    def _measure_ids(station: Any) -> list[str]:
        rows = _items(station)
        measures = rows[0].get("measures") if rows else []
        out: list[str] = []
        for row in measures if isinstance(measures, list) else []:
            if not isinstance(row, dict):
                continue
            measure_id = str(row.get("notation") or row.get("@id") or "").rsplit("/", 1)[-1]
            if measure_id.endswith(("-flow-i-900-m3s-qualified", "-level-i-900-m-qualified")):
                out.append(measure_id)
        return out

    @staticmethod
    def _latest_value(payload: Any) -> tuple[float | None, float | None]:
        newest: tuple[float, float] | None = None
        for row in _items(payload):
            value = _num(row.get("value"))
            when = _iso_epoch(row.get("dateTime") or row.get("date"))
            if value is None:
                continue
            candidate = (when if when is not None else 0.0, float(value))
            if newest is None or candidate[0] > newest[0]:
                newest = candidate
        return (newest[1], newest[0]) if newest else (None, None)

    def map(self, payload: Any) -> dict[str, float | None]:
        station, readings = payload if isinstance(payload, tuple) and len(payload) == 2 else ({}, {})
        rows = _items(station)
        meta = rows[0] if rows else {}
        lat = _num(meta.get("lat")) or self.latitude
        lon = _num(meta.get("long")) or self.longitude
        flow = level = None
        for measure_id, reading in readings.items() if isinstance(readings, dict) else []:
            value, _when = self._latest_value(reading)
            if measure_id.endswith("-flow-i-900-m3s-qualified"):
                flow = value
            elif measure_id.endswith("-level-i-900-m-qualified"):
                level = value
        if flow is None and level is None:
            raise DeviceOffline(f"{self.device_id}: EA Hydrology has no instantaneous flow/level")
        return {
            "discharge_m3s": flow,
            "gage_height_m": level,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        station = self._fetch(self.url)
        readings: dict[str, Any] = {}
        for measure_id in self._measure_ids(station):
            url = (
                "https://environment.data.gov.uk/hydrology/id/measures/"
                f"{quote(measure_id, safe='')}/readings.json?latest"
            )
            require_approved_source(self.policy_id).require_endpoint(url)
            try:
                readings[measure_id] = self._fetch(url)
            except DeviceOffline:
                continue
        return {k: v for k, v in self.map((station, readings)).items() if v is not None}


class RwsRiver(LiveDevice):
    """Rijkswaterstaat latest river observations — Netherlands, CC0."""

    model = "GAIA-RIVER (RWS WaterWebservices)"
    policy_id = "rws_water"
    fields = EaHydrologyRiver.fields
    source = (
        "Rijkswaterstaat WaterWebservices · CC0. Netherlands observations only — "
        "not German PEGELONLINE."
    )
    url = (
        "https://ddapi20-waterwebservices.rijkswaterstaat.nl/"
        "ONLINEWAARNEMINGENSERVICES/OphalenLaatsteWaarnemingen"
    )
    timeout = 25.0

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        location_code: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        if not _SAFE_RWS.fullmatch(location_code or ""):
            raise ValueError(f"invalid RWS location code: {location_code!r}")
        self.location_code = location_code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def _fetch(self, url: str) -> Any:
        _assert_url_allowed(url)
        body = {
            "LocatieLijst": [{"Code": self.location_code}],
            "AquoPlusWaarnemingMetadataLijst": [
                {"AquoMetadata": {"Compartiment": {"Code": "OW"}, "Grootheid": {"Code": code}}}
                for code in ("WATHTE", "Q")
            ],
        }
        try:
            response = httpx.post(
                url,
                json=body,
                headers={"User-Agent": _UA, "Accept": "application/json"},
                timeout=self.timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise DeviceOffline(f"{self.device_id}: RWS upstream unreachable") from exc
        if response.status_code != 200:
            raise DeviceOffline(f"{self.device_id}: RWS upstream HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: RWS upstream returned non-JSON") from exc

    @staticmethod
    def _code(row: dict[str, Any]) -> str:
        plus = (
            row.get("AquoPlusWaarnemingMetadata")
            if isinstance(row.get("AquoPlusWaarnemingMetadata"), dict)
            else row
        )
        meta = plus.get("AquoMetadata") if isinstance(plus.get("AquoMetadata"), dict) else {}
        grootheid = meta.get("Grootheid") if isinstance(meta.get("Grootheid"), dict) else {}
        return str(grootheid.get("Code") or row.get("Grootheid") or "")

    def map(self, payload: Any) -> dict[str, float | None]:
        groups = payload.get("WaarnemingenLijst") if isinstance(payload, dict) else []
        now = datetime.now(timezone.utc).timestamp()
        newest: dict[str, tuple[float, float]] = {}
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            code = self._code(group)
            measurements = group.get("MetingenLijst") or group.get("Waarnemingen") or []
            for row in measurements if isinstance(measurements, list) else []:
                if not isinstance(row, dict):
                    continue
                when = _iso_epoch(row.get("Tijdstip"))
                measure = row.get("Meetwaarde") if isinstance(row.get("Meetwaarde"), dict) else row
                value = _num(
                    measure.get("Waarde_Numeriek")
                    if isinstance(measure, dict)
                    else None
                )
                if when is None or value is None or now - when > 3 * 86400:
                    continue
                if code not in newest or when > newest[code][0]:
                    newest[code] = (when, float(value))
        level = newest.get("WATHTE", (0.0, None))[1]
        flow = newest.get("Q", (0.0, None))[1]
        if level is None and flow is None:
            raise DeviceOffline(f"{self.device_id}: RWS has no reading newer than 3 days")
        return {
            "discharge_m3s": flow,
            "gage_height_m": float(level) / 100.0 if level is not None else None,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


class UsaceReservoir(LiveDevice):
    """USACE CWMS reservoir pool elevation and storage — U.S. public domain."""

    model = "GAIA-RESERVOIR (USACE CWMS)"
    policy_id = "usace_cwms"
    fields = {
        "pool_elev_m": "m",
        "storage_m3": "m3",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = "U.S. Army Corps of Engineers CWMS Data API · U.S. Government public domain."

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        name: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (name or "").strip().upper()
        if not _SAFE_CWMS.fullmatch(code):
            raise ValueError(f"invalid CWMS location: {name!r}")
        self.name = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = self._series_url("Elev")
        self.headers = {"Accept": "application/json;version=2"}
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(self.url)
        policy.require_endpoint(self._series_url("Stor"))

    def _series_url(self, kind: str) -> str:
        series = f"{self.name}.{kind}.Inst.1Hour.0.Ccp-Rev"
        return (
            "https://cwms-data.usace.army.mil/cwms-data/timeseries"
            f"?name={quote(series, safe='.-')}&office=SWT"
        )

    @staticmethod
    def _last_value(payload: Any) -> float | None:
        rows = payload.get("values") if isinstance(payload, dict) else []
        for row in reversed(rows if isinstance(rows, list) else []):
            if isinstance(row, (list, tuple)) and len(row) > 1:
                value = _num(row[1])
                if value is not None:
                    return float(value)
        return None

    def map(self, payload: Any) -> dict[str, float | None]:
        elev, storage = payload if isinstance(payload, tuple) and len(payload) == 2 else ({}, {})
        elev_ft = self._last_value(elev)
        storage_acft = self._last_value(storage)
        if elev_ft is None and storage_acft is None:
            raise DeviceOffline(f"{self.device_id}: USACE CWMS Elev/Stor empty")
        return {
            "pool_elev_m": elev_ft * _FT_TO_M if elev_ft is not None else None,
            "storage_m3": storage_acft * _ACFT_TO_M3 if storage_acft is not None else None,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        elev = storage = {}
        try:
            elev = self._fetch(self._series_url("Elev"))
        except DeviceOffline:
            pass
        try:
            storage = self._fetch(self._series_url("Stor"))
        except DeviceOffline:
            pass
        return {k: v for k, v in self.map((elev, storage)).items() if v is not None}


EPA_UV_MESH: tuple[tuple[str, str, float, float], ...] = (
    ("10001", "New York", 40.7128, -74.0060),
    ("90012", "Los Angeles", 34.0522, -118.2437),
    ("60601", "Chicago", 41.8781, -87.6298),
    ("77002", "Houston", 29.7604, -95.3698),
    ("85001", "Phoenix", 33.4484, -112.0740),
    ("19103", "Philadelphia", 39.9526, -75.1652),
    ("78205", "San Antonio", 29.4241, -98.4936),
    ("92101", "San Diego", 32.7157, -117.1611),
    ("75201", "Dallas", 32.7767, -96.7970),
    ("94102", "San Francisco", 37.7749, -122.4194),
)


class EpaUvIndex(LiveDevice):
    """EPA Envirofacts hourly UV forecast cluster — U.S. public domain."""

    model = "GAIA-UV (EPA forecast)"
    policy_id = "epa_uv"
    fields = {"uv_index": "index", "latitude": "deg", "longitude": "deg"}
    source = (
        "U.S. EPA Envirofacts UV hourly forecast · U.S. Government public domain. "
        "Forecast product — not an in-situ pyranometer."
    )
    url = "https://data.epa.gov/efservice/getEnvirofactsUVHOURLY/ZIP/10001/JSON"
    timeout = 10.0

    def __init__(self, device_id: str, clock: SimClock, **kw: Any):
        super().__init__(device_id, clock, **kw)
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        by_zip = payload if isinstance(payload, dict) else {}
        hotspots: list[dict[str, Any]] = []
        mesh = {z: (city, lat, lon) for z, city, lat, lon in EPA_UV_MESH}
        for zip_code, rows in by_zip.items():
            if zip_code not in mesh or not isinstance(rows, list):
                continue
            best: tuple[float, dict[str, Any]] | None = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                value = _num(row.get("UV_VALUE") if row.get("UV_VALUE") is not None else row.get("UV_INDEX"))
                if value is None:
                    continue
                if best is None or float(value) > best[0]:
                    best = (float(value), row)
            if best is None:
                continue
            city, lat, lon = mesh[zip_code]
            row = best[1]
            hotspots.append({
                "uv_index": best[0],
                "latitude": lat,
                "longitude": lon,
                "zip": zip_code,
                "city": city,
                "hour_max": str(row.get("UV_HOUR") or row.get("HOUR") or row.get("DATE_TIME") or "")[:32],
            })
        if not hotspots:
            raise DeviceOffline(f"{self.device_id}: EPA UV forecast empty")
        hotspots.sort(key=lambda row: float(row["uv_index"]), reverse=True)
        return hotspots[: max(1, min(int(limit or len(hotspots)), len(EPA_UV_MESH)))]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {key: float(row[key]) for key in self.fields}

    def read(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for zip_code, _city, _lat, _lon in EPA_UV_MESH:
            url = f"https://data.epa.gov/efservice/getEnvirofactsUVHOURLY/ZIP/{zip_code}/JSON"
            try:
                payload[zip_code] = self._fetch(url)
            except DeviceOffline:
                continue
        return signed_cluster_read(
            self,
            self.collect_hotspots(payload),
            numeric_keys=("uv_index", "latitude", "longitude"),
            meta_keys=("zip", "city", "hour_max"),
        )


MET_IE_STATIONS: dict[str, tuple[float, float]] = {
    "Athenry": (53.289, -8.786),
    "Ballyhaise": (54.050, -7.317),
    "Belmullet": (54.228, -10.007),
    "Casement": (53.306, -6.439),
    "Claremorris": (53.711, -8.992),
    "Cork": (51.847, -8.486),
    "Dublin": (53.428, -6.241),
    "Finner": (54.494, -8.243),
    "Johnstown Castle": (52.298, -6.497),
    "Knock": (53.906, -8.817),
    "Mace Head": (53.326, -9.899),
    "Malin Head": (55.372, -7.339),
    "Roche's Point": (51.793, -8.244),
    "Shannon": (52.690, -8.918),
    "Valentia": (51.939, -10.241),
}


class MetEireannObs(LiveDevice):
    """Met Éireann current synoptic observation cluster — CC BY 4.0."""

    model = "GAIA-WEATHER (Met Éireann)"
    policy_id = "met_eireann"
    fields = {
        "temperature_c": "C",
        "humidity_pct": "pct",
        "wind_mps": "m/s",
        "rainfall_mm": "mm",
        "pressure_hpa": "hPa",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Contains modified Met Éireann observations; source: https://www.met.ie; "
        "© Met Éireann; licensed under CC BY 4.0 "
        "(https://creativecommons.org/licenses/by/4.0/); changes are relay mapping/unit "
        "conversion; no endorsement implied."
    )
    url = "https://www.met.ie/latest-reports/observations/download"

    def __init__(self, device_id: str, clock: SimClock, **kw: Any):
        super().__init__(device_id, clock, **kw)
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.headers = {"Accept": "text/csv"}

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        text = payload if isinstance(payload, str) else ""
        hotspots: list[dict[str, Any]] = []
        for row in csv.DictReader(io.StringIO(text)):
            station = str(row.get("Station") or "").strip()
            coords = MET_IE_STATIONS.get(station)
            temp = _num(row.get("Temperature (ºC)"))
            if coords is None or temp is None:
                continue
            item: dict[str, Any] = {
                "temperature_c": float(temp),
                "latitude": coords[0],
                "longitude": coords[1],
                "station": station,
            }
            for source_key, target_key in (
                ("Humidity (%)", "humidity_pct"),
                ("Rainfall (mm)", "rainfall_mm"),
                ("Pressure (hPa)", "pressure_hpa"),
            ):
                value = _num(row.get(source_key))
                if value is not None:
                    item[target_key] = float(value)
            wind = _num(row.get("Wind Speed (Kts)"))
            if wind is not None:
                item["wind_mps"] = float(wind) * 0.514444
            hotspots.append(item)
        if not hotspots:
            raise DeviceOffline(f"{self.device_id}: Met Éireann CSV has no mapped stations")
        hotspots.sort(key=lambda row: (row["station"] != "Dublin", row["station"]))
        return hotspots[: max(1, min(int(limit or len(hotspots)), 100))]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {key: (_num(row.get(key))) for key in self.fields}

    def read(self) -> dict[str, Any]:
        hotspots = self.collect_hotspots(self._fetch_text(self.url))
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=tuple(self.fields),
            meta_keys=("station",),
        )


EA_HYDROLOGY_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("ea-kingston-01", "8496ce69-482c-406a-a2f0-ac418ef8f099", 51.415482, -0.307629, "Thames at Kingston"),
    ("ea-reading-01", "f44bf96d-3953-4fec-88bd-30ef4e12e523", 51.461010, -0.967862, "Thames at Reading"),
    ("ea-bewdley-01", "8820d897-a09e-4857-8095-5834fee6962f", 52.383072, -2.321186, "Severn at Bewdley"),
    ("ea-northmuskham-01", "15eae722-7f1f-41cb-a93e-2acdeca10a0a", 53.136170, -0.799124, "Trent at North Muskham"),
    ("ea-armley-01", "655a16a0-8498-4b83-a8f6-6816952b2262", 53.801967, -1.575189, "Aire at Armley"),
    ("ea-bywell-01", "e786e60f-a0f1-4955-aa57-f22ba39c7427", 54.949776, -1.940430, "Tyne at Bywell"),
    ("ea-skelton-01", "213d70b2-894b-406b-9dc3-31d3ccec7f54", 53.991268, -1.134472, "Ouse at Skelton"),
    ("ea-windsor-01", "6c72f76a-b76c-4a28-8701-71cdca25c0f5", 51.485663, -0.589344, "Thames at Windsor"),
    ("ea-maidenhead-01", "79515906-3efd-424e-8e32-0db1873cb3a3", 51.52419, -0.70203, "Thames at Maidenhead"),
    ("ea-walton-01", "b92a2ca3-4eb9-4a8f-b82f-8bbc2a1dfbc9", 51.3919, -0.421389, "Thames at Walton"),
    ("ea-oxford-01", "839866a2-80ca-42a8-a695-8e6d6b165fa6", 51.750607, -1.246653, "Cherwell at Oxford"),
    ("ea-colwick-01", "0dcf81cb-5305-4e0b-b150-9b733ac44d0b", 52.953477, -1.078373, "Trent at Colwick"),
    ("ea-evesham-01", "bbaa85be-3a06-4d2b-b5ad-7f1f55b6554d", 52.091994, -1.94292, "Avon at Evesham"),
    ("ea-bathford-01", "e1167305-106b-4cd6-adbb-83d90590079e", 51.401758, -2.310019, "Avon at Bathford"),
    ("ea-thorverton-01", "3c4d4f78-2d0e-474a-b884-65a9daca18fb", 50.804172, -3.511302, "Exe at Thorverton"),
    ("ea-lowmoor-01", "d031ef9f-4e50-4c68-aa43-9b5589c32874", 54.488963, -1.438958, "Tees at Low Moor"),
    ("ea-methley-01", "825e7c96-b693-4366-8e39-a7fc587d442a", 53.726053, -1.382684, "Calder at Methley"),
    ("ea-derby-01", "6ff82c44-bbf2-4593-8218-76569f550b47", 52.928202, -1.47491, "Derwent at Derby"),
    ("ea-manchester-01", "8245803e-7926-4ff8-95fb-1f4b7be1d5cd", 53.499886, -2.271622, "Irwell at Manchester"),
    ("ea-adwick-01", "f22f80f8-1bb0-4e77-b225-291487060c6f", 53.512713, -1.282505, "Dearne at Adwick"),
)

RWS_RIVER_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("rws-lobith-01", "lobith.bovenrijn.tolkamer", 51.8495, 6.1024, "Boven-Rijn at Lobith"),
    ("rws-tiel-01", "tiel.waal", 51.882384, 5.440687, "Waal at Tiel"),
    ("rws-nijmegen-01", "nijmegen.waal", 51.853, 5.854, "Waal at Nijmegen"),
    ("rws-kampen-01", "kampen.ijssel", 52.552, 5.9264, "IJssel at Kampen"),
    ("rws-zutphen-01", "zutphen.ijssel", 52.154, 6.182, "IJssel at Zutphen"),
)

USACE_RESERVOIR_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("usace-keys-01", "KEYS", 36.1516667, -96.2516667, "Keystone Lake, OK"),
    ("usace-pine-01", "PINE", 34.1119444, -95.0794444, "Pine Creek Lake, OK"),
    ("usace-deni-01", "DENI", 33.8180556, -96.5722222, "Lake Texoma, TX/OK"),
    ("usace-huds-01", "HUDS", 36.2300934, -95.1821854, "Lake Hudson, OK"),
)


def register_p6_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P6 relays and return the number added."""

    def enabled(name: str) -> bool:
        return _env(name, "1").lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_EA_HYDROLOGY_ENABLED"):
        for device_id, guid, lat, lon, _place in EA_HYDROLOGY_MESH:
            fleet.add(EaHydrologyRiver(
                device_id, clock, guid=guid, latitude=lat, longitude=lon,
                site=f"live-river-{device_id}", key_dir=key_dir,
            ))
            n += 1
    if enabled("GAIA_RWS_WATER_ENABLED"):
        for device_id, code, lat, lon, _place in RWS_RIVER_MESH:
            fleet.add(RwsRiver(
                device_id, clock, location_code=code, latitude=lat, longitude=lon,
                site=f"live-river-{device_id}", key_dir=key_dir,
            ))
            n += 1
    if enabled("GAIA_USACE_CWMS_ENABLED"):
        for device_id, name, lat, lon, _place in USACE_RESERVOIR_MESH:
            fleet.add(UsaceReservoir(
                device_id, clock, name=name, latitude=lat, longitude=lon,
                site=f"live-reservoir-{name.lower()}", key_dir=key_dir,
            ))
            n += 1
    if enabled("GAIA_EPA_UV_ENABLED"):
        fleet.add(EpaUvIndex("epa-uv-01", clock, site="live-uv-us", key_dir=key_dir))
        n += 1
    if enabled("GAIA_MET_EIREANN_ENABLED"):
        fleet.add(MetEireannObs("met-ie-01", clock, site="live-weather-ireland", key_dir=key_dir))
        n += 1
    return n


__all__ = [
    "EaHydrologyRiver", "RwsRiver", "UsaceReservoir", "EpaUvIndex", "MetEireannObs",
    "EA_HYDROLOGY_MESH", "RWS_RIVER_MESH", "USACE_RESERVOIR_MESH",
    "EPA_UV_MESH", "MET_IE_STATIONS", "register_p6_relays",
]
