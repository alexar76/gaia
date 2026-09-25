"""P5 commercially-clear LIVE relays (2026-09 wave).

* PEGELONLINE DE rivers — DL-DE-Zero-2.0 (reuse ``gaia.river.read@v1``)
* AviationWeather METAR — U.S. PD (``gaia.aviation.read@v1``)
* Digitraffic road weather — CC BY 4.0 (``gaia.road.read@v1``)
* Digitraffic rail locations — CC BY 4.0 (``gaia.rail.read@v1``)
* U.S. Drought Monitor state stats — open + attribution (``gaia.drought.read@v1``)
* GeoShake FDSN/events — CC BY 4.0 (reuse ``gaia.quake.read@v1``)
* Canada NAAD Atom CAP — public CAP-CP redistribution (``gaia.alerts.read@v1`` sibling)
* NOAA SWPC solar wind + GOES X-ray — U.S. PD (deepen spacewx)
* NASA DONKI notifications — NASA open data (``gaia.spacewx.read@v1`` sibling)
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.devices.live_p0 import geojson_centroid, signed_cluster_read
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p5")

# WSV shortnames are mostly ASCII; a few Rhine gauges use umlauts (e.g. KÖLN).
# PEGELONLINE shortnames include spaces / () / umlauts (e.g. "FRANKFURT OSTHAFEN").
_SAFE_PEGEL = re.compile(r"^[\w][\w.\- ()]{0,63}$", re.UNICODE)
_SAFE_ICAO = re.compile(r"^[A-Z]{4}$")
_ATOM_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "georss": "http://www.georss.org/georss",
    "cap": "urn:oasis:names:tc:emergency:cap:1.1",
}

# Approximate state centroids for USDM state-statistics pins (lon, lat).
_US_STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "01": (-86.9023, 32.3182), "02": (-152.4044, 61.3707), "04": (-111.4312, 33.7298),
    "05": (-92.3731, 34.9697), "06": (-119.6816, 36.1162), "08": (-105.3111, 39.0598),
    "09": (-72.7554, 41.5978), "10": (-75.5071, 39.3185), "12": (-81.6868, 27.7663),
    "13": (-83.6431, 33.0406), "15": (-157.4983, 21.0943), "16": (-114.4788, 44.2405),
    "17": (-89.3985, 40.3495), "18": (-86.2583, 39.8494), "19": (-93.2105, 42.0115),
    "20": (-98.4842, 38.5266), "21": (-84.6701, 37.6681), "22": (-91.8678, 31.1695),
    "23": (-69.3819, 44.6939), "24": (-76.8021, 39.0639), "25": (-71.5301, 42.2302),
    "26": (-84.5361, 43.3266), "27": (-93.9002, 45.6945), "28": (-89.6787, 32.7416),
    "29": (-92.2884, 38.4561), "30": (-110.4544, 46.9219), "31": (-99.9018, 41.1254),
    "32": (-117.0554, 38.3135), "33": (-71.5639, 43.4525), "34": (-74.5210, 40.2989),
    "35": (-106.2485, 34.8405), "36": (-74.9481, 42.1657), "37": (-79.8064, 35.6301),
    "38": (-99.7840, 47.5289), "39": (-82.7649, 40.3888), "40": (-96.9289, 35.5653),
    "41": (-122.0709, 44.5720), "42": (-77.2098, 40.5908), "44": (-71.5118, 41.6809),
    "45": (-80.9066, 33.8569), "46": (-99.9018, 44.2998), "47": (-86.6923, 35.7478),
    "48": (-99.9018, 31.0545), "49": (-111.8624, 40.1500), "50": (-72.7107, 44.0459),
    "51": (-78.1697, 37.7693), "53": (-121.4905, 47.4009), "54": (-80.9545, 38.4912),
    "55": (-89.6165, 44.2685), "56": (-107.3025, 42.7559),
}
_US_STATE_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA",
    "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV",
    "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN",
    "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY",
}


def _drought_category_score(row: dict[str, Any]) -> tuple[float, str]:
    """Highest drought category with non-zero area percent → score 0–4."""
    for score, key in ((4.0, "d4"), (3.0, "d3"), (2.0, "d2"), (1.0, "d1"), (0.5, "d0")):
        pct = _num(row.get(key))
        if pct is not None and pct > 0.0:
            return score, key.upper()
    return 0.0, "NONE"


def _cap_severity(text: str) -> float:
    t = (text or "").lower()
    if "extreme" in t or "emergency" in t:
        return 4.0
    if "severe" in t or "warning" in t:
        return 3.0
    if "moderate" in t or "watch" in t:
        return 2.0
    if "yellow" in t or "advisory" in t or "statement" in t:
        return 1.0
    return 1.5


# ── PEGELONLINE ───────────────────────────────────────────────────────────────


class PegelonlineRiver(LiveDevice):
    """German federal waterway gauge — DL-DE-Zero-2.0 (commercial OK)."""

    model = "GAIA-RIVER (PEGELONLINE)"
    policy_id = "pegelonline"
    fields = {
        "discharge_m3s": "m3/s",
        "gage_height_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://www.pegelonline.wsv.de/webservices/rest-api/v2 "
        "(WSV PEGELONLINE raw hydrology; DL-DE-Zero-2.0 — free commercial reuse. "
        "German federal waterways only — not a flood warning, not NL/FR gauges.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str = "BONN",
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        sta = (station or "").strip()
        if not _SAFE_PEGEL.match(sta):
            raise ValueError(f"invalid PEGELONLINE station id: {station!r}")
        self.station = sta
        self.url = (
            "https://www.pegelonline.wsv.de/webservices/rest-api/v2/stations/"
            f"{quote(sta)}.json?includeTimeseries=true&includeCurrentMeasurement=true"
        )
        policy = require_approved_source("pegelonline")
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: PEGELONLINE empty")
        lat = _num(payload.get("latitude"))
        lon = _num(payload.get("longitude"))
        discharge = stage_m = None
        for ts in payload.get("timeseries") or []:
            if not isinstance(ts, dict):
                continue
            cur = ts.get("currentMeasurement") if isinstance(ts.get("currentMeasurement"), dict) else {}
            val = _num(cur.get("value"))
            if val is None:
                continue
            short = str(ts.get("shortname") or "").upper()
            unit = str(ts.get("unit") or "")
            if short == "Q":
                discharge = float(val)
            elif short == "W":
                # PEGELONLINE stage is centimetres on federal gauges.
                stage_m = float(val) / 100.0 if "cm" in unit.lower() or unit == "cm" else float(val)
        if discharge is None and stage_m is None:
            raise DeviceOffline(f"{self.device_id}: PEGELONLINE has no Q/W measurement")
        if lat is None or lon is None:
            raise DeviceOffline(f"{self.device_id}: PEGELONLINE missing coordinates")
        return {
            "discharge_m3s": discharge,
            "gage_height_m": stage_m,
            "latitude": lat,
            "longitude": lon,
        }


# ── Aviation METAR ────────────────────────────────────────────────────────────


class AviationMetar(LiveDevice):
    """AviationWeather.gov METAR — U.S. Government public domain."""

    model = "GAIA-METAR (AviationWeather)"
    policy_id = "aviation_metar"
    fields = {
        "temperature_c": "C",
        "dewpoint_c": "C",
        "wind_mps": "m/s",
        "pressure_hpa": "hPa",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://aviationweather.gov/data/api/ "
        "(NOAA/NWS Aviation Weather Center METAR JSON; U.S. Government public domain. "
        "Airport in-situ METAR — not an NWS land ASOS pin, not a forecast/TAF.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        icao: str = "KJFK",
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        code = (icao or "").strip().upper()
        if not _SAFE_ICAO.match(code):
            raise ValueError(f"invalid ICAO id: {icao!r}")
        self.icao = code
        self.url = (
            "https://aviationweather.gov/api/data/metar"
            f"?ids={quote(code)}&format=json"
        )
        policy = require_approved_source("aviation_metar")
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else []
        if not rows or not isinstance(rows[0], dict):
            raise DeviceOffline(f"{self.device_id}: METAR empty for {self.icao}")
        row = rows[0]
        temp = _num(row.get("temp"))
        if temp is None:
            raise DeviceOffline(f"{self.device_id}: METAR missing temperature")
        wspd_kt = _num(row.get("wspd"))
        wind_mps = float(wspd_kt) * 0.514444 if wspd_kt is not None else None
        return {
            "temperature_c": float(temp),
            "dewpoint_c": _num(row.get("dewp")),
            "wind_mps": wind_mps,
            "pressure_hpa": _num(row.get("altim")),
            "latitude": _num(row.get("lat")),
            "longitude": _num(row.get("lon")),
        }


# ── Digitraffic road weather ──────────────────────────────────────────────────


class DigitrafficRoadWeather(LiveDevice):
    """Fintraffic Digitraffic road weather stations — CC BY 4.0, Finland only."""

    model = "GAIA-ROAD (Digitraffic)"
    policy_id = "digitraffic_road"
    fields = {
        "temperature_c": "C",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://www.digitraffic.fi/en/terms-of-service/ "
        "(Fintraffic Digitraffic road weather via tie.digitraffic.fi; CC BY 4.0 — "
        "commercial reuse with attribution. Finnish roads only — not AIS, not EU traffic.)"
    )
    url = "https://tie.digitraffic.fi/api/weather/v1/stations/data"
    _stations_url = "https://tie.digitraffic.fi/api/weather/v1/stations"
    _default_limit = 200

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("digitraffic_road")
        policy.require_endpoint(self.url)
        policy.require_endpoint(self._stations_url)
        self.headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        """``payload`` is ``(stations_meta FeatureCollection, data dict)``."""
        meta, data = payload if isinstance(payload, tuple) and len(payload) == 2 else (None, payload)
        coords: dict[int, tuple[float, float]] = {}
        features = (meta or {}).get("features") if isinstance(meta, dict) else None
        if isinstance(features, list):
            for feat in features:
                if not isinstance(feat, dict):
                    continue
                props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
                sid = props.get("id") if props.get("id") is not None else feat.get("id")
                c = geojson_centroid(feat.get("geometry"))
                if sid is None or c is None:
                    continue
                try:
                    coords[int(sid)] = (float(c[0]), float(c[1]))
                except (TypeError, ValueError):
                    continue
        stations = (data or {}).get("stations") if isinstance(data, dict) else None
        if not isinstance(stations, list) or not stations:
            raise DeviceOffline(f"{self.device_id}: Digitraffic road weather empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for st in stations:
            if not isinstance(st, dict):
                continue
            try:
                sid = int(st.get("id"))
            except (TypeError, ValueError):
                continue
            latlon = coords.get(sid)
            if not latlon:
                continue
            vals = {
                str(v.get("name")): _num(v.get("value"))
                for v in (st.get("sensorValues") or [])
                if isinstance(v, dict)
            }
            temp = vals.get("ILMA")
            if temp is None:
                continue
            wind = vals.get("KESKITUULI")
            item: dict[str, Any] = {
                "temperature_c": float(temp),
                "latitude": latlon[0],
                "longitude": latlon[1],
                "station_id": str(sid),
            }
            if wind is not None:
                item["wind_mps"] = float(wind)
            scored.append((abs(float(temp)), item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: Digitraffic road weather had no ILMA")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 1000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {
            "temperature_c": float(row["temperature_c"]),
            "wind_mps": float(row["wind_mps"]) if row.get("wind_mps") is not None else None,
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
        }

    def read(self) -> dict[str, Any]:
        meta = self._fetch(self._stations_url)
        data = self._fetch(self.url)
        hotspots = self.collect_hotspots((meta, data))
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("temperature_c", "wind_mps", "latitude", "longitude"),
            meta_keys=("station_id",),
        )


# ── Digitraffic rail ──────────────────────────────────────────────────────────


class DigitrafficRail(LiveDevice):
    """Fintraffic Digitraffic train locations — CC BY 4.0, Finland only."""

    model = "GAIA-RAIL (Digitraffic)"
    policy_id = "digitraffic_rail"
    fields = {
        "speed_kmh": "km/h",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://www.digitraffic.fi/en/terms-of-service/ "
        "(Fintraffic Digitraffic rail locations via rata.digitraffic.fi; CC BY 4.0 — "
        "commercial reuse with attribution. Finnish rail only — not EU rail, not road.)"
    )
    url = "https://rata.digitraffic.fi/api/v1/train-locations/latest"
    _default_limit = 300

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("digitraffic_rail")
        policy.require_endpoint(self.url)
        self.headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = payload if isinstance(payload, list) else []
        if not rows:
            raise DeviceOffline(f"{self.device_id}: Digitraffic rail empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            loc = row.get("location") if isinstance(row.get("location"), dict) else {}
            coords = loc.get("coordinates") if isinstance(loc.get("coordinates"), list) else None
            if not coords or len(coords) < 2:
                continue
            lon, lat = _num(coords[0]), _num(coords[1])
            speed = _num(row.get("speed"))
            if lat is None or lon is None:
                continue
            item: dict[str, Any] = {
                "speed_kmh": float(speed) if speed is not None else 0.0,
                "latitude": float(lat),
                "longitude": float(lon),
                "train_number": str(row.get("trainNumber") or "")[:16],
            }
            scored.append((float(item["speed_kmh"]), item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: Digitraffic rail had no coordinates")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 2000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        return dict(self.collect_hotspots(payload, limit=1)[0])

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("speed_kmh", "latitude", "longitude"),
            meta_keys=("train_number",),
        )


# ── U.S. Drought Monitor ──────────────────────────────────────────────────────


class UsDroughtMonitor(LiveDevice):
    """U.S. Drought Monitor weekly state statistics — open + required attribution."""

    model = "GAIA-DROUGHT (USDM)"
    policy_id = "usdm"
    fields = {
        "severity_score": "score",
        "drought_pct": "pct",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://droughtmonitor.unl.edu/About/Permission.aspx "
        "(U.S. Drought Monitor via usdmdataservices.unl.edu state statistics; "
        "open data — attribute NDMC / USDA / NOAA / NASA. Weekly classification "
        "product — not an in-situ soil moisture gauge, not a flood warning.)"
    )
    url = "https://usdmdataservices.unl.edu/api/StateStatistics/GetDroughtSeverityStatisticsByAreaPercent"
    _default_limit = 60

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("usdm")
        policy.require_endpoint(self.url)
        self.headers = {"Accept": "application/json"}

    def _fetch_states(self) -> list[dict[str, Any]]:
        # Latest published Thursday map: request a two-week window ending today.
        from datetime import date, timedelta

        end = date.today()
        start = end - timedelta(days=14)
        rows: list[dict[str, Any]] = []
        for fips in _US_STATE_CENTROIDS:
            q = (
                f"{self.url}?aoi={quote(fips)}"
                f"&startdate={start.month}/{start.day}/{start.year}"
                f"&enddate={end.month}/{end.day}/{end.year}"
                "&statisticsType=1"
            )
            try:
                payload = self._fetch(q)
            except DeviceOffline:
                continue
            if not isinstance(payload, list) or not payload:
                continue
            # Prefer the newest mapDate for this state.
            newest = None
            for item in payload:
                if isinstance(item, dict) and item.get("mapDate"):
                    if newest is None or str(item["mapDate"]) > str(newest.get("mapDate")):
                        newest = item
            if newest is None:
                continue
            newest = dict(newest)
            newest["_fips"] = fips
            rows.append(newest)
        if not rows:
            raise DeviceOffline(f"{self.device_id}: USDM state statistics empty")
        return rows

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = payload if isinstance(payload, list) else []
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            fips = str(row.get("_fips") or "")
            lonlat = _US_STATE_CENTROIDS.get(fips)
            if not lonlat:
                continue
            score, cat = _drought_category_score(row)
            # USDM percents are nested (D4 ⊂ D3 ⊂ D2 ⊂ D1 ⊂ D0). Report the
            # area share of the highest non-empty category (honest headline).
            drought_pct = 0.0
            for key in ("d4", "d3", "d2", "d1", "d0"):
                pct = _num(row.get(key))
                if pct is not None and pct > 0.0:
                    drought_pct = float(pct)
                    break
            if score < 0.5 and drought_pct <= 0.0:
                continue
            abbr = _US_STATE_ABBR.get(fips, fips)
            scored.append((
                score + drought_pct / 1000.0,
                {
                    "severity_score": float(score),
                    "drought_pct": drought_pct,
                    "latitude": float(lonlat[1]),
                    "longitude": float(lonlat[0]),
                    "state": abbr,
                    "category": cat,
                    "map_date": str(row.get("mapDate") or "")[:32],
                },
            ))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: USDM had no drought area")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 100))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {
            "severity_score": float(row["severity_score"]),
            "drought_pct": float(row["drought_pct"]),
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
        }

    def read(self) -> dict[str, Any]:
        rows = self._fetch_states()
        hotspots = self.collect_hotspots(rows)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("severity_score", "drought_pct", "latitude", "longitude"),
            meta_keys=("state", "category", "map_date"),
        )


# ── GeoShake ──────────────────────────────────────────────────────────────────


class GeoShakeQuake(LiveDevice):
    """GeoShake community earthquake catalog — CC BY 4.0. Complements USGS/EMSC."""

    model = "GAIA-GEOSHAKE"
    policy_id = "geoshake"
    fields = {
        "magnitude": "Mw",
        "depth_km": "km",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.geoshake.org/developer "
        "(GeoShake community earthquake catalog; CC BY 4.0 — credit GeoShake. "
        "Complementary community network — not a USGS or EMSC replacement. "
        "Empty catalog → offline.)"
    )
    url = "https://api.geoshake.org/api/events?limit=100&hours=168"
    _fdsn_url = (
        "https://api.geoshake.org/fdsnws/event/1/query"
        "?format=text&limit=100&orderby=time&minmag=0"
    )
    _default_limit = 100

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("geoshake")
        policy.require_endpoint(self.url)
        policy.require_endpoint(self._fdsn_url)

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        if isinstance(payload, list):
            for row in payload:
                if not isinstance(row, dict):
                    continue
                lat = _num(row.get("lat") if row.get("lat") is not None else row.get("latitude"))
                lon = _num(row.get("lon") if row.get("lon") is not None else row.get("longitude"))
                mag = _num(row.get("mag") if row.get("mag") is not None else row.get("magnitude"))
                depth = _num(row.get("depth") if row.get("depth") is not None else row.get("depth_km"))
                if lat is None or lon is None or mag is None:
                    continue
                scored.append((float(mag), {
                    "magnitude": float(mag),
                    "depth_km": float(depth) if depth is not None else 0.0,
                    "latitude": float(lat),
                    "longitude": float(lon),
                    "region": str(row.get("place") or row.get("region") or "")[:160],
                }))
        elif isinstance(payload, str):
            for line in payload.splitlines():
                if not line or line.startswith("#") or line.startswith("EventID"):
                    continue
                parts = line.split("|")
                if len(parts) < 6:
                    continue
                # FDSN text: EventID|Time|Latitude|Longitude|Depth/km|Author|Catalog|Contributor|ContributorID|MagType|Magnitude|...
                try:
                    lat = float(parts[2])
                    lon = float(parts[3])
                    depth = float(parts[4]) if parts[4] else 0.0
                    mag = float(parts[10]) if len(parts) > 10 and parts[10] else None
                except ValueError:
                    continue
                if mag is None:
                    continue
                scored.append((mag, {
                    "magnitude": mag,
                    "depth_km": abs(depth),
                    "latitude": lat,
                    "longitude": lon,
                    "region": "",
                }))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: GeoShake catalog empty")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 500))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        return dict(self.collect_hotspots(payload, limit=1)[0])

    def read(self) -> dict[str, Any]:
        try:
            payload = self._fetch(self.url)
            hotspots = self.collect_hotspots(payload)
        except DeviceOffline:
            # FDSN text fallback (same licence).
            text = self._fetch_text(self._fdsn_url)
            hotspots = self.collect_hotspots(text)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("magnitude", "depth_km", "latitude", "longitude"),
            meta_keys=("region",),
        )


# ── Canada NAAD ───────────────────────────────────────────────────────────────


def _georss_centroid(entry: ET.Element) -> tuple[float, float] | None:
    """Return (lat, lon) from georss:point or the first georss:polygon ring."""
    point = entry.findtext("georss:point", default="", namespaces=_ATOM_NS) or ""
    parts = point.split()
    if len(parts) >= 2:
        lat, lon = _num(parts[0]), _num(parts[1])
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    for poly in entry.findall("georss:polygon", _ATOM_NS):
        nums = (poly.text or "").split()
        if len(nums) < 6:
            continue
        lats: list[float] = []
        lons: list[float] = []
        for i in range(0, len(nums) - 1, 2):
            la, lo = _num(nums[i]), _num(nums[i + 1])
            if la is None or lo is None:
                continue
            lats.append(float(la))
            lons.append(float(lo))
        if lats and lons:
            return sum(lats) / len(lats), sum(lons) / len(lons)
    return None


class NaadAlerts(LiveDevice):
    """Canada NAAD / Alert Ready Atom — CAP-CP public redistribution."""

    model = "GAIA-ALERTS (NAAD)"
    policy_id = "naad_alerts"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://rss.naad-adna.pelmorex.com/ "
        "(Canadian National Alert Aggregation & Dissemination System Atom; CAP-CP "
        "public alert redistribution. Cite the issuing authority (e.g. Environment "
        "Canada). Canada only — not NWS CAP, not an all-clear when empty.)"
    )
    url = "https://rss.naad-adna.pelmorex.com/"
    _default_limit = 100
    timeout = 45.0

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("naad_alerts")
        policy.require_endpoint(self.url)

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        text = payload if isinstance(payload, str) else None
        if text is None and isinstance(payload, (bytes, bytearray)):
            text = bytes(payload).decode("utf-8", "replace")
        if not text or "<feed" not in text[:2000]:
            raise DeviceOffline(f"{self.device_id}: NAAD Atom empty")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise DeviceOffline(f"{self.device_id}: NAAD Atom parse error") from exc
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in root.findall("a:entry", _ATOM_NS):
            title = (entry.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
            author = (entry.findtext("a:author/a:name", default="", namespaces=_ATOM_NS) or "").strip()
            centroid = _georss_centroid(entry)
            if centroid is None:
                continue
            lat, lon = centroid
            score = _cap_severity(title)
            scored.append((score, {
                "severity_score": score,
                "latitude": float(lat),
                "longitude": float(lon),
                "headline": title[:200],
                "issuer": author[:80],
            }))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: NAAD had no geolocated alerts")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 500))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {
            "severity_score": float(row["severity_score"]),
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
        }

    def read(self) -> dict[str, Any]:
        # Atom is XML — use text fetch (JSON decode would fail).
        self.headers = {
            "Accept": "application/atom+xml, application/xml, text/xml, */*",
        }
        text = self._fetch_text(self.url, max_chars=4_000_000)
        hotspots = self.collect_hotspots(text)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("severity_score", "latitude", "longitude"),
            meta_keys=("headline", "issuer"),
        )


# ── SWPC solar wind / X-ray ───────────────────────────────────────────────────


class SwpcSolarWind(LiveDevice):
    """NOAA SWPC real-time solar wind summary — U.S. PD."""

    model = "GAIA-SWPC-SOLARWIND"
    policy_id = "noaa_swpc"
    fields = {
        "solar_wind_kms": "km/s",
        "bt_nt": "nT",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://services.swpc.noaa.gov "
        "(NOAA SWPC solar-wind summary + RTSW magnetometer; U.S. Government public "
        "domain. L1 / heliophysics — not a planetary Kp substitute alone.)"
    )
    url = "https://services.swpc.noaa.gov/products/summary/solar-wind-speed.json"
    _mag_url = "https://services.swpc.noaa.gov/json/rtsw/rtsw_mag_1m.json"
    # Pin at Boulder SWPC for map geography (same as Kp product).
    _lat = 40.0150
    _lon = -105.2705

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("noaa_swpc")
        policy.require_endpoint(self.url)
        policy.require_endpoint(self._mag_url)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else []
        if not rows or not isinstance(rows[-1], dict):
            raise DeviceOffline(f"{self.device_id}: SWPC solar-wind empty")
        speed = _num(rows[-1].get("proton_speed"))
        if speed is None:
            raise DeviceOffline(f"{self.device_id}: SWPC solar-wind missing speed")
        return {
            "solar_wind_kms": float(speed),
            "bt_nt": None,
            "latitude": self._lat,
            "longitude": self._lon,
        }

    def read(self) -> dict[str, Any]:
        mapped = self.map(self._fetch(self.url))
        bt = None
        try:
            mag = self._fetch(self._mag_url)
            if isinstance(mag, list) and mag and isinstance(mag[-1], dict):
                bt = _num(mag[-1].get("bt"))
        except DeviceOffline:
            bt = None
        hotspot = {
            "solar_wind_kms": float(mapped["solar_wind_kms"]),
            "latitude": self._lat,
            "longitude": self._lon,
        }
        if bt is not None:
            hotspot["bt_nt"] = float(bt)
        return signed_cluster_read(
            self,
            [hotspot],
            numeric_keys=("solar_wind_kms", "bt_nt", "latitude", "longitude"),
        )


class SwpcGoesXray(LiveDevice):
    """NOAA GOES primary X-ray flux — U.S. PD."""

    model = "GAIA-SWPC-XRAY"
    policy_id = "noaa_swpc"
    fields = {
        "xray_flux": "W/m2",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://services.swpc.noaa.gov "
        "(NOAA SWPC GOES primary X-ray flux JSON; U.S. Government public domain. "
        "Soft X-ray irradiance — not a flare class label by itself.)"
    )
    url = "https://services.swpc.noaa.gov/json/goes/primary/xrays-6-hour.json"
    _lat = 40.0150
    _lon = -105.2705

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("noaa_swpc")
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload if isinstance(payload, list) else []
        flux = None
        for row in reversed(rows):
            if isinstance(row, dict):
                flux = _num(row.get("flux"))
                if flux is not None:
                    break
        if flux is None:
            raise DeviceOffline(f"{self.device_id}: GOES X-ray empty")
        return {
            "xray_flux": float(flux),
            "latitude": self._lat,
            "longitude": self._lon,
        }


# ── NASA DONKI ────────────────────────────────────────────────────────────────


class NasaDonki(LiveDevice):
    """NASA DONKI space-weather notifications — NASA open data."""

    model = "GAIA-DONKI (NASA)"
    policy_id = "nasa_donki"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.nasa.gov/ "
        "(NASA DONKI notifications; NASA open data — cite NASA/CCMC DONKI; no "
        "endorsement. Notification catalog — not a Kp index, not an evacuation order.)"
    )
    # Boulder pin for map geography (heliophysics products are not local).
    _lat = 40.0150
    _lon = -105.2705
    _default_limit = 50

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        key = (_env("GAIA_NASA_API_KEY", "") or _env("NASA_API_KEY", "") or "DEMO_KEY").strip()
        self.url = (
            "https://api.nasa.gov/DONKI/notifications"
            f"?type=all&api_key={quote(key)}"
        )
        policy = require_approved_source("nasa_donki")
        policy.require_endpoint(self.url)

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = payload if isinstance(payload, list) else []
        if not rows:
            raise DeviceOffline(f"{self.device_id}: DONKI notifications empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mtype = str(row.get("messageType") or "")
            score = 3.0 if mtype.upper() in {"CME", "FLR", "SEP", "MPC"} else 2.0
            scored.append((score, {
                "severity_score": score,
                "latitude": self._lat,
                "longitude": self._lon,
                "message_type": mtype[:32],
                "message_id": str(row.get("messageID") or "")[:64],
            }))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: DONKI had no notifications")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 200))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {
            "severity_score": float(row["severity_score"]),
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
        }

    def read(self) -> dict[str, Any]:
        hotspots = self.collect_hotspots(self._fetch(self.url))
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("severity_score", "latitude", "longitude"),
            meta_keys=("message_type", "message_id"),
        )


# Clickable map meshes — each row is one ATLAS pin at real coordinates.
# METAR: airport instruments (click the airport → latest METAR).
# PEGELONLINE: German federal gauges (click the gauge → discharge/stage).
METAR_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    # device_id, ICAO, lat, lon, place
    ("metar-kjfk-01", "KJFK", 40.6398, -73.7789, "JFK Airport"),
    ("metar-klga-01", "KLGA", 40.7772, -73.8726, "LaGuardia Airport"),
    ("metar-kbos-01", "KBOS", 42.3656, -71.0096, "Boston Logan"),
    ("metar-kord-01", "KORD", 41.9742, -87.9073, "Chicago O'Hare"),
    ("metar-klax-01", "KLAX", 33.9425, -118.4081, "Los Angeles Intl"),
    ("metar-ksfo-01", "KSFO", 37.6213, -122.3790, "San Francisco Intl"),
    ("metar-egll-01", "EGLL", 51.4700, -0.4543, "London Heathrow"),
    ("metar-lfpg-01", "LFPG", 49.0097, 2.5479, "Paris CDG"),
    ("metar-eddf-01", "EDDF", 50.0379, 8.5622, "Frankfurt Main"),
    ("metar-eddk-01", "EDDK", 50.8659, 7.1427, "Cologne/Bonn"),
    ("metar-eham-01", "EHAM", 52.3105, 4.7683, "Amsterdam Schiphol"),
    ("metar-efhk-01", "EFHK", 60.3172, 24.9633, "Helsinki Vantaa"),
    ("metar-rjtt-01", "RJTT", 35.5494, 139.7798, "Tokyo Haneda"),
    ("metar-yssy-01", "YSSY", -33.9399, 151.1753, "Sydney Kingsford Smith"),
)

PEGEL_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    # device_id, PEGELONLINE shortname, lat, lon, place
    ("pegel-bonn-01", "BONN", 50.7364, 7.1080, "Rhine at Bonn"),
    ("pegel-koeln-01", "KÖLN", 50.9369, 6.9633, "Rhine at Köln"),
    ("pegel-koblenz-01", "KOBLENZ", 50.3586, 7.6047, "Rhine at Koblenz"),
    ("pegel-mainz-01", "MAINZ", 50.0040, 8.2753, "Rhine at Mainz"),
    ("pegel-mannheim-01", "MANNHEIM", 49.4839, 8.4552, "Rhine at Mannheim"),
    ("pegel-worms-01", "WORMS", 49.6318, 8.3775, "Rhine at Worms"),
    ("pegel-speyer-01", "SPEYER", 49.3238, 8.4487, "Rhine at Speyer"),
    ("pegel-maxau-01", "MAXAU", 49.0390, 8.3056, "Rhine at Maxau"),
    ("pegel-kehl-01", "KEHL-KRONENHOF", 48.5633, 7.8077, "Rhine at Kehl"),
    ("pegel-breisach-01", "BREISACH", 48.0432, 7.5726, "Rhine at Breisach"),
    ("pegel-dresden-01", "DRESDEN", 51.0545, 13.7388, "Elbe at Dresden"),
    ("pegel-pirna-01", "PIRNA", 50.9646, 13.9298, "Elbe at Pirna"),
    ("pegel-schoena-01", "SCHÖNA", 50.8758, 14.2352, "Elbe at Schöna"),
    ("pegel-frankfurt-main-01", "FRANKFURT OSTHAFEN", 50.1057, 8.7150, "Main at Frankfurt"),
    ("pegel-raunheim-01", "RAUNHEIM", 50.0162, 8.4483, "Main at Raunheim"),
    ("pegel-koblenz-mosel-01", "Koblenz OP", 50.3679, 7.5807, "Mosel at Koblenz"),
    ("pegel-mannheim-neckar-01", "Mannheim Neckar", 49.4944, 8.4694, "Neckar at Mannheim"),
    ("pegel-hannmuenden-01", "HANN.MUENDEN", 51.4258, 9.6409, "Weser at Hann. Münden"),
    ("pegel-bremen-weser-01", "GROSSE WESERBRÜCKE", 53.0731, 8.8036, "Weser at Bremen"),
    ("pegel-frankfurt-oder-01", "FRANKFURT1 (ODER)", 52.3578, 14.5517, "Oder at Frankfurt"),
    ("pegel-eisenhuettenstadt-01", "EISENHÜTTENSTADT", 52.1532, 14.6879, "Oder at Eisenhüttenstadt"),
    ("pegel-calbe-01", "CALBE UP", 51.9063, 11.7888, "Saale at Calbe"),
)


def register_p5_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P5 commercially-clear relays. Returns count."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_PEGELONLINE_ENABLED", "1"):
        for device_id, station, _lat, _lon, place in PEGEL_MESH:
            try:
                fleet.add(
                    PegelonlineRiver(
                        device_id,
                        clock,
                        station=station,
                        site=f"live-river-pegel-{device_id}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("PEGELONLINE %s skipped: %s", device_id, exc)

    if enabled("GAIA_METAR_ENABLED", "1"):
        for device_id, icao, _lat, _lon, place in METAR_MESH:
            try:
                fleet.add(
                    AviationMetar(
                        device_id,
                        clock,
                        icao=icao,
                        site=f"live-aviation-{icao.lower()}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("METAR %s skipped: %s", device_id, exc)

    if enabled("GAIA_DIGITRAFFIC_ROAD_ENABLED", "1"):
        fleet.add(
            DigitrafficRoadWeather(
                "fintraffic-road-01", clock, site="live-road-finland", key_dir=key_dir
            )
        )
        n += 1

    if enabled("GAIA_DIGITRAFFIC_RAIL_ENABLED", "1"):
        fleet.add(
            DigitrafficRail(
                "fintraffic-rail-01", clock, site="live-rail-finland", key_dir=key_dir
            )
        )
        n += 1

    if enabled("GAIA_USDM_ENABLED", "1"):
        fleet.add(
            UsDroughtMonitor("usdm-01", clock, site="live-drought-us", key_dir=key_dir)
        )
        n += 1

    if enabled("GAIA_GEOSHAKE_ENABLED", "1"):
        fleet.add(
            GeoShakeQuake("geoshake-01", clock, site="live-quake-geoshake", key_dir=key_dir)
        )
        n += 1

    if enabled("GAIA_NAAD_ENABLED", "1"):
        fleet.add(
            NaadAlerts("naad-01", clock, site="live-alerts-canada", key_dir=key_dir)
        )
        n += 1

    if enabled("GAIA_SWPC_SOLARWIND_ENABLED", "1"):
        fleet.add(
            SwpcSolarWind("swpc-solarwind-01", clock, site="live-spacewx-sw", key_dir=key_dir)
        )
        n += 1

    if enabled("GAIA_SWPC_XRAY_ENABLED", "1"):
        fleet.add(
            SwpcGoesXray("swpc-xray-01", clock, site="live-spacewx-xray", key_dir=key_dir)
        )
        n += 1

    if enabled("GAIA_DONKI_ENABLED", "1"):
        fleet.add(
            NasaDonki("donki-01", clock, site="live-spacewx-donki", key_dir=key_dir)
        )
        n += 1

    return n


__all__ = [
    "PegelonlineRiver",
    "AviationMetar",
    "DigitrafficRoadWeather",
    "DigitrafficRail",
    "UsDroughtMonitor",
    "GeoShakeQuake",
    "NaadAlerts",
    "SwpcSolarWind",
    "SwpcGoesXray",
    "NasaDonki",
    "METAR_MESH",
    "PEGEL_MESH",
    "register_p5_relays",
]
