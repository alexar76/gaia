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

CLOCK. A LiveDevice reads the wall clock's world, so it wants a real-time clock:
construct the gateway with ``SimClock(realtime=True)`` so reading timestamps and
the verifier's rate/freshness checks line up with when the fetch actually
happened. (The frozen stepped clock is for the deterministic simulators.)

FAILURE SEMANTICS. An unreachable or erroring upstream must cost the buyer
nothing. Any ``httpx.HTTPError`` (connection failure, timeout, or a non-2xx via
``raise_for_status``) is wrapped into :class:`~gaia.devices.base.DeviceOffline`,
exactly like a dropout fault — so the gateway maps it to HTTP 503, the hub reads
that as a 502 upstream failure, and the Pay-on-Verified escrow never debits.
A flaky third-party API is an outage, not a 500 and not a charge.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline, VirtualDevice
from gaia.fleet import Fleet

# Units for the GAIA fields a live device may relay (mirrors the simulator
# field tables; every key here is also a key in plausibility.PHYSICS).
_FIELD_UNITS: dict[str, str] = {
    "temperature_c": "cel",
    "humidity_pct": "percent",
    "pressure_hpa": "hPa",
    "wind_mps": "m/s",
    "pm2_5_ugm3": "ug/m3",
    "pm10_ugm3": "ug/m3",
    "co2_ppm": "ppm",
    "voc_index": "index",
}


def _num(value: Any) -> float | None:
    """Coerce an upstream scalar to float, or None if absent/non-numeric."""
    if isinstance(value, bool):  # bool is an int subclass — never a measurement
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class LiveDevice(VirtualDevice):
    """A VirtualDevice backed by a real HTTP API instead of a simulator.

    Subclasses set ``url``/``headers``/``source`` and implement ``map()``.
    """

    model = "GAIA-LIVE"
    url: str = ""
    headers: dict[str, str] = {}
    source: str = ""
    timeout: float = 10.0

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _fetch(self, url: str) -> Any:
        """GET ``url`` and return parsed JSON, or raise DeviceOffline.

        Every httpx-level failure — connect error, timeout, or a non-2xx status
        surfaced by ``raise_for_status`` — becomes DeviceOffline so an upstream
        outage is billed like a dropout (503 → hub 502 → no debit), never a 500.
        """
        try:
            resp = httpx.get(url, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise DeviceOffline(
                f"{self.device_id}: upstream unreachable ({type(exc).__name__})"
            ) from exc

    # ── Mapping contract ───────────────────────────────────────────────────────

    def map(self, payload: Any) -> dict[str, float | None]:  # pragma: no cover - abstract
        """Map an upstream JSON payload onto GAIA fields (float or None each)."""
        raise NotImplementedError

    def sample(self) -> dict[str, float]:
        payload = self._fetch(self.url)
        # A null/absent upstream field is simply absent from the reading — never
        # a NaN or a fabricated zero.
        return {k: v for k, v in self.map(payload).items() if v is not None}


# ── National Weather Service (api.weather.gov) ───────────────────────────────


class NWSStation(LiveDevice):
    """US National Weather Service latest-observation relay.

    NWS/NOAA observations are a U.S. Government work in the public domain. The
    endpoint REQUIRES a self-identifying User-Agent (contact string) per its
    terms; anonymous requests are refused.
    """

    model = "GAIA-WS1 (NWS relay)"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
    }
    headers = {"User-Agent": "GAIA-oracle/0.1 (+https://iot.modelmarket.dev; contact@modelmarket.dev)"}
    source = "https://api.weather.gov (NOAA/NWS observations; U.S. Government public domain)"

    def __init__(self, device_id: str, clock: SimClock, *, station: str = "KNYC", **kw):
        super().__init__(device_id, clock, **kw)
        self.station = station
        self.url = f"https://api.weather.gov/stations/{station}/observations/latest"

    def map(self, payload: Any) -> dict[str, float | None]:
        props = (payload or {}).get("properties") or {}

        def field(key: str) -> float | None:
            # Each nested measurement is {"unitCode": ..., "value": <num|null>};
            # value is null between reports → the mapped field is dropped.
            return _num((props.get(key) or {}).get("value"))

        temp = field("temperature")           # already degC
        humidity = field("relativeHumidity")  # already percent
        pa = field("barometricPressure")      # pascals
        kmh = field("windSpeed")              # km/h
        return {
            "temperature_c": temp,
            "humidity_pct": humidity,
            "pressure_hpa": pa / 100.0 if pa is not None else None,   # Pa → hPa
            "wind_mps": kmh / 3.6 if kmh is not None else None,       # km/h → m/s
        }


# ── openSenseMap (api.opensensemap.org) ──────────────────────────────────────


