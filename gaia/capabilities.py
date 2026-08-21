"""GAIA runtime + AIMarket capability spec.

The demo fleet: two co-located weather stations (shared site truth — the
sibling check needs a twin), one air-quality node, one energy meter. Four
priced capabilities plus a free fleet status:

    gaia.weather.read@v1  $0.001   one attested reading (ws-01/ws-02)
    gaia.air.read@v1      $0.001   one attested reading (aq-01)
    gaia.energy.read@v1   $0.001   one attested reading (em-01)
    gaia.window@v1        $0.05    bundle of N readings in one invoke — the
                                   micro-billing pattern: the hub ledger bills
                                   whole cents (ceil), so sub-cent readings are
                                   sold in bundles that clear both the 1¢
                                   quantum and the Pay-on-Verified price floor
    gaia.verify@v1        $0.002   plausibility verdict as a sellable good
                                   (same math the /v1/verify endpoint serves)
    gaia.fleet.status@v1  free     device registry incl. pinned device pubkeys
"""

from __future__ import annotations

import os
from typing import Any

from oracle_core import Capability, OracleSpec

from gaia.clock import SimClock
from gaia.devices import AirQualitySim, EnergyMeterSim, SiteWeather, WeatherStationSim
from gaia.fleet import Fleet
from gaia.plausibility import PlausibilityVerifier
from gaia.verifier import VerifierService


class GatewayRuntime:
    """Everything the handlers close over: clock, fleet, verifier."""

    def __init__(
        self,
        *,
        key_dir: str = "data/devices",
        seed: int = 0,
        start_epoch: float = 1_767_225_600.0,
        tick_s: float = 60.0,
        autotick: bool = True,
    ):
        live = os.environ.get("GAIA_ENABLE_LIVE", "").strip().lower() in ("1", "true", "yes", "on")
        # Live relays stamp wall-clock fetch time; frozen sim-time would fail
        # freshness / rate checks against real upstream observations.
        self.clock = SimClock(start_epoch, realtime=live)
        self.tick_s = tick_s
        self.autotick = autotick
        self.fleet = Fleet()

        site = SiteWeather(self.clock, seed=seed)
        self.fleet.add(WeatherStationSim("ws-01", self.clock, site, site="demo-site-1",
                                         seed=seed, key_dir=key_dir))
        self.fleet.add(WeatherStationSim("ws-02", self.clock, site, site="demo-site-1",
                                         seed=seed + 1, key_dir=key_dir))
        self.fleet.add(AirQualitySim("aq-01", self.clock, site="demo-site-1",
                                     seed=seed + 2, key_dir=key_dir))
        self.fleet.add(EnergyMeterSim("em-01", self.clock, site="demo-site-1",
                                      seed=seed + 3, key_dir=key_dir))

        # Operator SIM extras from gaia/config/extra_sensors.yaml
        from gaia.devices.extra_sensors import register_sim_extras

        register_sim_extras(self.fleet, self.clock, key_dir=key_dir, seed=seed + 10)

        # Optional LIVE relays alongside the simulators (opt-in via GAIA_ENABLE_LIVE).
        # Each read hits a real public API (NWS, Open-Meteo, UK carbon, USGS, NOAA
        # tides, openSenseMap, SensorThings, optional OpenAQ) through the same
        # Ed25519 + plausibility path. Hosts are allowlisted (SSRF). Off by default
        # for deterministic tests; public demo sets GAIA_ENABLE_LIVE=1.
        if live:
            from gaia.devices.live import build_live_fleet

            for _dev in build_live_fleet(self.clock, key_dir=key_dir).devices():
                self.fleet.add(_dev)

        self.verifier = PlausibilityVerifier(self.fleet)
        self.service = VerifierService(self.verifier)

    def read(self, device_id: str) -> dict[str, Any]:
        """One reading; in autotick mode simulated time advances per read so
        consecutive reads see a moving world (like polling real hardware)."""
        if self.autotick:
            self.clock.advance(self.tick_s)
        return self.fleet.read(device_id)

    def warm_up(self, readings_per_device: int = 40) -> None:
        """Build enough history for z-scores/siblings before selling verdicts."""
        from gaia.devices.live import LiveDevice

        for _ in range(readings_per_device):
            self.clock.advance(self.tick_s)
            for device in self.fleet.devices():
                # Live relays build history from real reads over time — never hammer
                # a real public API with synthetic warm-up traffic.
                if isinstance(device, LiveDevice):
                    continue
                if device.fault.kind != "dropout":
                    self.fleet.read(device.device_id)


