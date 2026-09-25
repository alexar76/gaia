"""P12 Vigicrues VIC flood WARNING relays — France, Etalab Licence Ouverte 2.0.

InfoVigiCru.geojson only (not Hub'Eau gauges, not observations.json).
Empty GeoJSON or niveau 1 (green) is not sold as all-clear — fail closed offline.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.devices.live_p0 import geojson_centroid
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p12_flood")

_SAFE_VIC = re.compile(r"^[0-9]{1,4}$")
_SAFE_DEVICE = re.compile(r"^vic-[a-z0-9-]{3,48}$")
_MAX_AGE_S = 6 * 3600
_CACHE_TTL_S = 120.0
_MESH_CAP = 20

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()

_VIGICRUES_GEOJSON = "https://www.vigicrues.gouv.fr/services/InfoVigiCru.geojson"
_VIGICRUES_TERENT = "https://www.vigicrues.gouv.fr/services/TerEntVigiCru.json"


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
    compact = text.replace(".", "-", 2).replace(" ", "T", 1)
    try:
        when = datetime.fromisoformat(compact.replace("Z", "+00:00"))
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


def _geom_centroid(geom: Any) -> tuple[float, float] | None:
    """Return (lat, lon) from feature geometry (MultiLineString tronçons supported)."""
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


class VigicruesFlood(LiveDevice):
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
        self,
        device_id: str,
        clock: SimClock,
        *,
        territory: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        did = (device_id or "").strip().lower()
        if not _SAFE_DEVICE.fullmatch(did):
            raise ValueError(f"invalid Vigicrues device id: {device_id!r}")
        code = (territory or "").strip()
        if not _SAFE_VIC.fullmatch(code):
            raise ValueError(f"invalid VIC territory: {territory!r}")
        self.device_id = did
        self.territory = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = _VIGICRUES_GEOJSON
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(self.url)
        policy.require_endpoint(_VIGICRUES_TERENT)

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
            raise DeviceOffline(
                f"{self.device_id}: Vigicrues no tronçon for territory {self.territory}"
            )
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
        text = _cached(
            "p12flood:vigicrues",
            lambda: self._fetch_text(self.url, max_chars=4_000_000),
        )
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: Vigicrues GeoJSON malformed") from exc
        return {k: v for k, v in self.map(payload).items() if v is not None}


VIGICRUES_MESH: tuple[tuple[str, str, float, float, str], ...] = (
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

# Capabilities / legacy imports expect VicFlood on the monolith name.
VicFlood = VigicruesFlood
assert len(VIGICRUES_MESH) <= _MESH_CAP

P12_FLOOD_COUNT = len(VIGICRUES_MESH)


def atlas_rows() -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for device_id, territory, lat, lon, place in VIGICRUES_MESH:
        rows.append((
            device_id,
            {
                "layer": "flood",
                "label": f"VIC {place}",
                "capability": "gaia.flood.read@v1",
                "lat": lat,
                "lon": lon,
                "place": (
                    f"{place} (Vigicrues VIC · Etalab OL 2.0; WARNING, not Hub'Eau; "
                    f"territory {territory})"
                ),
                "kind": "point",
                "mode": "live",
            },
        ))
    return rows


def register_p12_flood(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register Vigicrues flood WARNING pins; return count added."""

    if _env("GAIA_VIGICRUES_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        return 0

    n = 0
    for device_id, territory, lat, lon, _place in VIGICRUES_MESH:
        try:
            fleet.add(
                VigicruesFlood(
                    device_id, clock, territory=territory,
                    latitude=lat, longitude=lon,
                    site=f"live-flood-{device_id}", key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("Vigicrues %s skipped: %s", device_id, exc)
    return n


__all__ = [
    "VigicruesFlood",
    "VicFlood",
    "VIGICRUES_MESH",
    "P12_FLOOD_COUNT",
    "atlas_rows",
    "register_p12_flood",
]