# Ordered free-text needles → GAIA field. A box's sensors are self-described by
# free-text title/unit, so we match case-insensitively by substring. Order
# matters only in that pm2.5 and pm10 are mutually exclusive by construction.
_OSM_MATCH: tuple[tuple[tuple[str, ...], str], ...] = (
    (("pm2.5", "pm2_5", "pm25"), "pm2_5_ugm3"),
    (("pm10",), "pm10_ugm3"),
    (("co2", "carbon dioxide", "kohlendioxid"), "co2_ppm"),
    (("voc",), "voc_index"),
)


class OpenSenseMapBox(LiveDevice):
    """openSenseMap sensor-box relay.

    openSenseMap is a citizen-science platform; each box carries its own licence
    (commonly CC BY-SA 4.0 or the Public Domain Dedication and Licence). Sensors
    are self-described free-text, so fields are matched by title/unit substring.
    """

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
                continue  # sensor present but never reported — skip, don't invent
            value = _num(meas.get("value"))
            if value is not None:
                out[gaia_field] = value
        return out


# ── OGC SensorThings API (the standards hero) ────────────────────────────────


_STA_DEFAULT_BASE = "https://airquality-frost.k8s.ilt-dmz.iosb.fraunhofer.de/v1.1"


class SensorThingsDatastream(LiveDevice):
    """OGC SensorThings API v1.1 relay — one reading per configured Datastream.

    SensorThings is the OGC standard for IoT observations; a Datastream is a
    time series of Observations. This device is configured with a mapping of
    ``datastream_id -> gaia_field`` and, per read, fetches the latest Observation
    of each datastream and relays its ``result`` as that GAIA field. Licence and
    attribution are whatever the server operator publishes.
    """

    model = "GAIA-STA (OGC SensorThings relay)"

    def __init__(self, device_id: str, clock: SimClock, *,
                 datastreams: dict[str, str], base_url: str = _STA_DEFAULT_BASE, **kw):
        super().__init__(device_id, clock, **kw)
        self.base_url = base_url.rstrip("/")
        self.datastreams = dict(datastreams)  # datastream_id -> gaia_field
        # Fields advertised are exactly the mapped GAIA fields (schema + WoT).
        self.fields = {f: _FIELD_UNITS.get(f, "") for f in self.datastreams.values()}
        self.source = f"OGC SensorThings API v1.1 ({self.base_url}; licence per server operator)"
        self.url = self.base_url  # informational; sample() fetches per datastream

    @staticmethod
    def _latest_result(payload: Any) -> float | None:
        """OGC Observations collection → the first (newest) result value."""
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


# ── Fleet factory ─────────────────────────────────────────────────────────────


def build_live_fleet(clock: SimClock, key_dir: str = "data/devices") -> Fleet:
    """A fleet of live relay devices for tests and a future live-main.

    Real, verified API HOSTS. The specific station / box / datastream IDENTIFIERS
    are deployment config: ``KNYC`` is a real NWS station; the openSenseMap box id
    and the SensorThings datastream ids default to placeholders that must be set
    to real resources (via the env vars below) before a live run — until then a
    read simply 404s into DeviceOffline, which costs the buyer nothing.

        GAIA_NWS_STATION       NWS station id                 (default KNYC)
        GAIA_OSM_BOX_ID        openSenseMap box id            (default placeholder)
        GAIA_STA_BASE_URL      SensorThings service base      (default Fraunhofer IOSB)
        GAIA_STA_DATASTREAM    one datastream id → pm2_5_ugm3 (default placeholder)

    This is a standalone factory; it does NOT alter the default GatewayRuntime,
    whose fleet stays the deterministic simulators.
    """
    station = os.environ.get("GAIA_NWS_STATION", "").strip() or "KNYC"
    # Default: outdoor Berlin senseBox that historically reports PM (citizen science).
    # Override with a fresher box id if this one goes quiet — offline → 503, no debit.
    box_id = os.environ.get("GAIA_OSM_BOX_ID", "").strip() or "5fcc05a9fab469001c59ebd8"
    sta_base = os.environ.get("GAIA_STA_BASE_URL", "").strip() or _STA_DEFAULT_BASE
    # Fraunhofer IOSB air-quality FROST — datastream 1 is a live PM series.
    sta_ds = os.environ.get("GAIA_STA_DATASTREAM", "").strip() or "1"

    fleet = Fleet()
    fleet.add(NWSStation("nws-01", clock, station=station,
                         site="live-weather", key_dir=key_dir))
    fleet.add(OpenSenseMapBox("osm-01", clock, box_id=box_id,
                              site="live-air", key_dir=key_dir))
    fleet.add(SensorThingsDatastream("sta-01", clock, base_url=sta_base,
                                     datastreams={sta_ds: "pm2_5_ugm3"},
                                     site="live-air", key_dir=key_dir))
    return fleet


__all__ = [
    "LiveDevice",
    "NWSStation",
    "OpenSenseMapBox",
    "SensorThingsDatastream",
    "build_live_fleet",
]