# ── Handlers ──────────────────────────────────────────────────────────────────


def _read_handler(runtime: GatewayRuntime, default_device: str):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        device_id = str(data.get("device_id") or default_device)
        return runtime.read(device_id)  # ValueError (unknown) -> {ok:false}
    return handler


def _fire_read_handler(runtime: GatewayRuntime, default_device: str):
    """Fire SKU: device_id + optional bbox + packetized hotspots (cursor resume)."""

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        from gaia.devices.hotspot_pages import CursorError, clamp_page_size
        from gaia.devices.live_open import (
            FirmsFireHotspot,
            _clamp_collect_total,
            _parse_bbox,
        )

        device_id = str(data.get("device_id") or default_device)
        device = runtime.fleet.get(device_id)
        if not isinstance(device, FirmsFireHotspot):
            return runtime.read(device_id)

        raw = data if isinstance(data, dict) else {}
        cursor = str(raw.get("cursor") or "").strip() or None
        page_size = (
            clamp_page_size(raw.get("page_size"))
            if raw.get("page_size") is not None
            else None
        )

        # Resume path — no upstream re-fetch; same cursor is idempotent.
        if cursor:
            try:
                return device.read_page_from_cursor(cursor, page_size=page_size)
            except CursorError as exc:
                raise ValueError(str(exc)) from exc

        bbox = _parse_bbox(raw)
        # Collect ceiling for the ranked session (may span many pages).
        # ``max_total`` preferred; ``limit`` kept for back-compat (= collect max).
        # Explicit ``max_total: null`` must still fall back to ``limit``.
        collect_raw = raw.get("max_total")
        if collect_raw is None:
            collect_raw = raw.get("limit")
        collect_max = (
            _clamp_collect_total(collect_raw) if collect_raw is not None else None
        )

        # Gate the whole set→read→clear window: handlers run concurrently
        # (asyncio.to_thread), and interleaved set_query would hand buyer A a
        # reading filtered by buyer B's bbox.
        with device.query_gate:
            device.set_query(
                bbox=bbox, collect_max=collect_max, page_size=page_size, limit=None
            )
            try:
                return runtime.read(device_id)
            finally:
                device.clear_query()

    return handler


def _argo_read_handler(runtime: GatewayRuntime, default_device: str):
    """Argo SKU: global active-float directory or one addressed WMO profile."""

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        from gaia.devices.live_p0 import ArgoFloat

        raw = data if isinstance(data, dict) else {}
        device_id = str(raw.get("device_id") or default_device)
        wmo = str(raw.get("wmo") or "").strip()
        # Friendly virtual address for agents; the canonical input remains
        # {device_id: argo-01, wmo: 690...} and uses one pinned relay key.
        if not wmo and device_id.startswith("argo-wmo-"):
            wmo = device_id.removeprefix("argo-wmo-")
            device_id = default_device
        if device_id != default_device:
            return runtime.read(device_id)
        device = runtime.fleet.get(default_device)
        if not isinstance(device, ArgoFloat) or not wmo:
            return runtime.read(default_device)
        # Keep set → read → clear atomic across concurrent paid invokes.
        with device.query_gate:
            device.set_wmo(wmo)
            try:
                return runtime.read(default_device)
            finally:
                device.clear_wmo()

    return handler


