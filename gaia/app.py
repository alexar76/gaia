"""GAIA FastAPI app — oracle-core surface + verifier + WoT + sim control."""

from __future__ import annotations

import os
from typing import Any, Optional

import hmac

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from oracle_core import create_app
from oracle_core.app import client_key
from oracle_core.ratelimit import RateLimiter
from pydantic import BaseModel, Field

from gaia import __version__
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.devices.base import DeviceOffline
from gaia.middleware import ProviderSignatureMiddleware
from gaia.wot import device_to_td


def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def sim_control_enabled() -> bool:
    """Whether the /sim/* control plane is mounted.

    Fail-closed in production (AIFACTORY_PROD=1) per the ecosystem convention,
    on otherwise (this is a simulator satellite). An explicit GAIA_SIM_CONTROL
    always wins so a demo can force it on, or a hardened dev box off."""
    explicit = os.environ.get("GAIA_SIM_CONTROL", "").strip()
    if explicit:
        return _truthy(explicit)
    return os.environ.get("AIFACTORY_PROD", "").strip() != "1"

_READ_CAP_BY_TYPE = {
    "WeatherStationSim": "gaia.weather.read@v1",
    "AirQualitySim": "gaia.air.read@v1",
    "EnergyMeterSim": "gaia.energy.read@v1",
    "NWSStation": "gaia.weather.read@v1",
    "OpenSenseMapBox": "gaia.air.read@v1",
    "SensorThingsDatastream": "gaia.air.read@v1",
    "OpenMeteoWeather": "gaia.weather.read@v1",
    "OpenMeteoAirQuality": "gaia.air.read@v1",
    "UKCarbonIntensity": "gaia.grid.read@v1",
    "USGSEarthquake": "gaia.quake.read@v1",
    "NOAATideStation": "gaia.tide.read@v1",
    "OpenAQLocation": "gaia.air.read@v1",
    "USGSRiverGauge": "gaia.river.read@v1",
    "NDBCBuoy": "gaia.marine.read@v1",
    "OpenMeteoMarine": "gaia.marine.read@v1",
    "UsgsGeomag": "gaia.geomag.read@v1",
    "EcccHydrometric": "gaia.river.read@v1",
    "SmhiHydrology": "gaia.river.read@v1",
    "FmiWeather": "gaia.weather.read@v1",
    "FintrafficAis": "gaia.ais.public.read@v1",
    "KystverketAis": "gaia.ais.public.read@v1",
    "NwsTsunamiAlerts": "gaia.tsunami.read@v1",
    "PtwcTsunamiAlerts": "gaia.tsunami.read@v1",
    "NhcCyclone": "gaia.cyclone.read@v1",
    "EmscQuake": "gaia.quake.read@v1",
    "EaFloodWarnings": "gaia.flood.read@v1",
    "AdsbLolTraffic": "gaia.adsb.public.read@v1",
    "GeoNetQuake": "gaia.quake.read@v1",
    "NwsFloodAlerts": "gaia.flood.read@v1",
    "FirmsFireHotspot": "gaia.fire.read@v1",
    "SafecastRadiation": "gaia.radiation.read@v1",
    "CyberNewsJamming": "gaia.jamming.read@v1",
    "EurefGnssIntegrity": "gaia.gnss.integrity.read@v1",
    "GaGnssInventory": "gaia.gnss.integrity.read@v1",
    "FeederDevice": "gaia.adsb.read@v1",  # overridden per-kind in wot when needed
}


class VerifyRequest(BaseModel):
    """Metis-compatible verify body (route accepted for compatibility, unused)."""

    input: Any = Field(..., description="Hub-composed audit string, or {reading, attestation}.")
    route: Optional[str] = None
    min_verify_score: Optional[float] = Field(None, ge=0.0, le=1.0)


class FaultRequest(BaseModel):
    device_id: str
    kind: str = Field(..., pattern="^(none|stuck|spike|drift|dropout)$")
    fields: list[str] = Field(default_factory=list)
    magnitude: float = 0.0


class ClockRequest(BaseModel):
    advance_s: float = Field(..., gt=0, le=86_400 * 365)


class FeederIngestRequest(BaseModel):
    device_id: str = Field(..., min_length=3, max_length=64)
    fields: dict[str, float]
    observed_at: Optional[float] = Field(
        None, description="Unix epoch seconds of the observation (default: ingest time)"
    )


def _read_cap_for(device: Any) -> str:
    from gaia.devices.feeder import FeederDevice

    if isinstance(device, FeederDevice):
        return "gaia.adsb.read@v1" if device.kind == "adsb" else "gaia.ais.read@v1"
    return _READ_CAP_BY_TYPE.get(type(device).__name__, "gaia.weather.read@v1")


