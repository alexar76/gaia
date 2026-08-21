"""AIMarket Protocol v2 — shared by every oracle in the family.

An oracle declares an :class:`OracleSpec` (name, product, priced capabilities with
handlers); this module turns it into a compliant ``.well-known`` doc, a signed
manifest (measured metrics overlaid), and an ``invoke`` that wraps every result in
a signed 7-field receipt + provenance. No per-oracle protocol code needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from oracle_core.metrics import Metrics
from oracle_core.signing import Signer
from oracle_core.tiers import enforce_free_tier


def utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def input_hash(input_data: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(input_data, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass
class Capability:
    capability_id: str  # e.g. "platon.random@v1"
    description: str
    handler: Callable[[dict[str, Any]], Any]  # sync or async; receives the input dict
    product_id: str = "prod-oracle"
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    price_per_call_usd: float = 0.001
    p50_latency_ms: float = 10
    success_rate_30d: float = 0.999

    # ── Cost controls, for the handful of capabilities that sell computation ─────
    # Empty/None on every capability whose work is bounded by construction, which is
    # 40 of the family's 42 — see oracle_core.tiers for why the other two need these
    # and what the numbers are.
    #
    # free_tier_max: {input field: ceiling applied when the call is NOT paid}. Over
    # the ceiling is refused with 402, never silently clamped.
    free_tier_max: dict[str, int] = field(default_factory=dict)
    # cost_ms: expected CPU milliseconds for a given input. Declared, not measured —
    # it has to be known BEFORE the work is done in order to ration it, so it is a
    # formula fitted to a benchmark (see each capability for the numbers). Only
    # relative accuracy matters; a 2x-slow machine scales every cost alike.
    cost_ms: Callable[[dict[str, Any]], float] | None = None
    # cpu_budget_ms_per_min: CPU-milliseconds one client may spend on this capability
    # per minute. Rationing *work* rather than *calls* is the whole point: a flat
    # call limit has to be either wrong for the cheap input or wrong for the expensive
    # one, since the two differ by four orders of magnitude here.
    cpu_budget_ms_per_min: float | None = None
    # global_cpu_budget_ms_per_min: the same, summed over ALL clients — i.e. the share
    # of the machine this one capability may consume. Per-IP limits are the wrong tool
    # against a distributed caller, and that is not hypothetical here: the operator's
    # own traffic analysis found a 72-address residential-proxy fleet. 60_000 is one
    # core; the whole family shares one box.
    global_cpu_budget_ms_per_min: float | None = None

    def estimate_cost_ms(self, input_data: dict[str, Any]) -> float:
        """Cost of this input in CPU-ms, or 1.0 for a capability that declares none.

        A broken or over-clever cost formula must not take the oracle down with it, so
        anything it raises degrades to the cheap default rather than to a 500. The
        floor of 1.0 keeps a budget from admitting unlimited "free" calls.
        """
        if self.cost_ms is None:
            return 1.0
        try:
            return max(1.0, float(self.cost_ms(input_data)))
        except Exception:  # noqa: BLE001 — see docstring
            return 1.0

    @property
    def name(self) -> str:
        return self.capability_id.split("@", 1)[0]

    def tool(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "capability_id": self.capability_id,
            "product_id": self.product_id,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "price_per_call_usd": self.price_per_call_usd,
            # Coerced for the same reason as the measured value in _tool_with_metrics: the
            # field is declared `integer`, and a spec author writing 10.5 would silently
            # make this oracle unfederatable.
            "p50_latency_ms": int(round(self.p50_latency_ms)),
            "success_rate_30d": self.success_rate_30d,
        }
        # Published, so a buyer can see the unpaid ceiling BEFORE spending a call to
        # discover it — the manifest is the only thing a discovering agent reads.
        # Emitted only when set, so the 40 capabilities with no cost controls keep a
        # byte-identical tool entry. `additionalProperties` is unset in
        # aimarket-protocol/schemas/manifest.json, so extra keys federate (the same
        # way `calls_observed` already does).
        if self.free_tier_max:
            out["free_tier_max"] = dict(self.free_tier_max)
        if self.cpu_budget_ms_per_min is not None:
            out["cpu_budget_ms_per_min"] = self.cpu_budget_ms_per_min
        if self.global_cpu_budget_ms_per_min is not None:
            out["global_cpu_budget_ms_per_min"] = self.global_cpu_budget_ms_per_min
        return out


@dataclass
class OracleSpec:
    name: str
    product_id: str
    description: str
    public_url: str
    categories: list[str]
    capabilities: list[Capability]
    signing_key_path: str = "data/signing_key"
    version: str = "0.1.0"
    related: list[str] = field(default_factory=list)

    def capability(self, capability_id: str) -> Capability:
        for c in self.capabilities:
            if c.capability_id == capability_id:
                return c
        raise ValueError(f"Unknown capability: {capability_id}")


class Protocol:
    """Binds an OracleSpec to a Signer + Metrics and serves the v2 surface."""

    def __init__(self, spec: OracleSpec, signer: Signer | None = None, metrics: Metrics | None = None):
        self.spec = spec
        self.signer = signer or Signer(spec.signing_key_path)
        self.metrics = metrics or Metrics()

    def well_known(self) -> dict[str, Any]:
        base = self.spec.public_url.rstrip("/")
        return {
            "name": self.spec.name,
            "protocol_versions": ["v2"],
            "hub_version": self.spec.version,
            "manifest_url": f"{base}/ai-market/v2/manifest",
            "mcp_endpoint": f"{base}/ai-market/v2/invoke",
            "capabilities_count": len(self.spec.capabilities),
            "signer_public_key": self.signer.public_key_b64,
            "description": self.spec.description,
            "categories": self.spec.categories,
            "protocol_version": "v2",
            "hub_name": self.spec.name,
            "hub_url": base,
            "ecosystem": {"product": self.spec.product_id, "related": self.spec.related},
        }

    def _tool_with_metrics(self, cap: Capability) -> dict[str, Any]:
        tool = cap.tool()
        cid = cap.capability_id
        observed = self.metrics.count(cid)
        p50 = self.metrics.p50_latency_ms(cid)
        sr = self.metrics.success_rate(cid)
        if p50 is not None:
            # int, not round(p50, 2): aimarket-protocol/schemas/manifest.json declares
            # p50_latency_ms as {"type": "integer"}, so a measured 2261.84 made the hub
            # reject the WHOLE manifest — "2261.84 is not of type 'integer'" — and index
            # zero of the family's 22 capabilities. Sub-millisecond precision in a
            # catalogue listing buys nothing; interoperating with the published schema
            # buys federation.
            tool["p50_latency_ms"] = int(round(p50))
        if sr is not None:
            tool["success_rate_30d"] = round(sr, 4)
        tool["calls_observed"] = observed
        tool["metrics_source"] = "measured" if observed else "declared"
        return tool

    def manifest(self) -> dict[str, Any]:
        tools = [self._tool_with_metrics(c) for c in self.spec.capabilities]
        body = {
            "protocol_version": "v2",
            "release_version": self.spec.version,
            "generated_at": utc_now_z(),
            "base_url": self.spec.public_url,
            "products_count": 1,
            "capabilities_count": len(tools),
            "total_capabilities": len(tools),
            "local_capabilities": len(tools),
            "federated_capabilities": 0,
            "hubs_indexed": 0,
            "tools": tools,
        }
        body["signature"] = self.signer.sign_manifest(body)
        return body

    async def invoke(
        self, capability_id: str, input_data: dict[str, Any], *, paid: bool = False
    ) -> dict[str, Any]:
        cap = self.spec.capability(capability_id)  # ValueError -> caller maps to {ok:false}
        # Default False, i.e. every caller that does not say otherwise gets the free
        # ceiling. That is deliberate: a new call site added later is bounded until
        # someone thinks about payment, rather than unbounded until someone notices.
        # Raises FreeTierExceeded BEFORE metrics start, so a refused call is not
        # recorded as a fast success and does not flatter p50 in the manifest.
        if not paid and cap.free_tier_max:
            enforce_free_tier(capability_id, cap.free_tier_max, input_data)
        start = time.perf_counter()
        success = True
        try:
            if inspect.iscoroutinefunction(cap.handler):
                output = await cap.handler(input_data)
            else:
                # Sync handlers can be CPU-bound (e.g. Chronos VDF: T sequential
                # squarings over an RSA-2048 modulus). Running them inline would
                # block the event loop and stall every other request — a trivial
                # DoS. Offload to a worker thread so one expensive call is isolated.
                output = await asyncio.to_thread(cap.handler, input_data)
                if inspect.isawaitable(output):
                    output = await output
        except Exception:
            success = False
            raise
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            self.metrics.record(capability_id, latency_ms, success)
        return self._envelope(cap, capability_id, input_data, output, latency_ms)

    def _envelope(self, cap, capability_id, input_data, output, latency_ms) -> dict[str, Any]:
        timestamp = utc_now_z()
        receipt = self.signer.sign_receipt(
            {
                "nonce": secrets.token_hex(8),
                "product_id": cap.product_id,
                "capability_id": capability_id,
                "price_usd": cap.price_per_call_usd,
                "timestamp": timestamp,
                "success": True,
                "latency_ms": round(latency_ms, 2),
            }
        )
        return {
            "capability_id": capability_id,
            "output": output,
            "price_usd": cap.price_per_call_usd,
            "provenance": {"source": self.spec.product_id, "timestamp": timestamp, "input_hash": input_hash(input_data)},
            "receipt": receipt,
        }
