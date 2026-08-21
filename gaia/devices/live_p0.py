"""P0 commercially-clear LIVE relays — ATLAS event layers + GAIA in-situ kinds.

ATLAS layers (event feeds, map expands ``hotspots[]``):

* NASA EONET natural events (NASA open data / CC0-class; no endorsement)
* NOAA SWPC planetary Kp + OVATION aurora (U.S. public domain)
* GOES-19/18 GLM lightning flashes via NOAA Open Data Dissemination S3 (U.S. PD)
* NWS CAP active alerts (``api.weather.gov`` — free for any purpose)

GAIA in-situ kinds:

* Sensor.Community SDS011 (ODbL + DbCL — cite; share-alike on a derived DB dump)
* MADIS CWOP only, via IEM (NOAA cooperative institute; CWOP has no restrictions)
* Argo profiling float (unrestricted; cite DOI 10.17882/42182)
* MET Norway METAR (CC BY 4.0 + NLOD — in-situ airport instruments, not a model)
* USGS geomag observatory F (U.S. PD — **not** INTERMAGNET / Kyoto Dst)

Own-edge IoT feeders live in ``feeder.py`` (kind ``iot``).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import (
    LiveDevice,
    _env,
    _lat_lon,
    _num,
)

log = logging.getLogger("gaia.devices.live_p0")

_SAFE_CWOP = re.compile(r"^[A-Z]{1,2}[0-9]{3,5}$", re.I)
_SAFE_WMO = re.compile(r"^[0-9]{5,8}$")
_SAFE_ICAO = re.compile(r"^[A-Z]{4}$", re.I)
_SAFE_IMO = re.compile(r"^[A-Z]{3}$")

_KT_TO_MPS = 0.514444
_INHG_TO_HPA = 33.8639
_F_TO_C = lambda f: (f - 32.0) * 5.0 / 9.0  # noqa: E731

_EONET_CATEGORY_SCORE = {
    "volcanoes": 85.0,
    "severeStorms": 80.0,
    "earthquakes": 75.0,
    "wildfires": 70.0,
    "floods": 70.0,
    "landslides": 60.0,
    "temperatureExtremes": 55.0,
    "dustHaze": 50.0,
    "seaLakeIce": 45.0,
    "drought": 40.0,
    "snow": 40.0,
    "waterColor": 30.0,
}

_CAP_SEVERITY = {
    "extreme": 95.0,
    "severe": 80.0,
    "moderate": 55.0,
    "minor": 30.0,
    "unknown": 40.0,
}

# NOAA Boulder / SWPC operations (planetary Kp is not a lat/lon event).
_SWPC_LAT = 40.015
_SWPC_LON = -105.270


def glm_available() -> bool:
    try:
        import h5py  # noqa: F401
    except ImportError:
        return False
    return True


def _ring_centroid(ring: Any) -> tuple[float, float] | None:
    if not isinstance(ring, (list, tuple)) or len(ring) < 3:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for pt in ring:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        lon, lat = _num(pt[0]), _num(pt[1])
        if lat is None or lon is None:
            continue
        xs.append(lon)
        ys.append(lat)
    if not xs:
        return None
    return sum(ys) / len(ys), sum(xs) / len(xs)


def geojson_centroid(geom: Any) -> tuple[float, float] | None:
    """Return (lat, lon) for a GeoJSON geometry. Polygons use the exterior ring."""
    if not isinstance(geom, dict):
        return None
    gtype = str(geom.get("type") or "")
    coords = geom.get("coordinates")
    if gtype == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        lon, lat = _num(coords[0]), _num(coords[1])
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    if gtype == "Polygon" and isinstance(coords, list) and coords:
        return _ring_centroid(coords[0])
    if gtype == "MultiPolygon" and isinstance(coords, list) and coords:
        best: tuple[float, float] | None = None
        best_n = 0
        for poly in coords:
            if not isinstance(poly, list) or not poly:
                continue
            c = _ring_centroid(poly[0])
            n = len(poly[0]) if isinstance(poly[0], list) else 0
            if c is not None and n >= best_n:
                best, best_n = c, n
        return best
    if gtype == "LineString" and isinstance(coords, list):
        return _ring_centroid(coords)
    return None


# south, west, north, east — coarse ISO 3166-1 boxes for EFFIS / WFS axis repair.
_ISO2_BBOX: dict[str, tuple[float, float, float, float]] = {
    "AD": (42.4, 1.4, 42.7, 1.8), "AL": (39.6, 19.3, 42.7, 21.1),
    "AT": (46.3, 9.5, 49.1, 17.2), "AZ": (38.4, 44.8, 41.9, 50.4),
    "BA": (42.5, 15.7, 45.3, 19.6), "BE": (49.5, 2.5, 51.5, 6.4),
    "BG": (41.2, 22.3, 44.2, 28.6), "BY": (51.3, 23.2, 56.2, 32.8),
    "CH": (45.8, 5.9, 47.8, 10.5), "CY": (34.5, 32.2, 35.7, 34.6),
    "CZ": (48.5, 12.1, 51.1, 18.9), "DE": (47.3, 5.9, 55.1, 15.0),
    "DK": (54.5, 8.0, 57.8, 15.2), "DZ": (18.9, -8.7, 37.1, 12.0),
    "EE": (57.5, 21.8, 59.8, 28.2), "EG": (22.0, 24.7, 31.7, 36.9),
    "EL": (34.8, 19.3, 41.8, 29.7), "GR": (34.8, 19.3, 41.8, 29.7),
    "ES": (27.6, -18.2, 43.9, 4.4), "FI": (59.8, 20.5, 70.1, 31.6),
    "FR": (41.3, -5.2, 51.1, 9.6), "GB": (49.8, -8.7, 60.9, 1.8),
    "UK": (49.8, -8.7, 60.9, 1.8), "GE": (41.0, 39.9, 43.6, 46.7),
    "HR": (42.4, 13.5, 46.6, 19.5), "HU": (45.7, 16.1, 48.6, 22.9),
    "IE": (51.4, -10.5, 55.4, -5.9), "IL": (29.5, 34.2, 33.4, 35.9),
    "IQ": (29.1, 38.8, 37.4, 48.6), "IS": (63.3, -24.6, 66.6, -13.5),
    "IT": (36.6, 6.6, 47.1, 18.6), "JO": (29.2, 34.9, 33.4, 39.3),
    "LB": (33.0, 35.1, 34.7, 36.7), "LY": (19.5, 9.3, 33.2, 25.2),
    "LT": (53.9, 20.9, 56.5, 26.9), "LU": (49.4, 5.7, 50.2, 6.5),
    "LV": (55.7, 20.9, 58.1, 28.3), "MA": (21.3, -17.3, 36.0, -1.0),
    "MD": (45.5, 26.6, 48.5, 30.2), "ME": (41.8, 18.4, 43.6, 20.4),
    "MK": (40.8, 20.4, 42.4, 23.0), "MT": (35.8, 14.2, 36.1, 14.6),
    "NL": (50.7, 3.3, 53.6, 7.3), "NO": (57.9, 4.5, 71.2, 31.2),
    "PL": (49.0, 14.1, 54.9, 24.2), "PT": (32.4, -31.3, 42.2, -6.2),
    "RO": (43.6, 20.2, 48.3, 29.8), "RS": (42.2, 18.8, 46.2, 23.0),
    "SE": (55.3, 10.9, 69.1, 24.2), "SI": (45.4, 13.4, 46.9, 16.6),
    "SK": (47.7, 16.8, 49.6, 22.6), "SY": (32.3, 35.7, 37.3, 42.4),
    "TN": (30.2, 7.5, 37.6, 11.6), "TR": (35.8, 25.7, 42.1, 44.9),
    "UA": (44.4, 22.1, 52.4, 40.3), "XK": (42.0, 20.0, 43.3, 21.8),
}
# Europe + Med + N. Africa + Near East — EFFIS Rapid Damage Assessment domain.
_EFFIS_DOMAIN = (20.0, -32.0, 72.0, 50.0)


def _in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    south, west, north, east = bbox
    return south <= lat <= north and west <= lon <= east


def resolve_wgs84(
    geom: Any,
    *,
    country: str | None = None,
    prefer_lat_first_when_ambiguous: bool = False,
) -> tuple[float, float] | None:
    """Return (lat, lon), repairing WFS/EPSG:4326 axis order when needed.

    RFC 7946 GeoJSON is (lon, lat). GeoServer WFS 1.0 ``outputFormat=geojson``
    for EPSG:4326 often emits (lat, lon) and ignores ``srsName=CRS:84``.
    When ``COUNTRY`` is present, keep the pair that lands in that ISO2 box.
    """
    rfc = geojson_centroid(geom)
    if rfc is None:
        return None
    lat_rfc, lon_rfc = rfc
    lat_wfs, lon_wfs = lon_rfc, lat_rfc
    code = str(country or "").strip().upper()
    bbox = _ISO2_BBOX.get(code)
    rfc_ok = _in_bbox(lat_rfc, lon_rfc, bbox) if bbox else _in_bbox(lat_rfc, lon_rfc, _EFFIS_DOMAIN)
    wfs_ok = _in_bbox(lat_wfs, lon_wfs, bbox) if bbox else _in_bbox(lat_wfs, lon_wfs, _EFFIS_DOMAIN)
    if rfc_ok and not wfs_ok:
        return lat_rfc, lon_rfc
    if wfs_ok and not rfc_ok:
        return lat_wfs, lon_wfs
    if rfc_ok and wfs_ok:
        return (lat_wfs, lon_wfs) if prefer_lat_first_when_ambiguous else (lat_rfc, lon_rfc)
    return (lat_wfs, lon_wfs) if prefer_lat_first_when_ambiguous else (lat_rfc, lon_rfc)


def signed_cluster_read(
    device: LiveDevice,
    hotspots: list[dict[str, Any]],
    *,
    numeric_keys: tuple[str, ...],
    meta_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Attest headline = first hotspot; attach the full cluster for ATLAS fan-out."""
    if not hotspots:
        raise DeviceOffline(f"{device.device_id}: empty cluster")
    honest: dict[str, float] = {}
    for key in numeric_keys:
        n = _num(hotspots[0].get(key))
        if n is not None:
            honest[key] = float(n)
    if not honest:
        raise DeviceOffline(f"{device.device_id}: headline has no numeric fields")
    values = {k: round(v, 4) for k, v in device._faulted(honest).items()}
    device._seq += 1
    cluster: list[dict[str, Any]] = []
    for row in hotspots:
        item: dict[str, Any] = {}
        for key in numeric_keys:
            n = _num(row.get(key))
            if n is not None:
                item[key] = round(float(n), 4)
        for key in meta_keys:
            val = row.get(key)
            if val is None:
                continue
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                item[key] = val
            else:
                text = str(val).strip()
                if text:
                    item[key] = text[:500]
        cluster.append(item)
    reading = {
        "device_id": device.device_id,
        "model": device.model,
        "site": device.site,
        "firmware": device.firmware,
        "seq": device._seq,
        "ts": device.clock.iso(),
        "values": values,
        "units": dict(device.fields),
        "hotspots": cluster,
        "hotspot_count": len(cluster),
    }
    if getattr(device, "attribution", ""):
        reading["attribution"] = device.attribution
    from gaia.attestation import sign_reading

    attestation = sign_reading(reading, device.signer)
    device._last_values = dict(values)
    return {"reading": reading, "attestation": attestation}


