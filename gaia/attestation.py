"""Per-device attestation — the physical-oracle identity chain.

Each virtual device holds its own Ed25519 key (a stand-in for a secure-element
key on real hardware) and signs every reading over a canonical that binds the
device identity, sequence number, timestamp, and a hash of the values:

    device:{id}|model:{model}|seq:{n}|ts:{iso}|values_sha256:{hex}

``seq`` + ``ts`` make readings replay-evident; the values hash makes them
tamper-evident. The GATEWAY then countersigns the whole invoke result with its
oracle-core receipt signature, giving the buyer a two-link chain:

    device key  → this reading is what the sensor produced
    gateway key → this reading is what the gateway sold (billed receipt)

On real hardware the device link would come from a secure element / TEE — the
same slot the AIMarket protocol already reserves via ``tee_attestation``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from oracle_core.signing import Signer


def _values_hash(values: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def reading_canonical(reading: dict[str, Any]) -> str:
    return (
        f"device:{reading.get('device_id', '')}"
        f"|model:{reading.get('model', '')}"
        f"|seq:{reading.get('seq', 0)}"
        f"|ts:{reading.get('ts', '')}"
        f"|values_sha256:{_values_hash(reading.get('values', {}))}"
    )


def sign_reading(reading: dict[str, Any], device_signer: Signer) -> dict[str, str]:
    return {
        "algorithm": "ed25519",
        "public_key": device_signer.public_key_b64,
        "value": device_signer.sign_canonical(reading_canonical(reading)),
        "canonical": "device|model|seq|ts|values_sha256",
    }


def verify_reading(reading: dict[str, Any], attestation: dict[str, str],
                   expected_pubkey: str | None = None) -> bool:
    """Verify a device attestation. When ``expected_pubkey`` is supplied (from
    the fleet registry / a pinned manifest) the signature must be by THAT key —
    a self-carried key alone proves consistency, not identity."""
    key = expected_pubkey or attestation.get("public_key", "")
    if not key:
        return False
    if expected_pubkey and attestation.get("public_key") != expected_pubkey:
        return False
    return Signer.verify(reading_canonical(reading), attestation.get("value", ""), key)
