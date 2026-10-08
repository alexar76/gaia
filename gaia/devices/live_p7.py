"""P7 commercially-clear LIVE relays — Scandinavia weather + UK rivers.

* MET Norway Frost — in-situ station observations (CC BY 4.0 + NLOD).
  Distinct from ``metno-01`` METAR (airport instruments only).
* DMI metObs — Denmark in-situ (CC BY 4.0; no API key as of 2025-12).
* SEPA KiWIS — Scotland hydrology (OGL). England is EA; Wales is NRW.
* NRW River Levels — Wales hydrology (OGL; free ``GAIA_NRW_API_KEY``).
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p7")

_SAFE_FROST_SRC = re.compile(r"^SN[0-9]{3,7}$", re.I)
_SAFE_DMI_STA = re.compile(r"^[0-9]{4,6}$")
_SAFE_SEPA_STA = re.compile(r"^[0-9]{4,8}$")
_SAFE_NRW_LOC = re.compile(r"^[0-9]{3,8}$")

_FROST_ELEMENTS = (
    "air_temperature",
    "relative_humidity",
    "air_pressure_at_sea_level",
    "wind_speed",
)
_DMI_PARAMS = (
    ("temp_dry", "temperature_c"),
    ("humidity", "humidity_pct"),
    ("pressure_at_sea", "pressure_hpa"),
    ("wind_speed", "wind_mps"),
)


def _iso_epoch(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _basic_auth(user: str) -> str:
    token = base64.b64encode(f"{user}:".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


# ── MET Norway Frost ──────────────────────────────────────────────────────────


class MetNorwayFrost(LiveDevice):
    """MET Norway Frost in-situ observations — CC BY 4.0 + NLOD (not METAR)."""

    model = "GAIA-WEATHER (MET Norway Frost)"
    policy_id = "met_norway_frost"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://frost.met.no "
        "(MET Norway Frost in-situ station observations; CC BY 4.0 + NLOD — "
        "attribution: MET Norway. Not METAR airport instruments and not "
        "locationforecast model output.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        client_id: str,
        source_id: str = "SN18700",
        latitude: float = 59.9423,
        longitude: float = 10.72,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        cid = (client_id or "").strip()
        if not cid or len(cid) > 128:
            raise ValueError("invalid Frost client id")
        src = (source_id or "").strip().upper()
        if not _SAFE_FROST_SRC.fullmatch(src):
            raise ValueError(f"invalid Frost source id: {source_id!r}")
        self.client_id = cid
        self.source_id = src
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.headers = {
            "Authorization": _basic_auth(cid),
            "Accept": "application/json",
        }
        elems = ",".join(_FROST_ELEMENTS)
        self.url = (
            "https://frost.met.no/observations/v0.jsonld"
            f"?sources={quote(src, safe='')}&referencetime=latest"
            f"&elements={quote(elems, safe=',')}"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    @staticmethod
    def _obs_value(payload: Any, element: str) -> float | None:
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        newest: tuple[float, float] | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            when = _iso_epoch(row.get("referenceTime")) or 0.0
            for obs in row.get("observations") or []:
                if not isinstance(obs, dict):
                    continue
                if str(obs.get("elementId") or "") != element:
                    continue
                value = _num(obs.get("value"))
                if value is None:
                    continue
                candidate = (when, float(value))
                if newest is None or candidate[0] >= newest[0]:
                    newest = candidate
        return newest[1] if newest else None

    def map(self, payload: Any) -> dict[str, float | None]:
        temp = self._obs_value(payload, "air_temperature")
        rh = self._obs_value(payload, "relative_humidity")
        pressure = self._obs_value(payload, "air_pressure_at_sea_level")
        wind = self._obs_value(payload, "wind_speed")
        if temp is None and pressure is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: Frost returned no usable observations")
        return {
            "temperature_c": temp,
            "humidity_pct": rh,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


# ── DMI metObs ────────────────────────────────────────────────────────────────


class DmiMetObs(LiveDevice):
    """Danish Meteorological Institute metObs — CC BY 4.0 (no API key)."""

    model = "GAIA-WEATHER (DMI metObs)"
    policy_id = "dmi_metobs"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://opendata.dmi.dk "
        "(DMI Open Data metObs; CC BY 4.0 — attribution: Danish Meteorological Institute. "
        "Denmark in-situ stations only.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station_id: str = "06180",
        latitude: float = 55.614,
        longitude: float = 12.6454,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        sta = (station_id or "").strip()
        if not _SAFE_DMI_STA.fullmatch(sta):
            raise ValueError(f"invalid DMI station id: {station_id!r}")
        self.station_id = sta
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        # Primary URL used by allowlist / source_policy; sample() fetches per-param.
        self.url = (
            "https://opendataapi.dmi.dk/v2/metObs/collections/observation/items"
            f"?stationId={quote(sta, safe='')}&parameterId=temp_dry&limit=1"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def _param_url(self, parameter_id: str) -> str:
        return (
            "https://opendataapi.dmi.dk/v2/metObs/collections/observation/items"
            f"?stationId={quote(self.station_id, safe='')}"
            f"&parameterId={quote(parameter_id, safe='')}&limit=1"
        )

    @staticmethod
    def _latest_feature(payload: Any) -> tuple[float | None, float | None, float | None]:
        feats = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(feats, list) or not feats:
            return None, None, None
        feat = feats[0] if isinstance(feats[0], dict) else {}
        props = feat.get("properties") if isinstance(feat, dict) else {}
        geom = feat.get("geometry") if isinstance(feat, dict) else {}
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        lon = lat = None
        if isinstance(coords, list) and len(coords) >= 2:
            lon, lat = _num(coords[0]), _num(coords[1])
        value = _num((props or {}).get("value")) if isinstance(props, dict) else None
        return value, lat, lon

    def map(self, payload: Any) -> dict[str, float | None]:
        # sample() passes a dict of parameter → FeatureCollection.
        if not isinstance(payload, dict) or "features" in payload:
            value, lat, lon = self._latest_feature(payload)
            if value is None:
                raise DeviceOffline(f"{self.device_id}: DMI metObs empty")
            return {
                "temperature_c": value,
                "humidity_pct": None,
                "pressure_hpa": None,
                "wind_mps": None,
                "latitude": lat or self.latitude,
                "longitude": lon or self.longitude,
            }
        out: dict[str, float | None] = {
            "temperature_c": None,
            "humidity_pct": None,
            "pressure_hpa": None,
            "wind_mps": None,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
        for param_id, field in _DMI_PARAMS:
            value, lat, lon = self._latest_feature(payload.get(param_id))
            if value is not None:
                out[field] = value
            if lat is not None:
                out["latitude"] = lat
            if lon is not None:
                out["longitude"] = lon
        if out["temperature_c"] is None and out["pressure_hpa"] is None:
            raise DeviceOffline(f"{self.device_id}: DMI metObs has no temperature/pressure")
        return out

    def sample(self) -> dict[str, float]:
        payloads: dict[str, Any] = {}
        for param_id, _field in _DMI_PARAMS:
            url = self._param_url(param_id)
            require_approved_source(self.policy_id).require_endpoint(url)
            try:
                payloads[param_id] = self._fetch(url)
            except DeviceOffline:
                continue
        return {k: v for k, v in self.map(payloads).items() if v is not None}


# ── SEPA KiWIS ────────────────────────────────────────────────────────────────


class SepaRiver(LiveDevice):
    """SEPA KiWIS river stage/flow — Scotland, Open Government Licence."""

    model = "GAIA-RIVER (SEPA KiWIS)"
    policy_id = "sepa_kiwis"
    fields = {
        "discharge_m3s": "m3/s",
        "gage_height_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://timeseries.sepa.org.uk/KiWIS/KiWIS "
        "(SEPA time-series KiWIS; Open Government Licence — cite Scottish Environment "
        "Protection Agency. Scotland hydrology only — not EA England or NRW Wales.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station_no: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        sta = (station_no or "").strip()
        if not _SAFE_SEPA_STA.fullmatch(sta):
            raise ValueError(f"invalid SEPA station_no: {station_no!r}")
        self.station_no = sta
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = self._values_url("SG")
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def _values_url(self, parameter: str) -> str:
        # SG = stage (m), Q = flow (m³/s); 15m.Cmd = latest 15-min commanded series.
        return (
            "https://timeseries.sepa.org.uk/KiWIS/KiWIS"
            "?service=kisters&type=queryServices&datasource=0"
            "&request=getTimeseriesValues"
            f"&ts_path=1/{quote(self.station_no, safe='')}/{quote(parameter, safe='')}/15m.Cmd"
            "&metadata=true"
            "&md_returnfields=station_no,station_name,station_latitude,station_longitude,river_name"
            "&returnfields=Timestamp,Value&format=json"
        )

    @staticmethod
    def _latest_row(payload: Any) -> tuple[float | None, float | None, float | None]:
        rows = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
        if not rows:
            return None, None, None
        block = rows[0] if isinstance(rows[0], dict) else {}
        if block.get("type") == "error":
            return None, None, None
        lat = _num(block.get("station_latitude"))
        lon = _num(block.get("station_longitude"))
        data = block.get("data")
        if not isinstance(data, list) or not data:
            return None, lat, lon
        newest: tuple[float, float] | None = None
        for point in data:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            when = _iso_epoch(point[0]) or 0.0
            value = _num(point[1])
            if value is None:
                continue
            candidate = (when, float(value))
            if newest is None or candidate[0] >= newest[0]:
                newest = candidate
        return (newest[1] if newest else None), lat, lon

    def map(self, payload: Any) -> dict[str, float | None]:
        stage_payload, flow_payload = (
            payload if isinstance(payload, tuple) and len(payload) == 2 else (payload, {})
        )
        stage, lat, lon = self._latest_row(stage_payload)
        flow, flow_lat, flow_lon = self._latest_row(flow_payload)
        lat = lat or flow_lat or self.latitude
        lon = lon or flow_lon or self.longitude
        if stage is None and flow is None:
            raise DeviceOffline(f"{self.device_id}: SEPA KiWIS has no stage/flow")
        return {
            "discharge_m3s": flow,
            "gage_height_m": stage,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        stage_url = self._values_url("SG")
        flow_url = self._values_url("Q")
        require_approved_source(self.policy_id).require_endpoint(stage_url)
        require_approved_source(self.policy_id).require_endpoint(flow_url)
        stage_payload = self._fetch(stage_url)
        try:
            flow_payload = self._fetch(flow_url)
        except DeviceOffline:
            flow_payload = []
        return {k: v for k, v in self.map((stage_payload, flow_payload)).items() if v is not None}


# ── NRW River Levels ──────────────────────────────────────────────────────────


class NrwRiver(LiveDevice):
    """Natural Resources Wales river levels — Wales, OGL (API key required)."""

    model = "GAIA-RIVER (NRW)"
    policy_id = "nrw_river"
    fields = {
        "discharge_m3s": "m3/s",
        "gage_height_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.naturalresources.wales/riverlevels/ "
        "(Natural Resources Wales River Levels API; Open Government Licence — cite "
        "Natural Resources Wales. Wales hydrology only — not EA England or SEPA.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        api_key: str,
        location: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        key = (api_key or "").strip()
        if not key or len(key) > 128:
            raise ValueError("invalid NRW API key")
        loc = (location or "").strip()
        if not _SAFE_NRW_LOC.fullmatch(loc):
            raise ValueError(f"invalid NRW location id: {location!r}")
        self.location = loc
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.headers = {
            "Ocp-Apim-Subscription-Key": key,
            "Accept": "application/json",
        }
        self.url = (
            "https://api.naturalresources.wales/riverlevels/v1/all"
            f"?Location={quote(loc, safe='')}"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            for key in ("value", "values", "data", "stations", "items", "results"):
                node = payload.get(key)
                if isinstance(node, list):
                    return [r for r in node if isinstance(r, dict)]
            return [payload]
        return []

    def map(self, payload: Any) -> dict[str, float | None]:
        stage = flow = lat = lon = None
        for row in self._rows(payload):
            lat = lat or _num(row.get("latitude") or row.get("lat") or row.get("Latitude"))
            lon = lon or _num(row.get("longitude") or row.get("lon") or row.get("Longitude"))
            for key in (
                "LatestValue", "latestValue", "latest_value", "Level", "level",
                "Stage", "stage", "Value", "value", "gageHeight", "waterLevel",
            ):
                if key in row and stage is None:
                    stage = _num(row.get(key))
            for key in ("Flow", "flow", "Discharge", "discharge", "Q"):
                if key in row and flow is None:
                    flow = _num(row.get(key))
            params = row.get("parameters") or row.get("Parameters") or []
            if isinstance(params, list):
                for param in params:
                    if not isinstance(param, dict):
                        continue
                    name = str(
                        param.get("parameter")
                        or param.get("name")
                        or param.get("parameterNameEN")
                        or param.get("titleEn")
                        or ""
                    ).lower()
                    value = _num(
                        param.get("latestValue")
                        or param.get("LatestValue")
                        or param.get("value")
                    )
                    if value is None:
                        continue
                    if any(tok in name for tok in ("level", "stage", "height", "levelm")):
                        stage = stage if stage is not None else value
                    elif any(tok in name for tok in ("flow", "discharge")):
                        flow = flow if flow is not None else value
                    elif stage is None:
                        stage = value
        if stage is None and flow is None:
            raise DeviceOffline(f"{self.device_id}: NRW river levels empty for {self.location}")
        return {
            "discharge_m3s": flow,
            "gage_height_m": stage,
            "latitude": lat or self.latitude,
            "longitude": lon or self.longitude,
        }


# ── Meshes + registration ─────────────────────────────────────────────────────

FROST_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("frost-blindern-01", "SN18700", 59.9423, 10.72, "Oslo Blindern"),
    ("frost-bergen-01", "SN50540", 60.383, 5.332, "Bergen Florida"),
    ("frost-trondheim-01", "SN68860", 63.4206, 10.4078, "Trondheim Voll"),
    ("frost-tromso-01", "SN90450", 69.6536, 18.9368, "Tromsø"),
    ("frost-stavanger-01", "SN44560", 58.8767, 5.6378, "Stavanger"),
    ("frost-kristiansand-01", "SN39040", 58.2043, 8.0850, "Kristiansand"),
)

DMI_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("dmi-cph-01", "06180", 55.614, 12.6454, "Copenhagen / Kastrup"),
    ("dmi-odense-01", "06120", 55.4749, 10.3305, "Odense / Funen"),
    ("dmi-aarhus-01", "06041", 56.297, 10.619, "Aarhus area / East Jutland"),
    ("dmi-aalborg-01", "06030", 57.095, 9.856, "Aalborg"),
    ("dmi-billund-01", "06104", 55.740, 9.152, "Billund"),
)

SEPA_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("sepa-abington-01", "322551", 55.48691533, -3.69056827, "Clyde at Abington"),
    ("sepa-almondbank-01", "14954", 56.41527516, -3.51359839, "Almond at Almondbank"),
    ("sepa-aberlour-01", "234150", 57.48021805, -3.205975095, "Spey at Aberlour"),
    ("sepa-ancrum-01", "15002", 55.51244139, -2.580776051, "Ale Water at Ancrum"),
    ("sepa-alford-01", "234170", 57.24230294, -2.717123004, "Don at Alford"),
)

# Location IDs from NRW River Levels API (Wales). Registered only with API key.
NRW_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("nrw-4016-01", "4016", 51.4816, -3.1791, "Cardiff area gauge 4016"),
    ("nrw-4060-01", "4060", 51.747, -3.379, "South Wales gauge 4060"),
    ("nrw-4164-01", "4164", 53.140, -3.280, "North Wales gauge 4164"),
)


def register_p7_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P7 relays and return the number added."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    frost_id = _env("GAIA_FROST_CLIENT_ID")
    if frost_id and enabled("GAIA_FROST_ENABLED", "1"):
        for device_id, source_id, lat, lon, _place in FROST_MESH:
            try:
                fleet.add(
                    MetNorwayFrost(
                        device_id,
                        clock,
                        client_id=frost_id,
                        source_id=source_id,
                        latitude=lat,
                        longitude=lon,
                        site=f"live-weather-{device_id}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("Frost %s skipped: %s", device_id, exc)
    elif enabled("GAIA_FROST_ENABLED", "1"):
        log.info("Frost skipped (set GAIA_FROST_CLIENT_ID from frost.met.no)")

    if enabled("GAIA_DMI_ENABLED", "1"):
        for device_id, station_id, lat, lon, _place in DMI_MESH:
            try:
                fleet.add(
                    DmiMetObs(
                        device_id,
                        clock,
                        station_id=station_id,
                        latitude=lat,
                        longitude=lon,
                        site=f"live-weather-{device_id}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("DMI %s skipped: %s", device_id, exc)

    if enabled("GAIA_SEPA_ENABLED", "1"):
        for device_id, station_no, lat, lon, _place in SEPA_MESH:
            try:
                fleet.add(
                    SepaRiver(
                        device_id,
                        clock,
                        station_no=station_no,
                        latitude=lat,
                        longitude=lon,
                        site=f"live-river-{device_id}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("SEPA %s skipped: %s", device_id, exc)

    nrw_key = _env("GAIA_NRW_API_KEY")
    if nrw_key and enabled("GAIA_NRW_ENABLED", "1"):
        for device_id, location, lat, lon, _place in NRW_MESH:
            try:
                fleet.add(
                    NrwRiver(
                        device_id,
                        clock,
                        api_key=nrw_key,
                        location=location,
                        latitude=lat,
                        longitude=lon,
                        site=f"live-river-{device_id}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("NRW %s skipped: %s", device_id, exc)
    elif enabled("GAIA_NRW_ENABLED", "1"):
        log.info(
            "NRW skipped (set GAIA_NRW_API_KEY from api-portal.naturalresources.wales)"
        )

    return n


__all__ = [
    "MetNorwayFrost",
    "DmiMetObs",
    "SepaRiver",
    "NrwRiver",
    "FROST_MESH",
    "DMI_MESH",
    "SEPA_MESH",
    "NRW_MESH",
    "register_p7_relays",
]
