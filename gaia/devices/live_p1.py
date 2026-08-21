"""P1 commercially-clear LIVE relays — verified licences only.

ATLAS:

* Flood — NWS CAP flood/flash-flood alerts (U.S. PD). USGS WaterWatch
  realtime JSON was retired (HTTP 301). GloFAS operational WMS requires
  Copernicus registration; we do **not** scrape it.
* EFFIS current fires (Copernicus / JRC; CC BY 4.0 since 2025-07-02)
* USGS elevated volcanoes (U.S. PD). Existing quake SKU already carries USGS
  PAGER ``alert`` / ShakeMap MMI when present on the GeoJSON feed.

GAIA in-situ:

* DWD SYNOP via Bright Sky (DWD CC BY 4.0 — attribution: Deutscher Wetterdienst)
* ECCC MSC GeoMet climate-hourly (End-use Licence — commercial + attribution)
* Defra AURN via London Air ERG JSON (OGL — cite Defra UK-AIR)
* GeoNet NZ earthquakes (CC BY 3.0 NZ)
* EIA US48 hourly demand (free key; cite EIA, no endorsement)
* UHSLC tide gauges (may be used and redistributed for free)
* KNMI 10-min observations (CC BY 4.0 — requires free ``GAIA_KNMI_API_KEY``)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _lat_lon, _num
from gaia.devices.live_p0 import NwsCapAlerts, geojson_centroid, resolve_wgs84, signed_cluster_read

_SAFE_VNUM = __import__("re").compile(r"^[0-9]{4,8}$")

log = logging.getLogger("gaia.devices.live_p1")

_SAFE_SITE = __import__("re").compile(r"^[A-Za-z0-9_-]{2,16}$")
_SAFE_UHSLC = __import__("re").compile(r"^[0-9]{1,6}$")
_SAFE_EIA = __import__("re").compile(r"^[A-Za-z0-9_-]{8,64}$")

_VOLCANO_SCORE = {
    "warning": 95.0,
    "watch": 80.0,
    "advisory": 55.0,
    "normal": 20.0,
    "unassigned": 40.0,
}


class NwsFloodAlerts(NwsCapAlerts):
    """NWS flood / flash-flood CAP — WaterWatch realtime JSON was retired (HTTP 301).

    GloFAS operational WMS still requires Copernicus registration; we do not scrape it.
    """

    model = "GAIA-FLOOD (NWS CAP)"
    source = (
        "https://api.weather.gov/alerts "
        "(NWS flood/flash-flood CAP GeoJSON; U.S. Government public domain. "
        "USGS WaterWatch /webservices/realtime was retired in 2026. "
        "Copernicus GloFAS WMS is not used — it requires registered access.)"
    )
    url = (
        "https://api.weather.gov/alerts/active"
        "?status=actual&message_type=alert&code=FLW,FFW,FLS,FFS"
    )


class EffisCurrentFires(LiveDevice):
    """Copernicus EFFIS current fires — CC BY 4.0 (cite Copernicus EMS / JRC)."""

    model = "GAIA-EFFIS (Copernicus)"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://forest-fire.emergency.copernicus.eu "
        "(Copernicus EFFIS burnt-area last 7 days; CC BY 4.0 — cite Copernicus EMS / JRC EFFIS)"
    )
    # GeoServer WFS 1.0 GeoJSON here is EPSG:4326 (lat, lon); srsName=CRS:84 is ignored.
    url = (
        "https://maps.effis.emergency.copernicus.eu/effis"
        "?service=WFS&version=1.0.0&request=GetFeature"
        "&typeName=modis.ba.poly.week&maxFeatures=500&outputFormat=geojson"
    )
    timeout = 30.0
    _default_limit = 500

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        features = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: EFFIS current-fires empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") or {}
            if not isinstance(props, dict):
                props = {}
            # maps.effis WFS GeoJSON is EPSG:4326 axis order (lat, lon), not RFC 7946.
            centroid = resolve_wgs84(
                feat.get("geometry"),
                country=str(props.get("COUNTRY") or ""),
                prefer_lat_first_when_ambiguous=True,
            )
            lat = lon = None
            if centroid:
                lat, lon = centroid
            lat = lat if lat is not None else _num(props.get("LATITUDE") or props.get("lat"))
            lon = lon if lon is not None else _num(props.get("LONGITUDE") or props.get("lon"))
            if lat is None or lon is None:
                continue
            area = _num(props.get("AREA_HA") or props.get("area_ha") or props.get("AREA"))
            score = min(100.0, 40.0 + (float(area) / 50.0 if area else 20.0))
            scored.append((
                score,
                {
                    "severity_score": score,
                    "latitude": float(lat),
                    "longitude": float(lon),
                    "area_ha": area,
                    "firedate": str(props.get("FIREDATE") or props.get("firedate") or "")[:40],
                },
            ))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: EFFIS features had no geometry")
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
            meta_keys=("area_ha", "firedate"),
        )


class UsgsVolcano(LiveDevice):
    """USGS elevated volcanoes (alert / aviation color) — U.S. public domain."""

    model = "GAIA-VOLCANO (USGS)"
    fields = {
        "severity_score": "score",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://volcanoes.usgs.gov "
        "(USGS Volcano Hazards Program elevated volcanoes; U.S. Government public domain)"
    )
    url = "https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes"
    _default_limit = 200

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = payload if isinstance(payload, list) else (
            (payload or {}).get("volcanoes") if isinstance(payload, dict) else None
        )
        if not isinstance(rows, list) or not rows:
            raise DeviceOffline(f"{self.device_id}: USGS elevated-volcanoes empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            lat = _num(row.get("latitude") or row.get("lat"))
            lon = _num(row.get("longitude") or row.get("lon"))
            if lat is None or lon is None:
                continue
            alert = str(row.get("alert_level") or row.get("alertLevel") or "unassigned").lower()
            score = _VOLCANO_SCORE.get(alert, 40.0)
            scored.append((
                score,
                {
                    "severity_score": score,
                    "latitude": float(lat),
                    "longitude": float(lon),
                    "name": str(row.get("volcano_name") or row.get("name") or "")[:160],
                    "alert": alert[:40],
                    "color": str(row.get("color_code") or row.get("colorCode") or "")[:40],
                },
            ))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: USGS volcanoes had no coordinates")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 500))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {k: float(row[k]) for k in self.fields}

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        rows = payload if isinstance(payload, list) else (
            (payload or {}).get("volcanoes") if isinstance(payload, dict) else None
        )
        enriched: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = dict(row)
                if _num(item.get("latitude") or item.get("lat")) is None:
                    vnum = str(item.get("vnum") or "").strip()
                    if _SAFE_VNUM.match(vnum):
                        try:
                            meta = self._fetch(
                                "https://volcanoes.usgs.gov/hans-public/api/volcano/"
                                f"getVolcano/{vnum}"
                            )
                        except DeviceOffline:
                            meta = None
                        if isinstance(meta, dict):
                            if meta.get("latitude") is not None:
                                item["latitude"] = meta.get("latitude")
                            if meta.get("longitude") is not None:
                                item["longitude"] = meta.get("longitude")
                enriched.append(item)
        hotspots = self.collect_hotspots(enriched)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("severity_score", "latitude", "longitude"),
            meta_keys=("name", "alert", "color"),
        )


class DwdBrightSky(LiveDevice):
    """DWD in-situ SYNOP via Bright Sky JSON — DWD data is CC BY 4.0."""

    model = "GAIA-DWD (Bright Sky)"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
    }
    source = (
        "https://opendata.dwd.de "
        "(Deutscher Wetterdienst observations via Bright Sky; CC BY 4.0 — "
        "attribution: Deutscher Wetterdienst / Bright Sky)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        latitude: float = 52.52,
        longitude: float = 13.41,
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        self.latitude, self.longitude = float(latitude), float(longitude)
        self.url = (
            "https://api.brightsky.dev/current_weather"
            f"?lat={self.latitude:.4f}&lon={self.longitude:.4f}"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        wx = (payload or {}).get("weather") if isinstance(payload, dict) else None
        if not isinstance(wx, dict):
            return {k: None for k in self.fields}
        wind = _num(wx.get("wind_speed"))
        # Bright Sky wind_speed is km/h.
        wind_mps = (wind / 3.6) if wind is not None else None
        return {
            "temperature_c": _num(wx.get("temperature")),
            "humidity_pct": _num(wx.get("relative_humidity")),
            "pressure_hpa": _num(wx.get("pressure_msl")),
            "wind_mps": wind_mps,
        }


class EcccClimateHourly(LiveDevice):
    """ECCC MSC GeoMet climate-hourly — End-use Licence (commercial + attribution)."""

    model = "GAIA-ECCC (MSC GeoMet)"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.weather.gc.ca "
        "(ECCC MSC GeoMet climate-hourly; End-use Licence — commercial use allowed "
        "with attribution to Environment and Climate Change Canada)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, station: str = "6105978", **kw):
        super().__init__(device_id, clock, **kw)
        sta = (station or "").strip()
        if not sta.isdigit() or not (4 <= len(sta) <= 10):
            raise ValueError(f"invalid ECCC climate station id: {station!r}")
        self.station = sta
        self.url = (
            "https://api.weather.gc.ca/collections/climate-hourly/items"
            f"?f=json&limit=1&sortby=-LOCAL_DATE&CLIMATE_IDENTIFIER={quote(sta)}"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        features = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            return {k: None for k in self.fields}
        feat = features[0] if isinstance(features[0], dict) else {}
        props = feat.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        centroid = geojson_centroid(feat.get("geometry"))
        lat = lon = None
        if centroid:
            lat, lon = centroid
        lat = lat if lat is not None else _num(props.get("LATITUDE") or props.get("lat"))
        lon = lon if lon is not None else _num(props.get("LONGITUDE") or props.get("lon"))
        wind_kmh = _num(props.get("WIND_SPEED") or props.get("wind_speed"))
        # The climate-hourly collection publishes station / sea-level pressure
        # in kPa. GAIA's shared weather contract is hPa.
        pressure_kpa = _num(props.get("STATION_PRESSURE") or props.get("PRESSURE_SEA"))
        return {
            "temperature_c": _num(props.get("TEMP") or props.get("MEAN_TEMP") or props.get("temperature")),
            "humidity_pct": _num(props.get("RELATIVE_HUMIDITY") or props.get("humidity")),
            "pressure_hpa": pressure_kpa * 10.0 if pressure_kpa is not None else None,
            "wind_mps": (wind_kmh / 3.6) if wind_kmh is not None else None,
            "latitude": lat,
            "longitude": lon,
        }


class DefraAurn(LiveDevice):
    """Defra AURN current UK DAQI via London Air ERG JSON.

    MonitoringIndex publishes pollutant-level Daily Air Quality Index values,
    not concentrations. The relay therefore exposes the maximum measured DAQI
    instead of mislabelling the index as PM2.5/PM10 µg/m³.
    """

    model = "GAIA-AURN (Defra)"
    fields = {
        "air_quality_index": "DAQI",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://uk-air.defra.gov.uk "
        "(Defra Automatic Urban and Rural Network; Open Government Licence — "
        "cite Defra UK-AIR; accessed via Imperial College London Air JSON)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, site_code: str = "MY1", **kw):
        super().__init__(device_id, clock, **kw)
        code = (site_code or "").strip().upper()
        if not _SAFE_SITE.match(code):
            raise ValueError(f"invalid AURN site code: {site_code!r}")
        self.site_code = code
        self.url = (
            "https://api.erg.ic.ac.uk/AirQuality/Hourly/MonitoringIndex/"
            f"SiteCode={code}/Json"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        # ERG wraps as HourlyAirQualityIndex / LocalAuthority / Site / Species.
        site = _find_aurn_site(payload)
        if not site:
            return {k: None for k in self.fields}
        lat = _num(site.get("@Latitude") or site.get("Latitude") or site.get("latitude"))
        lon = _num(site.get("@Longitude") or site.get("Longitude") or site.get("longitude"))
        measured_indexes: list[float] = []
        species = site.get("Species") or site.get("species") or []
        if isinstance(species, dict):
            species = [species]
        if isinstance(species, list):
            for sp in species:
                if not isinstance(sp, dict):
                    continue
                index = _num(sp.get("@AirQualityIndex") or sp.get("AirQualityIndex"))
                source = str(sp.get("@IndexSource") or sp.get("IndexSource") or "").lower()
                if index is None or index <= 0 or (source and source != "measurement"):
                    continue
                measured_indexes.append(index)
        return {
            "air_quality_index": max(measured_indexes) if measured_indexes else None,
            "latitude": lat if lat is not None else 51.5225,
            "longitude": lon if lon is not None else -0.1546,
        }

    def sample(self) -> dict[str, float]:
        mapped = self.map(self._fetch(self.url))
        if mapped.get("air_quality_index") is None:
            raise DeviceOffline(f"{self.device_id}: AURN returned no measured DAQI")
        return {k: v for k, v in mapped.items() if v is not None}


def _find_aurn_site(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    root = payload.get("HourlyAirQualityIndex") or payload
    if not isinstance(root, dict):
        return None
    authorities = root.get("LocalAuthority") or root.get("localAuthority") or root
    if isinstance(authorities, dict):
        authorities = [authorities]
    if not isinstance(authorities, list):
        return None
    for auth in authorities:
        if not isinstance(auth, dict):
            continue
        site = auth.get("Site") or auth.get("site")
        if isinstance(site, list) and site and isinstance(site[0], dict):
            return site[0]
        if isinstance(site, dict):
            return site
    return None


class GeoNetQuake(LiveDevice):
    """GNS Science GeoNet NZ earthquakes — CC BY 3.0 NZ."""

    model = "GAIA-GEONET (NZ)"
    fields = {
        "magnitude": "Mw",
        "depth_km": "km",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.geonet.org.nz "
        "(GNS Science GeoNet earthquake feed; CC BY 3.0 NZ — cite GeoNet / GNS Science)"
    )
    url = "https://api.geonet.org.nz/quake?MMI=3"
    _default_limit = 200

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        features = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: GeoNet quake feed empty")
        scored: list[tuple[float, dict[str, float]]] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") or {}
            coords = ((feat.get("geometry") or {}).get("coordinates")) or []
            lon = _num(coords[0]) if len(coords) > 0 else None
            lat = _num(coords[1]) if len(coords) > 1 else None
            depth = _num(coords[2]) if len(coords) > 2 else _num(
                props.get("depth") if isinstance(props, dict) else None
            )
            mag = _num(props.get("magnitude") if isinstance(props, dict) else None)
            if lat is None or lon is None or mag is None:
                continue
            scored.append((
                float(mag),
                {
                    "magnitude": float(mag),
                    "depth_km": float(depth) if depth is not None else 0.0,
                    "latitude": float(lat),
                    "longitude": float(lon),
                },
            ))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: GeoNet had no geolocated quakes")
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
        )


class EiaUsGrid(LiveDevice):
    """EIA US48 hourly electricity demand — free key, cite EIA, no endorsement."""

    model = "GAIA-EIA (US grid demand)"
    fields = {"demand_mw": "MW"}
    source = (
        "https://www.eia.gov "
        "(U.S. Energy Information Administration electricity rto region-data; "
        "free API — cite EIA; no EIA endorsement)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, api_key: str, **kw):
        super().__init__(device_id, clock, **kw)
        key = (api_key or "").strip()
        if not _SAFE_EIA.match(key):
            raise ValueError("invalid EIA API key")
        self.url = (
            "https://api.eia.gov/v2/electricity/rto/region-data/data/"
            "?frequency=hourly&data[0]=value&facets[respondent][]=US48"
            "&facets[type][]=D&sort[0][column]=period&sort[0][direction]=desc"
            f"&offset=0&length=1&api_key={key}"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        resp = (payload or {}).get("response") if isinstance(payload, dict) else None
        rows = (resp or {}).get("data") if isinstance(resp, dict) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return {"demand_mw": None}
        return {"demand_mw": _num(rows[0].get("value"))}


class UhslcTide(LiveDevice):
    """University of Hawaii Sea Level Center fast-delivery gauge — redistributable."""

    model = "GAIA-UHSLC"
    fields = {
        "water_level_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://uhslc.soest.hawaii.edu "
        "(UHSLC fast-delivery tide gauge; may be used and redistributed for free — cite UHSLC)"
    )
    timeout = 20.0

    def __init__(self, device_id: str, clock: SimClock, *, uhslc_id: str = "57", **kw):
        super().__init__(device_id, clock, **kw)
        uid = (uhslc_id or "").strip()
        if not _SAFE_UHSLC.match(uid):
            raise ValueError(f"invalid UHSLC id: {uhslc_id!r}")
        self.uhslc_id = uid
        self.url = (
            "https://uhslc.soest.hawaii.edu/erddap/tabledap/global_hourly_fast.json"
            "?time,sea_level,latitude,longitude"
            f"&uhslc_id={uid}&orderByMax(%22time%22)"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        table = (payload or {}).get("table") if isinstance(payload, dict) else None
        if not isinstance(table, dict):
            return {k: None for k in self.fields}
        names = [str(n) for n in (table.get("columnNames") or [])]
        rows = table.get("rows") or []
        if not names or not isinstance(rows, list) or not rows or not isinstance(rows[-1], list):
            return {k: None for k in self.fields}
        last = rows[-1]
        by = {names[i]: last[i] if i < len(last) else None for i in range(len(names))}
        sl = _num(by.get("sea_level"))
        # UHSLC sea_level is millimetres.
        level_m = (sl / 1000.0) if sl is not None else None
        lon = _num(by.get("longitude"))
        # ERDDAP uses degrees_east (0…360); ATLAS / GeoJSON use −180…180.
        if lon is not None and lon > 180.0:
            lon -= 360.0
        return {
            "water_level_m": level_m,
            "latitude": _num(by.get("latitude")),
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        payload = self._fetch(self.url)
        table = (payload or {}).get("table") if isinstance(payload, dict) else None
        names = [str(n) for n in ((table or {}).get("columnNames") or [])]
        rows = (table or {}).get("rows") or []
        if not names or not isinstance(rows, list) or not rows or not isinstance(rows[-1], list):
            raise DeviceOffline(f"{self.device_id}: UHSLC returned no observation")
        last = rows[-1]
        by = {names[i]: last[i] if i < len(last) else None for i in range(len(names))}
        raw_time = str(by.get("time") or "").strip()
        try:
            observed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError) as exc:
            raise DeviceOffline(f"{self.device_id}: UHSLC observation time invalid") from exc
        # This is the fast-delivery product. Never re-stamp an old archive row as a
        # fresh LIVE reading merely because the gateway fetched it now.
        if age_s < -300 or age_s > 72 * 3600:
            raise DeviceOffline(f"{self.device_id}: UHSLC observation is stale")
        mapped = self.map(payload)
        if mapped.get("water_level_m") is None:
            raise DeviceOffline(f"{self.device_id}: UHSLC returned no sea level")
        return {k: v for k, v in mapped.items() if v is not None}


class KnmiObservation(LiveDevice):
    """KNMI 10-minute in-situ observations — CC BY 4.0, free API key required."""

    model = "GAIA-KNMI"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
    }
    source = (
        "https://dataplatform.knmi.nl "
        "(KNMI 10-minute in-situ meteorological observations; CC BY 4.0 — cite KNMI)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, api_key: str, station: str = "06260", **kw):
        super().__init__(device_id, clock, **kw)
        key = (api_key or "").strip()
        if not key or len(key) > 128:
            raise ValueError("invalid KNMI API key")
        sta = (station or "").strip()
        if not sta.isdigit() or not (4 <= len(sta) <= 6):
            raise ValueError(f"invalid KNMI station id: {station!r}")
        self.headers = {"Authorization": key}
        self.url = (
            "https://api.dataplatform.knmi.nl/edr/v1/collections/"
            "10-minute-in-situ-meteorological-observations"
            f"/locations/{sta}?f=json"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        # EDR CoverageJSON / Feature — take last range values when present.
        if not isinstance(payload, dict):
            return {k: None for k in self.fields}
        params = payload.get("parameters") or payload.get("ranges") or {}
        if not isinstance(params, dict):
            params = {}

        def _last(name: str) -> float | None:
            node = params.get(name) or {}
            if isinstance(node, dict):
                vals = node.get("values") or node.get("value")
                if isinstance(vals, list) and vals:
                    return _num(vals[-1])
                return _num(node.get("values") if not isinstance(vals, list) else None)
            return _num(node)

        # KNMI uses tenths of °C / hPa in some products; EDR is usually SI.
        temp = _last("ta") or _last("temperature") or _last("T")
        rh = _last("rh") or _last("humidity")
        p = _last("p0") or _last("pressure") or _last("pp")
        ff = _last("ff") or _last("windSpeed") or _last("ws")
        return {
            "temperature_c": temp,
            "humidity_pct": rh,
            "pressure_hpa": p,
            "wind_mps": ff,
        }


def register_p1_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P1 layers/kinds when env toggles allow. Returns count."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_FLOOD_ENABLED", "1"):
        fleet.add(NwsFloodAlerts("nws-flood-01", clock, site="live-flood", key_dir=key_dir))
        n += 1
    if enabled("GAIA_EFFIS_ENABLED", "1"):
        fleet.add(EffisCurrentFires("effis-01", clock, site="live-effis", key_dir=key_dir))
        n += 1
    if enabled("GAIA_VOLCANO_ENABLED", "1"):
        fleet.add(UsgsVolcano("usgs-volcano-01", clock, site="live-volcano", key_dir=key_dir))
        n += 1
    if enabled("GAIA_DWD_ENABLED", "1"):
        lat, lon = _lat_lon(
            _env("GAIA_DWD_LAT"), _env("GAIA_DWD_LON"),
            default_lat=52.52, default_lon=13.41,
        )
        fleet.add(
            DwdBrightSky("dwd-01", clock, latitude=lat, longitude=lon,
                         site="live-weather-dwd", key_dir=key_dir)
        )
        n += 1
    if enabled("GAIA_ECCC_ENABLED", "1"):
        try:
            fleet.add(
                EcccClimateHourly(
                    "eccc-01", clock,
                    station=_env("GAIA_ECCC_STATION", "6105978"),
                    site="live-weather-eccc", key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("ECCC relay skipped: %s", exc)
    if enabled("GAIA_AURN_ENABLED", "1"):
        try:
            fleet.add(
                DefraAurn(
                    "aurn-01", clock,
                    site_code=_env("GAIA_AURN_SITE", "MY1"),
                    site="live-air-aurn",
                    key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("Defra AURN skipped: %s", exc)
    if enabled("GAIA_GEONET_ENABLED", "1"):
        fleet.add(GeoNetQuake("geonet-01", clock, site="live-quake-nz", key_dir=key_dir))
        n += 1
    eia_key = _env("GAIA_EIA_API_KEY")
    if eia_key and enabled("GAIA_EIA_ENABLED", "1"):
        try:
            fleet.add(EiaUsGrid("eia-01", clock, api_key=eia_key, site="live-grid-us", key_dir=key_dir))
            n += 1
        except ValueError as exc:
            log.warning("EIA grid skipped: %s", exc)
    elif enabled("GAIA_EIA_ENABLED", "1"):
        log.info("EIA grid skipped (set GAIA_EIA_API_KEY to enable)")
    if enabled("GAIA_UHSLC_ENABLED", "1"):
        try:
            fleet.add(
                UhslcTide(
                    "uhslc-01", clock,
                    uhslc_id=_env("GAIA_UHSLC_ID", "57"),
                    site="live-tide-uhslc", key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("UHSLC skipped: %s", exc)
    knmi_key = _env("GAIA_KNMI_API_KEY")
    if knmi_key and enabled("GAIA_KNMI_ENABLED", "1"):
        try:
            fleet.add(
                KnmiObservation(
                    "knmi-01", clock,
                    api_key=knmi_key,
                    station=_env("GAIA_KNMI_STATION", "06260"),
                    site="live-weather-knmi", key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("KNMI skipped: %s", exc)
    elif enabled("GAIA_KNMI_ENABLED", "1"):
        log.info("KNMI skipped (set GAIA_KNMI_API_KEY to enable)")
    return n


__all__ = [
    "NwsFloodAlerts",
    "EffisCurrentFires",
    "UsgsVolcano",
    "DwdBrightSky",
    "EcccClimateHourly",
    "DefraAurn",
    "GeoNetQuake",
    "EiaUsGrid",
    "UhslcTide",
    "KnmiObservation",
    "register_p1_relays",
]
