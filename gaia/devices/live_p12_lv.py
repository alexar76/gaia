"""P12 Latvia LVGMC LIVE relays — data.gov.lv HVD operative CSV (CC0-1.0, no key).

Dataset ``40d80be5-0c09-47c4-80f3-fad4bec19f33``: meteo/hidro operative CSV +
``meteo_stacijas.csv`` / ``hidro_stacijas.csv`` for WGS84 (GEOGR2/GEOGR1). Fail closed
if DATETIME is older than 6 h (naive timestamps interpreted as Europe/Riga local).

Self-contained so sibling P12 agents can merge without editing this file.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p12_lv")

_SAFE_LV = re.compile(r"^[A-Za-z0-9]{4,16}$")
_MAX_AGE_S = 6 * 3600
_CACHE_TTL_S = 120.0
_RIGA_TZ = timezone(timedelta(hours=3))

_DATASET = "40d80be5-0c09-47c4-80f3-fad4bec19f33"
_BASE = f"https://data.gov.lv/dati/dataset/{_DATASET}/resource"

_LV_WX_CSV = f"{_BASE}/17460efb-ae99-4d1d-8144-1068f184b05f/download/meteo_operativie_dati.csv"
_LV_HYDRO_CSV = f"{_BASE}/de5f06e9-6f44-497d-8ec2-72a2483608e8/download/hidro_operativie_dati.csv"
_LV_WX_STACIJAS = f"{_BASE}/c32c7afd-0d05-44fd-8b24-1de85b4bf11d/download/meteo_stacijas.csv"
_LV_HYDRO_STACIJAS = f"{_BASE}/93fd5e2c-20c4-496e-a920-ff29bda20383/download/hidro_stacijas.csv"

HTTPS_ALLOWLIST: tuple[str, ...] = ("data.gov.lv",)

SOURCE_POLICY_IDS: dict[str, str] = {"lvgmc_lv": "lvgmc_lv"}

_CAP_WEATHER = "gaia.weather.read@v1"
_CAP_RIVER = "gaia.river.read@v1"

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


def _csv_rows(text: str) -> list[dict[str, str]]:
    sample = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(sample))
    rows: list[dict[str, str]] = []
    for row in reader:
        if isinstance(row, dict):
            rows.append({str(k).strip(): ("" if v is None else str(v).strip()) for k, v in row.items()})
    return rows


def _stacija_coords(rows: list[dict[str, str]], station_id: str) -> tuple[float, float, str]:
    """Join STATION_ID → WGS84 via GEOGR2 (lat) and GEOGR1 (lon)."""
    want = (station_id or "").strip()
    for row in rows:
        if (row.get("STATION_ID") or "").strip() != want:
            continue
        lat = _num(row.get("GEOGR2"))
        lon = _num(row.get("GEOGR1"))
        name = (row.get("NAME") or want).strip()
        if lat is None or lon is None:
            break
        return lat, lon, name
    raise ValueError(f"LVGMC stacija missing coords for {station_id!r}")


def _mesh_from_stacijas(
    pins: tuple[tuple[str, str, str], ...],
    stacija_rows: list[dict[str, str]],
) -> tuple[tuple[str, str, float, float, str], ...]:
    out: list[tuple[str, str, float, float, str]] = []
    for device_id, station, place in pins:
        lat, lon, name = _stacija_coords(stacija_rows, station)
        out.append((device_id, station, lat, lon, place or name))
    return tuple(out)


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
        f"Latvia LVGMC operative meteorology via data.gov.lv dataset {_DATASET} · CC0-1.0. "
        "Latvia in-situ only."
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


_WX_PINS: tuple[tuple[str, str, str], ...] = (
    ("lv-wx-riga-01", "RIGASLU", "Rīga Universitāte"),
    ("lv-wx-daugavpils-01", "RIDM99MS", "Daugavpils"),
    ("lv-wx-liepaja-01", "RILP99PA", "Liepāja"),
    ("lv-wx-ventspils-01", "RIVE99PA", "Ventspils"),
    ("lv-wx-jelgava-01", "RIJE99PA", "Jelgava"),
    ("lv-wx-rezekne-01", "RIREZEKN", "Rēzekne"),
    ("lv-wx-aluksne-01", "RIAL99MS", "Alūksne"),
    ("lv-wx-saldus-01", "RISA99PA", "Saldus"),
)

_HYDRO_PINS: tuple[tuple[str, str, str], ...] = (
    ("lv-hydro-riga-01", "HD073810", "Daugava at Rīga"),
    ("lv-hydro-daugavpils-01", "HD073141", "Daugava at Daugavpils"),
    ("lv-hydro-jekabpils-01", "HD073151", "Daugava at Jēkabpils"),
    ("lv-hydro-ogre-01", "HD073401", "Daugava at Ogre"),
    ("lv-hydro-plavinas-01", "HD073904", "Daugava at Pļaviņas"),
    ("lv-hydro-jelgava-01", "HD073801", "Lielupe at Jelgava"),
)

LVGMC_WX_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("lv-wx-riga-01", "RIGASLU", 56.954797, 24.104686, "Rīga Universitāte"),
    ("lv-wx-daugavpils-01", "RIDM99MS", 55.8700, 26.6175, "Daugavpils"),
    ("lv-wx-liepaja-01", "RILP99PA", 56.475447, 21.020641, "Liepāja"),
    ("lv-wx-ventspils-01", "RIVE99PA", 57.3956, 21.5372, "Ventspils"),
    ("lv-wx-jelgava-01", "RIJE99PA", 56.556944, 23.964167, "Jelgava"),
    ("lv-wx-rezekne-01", "RIREZEKN", 56.4800, 27.357222, "Rēzekne"),
    ("lv-wx-aluksne-01", "RIAL99MS", 57.439578, 27.035378, "Alūksne"),
    ("lv-wx-saldus-01", "RISA99PA", 56.710278, 22.426667, "Saldus"),
)

LVGMC_HYDRO_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("lv-hydro-riga-01", "HD073810", 57.0325, 24.133889, "Daugava at Rīga"),
    ("lv-hydro-daugavpils-01", "HD073141", 55.8614, 26.5211, "Daugava at Daugavpils"),
    ("lv-hydro-jekabpils-01", "HD073151", 56.4978, 25.8914, "Daugava at Jēkabpils"),
    ("lv-hydro-ogre-01", "HD073401", 56.8128, 24.6417, "Daugava at Ogre"),
    ("lv-hydro-plavinas-01", "HD073904", 56.6164, 25.7297, "Daugava at Pļaviņas"),
    ("lv-hydro-jelgava-01", "HD073801", 56.6550, 23.7350, "Lielupe at Jelgava"),
)

P12_LV_COUNT = len(LVGMC_WX_MESH) + len(LVGMC_HYDRO_MESH)


def atlas_rows() -> list[tuple[str, float, float, str, str, str, str]]:
    """ATLAS catalog tuples: device_id, lat, lon, place, layer, capability_id, note."""
    rows: list[tuple[str, float, float, str, str, str, str]] = []
    for device_id, _st, lat, lon, place in LVGMC_WX_MESH:
        rows.append((
            device_id, lat, lon, place, "weather", _CAP_WEATHER,
            "LVGMC data.gov.lv · CC0-1.0",
        ))
    for device_id, _st, lat, lon, place in LVGMC_HYDRO_MESH:
        rows.append((
            device_id, lat, lon, place, "river", _CAP_RIVER,
            "LVGMC hydro · CC0-1.0",
        ))
    return rows


def register_p12_lv(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register Latvia LVGMC P12 relays and return the number added."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0

    def _add(device: LiveDevice) -> None:
        nonlocal n
        fleet.add(device)
        n += 1

    if not enabled("GAIA_LVGMC_ENABLED", "1"):
        return 0

    for device_id, station, lat, lon, _place in LVGMC_WX_MESH:
        try:
            _add(LvgmcWeather(
                device_id, clock, station=station, latitude=lat, longitude=lon,
                site=f"live-weather-{device_id}", key_dir=key_dir,
            ))
        except ValueError as exc:
            log.warning("LVGMC weather %s skipped: %s", device_id, exc)
    for device_id, station, lat, lon, _place in LVGMC_HYDRO_MESH:
        try:
            _add(LvgmcRiver(
                device_id, clock, station=station, latitude=lat, longitude=lon,
                site=f"live-river-{device_id}", key_dir=key_dir,
            ))
        except ValueError as exc:
            log.warning("LVGMC hydro %s skipped: %s", device_id, exc)

    return n


__all__ = [
    "LvgmcWeather",
    "LvgmcRiver",
    "LVGMC_WX_MESH",
    "LVGMC_HYDRO_MESH",
    "P12_LV_COUNT",
    "HTTPS_ALLOWLIST",
    "SOURCE_POLICY_IDS",
    "_WX_PINS",
    "_HYDRO_PINS",
    "_LV_WX_STACIJAS",
    "_LV_HYDRO_STACIJAS",
    "_mesh_from_stacijas",
    "_stacija_coords",
    "_csv_rows",
    "atlas_rows",
    "register_p12_lv",
]
