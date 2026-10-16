"""Actuation capabilities (gaia/actuation.py), advertised only when the operator enables them.

    gaia.actuator.list@v1  free   the operator's actuators: commands, limits, state,
                                  pinned keys, whether the kill switch is engaged
    gaia.actuate@v1        $0.05  switch one actuator on for N seconds, or off; the
                                  result is the evidence bundle (signed command, meter
                                  before and after, effect) — an unproven effect is
                                  answered ok:false and costs nothing

$0.05 is the hub's Pay-on-Verified price floor (AIMARKET_VERIFY_MIN_PRICE_USD), so a
buyer can hold the payment in escrow until GAIA's /v1/verify confirms the effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from gaia.actuation import ActuationController

ACTUATE_PRICE_USD = 0.05

ACTUATE_IN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actuator_id": {"type": "string", "description": "an id from gaia.actuator.list@v1",
                        "default": "plug-sim-01"},
        "command": {"type": "string", "enum": ["on", "off"], "default": "on"},
        "duration_s": {"type": "number", "minimum": 1, "maximum": 64800, "default": 60,
                       "description": "how long 'on' lasts; the plug switches itself off "
                                      "after it. Required for 'on'"},
    },
    "required": ["actuator_id", "command"],
}
ACTUATE_OUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actuation": {"type": "object", "description": "command record + Ed25519 attestation"},
        "before": {"type": "object", "description": "attested meter reading before"},
        "after": {"type": "object", "description": "attested meter reading after"},
        "effect": {"type": "object", "description": "expected vs observed, verified"},
        "simulated": {"type": "boolean",
                      "description": "true for a demo plug whose appliance exists only in software"},
        "auto_off_at": {"type": ["string", "null"]},
    },
}
LIST_OUT: dict[str, Any] = {
    "type": "object",
    "properties": {"actuators": {"type": "array"}, "killed": {"type": "boolean"},
                   "count": {"type": "integer"}},
}


def list_handler(controller: "ActuationController"):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        return {**controller.status(), "price_per_command_usd": ACTUATE_PRICE_USD,
                "pay_on_verified": "invoke gaia.actuate@v1 with a verify block; the hub "
                                   "holds the payment until /v1/verify confirms the effect"}
    return handler


def actuate_handler(controller: "ActuationController"):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        return controller.actuate(str(raw.get("actuator_id", "")), str(raw.get("command", "")),
                                  raw.get("duration_s"))
    return handler


__all__ = ["ACTUATE_IN", "ACTUATE_OUT", "ACTUATE_PRICE_USD", "LIST_OUT",
           "actuate_handler", "list_handler"]
