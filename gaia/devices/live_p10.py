"""P10 commercially-clear LIVE relays — SE Asia weather/air + Belgium AQ.

* Singapore NEA via data.gov.sg — Open Data Licence (commercial OK + attribution).
* Hong Kong Observatory rhrread — DATA.GOV.HK terms (commercial free + attribution).
* IRCELINE Belgium PM2.5 — CC BY 4.0.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p10")

_SAFE_SG = re.compile(r"^S[0-9]{1,4}$", re.I)
_SAFE_SG_REGION = re.compile(r"^(central|north|south|east|west)$", re.I)
_SAFE_IRCELINE = re.compile(r"^[0-9]{4,8}$")
_MAX_AGE_S = 6 * 3600
_CACHE_TTL_S = 120.0
_KNOT_TO_MPS = 0.514444

# These endpoints return the whole station/region collection.  One shared read
# per TTL is enough for every pin and avoids dozens of identical upstream calls.
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_KEY_LOCKS: dict[str, threading.Lock] = {}


def _cached(key: str, loader: Any) -> Any:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
        key_lock = _CACHE_KEY_LOCKS.setdefault(key, threading.Lock())
    # Coalesce a cold-cache burst. Holding only this endpoint's lock means a slow
    # Singapore feed does not block HKO/IRCELINE, while twenty station pins still
    # produce one upstream request rather than twenty concurrent requests.
    with key_lock:
        now = time.time()
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and now - hit[0] < _CACHE_TTL_S:
                return hit[1]
        payload = loader()
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), payload)
        return payload


def _iso_age_s(raw: Any) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when.astimezone(timezone.utc)).total_seconds()


def _reject_stale(device_id: str, raw: Any, *, label: str) -> None:
    age = _iso_age_s(raw)
    if age is None:
        raise DeviceOffline(f"{device_id}: {label} observation timestamp missing or malformed")
    if age > _MAX_AGE_S or age < -3600:
        raise DeviceOffline(f"{device_id}: {label} observation stale")


def _first_item(payload: Any) -> dict[str, Any]:
    items = payload.get("items") if isinstance(payload, dict) else None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def _reading_for_station(payload: Any, station_id: str) -> float | None:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return None
    readings = items[0].get("readings") if isinstance(items[0], dict) else None
    if not isinstance(readings, list):
        return None
    want = station_id.upper()
    for row in readings:
        if not isinstance(row, dict):
            continue
        if str(row.get("station_id") or "").upper() != want:
            continue
        return _num(row.get("value"))
    return None


class SgNeaWeather(LiveDevice):
    """Singapore NEA weather pin — wind (+ temp/RH/rain when published)."""

    model = "GAIA-WEATHER (Singapore NEA)"
    policy_id = "singapore_nea"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "wind_mps": "m/s",
        "precipitation_mm": "mm",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Singapore NEA via data.gov.sg · Singapore Open Data Licence — "
        "attribution required. In-situ station readings, not a forecast."
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
        code = (station or "").strip().upper()
        if not _SAFE_SG.fullmatch(code):
            raise ValueError(f"invalid Singapore NEA station: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self._urls = {
            "wind_mps": "https://api.data.gov.sg/v1/environment/wind-speed",
            "temperature_c": "https://api.data.gov.sg/v1/environment/air-temperature",
            "humidity_pct": "https://api.data.gov.sg/v1/environment/relative-humidity",
            "precipitation_mm": "https://api.data.gov.sg/v1/environment/rainfall",
        }
        for url in self._urls.values():
            policy.require_endpoint(url)

    def map(self, payload: Any) -> dict[str, float | None]:
        feeds = payload if isinstance(payload, dict) else {}
        out: dict[str, float | None] = {
            "temperature_c": None,
            "humidity_pct": None,
            "wind_mps": None,
            "precipitation_mm": None,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
        for field, blob in feeds.items():
            if field in out:
                item = _first_item(blob)
                _reject_stale(
                    self.device_id,
                    item.get("timestamp") or item.get("update_timestamp"),
                    label=f"Singapore NEA {field}",
                )
                value = _reading_for_station(blob, self.station)
                if field == "wind_mps" and value is not None:
                    metadata = blob.get("metadata") if isinstance(blob, dict) else {}
                    unit = str((metadata or {}).get("reading_unit") or "").strip().lower()
                    if unit in ("knots", "knot", "kn"):
                        value *= _KNOT_TO_MPS
                    elif unit not in ("m/s", "mps", "metres per second", "meters per second"):
                        raise DeviceOffline(
                            f"{self.device_id}: Singapore NEA wind unit missing or unsupported"
                        )
                out[field] = value
        return out

    def sample(self) -> dict[str, float]:
        feeds = {
            field: _cached(url, lambda u=url: self._fetch(u))
            for field, url in self._urls.items()
        }
        mapped = self.map(feeds)
        if mapped.get("wind_mps") is None and mapped.get("temperature_c") is None:
            raise DeviceOffline(f"{self.device_id}: Singapore NEA empty for {self.station}")
        return {k: v for k, v in mapped.items() if v is not None}


class SgNeaPsiAir(LiveDevice):
    """Singapore NEA regional PSI / PM2.5 — Open Data Licence."""

    model = "GAIA-AIR (Singapore PSI)"
    policy_id = "singapore_nea"
    fields = {
        "pm2_5_ugm3": "ug/m3",
        "air_quality_index": "PSI",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Singapore NEA PSI via data.gov.sg · Singapore Open Data Licence — "
        "attribution required. Regional 24h index, not a reference-grade monitor."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        region: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (region or "").strip().lower()
        if not _SAFE_SG_REGION.fullmatch(code):
            raise ValueError(f"invalid Singapore PSI region: {region!r}")
        self.region = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self.url = "https://api.data.gov.sg/v1/environment/psi"
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise DeviceOffline(f"{self.device_id}: Singapore PSI empty")
        _reject_stale(
            self.device_id,
            items[0].get("timestamp") or items[0].get("update_timestamp"),
            label="Singapore PSI",
        )
        readings = items[0].get("readings") or {}
        if not isinstance(readings, dict):
            raise DeviceOffline(f"{self.device_id}: Singapore PSI malformed")
        pm = readings.get("pm25_twenty_four_hourly") or {}
        psi = readings.get("psi_twenty_four_hourly") or {}
        pm_v = _num(pm.get(self.region)) if isinstance(pm, dict) else None
        psi_v = _num(psi.get(self.region)) if isinstance(psi, dict) else None
        if pm_v is None and psi_v is None:
            raise DeviceOffline(f"{self.device_id}: no PSI for region {self.region}")
        return {
            "pm2_5_ugm3": pm_v,
            "air_quality_index": psi_v,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class HkoWeather(LiveDevice):
    """Hong Kong Observatory current weather place — DATA.GOV.HK terms."""

    model = "GAIA-WEATHER (HKO)"
    policy_id = "hongkong_hko"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Hong Kong Observatory rhrread via DATA.GOV.HK — commercial reuse free "
        "with attribution to HKSAR Government / HKO. In-situ place temperature."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        place: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        name = (place or "").strip()
        if not name or len(name) > 80 or any(c in name for c in "\n\r\t"):
            raise ValueError(f"invalid HKO place: {place!r}")
        self.place = name
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self.url = (
            "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
            "?dataType=rhrread&lang=en"
        )
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: HKO empty")
        _reject_stale(
            self.device_id,
            payload.get("updateTime") or payload.get("update_time"),
            label="HKO",
        )
        temp_block = payload.get("temperature") or {}
        rows = temp_block.get("data") if isinstance(temp_block, dict) else None
        temperature = None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("place") or "") != self.place:
                    continue
                temperature = _num(row.get("value"))
                break
        humidity = None
        # Humidity is published only for Hong Kong Observatory in rhrread.
        if self.place == "Hong Kong Observatory":
            hum_block = payload.get("humidity") or {}
            hum_rows = hum_block.get("data") if isinstance(hum_block, dict) else None
            if isinstance(hum_rows, list) and hum_rows and isinstance(hum_rows[0], dict):
                humidity = _num(hum_rows[0].get("value"))
        if temperature is None:
            raise DeviceOffline(f"{self.device_id}: HKO no temperature for {self.place!r}")
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class IrcelineAir(LiveDevice):
    """IRCELINE Belgium in-situ PM2.5 — CC BY 4.0."""

    model = "GAIA-AIR (IRCELINE)"
    policy_id = "irceline_be"
    fields = {
        "pm2_5_ugm3": "ug/m3",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "IRCELINE / Belgian Interregional Environment Agency · CC BY 4.0 — "
        "cite IRCELINE. Belgium in-situ PM2.5 only."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        timeseries_id: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (timeseries_id or "").strip()
        if not _SAFE_IRCELINE.fullmatch(code):
            raise ValueError(f"invalid IRCELINE timeseries id: {timeseries_id!r}")
        self.timeseries_id = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self.url = (
            "https://geo.irceline.be/sos/api/v1/timeseries/"
            f"{quote(code)}?expanded=true"
        )
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: IRCELINE empty")
        last = payload.get("lastValue") or {}
        if not isinstance(last, dict):
            raise DeviceOffline(f"{self.device_id}: IRCELINE no lastValue")
        value = _num(last.get("value"))
        ts_ms = _num(last.get("timestamp"))
        if value is None:
            raise DeviceOffline(f"{self.device_id}: IRCELINE null PM2.5")
        if ts_ms is None:
            raise DeviceOffline(f"{self.device_id}: IRCELINE timestamp missing")
        age = time.time() - (float(ts_ms) / 1000.0)
        if age > _MAX_AGE_S or age < -3600:
            raise DeviceOffline(f"{self.device_id}: IRCELINE observation stale")
        lat = lon = None
        station = payload.get("station") or {}
        geom = station.get("geometry") if isinstance(station, dict) else None
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        if isinstance(coords, list) and len(coords) >= 2:
            lon = _num(coords[0])
            lat = _num(coords[1])
        return {
            "pm2_5_ugm3": value,
            "latitude": lat if lat is not None else self.latitude,
            "longitude": lon if lon is not None else self.longitude,
        }


# ── Meshes ────────────────────────────────────────────────────────────────────

SG_WEATHER_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("sg-wx-eastcoast-01", "S107", 1.3133, 103.9620, "East Coast Parkway"),
    ("sg-wx-marina-01", "S108", 1.2799, 103.8703, "Marina Gardens Drive"),
    ("sg-wx-nanyang-01", "S44", 1.3458, 103.6817, "Nanyang Avenue"),
    ("sg-wx-choachukang-01", "S121", 1.3738, 103.7217, "Old Choa Chu Kang Road"),
    ("sg-wx-payalebar-01", "S06", 1.3570, 103.9040, "Paya Lebar Airport"),
    ("sg-wx-ubin-01", "S106", 1.4168, 103.9673, "Pulau Ubin"),
    ("sg-wx-scotts-01", "S111", 1.3106, 103.8365, "Scotts Road"),
    ("sg-wx-sentosa-01", "S60", 1.2504, 103.8275, "Sentosa"),
    ("sg-wx-tuas-01", "S115", 1.2938, 103.6184, "Tuas South Avenue 3"),
    ("sg-wx-westcoast-01", "S116", 1.2824, 103.7545, "West Coast Highway"),
    ("sg-wx-semakau-01", "S102", 1.1902, 103.7657, "Semakau Island"),
    ("sg-wx-s23-01", "S23", 1.3858, 103.7119, "NW Singapore S23"),
)

SG_PSI_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("sg-psi-central-01", "central", 1.35735, 103.8200, "Singapore Central"),
    ("sg-psi-north-01", "north", 1.41803, 103.8200, "Singapore North"),
    ("sg-psi-south-01", "south", 1.29587, 103.8200, "Singapore South"),
    ("sg-psi-east-01", "east", 1.35735, 103.9400, "Singapore East"),
    ("sg-psi-west-01", "west", 1.35735, 103.7000, "Singapore West"),
)

# Approximate official place coordinates for HKO rhrread temperature mesh.
HKO_WEATHER_MESH: tuple[tuple[str, str, float, float], ...] = (
    ("hko-wx-observatory-01", "Hong Kong Observatory", 22.3022, 114.1743),
    ("hko-wx-kingspark-01", "King's Park", 22.3119, 114.1728),
    ("hko-wx-wongchukhang-01", "Wong Chuk Hang", 22.2478, 114.1736),
    ("hko-wx-takwuling-01", "Ta Kwu Ling", 22.5286, 114.1567),
    ("hko-wx-laufaushan-01", "Lau Fau Shan", 22.4683, 113.9836),
    ("hko-wx-taipo-01", "Tai Po", 22.4500, 114.1689),
    ("hko-wx-shatin-01", "Sha Tin", 22.3833, 114.1889),
    ("hko-wx-tuenmun-01", "Tuen Mun", 22.3911, 113.9772),
    ("hko-wx-tseungkwano-01", "Tseung Kwan O", 22.3075, 114.2522),
    ("hko-wx-saikung-01", "Sai Kung", 22.3833, 114.2700),
    ("hko-wx-cheungchau-01", "Cheung Chau", 22.2081, 114.0286),
    ("hko-wx-cheklapkok-01", "Chek Lap Kok", 22.3089, 113.9147),
    ("hko-wx-tsingyi-01", "Tsing Yi", 22.3456, 114.1069),
    ("hko-wx-happyvalley-01", "Happy Valley", 22.2700, 114.1836),
    ("hko-wx-stanley-01", "Stanley", 22.2186, 114.2136),
)

IRCELINE_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("be-aq-brussels-01", "100034", 50.8436, 4.3670, "Brussels Régent"),
    ("be-aq-mechelen-01", "100022", 51.0204, 4.4833, "Mechelen"),
    ("be-aq-deurne-01", "100040", 51.2264, 4.4487, "Antwerp Deurne"),
    ("be-aq-liege-01", "100030", 50.6380, 5.5718, "Liège"),
    ("be-aq-roeselare-01", "100005", 50.9532, 3.1212, "Roeselare"),
    ("be-aq-zelzate-01", "100012", 51.1961, 3.8229, "Zelzate"),
    ("be-aq-aarschot-01", "100014", 50.9775, 4.8376, "Aarschot"),
    ("be-aq-boom-01", "100011", 51.0920, 4.3801, "Boom"),
    ("be-aq-charleroi-01", "100044", 50.4516, 4.4255, "Charleroi Airport"),
    ("be-aq-linkeroever-01", "100008", 51.2362, 4.3852, "Antwerp Linkeroever"),
    ("be-aq-dessel-01", "100018", 51.2337, 5.1640, "Dessel"),
    ("be-aq-vilvoorde-01", "100016", 50.9414, 4.4367, "Vilvoorde"),
)


def register_p10_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P10 relays and return the number added."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_SG_NEA_ENABLED", "1"):
        for device_id, station, lat, lon, _place in SG_WEATHER_MESH:
            try:
                fleet.add(
                    SgNeaWeather(
                        device_id, clock, station=station,
                        latitude=lat, longitude=lon,
                        site=f"live-weather-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("Singapore weather %s skipped: %s", device_id, exc)
        for device_id, region, lat, lon, _place in SG_PSI_MESH:
            try:
                fleet.add(
                    SgNeaPsiAir(
                        device_id, clock, region=region,
                        latitude=lat, longitude=lon,
                        site=f"live-air-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("Singapore PSI %s skipped: %s", device_id, exc)

    if enabled("GAIA_HKO_ENABLED", "1"):
        for device_id, place, lat, lon in HKO_WEATHER_MESH:
            try:
                fleet.add(
                    HkoWeather(
                        device_id, clock, place=place,
                        latitude=lat, longitude=lon,
                        site=f"live-weather-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("HKO %s skipped: %s", device_id, exc)

    if enabled("GAIA_IRCELINE_ENABLED", "1"):
        for device_id, ts_id, lat, lon, _place in IRCELINE_MESH:
            try:
                fleet.add(
                    IrcelineAir(
                        device_id, clock, timeseries_id=ts_id,
                        latitude=lat, longitude=lon,
                        site=f"live-air-{device_id}", key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("IRCELINE %s skipped: %s", device_id, exc)
    return n


__all__ = [
    "SgNeaWeather",
    "SgNeaPsiAir",
    "HkoWeather",
    "IrcelineAir",
    "SG_WEATHER_MESH",
    "SG_PSI_MESH",
    "HKO_WEATHER_MESH",
    "IRCELINE_MESH",
    "register_p10_relays",
]
