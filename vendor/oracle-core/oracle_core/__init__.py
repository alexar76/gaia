"""oracle-core — shared AIMarket v2 infrastructure for the oracle family.

Build an oracle by declaring capabilities and handing them to ``create_app``:

    from oracle_core import Capability, OracleSpec, create_app

    spec = OracleSpec(
        name="My Oracle", product_id="prod-x", description="...",
        public_url="http://localhost:9300", categories=["..."],
        capabilities=[Capability("x.do@v1", "does x", handler=lambda d: {...})],
    )
    app = create_app(spec)

You get signed manifest + invoke (with receipts + measured metrics) + .well-known
+ rate-limiting + (optional) hybrid PQC for free.
"""

from oracle_core.app import create_app
from oracle_core.hub_client import HubClient
from oracle_core.metrics import Metrics
from oracle_core.protocol import Capability, OracleSpec, Protocol, input_hash, utc_now_z
from oracle_core.ratelimit import RateLimiter
from oracle_core.signing import Signer, pqc_available

__all__ = [
    "Capability",
    "OracleSpec",
    "Protocol",
    "create_app",
    "HubClient",
    "Metrics",
    "RateLimiter",
    "Signer",
    "pqc_available",
    "input_hash",
    "utc_now_z",
]
