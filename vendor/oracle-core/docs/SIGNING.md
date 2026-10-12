# oracle-core signing — implementation note

This document describes **what the code does today**. It is **not** a normative protocol spec,
an external audit, or a proof of correctness. For ecosystem-level honesty see
[`docs/crypto-maturity.en.md`](https://github.com/alexar76/oracles/blob/main/docs/crypto-maturity.en.md).

Implementation: [`oracle_core/signing.py`](../oracle_core/signing.py).

---

## Ed25519 (default, always on)

**Manifest canonical string** (4 fields + tools hash):

```
capabilities_count:{n}|generated_at:{iso}|protocol_version:{v}|tools_hash:{sha256}
```

`tools_hash = SHA-256(JSON.dumps(tools, sort_keys=True))`.

**Receipt canonical string** (7 fields):

```
nonce:{n}|product_id:{id}|capability_id:{cap}|price_usd:{p}|timestamp:{iso}|success:{0|1}|latency_ms:{ms}
```

**Signature object** on manifests (via `sign_payload`):

```json
{
  "algorithm": "ed25519",
  "public_key": "<base64 32-byte pubkey>",
  "value": "<base64 64-byte sig>"
}
```

Receipts embed `signature.algorithm` + `signature.value` only (legacy 7-field receipt shape).

Verification uses `cryptography` Ed25519 over the UTF-8 canonical string.

---

## Hybrid ML-DSA-65 (optional, off by default)

Enable: `ORACLE_PQC=1` or `Signer(..., pqc=True)` **and** install `dilithium-py`.

When enabled, `sign_payload` **adds** (does not replace Ed25519):

```json
{
  "pq_algorithm": "ml-dsa-65",
  "pq_public_key": "<base64 ML-DSA-65 public key>",
  "pq_value": "<base64 ML-DSA-65 signature>"
}
```

**Verification policy** (`verify_signature_object`):

1. Ed25519 **must** verify. Always, and first — it stays authoritative through the migration.
2. If PQ is **required** (`ORACLE_PQC_REQUIRE=1`, or `require_pq=True`) and `pq_value` is absent
   → **reject**.
3. If `pq_value` is present, ML-DSA-65 **must also** verify (both required).
4. If `pq_value` is absent and PQ is not required, Ed25519 alone suffices.

**Rationale (informal):** dual signatures — safe if either primitive holds during migration.
This is a common hybrid pattern but **has not been independently reviewed** for this codebase.

**Rule 2 is the point, not polish.** Under rules 1/3/4 alone, the *absence* of a PQ signature is a
valid document — so an adversary who breaks Ed25519 simply strips the `pq_*` fields and is accepted
on the classical signature. Hybrid signing without a require-policy buys **migration ability, not
post-quantum security**.

**Migration order is not symmetric.** A verifier without `dilithium-py` *rejects* a PQ-signed
document (fail-closed), so signers must never get ahead of verifiers:

| Phase | Do | Effect |
|-------|----|--------|
| 1 | install `aimarket-oracle-core[pqc]` on every **verifier** | no behaviour change; verifiers become PQ-capable |
| 2 | `ORACLE_PQC=1` on **signers**, one component at a time | documents carry both; both are checked |
| 3 | `ORACLE_PQC_REQUIRE=1` on **verifiers** | classical-only documents refused — actual PQ security |

`ORACLE_PQC_REQUIRE=1` with the library missing raises `PQCMisconfigured` rather than returning
`False`: a verifier demanding proof it cannot evaluate is broken, not strict, and a silent `False`
would fail every document for a reason unrelated to the documents.

---

## Key material

| Key | Storage | Notes |
|-----|---------|-------|
| Ed25519 | `{key_path}` — 64 B seed‖pub | Or `ORACLE_SIGNING_SEED_B64` env (32 B seed) |
| ML-DSA-65 | `{key_path}_mldsa` — hex pk/sk lines | Generated on first use when PQC enabled |

File permissions attempted `0600`. **No HSM integration** in-tree.

---

## Hub integration — the gap, and what closed it

This section used to read: *"the Hub verifies Ed25519 on its hot path; PQ fields are ignored."*
That was accurate and it made the PQ fields decoration on the busiest verification path in the
ecosystem. Three things were wrong, all now fixed:

- `aimarket_hub.signing` had **zero** `pq_` references. It now has `verify_hybrid()` implementing
  the same four-rule policy, and `verify_manifest_signature` routes through it — so a federated
  peer's ML-DSA-65 signature is actually checked. Extra: `aimarket-hub[pqc]`; policy flag:
  `AIMARKET_PQC_REQUIRE`.
- oracle-core's own `verify_manifest_signature` called `self.verify(...)` — Ed25519 only. A
  PQ-signed *oracle* manifest had its PQ layer skipped too. Now hybrid.
- `sign_receipt` built its signature dict by hand (`algorithm` + `value`), bypassing
  `sign_payload`, so **receipts carried no PQ signature at all** even with `ORACLE_PQC=1` — the
  most numerous signed object was the one the migration silently skipped. Now hybrid, and still
  wire-compatible: the hub reads `algorithm`/`value` and ignores the extra keys.

Interop is verified end to end: oracle-core signs hybrid, both implementations compute an identical
manifest canonical, the hub verifies both layers, and a stripped `pq_*` document is accepted with
require off and refused with require on.

**Still honest:** protocol v2.x has not frozen the PQ field names, and none of this has had an
external review. Treat phases 1–2 as migration plumbing; only phase 3 is a security claim.

---

## Test coverage

- `core/tests/test_core.py` — Ed25519 round-trip; hybrid when `dilithium-py` installed.
- `oracles/platon/backend/tests/test_signing.py` — hybrid tamper cases.

**Missing for production hardening:**

- Negative test vectors published in `aimarket-protocol`
- Cross-language verifiers (TypeScript, Solidity) for PQ extension
- External audit of canonical string choices and key-binding policy

See [KI-6](https://github.com/alexar76/aicom/blob/main/docs/known-issues.md#ki-6--oracle-family-cryptographic-maturity-not-production-hardened).
