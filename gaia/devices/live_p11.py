"""P11 commercially-clear LIVE relays — Iberia / Alps / Taiwan / Baltics / HK air.

* AEMET OpenData (ES weather) — Spanish PSI reuse + «Fuente: AEMET»; key-gated.
* MeteoSwiss OGD (CH weather) — CC BY 4.0; GeoJSON is LV95, converted to WGS84.
* BAFU/FOEN hydro via LINDAS JSON-LD (CH rivers) — OGD-CH Open-Use.
* Taiwan CWA OpenData — OGDL 1.0 (≡ CC BY 4.0); key-gated.
* Météo-France DPObs — Etalab Licence Ouverte 2.0; OAuth application id.
* Estonia EWS XML — CC BY 4.0.
* Iceland IMO AWS — ODC-By.
* Hong Kong EPD AQHI — DATA.GOV.HK (same rail as HKO). AQHI ≠ PM2.5.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from gaia.clock import SimClock
from gaia.devices._live_base import _UA
from gaia.devices._policy import _assert_url_allowed
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p11")

_SAFE_AEMET = re.compile(r"^[A-Z0-9]{3,8}$")
_SAFE_CWA = re.compile(r"^[0-9]{5}$")
_SAFE_MF = re.compile(r"^[0-9]{5,8}$")
_SAFE_BAFU = re.compile(r"^[0-9]{4,5}$")
_SAFE_IS = re.compile(r"^[0-9]{3,5}$")
_SAFE_SECRET = re.compile(r"^[A-Za-z0-9._:~+/-]{8,256}$")
_MAX_AGE_S = 6 * 3600
_CACHE_TTL_S = 120.0
_TOKEN_SKEW_S = 60.0

# Shared GeoJSON / XML / station-list feeds (one upstream GET per TTL, not per pin).
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_MF_TOKEN: dict[str, Any] = {"token": "", "exp": 0.0}
_MF_TOKEN_LOCK = threading.Lock()

_CH_TEMP_URL = (
    "https://data.geo.admin.ch/ch.meteoschweiz.messwerte-lufttemperatur-10min/"
    "ch.meteoschweiz.messwerte-lufttemperatur-10min_en.json"
)
_CH_RH_URL = (
    "https://data.geo.admin.ch/ch.meteoschweiz.messwerte-luftfeuchtigkeit-10min/"
    "ch.meteoschweiz.messwerte-luftfeuchtigkeit-10min_en.json"
)
_CH_QFF_URL = (
    "https://data.geo.admin.ch/ch.meteoschweiz.messwerte-luftdruck-qff-10min/"
    "ch.meteoschweiz.messwerte-luftdruck-qff-10min_en.json"
)
_CH_WIND_URL = (
    "https://data.geo.admin.ch/ch.meteoschweiz.messwerte-windgeschwindigkeit-kmh-10min/"
    "ch.meteoschweiz.messwerte-windgeschwindigkeit-kmh-10min_en.json"
)
_EE_URL = "https://www.ilmateenistus.ee/ilma_andmed/xml/observations.php"
_IS_OBS_URL = "https://api.vedur.is/weather/observations/aws/hour/latest?parameters=basic"
_HK_AQHI_URL = "https://dashboard.data.gov.hk/api/aqhi-individual?format=json"
_CWA_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001"
_MF_TOKEN_URL = "https://portail-api.meteofrance.fr/token"
_MF_OBS_URL = "https://public-api.meteofrance.fr/public/DPObs/v1/station/horaire"

_LD_DISCHARGE = "https://environment.ld.admin.ch/foen/hydro/dimension/discharge"
_LD_LEVEL = "https://environment.ld.admin.ch/foen/hydro/dimension/waterLevel"
_LD_TIME = "https://environment.ld.admin.ch/foen/hydro/dimension/measurementTime"


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
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
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


def lv95_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """Approximate swisstopo LV95 → WGS84 (enough for a map pin, not a survey)."""
    y = (float(easting) - 2_600_000.0) / 1_000_000.0
    x = (float(northing) - 1_200_000.0) / 1_000_000.0
    lon_aux = (
        2.6779094
        + 4.728982 * y
        + 0.791484 * y * x
        + 0.1306 * y * x * x
        - 0.0436 * y ** 3
    )
    lat_aux = (
        16.9023892
        + 3.238237 * x
        - 0.270978 * y * y
        - 0.002528 * x * x
        - 0.0447 * y * y * x
        - 0.0140 * x ** 3
    )
    return lat_aux * 100.0 / 36.0, lon_aux * 100.0 / 36.0


def _in_switzerland(lat: float, lon: float) -> bool:
    return 45.7 <= lat <= 47.9 and 5.8 <= lon <= 10.6


def _ld_scalar(node: Any) -> Any:
    if isinstance(node, dict) and "@value" in node:
        return node.get("@value")
    return node


def _ld_pick(payload: dict[str, Any], *needles: str) -> Any:
    for key, value in payload.items():
        tail = str(key).rsplit("/", 1)[-1]
        if key in needles or tail in needles:
            return _ld_scalar(value)
    return None


def _geojson_feature(payload: Any, station_name: str) -> dict[str, Any] | None:
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return None
    for row in features:
        if not isinstance(row, dict):
            continue
        props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        if str(props.get("station_name") or "") == station_name:
            return row
    return None


def _geojson_value(payload: Any, station_name: str) -> tuple[float | None, float | None, float | None]:
    feature = _geojson_feature(payload, station_name)
    if not feature:
        return None, None, None
    props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    value = _num(props.get("value"))
    geom = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
    coords = geom.get("coordinates") if isinstance(geom, dict) else None
    lat = lon = None
    if isinstance(coords, list) and len(coords) >= 2:
        easting, northing = _num(coords[0]), _num(coords[1])
        if easting is not None and northing is not None and easting > 1_000_000:
            wlat, wlon = lv95_to_wgs84(easting, northing)
            if _in_switzerland(wlat, wlon):
                lat, lon = wlat, wlon
    return value, lat, lon


def _aqhi_index(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text.endswith("+"):
        base = _num(text[:-1])
        return (base + 1.0) if base is not None else None
    return _num(text)


class AemetWeather(LiveDevice):
    """AEMET conventional in-situ observation — Spain, PSI reuse + attribution."""

    model = "GAIA-WEATHER (AEMET)"
    policy_id = "aemet_es"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "AEMET OpenData conventional observation · Spanish PSI reuse — "
        "Fuente: AEMET. Spain in-situ only, not a forecast."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str,
        api_key: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip().upper()
        if not _SAFE_AEMET.fullmatch(code):
            raise ValueError(f"invalid AEMET idema: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        key = _require_secret(api_key, name="AEMET API key")
        self.headers = {"api_key": key, "Accept": "application/json"}
        policy = require_approved_source(self.policy_id)
        self.url = (
            "https://opendata.aemet.es/opendata/api/observacion/convencional/"
            f"datos/estacion/{quote(code)}"
        )
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else None
        if not isinstance(rows, list) or not rows:
            raise DeviceOffline(f"{self.device_id}: AEMET empty for {self.station}")
        latest: dict[str, Any] | None = None
        latest_key = ""
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("fint") or row.get("fhora") or "")
            if latest is None or key >= latest_key:
                latest, latest_key = row, key
        if not latest:
            raise DeviceOffline(f"{self.device_id}: AEMET no rows for {self.station}")
        _reject_stale(self.device_id, latest.get("fint") or latest.get("fhora"), label="AEMET")
        temperature = _num(latest.get("ta"))
        humidity = _num(latest.get("hr"))
        pressure = _num(latest.get("pres"))
        wind = _num(latest.get("vv"))
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: AEMET no T/wind for {self.station}")
        lat = _num(latest.get("lat")) or self.latitude
        lon = _num(latest.get("lon")) or self.longitude
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        meta = self._fetch(self.url)
        if not isinstance(meta, dict):
            raise DeviceOffline(f"{self.device_id}: AEMET metadata empty")
        estado = meta.get("estado")
        if estado not in (200, "200", None):
            raise DeviceOffline(f"{self.device_id}: AEMET estado {estado}")
        datos = str(meta.get("datos") or "").strip()
        parsed = urlsplit(datos)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or host != "opendata.aemet.es":
            raise DeviceOffline(f"{self.device_id}: AEMET datos host refused")
        require_approved_source(self.policy_id).require_endpoint(datos)
        payload = self._fetch(datos)
        return {k: v for k, v in self.map(payload).items() if v is not None}


class MeteoSwissWeather(LiveDevice):
    """MeteoSwiss 10-min in-situ — Switzerland, CC BY 4.0."""

    model = "GAIA-WEATHER (MeteoSwiss)"
    policy_id = "meteoswiss_ch"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "MeteoSwiss OGD 10-min observations via data.geo.admin.ch · CC BY 4.0 — "
        "cite MeteoSwiss. Switzerland in-situ only, not a forecast. "
        "Source GeoJSON is LV95; readings are converted to WGS84."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station_name: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        self.station_name = _require_place(station_name, what="MeteoSwiss station")
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self._feeds = {
            "temperature_c": _CH_TEMP_URL,
            "humidity_pct": _CH_RH_URL,
            "pressure_hpa": _CH_QFF_URL,
            "wind_kmh": _CH_WIND_URL,
        }
        for url in self._feeds.values():
            policy.require_endpoint(url)

    def map(self, payload: Any) -> dict[str, float | None]:
        feeds = payload if isinstance(payload, dict) else {}
        temperature, tlat, tlon = _geojson_value(feeds.get("temperature_c"), self.station_name)
        humidity, _, _ = _geojson_value(feeds.get("humidity_pct"), self.station_name)
        pressure, _, _ = _geojson_value(feeds.get("pressure_hpa"), self.station_name)
        wind_kmh, wlat, wlon = _geojson_value(feeds.get("wind_kmh"), self.station_name)
        if temperature is None and wind_kmh is None:
            raise DeviceOffline(
                f"{self.device_id}: MeteoSwiss empty for {self.station_name!r}"
            )
        wind = (wind_kmh / 3.6) if wind_kmh is not None else None
        lat = tlat or wlat or self.latitude
        lon = tlon or wlon or self.longitude
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        feeds = {
            field: _cached(url, lambda u=url: self._fetch(u))
            for field, url in self._feeds.items()
        }
        return {k: v for k, v in self.map(feeds).items() if v is not None}


class BafuRiver(LiveDevice):
    """BAFU/FOEN hydro observation — Switzerland, OGD-CH Open-Use.

    ``gage_height_m`` is a geodetic water level (m a.s.l.), not a USGS-style stage.
    Flood dangerLevel is a warning product and is not sold on this SKU.
    """

    model = "GAIA-RIVER (BAFU)"
    policy_id = "bafu_hydro"
    fields = {
        "discharge_m3s": "m3/s",
        "gage_height_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "BAFU/FOEN hydrology via LINDAS · OGD-CH Open-Use — attribution required. "
        "Switzerland in-situ only. Water level is geodetic metres a.s.l., not USGS stage. "
        "Not PEGELONLINE, not eHYD, not Hub'Eau, not a flood warning."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_BAFU.fullmatch(code):
            raise ValueError(f"invalid BAFU station id: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.headers = {"Accept": "application/ld+json, application/json"}
        policy = require_approved_source(self.policy_id)
        self.url = (
            "https://environment.ld.admin.ch/foen/hydro/river/observation/"
            f"{quote(code)}"
        )
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: BAFU empty for {self.station}")
        flow = _num(_ld_pick(payload, _LD_DISCHARGE, "discharge"))
        level = _num(_ld_pick(payload, _LD_LEVEL, "waterLevel"))
        when = _ld_pick(payload, _LD_TIME, "measurementTime")
        _reject_stale(self.device_id, when, label="BAFU")
        if flow is not None and not (0.0 <= flow <= 20_000.0):
            flow = None
        if level is not None and not (0.0 <= level <= 5_000.0):
            level = None
        if flow is None and level is None:
            raise DeviceOffline(f"{self.device_id}: BAFU no Q/H for {self.station}")
        return {
            "discharge_m3s": flow,
            "gage_height_m": level,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


class CwaWeather(LiveDevice):
    """Taiwan CWA current weather — OGDL 1.0 (≡ CC BY 4.0)."""

    model = "GAIA-WEATHER (CWA)"
    policy_id = "cwa_tw"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Taiwan CWA OpenData O-A0003-001 · Open Government Data License 1.0 "
        "(compatible with CC BY 4.0) — cite CWA. Taiwan in-situ only, not a forecast."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str,
        api_key: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_CWA.fullmatch(code):
            raise ValueError(f"invalid CWA StationId: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        key = _require_secret(api_key, name="CWA API key")
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(_CWA_URL)
        self.url = f"{_CWA_URL}?Authorization={quote(key)}"

    def map(self, payload: Any) -> dict[str, float | None]:
        records = payload.get("records") if isinstance(payload, dict) else None
        stations = records.get("Station") if isinstance(records, dict) else None
        if not isinstance(stations, list):
            raise DeviceOffline(f"{self.device_id}: CWA empty")
        row = None
        for item in stations:
            if not isinstance(item, dict):
                continue
            if str(item.get("StationId") or "") == self.station:
                row = item
                break
        if not row:
            raise DeviceOffline(f"{self.device_id}: CWA no station {self.station}")
        elements = row.get("WeatherElement") if isinstance(row.get("WeatherElement"), dict) else {}
        obs_time = row.get("ObsTime") if isinstance(row.get("ObsTime"), dict) else {}
        _reject_stale(self.device_id, obs_time.get("DateTime"), label="CWA")
        temperature = _num(elements.get("AirTemperature"))
        humidity = _num(elements.get("RelativeHumidity"))
        pressure = _num(elements.get("AirPressure"))
        wind = _num(elements.get("WindSpeed"))
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: CWA no T/wind for {self.station}")
        lat, lon = self.latitude, self.longitude
        geo = row.get("GeoInfo") if isinstance(row.get("GeoInfo"), dict) else {}
        coords = geo.get("Coordinates") if isinstance(geo, dict) else None
        if isinstance(coords, list):
            for entry in coords:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("CoordinateName") or "") in ("WGS84", "TWD67", ""):
                    glat = _num(entry.get("StationLatitude"))
                    glon = _num(entry.get("StationLongitude"))
                    if glat is not None and glon is not None and -90 <= glat <= 90:
                        lat, lon = glat, glon
                        if str(entry.get("CoordinateName") or "") == "WGS84":
                            break
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class MeteoFranceWeather(LiveDevice):
    """Météo-France DPObs hourly station — Etalab Licence Ouverte 2.0."""

    model = "GAIA-WEATHER (Météo-France)"
    policy_id = "meteofrance_dpobs"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Météo-France DPObs hourly station observations · Etalab Licence Ouverte 2.0. "
        "France in-situ only, not a forecast, not Hub'Eau."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str,
        application_id: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_MF.fullmatch(code):
            raise ValueError(f"invalid Météo-France station id: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self._application_id = _require_secret(application_id, name="Météo-France application id")
        policy = require_approved_source(self.policy_id)
        self.url = f"{_MF_OBS_URL}?id_station={quote(code)}&format=json"
        policy.require_endpoint(self.url)
        policy.require_endpoint(_MF_TOKEN_URL)

    def _token(self) -> str:
        now = time.time()
        with _MF_TOKEN_LOCK:
            if _MF_TOKEN["token"] and now < float(_MF_TOKEN["exp"]) - _TOKEN_SKEW_S:
                return str(_MF_TOKEN["token"])
        url = _assert_url_allowed(_MF_TOKEN_URL)
        basic = base64.b64encode(f"{self._application_id}:".encode("ascii")).decode("ascii")
        try:
            resp = httpx.post(
                url,
                headers={
                    "User-Agent": _UA,
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
                timeout=self.timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise DeviceOffline(
                f"{self.device_id}: Météo-France token unreachable ({type(exc).__name__})"
            ) from exc
        if resp.status_code != 200:
            raise DeviceOffline(f"{self.device_id}: Météo-France token HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: Météo-France token not JSON") from exc
        token = str((body or {}).get("access_token") or "").strip()
        if not token:
            raise DeviceOffline(f"{self.device_id}: Météo-France token empty")
        expires = _num((body or {}).get("expires_in")) or 3600.0
        with _MF_TOKEN_LOCK:
            _MF_TOKEN["token"] = token
            _MF_TOKEN["exp"] = now + max(60.0, float(expires))
        return token

    def map(self, payload: Any) -> dict[str, float | None]:
        row: dict[str, Any] | None = None
        if isinstance(payload, list):
            dicts = [item for item in payload if isinstance(item, dict)]
            row = dicts[-1] if dicts else None
        elif isinstance(payload, dict):
            if isinstance(payload.get("properties"), dict):
                row = {**payload["properties"], "geometry": payload.get("geometry")}
            elif isinstance(payload.get("features"), list) and payload["features"]:
                feat = payload["features"][-1] if isinstance(payload["features"][-1], dict) else {}
                props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
                row = {**props, "geometry": feat.get("geometry")}
            else:
                row = payload
        if not row:
            raise DeviceOffline(f"{self.device_id}: Météo-France empty for {self.station}")
        _reject_stale(
            self.device_id,
            row.get("validity_time") or row.get("time") or row.get("date"),
            label="Météo-France",
        )
        temperature = _num(row.get("t") or row.get("t_air") or row.get("temperature"))
        if temperature is not None and temperature > 200.0:
            temperature = temperature - 273.15
        humidity = _num(row.get("u") or row.get("hu") or row.get("humidity"))
        pressure = _num(row.get("pmer") or row.get("pres") or row.get("psta") or row.get("pressure"))
        if pressure is not None and pressure > 2000.0:
            pressure = pressure / 100.0
        wind = _num(row.get("ff") or row.get("ff10m") or row.get("wind"))
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: Météo-France no T/wind for {self.station}")
        lat, lon = self.latitude, self.longitude
        geom = row.get("geometry") if isinstance(row.get("geometry"), dict) else {}
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        if isinstance(coords, list) and len(coords) >= 2:
            glon, glat = _num(coords[0]), _num(coords[1])
            if glat is not None and glon is not None:
                lat, lon = glat, glon
        lat = _num(row.get("lat") or row.get("latitude")) or lat
        lon = _num(row.get("lon") or row.get("longitude")) or lon
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        token = self._token()
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        payload = self._fetch(self.url)
        return {k: v for k, v in self.map(payload).items() if v is not None}


class EstoniaWeather(LiveDevice):
    """Estonian Weather Service synoptic XML — CC BY 4.0."""

    model = "GAIA-WEATHER (Estonia EWS)"
    policy_id = "estonia_ews"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Estonian Weather Service / Keskkonnaagentuur observations XML · CC BY 4.0 — "
        "cite Estonian Environment Agency. Estonia in-situ only, not a forecast."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station_name: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        self.station_name = _require_place(station_name, what="Estonia station")
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self.url = _EE_URL
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        root = payload
        if isinstance(payload, (bytes, str)):
            try:
                root = ET.fromstring(payload)
            except ET.ParseError as exc:
                raise DeviceOffline(f"{self.device_id}: Estonia XML malformed") from exc
        if not isinstance(root, ET.Element):
            raise DeviceOffline(f"{self.device_id}: Estonia empty")
        _reject_stale(self.device_id, root.get("timestamp"), label="Estonia EWS")
        station = None
        for node in root.findall("station"):
            if (node.findtext("name") or "").strip() == self.station_name:
                station = node
                break
        if station is None:
            raise DeviceOffline(
                f"{self.device_id}: Estonia no station {self.station_name!r}"
            )
        temperature = _num(station.findtext("airtemperature"))
        humidity = _num(station.findtext("relativehumidity"))
        pressure = _num(station.findtext("airpressure"))
        wind = _num(station.findtext("windspeed"))
        if temperature is None and wind is None:
            raise DeviceOffline(
                f"{self.device_id}: Estonia no T/wind for {self.station_name!r}"
            )
        lat = _num(station.findtext("latitude")) or self.latitude
        lon = _num(station.findtext("longitude")) or self.longitude
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        text = _cached(self.url, lambda: self._fetch_text(self.url))
        return {k: v for k, v in self.map(text).items() if v is not None}


class IcelandWeather(LiveDevice):
    """Icelandic Met Office AWS hourly — ODC-By."""

    model = "GAIA-WEATHER (Iceland IMO)"
    policy_id = "iceland_imo"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Icelandic Meteorological Office AWS hourly · ODC-By — cite IMO / Veðurstofa. "
        "Iceland in-situ only, not a forecast."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_IS.fullmatch(code):
            raise ValueError(f"invalid Iceland station id: {station!r}")
        self.station = int(code)
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self.url = _IS_OBS_URL
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else None
        if not isinstance(rows, list):
            raise DeviceOffline(f"{self.device_id}: Iceland empty")
        row = None
        for item in rows:
            if not isinstance(item, dict):
                continue
            if _num(item.get("station")) == float(self.station):
                row = item
                break
        if not row:
            raise DeviceOffline(f"{self.device_id}: Iceland no station {self.station}")
        _reject_stale(self.device_id, row.get("time"), label="Iceland IMO")
        temperature = _num(row.get("t"))
        humidity = _num(row.get("rh"))
        pressure = _num(row.get("p"))
        wind = _num(row.get("f"))
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: Iceland no T/wind for {self.station}")
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": _num(row.get("lat")) or self.latitude,
            "longitude": _num(row.get("lon")) or self.longitude,
        }

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class HkAqhiAir(LiveDevice):
    """Hong Kong EPD AQHI — DATA.GOV.HK. Index 1–10 / 10+, not PM2.5."""

    model = "GAIA-AIR (HK AQHI)"
    policy_id = "hongkong_aqhi"
    fields = {
        "air_quality_index": "AQHI",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Hong Kong EPD AQHI via DATA.GOV.HK — commercial reuse free with attribution "
        "to the HKSAR Government / EPD. AQHI is a 1–10 health index (10+ coded as 11), "
        "not a PM2.5 microgram reading and not HKO weather."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station_name: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        self.station_name = _require_place(station_name, what="HK AQHI station")
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self.url = _HK_AQHI_URL
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else None
        if not isinstance(rows, list):
            raise DeviceOffline(f"{self.device_id}: HK AQHI empty")
        row = None
        for item in rows:
            if not isinstance(item, dict):
                continue
            if str(item.get("station") or "") == self.station_name:
                row = item
                break
        if not row:
            raise DeviceOffline(
                f"{self.device_id}: HK AQHI no station {self.station_name!r}"
            )
        _reject_stale(
            self.device_id,
            row.get("publish_date"),
            label="HK AQHI",
            naive_tz=timezone(timedelta(hours=8)),
        )
        index = _aqhi_index(row.get("aqhi"))
        if index is None:
            raise DeviceOffline(
                f"{self.device_id}: HK AQHI null for {self.station_name!r}"
            )
        return {
            "air_quality_index": index,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


# ── Meshes (WGS84 catalog anchors) ────────────────────────────────────────────

AEMET_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("aemet-wx-madrid-01", "3195", 40.4117, -3.6781, "Madrid Retiro"),
    ("aemet-wx-barajas-01", "3129", 40.4667, -3.5556, "Madrid-Barajas"),
    ("aemet-wx-barcelona-01", "0076", 41.2928, 2.0700, "Barcelona Aeropuerto"),
    ("aemet-wx-valencia-01", "8416", 39.4806, -0.3664, "Valencia"),
    ("aemet-wx-sevilla-01", "5783", 37.4167, -5.8981, "Sevilla San Pablo"),
    ("aemet-wx-malaga-01", "6156A", 36.6667, -4.4881, "Málaga Aeropuerto"),
    ("aemet-wx-bilbao-01", "1024E", 43.3011, -2.9106, "Bilbao Aeropuerto"),
    ("aemet-wx-zaragoza-01", "9434", 41.6667, -1.0167, "Zaragoza Aeropuerto"),
    ("aemet-wx-palma-01", "B278", 39.5611, 2.6267, "Palma de Mallorca"),
    ("aemet-wx-grancanaria-01", "C449C", 27.9319, -15.3867, "Gran Canaria / Gando"),
    ("aemet-wx-acoruna-01", "1387", 43.3667, -8.4167, "A Coruña"),
    ("aemet-wx-alicante-01", "8025", 38.3667, -0.4944, "Alicante"),
    ("aemet-wx-valladolid-01", "2444", 41.6519, -4.7286, "Valladolid"),
    ("aemet-wx-santander-01", "1111X", 43.4281, -3.8314, "Santander"),
    ("aemet-wx-pamplona-01", "9263D", 42.7700, -1.6464, "Pamplona"),
)

METEOSWISS_MESH: tuple[tuple[str, str, float, float], ...] = (
    ("ch-wx-zurich-01", "Zürich / Fluntern", 47.3810, 8.5673),
    ("ch-wx-geneva-01", "Genève / Cointrin", 46.2485, 6.1223),
    ("ch-wx-bern-01", "Bern / Zollikofen", 46.9907, 7.4640),
    ("ch-wx-basel-01", "Basel / Binningen", 47.5411, 7.5836),
    ("ch-wx-lugano-01", "Lugano", 46.0038, 8.9601),
    ("ch-wx-sion-01", "Sion", 46.2175, 7.3153),
    ("ch-wx-luzern-01", "Luzern", 47.0364, 8.3010),
    ("ch-wx-chur-01", "Chur", 46.8704, 9.5306),
    ("ch-wx-davos-01", "Davos", 46.8130, 9.8435),
    ("ch-wx-stgallen-01", "St. Gallen", 47.4254, 9.3985),
    ("ch-wx-payerne-01", "Payerne", 46.8116, 6.9424),
    ("ch-wx-locarno-01", "Locarno / Monti", 46.1722, 8.7875),
    ("ch-wx-interlaken-01", "Interlaken", 46.6720, 7.8704),
    ("ch-wx-jungfraujoch-01", "Jungfraujoch", 46.5475, 7.9851),
    ("ch-wx-saentis-01", "Säntis", 47.2494, 9.3434),
)

BAFU_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("bafu-basel-01", "2289", 47.5594, 7.6167, "Rhein at Basel Rheinhalle"),
    ("bafu-rheinfelden-01", "2091", 47.5607, 7.7999, "Rhein at Rheinfelden"),
    ("bafu-rekingen-01", "2143", 47.5703, 8.3298, "Rhein at Rekingen"),
    ("bafu-diepoldsau-01", "2473", 47.3831, 9.6409, "Rhein at Diepoldsau Rietbrücke"),
    ("bafu-bern-01", "2135", 46.9331, 7.4480, "Aare at Bern Schönau"),
    ("bafu-thun-01", "2030", 46.7646, 7.6118, "Aare at Thun"),
    ("bafu-luzern-01", "2152", 47.0540, 8.2985, "Reuss at Luzern Geissmattbrücke"),
    ("bafu-sion-01", "2011", 46.2191, 7.3579, "Rhône at Sion"),
    ("bafu-porteduscex-01", "2009", 46.3496, 6.8886, "Rhône at Porte du Scex"),
    ("bafu-geneve-01", "2170", 46.1803, 6.1593, "Arve at Genève Bout du Monde"),
    ("bafu-chancy-01", "2174", 46.1530, 5.9707, "Rhône at Chancy Aux Ripes"),
    ("bafu-mellingen-01", "2018", 47.4210, 8.2713, "Reuss at Mellingen"),
    ("bafu-brugg-01", "2016", 47.4825, 8.1949, "Aare at Brugg"),
    ("bafu-zurich-01", "2176", 47.3677, 8.5262, "Sihl at Zürich"),
    ("bafu-branson-01", "2024", 46.1257, 7.0913, "Rhône at Branson"),
)

CWA_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("cwa-wx-taipei-01", "46692", 25.0378, 121.5149, "Taipei"),
    ("cwa-wx-taichung-01", "46749", 24.1457, 120.6841, "Taichung"),
    ("cwa-wx-tainan-01", "46741", 22.9933, 120.2048, "Tainan"),
    ("cwa-wx-kaohsiung-01", "46744", 22.5660, 120.3078, "Kaohsiung"),
    ("cwa-wx-hualien-01", "46699", 23.9751, 121.6133, "Hualien"),
    ("cwa-wx-taitung-01", "46766", 22.7522, 121.1546, "Taitung"),
    ("cwa-wx-keelung-01", "46694", 25.1333, 121.7333, "Keelung"),
    ("cwa-wx-hengchun-01", "46759", 21.9583, 120.7444, "Hengchun"),
    ("cwa-wx-yilan-01", "46708", 24.7640, 121.7565, "Yilan"),
    ("cwa-wx-penghu-01", "46695", 23.5656, 119.5631, "Penghu Magong"),
    ("cwa-wx-hsinchu-01", "46757", 24.8278, 120.9736, "Hsinchu"),
    ("cwa-wx-chiayi-01", "46753", 23.4969, 120.4328, "Chiayi"),
)

METEOFRANCE_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("mf-wx-paris-01", "07149", 48.8217, 2.3378, "Paris-Montsouris"),
    ("mf-wx-marseille-01", "07650", 43.4367, 5.2164, "Marignane"),
    ("mf-wx-lyon-01", "07481", 45.7264, 4.9417, "Lyon-Bron"),
    ("mf-wx-nice-01", "07690", 43.6489, 7.2092, "Nice"),
    ("mf-wx-bordeaux-01", "07510", 44.8308, -0.6914, "Bordeaux-Mérignac"),
    ("mf-wx-toulouse-01", "07630", 43.6211, 1.3789, "Toulouse-Blagnac"),
    ("mf-wx-lille-01", "07015", 50.5700, 3.0975, "Lille-Lesquin"),
    ("mf-wx-brest-01", "07110", 48.4442, -4.4120, "Brest-Guipavas"),
    ("mf-wx-strasbourg-01", "07190", 48.5494, 7.6403, "Strasbourg-Entzheim"),
    ("mf-wx-nantes-01", "07222", 47.1500, -1.6089, "Nantes-Bouguenais"),
    ("mf-wx-perpignan-01", "07607", 42.7369, 2.8728, "Perpignan"),
    ("mf-wx-clermont-01", "07460", 45.7867, 3.1492, "Clermont-Ferrand"),
)

ESTONIA_MESH: tuple[tuple[str, str, float, float], ...] = (
    ("ee-wx-tallinn-01", "Tallinn-Harku", 59.3981, 24.6029),
    ("ee-wx-tartu-01", "Tartu-Tõravere", 58.2641, 26.4613),
    ("ee-wx-parnu-01", "Pärnu", 58.3846, 24.4852),
    ("ee-wx-narva-01", "Narva", 59.3895, 28.1093),
    ("ee-wx-kuressaare-01", "Kuressaare linn", 58.2642, 22.4894),
    ("ee-wx-voru-01", "Võru", 57.8463, 27.0195),
    ("ee-wx-johvi-01", "Jõhvi", 59.3290, 27.3983),
    ("ee-wx-viljandi-01", "Viljandi", 58.3778, 25.6002),
    ("ee-wx-haapsalu-01", "Haapsalu", 58.9453, 23.5553),
    ("ee-wx-kunda-01", "Kunda", 59.5214, 26.5414),
    ("ee-wx-valga-01", "Valga", 57.7900, 26.0377),
    ("ee-wx-vilsandi-01", "Vilsandi", 58.3828, 21.8142),
)

ICELAND_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("is-wx-reykjavik-01", "1470", 64.1288, -21.9082, "Reykjavík"),
    ("is-wx-keflavik-01", "1350", 63.9829, -22.6005, "Keflavíkurflugvöllur"),
    ("is-wx-akureyri-01", "3471", 65.6961, -18.1113, "Akureyri Krossanesbraut"),
    ("is-wx-egilsstadir-01", "4271", 65.2762, -14.4046, "Egilsstaðaflugvöllur"),
    ("is-wx-stykkisholmur-01", "2050", 65.0717, -22.7324, "Stykkishólmur"),
    ("is-wx-hofn-01", "5544", 64.2691, -15.2135, "Höfn í Hornafirði"),
    ("is-wx-vestmannaeyjar-01", "6015", 63.4359, -20.2758, "Vestmannaeyjabær"),
    ("is-wx-selfoss-01", "6300", 63.9355, -20.9707, "Selfoss"),
    ("is-wx-husavik-01", "3696", 66.0418, -17.3281, "Húsavík"),
    ("is-wx-vik-01", "6049", 63.4224, -19.0019, "Vík í Mýrdal"),
    ("is-wx-raufarhofn-01", "4828", 66.4560, -15.9527, "Raufarhöfn"),
    ("is-wx-isafjordur-01", "2644", 66.0600, -23.1847, "Ísafjörður Tungudalur"),
)

HK_AQHI_MESH: tuple[tuple[str, str, float, float], ...] = (
    ("hk-aqhi-centralwestern-01", "Central/Western", 22.2849, 114.1444),
    ("hk-aqhi-southern-01", "Southern", 22.2475, 114.1661),
    ("hk-aqhi-eastern-01", "Eastern", 22.2828, 114.2194),
    ("hk-aqhi-kwuntong-01", "Kwun Tong", 22.3119, 114.2311),
    ("hk-aqhi-shamshuipo-01", "Sham Shui Po", 22.3303, 114.1594),
    ("hk-aqhi-kwaichung-01", "Kwai Chung", 22.3572, 114.1297),
    ("hk-aqhi-tsuenwan-01", "Tsuen Wan", 22.3717, 114.1144),
    ("hk-aqhi-tseungkwano-01", "Tseung Kwan O", 22.3178, 114.2594),
    ("hk-aqhi-yuenlong-01", "Yuen Long", 22.4447, 114.0225),
    ("hk-aqhi-tuenmun-01", "Tuen Mun", 22.3911, 113.9769),
    ("hk-aqhi-tungchung-01", "Tung Chung", 22.2889, 113.9436),
    ("hk-aqhi-taipo-01", "Tai Po", 22.4511, 114.1644),
    ("hk-aqhi-shatin-01", "Sha Tin", 22.3764, 114.1847),
    ("hk-aqhi-north-01", "North", 22.4967, 114.1283),
    ("hk-aqhi-tapmun-01", "Tap Mun", 22.4714, 114.3608),
    ("hk-aqhi-causewaybay-01", "Causeway Bay", 22.2800, 114.1850),
    ("hk-aqhi-central-01", "Central", 22.2819, 114.1581),
    ("hk-aqhi-mongkok-01", "Mong Kok", 22.3225, 114.1683),
)

P11_OPEN_COUNT = (
    len(METEOSWISS_MESH)
    + len(BAFU_MESH)
    + len(ESTONIA_MESH)
    + len(ICELAND_MESH)
    + len(HK_AQHI_MESH)
)
P11_KEYED_COUNT = len(AEMET_MESH) + len(CWA_MESH) + len(METEOFRANCE_MESH)


def register_p11_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P11 relays and return the number added."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_METEOSWISS_ENABLED", "1"):
        for device_id, station_name, lat, lon in METEOSWISS_MESH:
            try:
                fleet.add(
                    MeteoSwissWeather(
                        device_id, clock, station_name=station_name,
                        latitude=lat, longitude=lon,
                        site=f"live-weather-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("MeteoSwiss %s skipped: %s", device_id, exc)

    if enabled("GAIA_BAFU_ENABLED", "1"):
        for device_id, station, lat, lon, _place in BAFU_MESH:
            try:
                fleet.add(
                    BafuRiver(
                        device_id, clock, station=station,
                        latitude=lat, longitude=lon,
                        site=f"live-river-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("BAFU %s skipped: %s", device_id, exc)

    if enabled("GAIA_ESTONIA_ENABLED", "1"):
        for device_id, station_name, lat, lon in ESTONIA_MESH:
            try:
                fleet.add(
                    EstoniaWeather(
                        device_id, clock, station_name=station_name,
                        latitude=lat, longitude=lon,
                        site=f"live-weather-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("Estonia %s skipped: %s", device_id, exc)

    if enabled("GAIA_ICELAND_ENABLED", "1"):
        for device_id, station, lat, lon, _place in ICELAND_MESH:
            try:
                fleet.add(
                    IcelandWeather(
                        device_id, clock, station=station,
                        latitude=lat, longitude=lon,
                        site=f"live-weather-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("Iceland %s skipped: %s", device_id, exc)

    if enabled("GAIA_HK_AQHI_ENABLED", "1"):
        for device_id, station_name, lat, lon in HK_AQHI_MESH:
            try:
                fleet.add(
                    HkAqhiAir(
                        device_id, clock, station_name=station_name,
                        latitude=lat, longitude=lon,
                        site=f"live-air-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("HK AQHI %s skipped: %s", device_id, exc)

    aemet_key = _env("GAIA_AEMET_API_KEY")
    if aemet_key and enabled("GAIA_AEMET_ENABLED", "1"):
        for device_id, station, lat, lon, _place in AEMET_MESH:
            try:
                fleet.add(
                    AemetWeather(
                        device_id, clock, station=station, api_key=aemet_key,
                        latitude=lat, longitude=lon,
                        site=f"live-weather-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("AEMET %s skipped: %s", device_id, exc)
    elif enabled("GAIA_AEMET_ENABLED", "1"):
        log.info("AEMET skipped (set GAIA_AEMET_API_KEY to enable)")

    cwa_key = _env("GAIA_CWA_API_KEY")
    if cwa_key and enabled("GAIA_CWA_ENABLED", "1"):
        for device_id, station, lat, lon, _place in CWA_MESH:
            try:
                fleet.add(
                    CwaWeather(
                        device_id, clock, station=station, api_key=cwa_key,
                        latitude=lat, longitude=lon,
                        site=f"live-weather-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("CWA %s skipped: %s", device_id, exc)
    elif enabled("GAIA_CWA_ENABLED", "1"):
        log.info("CWA skipped (set GAIA_CWA_API_KEY to enable)")

    mf_id = _env("GAIA_METEOFRANCE_APPLICATION_ID")
    if mf_id and enabled("GAIA_METEOFRANCE_ENABLED", "1"):
        for device_id, station, lat, lon, _place in METEOFRANCE_MESH:
            try:
                fleet.add(
                    MeteoFranceWeather(
                        device_id, clock, station=station, application_id=mf_id,
                        latitude=lat, longitude=lon,
                        site=f"live-weather-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("Météo-France %s skipped: %s", device_id, exc)
    elif enabled("GAIA_METEOFRANCE_ENABLED", "1"):
        log.info("Météo-France skipped (set GAIA_METEOFRANCE_APPLICATION_ID to enable)")

    return n


__all__ = [
    "AemetWeather",
    "MeteoSwissWeather",
    "BafuRiver",
    "CwaWeather",
    "MeteoFranceWeather",
    "EstoniaWeather",
    "IcelandWeather",
    "HkAqhiAir",
    "AEMET_MESH",
    "METEOSWISS_MESH",
    "BAFU_MESH",
    "CWA_MESH",
    "METEOFRANCE_MESH",
    "ESTONIA_MESH",
    "ICELAND_MESH",
    "HK_AQHI_MESH",
    "P11_OPEN_COUNT",
    "P11_KEYED_COUNT",
    "lv95_to_wgs84",
    "register_p11_relays",
]
