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
from oracle_core.tiers import FreeTierExceeded, PaidTierPolicy, enforce_free_tier

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
    "FreeTierExceeded",
    "PaidTierPolicy",
    "enforce_free_tier",
    "input_hash",
    "utc_now_z",
    "__version__",
]

#: Must equal `version` in pyproject.toml — asserted by tests/test_packaging.py. The package
#: had no __version__ at all, so nothing could report which core an oracle was actually running
#: against, and there was no way to catch a bump made in one place and not the other. That is
#: not hypothetical: aimarket-agent shipped 2.1.2 declaring __version__ = "2.1.1", which is why
#: it has a self-consistency test and this now does too.
__version__ = "0.3.0"