def _gnss_integrity_handler(runtime: GatewayRuntime, default_device: str):
    """GNSS SKU: official network directory or one exact station id."""

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        from gaia.devices.gnss import EurefGnssIntegrity, GaGnssInventory

        raw = data if isinstance(data, dict) else {}
        device_id = str(raw.get("device_id") or default_device)
        station_id = str(raw.get("station_id") or "").strip().upper()
        if not station_id and device_id.startswith("gnss-station:euref:"):
            station_id = device_id.removeprefix("gnss-station:euref:")
            device_id = "gnss-euref-01"
        elif not station_id and device_id.startswith("gnss-station:ga:"):
            station_id = device_id.removeprefix("gnss-station:ga:")
            device_id = "gnss-ga-01"
        device = runtime.fleet.get(device_id)
        if not isinstance(device, (EurefGnssIntegrity, GaGnssInventory)) or not station_id:
            return runtime.read(device_id)
        with device.query_gate:
            device.set_station(station_id)
            try:
                return runtime.read(device_id)
            finally:
                device.clear_station()

    return handler


def _window_handler(runtime: GatewayRuntime):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        device_id = str(data.get("device_id") or "ws-01")
        n = int(data.get("n") or 10)
        if not 1 <= n <= 500:
            raise ValueError("n must be in [1, 500]")
        readings = [runtime.read(device_id) for _ in range(n)]
        return {"device_id": device_id, "count": n, "readings": readings}
    return handler


def _verify_handler(runtime: GatewayRuntime):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        reading = data.get("reading")
        if not isinstance(reading, dict):
            raise ValueError("input must carry a 'reading' object")
        attestation = data.get("attestation") if isinstance(data.get("attestation"), dict) else None
        min_score = data.get("min_verify_score")
        verdict = runtime.verifier.check(
            reading, attestation,
            min_score=float(min_score) if min_score is not None else None,
        )
        return verdict.to_dict()
    return handler


def _status_handler(runtime: GatewayRuntime):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        return runtime.fleet.status()
    return handler


# ── Spec assembly ─────────────────────────────────────────────────────────────

_READING_OUT = {
    "type": "object",
    "properties": {
        "reading": {"type": "object", "description": "device_id/model/site/seq/ts/values/units"},
        "attestation": {"type": "object", "description": "Ed25519 device signature over the reading canonical"},
    },
}

_DEVICE_IN = {
    "type": "object",
    "properties": {"device_id": {"type": "string", "description": "fleet device id"}},
}

_ARGO_IN = {
    "type": "object",
    "properties": {
        "device_id": {
            "type": "string",
            "description": "relay id (default argo-01; argo-wmo-{WMO} also accepted)",
        },
        "wmo": {
            "type": "string",
            "pattern": "^[0-9]{5,8}$",
            "description": (
                "optional Argo WMO platform number. Omit for the official global "
                "active-float directory; set it for that float's latest profile"
            ),
        },
    },
}

_GNSS_IN = {
    "type": "object",
    "properties": {
        "device_id": {
            "type": "string",
            "description": (
                "relay id gnss-euref-01 or gnss-ga-01; exact virtual ids "
                "gnss-station:euref:{STATION_ID} and gnss-station:ga:{STATION_ID} "
                "are also accepted"
            ),
        },
        "station_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9]{4}([A-Za-z0-9]{5})?$",
            "description": "optional source station id; omit for the selected network inventory",
        },
    },
}

_FIRE_IN = {
    "type": "object",
    "properties": {
        "device_id": {
            "type": "string",
            "description": "fleet device id (default firms-fire-01)",
        },
        "west": {"type": "number", "description": "optional bbox west (°lon)"},
        "south": {"type": "number", "description": "optional bbox south (°lat)"},
        "east": {"type": "number", "description": "optional bbox east (°lon)"},
        "north": {"type": "number", "description": "optional bbox north (°lat)"},
        "limit": {
            "type": "integer",
            "description": (
                "max hotspots to collect into the session (1–250000; default "
                "GAIA_FIRMS_COLLECT_MAX). Delivered in pages of page_size; use "
                "next_cursor to resume. Alias of max_total."
            ),
        },
        "max_total": {
            "type": "integer",
            "description": "same as limit — max ranked hotspots kept for paging",
        },
        "page_size": {
            "type": "integer",
            "description": "hotspots per packet (1–2000; default 500)",
        },
        "cursor": {
            "type": "string",
            "description": (
                "opaque continuation from previous next_cursor — idempotent retry "
                "safe; no upstream re-fetch"
            ),
        },
    },
}

