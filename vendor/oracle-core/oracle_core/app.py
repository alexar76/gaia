"""FastAPI app factory — every oracle gets a compliant AIMarket v2 surface for free."""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from oracle_core.protocol import OracleSpec, Protocol
from oracle_core.ratelimit import RateLimiter


class InvokeRequest(BaseModel):
    capability_id: str
    input: dict[str, Any] = Field(default_factory=dict)


def client_key(request: Request) -> str:
    """Per-client rate-limit key — the real client IP behind the reverse proxy.

    Behind nginx the socket peer is always 127.0.0.1, so trust the proxy-set
    ``X-Real-IP`` / first ``X-Forwarded-For`` hop. This is only spoofable by a
    client that can reach the app directly; the service binds to loopback and is
    published solely through nginx, so that path is closed.
    """
    xri = (request.headers.get("x-real-ip") or "").strip()
    if xri:
        return xri
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "*"


def create_app(
    spec: OracleSpec,
    cors_origins: str = "*",
    invoke_rate_limit: int = 120,
    extra: Optional[Callable[[FastAPI, Protocol], None]] = None,
) -> FastAPI:
    proto = Protocol(spec)
    limiter = RateLimiter(invoke_rate_limit)

    app = FastAPI(title=spec.name, description=spec.description, version=spec.version)
    app.state.protocol = proto  # exposed for tests / extra routes

    origins = [o.strip() for o in cors_origins.split(",") if o.strip()] or ["*"]
    allow_all = origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=not allow_all,  # "*" + credentials is invalid per the CORS spec
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "oracle": spec.product_id, "capabilities": len(spec.capabilities)}

    @app.get("/.well-known/ai-market.json")
    async def well_known() -> dict[str, Any]:
        return proto.well_known()

    @app.get("/ai-market/v2/manifest")
    async def manifest() -> dict[str, Any]:
        return proto.manifest()

    @app.post("/ai-market/v2/invoke")
    async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
        if not limiter.allow(client_key(request)):
            raise HTTPException(status_code=429, detail="rate limited")
        try:
            result = await proto.invoke(req.capability_id, req.input)
            return {"ok": True, **result}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    if extra is not None:
        extra(app, proto)

    return app
