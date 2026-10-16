"""Owner requests: an EIP-712 signature by the owner's wallet, checked by GAIA itself.

A real plug answers only its owner. The owner does not go through a hub and does not pay:
they sign the request with the wallet the operator's actuator file names as ``owner`` (or one
of its ``delegates`` — the owner's own agents), and GAIA recovers the signer from the
signature. Nothing the request says about who sent it is believed; the signature is the
identity.

Typed data (EIP-712, what a wallet such as MetaMask shows field by field):

    domain       {name: "GAIA", version: "1", chainId: 8453}
    OwnerRequest {gaia: string, actuator: string, action: string, durationS: uint256,
                  fromTs: uint256, toTs: uint256, issuedAt: uint256, nonce: bytes32}

``gaia`` is this GAIA's public URL, so a signature made for one GAIA is refused by another.
``action`` is ``on``, ``off`` or ``consumption``; ``durationS`` matters for ``on``, ``fromTs``
and ``toTs`` for ``consumption`` (0 when unused). ``issuedAt`` must be within ±120 s of now,
and each ``nonce`` is accepted once; used nonces are kept on disk for the window, so a restart
does not reopen it.

Request input: ``{"actuator_id": …, "command": …, "duration_s": …, "auth": {"signer": "0x…",
"issued_at": <unix s>, "nonce": "0x<64 hex>", "signature": "0x…"}}``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from gaia.payer_identity import Caller, normalize_identity

DOMAIN = {"name": "GAIA", "version": "1", "chainId": 8453}
ACTIONS = ("on", "off", "consumption")
MAX_SKEW_S = 120
VOUCHED_BY = "owner-signature"
_NONCE = re.compile(r"0x[0-9a-fA-F]{64}")
_SIG = re.compile(r"0x[0-9a-fA-F]{130}")
_EVM = re.compile(r"0x[0-9a-fA-F]{40}")

TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "OwnerRequest": [
        {"name": "gaia", "type": "string"},
        {"name": "actuator", "type": "string"},
        {"name": "action", "type": "string"},
        {"name": "durationS", "type": "uint256"},
        {"name": "fromTs", "type": "uint256"},
        {"name": "toTs", "type": "uint256"},
        {"name": "issuedAt", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}


class OwnerAuthError(ValueError):
    """The request is refused; the message says why (shown to the caller, nothing charged)."""


def typed_data(*, gaia: str, actuator: str, action: str, duration_s: int = 0, from_ts: int = 0,
               to_ts: int = 0, issued_at: int, nonce: str) -> dict[str, Any]:
    """The full EIP-712 document a wallet signs (eth_signTypedData_v4)."""
    return {
        "types": TYPES,
        "primaryType": "OwnerRequest",
        "domain": dict(DOMAIN),
        "message": {
            "gaia": gaia.rstrip("/"), "actuator": actuator, "action": action,
            "durationS": int(duration_s), "fromTs": int(from_ts), "toTs": int(to_ts),
            "issuedAt": int(issued_at), "nonce": nonce.lower(),
        },
    }


def recover_signer(document: dict[str, Any], signature: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    msg = dict(document["message"])
    msg["nonce"] = bytes.fromhex(msg["nonce"][2:])
    signable = encode_typed_data(full_message={**document, "message": msg})
    return Account.recover_message(signable, signature=signature).lower()


class NonceBook:
    """Nonces accepted within the skew window, persisted so a restart keeps refusing replays."""

    def __init__(self, path: str | Path | None, window_s: int = 2 * MAX_SKEW_S):
        self.path = Path(path) if path else None
        self.window_s = window_s
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        if self.path and self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._seen = {str(k): float(v) for k, v in raw.items()}
            except (OSError, ValueError):
                self._seen = {}

    def claim(self, nonce: str, now: float) -> bool:
        with self._lock:
            self._seen = {k: v for k, v in self._seen.items() if v > now - self.window_s}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            self._save()
            return True

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._seen), encoding="utf-8")
        os.replace(tmp, self.path)


def verify(raw: dict[str, Any], *, gaia_url: str, action: str, nonces: NonceBook,
           now: float | None = None) -> tuple[Caller, dict[str, Any]]:
    """The checked identity and the signed message, or OwnerAuthError with the reason."""
    now = time.time() if now is None else now
    auth = raw.get("auth")
    if not isinstance(auth, dict):
        raise OwnerAuthError("an owner request needs auth: {signer, issued_at, nonce, signature}")
    signer = str(auth.get("signer") or "").strip()
    nonce = str(auth.get("nonce") or "").strip().lower()
    signature = str(auth.get("signature") or "").strip()
    if not _EVM.fullmatch(signer):
        raise OwnerAuthError("auth.signer must be an EVM address (0x + 40 hex); owner requests "
                             "are EIP-712 signatures")
    if not _NONCE.fullmatch(nonce):
        raise OwnerAuthError("auth.nonce must be 32 random bytes as 0x + 64 hex")
    if not _SIG.fullmatch(signature):
        raise OwnerAuthError("auth.signature must be a 65-byte signature as 0x + 130 hex")
    try:
        issued_at = int(auth.get("issued_at"))
    except (TypeError, ValueError):
        raise OwnerAuthError("auth.issued_at must be unix seconds") from None
    if abs(now - issued_at) > MAX_SKEW_S:
        raise OwnerAuthError(f"auth.issued_at is {int(now - issued_at)} s from now; an owner "
                             f"request is good for ±{MAX_SKEW_S} s — sign it again")
    if action not in ACTIONS:
        raise OwnerAuthError(f"action must be one of {', '.join(ACTIONS)}")
    document = typed_data(
        gaia=gaia_url, actuator=str(raw.get("actuator_id") or ""), action=action,
        duration_s=int(float(raw.get("duration_s") or 0)), from_ts=int(raw.get("from_ts") or 0),
        to_ts=int(raw.get("to_ts") or 0), issued_at=issued_at, nonce=nonce)
    try:
        recovered = recover_signer(document, signature)
    except Exception:  # noqa: BLE001 — a malformed signature is a refusal, not a crash
        raise OwnerAuthError("the signature does not verify for this request") from None
    if recovered != signer.lower():
        # The usual cause: the request's fields differ from what was signed (another GAIA,
        # another actuator, another duration), or the wallet signed with another account.
        raise OwnerAuthError("the signature was made by another wallet or for another request "
                             "(check gaia, actuator, action, duration and time)")
    if not nonces.claim(nonce, now):
        raise OwnerAuthError("this nonce was already used; sign a fresh request")
    return Caller(id=normalize_identity(recovered), vouched_by=VOUCHED_BY), document["message"]


__all__ = ["ACTIONS", "DOMAIN", "MAX_SKEW_S", "NonceBook", "OwnerAuthError", "TYPES",
           "VOUCHED_BY", "recover_signer", "typed_data", "verify"]