_FIRE_OUT = {
    "type": "object",
    "properties": {
        "reading": {
            "type": "object",
            "description": (
                "device_id/model/site/seq/ts/values/units plus hotspot packet fields: "
                "hotspots[] (this page), hotspot_count, hotspot_total, hotspot_offset, "
                "hotspot_page_size, next_cursor (null when done), fetch_id, truncated"
            ),
        },
        "attestation": {
            "type": "object",
            "description": "Ed25519 device signature over the reading canonical (values hash)",
        },
    },
}

_GNSS_OUT = {
    "type": "object",
    "properties": {
        "reading": {
            "type": "object",
            "description": (
                "Signed network inventory or exact station reading. hotspots[] exposes stable "
                "point_id, coordinates, network, claim_class, state, cause, source_url and "
                "license; availability/latency are included only when the source publishes them."
            ),
        },
        "attestation": {
            "type": "object",
            "description": "Ed25519 relay signature over the reading canonical values hash",
        },
    },
}


def build_spec(runtime: GatewayRuntime, public_url: str | None = None) -> OracleSpec:
    url = public_url or os.environ.get("GAIA_PUBLIC_URL", "http://localhost:9320")
    product = "gaia.gateway"

    def _has(device_id: str) -> bool:
        try:
            runtime.fleet.get(device_id)
            return True
        except ValueError:
            return False

    caps = [
        Capability(
            capability_id="gaia.weather.read@v1",
            description="Live relay or demo-site weather (T/RH/P/wind), Ed25519-attested. "
                        "Prefer device_id=om-wx-01 (Open-Meteo) or nws-01 when GAIA_ENABLE_LIVE=1; "
                        "else sim ws-01/ws-02.",
            handler=_read_handler(runtime, "ws-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=15,
        ),
        Capability(
            capability_id="gaia.air.read@v1",
            description="Live relay or sim air-quality (PM2.5/PM10/CO2/VOC), Ed25519-attested. "
                        "Live device_ids: om-aq-01, osm-01, sta-01, openaq-01; sim aq-01.",
            handler=_read_handler(runtime, "aq-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=15,
        ),
        Capability(
            capability_id="gaia.energy.read@v1",
            description="One attested energy-meter reading (V/A/W + monotonic Wh register). "
                        "Sim device_id=em-01 (no public live grid meter yet).",
            handler=_read_handler(runtime, "em-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=15,
        ),
    ]
    # Live-only SKUs: only advertised when the relay device is actually registered
    # (GAIA_ENABLE_LIVE=1). Keeps the deterministic test fleet at six caps.
    if _has("uk-grid-01"):
        caps.append(Capability(
            capability_id="gaia.grid.read@v1",
            description="Live UK grid carbon-intensity relay (gCO₂/kWh) from the National Grid "
                        "ESO Carbon Intensity API. device_id=uk-grid-01. Ed25519-attested.",
            handler=_read_handler(runtime, "uk-grid-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=80,
        ))
    if _has("usgs-quake-01") or _has("emsc-01") or _has("geonet-01"):
        default_quake = (
            "usgs-quake-01" if _has("usgs-quake-01")
            else ("emsc-01" if _has("emsc-01") else "geonet-01")
        )
        caps.append(Capability(
            capability_id="gaia.quake.read@v1",
            description="Live earthquake relay — USGS GeoJSON M≥2.5, EMSC FDSN (CC BY 4.0, "
                        "cite EMSC; preliminary), and/or GeoNet NZ. Default "
                        f"device_id={default_quake}. Distinct pins; EMSC does not replace USGS.",
            handler=_read_handler(runtime, default_quake),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=120,
        ))
    from gaia.devices.live import NDBCBuoy, NOAATideStation, OpenMeteoMarine, USGSRiverGauge
    from gaia.devices.live_p1 import UhslcTide
    from gaia.devices.live_p2 import EcccHydrometric, SmhiHydrology

    tide_ids = [
        d.device_id
        for d in runtime.fleet.devices()
        if isinstance(d, (NOAATideStation, UhslcTide))
    ]
    if tide_ids:
        caps.append(Capability(
            capability_id="gaia.tide.read@v1",
            description="Live tide-gauge relay — NOAA CO-OPS (MLLW metres) and/or UHSLC "
                        f"fast-delivery. Default device_id={tide_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, tide_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=100,
        ))

    river_ids = [
        d.device_id
        for d in runtime.fleet.devices()
        if isinstance(d, (USGSRiverGauge, EcccHydrometric, SmhiHydrology))
    ]
    if river_ids:
        caps.append(Capability(
            capability_id="gaia.river.read@v1",
            description="Live river-gauge relay — USGS NWIS, ECCC hydrometric (End-use "
                        "Licence + attribution), and/or SMHI hydroobs (CC BY 4.0). Discharge "
                        f"(m³/s) + stage when the upstream publishes it. Default "
                        f"device_id={river_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, river_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=120,
        ))
    marine_ids = [
        d.device_id
        for d in runtime.fleet.devices()
        if isinstance(d, (NDBCBuoy, OpenMeteoMarine))
    ]
    if marine_ids:
        caps.append(Capability(
            capability_id="gaia.marine.read@v1",
            description="Live marine relay — wave height (m) + sea-surface temperature (°C) "
                        f"from NOAA NDBC buoy and/or Open-Meteo Marine. "
                        f"Default device_id={marine_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, marine_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=120,
        ))

    if _has("firms-fire-01"):
        caps.append(Capability(
            capability_id="gaia.fire.read@v1",
            description="Live NASA FIRMS VIIRS active-fire relay — attested headline is the "
                        "brightest non-low hotspot; reading.hotspots[] is the top-N cluster "
                        "from the same fetch (map layer). Optional buyer bbox "
                        "(west/south/east/north) + limit/max_total + page_size; resume with "
                        "cursor (idempotent). No client URLs. device_id=firms-fire-01. "
                        "Cite NASA FIRMS. Ed25519-attested.",
            handler=_fire_read_handler(runtime, "firms-fire-01"),
            product_id=product, input_schema=_FIRE_IN, output_schema=_FIRE_OUT,
            price_per_call_usd=0.002, p50_latency_ms=400,
        ))
    if _has("safecast-01"):
        caps.append(Capability(
            capability_id="gaia.radiation.read@v1",
            description="Live Safecast radiation relay — highest recent CPM near the operator "
                        "anchor (CC0). device_id=safecast-01. Ed25519-attested.",
            handler=_read_handler(runtime, "safecast-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    if _has("cybernews-jam-01"):
        caps.append(Capability(
            capability_id="gaia.jamming.read@v1",
            description="Live relay of ACTIVE/MONITORING records from the curated CyberNews "
                        "GNSS interference registry (CC BY 4.0 — attribution required). This "
                        "is source-attributed threat intelligence, not raw RF sensing or an "
                        "independently confirmed alert. device_id=cybernews-jam-01. "
                        "Ed25519-attested relay envelope.",
            handler=_read_handler(runtime, "cybernews-jam-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=150,
        ))
    if _has("gnss-euref-01") or _has("gnss-ga-01"):
        gnss_default = "gnss-euref-01" if _has("gnss-euref-01") else "gnss-ga-01"
        caps.append(Capability(
            capability_id="gaia.gnss.integrity.read@v1",
            description=(
                "Commercially-approved GNSS station inventories: EUREF EPN (CC BY 4.0) "
                "and Geoscience Australia (CC BY 3.0 AU), plus source-published EPN "
                "data-path availability/latency when present. Omit station_id for a "
                "network inventory; set device_id + station_id for one exact "
                "stations; set station_id (or use gnss-station:euref:{id}) for one exact "
                "station. Derived degradation means observation delivery degraded; cause "
                "is unestablished and this is not independent proof of RF jamming. "
                "EPN Central Bureau CC BY 4.0. Ed25519-attested relay envelope."
            ),
            handler=_gnss_integrity_handler(runtime, gnss_default),
            product_id=product, input_schema=_GNSS_IN, output_schema=_GNSS_OUT,
            price_per_call_usd=0.002, p50_latency_ms=350,
        ))

    if _has("eonet-01"):
        caps.append(Capability(
            capability_id="gaia.events.read@v1",
            description="Live NASA EONET open natural events (wildfire, volcano, storm, …). "
                        "Headline is the highest-scored geolocated event; hotspots[] is the "
                        "open-event cluster. Cite NASA EONET; no NASA endorsement. "
                        "device_id=eonet-01. Ed25519-attested.",
            handler=_read_handler(runtime, "eonet-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    if _has("swpc-01"):
        caps.append(Capability(
            capability_id="gaia.spacewx.read@v1",
            description="Live NOAA SWPC planetary Kp plus OVATION aurora grid sample "
                        "(U.S. public domain). Pin is Boulder; aurora cells are hotspots[]. "
                        "device_id=swpc-01. Ed25519-attested.",
            handler=_read_handler(runtime, "swpc-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    if _has("glm-01"):
        caps.append(Capability(
            capability_id="gaia.lightning.read@v1",
            description="Live GOES-19/18 GLM lightning flashes from NOAA Open Data Dissemination "
                        "(U.S. public domain — not Blitzortung). device_id=glm-01. "
                        "Ed25519-attested.",
            handler=_read_handler(runtime, "glm-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=400,
        ))
    if _has("nws-alerts-01"):
        caps.append(Capability(
            capability_id="gaia.alerts.read@v1",
            description="Live NWS CAP active alerts (GeoJSON centroids + WMO severity). "
                        "Free for any purpose. device_id=nws-alerts-01. Ed25519-attested.",
            handler=_read_handler(runtime, "nws-alerts-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=250,
        ))
    if _has("argo-01"):
        caps.append(Capability(
            capability_id="gaia.argo.read@v1",
            description="Official GDAC global directory of active Argo floats (last "
                        "transmission within 30 days), or latest near-surface T/S/P for "
                        "a specific WMO via input.wmo. Unrestricted; cite DOI "
                        "10.17882/42182. device_id=argo-01. Ed25519-attested.",
            handler=_argo_read_handler(runtime, "argo-01"),
            product_id=product, input_schema=_ARGO_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=600,
        ))
    if _has("usgs-geomag-01"):
        caps.append(Capability(
            capability_id="gaia.geomag.read@v1",
            description="Live USGS geomagnetic-observatory network total field F (nT): all "
                        "14 official stations, with source coordinates and observation age. "
                        "U.S. public domain — not INTERMAGNET. Default device_id="
                        "usgs-geomag-01 (BOU); select another usgs-geomag-* fleet id. "
                        "Ed25519-attested.",
            handler=_read_handler(runtime, "usgs-geomag-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    if _has("nws-flood-01") or _has("ea-flood-01"):
        default_flood = "nws-flood-01" if _has("nws-flood-01") else "ea-flood-01"
        caps.append(Capability(
            capability_id="gaia.flood.read@v1",
            description="Live flood warning products: NWS CAP (U.S. PD) and/or UK Environment "
                        "Agency OGL (England only — not SEPA/NRW). Not an in-situ river gauge. "
                        "GloFAS WMS is not scraped. Default "
                        f"device_id={default_flood}. Ed25519-attested.",
            handler=_read_handler(runtime, default_flood),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=250,
        ))
    if _has("effis-01"):
        caps.append(Capability(
            capability_id="gaia.effis.read@v1",
            description="Live Copernicus EFFIS current fires (CC BY 4.0 — cite Copernicus "
                        "EMS / JRC). device_id=effis-01. Ed25519-attested.",
            handler=_read_handler(runtime, "effis-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=300,
        ))
    if _has("usgs-volcano-01"):
        caps.append(Capability(
            capability_id="gaia.volcano.read@v1",
            description="Live USGS elevated volcanoes (alert / aviation color). U.S. public "
                        "domain. device_id=usgs-volcano-01. Ed25519-attested.",
            handler=_read_handler(runtime, "usgs-volcano-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    if _has("fintraffic-ais-01") or _has("kystverket-ais-01"):
        default_ais = "fintraffic-ais-01" if _has("fintraffic-ais-01") else "kystverket-ais-01"
        caps.append(Capability(
            capability_id="gaia.ais.public.read@v1",
            description="Live public AIS snapshot: Fintraffic Digitraffic (Finnish waters, "
                        "CC BY 4.0) and/or Kystverket via BarentsWatch (Norwegian waters, "
                        "NLOD 2.0). Geography-bound pins — not one Europe blob, not GFW, not "
                        f"own-edge gaia.ais.read@v1. Default device_id={default_ais}.",
            handler=_read_handler(runtime, default_ais),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=400,
        ))
    if _has("nws-tsunami-01") or _has("ptwc-01"):
        default_tsu = "nws-tsunami-01" if _has("nws-tsunami-01") else "ptwc-01"
        caps.append(Capability(
            capability_id="gaia.tsunami.read@v1",
            description="Live tsunami warning product: NWS CAP (U.S.) and/or PTWC Atom "
                        "(Pacific). Not a tide gauge. Empty feed → offline / no debit. "
                        f"Default device_id={default_tsu}. Ed25519-attested.",
            handler=_read_handler(runtime, default_tsu),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=250,
        ))
    if _has("nhc-cyclone-01"):
        caps.append(Capability(
            capability_id="gaia.cyclone.read@v1",
            description="Live NOAA NHC/CPHC active tropical cyclones (U.S. PD). Atlantic + "
                        "East Pacific + Central Pacific only — not JTWC, not a NW-Pacific "
                        "typhoon, not NASA EONET. Empty season → offline / no debit. "
                        "device_id=nhc-cyclone-01. Ed25519-attested.",
            handler=_read_handler(runtime, "nhc-cyclone-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    from gaia.devices.live_p3 import AdsbLolTraffic

    adsb_public_ids = [
        d.device_id for d in runtime.fleet.devices() if isinstance(d, AdsbLolTraffic)
    ]
    if adsb_public_ids:
        caps.append(Capability(
            capability_id="gaia.adsb.public.read@v1",
            description="Live ADSB.lol area snapshot (ODbL 1.0 — cite ADSB.lol; share-alike "
                        "applies to a derived public database). Operator-anchored lat/lon. "
                        "Not own-edge gaia.adsb.read@v1, not OpenSky, not ADSBx. Default "
                        f"device_id={adsb_public_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, adsb_public_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=300,
        ))

    from gaia.devices.feeder import FeederDevice

    adsb_ids = [
        d.device_id for d in runtime.fleet.devices()
        if isinstance(d, FeederDevice) and d.kind == "adsb"
    ]
    ais_ids = [
        d.device_id for d in runtime.fleet.devices()
        if isinstance(d, FeederDevice) and d.kind == "ais"
    ]
    iot_ids = [
        d.device_id for d in runtime.fleet.devices()
        if isinstance(d, FeederDevice) and d.kind == "iot"
    ]
    if adsb_ids:
        caps.append(Capability(
            capability_id="gaia.adsb.read@v1",
            description="Own-edge ADS-B feeder reading (operator dump1090 push). Offline until "
                        f"ingest. Default device_id={adsb_ids[0]}. Not a third-party aggregator. "
                        "Ed25519-attested.",
            handler=_read_handler(runtime, adsb_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=20,
        ))
    if ais_ids:
        caps.append(Capability(
            capability_id="gaia.ais.read@v1",
            description="Own-edge AIS feeder reading (operator AIS receiver push). Offline until "
                        f"ingest. Default device_id={ais_ids[0]}. Not a third-party aggregator. "
                        "Ed25519-attested.",
            handler=_read_handler(runtime, ais_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=20,
        ))
    if iot_ids:
        caps.append(Capability(
            capability_id="gaia.iot.read@v1",
            description="Own-edge IoT feeder (Tasmota / TTN / SenML-like T/RH/P/PM2.5). Offline "
                        f"until ingest. Default device_id={iot_ids[0]}. Not a third-party "
                        "aggregator. Ed25519-attested.",
            handler=_read_handler(runtime, iot_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=20,
        ))

    caps.extend([
        Capability(
            capability_id="gaia.window@v1",
            description="Bundle of N attested readings from one device in a single invoke "
                        "(micro-billing: clears the hub's 1-cent ledger quantum and the "
                        "Pay-on-Verified price floor).",
            handler=_window_handler(runtime),
            product_id=product,
            input_schema={"type": "object", "properties": {
                "device_id": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 500},
            }},
            output_schema={"type": "object", "properties": {
                "device_id": {"type": "string"}, "count": {"type": "integer"},
                "readings": {"type": "array"},
            }},
            price_per_call_usd=0.05, p50_latency_ms=60,
        ),
        Capability(
            capability_id="gaia.verify@v1",
            description="Statistical plausibility verdict over a GAIA reading "
                        "(bounds, z-score, rate, sibling agreement, attestation) — "
                        "the same math the /v1/verify escrow endpoint serves.",
            handler=_verify_handler(runtime),
            product_id=product,
            input_schema={"type": "object", "properties": {
                "reading": {"type": "object"},
                "attestation": {"type": "object"},
                "min_verify_score": {"type": "number", "minimum": 0, "maximum": 1},
            }, "required": ["reading"]},
            output_schema={"type": "object", "properties": {
                "verified": {"type": "boolean"}, "score": {"type": "number"},
                "summary": {"type": "string"}, "checks": {"type": "array"},
            }},
            price_per_call_usd=0.002, p50_latency_ms=5,
        ),
        Capability(
            capability_id="gaia.fleet.status@v1",
            description="Device registry: models, sites, pinned device pubkeys, fault state.",
            handler=_status_handler(runtime),
            product_id=product,
            output_schema={"type": "object", "properties": {
                "devices": {"type": "array"}, "count": {"type": "integer"},
            }},
            price_per_call_usd=0.0, p50_latency_ms=5,
        ),
    ])
    return OracleSpec(
        name="GAIA — physical-world oracle gateway",
        product_id=product,
        description="Virtual IoT devices (weather ×2, air quality, energy) plus live "
                    "public-API relays (Open-Meteo, NWS, UK carbon, USGS, NOAA tides/rivers/"
                    "marine, NASA FIRMS/EONET, SWPC, GOES GLM, NWS CAP, Safecast, CyberNews, "
                    "Sensor.Community, CWOP, Argo, MET Norway, USGS geomag, Copernicus EFFIS, "
                    "optional own-edge ADS-B/AIS/IoT feeders, NHC cyclones, public ADS-B) "
                    "sold as signed AIMarket capabilities, with a Metis-envelope statistical "
                    "verifier for Pay-on-Verified escrow.",
        public_url=url,
        categories=["iot", "sensors", "physical-data", "verification", "weather",
                    "air-quality", "energy", "seismic", "tides", "fire", "radiation",
                    "gnss", "traffic", "space-weather", "lightning", "alerts", "ocean",
                    "geomagnetism", "flood", "volcano", "cyclone", "adsb"],
        capabilities=caps,
        signing_key_path=os.environ.get("GAIA_SIGNING_KEY_PATH", "data/gaia_signing_key"),
        version="0.1.0",
        related=["aimarket-hub", "metis", "oracle-family"],
    )
