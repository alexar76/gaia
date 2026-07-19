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

        # Optional LIVE relays alongside the simulators (opt-in via GAIA_ENABLE_LIVE).
        # Each read hits a real public API (NWS weather / openSenseMap air / OGC
        # SensorThings) and goes through the same Ed25519 attestation + plausibility
        # gate. Off by default so the demo fleet stays deterministic; station/box/
        # datastream ids come from GAIA_NWS_STATION / GAIA_OSM_BOX_ID / GAIA_STA_*.
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


def build_spec(runtime: GatewayRuntime, public_url: str | None = None) -> OracleSpec:
    url = public_url or os.environ.get("GAIA_PUBLIC_URL", "http://localhost:9320")
    product = "gaia.gateway"
    caps = [
        Capability(
            capability_id="gaia.weather.read@v1",
            description="One Ed25519-attested weather reading (T/RH/P/wind) from a demo-site station.",
            handler=_read_handler(runtime, "ws-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=15,
        ),
        Capability(
            capability_id="gaia.air.read@v1",
            description="One attested air-quality reading (PM2.5/PM10/CO2/VOC).",
            handler=_read_handler(runtime, "aq-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=15,
        ),
        Capability(
            capability_id="gaia.energy.read@v1",
            description="One attested energy-meter reading (V/A/W + monotonic Wh register).",
            handler=_read_handler(runtime, "em-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=15,
        ),
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
    ]
    return OracleSpec(
        name="GAIA — physical-world oracle gateway",
        product_id=product,
        description="Virtual IoT devices (weather ×2, air quality, energy) sold as signed, "
                    "verifiable AIMarket capabilities, with a Metis-envelope-compatible "
                    "statistical verifier for Pay-on-Verified escrow.",
        public_url=url,
        categories=["iot", "sensors", "physical-data", "verification"],
        capabilities=caps,
        signing_key_path=os.environ.get("GAIA_SIGNING_KEY_PATH", "data/gaia_signing_key"),
        version="0.1.0",
        related=["aimarket-hub", "metis", "oracle-family"],
    )
