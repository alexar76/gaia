"""P12 CHMU Czech climate-now relays — CC BY 4.0, no API key.

HTTPS ``opendata.chmi.cz/meteorology/climate/now/data/`` 10-minute JSON file dumps.
WIGOS identifiers vary (``0-20000-0-{WMO}`` vs ``0-203-0-{WMO}``); catalog anchors from
``now/metadata/meta1-*.json`` (GEOGR1=lon, GEOGR2=lat). Observations only — not a forecast.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p12_cz")

CAPABILITY = "gaia.weather.read@v1"
HOSTS = frozenset({"opendata.chmi.cz"})
SOURCE_POLICY_ID = "chmu_cz"

_SAFE_WSI = re.compile(r"^0-\d+-0-[0-9]{4,8}$")
_MAX_AGE_S = 6 * 3600
_CACHE_TTL_S = 120.0

_CHMU_FILE = (
    "https://opendata.chmi.cz/meteorology/climate/now/data/"
    "10m-{wsi}-{day}.json"
)

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()


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


def _reject_stale(device_id: str, raw: Any, *, label: str) -> None:
    age = _iso_age_s(raw)
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


class ChmuWeather(LiveDevice):
    """CHMU Czech 10-min climate now dump — CC BY 4.0. Documented WMO / WSI mesh."""

    model = "GAIA-WEATHER (CHMU)"
    policy_id = SOURCE_POLICY_ID
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
        self, device_id: str, clock: SimClock, *, wsi: str,
        latitude: float, longitude: float, **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (wsi or "").strip()
        if not _SAFE_WSI.fullmatch(code):
            raise ValueError(f"invalid CHMU WSI: {wsi!r}")
        self.wsi = code
        self.station = code.rsplit("-", 1)[-1]
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.url = _CHMU_FILE.format(wsi=code, day=day)
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: CHMU empty")
        _reject_stale(self.device_id, payload.get("datumVytvoreni"), label="CHMU")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        values = inner.get("values") if isinstance(inner, dict) else None
        if not isinstance(values, list) or not values:
            raise DeviceOffline(f"{self.device_id}: CHMU no values for {self.wsi}")
        latest: dict[str, tuple[str, float]] = {}
        for row in values:
            if not isinstance(row, (list, tuple)) or len(row) < 4:
                continue
            station, element, when, raw = str(row[0]), str(row[1]), str(row[2]), row[3]
            if station != self.wsi:
                continue
            value = _num(raw)
            if value is None:
                continue
            prev = latest.get(element)
            if prev is None or when >= prev[0]:
                latest[element] = (when, value)
        if not latest:
            raise DeviceOffline(f"{self.device_id}: CHMU no rows for {self.wsi}")
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
            raise DeviceOffline(f"{self.device_id}: CHMU no T/wind for {self.wsi}")
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        payload = _cached(self.url, lambda: self._fetch(self.url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


# device_id, WSI, lat, lon, place — WGS84 from CHMI meta1 (GEOGR2=lat, GEOGR1=lon).
CHMU_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("cz-wx-praha-01", "0-20000-0-11518", 50.100278, 14.255556, "Praha-Ruzyně"),
    ("cz-wx-brno-01", "0-20000-0-11723", 49.153056, 16.688889, "Brno-Tuřany"),
    ("cz-wx-ostrava-01", "0-20000-0-11782", 49.691944, 18.112778, "Ostrava-Mošnov"),
    ("cz-wx-plzen-01", "0-20000-0-11450", 49.764722, 13.378889, "Plzeň-Mikulka"),
    ("cz-wx-liberec-01", "0-20000-0-11603", 50.76972, 15.02389, "Liberec"),
    ("cz-wx-olomouc-01", "0-203-0-11742", 49.5757644, 17.2839678, "Olomouc-Holice"),
    ("cz-wx-budejovice-01", "0-20000-0-11546", 48.951944, 14.469722, "České Budějovice"),
    ("cz-wx-hradec-01", "0-203-0-11649", 50.177649, 15.838452, "Hradec Králové"),
)

MESH = CHMU_MESH
P12_CZ_PIN_COUNT = len(CHMU_MESH)


def atlas_rows() -> list[tuple[str, float, float, str, str, str, str]]:
    """ATLAS catalog tuples: device_id, lat, lon, place, layer, capability_id, note."""
    rows: list[tuple[str, float, float, str, str, str, str]] = []
    for device_id, wsi, lat, lon, place in CHMU_MESH:
        rows.append((
            device_id, lat, lon, place, "weather", CAPABILITY,
            f"CHMI climate now · CC BY 4.0 ({wsi})",
        ))
    return rows


def register_p12_cz(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register CHMU climate-now relays; returns pins added (no API key required)."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    if not enabled("GAIA_CHMU_ENABLED", "1"):
        return 0

    n = 0
    for device_id, wsi, lat, lon, _place in CHMU_MESH:
        try:
            fleet.add(
                ChmuWeather(
                    device_id, clock, wsi=wsi,
                    latitude=lat, longitude=lon,
                    site=f"live-weather-{device_id}", key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("CHMU %s skipped: %s", device_id, exc)
    return n


__all__ = [
    "CAPABILITY",
    "HOSTS",
    "SOURCE_POLICY_ID",
    "ChmuWeather",
    "CHMU_MESH",
    "MESH",
    "P12_CZ_PIN_COUNT",
    "atlas_rows",
    "register_p12_cz",
]
