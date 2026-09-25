"""Ed25519 signing (+ optional hybrid ML-DSA-65) for the oracle family.

Canonical forms for manifest (4-field, with tools_hash) and receipts (7-field)
match the AIMarket hub exactly, so signatures verify against the live hub. PQC is
additive: when enabled, an ML-DSA-65 (FIPS 204) signature is attached alongside
Ed25519 — verifiers that understand it require both; the hub keeps checking Ed25519.

Decoupled from any single oracle's config: the seed may come from
``ORACLE_SIGNING_SEED_B64`` (secrets manager / KMS) and PQC from ``ORACLE_PQC=1``
or the constructor flag.

THE THREE PHASES, AND WHY THE THIRD ONE EXISTS
----------------------------------------------
1. ``ORACLE_PQC`` unset everywhere — Ed25519 only. Install the ``pqc`` extra on every
   VERIFIER first; that alone changes no behaviour but makes them PQ-capable.
2. ``ORACLE_PQC=1`` on signers — documents carry both signatures, and any verifier that
   understands PQ requires both. Ed25519 stays authoritative, so a bug in the PQ side
   cannot lock anyone out.
3. ``ORACLE_PQC_REQUIRE=1`` on verifiers — a document with NO ``pq_value`` is rejected.

Phase 3 is not optional polish, it is the point. Under phases 1–2 the absence of a PQ
signature is accepted, so an adversary who breaks Ed25519 simply STRIPS the ``pq_*``
fields and is accepted on the classical signature alone. Hybrid signing without a
require-policy buys migration ability, not post-quantum security.

Order matters and is not symmetric: a verifier that lacks the ``pqc`` extra REJECTS a
PQ-signed document (fail-closed), so signers must never get ahead of verifiers.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

try:
    from dilithium_py.ml_dsa import ML_DSA_65 as _MLDSA

    _PQ_LIB = True
except Exception:  # pragma: no cover
    _MLDSA = None
    _PQ_LIB = False


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def pqc_available() -> bool:
    """Whether THIS process can verify (or produce) an ML-DSA-65 signature."""
    return _PQ_LIB


def pqc_required() -> bool:
    """Whether this verifier refuses a document that carries no PQ signature.

    Phase 3. Off by default: turning it on before every SIGNER emits PQ signatures would
    reject the whole federation's traffic.
    """
    return _truthy(os.environ.get("ORACLE_PQC_REQUIRE"))


class PQCMisconfigured(RuntimeError):
    """Raised when PQ signatures are REQUIRED but this process cannot check one.

    Deliberately loud rather than a silent `False`: a verifier that demands post-quantum
    proof it is unable to evaluate is not "strict", it is broken, and every document would
    fail for a reason that has nothing to do with the documents.
    """


def _ensure_keypair(path: Path) -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if path.exists():
        raw = path.read_bytes()
        if len(raw) == 64:
            return raw[:32], raw[32:]
        raise RuntimeError(f"Ed25519 key file {path} is corrupted (size={len(raw)})")
    path.parent.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    seed = priv.private_bytes_raw()
    pub = priv.public_key().public_bytes_raw()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raw = path.read_bytes()
        if len(raw) == 64:
            return raw[:32], raw[32:]
        raise RuntimeError(f"Ed25519 key file {path} is corrupted (size={len(raw)})")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(seed + pub)
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return seed, pub


def _seed_from_env() -> bytes | None:
    raw = os.environ.get("ORACLE_SIGNING_SEED_B64")
    if not raw:
        return None
    seed = base64.b64decode(raw)
    if len(seed) != 32:
        raise RuntimeError("ORACLE_SIGNING_SEED_B64 must decode to a 32-byte seed")
    return seed


def _read_pq_keypair(path: Path) -> tuple[bytes, bytes]:
    try:
        lines = path.read_text().splitlines()
        if len(lines) != 2 or not all(lines):
            raise ValueError("expected exactly two non-empty hex lines")
        pair = (bytes.fromhex(lines[0]), bytes.fromhex(lines[1]))
        if not all(pair):
            raise ValueError("empty key")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"ML-DSA key file {path} is corrupted: {exc}") from exc
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return pair


def _load_or_make_pq(path: Path) -> tuple[bytes, bytes]:
    if path.exists():
        return _read_pq_keypair(path)
    pk, sk = _MLDSA.keygen()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{pk.hex()}\n{sk.hex()}\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_pq_keypair(path)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(payload)
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return pk, sk


def load_pq_keypair(
    key_path: str | Path, *, pqc: bool | None = None
) -> tuple[bytes, bytes] | None:
    """The ML-DSA keypair that belongs beside the Ed25519 key at ``key_path``, or None.

    Public because SKOPOS cannot use :class:`Signer` — its canonical form is the HUB's
    (``aimarket_hub.signing.Signer.manifest_canonical``, with ``by_hub``), not this
    package's — and a hand-rolled second copy of ML-DSA key handling is a second place
    to get key PERSISTENCE wrong, which is the mistake that republishes a satellite
    under a new identity. Resolve once and hold it, the way :class:`Signer` does; this
    touches the filesystem.

    The key is written as ``<key_path>_mldsa``, the convention every phase-2 signer
    already follows, so it persists on whatever volume the classical key does.
    """
    if pqc is None:
        pqc = _truthy(os.environ.get("ORACLE_PQC"))
    if not pqc:
        return None
    if not _PQ_LIB:
        raise PQCMisconfigured(
            "ORACLE_PQC is on but dilithium-py is missing — "
            "install aimarket-oracle-core[pqc] on this signer"
        )
    return _load_or_make_pq(Path(f"{key_path}_mldsa"))


def pq_fields(pair: tuple[bytes, bytes] | None, canonical: str) -> dict[str, str]:
    """The ML-DSA half of a signature object; ``{}`` for None, so callers can splat it.

    One definition, because the FIELD NAMES are the interoperability contract — a
    second spelling of ``pq_value`` is a signature no verifier in the ecosystem looks at.
    """
    if pair is None:
        return {}
    pk, sk = pair
    return {
        "pq_algorithm": "ml-dsa-65",
        "pq_public_key": base64.b64encode(pk).decode(),
        "pq_value": base64.b64encode(_MLDSA.sign(sk, canonical.encode())).decode(),
    }


class Signer:
    def __init__(self, key_path: str | Path = "data/oracle_signing_key", pqc: bool | None = None) -> None:
        self.key_path = Path(key_path)
        env_seed = _seed_from_env()
        if env_seed is not None:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            self._seed = env_seed
            self._pub_bytes = (
                Ed25519PrivateKey.from_private_bytes(env_seed).public_key().public_bytes_raw()
            )
        else:
            self._seed, self._pub_bytes = _ensure_keypair(self.key_path)
        self._public_key_b64 = base64.b64encode(self._pub_bytes).decode()

        if pqc is None:
            pqc = os.environ.get("ORACLE_PQC", "").lower() in ("1", "true", "yes")
        self._pq: tuple[bytes, bytes] | None = None
        if pqc:
            if not _PQ_LIB:
                raise PQCMisconfigured(
                    "ORACLE_PQC is on but dilithium-py is missing — "
                    "install aimarket-oracle-core[pqc] on this signer"
                )
            self._pq = _load_or_make_pq(Path(f"{self.key_path}_mldsa"))

    @property
    def public_key_b64(self) -> str:
        return self._public_key_b64

    def sign_canonical(self, canonical: str) -> str:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        sig = Ed25519PrivateKey.from_private_bytes(self._seed).sign(canonical.encode())
        return base64.b64encode(sig).decode()

    def sign_payload(self, canonical: str) -> dict[str, str]:
        obj: dict[str, str] = {
            "algorithm": "ed25519",
            "public_key": self._public_key_b64,
            "value": self.sign_canonical(canonical),
        }
        obj.update(pq_fields(self._pq, canonical))
        return obj

    @staticmethod
    def verify(canonical: str, value_b64: str, public_key_b64: str) -> bool:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
            pub.verify(base64.b64decode(value_b64), canonical.encode())
            return True
        except (InvalidSignature, ValueError):
            return False

    @staticmethod
    def verify_signature_object(canonical: str, sig: dict[str, Any],
                                ed_public_key_b64: str | None = None, *,
                                require_pq: bool | None = None,
                                pq_public_key_b64: str | None = None) -> bool:
        """Verify a hybrid signature object.

        Policy, in order:
          1. Ed25519 MUST verify. Always. It stays authoritative through the migration.
          2. If PQ is REQUIRED and the object carries no `pq_value` -> reject. This is the
             downgrade guard: without it, stripping the `pq_*` fields is a valid document.
          3. If `pq_value` is present, ML-DSA-65 MUST verify too (both required).
          4. If it is absent and PQ is not required, Ed25519 alone suffices.

        `require_pq` defaults to `ORACLE_PQC_REQUIRE`. Pass it explicitly to enforce per
        issuer or per tier while the federation is mid-migration.

        WHY `pq_public_key_b64` EXISTS, AND WHY IT IS NOT OPTIONAL FOR PHASE 3
        ---------------------------------------------------------------------
        Ed25519 is checked against a key the CALLER pinned. The PQ key, when this argument is
        omitted, is read out of the signature object itself — self-asserted. Against the only
        adversary the PQ layer is for (one who can forge Ed25519) a self-asserted PQ key is
        worthless: they forge the classical signature with the broken pinned key and attach an
        ML-DSA keypair of their own.

        There is no cryptographic shortcut around this. A PQ identity has to be pinned the same
        way the classical one is. The practical route is to record each peer's `pq_public_key` on
        FIRST SIGHT, now, while Ed25519 signatures can still authenticate it — which is the real
        reason phase 2 is urgent, and not merely "start emitting a second signature".
        """
        must_pq = pqc_required() if require_pq is None else bool(require_pq)
        if must_pq and not _PQ_LIB:
            # Requiring a proof you cannot evaluate is a broken verifier, not a strict one.
            raise PQCMisconfigured(
                "ORACLE_PQC_REQUIRE is on but dilithium-py is missing — "
                "install aimarket-oracle-core[pqc] on this verifier")
        if sig.get("algorithm") != "ed25519":
            return False
        ed_key = ed_public_key_b64 or sig.get("public_key")
        if not ed_key or not Signer.verify(canonical, sig.get("value", ""), ed_key):
            return False
        if not sig.get("pq_value"):
            # Phase 3: a classical-only document is refused once PQ is required.
            return not must_pq
        if not _PQ_LIB:
            return False        # fail-closed: cannot check a PQ signature that is present
        if sig.get("pq_algorithm") != "ml-dsa-65":
            return False
        presented = sig.get("pq_public_key", "")
        if pq_public_key_b64 and presented != pq_public_key_b64:
            return False        # pinned PQ identity: a substituted key is a forgery attempt
        try:
            pk = base64.b64decode(presented)
            return bool(_MLDSA.verify(pk, canonical.encode(), base64.b64decode(sig["pq_value"])))
        except Exception:
            return False

    # --- manifest / receipt canonicals (match the AIMarket hub) ---
    def manifest_canonical(self, manifest: dict[str, Any]) -> str:
        """Byte-identical to aimarket_hub.signing.Signer.manifest_canonical.

        `by_hub_hash` is the fifth field and was missing here. The hub added it so a relay
        cannot tamper with per-peer trust_score and routing metadata under a valid
        signature — and since the hub verifies with ITS canonical, every oracle manifest
        failed with "Invalid manifest signature" and no oracle could federate at all. An
        oracle serves no `by_hub`, so this hashes `{}`, which is exactly what the hub
        computes for the absent key: the two agree without the oracle inventing a field.

        If the hub's canonical gains a sixth field, this must follow the same day.
        """
        tools = manifest.get("tools", [])
        tools_hash = hashlib.sha256(
            json.dumps(tools, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        by_hub_hash = hashlib.sha256(
            json.dumps(manifest.get("by_hub", {}), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return (
            f"capabilities_count:{manifest.get('capabilities_count', 0)}"
            f"|generated_at:{manifest.get('generated_at', '')}"
            f"|protocol_version:{manifest.get('protocol_version', 'v1')}"
            f"|tools_hash:{tools_hash}"
            f"|by_hub_hash:{by_hub_hash}"
        )

    def sign_manifest(self, manifest: dict[str, Any]) -> dict[str, str]:
        return self.sign_payload(self.manifest_canonical(manifest))

    def verify_manifest_signature(self, manifest: dict[str, Any], public_key_b64: str | None = None,
                                  *, require_pq: bool | None = None,
                                  pq_public_key_b64: str | None = None) -> bool:
        """Hybrid, not Ed25519-only.

        This used to call `self.verify(...)`, so a PQ-signed manifest was checked on its
        classical signature and the ML-DSA one was never looked at — the PQ fields were
        decoration. Routing through `verify_signature_object` makes them load-bearing.
        """
        sig = manifest.get("signature") or {}
        key = public_key_b64 or sig.get("public_key") or self._public_key_b64
        return Signer.verify_signature_object(
            self.manifest_canonical(manifest), sig, key, require_pq=require_pq,
            pq_public_key_b64=pq_public_key_b64)

    @staticmethod
    def receipt_canonical(receipt: dict[str, Any]) -> str:
        success = 1 if receipt.get("success", True) else 0
        return (
            f"nonce:{receipt.get('nonce', '')}"
            f"|product_id:{receipt.get('product_id', '')}"
            f"|capability_id:{receipt.get('capability_id', '')}"
            f"|price_usd:{receipt.get('price_usd', 0)}"
            f"|timestamp:{receipt.get('timestamp', '')}"
            f"|success:{success}"
            f"|latency_ms:{receipt.get('latency_ms', 0)}"
        )

    def sign_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """Sign a receipt, hybrid when PQC is enabled.

        This used to build the signature dict by hand with only `algorithm` + `value`,
        bypassing `sign_payload` — so receipts carried NO PQ signature even with
        ORACLE_PQC=1, and the busiest signed object in the ecosystem was the one object
        the migration silently skipped. The extra `pq_*` keys are additive: the hub reads
        `algorithm` and `value` and ignores the rest, so this stays wire-compatible.
        """
        signed = dict(receipt)
        signed["signature"] = self.sign_payload(self.receipt_canonical(receipt))
        return signed

    def verify_receipt(self, receipt: dict[str, Any], public_key_b64: str | None = None,
                       *, require_pq: bool | None = None,
                       pq_public_key_b64: str | None = None) -> bool:
        """Hybrid, for the same reason as manifests."""
        sig = receipt.get("signature") or {}
        key = public_key_b64 or sig.get("public_key") or self._public_key_b64
        body = {k: v for k, v in receipt.items() if k != "signature"}
        return Signer.verify_signature_object(
            self.receipt_canonical(body), sig, key, require_pq=require_pq,
            pq_public_key_b64=pq_public_key_b64)