def parse_metar(text: str) -> dict[str, float]:
    """Parse WMO FM-15 METAR for T/Td, wind, QNH. Last complete report wins."""
    if not text or not str(text).strip():
        return {}
    reports = []
    for line in str(text).replace("\r", "\n").split("\n"):
        body = line.strip()
        if not body:
            continue
        if "METAR" in body.upper() or _SAFE_ICAO.match(body[:4] or ""):
            reports.append(body)
    if not reports:
        # Some MET Norway bodies are a single space-joined bulletin.
        reports = [str(text).strip()]
    out: dict[str, float] = {}
    for raw in reports:
        body = re.sub(r"\s+", " ", raw).strip()
        # Wind: dddssKT / dddssGggKT / dddssMPS (also VRB).
        wind = re.search(
            r"\b(?:VRB|\d{3})(\d{2,3})(?:G\d{2,3})?(KT|MPS)\b", body, re.I
        )
        if wind:
            spd = float(wind.group(1))
            unit = wind.group(2).upper()
            out["wind_mps"] = spd if unit == "MPS" else spd * _KT_TO_MPS
        # Temperature / dewpoint: 17/12 or M01/M03 (M = minus).
        td = re.search(r"\s(M?\d{2})/(M?\d{2})\s", f" {body} ")
        if td:
            def _c(tok: str) -> float:
                neg = tok.startswith("M")
                n = float(tok[1:] if neg else tok)
                return -n if neg else n

            out["temperature_c"] = _c(td.group(1))
            # Dewpoint kept only to derive RH when both present.
            dew = _c(td.group(2))
            t = out["temperature_c"]
            # Magnus formula (WMO-recommended approximation).
            try:
                a, b = 17.625, 243.04
                gamma = (a * dew) / (b + dew) - (a * t) / (b + t)
                rh = 100.0 * math.exp(gamma)
                if 0.0 <= rh <= 100.0:
                    out["humidity_pct"] = rh
            except (ZeroDivisionError, OverflowError):
                pass
        qnh = re.search(r"\bQ(\d{4})\b", body)
        if qnh:
            out["pressure_hpa"] = float(qnh.group(1))
        else:
            alt = re.search(r"\bA(\d{4})\b", body)
            if alt:
                out["pressure_hpa"] = (float(alt.group(1)) / 100.0) * _INHG_TO_HPA
    return out