def build_app(runtime: GatewayRuntime | None = None,
              public_url: str | None = None) -> FastAPI:
    runtime = runtime or GatewayRuntime(
        key_dir=os.environ.get("GAIA_KEY_DIR", "data/devices"),
        tick_s=float(os.environ.get("GAIA_TICK_S", "60")),
    )
    spec = build_spec(runtime, public_url)

    # Per-client limiters for the GAIA-added routes (oracle-core only rate-limits
    # /ai-market/v2/invoke). The verify endpoint runs real (if cheap) work and the
    # trace store is finite, so cap both; sim control gets a tighter bucket.
    verify_limiter = RateLimiter(int(os.environ.get("GAIA_VERIFY_RATE_LIMIT", "120")))
    aux_limiter = RateLimiter(int(os.environ.get("GAIA_AUX_RATE_LIMIT", "240")))
    sim_limiter = RateLimiter(int(os.environ.get("GAIA_SIM_RATE_LIMIT", "30")))
    sim_token = os.environ.get("GAIA_SIM_TOKEN", "").strip()

    def _limit(limiter: RateLimiter, request: Request) -> None:
        if not limiter.allow(client_key(request)):
            raise HTTPException(status_code=429, detail="rate limited")

    def extra(app: FastAPI, proto) -> None:
        app.state.runtime = runtime

        @app.exception_handler(DeviceOffline)
        async def device_offline(request: Request, exc: DeviceOffline) -> JSONResponse:
            # 503 (a provider fault) — the hub maps provider 5xx to 502 and never
            # debits the buyer: an offline sensor costs nothing.
            return JSONResponse(status_code=503, content={"detail": str(exc)})

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {"status": "ok", "service": "gaia", "version": __version__,
                    "devices": len(runtime.fleet.devices()),
                    "sim_control": sim_control_enabled()}

        # ── Verifier (Pay-on-Verified escrow slot) ──────────────────────────

        @app.post("/v1/verify")
        async def verify(body: VerifyRequest, request: Request) -> dict[str, Any]:
            _limit(verify_limiter, request)
            return runtime.service.verify(body.input, body.min_verify_score)

        @app.get("/v1/traces/{trace_id}")
        async def trace(trace_id: str, request: Request) -> dict[str, Any]:
            _limit(aux_limiter, request)
            rec = runtime.service.trace(trace_id)
            if rec is None:
                raise HTTPException(status_code=404, detail="Trace not found")
            return rec

        # ── W3C WoT Thing Descriptions ──────────────────────────────────────

        @app.get("/wot")
        async def wot_directory(request: Request) -> dict[str, Any]:
            _limit(aux_limiter, request)
            things = [
                device_to_td(d, spec.public_url,
                             _read_cap_for(d),
                             0.001)
                for d in runtime.fleet.devices()
            ]
            return {"things": things, "count": len(things)}

        @app.get("/wot/{device_id}")
        async def wot_thing(device_id: str, request: Request) -> dict[str, Any]:
            _limit(aux_limiter, request)
            try:
                device = runtime.fleet.get(device_id)
            except ValueError:
                raise HTTPException(status_code=404, detail="Unknown device") from None
            cap = _read_cap_for(device)
            return device_to_td(device, spec.public_url, cap, 0.001)

        # ── Edge feeder ingest (own dump1090 / AIS) ─────────────────────────

        @app.post("/feeder/v1/ingest")
        async def feeder_ingest(
            body: FeederIngestRequest,
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> dict[str, Any]:
            _limit(aux_limiter, request)
            from gaia.devices.feeder import FeederDevice, feeder_token, ingest

            token = feeder_token()
            if not token:
                raise HTTPException(
                    status_code=503,
                    detail="feeder ingest requires GAIA_FEEDER_TOKEN",
                )
            expected = f"Bearer {token}"
            if not (authorization and hmac.compare_digest(authorization, expected)):
                raise HTTPException(status_code=401, detail="invalid or missing Authorization")
            feeders = {
                d.device_id: d
                for d in runtime.fleet.devices()
                if isinstance(d, FeederDevice)
            }
            if not feeders:
                raise HTTPException(
                    status_code=503,
                    detail="no feeder devices registered (set GAIA_FEEDER_ENABLED=1)",
                )
            try:
                rec = ingest(
                    body.device_id,
                    body.fields,
                    observed_at=body.observed_at,
                    allowed_devices=feeders,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            return {"ok": True, "device_id": rec["device_id"],
                    "observed_at": rec["observed_at"], "fields": rec["fields"]}

        # ── Simulation control ───────────────────────────────────────────────
        # Steerable physics is the point of a SIMULATOR gateway, but these routes
        # mutate the shared runtime (fault-inject, skew time) that also backs the
        # PAID read/verify capabilities — so they are fail-closed in production and
        # optionally shared-secret gated. A real-hardware GAIA sets AIFACTORY_PROD
        # (or GAIA_SIM_CONTROL=0) and they are never mounted.

        def _sim_guard(request: Request, x_sim_token: str | None) -> None:
            _limit(sim_limiter, request)
            if not sim_token:
                raise HTTPException(
                    status_code=503,
                    detail="sim control requires GAIA_SIM_TOKEN",
                )
            if not (x_sim_token and hmac.compare_digest(x_sim_token, sim_token)):
                raise HTTPException(status_code=401, detail="invalid or missing X-Sim-Token")

        if sim_control_enabled():
            @app.post("/sim/fault")
            async def sim_fault(body: FaultRequest, request: Request,
                                x_sim_token: str | None = Header(default=None, alias="X-Sim-Token")) -> dict[str, Any]:
                _sim_guard(request, x_sim_token)
                try:
                    device = runtime.fleet.get(body.device_id)
                except ValueError:
                    raise HTTPException(status_code=404, detail="Unknown device") from None
                if body.kind == "none":
                    device.clear_fault()
                else:
                    device.inject_fault(body.kind, fields=body.fields or None,
                                        magnitude=body.magnitude)
                return {"device_id": body.device_id, "fault": device.fault.kind}

            @app.post("/sim/clock")
            async def sim_clock(body: ClockRequest, request: Request,
                                x_sim_token: str | None = Header(default=None, alias="X-Sim-Token")) -> dict[str, Any]:
                _sim_guard(request, x_sim_token)
                runtime.clock.advance(body.advance_s)
                return {"now": runtime.clock.iso()}

    app = create_app(spec, cors_origins=os.environ.get("GAIA_CORS_ORIGINS", "*"), extra=extra)
    # Sign invoke responses with the gateway key (hub supply-security handshake).
    app.add_middleware(ProviderSignatureMiddleware, signer=app.state.protocol.signer)
    return app
