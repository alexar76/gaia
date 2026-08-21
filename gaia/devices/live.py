"""Live devices — real public APIs relayed onto GAIA field names.

A :class:`LiveDevice` is a :class:`~gaia.devices.base.VirtualDevice` whose
``sample()`` does a synchronous ``httpx.get`` against a REAL public API and maps
the response onto the GAIA field vocabulary. Everything downstream — the
Ed25519 attestation, the fleet history, the plausibility verifier, the
Pay-on-Verified escrow envelope — is reused unchanged: a live reading is just a
reading whose numbers came off the wire instead of off a simulator.

WHAT THE KEY ATTESTS (this is the honest part). A simulator's device key stands
in for a secure-element key that proves *sensor ownership*: the device produced
these numbers. A LiveDevice owns no sensor — it is a RELAY. Its key therefore
attests a weaker, precise claim:

    the gateway faithfully relayed what upstream API X returned at fetch time

i.e. a chain-of-custody signature over "this is the payload host X served me,
mapped to GAIA fields, at ts", NOT "this gateway measured the weather". The
upstream provenance (source URL + licence) is recorded on the device as the
``source`` attribute and surfaced via ``Fleet.status()`` so a buyer can see
exactly whose data they are paying to have relayed and verified.

SECURITY. Upstream hosts are allowlisted (SSRF defence). Path/query identifiers
(station ids, lat/lon, box ids) are sanitized; invoke clients never supply a URL.
An unreachable or erroring upstream raises :class:`~gaia.devices.base.DeviceOffline`
→ HTTP 503 → hub 502 → no debit.

CLOCK. Construct with ``SimClock(realtime=True)`` so freshness / rate checks line
up with wall-clock fetch time.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline, VirtualDevice
# Re-exported: eight modules and the test suite import these FROM here, so the split
# must not move the public surface. _live_base holds the base class and coercion
# helpers; _policy holds the access/licence rules.
from gaia.devices._live_base import (
    _FIELD_UNITS,
    _FT3S_TO_M3S,
    _FT_TO_M,
    _SAFE_BOX,
    _SAFE_DS,
    _SAFE_NDBC,
    _SAFE_NOAA,
    _SAFE_OPENAQ,
    _SAFE_STATION,
    _SAFE_USGS_SITE,
    _STA_DEFAULT_BASE,
    _UA,
    LiveDevice,
    _lat_lon,
    _num,
)
# The three Open-Meteo relays moved to live_om.py (one licence, one place); they are
# re-exported below so gaia.devices.live stays the single import surface.
from gaia.devices.live_om import OpenMeteoAirQuality, OpenMeteoMarine, OpenMeteoWeather
from gaia.devices._policy import (
    _ALLOWED_HOSTS,
    _assert_url_allowed,
    _env,
    _om_apikey_suffix,
    _om_auth_headers,
    _om_origin,
    _om_source,
)
from gaia.fleet import Fleet

log = logging.getLogger("gaia.live")

# _FIELD_UNITS / LiveDevice / _num / _lat_lon now live in _live_base.py (imported above).


# ── National Weather Service (api.weather.gov) ────────────────────────────────


class NWSStation(LiveDevice):
    """US National Weather Service latest-observation relay (public domain)."""

    model = "GAIA-WS1 (NWS relay)"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
    }
    source = "https://api.weather.gov (NOAA/NWS observations; U.S. Government public domain)"

    def __init__(self, device_id: str, clock: SimClock, *, station: str = "KNYC", **kw):
        super().__init__(device_id, clock, **kw)
        if not _SAFE_STATION.match(station):
            raise ValueError(f"invalid NWS station id: {station!r}")
        self.station = station.upper()
        self.url = f"https://api.weather.gov/stations/{self.station}/observations/latest"

    def map(self, payload: Any) -> dict[str, float | None]:
        props = (payload or {}).get("properties") or {}

        def field(key: str) -> float | None:
            return _num((props.get(key) or {}).get("value"))

        temp = field("temperature")
        humidity = field("relativeHumidity")
        pa = field("barometricPressure")
        kmh = field("windSpeed")
        return {
            "temperature_c": temp,
            "humidity_pct": humidity,
            "pressure_hpa": pa / 100.0 if pa is not None else None,
            "wind_mps": kmh / 3.6 if kmh is not None else None,
        }


# ── openSenseMap ──────────────────────────────────────────────────────────────


_OSM_MATCH: tuple[tuple[tuple[str, ...], str], ...] = (
    (("pm2.5", "pm2_5", "pm25"), "pm2_5_ugm3"),
    (("pm10",), "pm10_ugm3"),
    (("co2", "carbon dioxide", "kohlendioxid"), "co2_ppm"),
    (("voc",), "voc_index"),
)


class OpenSenseMapBox(LiveDevice):
    """openSenseMap citizen-science box relay (licence per box)."""

    model = "GAIA-AQ1 (openSenseMap relay)"
    fields = {
        "pm2_5_ugm3": "ug/m3",
        "pm10_ugm3": "ug/m3",
        "co2_ppm": "ppm",
        "voc_index": "index",
    }
    source = "https://opensensemap.org (openSenseMap; licence per box — commonly CC BY-SA 4.0 / PDDL)"

    def __init__(self, device_id: str, clock: SimClock, *, box_id: str, **kw):
        super().__init__(device_id, clock, **kw)
        if not _SAFE_BOX.match(box_id):
            raise ValueError(f"invalid openSenseMap box id: {box_id!r}")
        self.box_id = box_id
        self.url = f"https://api.opensensemap.org/boxes/{box_id}?format=json"

    @staticmethod
    def _match(title: str, unit: str) -> str | None:
        hay = f"{title} {unit}".lower()
        for needles, gaia_field in _OSM_MATCH:
            if any(n in hay for n in needles):
                return gaia_field
        return None

    def map(self, payload: Any) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for sensor in (payload or {}).get("sensors") or []:
            if not isinstance(sensor, dict):
                continue
            gaia_field = self._match(str(sensor.get("title", "")), str(sensor.get("unit", "")))
            if gaia_field is None:
                continue
            meas = sensor.get("lastMeasurement")
            if not isinstance(meas, dict):
                continue
            value = _num(meas.get("value"))
            if value is not None:
                out[gaia_field] = value
        return out


# ── OGC SensorThings ──────────────────────────────────────────────────────────


class SensorThingsDatastream(LiveDevice):
    """OGC SensorThings API v1.1 — one Observation per configured Datastream."""

    model = "GAIA-STA (OGC SensorThings relay)"
    timeout = 20.0

    def __init__(self, device_id: str, clock: SimClock, *,
                 datastreams: dict[str, str], base_url: str = _STA_DEFAULT_BASE, **kw):
        super().__init__(device_id, clock, **kw)
        base = base_url.rstrip("/")
        _assert_url_allowed(base + "/")
        for ds_id in datastreams:
            if not _SAFE_DS.match(ds_id):
                raise ValueError(f"invalid SensorThings datastream id: {ds_id!r}")
        self.base_url = base
        self.datastreams = dict(datastreams)
        self.fields = {f: _FIELD_UNITS.get(f, "") for f in self.datastreams.values()}
        self.source = f"OGC SensorThings API v1.1 ({self.base_url}; licence per server operator)"
        self.url = self.base_url

    @staticmethod
    def _latest_result(payload: Any) -> float | None:
        values = (payload or {}).get("value") or []
        if not values or not isinstance(values[0], dict):
            return None
        return _num(values[0].get("result"))

    def sample(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for ds_id, gaia_field in self.datastreams.items():
            url = (f"{self.base_url}/Datastreams({ds_id})/Observations"
                   f"?$orderby=phenomenonTime desc&$top=1")
            value = self._latest_result(self._fetch(url))
            if value is not None:
                out[gaia_field] = value
        return out


# ── UK National Grid carbon intensity ─────────────────────────────────────────


class UKCarbonIntensity(LiveDevice):
    """UK Carbon Intensity API — national half-hour gCO₂/kWh (no key)."""

    model = "GAIA-GRID (UK carbon intensity)"
    fields = {"carbon_intensity_gco2_kwh": "gCO2/kWh"}
    source = (
        "https://carbonintensity.org.uk "
        "(National Grid ESO Carbon Intensity API; open data)"
    )
    url = "https://api.carbonintensity.org.uk/intensity"

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = (payload or {}).get("data") or []
        if not rows or not isinstance(rows[0], dict):
            return {"carbon_intensity_gco2_kwh": None}
        intensity = rows[0].get("intensity") or {}
        # Prefer measured actual; fall back to forecast so the relay stays useful
        # during the brief window before actual is published.
        value = _num(intensity.get("actual"))
        if value is None:
            value = _num(intensity.get("forecast"))
        return {"carbon_intensity_gco2_kwh": value}


# ── USGS earthquakes ──────────────────────────────────────────────────────────


class USGSEarthquake(LiveDevice):
    """USGS GeoJSON feed — strongest/recent M≥2.5 events + ``hotspots`` cluster."""

    model = "GAIA-QUAKE (USGS GeoJSON)"
    fields = {
        "magnitude": "Mw",
        "depth_km": "km",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://earthquake.usgs.gov "
        "(USGS Earthquake Hazards Program GeoJSON; U.S. Government public domain)"
    )
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
    _default_limit = 5000

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, float]]:
        features = (payload or {}).get("features") or []
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: USGS feed has no events")
        scored: list[tuple[float, dict[str, float]]] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") or {}
            coords = ((feat.get("geometry") or {}).get("coordinates")) or []
            lon = _num(coords[0]) if len(coords) > 0 else None
            lat = _num(coords[1]) if len(coords) > 1 else None
            depth = _num(coords[2]) if len(coords) > 2 else None
            mag = _num(props.get("mag"))
            if lat is None or lon is None or mag is None:
                continue
            scored.append(
                (
                    float(mag),
                    {
                        "magnitude": float(mag),
                        "depth_km": float(depth) if depth is not None else 0.0,
                        "latitude": float(lat),
                        "longitude": float(lon),
                    },
                )
            )
        if not scored:
            raise DeviceOffline(f"{self.device_id}: USGS feed has no geolocated events")
        # Prefer magnitude, keep feed order as secondary (already roughly time-sorted).
        scored.sort(key=lambda t: t[0], reverse=True)
        # USGS day feed is already the full public set — keep every geolocated event.
        cap = max(1, min(int(limit or self._default_limit), 10_000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        return dict(self.collect_hotspots(payload, limit=1)[0])

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        honest = dict(hotspots[0])
        values = {k: round(v, 4) for k, v in self._faulted(honest).items()}
        self._seq += 1
        cluster = [
            {
                "magnitude": round(float(h["magnitude"]), 4),
                "depth_km": round(float(h["depth_km"]), 4),
                "latitude": round(float(h["latitude"]), 4),
                "longitude": round(float(h["longitude"]), 4),
            }
            for h in hotspots
        ]
        reading = {
            "device_id": self.device_id,
            "model": self.model,
            "site": self.site,
            "firmware": self.firmware,
            "seq": self._seq,
            "ts": self.clock.iso(),
            "values": values,
            "units": dict(self.fields),
            "hotspots": cluster,
            "hotspot_count": len(cluster),
        }
        if self.attribution:
            reading["attribution"] = self.attribution
        from gaia.attestation import sign_reading

        attestation = sign_reading(reading, self.signer)
        self._last_values = dict(values)
        return {"reading": reading, "attestation": attestation}


# ── NOAA CO-OPS tides ─────────────────────────────────────────────────────────


class NOAATideStation(LiveDevice):
    """NOAA CO-OPS latest water level (metric, MLLW) — public domain."""

    model = "GAIA-TIDE (NOAA CO-OPS)"
    fields = {"water_level_m": "m"}
    source = (
        "https://tidesandcurrents.noaa.gov "
        "(NOAA CO-OPS Data API; U.S. Government public domain)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, station: str = "8518750", **kw):
        super().__init__(device_id, clock, **kw)
        if not _SAFE_NOAA.match(station):
            raise ValueError(f"invalid NOAA tide station id: {station!r}")
        self.station = station
        self.url = (
            "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
            f"?date=latest&station={station}&product=water_level"
            "&datum=MLLW&units=metric&time_zone=gmt&format=json"
            "&application=GAIA-iot.modelmarket.dev"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = (payload or {}).get("data") or []
        if not rows or not isinstance(rows[0], dict):
            # NOAA returns {"error": {...}} when a station is quiet — treat as offline
            # by returning empty so sample() yields {} and callers see no values;
            # raise if the payload is an explicit error object.
            if isinstance(payload, dict) and payload.get("error"):
                raise DeviceOffline(f"{self.device_id}: NOAA reported error")
            return {"water_level_m": None}
        return {"water_level_m": _num(rows[0].get("v"))}


# ── USGS NWIS rivers ───────────────────────────────────────────────────────────


class USGSRiverGauge(LiveDevice):
    """USGS NWIS instantaneous values — discharge + gage height (metric)."""

    model = "GAIA-RIVER (USGS NWIS)"
    fields = {
        "discharge_m3s": "m3/s",
        "gage_height_m": "m",
    }
    source = (
        "https://waterservices.usgs.gov "
        "(USGS National Water Information System; U.S. Government public domain)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        usgs_site: str = "01646500",
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        if not _SAFE_USGS_SITE.match(usgs_site):
            raise ValueError(f"invalid USGS site id: {usgs_site!r}")
        self.usgs_site = usgs_site
        self.url = (
            "https://waterservices.usgs.gov/nwis/iv/"
            f"?format=json&sites={usgs_site}&parameterCd=00060,00065&siteStatus=all"
        )

    def map(self, payload: Any) -> dict[str, float | None]:
        series = (((payload or {}).get("value") or {}).get("timeSeries")) or []
        out: dict[str, float | None] = {"discharge_m3s": None, "gage_height_m": None}
        for row in series:
            if not isinstance(row, dict):
                continue
            codes = ((row.get("variable") or {}).get("variableCode")) or []
            code = ""
            if codes and isinstance(codes[0], dict):
                code = str(codes[0].get("value") or "")
            values = ((row.get("values") or [{}])[0].get("value")) or []
            if not values or not isinstance(values[-1], dict):
                continue
            raw = _num(values[-1].get("value"))
            if raw is None:
                continue
            if code == "00060":  # ft³/s → m³/s
                out["discharge_m3s"] = raw * _FT3S_TO_M3S
            elif code == "00065":  # ft → m
                out["gage_height_m"] = raw * _FT_TO_M
        if out["discharge_m3s"] is None and out["gage_height_m"] is None:
            raise DeviceOffline(f"{self.device_id}: USGS NWIS has no values")
        return out


# ── NOAA NDBC buoy ─────────────────────────────────────────────────────────────


class NDBCBuoy(LiveDevice):
    """NOAA NDBC realtime buoy — wave height + SST (+ wind when present)."""

    model = "GAIA-BUOY (NOAA NDBC)"
    fields = {
        "wave_height_m": "m",
        "sst_c": "cel",
        "wind_mps": "m/s",
    }
    source = (
        "https://www.ndbc.noaa.gov "
        "(NOAA National Data Buoy Center realtime2; U.S. Government public domain)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, station: str = "44025", **kw):
        super().__init__(device_id, clock, **kw)
        if not _SAFE_NDBC.match(station):
            raise ValueError(f"invalid NDBC station id: {station!r}")
        self.station = station.lower()
        self.url = f"https://www.ndbc.noaa.gov/data/realtime2/{self.station}.txt"

    def _fetch_text(self, url: str) -> str:
        url = _assert_url_allowed(url)
        headers = {"User-Agent": _UA, **(self.headers or {})}
        try:
            resp = httpx.get(
                url, headers=headers, timeout=self.timeout, follow_redirects=False
            )
            if resp.status_code != 200:
                raise DeviceOffline(
                    f"{self.device_id}: upstream HTTP {resp.status_code}"
                )
            return resp.text
        except DeviceOffline:
            raise
        except httpx.HTTPError as exc:
            raise DeviceOffline(
                f"{self.device_id}: upstream unreachable ({type(exc).__name__})"
            ) from exc

    def sample(self) -> dict[str, float]:
        text = self._fetch_text(self.url)
        mapped = {k: v for k, v in self.map_text(text).items() if v is not None}
        if not mapped:
            raise DeviceOffline(f"{self.device_id}: NDBC has no usable fields")
        return mapped

    def map(self, payload: Any) -> dict[str, float | None]:  # pragma: no cover
        raise NotImplementedError("NDBCBuoy uses map_text()")

    def map_text(self, text: str) -> dict[str, float | None]:
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        data_lines = [ln for ln in lines if not ln.startswith("#")]
        if not data_lines:
            raise DeviceOffline(f"{self.device_id}: NDBC feed empty")
        header = None
        for ln in lines:
            if ln.startswith("#") and ("WVHT" in ln or "WSPD" in ln):
                header = ln.lstrip("#").split()
                break
        if not header:
            header = [
                "YY", "MM", "DD", "hh", "mm", "WDIR", "WSPD", "GST", "WVHT",
                "DPD", "APD", "MWD", "PRES", "ATMP", "WTMP", "DEWP", "VIS",
                "PTDY", "TIDE",
            ]
        cols = data_lines[0].split()
        by = {header[i]: cols[i] for i in range(min(len(header), len(cols)))}

        def cell(name: str) -> float | None:
            raw = by.get(name, "MM")
            if raw in ("MM", "NaN", ""):
                return None
            return _num(raw)

        return {
            "wave_height_m": cell("WVHT"),
            "sst_c": cell("WTMP"),
            "wind_mps": cell("WSPD"),
        }


# ── OpenAQ v3 (optional API key) ──────────────────────────────────────────────


class OpenAQLocation(LiveDevice):
    """OpenAQ v3 latest measurements for one location (requires free API key)."""

    model = "GAIA-AQ1 (OpenAQ v3 relay)"
    fields = {
        "pm2_5_ugm3": "ug/m3",
        "pm10_ugm3": "ug/m3",
        "co2_ppm": "ppm",
    }
    source = "https://openaq.org (OpenAQ v3 API; open data — API key required)"

    def __init__(self, device_id: str, clock: SimClock, *,
                 location_id: str, api_key: str, **kw):
        super().__init__(device_id, clock, **kw)
        if not _SAFE_OPENAQ.match(location_id):
            raise ValueError(f"invalid OpenAQ location id: {location_id!r}")
        if not api_key or len(api_key) < 8 or any(c in api_key for c in " \n\r\t"):
            raise ValueError("OpenAQ API key missing or malformed")
        self.location_id = location_id
        self.headers = {"X-API-Key": api_key, "Accept": "application/json"}
        self.url = f"https://api.openaq.org/v3/locations/{location_id}/latest"

    def map(self, payload: Any) -> dict[str, float | None]:
        # v3 latest: {"results":[{"value":…,"parameter":{"name":"pm25"}}, …]}
        results = (payload or {}).get("results") or (payload or {}).get("data") or []
        out: dict[str, float | None] = {
            "pm2_5_ugm3": None, "pm10_ugm3": None, "co2_ppm": None,
        }
        for row in results:
            if not isinstance(row, dict):
                continue
            param = row.get("parameter")
            name = ""
            if isinstance(param, dict):
                name = str(param.get("name") or param.get("id") or "").lower()
            else:
                name = str(param or row.get("parameterId") or "").lower()
            value = _num(row.get("value"))
            if value is None:
                continue
            if name in ("pm25", "pm2.5", "pm2_5"):
                out["pm2_5_ugm3"] = value
            elif name == "pm10":
                out["pm10_ugm3"] = value
            elif name in ("co2", "carbon dioxide"):
                out["co2_ppm"] = value
        return out


# ── Fleet factory ─────────────────────────────────────────────────────────────


def build_live_fleet(clock: SimClock, key_dir: str = "data/devices") -> Fleet:
    """Register live relays. Identifiers come from env; hosts are allowlisted.

    Env (all optional — sensible public defaults where possible)::

        GAIA_NWS_STATION              NWS station (default KNYC)
        GAIA_OSM_BOX_ID               openSenseMap box id
        GAIA_STA_BASE_URL / _DATASTREAM / GAIA_STA_ENABLED (default 1)
        GAIA_OM_LAT / GAIA_OM_LON     Open-Meteo weather+AQ coords (default Berlin)
        GAIA_OM_WEATHER_ENABLED       default 1
        GAIA_OM_AQ_ENABLED            default 1
        GAIA_OM_MESH_ENABLED          global city mesh (default 1)
        GAIA_UK_CARBON_ENABLED        default 1
        GAIA_USGS_QUAKE_ENABLED       default 1
        GAIA_NOAA_TIDE_STATION        CO-OPS station (default 8518750 Battery NYC)
        GAIA_NOAA_TIDE_ENABLED        default 1
        GAIA_USGS_RIVER_SITE          NWIS site (default 01646500 Potomac Little Falls)
        GAIA_USGS_RIVER_ENABLED       default 1
        GAIA_NDBC_STATION             buoy id (default 44025 NY Bight)
        GAIA_NDBC_ENABLED             default 1
        GAIA_OM_MARINE_LAT/LON        Open-Meteo Marine coords (default NYC harbor)
        GAIA_OM_MARINE_ENABLED        default 1
        GAIA_OPENAQ_API_KEY           required to enable OpenAQ
        GAIA_OPENAQ_LOCATION_ID       default 2178 (when key present)
        GAIA_FIRMS_ENABLED            NASA FIRMS fire (default 1)
        GAIA_FIRMS_MAP_KEY            optional FIRMS map key (else keyless CSV)
        GAIA_SAFECAST_ENABLED         Safecast radiation CC0 (default 1)
        GAIA_SAFECAST_LAT/LON         Safecast query anchor (default Fukushima)
        GAIA_SAFECAST_MAX_AGE_DAYS    Recency window; 0 = archive / no captured_after
        GAIA_SAFECAST_MAX_PAGES       Page budget (default 5 recent / 40 archive)
        GAIA_CYBERNEWS_ENABLED        CyberNews GNSS CC BY (default 1)
        GAIA_FEEDER_ENABLED           own edge ADS-B/AIS feeders (default 0)
        GAIA_FEEDER_TOKEN             Bearer token for POST /feeder/v1/ingest
    """
    from gaia.devices.om_mesh import OM_MESH_CITIES

    fleet = Fleet()

    station = _env("GAIA_NWS_STATION", "KNYC")
    box_id = _env("GAIA_OSM_BOX_ID", "5fcc05a9fab469001c59ebd8")
    sta_base = _env("GAIA_STA_BASE_URL", _STA_DEFAULT_BASE)
    sta_ds = _env("GAIA_STA_DATASTREAM", "1")
    om_lat, om_lon = _lat_lon(
        _env("GAIA_OM_LAT"), _env("GAIA_OM_LON"),
        default_lat=52.52, default_lon=13.41,
    )
    tide_station = _env("GAIA_NOAA_TIDE_STATION", "8518750")
    usgs_river = _env("GAIA_USGS_RIVER_SITE", "01646500")
    ndbc_station = _env("GAIA_NDBC_STATION", "44025")
    om_marine_lat, om_marine_lon = _lat_lon(
        _env("GAIA_OM_MARINE_LAT"), _env("GAIA_OM_MARINE_LON"),
        default_lat=40.70, default_lon=-74.01,
    )

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    fleet.add(NWSStation("nws-01", clock, station=station,
                         site="live-weather", key_dir=key_dir))
    fleet.add(OpenSenseMapBox("osm-01", clock, box_id=box_id,
                              site="live-air", key_dir=key_dir))
    if enabled("GAIA_STA_ENABLED", "1"):
        fleet.add(SensorThingsDatastream(
            "sta-01", clock, base_url=sta_base,
            datastreams={sta_ds: "pm2_5_ugm3"},
            site="live-air", key_dir=key_dir,
        ))

    if enabled("GAIA_OM_WEATHER_ENABLED", "1"):
        fleet.add(OpenMeteoWeather(
            "om-wx-01", clock, latitude=om_lat, longitude=om_lon,
            site="live-weather-eu", key_dir=key_dir,
        ))
    if enabled("GAIA_OM_AQ_ENABLED", "1"):
        fleet.add(OpenMeteoAirQuality(
            "om-aq-01", clock, latitude=om_lat, longitude=om_lon,
            site="live-air-eu", key_dir=key_dir,
        ))

    # Global Open-Meteo mesh — Ottawa, New Delhi, Tokyo, … (operator anchors).
    if enabled("GAIA_OM_MESH_ENABLED", "1"):
        wx_on = enabled("GAIA_OM_WEATHER_ENABLED", "1")
        aq_on = enabled("GAIA_OM_AQ_ENABLED", "1")
        for city in OM_MESH_CITIES:
            slug = city["slug"]
            lat = float(city["lat"])
            lon = float(city["lon"])
            site = f"live-om-{slug}"
            if wx_on:
                fleet.add(OpenMeteoWeather(
                    f"om-wx-{slug}", clock, latitude=lat, longitude=lon,
                    site=site, key_dir=key_dir,
                ))
            if aq_on:
                fleet.add(OpenMeteoAirQuality(
                    f"om-aq-{slug}", clock, latitude=lat, longitude=lon,
                    site=site, key_dir=key_dir,
                ))

    if enabled("GAIA_UK_CARBON_ENABLED", "1"):
        fleet.add(UKCarbonIntensity(
            "uk-grid-01", clock, site="live-grid-uk", key_dir=key_dir,
        ))
    if enabled("GAIA_USGS_QUAKE_ENABLED", "1"):
        fleet.add(USGSEarthquake(
            "usgs-quake-01", clock, site="live-quake", key_dir=key_dir,
        ))
    if enabled("GAIA_NOAA_TIDE_ENABLED", "1"):
        fleet.add(NOAATideStation(
            "noaa-tide-01", clock, station=tide_station,
            site="live-tide", key_dir=key_dir,
        ))
    if enabled("GAIA_USGS_RIVER_ENABLED", "1"):
        fleet.add(USGSRiverGauge(
            "usgs-river-01", clock, usgs_site=usgs_river,
            site="live-river", key_dir=key_dir,
        ))
    if enabled("GAIA_NDBC_ENABLED", "1"):
        fleet.add(NDBCBuoy(
            "ndbc-01", clock, station=ndbc_station,
            site="live-buoy", key_dir=key_dir,
        ))
    if enabled("GAIA_OM_MARINE_ENABLED", "1"):
        fleet.add(OpenMeteoMarine(
            "om-marine-01", clock,
            latitude=om_marine_lat, longitude=om_marine_lon,
            site="live-marine", key_dir=key_dir,
        ))

    openaq_key = _env("GAIA_OPENAQ_API_KEY")
    if openaq_key:
        loc = _env("GAIA_OPENAQ_LOCATION_ID", "2178")
        try:
            fleet.add(OpenAQLocation(
                "openaq-01", clock, location_id=loc, api_key=openaq_key,
                site="live-air-openaq", key_dir=key_dir,
            ))
        except ValueError as exc:
            log.warning("OpenAQ relay skipped: %s", exc)
    else:
        log.info("OpenAQ relay skipped (set GAIA_OPENAQ_API_KEY to enable)")

    # Free-to-commercialize open relays (FIRMS / Safecast / CyberNews).
    from gaia.devices.live_open import register_open_relays

    n_open = register_open_relays(fleet, clock, key_dir=key_dir)
    if n_open:
        log.info("Registered %s free-to-commercialize LIVE relay(s)", n_open)

    from gaia.devices.live_p0 import register_p0_relays

    n_p0 = register_p0_relays(fleet, clock, key_dir=key_dir)
    if n_p0:
        log.info("Registered %s P0 LIVE relay(s)", n_p0)

    # A separate evidence class from interference events: EPN station/data-path
    # observations are never silently relabelled as RF jamming.
    from gaia.devices.gnss import register_gnss_relays

    n_gnss = register_gnss_relays(fleet, clock, key_dir=key_dir)
    if n_gnss:
        log.info("Registered %s commercially-approved GNSS integrity relay(s)", n_gnss)

    from gaia.devices.live_p1 import register_p1_relays

    n_p1 = register_p1_relays(fleet, clock, key_dir=key_dir)
    if n_p1:
        log.info("Registered %s P1 LIVE relay(s)", n_p1)

    from gaia.devices.live_p2 import register_p2_relays

    n_p2 = register_p2_relays(fleet, clock, key_dir=key_dir)
    if n_p2:
        log.info("Registered %s P2 LIVE relay(s)", n_p2)

    from gaia.devices.live_p3 import register_p3_relays

    n_p3 = register_p3_relays(fleet, clock, key_dir=key_dir)
    if n_p3:
        log.info("Registered %s P3 LIVE relay(s)", n_p3)

    # Own-edge ADS-B / AIS / IoT feeders (opt-in; offline until ingest push).
    from gaia.devices.feeder import register_feeders

    register_feeders(fleet, clock, key_dir=key_dir)

    # Operator extras from gaia/config/extra_sensors.yaml (one-command add).
    from gaia.devices.extra_sensors import register_live_extras

    n_extra = register_live_extras(fleet, clock, key_dir=key_dir)
    if n_extra:
        log.info("Registered %s extra LIVE sensor(s) from extra_sensors.yaml", n_extra)

    return fleet


__all__ = [
    "LiveDevice",
    "NWSStation",
    "OpenSenseMapBox",
    "SensorThingsDatastream",
    "OpenMeteoWeather",
    "OpenMeteoAirQuality",
    "OpenMeteoMarine",
    "UKCarbonIntensity",
    "USGSEarthquake",
    "USGSRiverGauge",
    "NOAATideStation",
    "NDBCBuoy",
    "OpenAQLocation",
    "build_live_fleet",
    "_ALLOWED_HOSTS",
    "_assert_url_allowed",
    "_env",
]
