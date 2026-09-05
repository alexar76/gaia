"""P2 commercially-clear LIVE relays — licence-pinned public APIs only.

* Fintraffic AIS (CC BY 4.0, Finnish waters) — **not** own-edge ``gaia.ais.read@v1``
* ECCC hydrometric realtime (MSC End-use Licence — commercial + attribution)
* FMI open weather observations (CC BY 4.0)
* NWS CAP tsunami alerts (U.S. PD) — CAP, not a tide gauge
* SMHI hydrology 15-min discharge (CC BY 4.0)

Kystverket AIS and EMSC live as P3 (``live_p3.py``). Still blocked: USGS WQ
(stale IV series), GFW / AISStream.
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
from gaia.devices.live_p0 import NwsCapAlerts, geojson_centroid, signed_cluster_read
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p2")

# No vessel makes 60 kn — that is the maritime bound gaia.plausibility already
# enforces for sog_knots. AIS encodes SOG in 1/10 kn buckets up to 102.2 ("or
# higher"), so anything above the bound is a broken encoder, not a fast ship.
# The Baltic snapshot carries a handful every time. Their POSITION is still good,
# so keep the vessel and drop only the speed.
_MAX_PLAUSIBLE_SOG = 60.0

_SAFE_HYDRO = re.compile(r"^[0-9A-Z]{5,12}$")
_SAFE_SMHI = re.compile(r"^[0-9]{1,6}$")
_SAFE_PLACE = re.compile(r"^[A-Za-z][A-Za-z0-9 ._-]{1,40}$")
_FMI_NAME_VALUE = re.compile(
    r"<BsWfs:ParameterName>([^<]+)</BsWfs:ParameterName>"
    r"\s*<BsWfs:ParameterValue>([^<]*)</BsWfs:ParameterValue>",
    re.I,
)
_FMI_POS = re.compile(r"<gml:pos>\s*([0-9.+-]+)\s+([0-9.+-]+)", re.I)


class FintrafficAis(LiveDevice):
    """Fintraffic Digitraffic AIS snapshot — Finnish waters, CC BY 4.0.

    Distinct from own-edge ``gaia.ais.read@v1`` (operator receiver). This relay
    attests custody of the public REST snapshot, not a physical AIS antenna.
    """

    model = "GAIA-AIS (Fintraffic)"
    policy_id = "fintraffic_ais"
    fields = {
        "sog_knots": "kn",
        "cog_deg": "deg",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://www.digitraffic.fi/en/terms-of-service/ "
        "(Fintraffic maritime AIS via meri.digitraffic.fi; CC BY 4.0 — commercial "
        "reuse with attribution. Finnish waters only — not global AIS, not GFW, "
        "not an own-edge receiver.)"
    )
    url = "https://meri.digitraffic.fi/api/ais/v1/locations"
    timeout = 25.0
    _default_limit = 400

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("fintraffic_ais")
        policy.require_endpoint(self.url)
        self.headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        features = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: Fintraffic AIS snapshot empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            centroid = geojson_centroid(feat.get("geometry"))
            props = feat.get("properties") or {}
            if not isinstance(props, dict):
                props = {}
            lat = lon = None
            if centroid:
                lat, lon = centroid
            if lat is None or lon is None:
                continue
            sog = _num(props.get("sog"))
            cog = _num(props.get("cog"))
            # ITU-R M.1371: SOG 102.3 kn = not available, 102.2 = "102.2 kn or
            # higher"; COG 360.0 = not available. Omit the field — a missing key
            # is honest, 0.0 would sell "stopped" and "heading due north", and
            # signed_cluster_read drops absent numerics. Implausible speeds go the
            # same way: the cluster is sorted by SOG, so without this the attested
            # headline is always the most broken transmitter in the snapshot.
            if sog is not None and sog > _MAX_PLAUSIBLE_SOG:
                sog = None
            if cog is not None and cog >= 360.0:
                cog = None
            mmsi = props.get("mmsi") or feat.get("mmsi")
            item: dict[str, Any] = {
                "latitude": float(lat),
                "longitude": float(lon),
                "mmsi": str(mmsi or "")[:16],
                "nav_stat": str(props.get("navStat") if props.get("navStat") is not None else "")[:8],
            }
            if sog is not None:
                item["sog_knots"] = float(sog)
            if cog is not None:
                item["cog_deg"] = float(cog)
            scored.append((float(item.get("sog_knots") or 0.0), item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: Fintraffic AIS had no geometry")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 2000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        # Absent = upstream said "not available"; keep it None, never 0.0.
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


class EcccHydrometric(LiveDevice):
    """ECCC MSC GeoMet hydrometric-realtime — discharge + stage (End-use Licence)."""

    model = "GAIA-RIVER (ECCC hydrometric)"
    policy_id = "eccc_hydrometric"
    fields = {
        "discharge_m3s": "m3/s",
        "gage_height_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.weather.gc.ca "
        "(ECCC MSC GeoMet hydrometric-realtime; End-use Licence — commercial use "
        "allowed with attribution to Environment and Climate Change Canada. "
        "Stage may be a geodetic water level, not a USGS-style gage datum.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str = "02HC003",
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        sta = (station or "").strip().upper()
        if not _SAFE_HYDRO.match(sta):
            raise ValueError(f"invalid ECCC hydrometric station id: {station!r}")
        self.station = sta
        self.url = (
            "https://api.weather.gc.ca/collections/hydrometric-realtime/items"
            f"?f=json&limit=1&sortby=-DATETIME&STATION_NUMBER={quote(sta)}"
        )
        policy = require_approved_source("eccc_hydrometric")
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        features = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: ECCC hydrometric empty")
        feat = features[0] if isinstance(features[0], dict) else {}
        props = feat.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        centroid = geojson_centroid(feat.get("geometry"))
        lat = lon = None
        if centroid:
            lat, lon = centroid
        discharge = _num(props.get("DISCHARGE"))
        level = _num(props.get("LEVEL"))
        if discharge is None and level is None:
            raise DeviceOffline(f"{self.device_id}: ECCC hydrometric has no values")
        return {
            "discharge_m3s": discharge,
            "gage_height_m": level,
            "latitude": lat,
            "longitude": lon,
        }


class FmiWeather(LiveDevice):
    """FMI open WFS simple observations — CC BY 4.0, no key."""

    model = "GAIA-FMI (open data)"
    policy_id = "fmi_opendata"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://en.ilmatieteenlaitos.fi/open-data-licence "
        "(Finnish Meteorological Institute open observations via opendata.fmi.fi; "
        "CC BY 4.0 — attribution: Finnish Meteorological Institute)"
    )
    timeout = 25.0

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        place: str = "Helsinki",
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        loc = (place or "").strip()
        if not _SAFE_PLACE.match(loc):
            raise ValueError(f"invalid FMI place: {place!r}")
        self.place = loc
        self.url = (
            "https://opendata.fmi.fi/wfs?service=WFS&version=2.0.0"
            "&request=getFeature&storedquery_id=fmi::observations::weather::simple"
            f"&place={quote(loc)}&maxlocations=1&timestep=60"
            "&parameters=t2m,ws_10min,rh,p_sea"
        )
        policy = require_approved_source("fmi_opendata")
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        text = payload if isinstance(payload, str) else ""
        if not text.strip():
            raise DeviceOffline(f"{self.device_id}: FMI WFS empty")
        latest: dict[str, float] = {}
        for name, raw in _FMI_NAME_VALUE.findall(text):
            val = _num(raw)
            if val is None:
                continue
            latest[name.strip()] = val
        lat = lon = None
        pos = _FMI_POS.search(text)
        if pos:
            # EPSG:4258 in this stored query is lat lon.
            lat = _num(pos.group(1))
            lon = _num(pos.group(2))
        if not latest:
            # Namespace-agnostic fallback via ElementTree.
            latest, lat, lon = _fmi_parse_etree(text, lat, lon)
        if not latest:
            raise DeviceOffline(f"{self.device_id}: FMI WFS had no parameters")
        return {
            "temperature_c": latest.get("t2m"),
            "humidity_pct": latest.get("rh"),
            "pressure_hpa": latest.get("p_sea"),
            "wind_mps": latest.get("ws_10min"),
            "latitude": lat,
            "longitude": lon,
        }

    def read(self) -> dict[str, Any]:
        text = self._fetch_text(self.url)
        mapped = self.map(text)
        honest = {k: v for k, v in mapped.items() if v is not None}
        if not honest:
            raise DeviceOffline(f"{self.device_id}: FMI mapped empty")
        values = {k: round(v, 4) for k, v in self._faulted(honest).items()}
        self._seq += 1
        reading = {
            "device_id": self.device_id,
            "model": self.model,
            "site": self.site,
            "firmware": self.firmware,
            "seq": self._seq,
            "ts": self.clock.iso(),
            "values": values,
            "units": dict(self.fields),
        }
        if self.attribution:
            reading["attribution"] = self.attribution
        from gaia.attestation import sign_reading

        attestation = sign_reading(reading, self.signer)
        self._last_values = dict(values)
        return {"reading": reading, "attestation": attestation}


def _fmi_parse_etree(
    text: str, lat: float | None, lon: float | None
) -> tuple[dict[str, float], float | None, float | None]:
    latest: dict[str, float] = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return latest, lat, lon
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "pos" and el.text and lat is None:
            parts = el.text.split()
            if len(parts) >= 2:
                lat = _num(parts[0])
                lon = _num(parts[1])
    names = [
        el.text for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "ParameterName" and el.text
    ]
    values = [
        el.text for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "ParameterValue"
    ]
    for name, raw in zip(names, values):
        val = _num(raw)
        if val is not None:
            latest[name.strip()] = val
    return latest, lat, lon


class NwsTsunamiAlerts(NwsCapAlerts):
    """NWS tsunami CAP — warning / watch / advisory. Often empty; empty → offline."""

    model = "GAIA-TSUNAMI (NWS CAP)"
    source = (
        "https://api.weather.gov/alerts "
        "(NWS tsunami CAP GeoJSON; U.S. Government public domain. "
        "This is a warning product, not a tide gauge or tsunami sensor.)"
    )
    url = (
        "https://api.weather.gov/alerts/active"
        "?status=actual&message_type=alert&code=TSW,TSA,TSY"
    )


class SmhiHydrology(LiveDevice):
    """SMHI open hydrology — 15-min discharge (CC BY 4.0)."""

    model = "GAIA-RIVER (SMHI hydroobs)"
    policy_id = "smhi_hydro"
    fields = {
        "discharge_m3s": "m3/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://opendata.smhi.se "
        "(SMHI hydrology observations; CC BY 4.0 — attribution: SMHI. "
        "15-minute discharge, not a flood forecast.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str = "2357",
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        sta = (station or "").strip()
        if not _SAFE_SMHI.match(sta):
            raise ValueError(f"invalid SMHI hydrology station id: {station!r}")
        self.station = sta
        self.url = (
            "https://opendata-download-hydroobs.smhi.se/api/version/1.0"
            f"/parameter/2/station/{sta}/period/latest-day/data.json"
        )
        policy = require_approved_source("smhi_hydro")
        policy.require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, dict):
            raise DeviceOffline(f"{self.device_id}: SMHI hydrology empty")
        values = payload.get("value")
        if not isinstance(values, list) or not values:
            raise DeviceOffline(f"{self.device_id}: SMHI hydrology has no values")
        last = values[-1] if isinstance(values[-1], dict) else {}
        discharge = _num(last.get("value"))
        if discharge is None:
            raise DeviceOffline(f"{self.device_id}: SMHI hydrology last value missing")
        lat = lon = None
        pos = payload.get("position")
        if isinstance(pos, list) and pos and isinstance(pos[0], dict):
            lat = _num(pos[0].get("latitude"))
            lon = _num(pos[0].get("longitude"))
        elif isinstance(pos, dict):
            lat = _num(pos.get("latitude"))
            lon = _num(pos.get("longitude"))
        station = payload.get("station") if isinstance(payload.get("station"), dict) else {}
        if lat is None:
            lat = _num(station.get("latitude"))
            lon = _num(station.get("longitude"))
        return {
            "discharge_m3s": discharge,
            "latitude": lat,
            "longitude": lon,
        }


def register_p2_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P2 commercially-clear relays. Returns count."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_FINTRAFFIC_AIS_ENABLED", "1"):
        fleet.add(
            FintrafficAis(
                "fintraffic-ais-01", clock, site="live-ais-finland", key_dir=key_dir
            )
        )
        n += 1
    if enabled("GAIA_ECCC_HYDRO_ENABLED", "1"):
        try:
            fleet.add(
                EcccHydrometric(
                    "eccc-hydro-01",
                    clock,
                    station=_env("GAIA_ECCC_HYDRO_STATION", "02HC003"),
                    site="live-river-eccc",
                    key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("ECCC hydrometric skipped: %s", exc)
    if enabled("GAIA_FMI_ENABLED", "1"):
        try:
            fleet.add(
                FmiWeather(
                    "fmi-01",
                    clock,
                    place=_env("GAIA_FMI_PLACE", "Helsinki"),
                    site="live-weather-fmi",
                    key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("FMI weather skipped: %s", exc)
    if enabled("GAIA_TSUNAMI_ENABLED", "1"):
        fleet.add(
            NwsTsunamiAlerts(
                "nws-tsunami-01", clock, site="live-tsunami", key_dir=key_dir
            )
        )
        n += 1
    if enabled("GAIA_SMHI_HYDRO_ENABLED", "1"):
        try:
            fleet.add(
                SmhiHydrology(
                    "smhi-hydro-01",
                    clock,
                    station=_env("GAIA_SMHI_STATION", "2357"),
                    site="live-river-smhi",
                    key_dir=key_dir,
                )
            )
            n += 1
        except ValueError as exc:
            log.warning("SMHI hydrology skipped: %s", exc)
    return n


__all__ = [
    "FintrafficAis",
    "EcccHydrometric",
    "FmiWeather",
    "NwsTsunamiAlerts",
    "SmhiHydrology",
    "register_p2_relays",
]
