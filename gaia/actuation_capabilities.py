"""Actuation capabilities (gaia/actuation.py), advertised only when the operator enables them.

    gaia.actuator.list@v1      free   the operator's actuators: commands, limits, state,
                                      access, whether consumption is for sale, kill switch
    gaia.actuate@v1            $0.05  switch one actuator on for N seconds, or off; the
                                      result is the evidence bundle (signed command, meter
                                      before and after, effect) — an unproven effect is
                                      answered ok:false and costs nothing
    gaia.owner.command@v1      free   the owner switches their own plug with an EIP-712
                                      signature of their wallet (gaia/owner_auth.py)
    gaia.owner.consumption@v1  free   the owner reads their own plug's consumption
    gaia.consumption.read@v1   $0.01  anyone reads a plug's consumption — only where its owner
                                      put it on sale, no finer and no further back than allowed

$0.05 is the hub's Pay-on-Verified price floor (AIMARKET_VERIFY_MIN_PRICE_USD), so a
buyer can hold the payment in escrow until GAIA's /v1/verify confirms the effect.

Paying is not permission. Who may command an actuator is its access policy and the lease a
verified "on" holds (gaia/actuation.py); the caller is the payer a trusted hub vouched for
on this request (gaia/payer_identity.py), or nobody.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from gaia.actuation import ActuationController
    from gaia.consumption import ConsumptionStore
    from gaia.owner_auth import NonceBook

ACTUATE_PRICE_USD = 0.05
CONSUMPTION_PRICE_USD = float(os.environ.get("GAIA_CONSUMPTION_PRICE_USD") or 0.01)

OWNER_AUTH: dict[str, Any] = {
    "type": "object",
    "description": "an EIP-712 signature of the owner's wallet over this request "
                   "(gaia/owner_auth.py: domain GAIA/1/8453, type OwnerRequest)",
    "properties": {
        "signer": {"type": "string", "description": "the owner's (or a delegate's) EVM address"},
        "issued_at": {"type": "integer", "description": "unix seconds, within ±120 s of now"},
        "nonce": {"type": "string", "description": "0x + 64 hex, random, used once"},
        "signature": {"type": "string", "description": "0x + 130 hex (eth_signTypedData_v4)"},
    },
    "required": ["signer", "issued_at", "nonce", "signature"],
}
OWNER_COMMAND_IN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actuator_id": {"type": "string"},
        "command": {"type": "string", "enum": ["on", "off"]},
        "duration_s": {"type": "integer", "minimum": 1, "maximum": 64800,
                       "description": "for 'on': how long; the plug switches itself off after it"},
        "auth": OWNER_AUTH,
    },
    "required": ["actuator_id", "command", "auth"],
}
CONSUMPTION_IN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actuator_id": {"type": "string", "default": "plug-sim-01"},
        "from": {"type": ["string", "integer"], "description": "ISO 8601 UTC or unix s; default 24 h ago"},
        "to": {"type": ["string", "integer"], "description": "ISO 8601 UTC or unix s; default now"},
        "resolution": {"type": "string", "enum": ["hour", "day"], "default": "hour"},
    },
    "required": ["actuator_id"],
}
OWNER_CONSUMPTION_IN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actuator_id": {"type": "string"},
        "from_ts": {"type": "integer", "description": "unix s (signed as fromTs)"},
        "to_ts": {"type": "integer", "description": "unix s (signed as toTs)"},
        "resolution": {"type": "string", "enum": ["hour", "day"], "default": "hour"},
        "auth": OWNER_AUTH,
    },
    "required": ["actuator_id", "from_ts", "to_ts", "auth"],
}
CONSUMPTION_OUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actuator_id": {"type": "string"}, "resolution": {"type": "string"},
        "from": {"type": "string"}, "to": {"type": "string"},
        "buckets": {"type": "array", "description": "start, end, energy_wh, mean_w, peak_w, "
                                                     "on_minutes, samples"},
        "totals": {"type": "object"},
        "attestation": {"type": "object",
                        "description": "Ed25519 by the plug's device key over the series' canonical JSON"},
        "simulated": {"type": "boolean"},
    },
}

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
        "lease": {"type": ["object", "null"],
                  "description": "after a verified 'on': {holder, until}. Until then only "
                                 "the holder or the owner may switch it; holder null means "
                                 "the call had no vouched payer, so nobody else can end it early"},
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
    from gaia import payer_identity

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        # Set for this request by PayerIdentityMiddleware; anonymous when nothing vouched.
        who = payer_identity.current()
        if who.problem:
            raise ValueError(who.problem)
        return controller.actuate(str(raw.get("actuator_id", "")), str(raw.get("command", "")),
                                  raw.get("duration_s"), caller=who.caller)
    return handler


def _owned_by(spec: Any, caller: Any) -> bool:
    return spec.is_owner(caller) or caller.id in spec.delegates


def owner_command_handler(controller: "ActuationController", gaia_url: str, nonces: "NonceBook"):
    from gaia import owner_auth

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        aid = str(raw.get("actuator_id", ""))
        command = str(raw.get("command", "")).strip().lower()
        if command not in ("on", "off"):
            raise ValueError("command must be on or off")
        spec = controller.specs.get(aid)
        if spec is None:
            raise ValueError(f"unknown actuator {aid[:60]!r}")
        caller, _ = owner_auth.verify(raw, gaia_url=gaia_url, action=command, nonces=nonces)
        if not _owned_by(spec, caller):
            raise ValueError(f"{aid} has no owner or delegate {caller.id[:6]}…{caller.id[-4:]}; "
                             "gaia.owner.command@v1 is its owner's (a public demo plug has none "
                             "— switch it with gaia.actuate@v1); nothing charged")
        return controller.actuate(aid, command, raw.get("duration_s"), caller=caller)
    return handler


def _series(controller: "ActuationController", store: "ConsumptionStore", spec: Any,
            from_ts: int, to_ts: int, resolution: str) -> dict[str, Any]:
    from gaia.consumption import MAX_BUCKETS, series_canonical

    if resolution not in MAX_BUCKETS:
        raise ValueError("resolution must be hour or day")
    if to_ts <= from_ts:
        raise ValueError("to must be after from")
    width = 3600 if resolution == "hour" else 86400
    if (to_ts - from_ts) / width > MAX_BUCKETS[resolution]:
        raise ValueError(f"at most {MAX_BUCKETS[resolution]} {resolution} buckets per call; "
                         "narrow from/to or use resolution day")
    buckets = store.series(spec.actuator_id, from_ts, to_ts, resolution)
    doc = {"actuator_id": spec.actuator_id, "resolution": resolution,
           "from": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(from_ts)),
           "to": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(to_ts)),
           "buckets": buckets}
    signer = controller._plug(spec.actuator_id).signer
    return {**doc,
            "totals": {"energy_wh": round(sum(b["energy_wh"] for b in buckets), 3),
                       "on_minutes": round(sum(b["on_minutes"] for b in buckets), 2),
                       "buckets": len(buckets)},
            "attestation": {"algorithm": "ed25519", "public_key": signer.public_key_b64,
                            "value": signer.sign_canonical(series_canonical(doc))},
            "simulated": spec.kind == "sim-plug"}


def consumption_read_handler(controller: "ActuationController", store: "ConsumptionStore"):
    from gaia.consumption import parse_time

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        aid = str(raw.get("actuator_id", ""))
        spec = controller.specs.get(aid)
        if spec is None:
            raise ValueError(f"unknown actuator {aid[:60]!r}")
        if not spec.consumption_sale:
            raise ValueError(f"{aid}: its owner has not put its consumption on sale; nothing charged")
        resolution = str(raw.get("resolution") or spec.consumption_resolution).strip().lower()
        if spec.consumption_resolution == "day" and resolution == "hour":
            raise ValueError(f"{aid}: its owner sells only daily totals; ask resolution day, "
                             "nothing charged")
        # A sold series holds whole, finished buckets only: a window narrower than a bucket,
        # or the bucket still filling (read again and again), would hand out finer data than
        # the owner chose to sell. So from rounds up and to rounds down to the bucket.
        width = 3600 if resolution == "hour" else 86400
        now = int(time.time())
        to_ts = min(parse_time(raw.get("to"), now), now) // width * width
        from_ts = -(-parse_time(raw.get("from"), to_ts - 86400) // width) * width
        earliest = now - spec.consumption_history_days * 86400
        if from_ts < earliest:
            raise ValueError(f"{aid}: its owner sells at most {spec.consumption_history_days} days "
                             "back; move from forward, nothing charged")
        if to_ts <= from_ts:
            raise ValueError(f"{aid}: no finished {resolution} in that window (a sold series holds "
                             f"whole UTC {resolution}s only); nothing charged")
        return _series(controller, store, spec, from_ts, to_ts, resolution)
    return handler


def owner_consumption_handler(controller: "ActuationController", store: "ConsumptionStore",
                              gaia_url: str, nonces: "NonceBook"):
    from gaia import owner_auth

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        aid = str(raw.get("actuator_id", ""))
        spec = controller.specs.get(aid)
        if spec is None:
            raise ValueError(f"unknown actuator {aid[:60]!r}")
        caller, msg = owner_auth.verify(raw, gaia_url=gaia_url, action="consumption", nonces=nonces)
        if not _owned_by(spec, caller):
            raise ValueError(f"{aid}: this wallet is not its owner or a delegate; others read it "
                             "with gaia.consumption.read@v1 where it is for sale")
        resolution = str(raw.get("resolution") or "hour").strip().lower()
        return _series(controller, store, spec, int(msg["fromTs"]), int(msg["toTs"]), resolution)
    return handler


__all__ = ["ACTUATE_IN", "ACTUATE_OUT", "ACTUATE_PRICE_USD", "CONSUMPTION_IN", "CONSUMPTION_OUT",
           "CONSUMPTION_PRICE_USD", "LIST_OUT", "OWNER_COMMAND_IN", "OWNER_CONSUMPTION_IN",
           "actuate_handler", "consumption_read_handler", "list_handler",
           "owner_command_handler", "owner_consumption_handler"]