def parse_glm_lcfa(data: bytes) -> list[dict[str, float]]:
    """Parse GOES GLM-L2-LCFA NetCDF/HDF5 bytes into flash points."""
    try:
        import h5py
    except ImportError as exc:
        raise DeviceOffline("GLM parser requires h5py") from exc
    if not data:
        return []
    flashes: list[dict[str, float]] = []
    with h5py.File(io.BytesIO(data), "r") as handle:
        def _dataset(name: str):
            if name in handle:
                return handle[name]
            for key in handle.keys():
                node = handle[key]
                if hasattr(node, "keys") and name in node:
                    return node[name]
            return None

        def _scaled(name: str):
            ds = _dataset(name)
            if ds is None:
                return None
            values = ds[:]
            scale = _num(ds.attrs.get("scale_factor"))
            offset = _num(ds.attrs.get("add_offset"))
            if scale is None:
                scale = 1.0
            if offset is None:
                offset = 0.0
            fill = ds.attrs.get("_FillValue")
            return values, scale, offset, fill

        lat_pack = _scaled("flash_lat")
        lon_pack = _scaled("flash_lon")
        energy_pack = _scaled("flash_energy")
        if lat_pack is None or lon_pack is None:
            return []
        lat, lat_s, lat_o, lat_fill = lat_pack
        lon, lon_s, lon_o, lon_fill = lon_pack
        n = min(len(lat), len(lon), len(energy_pack[0]) if energy_pack is not None else len(lat))
        for i in range(n):
            if lat_fill is not None and lat[i] == lat_fill:
                continue
            if lon_fill is not None and lon[i] == lon_fill:
                continue
            la = _num(lat[i])
            lo = _num(lon[i])
            if la is None or lo is None:
                continue
            la = la * lat_s + lat_o
            lo = lo * lon_s + lon_o
            if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
                continue
            row: dict[str, float] = {
                "latitude": float(la),
                "longitude": float(lo),
            }
            if energy_pack is not None:
                energy, e_s, e_o, e_fill = energy_pack
                if e_fill is None or energy[i] != e_fill:
                    packed = _num(energy[i])
                    if packed is not None and packed >= 0.0:
                        ej = packed * e_s + e_o
                        if ej >= 0.0:
                            # Packed GLM energy is Joules after scale_factor (~1e-15).
                            row["energy_fj"] = float(ej) * 1e15
            flashes.append(row)
    flashes.sort(key=lambda h: h.get("energy_fj", 0.0), reverse=True)
    return flashes


def _s3_nc_keys(xml_text: str) -> list[str]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    keys: list[str] = []
    for el in root.iter():
        if el.tag.endswith("Key") and el.text and el.text.endswith(".nc"):
            keys.append(el.text.strip())
    keys.sort()
    return keys


# ── NASA EONET ────────────────────────────────────────────────────────────────


