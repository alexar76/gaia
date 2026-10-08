"""P12 commercially-clear LIVE relays — Alps / Baltics / FR warning+grid / IE / JP / BR / CZ / KR.

Open (no key):
* GeoSphere Austria TAWES — CC BY 4.0 weather observations (not paid forecasts).
* Lithuania LHMT Meteo.lt — CC BY-SA 4.0 weather + Nemunas hydro (live query; not /forecasts).
* Latvia LVGMC data.gov.lv HVD — CC0-1.0 weather + hydro CSV.
* Vigicrues VIC — Etalab OL 2.0 flood WARNING (not Hub'Eau gauges; empty ≠ all-clear).
* RTE éCO2mix — Etalab OL 2.0 France national production-only CO₂.
* Ireland OPW waterlevel.ie — CC BY 4.0 river gauges (pre-generated GeoJSON; ids >41000 excluded).
* JMA AMeDAS / quake / typhoon — Public Data License 1.0 (CC BY compatible). Website JSON; cite JMA.
* INMET Brazil WIS2 SYNOP — WMO core unrestricted. HTTPS wis2bra.inmet.gov.br only.
* CHMU Czech 10-min climate now — CC BY 4.0 file dumps.

Keyed (register when env is set):
* KMA ASOS — Public Nuri Type 1. ``GAIA_KMA_SERVICE_KEY``. Not AirKorea (Type 3 ND).
* Copernicus GFM observed flood — ``GAIA_GFM_TOKEN``. Distinct from GloFAS WMS.

Copernicus EDO Combined Drought Indicator is skipped: no lightweight cell-at-coordinate
HTTPS product (redirect / WMS wall) — do not fake a drought reading.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import quote

from gaia.clock import SimClock
from gaia.devices._live_base import _UA
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.devices.live_p0 import geojson_centroid, signed_cluster_read
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p12")

_SAFE_AT = re.compile(r"^[0-9]{4,8}$")
_SAFE_LT = re.compile(r"^[a-z0-9-]{4,40}$")
_SAFE_LV = re.compile(r"^[A-Za-z0-9]{4,16}$")
_SAFE_VIC = re.compile(r"^[0-9]{1,4}$")
_SAFE_IE = re.compile(r"^[0-9]{3,8}$")
_SAFE_JMA = re.compile(r"^[0-9]{5}$")
_SAFE_BR = re.compile(r"^[0-9]{5}$")
_SAFE_CZ = re.compile(r"^[0-9]{5}$")
_SAFE_KR = re.compile(r"^[0-9]{3}$")
_SAFE_SECRET = re.compile(r"^[A-Za-z0-9._:~+/%-]{8,512}$")
_MAX_AGE_S = 6 * 3600
_CACHE_TTL_S = 120.0
_RIGA_TZ = timezone(timedelta(hours=3))  # EEST in September; naive LVGMC clocks are local.

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()

_AT_URL = (
    "https://dataset.api.hub.geosphere.at/v1/station/current/tawes-v1-10min"
    "?parameters=TL,RF,P,FF&station_ids="
)
_LT_WX = "https://api.meteo.lt/v1/stations/{code}/observations/latest"
_LT_HYDRO = "https://api.meteo.lt/v1/hydro-stations/{code}/observations/measured/latest"
_LV_WX_CSV = (
    "https://data.gov.lv/dati/dataset/40d80be5-0c09-47c4-80f3-fad4bec19f33/"
    "resource/17460efb-ae99-4d1d-8144-1068f184b05f/download/meteo_operativie_dati.csv"
)
_LV_HYDRO_CSV = (
    "https://data.gov.lv/dati/dataset/40d80be5-0c09-47c4-80f3-fad4bec19f33/"
    "resource/de5f06e9-6f44-497d-8ec2-72a2483608e8/download/hidro_operativie_dati.csv"
)
_VIC_GEOJSON = "https://www.vigicrues.gouv.fr/services/InfoVigiCru.geojson"
_VIC_TERENT = "https://www.vigicrues.gouv.fr/services/TerEntVigiCru.json"
_RTE_URL = (
    "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
    "eco2mix-national-tr/records?where=taux_co2%20is%20not%20null"
    "&order_by=date_heure%20desc&limit=1"
)
_IE_URL = "https://waterlevel.ie/geojson/latest/"
_JMA_LATEST = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
_JMA_MAP = "https://www.jma.go.jp/bosai/amedas/data/map/{ts}.json"
_JMA_QUAKE = "https://www.jma.go.jp/bosai/quake/data/list.json"
_JMA_TC = "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json"
_JMA_TC_SPEC = "https://www.jma.go.jp/bosai/typhoon/data/{tc}/specifications.json"
_INMET_ITEMS = (
    "https://wis2bra.inmet.gov.br/oapi/collections/"
    "urn:wmo:md:br-inmet:synop/items"
)
_CHMU_FILE = (
    "https://opendata.chmi.cz/meteorology/climate/now/data/"
    "10m-0-20000-0-{wmo}-{day}.json"
)
_KMA_URL = "https://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList"
_GFM_URL = "https://api.gfm.eodc.eu/v1/geojson/observed"

_JMA_COD = re.compile(
    r"(?P<lat>[+-]\d+(?:\.\d+)?)(?P<lon>[+-]\d+(?:\.\d+)?)(?P<dep>[+-]\d+)?"
)


def _require_secret(value: str, *, name: str) -> str:
    key = (value or "").strip()
    if not _SAFE_SECRET.fullmatch(key):
        raise ValueError(f"invalid {name}")
    return key


def _require_place(name: str, *, what: str) -> str:
    text = (name or "").strip()
    if not text or len(text) > 80 or any(c in text for c in "\n\r\t"):
        raise ValueError(f"invalid {what}: {name!r}")
    return text


def _iso_age_s(raw: Any, *, naive_tz: timezone = timezone.utc) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        return time.time() - ts
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit() or (text.replace(".", "", 1).isdigit() and text.count(".") < 2):
        try:
            ts = float(text)
        except ValueError:
            ts = None
        else:
            if ts > 1e12:
                ts /= 1000.0
            if ts > 1e9:
                return time.time() - ts
    compact = text.replace(".", "-", 2).replace(" ", "T", 1)
    try:
        when = datetime.fromisoformat(compact.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=naive_tz)
    return (datetime.now(timezone.utc) - when.astimezone(timezone.utc)).total_seconds()


def _reject_stale(device_id: str, raw: Any, *, label: str, naive_tz: timezone = timezone.utc) -> None:
    age = _iso_age_s(raw, naive_tz=naive_tz)
    if age is None:
        return
    if age > _MAX_AGE_S or age < -3600:
        raise DeviceOffline(f"{device_id}: {label} observation stale")


def _cached(key: str, loader: Any) -> Any:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    payload = loader()
    with _CACHE_LOCK:
        _CACHE[key] = (now, payload)
    return payload


def _param_value(parameters: Any, *names: str) -> float | None:
    if not isinstance(parameters, dict):
        return None
    for name in names:
        node = parameters.get(name)
        if isinstance(node, dict):
            data = node.get("data")
            if isinstance(data, list) and data:
                value = _num(data[-1])
                if value is not None:
                    return value
            value = _num(node.get("value"))
            if value is not None:
                return value
        else:
            value = _num(node)
            if value is not None:
                return value
    return None


def _geom_centroid(geom: Any) -> tuple[float, float] | None:
    """Return (lat, lon). Extends P0 helper with MultiLineString (Vigicrues tronçons)."""
    if not isinstance(geom, dict):
        return None
    if str(geom.get("type") or "") == "MultiLineString":
        coords = geom.get("coordinates")
        pts: list[tuple[float, float]] = []
        if isinstance(coords, list):
            for line in coords:
                if not isinstance(line, list):
                    continue
                for pt in line:
                    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        lon, lat = _num(pt[0]), _num(pt[1])
                        if lat is not None and lon is not None:
                            pts.append((float(lon), float(lat)))
        if not pts:
            return None
        return (
            sum(p[1] for p in pts) / len(pts),
            sum(p[0] for p in pts) / len(pts),
        )
    return geojson_centroid(geom)


def _csv_rows(text: str) -> list[dict[str, str]]:
    sample = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(sample))
    rows: list[dict[str, str]] = []
    for row in reader:
        if isinstance(row, dict):
            rows.append({str(k).strip(): ("" if v is None else str(v).strip()) for k, v in row.items()})
    return rows


def _lv_latest(rows: list[dict[str, str]], station: str, abbrev: str) -> tuple[float | None, str]:
    latest_when = ""
    latest_val: float | None = None
    for row in rows:
        if (row.get("STATION_ID") or "").strip() != station:
            continue
        if (row.get("ABBREVIATION") or "").strip() != abbrev:
            continue
        when = (row.get("DATETIME") or "").strip()
        value = _num(row.get("VALUE"))
        if value is None:
            continue
        if when >= latest_when:
            latest_when, latest_val = when, value
    return latest_val, latest_when


def _scale_obs(value: float | None, *, kind: str) -> float | None:
    if value is None:
        return None
    if kind == "temp" and abs(value) > 80.0:
        return value * 0.01
    if kind == "pressure" and value > 2000.0:
        return value / 10.0
    if kind == "wind" and value > 80.0:
        return value * 0.1
    if kind == "level" and value > 30.0:
        return value / 100.0
    return value


# ── Devices ───────────────────────────────────────────────────────────────────


class GeosphereWeather(LiveDevice):
    """GeoSphere Austria TAWES 10-min in-situ — CC BY 4.0. Observations only."""

    model = "GAIA-WEATHER (GeoSphere AT)"
    policy_id = "geosphere_at"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "GeoSphere Austria TAWES 10-min via dataset.api.hub.geosphere.at · CC BY 4.0 — "
        "cite GeoSphere Austria. Austria in-situ only, not a paid forecast."
    )
    timeout = 20.0

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_AT.fullmatch(code):
            raise ValueError(f"invalid GeoSphere station: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        ids = ",".join(row[1] for row in GEOSPHERE_MESH)
        self.url = f"{_AT_URL}{quote(ids, safe=',')}"
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: GeoSphere empty")
        stamps = payload.get("timestamps") if isinstance(payload, dict) else None
        if isinstance(stamps, list) and stamps:
            _reject_stale(self.device_id, stamps[-1], label="GeoSphere TAWES")
        row = None
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            if str(props.get("station") or "") == self.station:
                row = feat
                break
        if not row:
            raise DeviceOffline(f"{self.device_id}: GeoSphere no station {self.station}")
        props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        params = props.get("parameters")
        temperature = _param_value(params, "TL")
        humidity = _param_value(params, "RF")
        pressure = _param_value(params, "P")
        wind = _param_value(params, "FF")
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: GeoSphere no T/wind for {self.station}")
        lat, lon = self.latitude, self.longitude
        centroid = _geom_centroid(row.get("geometry"))
        if centroid:
            lat, lon = centroid
        return {
            "temperature_c": temperature, "humidity_pct": humidity,
            "pressure_hpa": pressure, "wind_mps": wind,
            "latitude": lat, "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class LhmtWeather(LiveDevice):
    """Lithuania LHMT Meteo.lt station observation — CC BY-SA 4.0. Not a forecast."""

    model = "GAIA-WEATHER (LHMT)"
    policy_id = "lhmt_lt"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Lithuania LHMT Meteo.lt station observations · CC BY-SA 4.0 — cite LHMT. "
        "Live query (like Sensor.Community ODbL). Observations only — not /forecasts."
    )

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip().lower()
        if not _SAFE_LT.fullmatch(code):
            raise ValueError(f"invalid LHMT station: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = _LT_WX.format(code=quote(code, safe="-"))
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: LHMT empty")
        obs = payload.get("observations")
        rows = [item for item in obs if isinstance(item, dict)] if isinstance(obs, list) else []
        if not rows:
            raise DeviceOffline(f"{self.device_id}: LHMT no observations for {self.station}")
        latest = rows[-1]
        _reject_stale(self.device_id, latest.get("observationTimeUtc"), label="LHMT")
        temperature = _num(latest.get("airTemperature"))
        humidity = _num(latest.get("relativeHumidity"))
        pressure = _num(latest.get("seaLevelPressure"))
        wind = _num(latest.get("windSpeed"))
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: LHMT no T/wind for {self.station}")
        station = payload.get("station") if isinstance(payload.get("station"), dict) else {}
        coords = station.get("coordinates") if isinstance(station.get("coordinates"), dict) else {}
        return {
            "temperature_c": temperature, "humidity_pct": humidity,
            "pressure_hpa": pressure, "wind_mps": wind,
            "latitude": _num(coords.get("latitude")) or self.latitude,
            "longitude": _num(coords.get("longitude")) or self.longitude,
        }


class LhmtRiver(LiveDevice):
    """Lithuania LHMT hydro observation — CC BY-SA 4.0. waterLevel is centimetres."""

    model = "GAIA-RIVER (LHMT)"
    policy_id = "lhmt_lt"
    fields = {
        "gage_height_m": "m",
        "water_temperature_c": "cel",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Lithuania LHMT Meteo.lt hydro-station measured observations · CC BY-SA 4.0 — "
        "cite LHMT. waterLevel published in cm, sold as metres. Not a forecast."
    )

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip().lower()
        if not _SAFE_LT.fullmatch(code):
            raise ValueError(f"invalid LHMT hydro station: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = _LT_HYDRO.format(code=quote(code, safe="-"))
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: LHMT hydro empty")
        obs = payload.get("observations")
        rows = [item for item in obs if isinstance(item, dict)] if isinstance(obs, list) else []
        if not rows:
            raise DeviceOffline(f"{self.device_id}: LHMT hydro no rows for {self.station}")
        latest = rows[-1]
        _reject_stale(self.device_id, latest.get("observationTimeUtc"), label="LHMT hydro")
        raw_level = _num(latest.get("waterLevel"))
        level = (raw_level / 100.0) if raw_level is not None else None
        temp = _num(latest.get("waterTemperature"))
        if level is None and temp is None:
            raise DeviceOffline(f"{self.device_id}: LHMT hydro no H/T for {self.station}")
        station = payload.get("station") if isinstance(payload.get("station"), dict) else {}
        coords = station.get("coordinates") if isinstance(station.get("coordinates"), dict) else {}
        return {
            "gage_height_m": level,
            "water_temperature_c": temp,
            "latitude": _num(coords.get("latitude")) or self.latitude,
            "longitude": _num(coords.get("longitude")) or self.longitude,
        }


class LvgmcWeather(LiveDevice):
    """Latvia LVGMC HVD operative CSV — CC0-1.0. Fail closed if DATETIME >6h stale."""

    model = "GAIA-WEATHER (LVGMC)"
    policy_id = "lvgmc_lv"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Latvia LVGMC operative meteorology via data.gov.lv dataset "
        "40d80be5-0c09-47c4-80f3-fad4bec19f33 · CC0-1.0. Latvia in-situ only."
    )
    timeout = 20.0

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_LV.fullmatch(code):
            raise ValueError(f"invalid LVGMC station: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = _LV_WX_CSV
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else None
        if not isinstance(rows, list) or not rows:
            raise DeviceOffline(f"{self.device_id}: LVGMC weather CSV empty")
        temp, t_when = _lv_latest(rows, self.station, "TDRY")
        if temp is None:
            temp, t_when = _lv_latest(rows, self.station, "HTDRY")
        rh, _ = _lv_latest(rows, self.station, "RLH")
        if rh is None:
            rh, _ = _lv_latest(rows, self.station, "HRLH")
        pres, _ = _lv_latest(rows, self.station, "PRSL")
        if pres is None:
            pres, _ = _lv_latest(rows, self.station, "HPRSL")
        wind, _ = _lv_latest(rows, self.station, "WNS10")
        if wind is None:
            wind, _ = _lv_latest(rows, self.station, "HWNDS")
        if not t_when:
            raise DeviceOffline(f"{self.device_id}: LVGMC no DATETIME for {self.station}")
        _reject_stale(self.device_id, t_when, label="LVGMC", naive_tz=_RIGA_TZ)
        temperature = _scale_obs(temp, kind="temp")
        humidity = rh
        pressure = _scale_obs(pres, kind="pressure")
        wind_mps = _scale_obs(wind, kind="wind")
        if temperature is None and wind_mps is None:
            raise DeviceOffline(f"{self.device_id}: LVGMC no T/wind for {self.station}")
        return {
            "temperature_c": temperature, "humidity_pct": humidity,
            "pressure_hpa": pressure, "wind_mps": wind_mps,
            "latitude": self.latitude, "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        text = _cached(self.url, lambda: self._fetch_text(self.url, max_chars=4_000_000))
        return {k: v for k, v in self.map(_csv_rows(text)).items() if v is not None}


class LvgmcRiver(LiveDevice):
    """Latvia LVGMC HVD hydro CSV — CC0-1.0."""

    model = "GAIA-RIVER (LVGMC)"
    policy_id = "lvgmc_lv"
    fields = {
        "gage_height_m": "m",
        "water_temperature_c": "cel",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Latvia LVGMC operative hydrology via data.gov.lv · CC0-1.0. "
        "LIMEN water level. Latvia in-situ only — not a flood warning."
    )
    timeout = 20.0

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_LV.fullmatch(code):
            raise ValueError(f"invalid LVGMC hydro station: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = _LV_HYDRO_CSV
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else None
        if not isinstance(rows, list) or not rows:
            raise DeviceOffline(f"{self.device_id}: LVGMC hydro CSV empty")
        level, when = _lv_latest(rows, self.station, "LIMEN")
        temp, t_when = _lv_latest(rows, self.station, "WTEMD")
        stamp = when or t_when
        if not stamp:
            raise DeviceOffline(f"{self.device_id}: LVGMC hydro no DATETIME for {self.station}")
        _reject_stale(self.device_id, stamp, label="LVGMC hydro", naive_tz=_RIGA_TZ)
        gage = _scale_obs(level, kind="level")
        if gage is None and temp is None:
            raise DeviceOffline(f"{self.device_id}: LVGMC hydro no H/T for {self.station}")
        return {
            "gage_height_m": gage,
            "water_temperature_c": temp,
            "latitude": self.latitude, "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        text = _cached(self.url, lambda: self._fetch_text(self.url, max_chars=4_000_000))
        return {k: v for k, v in self.map(_csv_rows(text)).items() if v is not None}


class VicFlood(LiveDevice):
    """Vigicrues VIC flood WARNING — Etalab OL 2.0. Not Hub'Eau. Empty ≠ all-clear."""

    model = "GAIA-FLOOD (Vigicrues)"
    policy_id = "vigicrues_fr"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Vigicrues VIC InfoVigiCru.geojson · Etalab Licence Ouverte 2.0. "
        "Flood WARNING product, not Hub'Eau gauges, not observations.json. "
        "Niveau 1 (green) is not sold as all-clear — empty / green → offline."
    )
    timeout = 25.0

    def __init__(
        self, device_id: str, clock: SimClock, *, territory: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (territory or "").strip()
        if not _SAFE_VIC.fullmatch(code):
            raise ValueError(f"invalid VIC territory: {territory!r}")
        self.territory = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = _VIC_GEOJSON
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(self.url)
        policy.require_endpoint(_VIC_TERENT)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: Vigicrues empty")
        _reject_stale(
            self.device_id,
            payload.get("DtHrInfoVigiCru") or payload.get("DateHeureCreationFichier"),
            label="Vigicrues",
        )
        features = payload.get("features")
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: Vigicrues GeoJSON empty (not all-clear)")
        best_level = 0
        best_lat, best_lon = self.latitude, self.longitude
        found = False
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            parent = str(props.get("cdensup_1") or props.get("CdEntVigiCru") or "")
            if parent != self.territory:
                continue
            found = True
            level = _num(props.get("NivInfViCr") or props.get("NivSituVigiCru"))
            if level is None:
                continue
            if level >= best_level:
                best_level = int(level)
                centroid = _geom_centroid(feat.get("geometry"))
                if centroid:
                    best_lat, best_lon = centroid
        if not found:
            raise DeviceOffline(f"{self.device_id}: Vigicrues no tronçon for territory {self.territory}")
        if best_level < 2:
            raise DeviceOffline(
                f"{self.device_id}: Vigicrues no warning on territory {self.territory} "
                "(green/empty is not all-clear)"
            )
        return {
            "severity_score": float(best_level),
            "latitude": best_lat,
            "longitude": best_lon,
        }

    def sample(self) -> dict[str, float]:
        text = _cached(self.url, lambda: self._fetch_text(self.url, max_chars=4_000_000))
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: Vigicrues GeoJSON malformed") from exc
        return {k: v for k, v in self.map(payload).items() if v is not None}


class RteGrid(LiveDevice):
    """RTE éCO2mix national production-only CO₂ — Etalab OL 2.0. Excludes imports/lifecycle."""

    model = "GAIA-GRID (RTE éCO2mix)"
    policy_id = "rte_eco2mix"
    fields = {
        "carbon_intensity_gco2_kwh": "gCO2/kWh",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "RTE éCO2mix national temps réel via odre.opendatasoft.com · Etalab Licence "
        "Ouverte 2.0. Production-only CO₂ (taux_co2) — excludes imports and lifecycle. "
        "France national pin, not ENTSO-E."
    )

    def __init__(
        self, device_id: str, clock: SimClock, *,
        latitude: float = 48.8566, longitude: float = 2.3522, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = _RTE_URL
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise DeviceOffline(f"{self.device_id}: RTE éCO2mix empty")
        row = rows[0]
        _reject_stale(self.device_id, row.get("date_heure") or row.get("date"), label="RTE éCO2mix")
        value = _num(row.get("taux_co2"))
        if value is None:
            raise DeviceOffline(f"{self.device_id}: RTE taux_co2 null")
        return {
            "carbon_intensity_gco2_kwh": value,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class OpwRiver(LiveDevice):
    """Ireland OPW waterlevel.ie — CC BY 4.0. Pre-generated GeoJSON; station ids >41000 excluded."""

    model = "GAIA-RIVER (OPW)"
    policy_id = "opw_ie"
    fields = {
        "gage_height_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Ireland OPW waterlevel.ie latest GeoJSON · CC BY 4.0 — cite OPW. "
        "Pre-generated files only. Stations >41000 excluded. Not a flood warning."
    )
    timeout = 20.0

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_IE.fullmatch(code):
            raise ValueError(f"invalid OPW station: {station!r}")
        sid = int(code)
        if sid > 41000:
            raise ValueError(f"OPW station {sid} excluded (>41000)")
        self.station = sid
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = _IE_URL
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: OPW GeoJSON empty")
        matches: list[Any] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            try:
                sid = int(str(props.get("station_ref") or "").strip() or "0")
            except ValueError:
                continue
            if sid == self.station:
                matches.append(feat)
        row = None
        for feat in matches:
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            sensor = str(props.get("sensor") or props.get("sensor_ref") or "").strip()
            if sensor in ("0001", "1"):
                row = feat
                break
        if row is None and matches:
            row = matches[0]
        if not row:
            raise DeviceOffline(f"{self.device_id}: OPW no station {self.station}")
        props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        _reject_stale(self.device_id, props.get("datetime"), label="OPW")
        level = _num(props.get("value"))
        if level is None:
            raise DeviceOffline(f"{self.device_id}: OPW null value for {self.station}")
        lat, lon = self.latitude, self.longitude
        centroid = _geom_centroid(row.get("geometry"))
        if centroid:
            lat, lon = centroid
        return {"gage_height_m": level, "latitude": lat, "longitude": lon}

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class JmaAmedasWeather(LiveDevice):
    """JMA AMeDAS website JSON — Public Data License 1.0 (CC BY compatible). Observations only."""

    model = "GAIA-WEATHER (JMA AMeDAS)"
    policy_id = "jma_amedas"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "JMA AMeDAS bosai website JSON · Public Data License 1.0 (compatible with CC BY 4.0) "
        "— cite Japan Meteorological Agency. Undocumented website JSON may move. "
        "Observations only — not a JP forecast licence."
    )

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_JMA.fullmatch(code):
            raise ValueError(f"invalid AMeDAS id: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(_JMA_LATEST)
        policy.require_endpoint(_JMA_MAP.format(ts="20200101000000"))

    def map(self, payload: Any) -> dict[str, float | None]:
        table = payload if isinstance(payload, dict) else None
        if not isinstance(table, dict):
            raise DeviceOffline(f"{self.device_id}: JMA AMeDAS empty")
        row = table.get(self.station)
        if not isinstance(row, dict):
            raise DeviceOffline(f"{self.device_id}: JMA AMeDAS no station {self.station}")
        def _pair(key: str) -> float | None:
            node = row.get(key)
            if isinstance(node, list) and node:
                return _num(node[0])
            return _num(node)
        temperature = _pair("temp")
        humidity = _pair("humidity")
        pressure = _pair("pressure") or _pair("normalPressure")
        wind = _pair("wind")
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: JMA AMeDAS no T/wind for {self.station}")
        return {
            "temperature_c": temperature, "humidity_pct": humidity,
            "pressure_hpa": pressure, "wind_mps": wind,
            "latitude": self.latitude, "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        stamp = _cached(_JMA_LATEST, lambda: self._fetch_text(_JMA_LATEST, max_chars=128)).strip()
        _reject_stale(self.device_id, stamp, label="JMA AMeDAS")
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        ts = when.strftime("%Y%m%d%H%M%S")
        if not re.fullmatch(r"[0-9]{14}", ts):
            raise DeviceOffline(f"{self.device_id}: JMA AMeDAS timestamp refused")
        url = _JMA_MAP.format(ts=ts)
        require_approved_source(self.policy_id).require_endpoint(url)
        payload = _cached(url, lambda: self._fetch(url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class JmaQuake(LiveDevice):
    """JMA official quake list.json — Public Data License. Official host only, not p2pquake."""

    model = "GAIA-QUAKE (JMA)"
    policy_id = "jma_quake"
    fields = {
        "magnitude": "Mw",
        "depth_km": "km",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "JMA bosai quake list.json · Public Data License 1.0 (CC BY compatible) — "
        "cite Japan Meteorological Agency. Official www.jma.go.jp only — not p2pquake."
    )
    url = _JMA_QUAKE
    _default_limit = 80

    def __init__(self, device_id: str, clock: SimClock, **kw: Any):
        super().__init__(device_id, clock, **kw)
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = payload if isinstance(payload, list) else None
        if not isinstance(rows, list) or not rows:
            raise DeviceOffline(f"{self.device_id}: JMA quake list empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mag = _num(row.get("mag"))
            match = _JMA_COD.search(str(row.get("cod") or ""))
            if mag is None or not match:
                continue
            lat = _num(match.group("lat"))
            lon = _num(match.group("lon"))
            depth_m = _num(match.group("dep"))
            if lat is None or lon is None:
                continue
            depth_km = abs(depth_m) / 1000.0 if depth_m is not None else 0.0
            item: dict[str, Any] = {
                "magnitude": float(mag),
                "depth_km": float(depth_km),
                "latitude": float(lat),
                "longitude": float(lon),
                "region": str(row.get("en_anm") or row.get("anm") or "")[:160],
            }
            scored.append((float(mag), item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: JMA had no geolocated events")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 200))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {k: (float(row[k]) if row.get(k) is not None else None) for k in self.fields}

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        return signed_cluster_read(
            self, self.collect_hotspots(payload),
            numeric_keys=("magnitude", "depth_km", "latitude", "longitude"),
            meta_keys=("region",),
        )


class JmaTyphoon(LiveDevice):
    """JMA NW-Pacific typhoon — Public Data License. Empty season = offline. Not NHC/JTWC."""

    model = "GAIA-CYCLONE (JMA)"
    policy_id = "jma_typhoon"
    fields = {
        "intensity_kn": "kn",
        "pressure_hpa": "hPa",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "JMA bosai typhoon targetTc.json + specifications.json · Public Data License 1.0 "
        "— cite JMA. NW Pacific only. Not NHC, not JTWC. Empty season → offline."
    )
    url = _JMA_TC

    def __init__(self, device_id: str, clock: SimClock, **kw: Any):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(self.url)
        policy.require_endpoint(_JMA_TC_SPEC.format(tc="TC0000"))

    def _analysis(self, spec: Any) -> dict[str, Any] | None:
        rows = spec if isinstance(spec, list) else None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            part = row.get("part")
            label = part.get("en") if isinstance(part, dict) else str(part or "")
            if str(label).lower() == "analysis" or label == "実況":
                return row
        return next((row for row in rows if isinstance(row, dict) and row.get("position")), None)

    def map(self, payload: Any) -> dict[str, float | None]:
        storms = payload if isinstance(payload, list) else None
        if not isinstance(storms, list) or not storms:
            raise DeviceOffline(f"{self.device_id}: JMA typhoon empty (off-season)")
        first = storms[0] if isinstance(storms[0], dict) else None
        if not first:
            raise DeviceOffline(f"{self.device_id}: JMA typhoon empty (off-season)")
        spec = first.get("specifications")
        analysis = self._analysis(spec)
        if not analysis:
            raise DeviceOffline(f"{self.device_id}: JMA typhoon no analysis")
        _reject_stale(
            self.device_id,
            ((analysis.get("validtime") or {}).get("UTC") if isinstance(analysis.get("validtime"), dict) else None)
            or first.get("issue"),
            label="JMA typhoon",
        )
        pos = analysis.get("position") if isinstance(analysis.get("position"), dict) else {}
        deg = pos.get("deg") if isinstance(pos.get("deg"), list) else None
        lat = lon = None
        if isinstance(deg, list) and len(deg) >= 2:
            lat, lon = _num(deg[0]), _num(deg[1])
        wind = analysis.get("maximumWind") if isinstance(analysis.get("maximumWind"), dict) else {}
        sustained = wind.get("sustained") if isinstance(wind.get("sustained"), dict) else {}
        intensity = _num(sustained.get("kt"))
        pressure = _num(analysis.get("pressure"))
        if lat is None or lon is None or intensity is None:
            raise DeviceOffline(f"{self.device_id}: JMA typhoon missing center/intensity")
        return {
            "intensity_kn": intensity,
            "pressure_hpa": pressure,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        storms = _cached(self.url, lambda: self._fetch(self.url))
        if not isinstance(storms, list) or not storms or not isinstance(storms[0], dict):
            raise DeviceOffline(f"{self.device_id}: JMA typhoon empty (off-season)")
        tc = str(storms[0].get("tropicalCyclone") or "").strip()
        if not re.fullmatch(r"TC[0-9]{4}", tc):
            raise DeviceOffline(f"{self.device_id}: JMA typhoon id refused")
        spec_url = _JMA_TC_SPEC.format(tc=tc)
        require_approved_source(self.policy_id).require_endpoint(spec_url)
        spec = _cached(spec_url, lambda: self._fetch(spec_url))
        merged = [{**storms[0], "specifications": spec}]
        return {k: v for k, v in self.map(merged).items() if v is not None}


class InmetWeather(LiveDevice):
    """INMET Brazil WIS2 SYNOP — WMO core unrestricted. HTTPS wis2bra.inmet.gov.br only."""

    model = "GAIA-WEATHER (INMET)"
    policy_id = "inmet_br"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "INMET Brazil WIS2 SYNOP via wis2bra.inmet.gov.br OGC API · WMO core unrestricted "
        "— cite INMET. HTTPS only. Brazil in-situ only, not a forecast."
    )
    timeout = 20.0

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_BR.fullmatch(code):
            raise ValueError(f"invalid INMET traditional id: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = self._items_url()
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def _items_url(self) -> str:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        west, south = self.longitude - 0.6, self.latitude - 0.6
        east, north = self.longitude + 0.6, self.latitude + 0.6
        return (
            f"{_INMET_ITEMS}?bbox={west:.4f},{south:.4f},{east:.4f},{north:.4f}"
            f"&datetime={start}/{end}&limit=80"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: INMET SYNOP empty")
        by_name: dict[str, tuple[float, str, float, float]] = {}
        latest_when = ""
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            wigos = str(props.get("wigos_station_identifier") or props.get("id") or "")
            trad = str(props.get("traditional_station_identifier") or "")
            if self.station not in wigos and trad != self.station:
                # bbox query: accept any feature in the city cell when traditional id is absent
                if trad and trad != self.station:
                    continue
            name = str(props.get("name") or "").lower()
            value = _num(props.get("value"))
            when = str(props.get("reportTime") or props.get("phenomenonTime") or "")
            centroid = _geom_centroid(feat.get("geometry"))
            lat = centroid[0] if centroid else self.latitude
            lon = centroid[1] if centroid else self.longitude
            if value is None or not name:
                continue
            if when >= latest_when:
                latest_when = when
            prev = by_name.get(name)
            if prev is None or when >= prev[1]:
                by_name[name] = (value, when, lat, lon)
        if latest_when:
            _reject_stale(self.device_id, latest_when.split("/")[-1], label="INMET")
        def pick(*names: str) -> float | None:
            for name in names:
                if name in by_name:
                    return by_name[name][0]
            return None
        temperature = pick("air_temperature", "airtemperature", "temperature")
        if temperature is not None and temperature > 200.0:
            temperature = temperature - 273.15
        humidity = pick("relative_humidity", "relativehumidity", "humidity")
        pressure = pick("air_pressure", "pressure", "surface_air_pressure")
        if pressure is not None and pressure > 2000.0:
            pressure = pressure / 100.0
        wind = pick("wind_speed", "windspeed")
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: INMET no T/wind near {self.station}")
        lat = lon = None
        for rec in by_name.values():
            lat, lon = rec[2], rec[3]
            break
        return {
            "temperature_c": temperature, "humidity_pct": humidity,
            "pressure_hpa": pressure, "wind_mps": wind,
            "latitude": lat or self.latitude, "longitude": lon or self.longitude,
        }

    def sample(self) -> dict[str, float]:
        self.url = self._items_url()
        require_approved_source(self.policy_id).require_endpoint(self.url)
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class ChmuWeather(LiveDevice):
    """CHMU Czech 10-min climate now dump — CC BY 4.0. Documented WMO mesh."""

    model = "GAIA-WEATHER (CHMU)"
    policy_id = "chmu_cz"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "CHMI / ČHMÚ climate now 10-min JSON · CC BY 4.0 — cite Czech Hydrometeorological "
        "Institute. Czech in-situ only, not a forecast. File dumps are cached."
    )
    timeout = 20.0

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_CZ.fullmatch(code):
            raise ValueError(f"invalid CHMU WMO id: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.url = _CHMU_FILE.format(wmo=code, day=day)
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: CHMU empty")
        _reject_stale(self.device_id, payload.get("datumVytvoreni"), label="CHMU")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        values = inner.get("values") if isinstance(inner, dict) else None
        if not isinstance(values, list) or not values:
            raise DeviceOffline(f"{self.device_id}: CHMU no values for {self.station}")
        latest: dict[str, tuple[str, float]] = {}
        for row in values:
            if not isinstance(row, (list, tuple)) or len(row) < 4:
                continue
            station, element, when, raw = str(row[0]), str(row[1]), str(row[2]), row[3]
            if not station.endswith(self.station):
                continue
            value = _num(raw)
            if value is None:
                continue
            prev = latest.get(element)
            if prev is None or when >= prev[0]:
                latest[element] = (when, value)
        if not latest:
            raise DeviceOffline(f"{self.device_id}: CHMU no rows for {self.station}")
        newest = max(when for when, _ in latest.values())
        _reject_stale(self.device_id, newest, label="CHMU")

        def pick(*names: str) -> float | None:
            for name in names:
                if name in latest:
                    return latest[name][1]
            upper = {k.upper(): v[1] for k, v in latest.items()}
            for name in names:
                if name.upper() in upper:
                    return upper[name.upper()]
            return None

        temperature = pick("T", "T10", "TA", "TEMP")
        humidity = pick("H", "RH", "U", "HUMIDITY")
        pressure = pick("P", "P0", "QFF", "PRESS")
        wind = pick("F", "FF", "WSPD", "WIND")
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: CHMU no T/wind for {self.station}")
        return {
            "temperature_c": temperature, "humidity_pct": humidity,
            "pressure_hpa": pressure, "wind_mps": wind,
            "latitude": self.latitude, "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class KmaAsosWeather(LiveDevice):
    """KMA ASOS hourly — Public Nuri Type 1 (commercial OK). Not AirKorea (Type 3 ND)."""

    model = "GAIA-WEATHER (KMA ASOS)"
    policy_id = "kma_asos"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "KMA ASOS hourly via apis.data.go.kr · Public Nuri Type 1 (commercial reuse OK) — "
        "cite KMA. Korea in-situ only. AirKorea (Type 3 ND) is not used."
    )

    def __init__(
        self, device_id: str, clock: SimClock, *, station: str, api_key: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_KR.fullmatch(code):
            raise ValueError(f"invalid KMA ASOS id: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        key = _require_secret(api_key, name="KMA service key")
        now = datetime.now(timezone.utc) + timedelta(hours=9)
        day = now.strftime("%Y%m%d")
        hour = now.strftime("%H")
        self.url = (
            f"{_KMA_URL}?serviceKey={quote(key)}"
            f"&pageNo=1&numOfRows=10&dataType=JSON&dataCd=ASOS&dateCd=HR"
            f"&stnIds={quote(code)}&startDt={day}&startHh=00&endDt={day}&endHh={hour}"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        body = payload.get("response") if isinstance(payload, dict) else None
        inner = body.get("body") if isinstance(body, dict) else None
        items = inner.get("items") if isinstance(inner, dict) else None
        rows = items.get("item") if isinstance(items, dict) else items
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or not rows:
            raise DeviceOffline(f"{self.device_id}: KMA ASOS empty for {self.station}")
        latest = None
        latest_key = ""
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("tm") or "")
            if latest is None or key >= latest_key:
                latest, latest_key = row, key
        if not latest:
            raise DeviceOffline(f"{self.device_id}: KMA ASOS no rows for {self.station}")
        _reject_stale(self.device_id, latest.get("tm"), label="KMA ASOS", naive_tz=timezone(timedelta(hours=9)))
        temperature = _num(latest.get("ta"))
        humidity = _num(latest.get("hm"))
        pressure = _num(latest.get("pa") or latest.get("ps"))
        wind = _num(latest.get("ws"))
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: KMA ASOS no T/wind for {self.station}")
        return {
            "temperature_c": temperature, "humidity_pct": humidity,
            "pressure_hpa": pressure, "wind_mps": wind,
            "latitude": self.latitude, "longitude": self.longitude,
        }


class GfmFlood(LiveDevice):
    """Copernicus GFM observed flood — token-gated. Distinct from GloFAS WMS. No invented readings."""

    model = "GAIA-FLOOD (GFM)"
    policy_id = "gfm_observed"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Copernicus Global Flood Monitoring observed flood via api.gfm.eodc.eu · CC BY 4.0 "
        "— cite Copernicus EMS / EODC. Observed flood product, not GloFAS WMS, not a gauge."
    )
    url = _GFM_URL
    timeout = 20.0

    def __init__(
        self, device_id: str, clock: SimClock, *, api_token: str,
        latitude: float = 50.0, longitude: float = 10.0, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        token = _require_secret(api_token, name="GFM token")
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": _UA}
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: GFM observed flood empty")
        best = None
        best_score = -1.0
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            centroid = _geom_centroid(feat.get("geometry"))
            if not centroid:
                continue
            score = _num(props.get("severity") or props.get("water_extent") or props.get("score")) or 1.0
            if score >= best_score:
                best_score = score
                best = {
                    "severity_score": float(score),
                    "latitude": centroid[0],
                    "longitude": centroid[1],
                }
        if not best:
            raise DeviceOffline(f"{self.device_id}: GFM had no geolocated flood features")
        return best


# ── Meshes (WGS84 catalog anchors) ────────────────────────────────────────────

GEOSPHERE_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("at-wx-wien-01", "11035", 48.2486, 16.3564, "Wien/Hohe Warte"),
    ("at-wx-graz-01", "11240", 46.9806, 15.4400, "Graz-Thalerhof"),
    ("at-wx-innsbruck-01", "11320", 47.2600, 11.3842, "Innsbruck/Universität"),
    ("at-wx-salzburg-01", "11150", 47.7894, 13.0086, "Salzburg-Flughafen"),
    ("at-wx-linz-01", "11010", 48.2353, 14.1881, "Linz/Hörsching"),
    ("at-wx-klagenfurt-01", "11331", 46.6483, 14.3183, "Klagenfurt-Flughafen"),
    ("at-wx-bregenz-01", "11101", 47.4992, 9.7461, "Bregenz"),
    ("at-wx-eisenstadt-01", "11190", 47.8542, 16.5383, "Eisenstadt"),
    ("at-wx-stpoelten-01", "11389", 48.1997, 15.6311, "St.Pölten Landhaus"),
    ("at-wx-wienstadt-01", "11034", 48.1983, 16.3669, "Wien-Innere Stadt"),
    ("at-wx-villach-01", "11213", 46.6181, 13.8739, "Villach"),
    ("at-wx-lienz-01", "11204", 46.8256, 12.8064, "Lienz"),
    ("at-wx-linzstadt-01", "11060", 48.2964, 14.2856, "Linz-Stadt"),
    ("at-wx-wienerneustadt-01", "11182", 47.8322, 16.2314, "Wiener Neustadt"),
    ("at-wx-innsbruckapt-01", "11121", 47.2600, 11.3567, "Innsbruck-Flughafen"),
)

LHMT_WX_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("lt-wx-vilnius-01", "vilniaus-ams", 54.6260, 25.1071, "Vilnius"),
    ("lt-wx-kaunas-01", "kauno-ams", 54.8840, 23.8359, "Kaunas"),
    ("lt-wx-klaipeda-01", "klaipedos-ams", 55.7314, 21.0916, "Klaipėda"),
    ("lt-wx-siauliai-01", "siauliu-ams", 55.9422, 23.3311, "Šiauliai"),
    ("lt-wx-panevezys-01", "panevezio-ams", 55.7352, 24.4172, "Panevėžys"),
    ("lt-wx-alytus-01", "alytaus-ams", 54.4124, 24.0633, "Alytus"),
    ("lt-wx-utena-01", "utenos-ams", 55.5153, 25.5897, "Utena"),
    ("lt-wx-marijampole-01", "marijampoles-ams", 54.5289, 23.3518, "Marijampolė"),
    ("lt-wx-telsiai-01", "telsiu-ams", 55.9912, 22.2567, "Telšiai"),
    ("lt-wx-taurage-01", "taurages-ams", 55.2568, 22.2780, "Tauragė"),
    ("lt-wx-nida-01", "nidos-ams", 55.3022, 21.0074, "Nida"),
    ("lt-wx-mazeikiai-01", "mazeikiu-ams", 56.3539, 22.3177, "Mažeikiai"),
)

LHMT_HYDRO_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("lt-hydro-kaunas-01", "kauno-vms", 54.8821, 23.9269, "Nemunas at Kaunas"),
    ("lt-hydro-druskininkai-01", "druskininku-vms", 54.0187, 23.9836, "Nemunas at Druskininkai"),
    ("lt-hydro-smalininkai-01", "smalininku-vms", 55.0723, 22.5859, "Nemunas at Smalininkai"),
    ("lt-hydro-panemune-01", "panemunes-vms", 55.0865, 21.9028, "Nemunas at Panemunė"),
    ("lt-hydro-vilnius-01", "vilniaus-neris-vms", 54.6919, 25.2763, "Neris at Vilnius"),
    ("lt-hydro-jonava-01", "jonavos-vms", 55.0750, 24.2920, "Neris at Jonava"),
    ("lt-hydro-birstonas-01", "birstono-vms", 54.6135, 24.0336, "Kauno marios at Birštonas"),
    ("lt-hydro-lampedziai-01", "lampedziu-vms", 54.9064, 23.8176, "Nemunas at Lampėdžiai"),
)

LVGMC_WX_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("lv-wx-riga-01", "RIGASLU", 56.9548, 24.1047, "Rīga Universitāte"),
    ("lv-wx-daugavpils-01", "RIDM99MS", 55.8700, 26.6175, "Daugavpils"),
    ("lv-wx-liepaja-01", "RILP99PA", 56.4754, 21.0206, "Liepāja"),
    ("lv-wx-ventspils-01", "RIVE99PA", 57.3956, 21.5372, "Ventspils"),
    ("lv-wx-jelgava-01", "RIJE99PA", 56.5569, 23.9642, "Jelgava"),
    ("lv-wx-rezekne-01", "RIREZEKN", 56.4800, 27.3572, "Rēzekne"),
    ("lv-wx-aluksne-01", "RIAL99MS", 57.4396, 27.0354, "Alūksne"),
    ("lv-wx-saldus-01", "RISA99PA", 56.7103, 22.4267, "Saldus"),
    ("lv-wx-skulte-01", "RISE99MS", 57.3006, 24.4122, "Skulte"),
    ("lv-wx-dobele-01", "RIDO99MS", 56.6200, 23.3197, "Dobele"),
    ("lv-wx-stende-01", "RIST99PA", 57.1833, 22.5508, "Stende"),
    ("lv-wx-ainazi-01", "RIAI99PA", 57.8678, 24.3658, "Ainaži"),
)

LVGMC_HYDRO_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("lv-hydro-riga-01", "HD073810", 57.0325, 24.1339, "Daugava at Rīga"),
    ("lv-hydro-daugavpils-01", "HD073141", 55.8614, 26.5211, "Daugava at Daugavpils"),
    ("lv-hydro-jekabpils-01", "HD073151", 56.4978, 25.8914, "Daugava at Jēkabpils"),
    ("lv-hydro-ogre-01", "HD073401", 56.8128, 24.6417, "Daugava at Ogre"),
    ("lv-hydro-plavinas-01", "HD073904", 56.6164, 25.7297, "Daugava at Pļaviņas"),
    ("lv-hydro-jelgava-01", "HD073801", 56.6550, 23.7350, "Lielupe at Jelgava"),
    ("lv-hydro-daugavgriva-01", "DAUGAVGR", 57.0600, 24.0211, "Daugavgrīva"),
    ("lv-hydro-lielupe-01", "SEJU99MS", 56.9833, 23.8875, "Lielupes grīva"),
)

VIC_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("vic-meuse-01", "2", 49.1193, 6.1757, "Meuse-Moselle"),
    ("vic-rhin-01", "3", 48.5734, 7.7521, "Rhin-Sarre"),
    ("vic-seineaval-01", "4", 49.4431, 1.0993, "Seine aval-Côtiers Normands"),
    ("vic-seineamont-01", "6", 48.2973, 4.0744, "Seine amont-Marne amont"),
    ("vic-seinemoy-01", "7", 48.8566, 2.3522, "Seine moyenne-Yonne-Loing"),
    ("vic-vilaine-01", "8", 48.1173, -1.6778, "Vilaine-Côtiers Bretons"),
    ("vic-maine-01", "9", 47.2184, -1.5536, "Maine-Loire aval"),
    ("vic-rhoneamont-01", "18", 45.7640, 4.8357, "Rhône amont-Saône"),
    ("vic-alpes-01", "19", 45.1885, 5.7245, "Alpes du Nord"),
    ("vic-delta-01", "20", 43.9493, 4.8055, "Grand Delta"),
    ("vic-medouest-01", "21", 43.6108, 3.8767, "Méditerranée Ouest"),
    ("vic-medest-01", "22", 43.7102, 7.2620, "Méditerranée Est"),
    ("vic-garonne-01", "25", 43.6047, 1.4442, "Garonne-Tarn-Lot"),
    ("vic-corse-01", "26", 41.9267, 8.7369, "Méditerranée Est Corse"),
    ("vic-nord-01", "29", 50.6292, 3.0573, "Bassins du Nord"),
    ("vic-loire-01", "30", 47.9029, 1.9093, "Loire-Allier-Cher-Indre"),
    ("vic-vienne-01", "31", 46.5802, 0.3404, "Vienne-Charente-Atlantique"),
    ("vic-gironde-01", "32", 44.8378, -0.5792, "Gironde-Adour-Dordogne"),
    ("vic-guyane-01", "973", 4.9371, -52.3260, "Guyane"),
)

RTE_MESH: tuple[tuple[str, float, float, str], ...] = (
    ("rte-grid-01", 48.8566, 2.3522, "France national (Paris pin)"),
)

OPW_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("ie-river-athlone-01", "26027", 53.4215, -7.9408, "Shannon at Athlone"),
    ("ie-river-shannonbridge-01", "26028", 53.2799, -8.0496, "Shannon at Shannonbridge"),
    ("ie-river-galway-01", "30099", 53.2780, -9.0563, "Corrib at Galway Barrage"),
    ("ie-river-limerick-01", "24063", 52.6585, -8.6445, "Shannon at Limerick Dock"),
    ("ie-river-carlow-01", "14001", 52.8342, -6.9380, "Barrow at Carlow"),
    ("ie-river-nore-01", "15002", 52.6534, -7.2504, "Nore at John's Bridge"),
    ("ie-river-carricksuir-01", "16062", 52.3441, -7.4104, "Suir at Carrick-on-Suir"),
    ("ie-river-boyne-01", "7007", 53.4531, -6.9588, "Boyne Aqueduct"),
    ("ie-river-trim-01", "7005", 53.5564, -6.7918, "Boyne at Trim"),
    ("ie-river-enniscorthy-01", "12002", 52.5023, -6.5669, "Slaney at Enniscorthy"),
    ("ie-river-shannonapt-01", "27069", 52.6787, -8.9179, "Shannon Airport"),
    ("ie-river-dundalk-01", "6061", 54.0077, -6.3855, "Dundalk Port"),
    ("ie-river-fermoy-01", "18106", 52.1386, -8.2766, "Blackwater at Fermoy"),
    ("ie-river-carrickshannon-01", "26324", 53.9432, -8.0956, "Shannon at Carrick-on-Shannon"),
    ("ie-river-barrow-01", "14022", 52.8461, -6.9318, "Barrow New Bridge"),
    ("ie-river-duleek-01", "8011", 53.6562, -6.4077, "Nanny at Duleek"),
)

JMA_AMEDAS_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("jp-wx-tokyo-01", "44132", 35.6917, 139.7500, "Tokyo"),
    ("jp-wx-osaka-01", "62078", 34.6817, 135.5183, "Osaka"),
    ("jp-wx-sapporo-01", "14163", 43.0600, 141.3283, "Sapporo"),
    ("jp-wx-fukuoka-01", "82182", 33.5817, 130.3750, "Fukuoka"),
    ("jp-wx-nagoya-01", "51106", 35.1667, 136.9650, "Nagoya"),
    ("jp-wx-kyoto-01", "61286", 35.0133, 135.7317, "Kyoto"),
    ("jp-wx-hiroshima-01", "67437", 34.3983, 132.4617, "Hiroshima"),
    ("jp-wx-sendai-01", "34392", 38.2617, 140.8967, "Sendai"),
    ("jp-wx-naha-01", "91197", 26.2067, 127.6867, "Naha"),
    ("jp-wx-niigata-01", "54232", 37.8933, 139.0183, "Niigata"),
    ("jp-wx-kagoshima-01", "88317", 31.5550, 130.5467, "Kagoshima"),
    ("jp-wx-kanazawa-01", "56227", 36.5883, 136.6333, "Kanazawa"),
    ("jp-wx-takamatsu-01", "72086", 34.3183, 134.0533, "Takamatsu"),
)

INMET_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("br-wx-saopaulo-01", "83780", -23.6267, -46.6556, "São Paulo"),
    ("br-wx-rio-01", "83743", -22.8953, -43.1631, "Rio de Janeiro"),
    ("br-wx-brasilia-01", "83378", -15.7894, -47.9258, "Brasília"),
    ("br-wx-bh-01", "83587", -19.9320, -43.9378, "Belo Horizonte"),
    ("br-wx-salvador-01", "83229", -12.9083, -38.3225, "Salvador"),
    ("br-wx-recife-01", "82900", -8.0594, -34.9200, "Recife"),
    ("br-wx-fortaleza-01", "82397", -3.7758, -38.5328, "Fortaleza"),
    ("br-wx-manaus-01", "82331", -3.1033, -60.0164, "Manaus"),
    ("br-wx-belem-01", "82191", -1.4378, -48.4600, "Belém"),
    ("br-wx-curitiba-01", "83842", -25.4361, -49.2669, "Curitiba"),
    ("br-wx-portoalegre-01", "83967", -30.0531, -51.1669, "Porto Alegre"),
    ("br-wx-goiania-01", "83423", -16.6422, -49.2203, "Goiânia"),
)

CHMU_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("cz-wx-prague-01", "11518", 50.1003, 14.2556, "Praha-Ruzyně"),
    ("cz-wx-brno-01", "11723", 49.1531, 16.6889, "Brno-Tuřany"),
    ("cz-wx-ostrava-01", "11782", 49.6919, 18.1128, "Ostrava-Mošnov"),
    ("cz-wx-plzen-01", "11450", 49.7647, 13.3789, "Plzeň-Mikulka"),
    ("cz-wx-cheb-01", "11406", 50.0683, 12.3914, "Cheb"),
    ("cz-wx-kvary-01", "11414", 50.2017, 12.9142, "Karlovy Vary"),
    ("cz-wx-liberec-01", "11603", 50.7697, 15.0239, "Liberec"),
    ("cz-wx-budejovice-01", "11546", 48.9519, 14.4697, "České Budějovice"),
    ("cz-wx-usti-01", "11502", 50.6833, 14.0411, "Ústí nad Labem"),
    ("cz-wx-pardubice-01", "11652", 50.0161, 15.7403, "Pardubice"),
    ("cz-wx-prostejov-01", "11747", 49.4525, 17.1347, "Prostějov"),
    ("cz-wx-primda-01", "11423", 49.6694, 12.6781, "Přimda"),
)

KMA_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("kr-wx-seoul-01", "108", 37.5714, 126.9658, "Seoul"),
    ("kr-wx-busan-01", "159", 35.1047, 129.0320, "Busan"),
    ("kr-wx-incheon-01", "112", 37.4777, 126.6249, "Incheon"),
    ("kr-wx-daegu-01", "143", 35.8780, 128.6530, "Daegu"),
    ("kr-wx-gwangju-01", "156", 35.1730, 126.8910, "Gwangju"),
    ("kr-wx-daejeon-01", "133", 36.3720, 127.3720, "Daejeon"),
    ("kr-wx-jeju-01", "184", 33.5141, 126.5297, "Jeju"),
)

def _p12_open_count() -> int:
    total = 0
    _slice_counts: tuple[tuple[str, str], ...] = (
        ("gaia.devices.live_p12_at", "P12_AT_COUNT"),
        ("gaia.devices.live_p12_lt", "P12_LT_COUNT"),
        ("gaia.devices.live_p12_lv", "P12_LV_COUNT"),
        ("gaia.devices.live_p12_flood", "P12_FLOOD_COUNT"),
        ("gaia.devices.live_p12_rte", "P12_RTE_COUNT"),
        ("gaia.devices.live_p12_opw", "P12_OPW_COUNT"),
        ("gaia.devices.live_p12_cz", "P12_CZ_PIN_COUNT"),
    )
    for mod_name, attr in _slice_counts:
        try:
            mod = __import__(mod_name, fromlist=[attr])
            total += int(getattr(mod, attr))
        except (ImportError, AttributeError):
            continue
    return total


# Open P12 slice total (excludes JMA — live_p12_jma; INMET not shipped; KMA is keyed).
P12_OPEN_COUNT = _p12_open_count()
P12_KEYED_COUNT = len(KMA_MESH) + 1  # gfm-flood-01


def _register_p12_gfm(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    gfm_token = _env("GAIA_GFM_TOKEN")
    if not gfm_token or not enabled("GAIA_GFM_ENABLED", "1"):
        if enabled("GAIA_GFM_ENABLED", "1"):
            log.info("GFM skipped (set GAIA_GFM_TOKEN to enable)")
        return 0
    try:
        fleet.add(GfmFlood(
            "gfm-flood-01", clock, api_token=gfm_token,
            site="live-flood-gfm", key_dir=key_dir,
        ))
        return 1
    except ValueError as exc:
        log.warning("GFM skipped: %s", exc)
        return 0


def register_p12_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P12 slice relays (not JMA — use register_p12_jma) plus optional GFM."""

    n = 0
    _slice_registrars: tuple[tuple[str, str], ...] = (
        ("gaia.devices.live_p12_at", "register_p12_at"),
        ("gaia.devices.live_p12_lt", "register_p12_lt"),
        ("gaia.devices.live_p12_lv", "register_p12_lv"),
        ("gaia.devices.live_p12_flood", "register_p12_flood"),
        ("gaia.devices.live_p12_rte", "register_p12_rte"),
        ("gaia.devices.live_p12_opw", "register_p12_opw"),
        ("gaia.devices.live_p12_cz", "register_p12_cz"),
        ("gaia.devices.live_p12_kma", "register_p12_kma"),
    )
    for mod_name, fn_name in _slice_registrars:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            register = getattr(mod, fn_name)
        except ImportError as exc:
            log.warning("P12 slice %s unavailable: %s", mod_name, exc)
            continue
        try:
            added = register(fleet, clock, key_dir=key_dir)
        except Exception as exc:
            log.warning("P12 slice %s failed: %s", mod_name, exc)
            continue
        n += int(added or 0)
    n += _register_p12_gfm(fleet, clock, key_dir=key_dir)
    return n


__all__ = [
    "GeosphereWeather",
    "LhmtWeather",
    "LhmtRiver",
    "LvgmcWeather",
    "LvgmcRiver",
    "VicFlood",
    "RteGrid",
    "OpwRiver",
    "JmaAmedasWeather",
    "JmaQuake",
    "JmaTyphoon",
    "InmetWeather",
    "ChmuWeather",
    "KmaAsosWeather",
    "GfmFlood",
    "GEOSPHERE_MESH",
    "LHMT_WX_MESH",
    "LHMT_HYDRO_MESH",
    "LVGMC_WX_MESH",
    "LVGMC_HYDRO_MESH",
    "VIC_MESH",
    "RTE_MESH",
    "OPW_MESH",
    "JMA_AMEDAS_MESH",
    "INMET_MESH",
    "CHMU_MESH",
    "KMA_MESH",
    "P12_OPEN_COUNT",
    "P12_KEYED_COUNT",
    "register_p12_relays",
]
