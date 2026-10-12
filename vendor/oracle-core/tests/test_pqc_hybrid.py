"""Hybrid Ed25519 + ML-DSA-65: the three migration phases, and the downgrade attack.

The load-bearing test here is `test_stripping_the_pq_fields_is_refused_when_pq_is_required`.
Without a require-policy, hybrid signing buys migration ability and NOT post-quantum security:
an adversary who breaks Ed25519 simply deletes the `pq_*` keys and is accepted on the classical
signature alone.
"""

from __future__ import annotations

import stat

import pytest
from oracle_core.signing import (
    PQCMisconfigured,
    Signer,
    pqc_available,
    pqc_required,
)

pytestmark = pytest.mark.skipif(not pqc_available(), reason="dilithium-py not installed")

CANON = "capabilities_count:1|generated_at:x|protocol_version:v2|tools_hash:a|by_hub_hash:b"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("ORACLE_PQC", "ORACLE_PQC_REQUIRE", "ORACLE_SIGNING_SEED_B64"):
        monkeypatch.delenv(var, raising=False)


def _signer(tmp_path, pqc: bool):
    return Signer(tmp_path / "key", pqc=pqc)


# ─────────────────────────────────────────────────── phase 1: capability only


def test_pqc_is_off_unless_asked(tmp_path):
    sig = _signer(tmp_path, False).sign_payload(CANON)
    assert "pq_value" not in sig and sig["algorithm"] == "ed25519"


def test_a_classical_document_still_verifies(tmp_path):
    s = _signer(tmp_path, False)
    assert Signer.verify_signature_object(CANON, s.sign_payload(CANON)) is True


def test_require_is_off_by_default():
    """Turning it on before every signer emits PQ would reject the whole federation."""
    assert pqc_required() is False


# ─────────────────────────────────────────────────── phase 2: sign hybrid


def test_enabling_pqc_attaches_a_second_signature(tmp_path):
    sig = _signer(tmp_path, True).sign_payload(CANON)
    assert sig["algorithm"] == "ed25519"          # classical stays authoritative
    assert sig["pq_algorithm"] == "ml-dsa-65"
    assert sig["pq_value"] and sig["pq_public_key"]


def test_both_signatures_must_verify(tmp_path):
    sig = _signer(tmp_path, True).sign_payload(CANON)
    assert Signer.verify_signature_object(CANON, sig) is True


def test_a_tampered_pq_signature_fails_even_though_ed25519_is_valid(tmp_path):
    """The whole point of 'both required': a broken PQ side must not pass on Ed25519 alone."""
    s = _signer(tmp_path, True)
    sig = s.sign_payload(CANON)
    other = _signer(tmp_path / "other", True).sign_payload(CANON)
    sig["pq_value"] = other["pq_value"]           # valid-looking, wrong key
    assert Signer.verify_signature_object(CANON, sig) is False


def test_a_tampered_ed25519_signature_fails_even_though_pq_is_valid(tmp_path):
    s = _signer(tmp_path, True)
    sig = s.sign_payload(CANON)
    sig["value"] = _signer(tmp_path / "other", True).sign_payload(CANON)["value"]
    assert Signer.verify_signature_object(CANON, sig) is False


def test_the_pq_key_persists_across_restarts(tmp_path):
    """The ML-DSA keypair lives beside the Ed25519 one. If the volume is not persisted the PQ
    identity changes on every container recreate, and a pinned pubkey breaks."""
    first = _signer(tmp_path, True).sign_payload(CANON)["pq_public_key"]
    assert (tmp_path / "key_mldsa").is_file()
    assert stat.S_IMODE((tmp_path / "key_mldsa").stat().st_mode) == 0o600
    assert _signer(tmp_path, True).sign_payload(CANON)["pq_public_key"] == first


def test_algorithm_labels_are_load_bearing(tmp_path):
    signature = _signer(tmp_path, True).sign_payload(CANON)
    signature["pq_algorithm"] = "not-ml-dsa"
    assert Signer.verify_signature_object(CANON, signature) is False


# ─────────────────────────────────────────────────── phase 3: require


