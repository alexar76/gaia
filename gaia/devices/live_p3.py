"""P3 commercially-clear LIVE relays — licence-pinned public APIs only.

* NOAA NHC CurrentStorms.json (U.S. PD) — Atlantic / East / Central Pacific
* EMSC FDSN (CC BY 4.0) — Euro-Med density; not a USGS replacement
* UK Environment Agency flood warnings (OGL) — England, not UK
* PTWC Atom on tsunami.gov (U.S. PD) — warning product, not a tide gauge
* Kystverket AIS via BarentsWatch (NLOD 2.0) — Norwegian waters; token required
* ADSB.lol (ODbL 1.0) — public aircraft near an operator anchor; not own-edge ADS-B

Blocked on purpose: GDACS, Geoscience Australia earthquakes, USGS water-quality IV,
OpenSky / ADSBx fallback, GFW / AISStream.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import httpx

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _lat_lon, _num
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices._policy import _assert_url_allowed
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p3")


def _first(row: dict, *keys: str, default=None):
    """First key that is actually present, treating 0 / "" as real values.

    A plain ``a or b`` chain silently drops zero — and zero is a legitimate
    AIS speed, course and navigational status.
    """
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return default

# Same maritime bound as Fintraffic: keep the vessel, drop broken SOG/COG.
_MAX_PLAUSIBLE_SOG = 60.0
_KT_TO_MPS = 0.514444
_FT_TO_M = 0.3048
_SAFE_EA_AREA = re.compile(r"^[0-9A-Za-z]{4,16}$")
_NHC_BASIN = re.compile(r"^(al|ep|cp)\d", re.I)
_PTWC_ALERT = re.compile(
    r"\b(tsunami\s+)?(warning|watch|advisory|threat)\b",
    re.I,
)
_PTWC_INFO_ONLY = re.compile(
    r"earthquake\s+information|information\s+statement|cancellation",
    re.I,
)


def _geojson_lon_lat(geom: Any) -> tuple[float | None, float | None]:
    if not isinstance(geom, dict):
        return None, None
    coords = geom.get("coordinates") or []
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None, None
    return _num(coords[0]), _num(coords[1])


class NhcCyclone(LiveDevice):
    """NOAA NHC / CPHC active storms — Atlantic, East Pacific, Central Pacific.

    Empty season → offline. This is not JTWC and not a NW-Pacific typhoon feed.
    """

    model = "GAIA-CYCLONE (NHC)"
    policy_id = "nhc_cyclone"
    fields = {
        "intensity_kn": "kn",
        "pressure_hpa": "hPa",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://www.nhc.noaa.gov/CurrentStorms.json "
        "(NOAA National Hurricane Center / CPHC active storms; U.S. Government "
        "public domain. Atlantic + East Pacific + Central Pacific only — not "
        "JTWC, not a NW-Pacific typhoon / 台风 advisory, not NASA EONET.)"
    )
    url = "https://www.nhc.noaa.gov/CurrentStorms.json"
    _default_limit = 40

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("nhc_cyclone")
        policy.require_endpoint(self.url)

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        storms = (payload or {}).get("activeStorms") if isinstance(payload, dict) else None
        if not isinstance(storms, list) or not storms:
            raise DeviceOffline(f"{self.device_id}: NHC CurrentStorms empty (off-season)")
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in storms:
            if not isinstance(row, dict):
                continue
            storm_id = str(row.get("id") or "")[:16]
            if storm_id and not _NHC_BASIN.match(storm_id):
                continue
            lat = _num(row.get("latitudeNumeric") if row.get("latitudeNumeric") is not None else row.get("latitude"))
            lon = _num(row.get("longitudeNumeric") if row.get("longitudeNumeric") is not None else row.get("longitude"))
            intensity = _num(row.get("intensity"))
            if lat is None or lon is None or intensity is None:
                continue
            item: dict[str, Any] = {
                "intensity_kn": float(intensity),
                "latitude": float(lat),
                "longitude": float(lon),
                "name": str(row.get("name") or storm_id or "unnamed")[:80],
                "classification": str(row.get("classification") or "")[:8],
                "storm_id": storm_id,
            }
            pressure = _num(row.get("pressure"))
            if pressure is not None:
                item["pressure_hpa"] = float(pressure)
            scored.append((float(intensity), item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: NHC had no geolocated AL/EP/CP storms")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 80))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {k: (float(row[k]) if row.get(k) is not None else None) for k in self.fields}

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("intensity_kn", "pressure_hpa", "latitude", "longitude"),
            meta_keys=("name", "classification", "storm_id"),
        )


class EmscQuake(LiveDevice):
    """EMSC FDSN event query — CC BY 4.0. Distinct pin from USGS; parameters preliminary."""

    model = "GAIA-EMSC (FDSN)"
    policy_id = "emsc_fdsn"
    fields = {
        "magnitude": "Mw",
        "depth_km": "km",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://www.seismicportal.eu/fdsnws/event/1/query "
        "(EMSC-CSEM FDSN event service; CC BY 4.0 — cite EMSC. Parameters are "
        "preliminary. Euro-Mediterranean density plus global M≥4.5 — not a USGS "
        "replacement.)"
    )
    url = (
        "https://www.seismicportal.eu/fdsnws/event/1/query"
        "?limit=100&format=json&orderby=time&minmagnitude=2.5"
    )
    _default_limit = 200

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("emsc_fdsn")
        policy.require_endpoint(self.url)

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        features = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: EMSC FDSN feed empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            lon, lat = _geojson_lon_lat(feat.get("geometry"))
            if lat is None:
                lat = _num(props.get("lat"))
            if lon is None:
                lon = _num(props.get("lon"))
            mag = _num(props.get("mag") if props.get("mag") is not None else props.get("magnitude"))
            depth = _num(props.get("depth"))
            coords = ((feat.get("geometry") or {}).get("coordinates")) if isinstance(feat.get("geometry"), dict) else None
            if depth is None and isinstance(coords, (list, tuple)) and len(coords) > 2:
                depth = _num(coords[2])
            if depth is not None:
                depth = abs(float(depth))
            if lat is None or lon is None or mag is None:
                continue
            item: dict[str, Any] = {
                "magnitude": float(mag),
                "depth_km": float(depth) if depth is not None else 0.0,
                "latitude": float(lat),
                "longitude": float(lon),
            }
            region = str(props.get("flynn_region") or "")[:160]
            if region:
                item["region"] = region
            scored.append((float(mag), item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: EMSC had no geolocated events")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 1000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        return dict(self.collect_hotspots(payload, limit=1)[0])

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("magnitude", "depth_km", "latitude", "longitude"),
            meta_keys=("region",),
        )


class EaFloodWarnings(LiveDevice):
    """UK Environment Agency flood warnings — OGL, England only (not SEPA / NRW)."""

    model = "GAIA-FLOOD (EA England)"
    policy_id = "uk_ea_flood"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://environment.data.gov.uk/flood-monitoring/id/floods "
        "(Environment Agency real-time flood warnings; Open Government Licence. "
        "This uses Environment Agency flood and river level data from the "
        "real-time data API (Beta). England only — not Scotland SEPA, not Wales "
        "NRW, not a GloFAS scrape, not an in-situ river gauge.)"
    )
    url = "https://environment.data.gov.uk/flood-monitoring/id/floods"
    _area_url = "https://environment.data.gov.uk/flood-monitoring/id/floodAreas/"
    _default_limit = 80
    _SEV = {1: 95.0, 2: 80.0, 3: 55.0}

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("uk_ea_flood")
        policy.require_endpoint(self.url)
        self._area_cache: dict[str, tuple[float, float]] = {}

    def _item_lat_lon(self, item: dict[str, Any]) -> tuple[float | None, float | None]:
        lat = _num(item.get("lat") if item.get("lat") is not None else item.get("latitude"))
        lon = _num(item.get("long") if item.get("long") is not None else item.get("longitude"))
        area = item.get("floodArea") if isinstance(item.get("floodArea"), dict) else {}
        if lat is None:
            lat = _num(area.get("lat"))
        if lon is None:
            lon = _num(area.get("long"))
        return lat, lon

    def _enrich_items(self, items: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in items[: self._default_limit]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            lat, lon = self._item_lat_lon(item)
            area_id = str(item.get("floodAreaID") or "")[:16]
            if (lat is None or lon is None) and _SAFE_EA_AREA.match(area_id):
                cached = self._area_cache.get(area_id)
                if cached is None:
                    try:
                        area = self._fetch(self._area_url + quote(area_id, safe=""))
                    except DeviceOffline:
                        area = None
                    blob = area.get("items") if isinstance(area, dict) else None
                    if not isinstance(blob, dict):
                        blob = area if isinstance(area, dict) else {}
                    cached_lat, cached_lon = _num(blob.get("lat")), _num(blob.get("long"))
                    if cached_lat is not None and cached_lon is not None:
                        cached = (float(cached_lat), float(cached_lon))
                        self._area_cache[area_id] = cached
                if cached is not None:
                    lat, lon = cached
                    item["lat"], item["long"] = lat, lon
            out.append(item)
        return out

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        items = (payload or {}).get("items") if isinstance(payload, dict) else None
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            raise DeviceOffline(f"{self.device_id}: EA flood list empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            level = _num(item.get("severityLevel"))
            if level is None or int(level) not in self._SEV:
                continue
            lat, lon = self._item_lat_lon(item)
            if lat is None or lon is None:
                continue
            score = self._SEV[int(level)]
            item_out: dict[str, Any] = {
                "severity_score": float(score),
                "latitude": float(lat),
                "longitude": float(lon),
                "event": str(item.get("severity") or "Flood warning")[:160],
                "headline": str(item.get("description") or item.get("severity") or "")[:300],
                "severity": str(item.get("severity") or "")[:40],
                "area": str(
                    (item.get("floodArea") or {}).get("county")
                    if isinstance(item.get("floodArea"), dict)
                    else item.get("eaAreaName") or ""
                )[:200],
            }
            scored.append((score, item_out))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: EA floods had no geolocated warnings")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 400))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {k: float(row[k]) for k in self.fields}

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        items = (payload or {}).get("items") if isinstance(payload, dict) else None
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            raise DeviceOffline(f"{self.device_id}: EA flood list empty")
        payload = dict(payload)
        payload["items"] = self._enrich_items(items)
        hotspots = self.collect_hotspots(payload)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("severity_score", "latitude", "longitude"),
            meta_keys=("event", "headline", "severity", "area"),
        )


class PtwcTsunamiAlerts(LiveDevice):
    """PTWC Atom feed — Pacific / Caribbean warning product. Empty → offline."""

    model = "GAIA-TSUNAMI (PTWC Atom)"
    policy_id = "ptwc_tsunami"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://www.tsunami.gov/events/xml/PHEBAtom.xml "
        "(Pacific Tsunami Warning Center Atom; U.S. Government public domain. "
        "This is a warning product, not a tide gauge. Information-only earthquake "
        "statements are not sold as warnings. Empty feed → offline / no debit.)"
    )
    url = "https://www.tsunami.gov/events/xml/PHEBAtom.xml"
    _default_limit = 40
    _SEV = {"warning": 95.0, "watch": 80.0, "advisory": 65.0, "threat": 70.0}

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("ptwc_tsunami")
        policy.require_endpoint(self.url)

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        text = payload if isinstance(payload, str) else ""
        if not text.strip():
            raise DeviceOffline(f"{self.device_id}: PTWC Atom empty")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise DeviceOffline(f"{self.device_id}: PTWC Atom unparseable") from exc
        scored: list[tuple[float, dict[str, Any]]] = []
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1].lower()
            if tag != "entry":
                continue
            title = summary = category = ""
            lat = lon = None
            for child in list(el) + list(el.iter()):
                ctag = child.tag.rsplit("}", 1)[-1].lower()
                if ctag == "title" and child.text and not title:
                    title = child.text.strip()
                elif ctag in ("summary", "content") and child.text and not summary:
                    summary = re.sub(r"<[^>]+>", " ", child.text).strip()
                elif ctag == "category":
                    category = str(child.get("term") or child.get("label") or child.text or category)
                elif ctag == "point" and child.text:
                    parts = child.text.split()
                    if len(parts) >= 2:
                        lat, lon = _num(parts[0]), _num(parts[1])
            blob = f"{title} {summary} {category}"
            if _PTWC_INFO_ONLY.search(blob) and not _PTWC_ALERT.search(title or ""):
                continue
            kind = None
            for key in ("warning", "watch", "advisory", "threat"):
                if re.search(rf"\b{key}\b", blob, re.I):
                    kind = key
                    break
            if kind is None:
                continue
            if lat is None or lon is None:
                continue
            score = self._SEV[kind]
            scored.append((
                score,
                {
                    "severity_score": float(score),
                    "latitude": float(lat),
                    "longitude": float(lon),
                    "event": f"Tsunami {kind.title()}"[:160],
                    "headline": (title or summary)[:300],
                    "severity": kind.title()[:40],
                    "area": "PTWC",
                },
            ))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: PTWC Atom has no warning/watch/advisory")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 80))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {k: float(row[k]) for k in self.fields}

    def read(self) -> dict[str, Any]:
        text = self._fetch_text(self.url)
        hotspots = self.collect_hotspots(text)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("severity_score", "latitude", "longitude"),
            meta_keys=("event", "headline", "severity", "area"),
        )


class KystverketAis(LiveDevice):
    """Kystverket AIS via BarentsWatch — NLOD 2.0, Norwegian waters. Token required."""

    model = "GAIA-AIS (Kystverket / BarentsWatch)"
    policy_id = "kystverket_ais"
    fields = {
        "sog_knots": "kn",
        "cog_deg": "deg",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://live.ais.barentswatch.no/v1/latest/combined "
        "(Norwegian Coastal Administration AIS via BarentsWatch; NLOD 2.0 — "
        "commercial reuse with attribution to Kystverket / BarentsWatch. "
        "Norwegian waters / Svalbard / Jan Mayen only — not Finnish AIS, not "
        "global AIS, not GFW, not an own-edge receiver. No fishing vessels "
        "under 15 m; no leisure/sailing under 45 m.)"
    )
    _token_url = "https://id.barentswatch.no/connect/token"
    timeout = 25.0
    _default_limit = 400

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        token: str = "",
        client_id: str = "",
        client_secret: str = "",
        xmin: float = 4.0,
        xmax: float = 32.0,
        ymin: float = 57.0,
        ymax: float = 81.0,
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        self._static_token = (token or "").strip()
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        if not self._static_token and not (self._client_id and self._client_secret):
            raise ValueError("Kystverket AIS needs GAIA_BARENTSWATCH_TOKEN or client id/secret")
        # The bwapi geodata path is retired: it answers 401 even for a valid
        # `ais`-scoped token, which reads as "bad credentials" and sent us
        # hunting for keys that were correct all along. The live AIS service is
        # the current one; it takes the bbox as a POST geometry filter.
        self._bbox = (float(xmin), float(ymin), float(xmax), float(ymax))
        self.url = "https://live.ais.barentswatch.no/v1/latest/combined"
        policy = require_approved_source("kystverket_ais")
        policy.require_endpoint(self.url)
        self._cached_token = ""
        self._token_exp = 0.0
        self.headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

    def _access_token(self) -> str:
        if self._static_token:
            return self._static_token
        now = time.time()
        if self._cached_token and now < self._token_exp:
            return self._cached_token
        _assert_url_allowed(self._token_url)
        try:
            resp = httpx.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "ais",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise DeviceOffline(f"{self.device_id}: BarentsWatch token request failed") from exc
        if resp.status_code != 200:
            raise DeviceOffline(f"{self.device_id}: BarentsWatch token HTTP {resp.status_code}")
        body = resp.json() if resp.content else {}
        token = str((body or {}).get("access_token") or "").strip()
        if not token:
            raise DeviceOffline(f"{self.device_id}: BarentsWatch token empty")
        expires = _num((body or {}).get("expires_in")) or 300.0
        self._cached_token = token
        self._token_exp = now + max(30.0, float(expires) - 60.0)
        return token

    def _fetch(self, url: str) -> Any:
        """POST the bbox filter — the live API has no GET bbox form.

        Filtering server-side is the difference between 238 KB and the full
        1 MB Norwegian snapshot on every read.
        """
        _assert_url_allowed(url)
        self.headers["Authorization"] = f"Bearer {self._access_token()}"
        xmin, ymin, xmax, ymax = self._bbox
        body = {
            "modelType": "Full",
            "modelFormat": "Json",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [xmin, ymin], [xmax, ymin], [xmax, ymax],
                    [xmin, ymax], [xmin, ymin],
                ]],
            },
        }
        try:
            resp = httpx.post(
                url,
                json=body,
                headers={**self.headers, "Content-Type": "application/json"},
                timeout=self.timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise DeviceOffline(
                f"{self.device_id}: upstream unreachable ({type(exc).__name__})"
            ) from exc
        if resp.status_code != 200:
            raise DeviceOffline(f"{self.device_id}: upstream HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: upstream returned non-JSON") from exc

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows: list[Any]
        if isinstance(payload, dict):
            rows = payload.get("features") or payload.get("items") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        if not isinstance(rows, list) or not rows:
            raise DeviceOffline(f"{self.device_id}: BarentsWatch AIS snapshot empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for feat in rows:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else feat
            lon, lat = _geojson_lon_lat(feat.get("geometry"))
            if lat is None:
                lat = _num(props.get("latitude") if props.get("latitude") is not None else props.get("lat"))
            if lon is None:
                lon = _num(props.get("longitude") if props.get("longitude") is not None else props.get("lon"))
            if lat is None or lon is None:
                continue
            # The live service spells these out in full; keep the short forms
            # so a GeoJSON-shaped payload still parses.
            sog = _num(_first(props, "speedOverGround", "sog", "speed"))
            cog = _num(_first(props, "courseOverGround", "cog", "course"))
            if sog is not None and sog > _MAX_PLAUSIBLE_SOG:
                sog = None
            if cog is not None and cog >= 360.0:
                cog = None
            mmsi = props.get("mmsi") or feat.get("mmsi")
            item: dict[str, Any] = {
                "latitude": float(lat),
                "longitude": float(lon),
                "mmsi": str(mmsi or "")[:16],
                # `or ""` would erase navigational status 0 ("under way using
                # engine"), which is a real value and the commonest one.
                "nav_stat": str(
                    _first(props, "navStat", "navigationalStatus", default="")
                )[:8],
            }
            if sog is not None:
                item["sog_knots"] = float(sog)
            if cog is not None:
                item["cog_deg"] = float(cog)
            scored.append((float(item.get("sog_knots") or 0.0), item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: BarentsWatch AIS had no geometry")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 2000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {
            k: (float(row[k]) if row.get(k) is not None else None)
            for k in self.fields
        }

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("sog_knots", "cog_deg", "latitude", "longitude"),
            meta_keys=("mmsi", "nav_stat"),
        )


class AdsbLolTraffic(LiveDevice):
    """ADSB.lol area query — ODbL 1.0. Operator-anchored; not own-edge dump1090."""

    model = "GAIA-ADSB (adsb.lol)"
    policy_id = "adsb_lol"
    fields = {
        "altitude_m": "m",
        "speed_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.adsb.lol "
        "(adsb.lol open API; ODbL 1.0 — commercial reading OK; a public derived "
        "database is share-alike. Cite ADSB.lol. Isolate any derived ADS-B DB. "
        "Not own-edge gaia.adsb.read@v1, not OpenSky, not ADSBx.)"
    )
    timeout = 20.0
    _default_limit = 400

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        latitude: float = 51.4700,
        longitude: float = -0.4543,
        dist_nm: float = 80.0,
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        self.latitude, self.longitude = _lat_lon(
            str(latitude), str(longitude), default_lat=51.4700, default_lon=-0.4543
        )
        dist = max(1.0, min(float(dist_nm), 250.0))
        self.url = (
            "https://api.adsb.lol/v2/lat/"
            f"{self.latitude:.4f}/lon/{self.longitude:.4f}/dist/{dist:.0f}"
        )
        policy = require_approved_source("adsb_lol")
        policy.require_endpoint(self.url)
        self.headers = {"Accept": "application/json"}

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = (payload or {}).get("ac") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise DeviceOffline(f"{self.device_id}: ADSB.lol area empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            lat = _num(row.get("lat"))
            lon = _num(row.get("lon"))
            if lat is None or lon is None:
                continue
            alt_raw = row.get("alt_baro")
            if str(alt_raw).strip().lower() == "ground":
                alt_m = 0.0
            else:
                alt_ft = _num(alt_raw)
                alt_m = float(alt_ft) * _FT_TO_M if alt_ft is not None else None
            gs_kn = _num(row.get("gs"))
            speed = float(gs_kn) * _KT_TO_MPS if gs_kn is not None else None
            icao = str(row.get("hex") or "")[:8]
            item: dict[str, Any] = {
                "latitude": float(lat),
                "longitude": float(lon),
                "icao": icao,
                "flight": str(row.get("flight") or "").strip()[:12],
            }
            if alt_m is not None:
                item["altitude_m"] = float(alt_m)
            if speed is not None:
                item["speed_mps"] = float(speed)
            scored.append((float(item.get("speed_mps") or 0.0), item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: ADSB.lol had no geolocated aircraft")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 2000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {
            k: (float(row[k]) if row.get(k) is not None else None)
            for k in self.fields
        }

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("altitude_m", "speed_mps", "latitude", "longitude"),
            meta_keys=("icao", "flight"),
        )


def register_p3_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P3 commercially-clear relays. Returns count."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_NHC_ENABLED", "1"):
        fleet.add(NhcCyclone("nhc-cyclone-01", clock, site="live-cyclone-nhc", key_dir=key_dir))
        n += 1
    if enabled("GAIA_EMSC_ENABLED", "1"):
        fleet.add(EmscQuake("emsc-01", clock, site="live-quake-emsc", key_dir=key_dir))
        n += 1
    if enabled("GAIA_EA_FLOOD_ENABLED", "1"):
        fleet.add(EaFloodWarnings("ea-flood-01", clock, site="live-flood-ea", key_dir=key_dir))
        n += 1
    if enabled("GAIA_PTWC_ENABLED", "1"):
        fleet.add(PtwcTsunamiAlerts("ptwc-01", clock, site="live-tsunami-ptwc", key_dir=key_dir))
        n += 1
    if enabled("GAIA_KYSTVERKET_AIS_ENABLED", "1"):
        token = _env("GAIA_BARENTSWATCH_TOKEN")
        client_id = _env("GAIA_BARENTSWATCH_CLIENT_ID")
        client_secret = _env("GAIA_BARENTSWATCH_CLIENT_SECRET")
        if token or (client_id and client_secret):
            try:
                fleet.add(
                    KystverketAis(
                        "kystverket-ais-01",
                        clock,
                        token=token,
                        client_id=client_id,
                        client_secret=client_secret,
                        site="live-ais-norway",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("Kystverket AIS skipped: %s", exc)
        else:
            log.info("Kystverket AIS skipped (set GAIA_BARENTSWATCH_TOKEN or client id/secret)")
    if enabled("GAIA_ADSB_LOL_ENABLED", "1"):
        try:
            lat, lon = _lat_lon(
                _env("GAIA_ADSB_LOL_LAT", "51.4700"),
                _env("GAIA_ADSB_LOL_LON", "-0.4543"),
                default_lat=51.4700,
                default_lon=-0.4543,
            )
            dist = float(_env("GAIA_ADSB_LOL_DIST_NM", "80") or "80")
        except ValueError as exc:
            log.warning("ADSB.lol skipped: %s", exc)
        else:
            fleet.add(
                AdsbLolTraffic(
                    "adsb-lol-01",
                    clock,
                    latitude=lat,
                    longitude=lon,
                    dist_nm=dist,
                    site="live-adsb-lol",
                    key_dir=key_dir,
                )
            )
            n += 1
    return n


__all__ = [
    "NhcCyclone",
    "EmscQuake",
    "EaFloodWarnings",
    "PtwcTsunamiAlerts",
    "KystverketAis",
    "AdsbLolTraffic",
    "register_p3_relays",
]
