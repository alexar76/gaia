"""FastAPI app factory — every oracle gets a compliant AIMarket v2 surface for free."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from oracle_core.protocol import OracleSpec, Protocol
from oracle_core.ratelimit import RateLimiter
from oracle_core.tiers import FreeTierExceeded, PaidTierPolicy, enforce_free_tier


logger = logging.getLogger(__name__)


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
    paid_tier: Optional[PaidTierPolicy] = None,
) -> FastAPI:
    proto = Protocol(spec)
    limiter = RateLimiter(invoke_rate_limit)
    # Read from the environment by default, and empty by default there too, so an
    # oracle that says nothing about payment grants nothing — see oracle_core.tiers.
    policy = PaidTierPolicy.from_env() if paid_tier is None else paid_tier
    # Budgets in CPU-milliseconds per minute, for the capabilities that sell
    # computation. The global limiter above still applies on top and counts requests,
    # so a caller must be under BOTH: these can only ever tighten what the app was
    # configured with, never widen it.
    per_cap: dict[str, RateLimiter] = {
        c.capability_id: RateLimiter(c.cpu_budget_ms_per_min)
        for c in spec.capabilities
        if c.cpu_budget_ms_per_min is not None
    }
    # Aggregate budgets, keyed on nothing — one shared bucket per capability, i.e. the
    # share of the machine it may consume. Held in a separate map from per_cap so the
    # two stay independently expressible: a capability may want an aggregate ceiling
    # with no per-client one, or vice versa.
    per_cap_global: dict[str, RateLimiter] = {
        c.capability_id: RateLimiter(c.global_cpu_budget_ms_per_min)
        for c in spec.capabilities
        if c.global_cpu_budget_ms_per_min is not None
    }

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
    async def invoke(req: InvokeRequest, request: Request) -> Any:
        caller = client_key(request)
        if not limiter.allow(caller):
            raise HTTPException(status_code=429, detail="rate limited")
        paid = policy.is_paid(request.headers, caller)

        # The free-tier ceiling is checked BEFORE the CPU budget, and the order is load
        # bearing. A request over the ceiling is over it permanently, so answering 429
        # ("retry shortly") would send the caller into a retry loop that can never
        # succeed, while 402 tells them the actual ceiling and that payment lifts it.
        # Budget exhaustion, by contrast, really does clear on its own — so it is the
        # refusal that deserves 429, and it belongs second.
        #
        # Protocol.invoke enforces the same ceiling again. That is deliberate: this
        # layer produces the better error, and that layer keeps the invariant true for
        # every caller, including ones that never come through HTTP.
        cap = per_cap.get(req.capability_id) or per_cap_global.get(req.capability_id)
        if not paid:
            try:
                enforce_free_tier(
                    req.capability_id,
                    spec.capability(req.capability_id).free_tier_max,
                    req.input,
                )
            except ValueError:
                # Unknown capability_id — fall through so the normal handler produces
                # the "Unknown capability: …" message rather than a confusing 402.
                pass
            except FreeTierExceeded as exc:
                return JSONResponse(status_code=402, content=exc.as_body())

        cap_limiter = per_cap.get(req.capability_id)
        cap_global = per_cap_global.get(req.capability_id)
        if cap_limiter is not None or cap_global is not None:
            # Estimated before the work, because that is the only time rationing it is
            # possible. A cheap input costs almost nothing and stays freely repeatable;
            # an expensive one spends the minute in a single call.
            # Present by construction: both maps are built from spec.capabilities, so a
            # hit in either means this id exists.
            cost_ms = spec.capability(req.capability_id).estimate_cost_ms(req.input)
            # Both budgets are TESTED before either is charged. Charging the per-client
            # bucket and then refusing on capacity would debit a caller for work that
            # was never performed, and they would have no way to tell.
            if cap_limiter is not None and not cap_limiter.would_allow(caller, cost_ms):
                # Distinct message from the app-wide one: a caller well under 120/min
                # who is refused anyway would otherwise have no way to tell that this
                # capability is budgeted separately, so the error names the budget, what
                # this call would have cost, and what is left.
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"rate limited: {req.capability_id} budgets "
                        f"{cap_limiter.limit:.0f} ms of CPU per minute per client "
                        f"because its cost scales with the input; this call is "
                        f"~{cost_ms:.0f} ms and {cap_limiter.limit - cap_limiter.spent(caller):.0f} ms "
                        f"remain. Retry later, or ask for less work."
                    ),
                )
            if cap_global is not None and not cap_global.would_allow("*", cost_ms):
                # Reported AFTER the per-client budget so the message a normal caller
                # sees is about their own allowance rather than about the server's load.
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"rate limited: {req.capability_id} is at its capacity ceiling "
                        f"of {cap_global.limit:.0f} ms of CPU per minute across all "
                        f"clients — retry shortly"
                    ),
                )
            # Both budgets said yes; charge them.
            if cap_limiter is not None:
                cap_limiter.allow(caller, cost_ms)
            if cap_global is not None:
                cap_global.allow("*", cost_ms)
        try:
            result = await proto.invoke(req.capability_id, req.input, paid=paid)
            return {"ok": True, **result}
        except FreeTierExceeded as exc:
            # Normally unreachable — the check above already answered. Kept because
            # Protocol.invoke owns the invariant, and if the two ever disagree the
            # answer should still be the correct 402 rather than an uncaught 500.
            # 402, not 400: nothing about the request is malformed — the caller asked
            # for work this tier does not include. 402 is the status the rest of the
            # ecosystem already uses for that (the hub answers it for a priced invoke
            # with no channel), so a client that handles one handles this.
            return JSONResponse(status_code=402, content=exc.as_body())
        except ValueError as exc:
            # A handler rejecting its input, e.g. "points must be a list of [x, y] pairs".
            return {"ok": False, "error": str(exc)}
        except KeyError as exc:
            # A required field was simply absent. Only ValueError used to be translated, so
            # `lumen.score` without `target_node` answered a bare 500 Internal Server Error
            # — a caller that read the schema and mistyped one field learned nothing and had
            # nothing to correct. KeyError stringifies to a quoted name, hence the strip.
            return {"ok": False, "error": f"missing required input field: {str(exc).strip(chr(39))}"}
        except RuntimeError as exc:
            # A federated upstream refused. The oracle-family proxy raises this carrying the
            # upstream's own message ("Unknown capability: …"), which is exactly what the
            # caller needs and exactly what the 500 was swallowing.
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all, see below
            # Anything else is a real fault, not a refusal, so it stays a 5xx. But it says
            # what broke: an empty "Internal Server Error" is indistinguishable from a dead
            # process, and callers retry it forever. Full traceback goes to the log.
            logger.exception("invoke %s failed unexpectedly", req.capability_id)
            raise HTTPException(
                status_code=500, detail=f"{type(exc).__name__}: {exc}"
            ) from exc

    if extra is not None:
        extra(app, proto)

    return app
