"""P12 JMA LIVE relays — AMeDAS weather mesh, official quake list, NW-Pacific typhoon.

Public Data License 1.0 (compatible with CC BY 4.0). HTTPS ``www.jma.go.jp`` only.
Website JSON may move. Observations / official events only — not a JP forecast licence,
not p2pquake, not NHC, not JTWC. Empty typhoon season → offline.

This module is self-contained so sibling P12 agents can merge HOSTS / SOURCE_POLICIES
/ ATLAS_ROWS without editing this file.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.devices.live_p0 import signed_cluster_read

log = logging.getLogger("gaia.devices.live_p12_jma")

HOSTS: frozenset[str] = frozenset({"www.jma.go.jp"})

_SAFE_AMEDAS = re.compile(r"^[0-9]{5}$")
_SAFE_TC = re.compile(r"^TC[0-9]{4}$")
_JMA_COD = re.compile(
    r"(?P<lat>[+-]\d+(?:\.\d+)?)(?P<lon>[+-]\d+(?:\.\d+)?)(?P<dep>[+-]\d+)?"
)
_MAX_AGE_S = 6 * 3600
_CACHE_TTL_S = 120.0

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()

_JMA_LATEST = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
_JMA_MAP = "https://www.jma.go.jp/bosai/amedas/data/map/{ts}.json"
_JMA_QUAKE = "https://www.jma.go.jp/bosai/quake/data/list.json"
_JMA_TC = "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json"
_JMA_TC_SPEC = "https://www.jma.go.jp/bosai/typhoon/data/{tc}/specifications.json"

_PDL = (
    "Commercial reuse is permitted under the Japan Public Data License 1.0 "
    "(compatible with CC BY 4.0)."
)

SOURCE_POLICIES: dict[str, dict[str, Any]] = {
    "jma_amedas": {
        "source_id": "jma_amedas",
        "name": "JMA AMeDAS",
        "licence": "Public Data License 1.0",
        "commercial_basis": f"{_PDL} Japan in-situ observations only — not a forecast licence.",
        "attribution": "JMA AMeDAS · Public Data License 1.0 (cite JMA)",
        "hosts": ("www.jma.go.jp",),
        "licence_url": "https://www.jma.go.jp/jma/info/license.html",
    },
    "jma_quake": {
        "source_id": "jma_quake",
        "name": "JMA earthquake list",
        "licence": "Public Data License 1.0",
        "commercial_basis": f"{_PDL} Official www.jma.go.jp only — not p2pquake.",
        "attribution": "JMA quake · Public Data License 1.0 (cite JMA)",
        "hosts": ("www.jma.go.jp",),
        "licence_url": "https://www.jma.go.jp/jma/info/license.html",
    },
    "jma_typhoon": {
        "source_id": "jma_typhoon",
        "name": "JMA typhoon",
        "licence": "Public Data License 1.0",
        "commercial_basis": f"{_PDL} NW Pacific only — not NHC, not JTWC.",
        "attribution": "JMA typhoon · Public Data License 1.0 (cite JMA)",
        "hosts": ("www.jma.go.jp",),
        "licence_url": "https://www.jma.go.jp/jma/info/license.html",
    },
}


def _require_jma_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in HOSTS:
        raise ValueError(f"endpoint is not approved for JMA: {url}")


def _iso_age_s(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
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


def _pair(row: dict[str, Any], key: str) -> float | None:
    node = row.get(key)
    if isinstance(node, list) and node:
        return _num(node[0])
    return _num(node)


class JmaAmedasWeather(LiveDevice):
    """JMA AMeDAS website JSON — Public Data License 1.0 (≡ CC BY 4.0). Observations only."""

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
        if not _SAFE_AMEDAS.fullmatch(code):
            raise ValueError(f"invalid AMeDAS id: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        _require_jma_url(_JMA_LATEST)
        _require_jma_url(_JMA_MAP.format(ts="20200101000000"))

    def map(self, payload: Any) -> dict[str, float | None]:
        table = payload if isinstance(payload, dict) else None
        if not isinstance(table, dict):
            raise DeviceOffline(f"{self.device_id}: JMA AMeDAS empty")
        row = table.get(self.station)
        if not isinstance(row, dict):
            raise DeviceOffline(f"{self.device_id}: JMA AMeDAS no station {self.station}")
        temperature = _pair(row, "temp")
        humidity = _pair(row, "humidity")
        pressure = _pair(row, "pressure") or _pair(row, "normalPressure")
        wind = _pair(row, "wind")
        if temperature is None and wind is None:
            raise DeviceOffline(f"{self.device_id}: JMA AMeDAS no T/wind for {self.station}")
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        stamp = _cached(_JMA_LATEST, lambda: self._fetch_text(_JMA_LATEST, max_chars=128)).strip()
        _reject_stale(self.device_id, stamp, label="JMA AMeDAS")
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        ts = when.strftime("%Y%m%d%H%M%S")
        if not re.fullmatch(r"[0-9]{14}", ts):
            raise DeviceOffline(f"{self.device_id}: JMA AMeDAS timestamp refused")
        url = _JMA_MAP.format(ts=ts)
        _require_jma_url(url)
        payload = _cached(url, lambda: self._fetch(url))
        return {k: v for k, v in self.map(payload).items() if v is not None}


class JmaQuake(LiveDevice):
    """JMA official quake list.json. Official host only — not p2pquake."""

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
        _require_jma_url(self.url)

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
            scored.append((
                float(mag),
                {
                    "magnitude": float(mag),
                    "depth_km": float(depth_km),
                    "latitude": float(lat),
                    "longitude": float(lon),
                    "region": str(row.get("en_anm") or row.get("anm") or "")[:160],
                },
            ))
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
    """JMA NW-Pacific typhoon. Empty season = offline. Not NHC / JTWC."""

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
        _require_jma_url(self.url)
        _require_jma_url(_JMA_TC_SPEC.format(tc="TC0000"))

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
        analysis = self._analysis(first.get("specifications"))
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
        if not _SAFE_TC.fullmatch(tc):
            raise DeviceOffline(f"{self.device_id}: JMA typhoon id refused")
        spec_url = _JMA_TC_SPEC.format(tc=tc)
        _require_jma_url(spec_url)
        spec = _cached(spec_url, lambda: self._fetch(spec_url))
        return {k: v for k, v in self.map([{**storms[0], "specifications": spec}]).items() if v is not None}


# Official AMeDAS codes + WGS84 from amedastable.json (deg + minutes / 60).
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

ATLAS_ROWS: tuple[dict[str, Any], ...] = tuple(
    {
        "device_id": device_id,
        "lat": lat,
        "lon": lon,
        "place": f"{place} (JMA AMeDAS · Public Data License 1.0; cite JMA)",
        "layer": "weather",
        "capability_id": "gaia.weather.read@v1",
        "kind": "point",
        "label": station,
    }
    for device_id, station, lat, lon, place in JMA_AMEDAS_MESH
) + (
    {
        "device_id": "jma-quake-01",
        "lat": 36.0,
        "lon": 138.0,
        "place": "JMA official list.json (Public Data License; cite JMA; not p2pquake)",
        "layer": "quake",
        "capability_id": "gaia.quake.read@v1",
        "kind": "event",
        "label": "JMA Earthquakes",
    },
    {
        "device_id": "jma-typhoon-01",
        "lat": 20.0,
        "lon": 140.0,
        "place": "JMA NW-Pacific typhoon (Public Data License; cite JMA; not NHC/JTWC)",
        "layer": "cyclone",
        "capability_id": "gaia.cyclone.read@v1",
        "kind": "event",
        "label": "JMA Typhoon",
    },
)

P12_JMA_OPEN_COUNT = len(JMA_AMEDAS_MESH) + 2
P12_JMA_KEYED_COUNT = 0


def register_p12_jma(fleet: Any, clock: SimClock, key_dir: str) -> int:
    """Register JMA P12 relays and return the number added."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_JMA_AMEDAS_ENABLED", "1"):
        for device_id, station, lat, lon, _place in JMA_AMEDAS_MESH:
            try:
                fleet.add(JmaAmedasWeather(
                    device_id, clock, station=station, latitude=lat, longitude=lon,
                    site=f"live-weather-{device_id}", key_dir=key_dir,
                ))
                n += 1
            except ValueError as exc:
                log.warning("JMA AMeDAS %s skipped: %s", device_id, exc)
    if enabled("GAIA_JMA_QUAKE_ENABLED", "1"):
        try:
            fleet.add(JmaQuake("jma-quake-01", clock, site="live-quake-jma", key_dir=key_dir))
            n += 1
        except ValueError as exc:
            log.warning("JMA quake skipped: %s", exc)
    if enabled("GAIA_JMA_TYPHOON_ENABLED", "1"):
        try:
            fleet.add(JmaTyphoon("jma-typhoon-01", clock, site="live-cyclone-jma", key_dir=key_dir))
            n += 1
        except ValueError as exc:
            log.warning("JMA typhoon skipped: %s", exc)
    return n


__all__ = [
    "HOSTS",
    "SOURCE_POLICIES",
    "JMA_AMEDAS_MESH",
    "ATLAS_ROWS",
    "P12_JMA_OPEN_COUNT",
    "P12_JMA_KEYED_COUNT",
    "JmaAmedasWeather",
    "JmaQuake",
    "JmaTyphoon",
    "register_p12_jma",
]
