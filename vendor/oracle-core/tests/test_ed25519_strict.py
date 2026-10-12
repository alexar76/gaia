"""Ed25519 signing against the RFC 8032 test vectors, and strict verification.

OpenSSL (`cryptography`) accepts, under the identity public key, the signature R = identity,
S = 0 for EVERY message — no secret key exists for it, so anyone can make one. An all-zero key
(order 4) is forgeable the same way after a few tries. `Signer.verify` refuses small-order keys
and small-order R, as libsodium does (KI-6 self-verification, 2026-10-12).
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from oracle_core.signing import Signer, has_small_order

b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731

# RFC 8032 §7.1, TEST 1 (empty message) and TEST 2 (one byte 0x72 = "r").
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "r",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]

# The eight points of order dividing 8 (computed as L*P in the Sortes VRF tests), both signs.
SMALL_ORDER_POINTS = [bytes.fromhex(h) for h in (
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0000000000000000000000000000000000000000000000000000000000000080",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
)]
IDENTITY = bytes.fromhex("01" + "00" * 31)


@pytest.mark.parametrize("sk,pk,msg,sig", RFC8032)
def test_rfc8032_vectors_sign_and_verify(sk, pk, msg, sig):
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(sk))
    from cryptography.hazmat.primitives import serialization
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert raw.hex() == pk
    assert key.sign(msg.encode()).hex() == sig
    assert Signer.verify(msg, b64(bytes.fromhex(sig)), b64(bytes.fromhex(pk))) is True
    assert Signer.verify(msg + "x", b64(bytes.fromhex(sig)), b64(bytes.fromhex(pk))) is False


def test_every_small_order_point_is_recognised_and_honest_keys_are_not():
    assert all(has_small_order(p) for p in SMALL_ORDER_POINTS)
    for i in range(50):
        honest = Ed25519PrivateKey.generate().public_key()
        from cryptography.hazmat.primitives import serialization
        raw = honest.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        assert has_small_order(raw) is False


def test_the_identity_key_forgery_is_what_openssl_accepts_and_signer_refuses():
    forged = IDENTITY + bytes(32)                        # R = identity, S = 0
    raw_accepts = True
    try:
        Ed25519PublicKey.from_public_bytes(IDENTITY).verify(forged, b"any message at all")
    except Exception:  # noqa: BLE001
        raw_accepts = False
    # The library itself accepts it (the reason for this guard); if it ever stops, the guard is
    # still right, so this documents the dependency's behaviour rather than requiring it.
    if raw_accepts:
        assert Signer.verify("any message at all", b64(forged), b64(IDENTITY)) is False
    for msg in ("pay 100 to mallory", "a different statement"):
        assert Signer.verify(msg, b64(forged), b64(IDENTITY)) is False


def test_a_small_order_r_is_refused_even_under_an_honest_key():
    from cryptography.hazmat.primitives import serialization
    key = Ed25519PrivateKey.generate()
    pk = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sig = key.sign(b"m")
    assert Signer.verify("m", b64(sig), b64(pk)) is True
    for r in SMALL_ORDER_POINTS:
        assert Signer.verify("m", b64(r + sig[32:]), b64(pk)) is False


def test_the_zero_key_is_refused_for_every_r_and_s_tried():
    zero = bytes(32)
    for r in SMALL_ORDER_POINTS:
        for s in (bytes(32), hashlib.sha256(r).digest()[:31] + b"\x00"):
            assert Signer.verify("m", b64(r + s), b64(zero)) is False
