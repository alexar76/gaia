# aimarket-oracle-core

Shared infrastructure for the [alexar76 oracle family](https://github.com/alexar76/oracles) —
the layer that turns a pure function into a sellable, verifiable capability on
**AIMarket Protocol v2**.

An oracle written on top of this declares its capabilities and its maths. Everything else —
the HTTP surface, the signed manifest, priced invoke, Ed25519 receipts, measured latency and
success metrics, rate limiting, and hub federation — comes from here.

```python
from oracle_core import Capability, OracleSpec

SPEC = OracleSpec(
    name="Example Oracle",
    product_id="prod-example",
    description="What it sells, in a sentence an agent can choose on.",
    capabilities=[
        Capability(
            capability_id="example.double@v1",
            product_id="prod-example",
            description="Return twice the input.",
            handler=lambda d: {"result": 2 * float(d["value"])},
            input_schema={"type": "object", "required": ["value"],
                          "properties": {"value": {"type": "number"}}},
            output_schema={"type": "object", "required": ["result"],
                           "properties": {"result": {"type": "number"}}},
            price_per_call_usd=0.001,
        ),
    ],
)
```

## Install

```bash
pip install aimarket-oracle-core
```

The distribution is `aimarket-oracle-core`; the import is `oracle_core`. The unprefixed
name `oracle-core` on PyPI is an **unrelated project** that installs a module of the same
name — installing it in place of this one fails at
`ImportError: cannot import name 'Capability' from 'oracle_core'`.

## What you get

| | |
|---|---|
| **Protocol** | `.well-known/ai-market.json` discovery, v2 manifest, priced `invoke` |
| **Signing** | Ed25519 manifest signatures and 7-field receipts, canonical form shared with the hub; optional hybrid post-quantum via the `pqc` extra |
| **Metrics** | measured `p50_latency_ms` and `success_rate_30d`, not declared constants |
| **Safety** | handler exceptions become named refusals (`{"ok": false, "error": …}`) rather than opaque 500s |
| **Federation** | manifests a hub can verify byte-for-byte and re-list |
| **Cost controls** | free-tier ceilings and CPU budgets for capabilities that sell computation |

## Cost controls

Most capabilities are bounded by construction — their worst legal input costs a
fraction of a millisecond, and they need nothing here. Leave these fields unset
and behaviour is exactly as it was.

A capability that *sells computation* is different: if the caller picks how much
work to do, then the schema's own `maximum` is a promise to burn that much CPU on
request, for free, to anyone. `chronos.eval@v1` at `MAX_DIFFICULTY` is 6.8
sequential seconds; `aestus.seal@v1` at `MAX_T` is ~36. At the generic 120
invokes/min that is ~70 CPU-seconds of demand per second from one address.

```python
Capability(
    capability_id="mine.expensive@v1",
    handler=_run,
    # Unpaid callers are REFUSED above these (402, ceiling in the body) — never
    # silently served smaller, which would sign a receipt for work the caller did
    # not request. Set each to the schema's own default so an argument-free call is
    # never refused. Dotted paths reach into nested objects ("puzzle.T").
    free_tier_max={"iterations": 10_000},
    # What one input costs, in CPU-ms. Fit it to a benchmark and record the numbers
    # in a comment; only relative accuracy matters, since a slower machine scales
    # every cost alike. A formula that raises degrades to 1 ms rather than 500ing.
    cost_ms=lambda d: d.get("iterations", 10_000) / 25.0,
    cpu_budget_ms_per_min=20_000,         # a third of a core, per client
    global_cpu_budget_ms_per_min=60_000,  # one core, across everyone
)
```

Budgets ration **work, not calls**, and that is the point: a flat call limit has
to be either wrong for the cheap input or wrong for the expensive one. Charging
each request its real cost leaves exploration effectively unlimited while capping
the expensive tail. (A flat 2-calls-per-minute was tried first and refused this
package's own test suite on the fourth request.)

Ceilings and budgets are published in the manifest, so a buyer reads them before
spending a call to discover them. Who may exceed a ceiling is a deployment
question, not a code one — `ORACLE_PAID_TIER_SECRET` or
`ORACLE_TRUSTED_PAYMENT_PROXIES`, both unset by default, which means nothing is
ever lifted until an operator says so. A loopback caller is never a payment proxy:
behind a proxy that passes no `X-Real-IP` every caller would be 127.0.0.1. Full rationale and the measured numbers:
`oracle_core/tiers.py` and `docs/free-and-paid-tiers.md` in the monorepo.

## Price gate

A price in the manifest is a promise only if something enforces it. This core takes no
payment itself; the hub that sells your capabilities does. But your invoke URL is
public, and until 0.5.0 a caller who skipped the hub got every priced capability for
free (seen on GAIA and EMPORION, 2026-10-16).

With the gate on, a capability priced above zero is served only to:

- a call a hub you trust signed (it took the payment, or chose to give a trial), or
- a caller you nominated with `ORACLE_PAID_TIER_SECRET` (your own services).

Anyone else gets `402` with `payment_ways` naming the hubs that sell it. A signature
that does not hold (unknown hub, wrong key, stale, other capability or input, other
provider, replayed) gets `401` and the reason. Free capabilities are never gated.

| variable | meaning |
|---|---|
| `ORACLE_TRUSTED_HUBS` | `url=key,url=key`: each hub's URL and its Ed25519 `signer_public_key` from its `/.well-known/ai-market.json`. Repeat a URL to accept two keys during a rotation. |
| `ORACLE_PRICE_GATE` | `enforce`, `report` or `off`. Unset: `enforce` if a hub is trusted, `off` if none is. `report` serves everything and logs what it would refuse; run it on production before `enforce`. Anything else reads as `enforce`. |
| `ORACLE_HUB_AUDIENCES` | Extra base URLs you answer at, for a hub that reaches you under another name than `public_url`. |
| `ORACLE_HUB_MAX_SKEW_S` | Clock tolerance for the hub's timestamp, default 120. |

Behind your own hub, pin your own hub. The hub side (`aimarket_hub/provider_request_auth.py`)
signs local listings always and federated peers only when it is their seller of record
(`AIMARKET_SELLS_FOR`). The wire format is in `oracle_core/price_gate.py`, and both sides
pin the same test vector.

## Call log

Since 0.6.0 every invoke can be written down — served, refused by the price gate, over a limit,
failed — as one row in SQLite (default) or Postgres: capability, outcome, HTTP status, who vouched
for the call, the price of a served priced call, latency, a salted hash of the client address and
the SHA-256 of the input. Never the input itself and never an IP address; an error message that
quotes the input is scrubbed first. The operator reads `/ops/v1/stats`, `/ops/v1/calls` and
`/ops/v1/metrics` (Prometheus) with `ORACLE_OPS_TOKEN`.

| variable | meaning |
|---|---|
| `ORACLE_CALLS` | `1` keeps the log (off by default in the library) |
| `ORACLE_DATA_DIR` | where the SQLite file and the hash salt live |
| `ORACLE_DATABASE_URL` | `postgresql://…` for Postgres (extra `postgres`) |
| `ORACLE_CALLS_RETENTION_DAYS` | days to keep rows, default 90 |
| `ORACLE_OPS_TOKEN` | Bearer token of `/ops/v1/*`; without it they answer 503 |

Details, in five languages: [docs/CALL-LOG.md](docs/CALL-LOG.md).

## Extras

- `pqc` — hybrid post-quantum signatures (`dilithium-py`)
- `postgres` — the call log (and other stores) on Postgres (`psycopg` 3)
- `dev` — pytest, pytest-asyncio, httpx

Apache-2.0.
