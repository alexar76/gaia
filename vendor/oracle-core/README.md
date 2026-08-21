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
ever lifted until an operator says so. Full rationale and the measured numbers:
`oracle_core/tiers.py` and `docs/free-and-paid-tiers.md` in the monorepo.

## Extras

- `pqc` — hybrid post-quantum signatures (`dilithium-py`)
- `dev` — pytest, pytest-asyncio, httpx

Apache-2.0.