def test_stripping_the_pq_fields_is_refused_when_pq_is_required(tmp_path):
    """THE downgrade attack. An adversary with a broken Ed25519 deletes the pq_* keys; under
    phases 1-2 that document is accepted, which is why phase 3 has to exist."""
    sig = _signer(tmp_path, True).sign_payload(CANON)
    stripped = {k: v for k, v in sig.items() if not k.startswith("pq_")}
    assert Signer.verify_signature_object(CANON, stripped, require_pq=False) is True
    assert Signer.verify_signature_object(CANON, stripped, require_pq=True) is False


def test_require_comes_from_the_environment(tmp_path, monkeypatch):
    sig = _signer(tmp_path, True).sign_payload(CANON)
    stripped = {k: v for k, v in sig.items() if not k.startswith("pq_")}
    monkeypatch.setenv("ORACLE_PQC_REQUIRE", "1")
    assert pqc_required() is True
    assert Signer.verify_signature_object(CANON, stripped) is False
    assert Signer.verify_signature_object(CANON, sig) is True


def test_an_explicit_flag_overrides_the_environment(tmp_path, monkeypatch):
    """So a federation mid-migration can require PQ per issuer instead of globally."""
    monkeypatch.setenv("ORACLE_PQC_REQUIRE", "1")
    sig = _signer(tmp_path, True).sign_payload(CANON)
    stripped = {k: v for k, v in sig.items() if not k.startswith("pq_")}
    assert Signer.verify_signature_object(CANON, stripped, require_pq=False) is True


# ─────────────────────────────────────────────────── manifests + receipts


def test_a_manifest_is_verified_on_both_signatures(tmp_path):
    """`verify_manifest_signature` used to call Ed25519-only, so the PQ fields on a manifest were
    decoration — signed but never checked."""
    s = _signer(tmp_path, True)
    manifest = {"capabilities_count": 1, "generated_at": "x", "protocol_version": "v2",
                "tools": [{"a": 1}]}
    manifest["signature"] = s.sign_manifest(manifest)
    assert s.verify_manifest_signature(manifest) is True
    manifest["signature"]["pq_value"] = _signer(tmp_path / "o", True).sign_payload("z")["pq_value"]
    assert s.verify_manifest_signature(manifest) is False


def test_a_receipt_now_carries_a_pq_signature(tmp_path):
    """`sign_receipt` built its signature dict by hand and bypassed sign_payload, so the busiest
    signed object in the ecosystem was the one the migration skipped."""
    s = _signer(tmp_path, True)
    signed = s.sign_receipt({"nonce": "n", "product_id": "p", "capability_id": "c",
                             "price_usd": 0.1, "timestamp": "t", "success": True,
                             "latency_ms": 5})
    assert signed["signature"]["pq_algorithm"] == "ml-dsa-65"
    assert s.verify_receipt(signed) is True


def test_a_receipt_signature_stays_wire_compatible(tmp_path):
    """The hub reads `algorithm` and `value` and ignores the rest, so pq_* keys are additive."""
    s = _signer(tmp_path, True)
    signed = s.sign_receipt({"nonce": "n", "product_id": "p", "capability_id": "c",
                             "price_usd": 0.1, "timestamp": "t", "success": True, "latency_ms": 5})
    sig = signed["signature"]
    assert sig["algorithm"] == "ed25519" and sig["value"]
    body = {k: v for k, v in signed.items() if k != "signature"}
    assert Signer.verify(s.receipt_canonical(body), sig["value"], s.public_key_b64) is True


def test_a_classical_receipt_still_verifies(tmp_path):
    """Migration safety: receipts signed before PQC was on must keep verifying."""
    s = _signer(tmp_path, False)
    signed = s.sign_receipt({"nonce": "n", "product_id": "p", "capability_id": "c",
                             "price_usd": 0.1, "timestamp": "t", "success": True, "latency_ms": 5})
    assert "pq_value" not in signed["signature"]
    assert s.verify_receipt(signed) is True


# ─────────────────────────────────────────────────── misconfiguration


def test_requiring_pq_without_the_library_is_loud_not_silent(monkeypatch):
    """A verifier that demands proof it cannot evaluate is broken, not strict. Returning False
    would fail every document for a reason that has nothing to do with the documents."""
    import oracle_core.signing as mod

    monkeypatch.setattr(mod, "_PQ_LIB", False)
    with pytest.raises(PQCMisconfigured, match="dilithium-py"):
        mod.Signer.verify_signature_object(CANON, {"value": "x", "public_key": "y"},
                                           require_pq=True)


