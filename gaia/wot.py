"""W3C WoT Thing Description 1.1 ↔ AIMarket capability bridge.

Export: every GAIA device publishes a Thing Description whose property forms
point at the AIMarket invoke endpoint, with the price and capability id carried
in ``aimarket:*`` extension terms — a WoT consumer sees a Thing, an AIMarket
agent sees a priced capability, both are looking at the same JSON-LD document.
Once GAIA's price gate names the hubs that sell it (oracle_core.price_gate), a
priced form points at each such hub instead, with ``aimarket:product_id`` and
``aimarket:source_hub`` for the hub's invoke body: an enforcing gate answers a
direct priced call with 402, so a form at GAIA itself would offer a read nobody but
a hub could make.

Import: ``td_to_tools()`` turns any Thing Description into AIMarket tool/
capability dicts (the shape hub manifests and supply/register accept), so an
existing WoT device can be listed on a hub by translation alone — the missing
piece is only the proxy that forwards invokes to the Thing's own forms.
"""

from __future__ import annotations

from typing import Any, Sequence

from gaia.actuation_capabilities import ACTUATE_PRICE_USD

TD_CONTEXT = "https://www.w3.org/2022/wot/td/v1.1"
AIMARKET_CONTEXT = {"aimarket": "https://modelmarket.dev/ns/aimarket#"}


# ── Export: device → Thing Description ───────────────────────────────────────


def _forms(base: str, op: str, capability_id: str, input_payload: dict[str, Any],
           price: float, hubs: Sequence[str], product_id: str) -> list[dict[str, Any]]:
    """Where to make the call: each hub that sells it when it is priced and hubs are
    named, otherwise this GAIA. A free capability is never gated, so it stays here."""
    common = {
        "htv:methodName": "POST",
        "contentType": "application/json",
        "op": [op],
        "aimarket:capability_id": capability_id,
        "aimarket:input": input_payload,
        "aimarket:price_per_call_usd": price,
    }
    if price > 0 and hubs:
        return [{"href": f"{hub.rstrip('/')}/ai-market/v2/invoke", **common,
                 "aimarket:product_id": product_id, "aimarket:source_hub": base}
                for hub in hubs]
    return [{"href": f"{base}/ai-market/v2/invoke", **common}]


def device_to_td(device: Any, base_url: str, capability_id: str,
                 price_per_call_usd: float, *, hubs: Sequence[str] = (),
                 product_id: str = "gaia.gateway",
                 action_price_usd: float = ACTUATE_PRICE_USD) -> dict[str, Any]:
    """Thing Description for one GAIA device.

    ``capability_id`` is the read capability that serves every property —
    AIMarket sells device access per reading, so all properties share one form.
    ``hubs`` are the hubs GAIA's price gate trusts to sell its priced calls; with
    any named, priced forms point at them (see the module docstring).
    """
    base = base_url.rstrip("/")
    read_input = {"device_id": device.device_id}
    properties: dict[str, Any] = {}
    for fname, unit in device.fields.items():
        properties[fname] = {
            "type": "number",
            "unit": unit,
            "readOnly": True,
            "observable": False,
            "forms": _forms(base, "readproperty", capability_id, read_input,
                            price_per_call_usd, hubs, product_id),
        }
    sold = (f"bought per invoke through {', '.join(hubs)}"
            if hubs and price_per_call_usd > 0 else "billed per invoke")
    td = {
        "@context": [TD_CONTEXT, AIMARKET_CONTEXT],
        "id": f"urn:dev:gaia:{device.device_id}",
        "title": f"{device.device_id} — {device.model}",
        "description": f"GAIA physical oracle at site {device.site}; every reading is "
                       f"Ed25519-attested by the device key and {sold}.",
        "version": {"instance": device.firmware},
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": properties,
        "aimarket:price_per_call_usd": price_per_call_usd,
        "aimarket:device_pubkey": device.signer.public_key_b64,
        "aimarket:site": device.site,
    }
    if callable(getattr(device, "set_relay", None)):
        td["actions"] = _plug_actions(device.device_id, base, action_price_usd, hubs, product_id)
    return td


def _plug_actions(device_id: str, base: str, price: float, hubs: Sequence[str],
                  product_id: str) -> dict[str, Any]:
    """WoT actions for a metering plug: each one is a paid gaia.actuate@v1 invoke whose
    result is evidence of the effect (gaia/actuation.py)."""
    def forms(command: str) -> list[dict[str, Any]]:
        return _forms(base, "invokeaction", "gaia.actuate@v1",
                      {"actuator_id": device_id, "command": command}, price, hubs, product_id)
    return {
        "switchOn": {
            "title": "Switch on for a bounded time",
            "description": "Paid only for the measured effect; the plug switches itself off "
                           "after duration_s.",
            "input": {"type": "object", "required": ["duration_s"],
                      "properties": {"duration_s": {"type": "number", "minimum": 1}}},
            "safe": False, "idempotent": False, "forms": forms("on"),
        },
        "switchOff": {
            "title": "Switch off", "safe": False, "idempotent": True, "forms": forms("off"),
        },
    }


# ── Import: Thing Description → AIMarket tools ───────────────────────────────


def _prop_schema(prop: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": prop.get("type", "number")}
    for key in ("unit", "minimum", "maximum", "enum"):
        if key in prop:
            schema[key] = prop[key]
    return schema


def td_to_tools(td: dict[str, Any], *, product_id: str,
                default_price_usd: float = 0.001) -> list[dict[str, Any]]:
    """AIMarket tool dicts (manifest `tools[]` shape) from a Thing Description.

    Each property becomes ``<thing>.<property>.read@v1``; each action becomes
    ``<thing>.<action>@v1``. Prices come from ``aimarket:price_per_call_usd``
    (document-level or per-form) with a caller default. The result is what a
    hub manifest / supply-register call accepts — actually SERVING the invokes
    needs a proxy to the Thing's forms, which is deployment wiring, not schema.
    """
    thing_id = str(td.get("id", "thing")).rsplit(":", 1)[-1] or "thing"
    doc_price = float(td.get("aimarket:price_per_call_usd", default_price_usd))
    tools: list[dict[str, Any]] = []

    for pname, prop in (td.get("properties") or {}).items():
        forms = prop.get("forms") or [{}]
        price = float(forms[0].get("aimarket:price_per_call_usd", doc_price))
        tools.append({
            "name": f"{thing_id}.{pname}.read",
            "capability_id": f"{thing_id}.{pname}.read@v1",
            "product_id": product_id,
            "description": prop.get("description", f"Read property {pname!r} of {td.get('title', thing_id)}"),
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {"type": "object", "properties": {pname: _prop_schema(prop)}},
            "price_per_call_usd": price,
            "p50_latency_ms": 50,
            "success_rate_30d": 0.99,
        })

    for aname, action in (td.get("actions") or {}).items():
        forms = action.get("forms") or [{}]
        price = float(forms[0].get("aimarket:price_per_call_usd", doc_price))
        tools.append({
            "name": f"{thing_id}.{aname}",
            "capability_id": f"{thing_id}.{aname}@v1",
            "product_id": product_id,
            "description": action.get("description", f"Invoke action {aname!r} of {td.get('title', thing_id)}"),
            "input_schema": action.get("input", {"type": "object", "properties": {}}),
            "output_schema": action.get("output", {"type": "object"}),
            "price_per_call_usd": price,
            "p50_latency_ms": 200,
            "success_rate_30d": 0.99,
        })

    return tools
