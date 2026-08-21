# oracle-core signing — implementation note

This document describes **what the code does today**. It is **not** a normative protocol spec,
an external audit, or a proof of correctness. For ecosystem-level honesty see
[`docs/crypto-maturity.en.md`](../../docs/crypto-maturity.en.md).

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

1. Ed25519 **must** verify.
2. If `pq_value` is present, ML-DSA-65 **must also** verify (both required).
3. If `pq_value` is absent, Ed25519 alone suffices.

**Rationale (informal):** dual signatures — safe if either primitive holds during migration.
This is a common hybrid pattern but **has not been independently reviewed** for this codebase.

---

## Key material

| Key | Storage | Notes |
|-----|---------|-------|
| Ed25519 | `{key_path}` — 64 B seed‖pub | Or `ORACLE_SIGNING_SEED_B64` env (32 B seed) |
| ML-DSA-65 | `{key_path}_mldsa` — hex pk/sk lines | Generated on first use when PQC enabled |

File permissions attempted `0600`. **No HSM integration** in-tree.

---

## Hub integration gap

The AIMarket Hub verifies **Ed25519 manifest/receipt signatures** on its hot path. PQ fields are
**ignored** unless a consumer calls `Signer.verify_signature_object` locally.

Until protocol v2.x freezes PQ fields and the Hub enforces both layers, treat hybrid PQC as
**operator opt-in**, not ecosystem-wide post-quantum readiness.

---

## Test coverage

- `core/tests/test_core.py` — Ed25519 round-trip; hybrid when `dilithium-py` installed.
- `oracles/platon/backend/tests/test_signing.py` — hybrid tamper cases.

**Missing for production hardening:**

- Negative test vectors published in `aimarket-protocol`
- Cross-language verifiers (TypeScript, Solidity) for PQ extension
- External audit of canonical string choices and key-binding policy

See [KI-6](https://github.com/alexar76/aicom/blob/main/docs/known-issues.md#ki-6--oracle-family-cryptographic-maturity-not-production-hardened).