def test_signing_with_pq_without_the_library_is_loud_not_silent(monkeypatch, tmp_path):
    import oracle_core.signing as mod

    monkeypatch.setattr(mod, "_PQ_LIB", False)
    with pytest.raises(PQCMisconfigured, match="dilithium-py"):
        mod.Signer(tmp_path / "key", pqc=True)


# ──────────────────────────────────── pinning the PQ identity (phase-3 prerequisite)


def _substituted(sig: dict, canonical: str, attacker_pq) -> dict:
    """The signature an adversary who has broken Ed25519 would present.

    They forge the classical signature against the pinned (broken) key and attach an ML-DSA
    keypair they hold. Both layers verify on their own terms; only a PINNED PQ key catches it.
    """
    import base64

    from dilithium_py.ml_dsa import ML_DSA_65

    pk, sk = attacker_pq
    forged = dict(sig)
    forged["pq_public_key"] = base64.b64encode(pk).decode()
    forged["pq_value"] = base64.b64encode(ML_DSA_65.sign(sk, canonical.encode())).decode()
    return forged


def test_pinned_pq_key_accepts_the_real_signer(tmp_path):
    s = _signer(tmp_path, True)
    sig = s.sign_payload(CANON)
    assert Signer.verify_signature_object(
        CANON, sig, s.public_key_b64, pq_public_key_b64=sig["pq_public_key"])


def test_pinned_pq_key_refuses_a_substituted_one(tmp_path):
    from dilithium_py.ml_dsa import ML_DSA_65

    s = _signer(tmp_path, True)
    sig = s.sign_payload(CANON)
    forged = _substituted(sig, CANON, ML_DSA_65.keygen())

    # Unpinned, the substitution passes — the PQ layer authenticated the DOCUMENT but not the
    # PEER. This assertion is deliberate: it records the limitation instead of implying the
    # `pq_*` fields alone defeat an Ed25519 break.
    assert Signer.verify_signature_object(CANON, forged, s.public_key_b64) is True

    # Pinned, it is refused.
    assert Signer.verify_signature_object(
        CANON, forged, s.public_key_b64, pq_public_key_b64=sig["pq_public_key"]) is False


def test_pinning_reaches_manifests_and_receipts(tmp_path):
    from dilithium_py.ml_dsa import ML_DSA_65

    s = _signer(tmp_path, True)

    manifest = {"capabilities_count": 1, "generated_at": "x", "protocol_version": "v2",
                "tools": [{"name": "t"}]}
    signed = dict(manifest)
    signed["signature"] = s.sign_manifest(manifest)
    real_pq = signed["signature"]["pq_public_key"]
    assert s.verify_manifest_signature(signed, s.public_key_b64, pq_public_key_b64=real_pq)

    swapped = dict(signed)
    swapped["signature"] = _substituted(signed["signature"], s.manifest_canonical(signed),
                                        ML_DSA_65.keygen())
    assert s.verify_manifest_signature(swapped, s.public_key_b64,
                                       pq_public_key_b64=real_pq) is False

    receipt = s.sign_receipt({"nonce": "n1", "product_id": "p", "capability_id": "c",
                              "price_usd": 1, "timestamp": "t", "success": True,
                              "latency_ms": 5})
    assert s.verify_receipt(receipt, s.public_key_b64,
                            pq_public_key_b64=receipt["signature"]["pq_public_key"])
    body = {k: v for k, v in receipt.items() if k != "signature"}
    bad = dict(receipt)
    bad["signature"] = _substituted(receipt["signature"], s.receipt_canonical(body),
                                    ML_DSA_65.keygen())
    assert s.verify_receipt(bad, s.public_key_b64,
                            pq_public_key_b64=receipt["signature"]["pq_public_key"]) is False


def test_pinning_does_not_rescue_a_broken_classical_signature(tmp_path):
    """A correct PQ signature must not make a bad Ed25519 one acceptable. Rule 1 runs first."""
    s = _signer(tmp_path, True)
    sig = s.sign_payload(CANON)
    sig["value"] = sig["value"][:-4] + "AAAA"
    assert Signer.verify_signature_object(
        CANON, sig, s.public_key_b64, pq_public_key_b64=sig["pq_public_key"]) is False