class EonetEvents(LiveDevice):
    """NASA Earth Observatory Natural Event Tracker — open events as hotspots."""

    model = "GAIA-EONET (NASA)"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://eonet.gsfc.nasa.gov "
        "(NASA EONET v3; NASA open data — cite NASA EONET; no NASA endorsement)"
    )
    url = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=100"
    _default_limit = 200

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        events = (payload or {}).get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list) or not events:
            raise DeviceOffline(f"{self.device_id}: EONET returned no open events")
        scored: list[tuple[float, dict[str, Any]]] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            geoms = ev.get("geometry") or []
            if not isinstance(geoms, list) or not geoms:
                continue
            last = geoms[-1] if isinstance(geoms[-1], dict) else None
            if last is None:
                continue
            coords = last.get("coordinates")
            lat = lon = None
            if last.get("type") == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
                lon, lat = _num(coords[0]), _num(coords[1])
            else:
                c = geojson_centroid(last)
                if c:
                    lat, lon = c
            if lat is None or lon is None:
                continue
            cats = ev.get("categories") or []
            cat_id = ""
            cat_title = ""
            if isinstance(cats, list) and cats and isinstance(cats[0], dict):
                cat_id = str(cats[0].get("id") or "")
                cat_title = str(cats[0].get("title") or cat_id)
            mag = _num(last.get("magnitudeValue"))
            base = _EONET_CATEGORY_SCORE.get(cat_id, 50.0)
            score = min(100.0, base + (float(mag) if mag is not None else 0.0) * 0.5)
            item: dict[str, Any] = {
                "severity_score": float(score),
                "latitude": float(lat),
                "longitude": float(lon),
                "event_id": str(ev.get("id") or "")[:80],
                "title": str(ev.get("title") or "")[:300],
                "category": cat_title[:120],
            }
            if mag is not None:
                item["magnitude"] = float(mag)
            scored.append((score, item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: EONET events had no geolocated geometry")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 500))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {k: float(row[k]) for k in self.fields}

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("severity_score", "latitude", "longitude"),
            meta_keys=("event_id", "title", "category", "magnitude"),
        )


# ── NOAA SWPC ─────────────────────────────────────────────────────────────────


class SwpcSpaceWeather(LiveDevice):
    """NOAA SWPC planetary Kp (Boulder pin) + OVATION aurora hotspots."""

    model = "GAIA-SWPC (NOAA)"
    fields = {
        "kp_index": "Kp",
        "aurora_pct": "pct",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://services.swpc.noaa.gov "
        "(NOAA Space Weather Prediction Center JSON; U.S. Government public domain)"
    )
    url = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
    _ovation_url = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
    _default_limit = 400

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        """``payload`` is the OVATION JSON; Kp is merged in ``read()``."""
        coords = (payload or {}).get("coordinates") if isinstance(payload, dict) else None
        if not isinstance(coords, list) or not coords:
            # Still sell Kp at Boulder when OVATION is empty.
            return [{
                "kp_index": 0.0,
                "aurora_pct": 0.0,
                "latitude": _SWPC_LAT,
                "longitude": _SWPC_LON,
            }]
        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in coords:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            lon, lat, aurora = _num(row[0]), _num(row[1]), _num(row[2])
            if lat is None or lon is None or aurora is None:
                continue
            if aurora < 5.0:
                continue
            ranked.append(
                (
                    float(aurora),
                    {
                        "aurora_pct": float(aurora),
                        "latitude": float(lat),
                        "longitude": float(lon),
                    },
                )
            )
        ranked.sort(key=lambda t: t[0], reverse=True)
        # 2° grid dedup so the map is not a 50k-point mesh.
        out: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        cap = max(1, min(int(limit or self._default_limit), 2000))
        for aurora, item in ranked:
            key = (int(item["latitude"] * 0.5), int(item["longitude"] * 0.5))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= cap:
                break
        return out or [{
            "aurora_pct": 0.0,
            "latitude": _SWPC_LAT,
            "longitude": _SWPC_LON,
        }]

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else []
        if not rows or not isinstance(rows[-1], dict):
            return {"kp_index": None, "aurora_pct": None, "latitude": _SWPC_LAT, "longitude": _SWPC_LON}
        last = rows[-1]
        kp = _num(last.get("kp_index"))
        if kp is None:
            kp = _num(last.get("estimated_kp"))
        if kp is None:
            kp = _num(last.get("kp"))
        return {
            "kp_index": kp,
            "aurora_pct": None,
            "latitude": _SWPC_LAT,
            "longitude": _SWPC_LON,
        }

    def read(self) -> dict[str, Any]:
        kp_payload = self._fetch(self.url)
        mapped = self.map(kp_payload)
        kp = mapped.get("kp_index")
        if kp is None:
            raise DeviceOffline(f"{self.device_id}: SWPC Kp feed empty")
        try:
            ovation = self._fetch(self._ovation_url)
            aurora_hs = self.collect_hotspots(ovation)
        except DeviceOffline:
            aurora_hs = []
        headline = {
            "kp_index": float(kp),
            "aurora_pct": float(aurora_hs[0]["aurora_pct"]) if aurora_hs else 0.0,
            "latitude": _SWPC_LAT,
            "longitude": _SWPC_LON,
        }
        cluster = [headline]
        for row in aurora_hs:
            item = dict(row)
            item["kp_index"] = float(kp)
            cluster.append(item)
        return signed_cluster_read(
            self,
            cluster,
            numeric_keys=("kp_index", "aurora_pct", "latitude", "longitude"),
        )


# ── GOES GLM ──────────────────────────────────────────────────────────────────


class GoesGlmLightning(LiveDevice):
    """GOES-19/18 GLM-L2-LCFA flashes from NOAA Open Data Dissemination (S3).

    GOES-16 East stopped publishing GLM LCFA in 2025; operational lightning is
    GOES-19 (East) and GOES-18 (West). Both buckets are U.S. public domain.
    """

    model = "GAIA-GLM (NOAA GOES)"
    fields = {
        "energy_fj": "fJ",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://www.noaa.gov/nodd "
        "(NOAA GOES-19/GOES-18 GLM-L2-LCFA via NODD; U.S. Government public domain "
        "— not Blitzortung)"
    )
    url = "https://noaa-goes19.s3.amazonaws.com/"
    timeout = 45.0
    _default_limit = 2000
    _buckets = (
        "noaa-goes19.s3.amazonaws.com",
        "noaa-goes18.s3.amazonaws.com",
    )

    def _list_latest(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        for host in self._buckets:
            keys: list[str] = []
            for hours_back in range(0, 6):
                t = now - timedelta(hours=hours_back)
                prefix = f"GLM-L2-LCFA/{t.year}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"
                try:
                    xml = self._fetch_text(
                        f"https://{host}/?list-type=2&prefix={prefix}&max-keys=200"
                    )
                except DeviceOffline:
                    continue
                keys.extend(_s3_nc_keys(xml))
                if keys:
                    break
            if keys:
                return host, keys[-1]
        raise DeviceOffline(f"{self.device_id}: no recent GLM-L2-LCFA objects on NODD")

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        flashes = payload if isinstance(payload, list) else []
        if not flashes:
            raise DeviceOffline(f"{self.device_id}: GLM file had no flashes")
        cap = max(1, min(int(limit or self._default_limit), 5000))
        return [h for h in flashes[:cap] if isinstance(h, dict)]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {
            "energy_fj": _num(row.get("energy_fj")) or 0.0,
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
        }

    def read(self) -> dict[str, Any]:
        host, key = self._list_latest()
        data = self._fetch_bytes(f"https://{host}/{key}", max_bytes=32 * 1024 * 1024)
        flashes = parse_glm_lcfa(data)
        hotspots = self.collect_hotspots(flashes)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("energy_fj", "latitude", "longitude"),
        )


# ── NWS CAP ───────────────────────────────────────────────────────────────────


class NwsCapAlerts(LiveDevice):
    """NWS CAP GeoJSON active alerts — centroid + WMO severity score."""

    model = "GAIA-CAP (NWS)"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.weather.gov/alerts/active "
        "(NWS Common Alerting Protocol GeoJSON; free for any purpose — U.S. PD)"
    )
    url = "https://api.weather.gov/alerts/active?status=actual&message_type=alert"
    _default_limit = 400

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        features = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: NWS CAP feed empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            centroid = geojson_centroid(feat.get("geometry"))
            if centroid is None:
                continue
            lat, lon = centroid
            props = feat.get("properties") or {}
            if not isinstance(props, dict):
                props = {}
            sev = str(props.get("severity") or "Unknown").strip().lower()
            score = _CAP_SEVERITY.get(sev, 40.0)
            item: dict[str, Any] = {
                "severity_score": float(score),
                "latitude": float(lat),
                "longitude": float(lon),
                "event": str(props.get("event") or "")[:160],
                "headline": str(props.get("headline") or props.get("event") or "")[:300],
                "severity": str(props.get("severity") or "")[:40],
                "area": str(props.get("areaDesc") or "")[:200],
            }
            scored.append((score, item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: NWS CAP alerts had no geometry")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 2000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {k: float(row[k]) for k in self.fields}

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("severity_score", "latitude", "longitude"),
            meta_keys=("event", "headline", "severity", "area"),
        )


# ── Sensor.Community ──────────────────────────────────────────────────────────


class SensorCommunityAir(LiveDevice):
    """Sensor.Community (luftdaten) SDS011 area query — ODbL, cite, no closed dump."""

    model = "GAIA-SC (Sensor.Community)"
    fields = {
        "pm2_5_ugm3": "ug/m3",
        "pm10_ugm3": "ug/m3",
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://data.sensor.community "
        "(Sensor.Community / luftdaten.info; ODbL + DbCL — commercial OK; cite "
        "Sensor.Community; share-alike applies to a derived database dump, not this live query)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        latitude: float = 52.52,
        longitude: float = 13.41,
        radius_km: float = 1.0,
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        self.latitude, self.longitude = float(latitude), float(longitude)
        radius = max(0.1, min(float(radius_km), 25.0))
        self.url = (
            "https://data.sensor.community/airrohr/v1/filter/"
            f"area={self.latitude:.5f},{self.longitude:.5f},{radius:.2f}"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else []
        best: dict[str, float] | None = None
        best_n = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            loc = row.get("location") or {}
            lat = _num((loc or {}).get("latitude"))
            lon = _num((loc or {}).get("longitude"))
            values = row.get("sensordatavalues") or []
            if not isinstance(values, list):
                continue
            fields: dict[str, float] = {}
            for item in values:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("value_type") or "").strip().lower()
                n = _num(item.get("value"))
                if n is None:
                    continue
                if kind in {"p2", "pm2.5", "pm2_5"}:
                    fields["pm2_5_ugm3"] = n
                elif kind in {"p1", "pm10"}:
                    fields["pm10_ugm3"] = n
                elif kind in {"temperature", "temp"}:
                    fields["temperature_c"] = n
                elif kind in {"humidity"}:
                    fields["humidity_pct"] = n
                elif kind in {"pressure"}:
                    # Sensor.Community pressure is often Pa.
                    fields["pressure_hpa"] = n / 100.0 if n > 2000 else n
            if "pm2_5_ugm3" not in fields and "pm10_ugm3" not in fields:
                continue
            if lat is not None and lon is not None:
                fields["latitude"] = float(lat)
                fields["longitude"] = float(lon)
            else:
                fields["latitude"] = self.latitude
                fields["longitude"] = self.longitude
            if len(fields) > best_n:
                best, best_n = fields, len(fields)
        if not best:
            raise DeviceOffline(f"{self.device_id}: Sensor.Community area had no PM readings")
        return {k: best.get(k) for k in self.fields}


# ── MADIS CWOP (via IEM) ──────────────────────────────────────────────────────


class CwopStation(LiveDevice):
    """Citizens Weather Observer Program — MADIS CWOP subset only (no restrictions).

    Accessed via Iowa Environmental Mesonet (NOAA cooperative institute) JSON.
    Do **not** mix restricted MADIS mesonets into this SKU.
    """

    model = "GAIA-CWOP (MADIS)"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://madis.ncep.noaa.gov "
        "(NOAA MADIS Citizens Weather Observer Program — no redistribution "
        "restrictions; accessed via Iowa Environmental Mesonet, NOAA CI. "
        "This SKU is CWOP-only — other MADIS mesonets are not included.)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, station: str = "EW1156", **kw):
        super().__init__(device_id, clock, **kw)
        sta = (station or "").strip().upper()
        if not _SAFE_CWOP.match(sta):
            raise ValueError(f"invalid CWOP station id: {station!r}")
        self.station = sta
        self.url = (
            "https://mesonet.agron.iastate.edu/api/1/ob/current.json"
            f"?network=CWOP&station={self.station}"
        )
        self._fallback_url = (
            "https://mesonet.agron.iastate.edu/json/current.py"
            f"?network=CWOP&station={self.station}"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        row: dict[str, Any] | None = None
        if isinstance(payload, dict):
            data = payload.get("data") or payload.get("last") or payload.get("ob")
            if isinstance(data, list) and data and isinstance(data[0], dict):
                row = data[0]
            elif isinstance(data, dict):
                row = data
            elif isinstance(payload.get("tmpf"), (int, float, str)):
                row = payload
        if not row:
            return {k: None for k in self.fields}
        if row.get("tmpc") is not None:
            temp_c = _num(row.get("tmpc"))
        elif row.get("tmpf") is not None:
            temp_c = _F_TO_C(_num(row.get("tmpf")) or 0.0) if _num(row.get("tmpf")) is not None else None
        else:
            temp_c = None
        humidity = _num(row.get("relh") or row.get("humidity"))
        mslp = _num(row.get("mslp") or row.get("alti") or row.get("pressure"))
        pressure = None
        if mslp is not None:
            pressure = mslp * _INHG_TO_HPA if mslp < 40 else mslp
        sknt = _num(row.get("sknt") or row.get("wind_sknt"))
        wind = (sknt * _KT_TO_MPS) if sknt is not None else _num(row.get("sped"))
        lat = _num(row.get("lat") or row.get("latitude"))
        lon = _num(row.get("lon") or row.get("longitude"))
        return {
            "temperature_c": temp_c,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        try:
            payload = self._fetch(self.url)
        except DeviceOffline:
            payload = self._fetch(self._fallback_url)
        mapped = {k: v for k, v in self.map(payload).items() if v is not None}
        if "temperature_c" not in mapped and "pressure_hpa" not in mapped:
            raise DeviceOffline(f"{self.device_id}: CWOP station {self.station} has no usable obs")
        return mapped


# ── Argo ──────────────────────────────────────────────────────────────────────


class ArgoFloat(LiveDevice):
    """Global active-Argo directory + latest profile for an addressed WMO.

    A float is active under the official ADMT rule when it transmitted within
    the last 30 days.  The default read parses the GDAC global profile index
    into one latest-position row per active WMO.  An invoke carrying ``wmo``
    reads that float's latest near-surface T/S/P from the GDAC ERDDAP view.
    Both result shapes are signed by the same relay identity; the claim is
    source attribution/chain of custody, not that GAIA owns the ocean sensor.
    """

    model = "GAIA-ARGO"
    fields = {
        "temperature_c": "cel",
        "salinity_psu": "PSU",
        "pressure_dbar": "dbar",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://data-argo.ifremer.fr (Argo Global Data Assembly Centre; "
        "unrestricted — cite Argo DOI 10.17882/42182; active = transmission "
        "within 30 days; profiles via Ifremer GDAC ERDDAP)"
    )
    timeout = 60.0

    _DIRECTORY_URL = "https://data-argo.ifremer.fr/ar_index_global_prof.txt.gz"
    _ERDDAP_BASE = "https://erddap.ifremer.fr/erddap/tabledap/ArgoFloats.json"
    _ARGOVIS_BASE = "https://argovis-api.colorado.edu/argo"
    _DOI = "10.17882/42182"
    _ACTIVE_DAYS = 30
    _GOOD_QC = frozenset({"1", "2", "5", "8"})
    _MAX_INDEX_BYTES = 96 * 1024 * 1024
    _MAX_INDEX_ROWS = 6_000_000
    _SAFE_DAC = re.compile(r"^[a-z0-9_-]{2,24}$", re.I)
    _SAFE_PROFILE_FILE = re.compile(r"^[A-Za-z0-9_.-]{8,96}\.nc$")

    def __init__(self, device_id: str, clock: SimClock, *, wmo: str = "4902911", **kw):
        super().__init__(device_id, clock, **kw)
        wmo = (wmo or "").strip()
        if not _SAFE_WMO.match(wmo):
            raise ValueError(f"invalid Argo WMO id: {wmo!r}")
        self.wmo = wmo  # fallback profile when a first-ever GDAC index fetch fails
        self.url = self._DIRECTORY_URL
        self.query_gate = threading.Lock()
        self._state_lock = threading.Lock()
        self._query_wmo: str | None = None
        self._directory: list[dict[str, Any]] | None = None
        self._directory_by_wmo: dict[str, dict[str, Any]] = {}
        self._directory_cached_at = 0.0
        try:
            ttl = float(_env("GAIA_ARGO_DIRECTORY_TTL_S", "3600"))
        except ValueError:
            ttl = 3600.0
        self._directory_ttl_s = max(300.0, min(ttl, 86_400.0))
        cache_path = _env("GAIA_ARGO_CACHE_PATH", "")
        self._cache_path = Path(cache_path) if cache_path else None
        self._directory_meta: dict[str, Any] = {}
        self._load_disk_cache()

    @staticmethod
    def _validate_wmo(raw: Any) -> str:
        wmo = str(raw or "").strip()
        if not _SAFE_WMO.fullmatch(wmo):
            raise ValueError("wmo must be a 5–8 digit Argo platform number")
        return wmo

    def set_wmo(self, raw: Any) -> None:
        with self._state_lock:
            self._query_wmo = self._validate_wmo(raw)

    def clear_wmo(self) -> None:
        with self._state_lock:
            self._query_wmo = None

    def _erddap_url(self, wmo: str, observed_at: str = "") -> str:
        columns = (
            "time,latitude,longitude,"
            "pres,pres_adjusted,pres_qc,pres_adjusted_qc,"
            "temp,temp_adjusted,temp_qc,temp_adjusted_qc,"
            "psal,psal_adjusted,psal_qc,psal_adjusted_qc,data_mode"
        )
        url = f"{self._ERDDAP_BASE}?{columns}&platform_number=%22{wmo}%22&pres<=20"
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z", observed_at or ""):
            return f"{url}&time=%22{observed_at}%22"
        return f"{url}&orderByMax(%22time%22)"

    def _argovis_url(self, wmo: str) -> str:
        return f"{self._ARGOVIS_BASE}?id={wmo}"

    def map(self, payload: Any) -> dict[str, float | None]:
        # Argovis: list of profiles.
        if isinstance(payload, list) and payload:
            return self._map_argovis(payload)
        # ERDDAP tabledap JSON.
        if isinstance(payload, dict) and "table" in payload:
            return self._map_erddap(payload)
        if isinstance(payload, dict) and payload.get("geolocation"):
            return self._map_argovis([payload])
        return {k: None for k in self.fields}

    def _map_argovis(self, rows: list[Any]) -> dict[str, float | None]:
        best: dict[str, float | None] = {k: None for k in self.fields}
        best_time = ""
        for row in rows:
            if not isinstance(row, dict):
                continue
            geo = row.get("geolocation") or {}
            coords = geo.get("coordinates") if isinstance(geo, dict) else None
            lat = lon = None
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                lon, lat = _num(coords[0]), _num(coords[1])
            lat = lat if lat is not None else _num(row.get("lat") or row.get("latitude"))
            lon = lon if lon is not None else _num(row.get("lon") or row.get("longitude"))
            temp = sal = pres = None
            data = row.get("data")
            if isinstance(data, dict):
                temp = _first_num(data.get("temperature") or data.get("temp") or data.get("temperature_degC"))
                sal = _first_num(data.get("salinity") or data.get("psal") or data.get("practical_salinity"))
                pres = _first_num(data.get("pressure_dbar") or data.get("pres") or data.get("pressure"))
            elif isinstance(data, list) and data:
                # [[pres, temp, psal], ...] or named rows
                surface = data[0]
                if isinstance(surface, (list, tuple)) and len(surface) >= 2:
                    pres = _num(surface[0])
                    temp = _num(surface[1])
                    sal = _num(surface[2]) if len(surface) > 2 else None
                elif isinstance(surface, dict):
                    temp = _num(surface.get("temp") or surface.get("temperature"))
                    sal = _num(surface.get("psal") or surface.get("salinity"))
                    pres = _num(surface.get("pres") or surface.get("pressure"))
            ts = str(row.get("timestamp") or row.get("time") or "")
            if temp is None and sal is None:
                continue
            if ts >= best_time:
                best_time = ts
                best = {
                    "temperature_c": temp,
                    "salinity_psu": sal,
                    "pressure_dbar": pres,
                    "latitude": lat,
                    "longitude": lon,
                }
        return best

    def _map_erddap(self, payload: dict[str, Any]) -> dict[str, float | None]:
        by = self._select_erddap_row(payload)
        if not by:
            return {k: None for k in self.fields}
        return {
            "temperature_c": _num(by.get("temp")) if "temp" in by else _num(by.get("temperature")),
            "salinity_psu": _num(by.get("psal")) if "psal" in by else _num(by.get("salinity")),
            "pressure_dbar": _num(by.get("pres")) if "pres" in by else _num(by.get("pressure")),
            "latitude": _num(by.get("latitude")),
            "longitude": _num(by.get("longitude")),
        }

    @staticmethod
    def _qc_value(
        row: dict[str, Any],
        base: str,
        *,
        low: float,
        high: float,
    ) -> float | None:
        for key in (f"{base}_adjusted", base):
            value = _num(row.get(key))
            if value is None or not low <= value <= high:
                continue
            qc_raw = row.get(f"{key}_qc")
            qc = str(qc_raw or "").strip()
            # Old/fallback ERDDAP shapes in the wild omit QC columns; retain
            # bounds validation there. When QC is present, enforce ADMT flags.
            if qc and qc not in ArgoFloat._GOOD_QC:
                continue
            return float(value)
        return None

    @classmethod
    def _select_erddap_row(cls, payload: dict[str, Any]) -> dict[str, Any]:
        table = payload.get("table") or {}
        names = [str(n) for n in (table.get("columnNames") or [])]
        rows = table.get("rows") or []
        if not names or not isinstance(rows, list) or not rows:
            return {}
        best: dict[str, Any] = {}
        best_rank: tuple[str, int, float] = ("", -1, float("-inf"))
        for raw in rows:
            if not isinstance(raw, list):
                continue
            by = {names[i]: raw[i] if i < len(raw) else None for i in range(len(names))}
            ts = str(by.get("time") or "")
            pressure = cls._qc_value(by, "pres", low=0.0, high=12_000.0)
            temp = cls._qc_value(by, "temp", low=-2.5, high=40.0)
            salinity = cls._qc_value(by, "psal", low=2.0, high=41.0)
            usable = int(temp is not None) + int(salinity is not None)
            p = pressure if pressure is not None else float("inf")
            # Prefer newest profile, then the row carrying the most QC-good
            # physics, then the shallowest valid pressure level.
            rank = (ts, usable, -p)
            if rank >= best_rank:
                best = dict(by)
                best["pres"] = pressure
                best["temp"] = temp
                best["psal"] = salinity
                best_rank = rank
        return best

    @staticmethod
    def _argo_time(raw: str) -> str:
        text = str(raw or "").strip()
        try:
            dt = datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return ""
        return dt.isoformat().replace("+00:00", "Z")

    @classmethod
    def parse_gdac_index(
        cls,
        payload: bytes,
        *,
        now: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return latest GDAC position per WMO active in the official 30-day window."""
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=cls._ACTIVE_DAYS)).strftime("%Y%m%d%H%M%S")
        latest: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
        directory_updated_at = ""
        scanned = 0
        try:
            stream = gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb")
            with io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="") as text:
                for line in text:
                    scanned += 1
                    if scanned > cls._MAX_INDEX_ROWS:
                        raise DeviceOffline("Argo GDAC index exceeds safe row limit")
                    if line.startswith("#"):
                        if line.startswith("# Date of update"):
                            directory_updated_at = cls._argo_time(line.partition(":")[2])
                        continue
                    if line.startswith("file,date,"):
                        continue
                    cols = line.rstrip("\r\n").split(",")
                    if len(cols) < 8:
                        continue
                    path, observed = cols[0].strip(), cols[1].strip()
                    if len(observed) < 14 or observed[:14] < cutoff:
                        continue
                    parts = path.split("/")
                    if len(parts) != 4 or parts[2] != "profiles":
                        continue
                    dac, wmo, filename = parts[0], parts[1], parts[3]
                    if (
                        not cls._SAFE_DAC.fullmatch(dac)
                        or not _SAFE_WMO.fullmatch(wmo)
                        or not cls._SAFE_PROFILE_FILE.fullmatch(filename)
                    ):
                        continue
                    lat, lon = _num(cols[2]), _num(cols[3])
                    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        continue
                    observed_at = cls._argo_time(observed)
                    if not observed_at:
                        continue
                    updated = cols[7].strip()
                    profile_url = f"https://data-argo.ifremer.fr/dac/{path}"
                    row = {
                        "wmo": wmo,
                        "latitude": round(float(lat), 5),
                        "longitude": round(float(lon), 5),
                        "observed_at": observed_at,
                        "profile_url": profile_url,
                        "source_url": profile_url,
                        "directory_url": cls._DIRECTORY_URL,
                        "profile_path": path,
                        "dac": dac,
                        "profiler_type": cols[5].strip()[:24],
                        "institution": cols[6].strip()[:24],
                        "date_updated": cls._argo_time(updated),
                    }
                    rank = (observed[:14], updated[:14])
                    previous = latest.get(wmo)
                    if previous is None or rank >= previous[0]:
                        latest[wmo] = (rank, row)
        except (OSError, EOFError) as exc:
            raise DeviceOffline("Argo GDAC index is not valid gzip") from exc
        rows = [latest[wmo][1] for wmo in sorted(latest)]
        if not rows:
            raise DeviceOffline("Argo GDAC index has no active geolocated floats")
        return rows, {
            "directory_updated_at": directory_updated_at,
            "active_window_days": cls._ACTIVE_DAYS,
            "active_float_count": len(rows),
            "source_url": cls._DIRECTORY_URL,
            "doi": cls._DOI,
        }

    def _load_disk_cache(self) -> None:
        path = self._cache_path
        if path is None or not path.is_file():
            return
        try:
            if path.stat().st_size > 12 * 1024 * 1024:
                return
            cached = json.loads(path.read_text(encoding="utf-8"))
            rows = cached.get("floats") if isinstance(cached, dict) else None
            meta = cached.get("meta") if isinstance(cached, dict) else None
            if not isinstance(rows, list) or not rows or not isinstance(meta, dict):
                return
            safe_rows = [row for row in rows if isinstance(row, dict) and _SAFE_WMO.fullmatch(str(row.get("wmo") or ""))]
            if not safe_rows:
                return
            self._directory = safe_rows
            self._directory_by_wmo = {str(row["wmo"]): row for row in safe_rows}
            self._directory_meta = dict(meta)
            age = max(0.0, time.time() - path.stat().st_mtime)
            self._directory_cached_at = time.monotonic() - age
        except (OSError, ValueError, json.JSONDecodeError):
            log.warning("%s: ignoring invalid Argo directory cache", self.device_id)

    def _save_disk_cache(self, rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
        path = self._cache_path
        if path is None:
            return
        tmp = path.with_name(f"{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({"version": 1, "meta": meta, "floats": rows}, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            log.warning("%s: could not persist Argo directory cache", self.device_id)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _directory_snapshot(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        with self._state_lock:
            if (
                self._directory is not None
                and time.monotonic() - self._directory_cached_at < self._directory_ttl_s
            ):
                return list(self._directory), dict(self._directory_meta)
            stale_rows = list(self._directory or [])
            stale_meta = dict(self._directory_meta)
        try:
            payload = self._fetch_bytes(self._DIRECTORY_URL, max_bytes=self._MAX_INDEX_BYTES)
            rows, meta = self.parse_gdac_index(payload)
        except DeviceOffline:
            if stale_rows:
                stale_meta["stale"] = True
                return stale_rows, stale_meta
            raise
        with self._state_lock:
            self._directory = rows
            self._directory_by_wmo = {str(row["wmo"]): row for row in rows}
            self._directory_meta = meta
            self._directory_cached_at = time.monotonic()
        self._save_disk_cache(rows, meta)
        return list(rows), dict(meta)

    def _sign(self, values: dict[str, float], **extra: Any) -> dict[str, Any]:
        from gaia.attestation import sign_reading

        honest = {k: float(v) for k, v in values.items() if isinstance(v, (int, float))}
        signed_values = {k: round(v, 4) for k, v in self._faulted(honest).items()}
        with self._state_lock:
            self._seq += 1
            seq = self._seq
        reading = {
            "device_id": self.device_id,
            "model": self.model,
            "site": self.site,
            "firmware": self.firmware,
            "seq": seq,
            "ts": self.clock.iso(),
            "values": signed_values,
            "units": dict(self.fields),
            **extra,
        }
        attestation = sign_reading(reading, self.signer)
        with self._state_lock:
            self._last_values = dict(signed_values)
        return {"reading": reading, "attestation": attestation}

    def _read_directory(self) -> dict[str, Any]:
        rows, meta = self._directory_snapshot()
        newest = max(rows, key=lambda row: str(row.get("observed_at") or ""))
        return self._sign(
            {
                "latitude": float(newest["latitude"]),
                "longitude": float(newest["longitude"]),
            },
            hotspots=rows,
            hotspot_count=len(rows),
            active_float_count=len(rows),
            active_window_days=self._ACTIVE_DAYS,
            directory_updated_at=meta.get("directory_updated_at"),
            directory_stale=bool(meta.get("stale")),
            source_url=self._DIRECTORY_URL,
            doi=self._DOI,
        )

    def _profile_directory_row(self, wmo: str) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._directory_by_wmo.get(wmo) or {})

    def _read_profile(self, wmo: str) -> dict[str, Any]:
        directory_row = self._profile_directory_row(wmo)
        directory_observed_at = str(directory_row.get("observed_at") or "")
        erddap_url = self._erddap_url(wmo, directory_observed_at)
        observed_at = ""
        upstream_url = erddap_url
        profile_quality = "qc_good"
        try:
            payload = self._fetch(erddap_url)
            mapped = {k: v for k, v in self.map(payload).items() if v is not None}
            if isinstance(payload, dict):
                observed_at = str(self._select_erddap_row(payload).get("time") or "")
            if "temperature_c" not in mapped and "salinity_psu" not in mapped:
                profile_quality = "position_only_qc_rejected"
            if not mapped:
                raise DeviceOffline(f"{self.device_id}: Argo WMO {wmo} has no profile values")
        except DeviceOffline:
            upstream_url = self._argovis_url(wmo)
            payload = self._fetch(upstream_url)
            mapped = {k: v for k, v in self.map(payload).items() if v is not None}
            if isinstance(payload, list):
                observed_at = max(
                    (str(row.get("timestamp") or row.get("time") or "") for row in payload if isinstance(row, dict)),
                    default="",
                )
            if "temperature_c" not in mapped and "salinity_psu" not in mapped:
                raise DeviceOffline(f"{self.device_id}: Argo WMO {wmo} has no recent profile")
            profile_quality = "argovis_fallback_no_gdac_qc"
        profile_url = str(directory_row.get("profile_url") or upstream_url)
        return self._sign(
            {str(k): float(v) for k, v in mapped.items() if isinstance(v, (int, float))},
            wmo=wmo,
            subject_id=f"argo:wmo:{wmo}",
            observed_at=observed_at or directory_row.get("observed_at"),
            profile_url=profile_url,
            source_url=upstream_url,
            directory_url=self._DIRECTORY_URL,
            profile_path=directory_row.get("profile_path"),
            dac=directory_row.get("dac"),
            profile_quality=profile_quality,
            doi=self._DOI,
        )

    def read(self) -> dict[str, Any]:
        with self._state_lock:
            wmo = self._query_wmo
        if wmo:
            return self._read_profile(wmo)
        try:
            return self._read_directory()
        except DeviceOffline:
            # Honest availability fallback: one real profile, never a fabricated
            # directory.  ATLAS keeps its last persisted global snapshot.
            return self._read_profile(self.wmo)

    def sample(self) -> dict[str, float]:
        """Compatibility sampler: latest profile for the configured fallback WMO."""
        payload = self._fetch(self._erddap_url(self.wmo))
        mapped = {k: v for k, v in self.map(payload).items() if v is not None}
        if "temperature_c" not in mapped and "salinity_psu" not in mapped:
            raise DeviceOffline(f"{self.device_id}: Argo WMO {self.wmo} has no recent profile")
        return mapped


def _first_num(value: Any) -> float | None:
    if isinstance(value, list):
        for item in value:
            n = _num(item)
            if n is not None:
                return n
        return None
    return _num(value)


# ── MET Norway METAR ──────────────────────────────────────────────────────────


class MetNorwayMetar(LiveDevice):
    """MET Norway TAF/METAR — in-situ airport instruments (CC BY 4.0 + NLOD)."""

    model = "GAIA-METNO (METAR)"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
    }
    source = (
        "https://api.met.no/weatherapi/tafmetar/1.0/ "
        "(MET Norway TAF/METAR; CC BY 4.0 + NLOD — attribution: MET Norway. "
        "In-situ METAR, not locationforecast model output.)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, icao: str = "ENGM", **kw):
        super().__init__(device_id, clock, **kw)
        icao = (icao or "").strip().upper()
        if not _SAFE_ICAO.match(icao):
            raise ValueError(f"invalid ICAO id: {icao!r}")
        self.icao = icao
        self.url = f"https://api.met.no/weatherapi/tafmetar/1.0/metar.txt?icao={icao}"

    def map(self, payload: Any) -> dict[str, float | None]:  # pragma: no cover
        parsed = parse_metar(str(payload or ""))
        return {k: parsed.get(k) for k in self.fields}

    def sample(self) -> dict[str, float]:
        text = self._fetch_text(self.url)
        mapped = {k: v for k, v in parse_metar(text).items() if v is not None}
        if "temperature_c" not in mapped and "pressure_hpa" not in mapped:
            raise DeviceOffline(f"{self.device_id}: MET Norway METAR {self.icao} unparseable")
        return mapped


# ── USGS geomag ───────────────────────────────────────────────────────────────


class UsgsGeomag(LiveDevice):
    """USGS geomagnetic observatory total field F (nT). Direct USGS — not INTERMAGNET."""

    model = "GAIA-GEOMAG (USGS)"
    fields = {
        "field_nt": "nT",
        "observation_age_s": "s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://geomag.usgs.gov/ws/data/ "
        "(USGS Geomagnetism Program observatory F; U.S. Government public domain. "
        "Not INTERMAGNET (CC BY-NC) and not Kyoto Dst.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        observatory: str = "BOU",
        latitude: float = 40.1375,
        longitude: float = -105.2372,
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        imo = (observatory or "").strip().upper()
        if not _SAFE_IMO.match(imo):
            raise ValueError(f"invalid USGS geomag observatory id: {observatory!r}")
        fallback_lat = float(latitude)
        fallback_lon = float(longitude)
        if not -90.0 <= fallback_lat <= 90.0 or not -180.0 <= fallback_lon <= 180.0:
            raise ValueError("invalid USGS geomag fallback coordinates")
        self.observatory = imo
        self.fallback_latitude = fallback_lat
        self.fallback_longitude = fallback_lon
        self.url = self._request_url()

    def _request_url(self) -> str:
        """Build a moving three-hour window; never freeze the relay at boot time."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=3)
        return (
            "https://geomag.usgs.gov/ws/data/"
            f"?id={self.observatory}&elements=F&format=json&sampling_period=60"
            f"&starttime={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&endtime={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            return {k: None for k in self.fields}
        lat = lon = None
        meta = payload.get("metadata") or {}
        if isinstance(meta, dict):
            imo = ((meta.get("intermagnet") or {}).get("imo") or {}) if isinstance(meta.get("intermagnet"), dict) else {}
            coords = imo.get("coordinates") if isinstance(imo, dict) else None
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                lon, lat = _num(coords[0]), _num(coords[1])
        field = None
        field_index = None
        values = payload.get("values") or []
        if isinstance(values, list):
            for series in values:
                if not isinstance(series, dict):
                    continue
                if str(series.get("id") or "").upper() != "F":
                    continue
                pts = series.get("values") or []
                if not isinstance(pts, list):
                    continue
                for index in range(len(pts) - 1, -1, -1):
                    item = pts[index]
                    n = _num(item)
                    if n is not None:
                        field = n
                        field_index = index
                        break
        if field is None:
            # Alternate shape: times[] + values[] parallel arrays.
            pts = payload.get("F") or payload.get("f")
            if isinstance(pts, list):
                for index in range(len(pts) - 1, -1, -1):
                    item = pts[index]
                    n = _num(item)
                    if n is not None:
                        field = n
                        field_index = index
                        break
        age_s = None
        times = payload.get("times") or []
        if field_index is not None and isinstance(times, list) and field_index < len(times):
            try:
                observed = datetime.fromisoformat(str(times[field_index]).replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                age_s = max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())
            except (TypeError, ValueError):
                age_s = None
        return {
            "field_nt": field,
            "observation_age_s": age_s,
            "latitude": lat if lat is not None else self.fallback_latitude,
            "longitude": lon if lon is not None else self.fallback_longitude,
        }

    def sample(self) -> dict[str, float]:
        self.url = self._request_url()
        mapped = super().sample()
        if "field_nt" not in mapped:
            raise DeviceOffline(f"{self.device_id}: USGS geomag F series empty")
        return mapped


def register_p0_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P0 ATLAS event layers + GAIA in-situ kinds. Returns count."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_EONET_ENABLED", "1"):
        fleet.add(EonetEvents("eonet-01", clock, site="live-events", key_dir=key_dir))
        n += 1
    if enabled("GAIA_SWPC_ENABLED", "1"):
        fleet.add(SwpcSpaceWeather("swpc-01", clock, site="live-spacewx", key_dir=key_dir))
        n += 1
    if enabled("GAIA_GLM_ENABLED", "1"):
        if glm_available():
            fleet.add(GoesGlmLightning("glm-01", clock, site="live-lightning", key_dir=key_dir))
            n += 1
        else:
            log.warning("GOES GLM skipped — install h5py to parse NOAA NODD NetCDF")
    if enabled("GAIA_CAP_ENABLED", "1"):
        fleet.add(NwsCapAlerts("nws-alerts-01", clock, site="live-alerts", key_dir=key_dir))
        n += 1

    if enabled("GAIA_SC_ENABLED", "1"):
        lat, lon = _lat_lon(
            _env("GAIA_SC_LAT"), _env("GAIA_SC_LON"),
            default_lat=52.52, default_lon=13.41,
        )
        try:
            radius = float(_env("GAIA_SC_RADIUS_KM", "1"))
        except ValueError:
            radius = 1.0
        fleet.add(
            SensorCommunityAir(
                "sc-01", clock, latitude=lat, longitude=lon, radius_km=radius,
                site="live-air-sc", key_dir=key_dir,
            )
        )
        n += 1
    if enabled("GAIA_CWOP_ENABLED", "1"):
        try:
            fleet.add(
                CwopStation(
                    "cwop-01", clock,
                    station=_env("GAIA_CWOP_STATION", "EW1156"),
                    site="live-weather-cwop", key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("CWOP relay skipped: %s", exc)
    if enabled("GAIA_ARGO_ENABLED", "1"):
        try:
            fleet.add(
                ArgoFloat(
                    "argo-01", clock,
                    wmo=_env("GAIA_ARGO_WMO", "4902911"),
                    site="live-argo", key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("Argo relay skipped: %s", exc)
    if enabled("GAIA_METNO_ENABLED", "1"):
        try:
            fleet.add(
                MetNorwayMetar(
                    "metno-01", clock,
                    icao=_env("GAIA_METNO_ICAO", "ENGM"),
                    site="live-weather-metno", key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("MET Norway METAR skipped: %s", exc)
    if enabled("GAIA_GEOMAG_ENABLED", "1"):
        try:
            fleet.add(
                UsgsGeomag(
                    "usgs-geomag-01", clock,
                    observatory=_env("GAIA_GEOMAG_IMO", "BOU"),
                    site="live-geomag", key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("USGS geomag skipped: %s", exc)
    return n


__all__ = [
    "EonetEvents",
    "SwpcSpaceWeather",
    "GoesGlmLightning",
    "NwsCapAlerts",
    "SensorCommunityAir",
    "CwopStation",
    "ArgoFloat",
    "MetNorwayMetar",
    "UsgsGeomag",
    "register_p0_relays",
    "parse_metar",
    "parse_glm_lcfa",
    "geojson_centroid",
    "resolve_wgs84",
    "glm_available",
]
