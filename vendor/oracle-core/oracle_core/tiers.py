"""Free vs paid tier policy — bounds the CPU an unpaid caller can command.

Most capabilities in the family are microseconds of work and are given away
deliberately: the servers run regardless, and a stranger who can try a capability
for free is the cheapest promotion there is.

Two are not. They *sell computation* — a Wesolowski VDF and an RSW time-lock
puzzle are priced in enforced sequential squarings, and both are sequential by
construction, so a single call pins a whole core for its whole duration and no
amount of hardware parallelises it away:

    aestus.seal@v1   T = 5_000_000 (MAX_T)          ~36 s   (linear in T)
    chronos.eval@v1  difficulty = 1_000_000          6.8 s

The generic per-IP limiter admits 120 invokes/min, so one anonymous client with
one address can lawfully demand ~70 CPU-seconds per second of wall clock from a
single machine that serves the whole family. That is not lost revenue — it is a
capacity failure, and it needs no malice: a caller who reads the schema, sees
``maximum: 5000000`` and loops is doing exactly what the manifest invites.

So the ceiling on *work* is separated from the price of the call. An unpaid call
gets a demo-sized bound — large enough that the mechanism is fully visible and
the proof verifies, small enough that a loop of them costs a fraction of a core.
The full range is what payment buys.

Four knobs, all declared per capability (see :class:`oracle_core.Capability`):

    free_tier_max                   {input field: ceiling for unpaid calls}; a dotted key
                                    reaches into a nested object ("puzzle.T")
    cost_ms                         callable: expected CPU milliseconds for a given input
    cpu_budget_ms_per_min           CPU-ms one client may spend here per minute
    global_cpu_budget_ms_per_min    the same, summed over ALL clients

The budgets ration *work*, not calls, and that is the point: a flat call limit has to be
either wrong for the cheap input or wrong for the expensive one, since the two differ by
four orders of magnitude here. Charging each request its real cost leaves exploration at
low difficulty effectively unlimited while capping the expensive tail.

Refuse, never silently clamp
----------------------------
An unpaid call over the ceiling is REFUSED with 402 and the ceiling in the body.
Quietly doing less work than asked would mint a receipt attesting a difficulty
the caller did not request, and a caller timing the response would reasonably
conclude the oracle cheats. (Chronos and Aestus still clamp internally against
their own hard maxima — that is a different guard, against input above the
*declared schema*, and it stays.)

What counts as paid
-------------------
An oracle cannot verify a payment channel by itself: channels live in the hub's
ledger, and channel ids travel in receipts, so mere presence of an id proves
nothing. Rather than couple every invoke to a hub round trip, the lift is granted
only to a caller the operator has explicitly nominated, by one of two means:

``ORACLE_PAID_TIER_SECRET``
    A shared secret sent as ``X-AIMarket-Paid-Tier``. Strongest, and independent
    of network topology — prefer it.

``ORACLE_TRUSTED_PAYMENT_PROXIES``
    Comma-separated IPs/CIDRs. A request from one of them carrying a non-empty
    ``X-Payment-Channel`` is treated as paid, on the grounds that the hub took a
    hold before forwarding it. This trusts the reverse proxy to set ``X-Real-IP``
    (the same trust the rate limiter already places — see ``app.client_key``);
    operators who cannot guarantee that should use the secret instead.

Neither set is the default, and it means *no call is ever lifted*: the free
ceiling applies to everybody, including the hub. That is the intended default for
a family that currently sells nothing — it fails closed, and turning selling on
is one environment variable per side, not a code change.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


PAID_TIER_HEADER = "x-aimarket-paid-tier"
PAYMENT_CHANNEL_HEADER = "x-payment-channel"

SECRET_ENV = "ORACLE_PAID_TIER_SECRET"
PROXIES_ENV = "ORACLE_TRUSTED_PAYMENT_PROXIES"


class FreeTierExceeded(Exception):
    """An unpaid call asked for more work than the free tier grants.

    Carries the field, the value asked for and the ceiling so the HTTP layer can
    answer 402 with something the caller can act on rather than a bare string.
    """

    def __init__(self, capability_id: str, field: str, requested: int, ceiling: int) -> None:
        self.capability_id = capability_id
        self.field = field
        self.requested = requested
        self.ceiling = ceiling
        super().__init__(
            f"{capability_id}: '{field}'={requested} exceeds the free-tier ceiling of "
            f"{ceiling}. Unpaid calls are bounded because this capability sells "
            f"sequential computation. Send a payment channel for the full range, or "
            f"lower '{field}' to {ceiling} or less."
        )

    def as_body(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "payment_required",
            "detail": str(self),
            "capability_id": self.capability_id,
            "free_tier": {"field": self.field, "requested": self.requested, "max": self.ceiling},
        }


def enforce_free_tier(
    capability_id: str, free_tier_max: Mapping[str, int], input_data: Mapping[str, Any]
) -> None:
    """Raise :class:`FreeTierExceeded` if an unpaid ``input_data`` is over budget.

    Only fields actually present are checked — an absent field takes the schema
    default, and every declared default in the family is at or below its own
    ceiling, so the ordinary "call it with no arguments" path is never refused.
    A field present but not coercible to int is left alone: rejecting malformed
    input is the handler's job and its message is the better one.

    A key may be dotted (``"puzzle.T"``) to reach into a nested object. That is not
    a flourish: ``aestus.open@v1`` takes a whole puzzle and does ``T`` squarings
    where ``T`` is a field *inside* it, so a top-level-only check would bound
    ``seal`` and leave the identical cost wide open one endpoint over.
    """
    for field_name, ceiling in free_tier_max.items():
        found, value = _lookup(input_data, field_name)
        if not found:
            continue
        try:
            requested = int(value)
        except (TypeError, ValueError):
            continue
        if requested > ceiling:
            raise FreeTierExceeded(capability_id, field_name, requested, ceiling)


def _lookup(data: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted path, returning ``(found, value)``. Never raises."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


@dataclass(frozen=True)
class PaidTierPolicy:
    """How this deployment recognises a call that has been paid for."""

    secret: str = ""
    trusted_proxies: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PaidTierPolicy":
        src = os.environ if env is None else env
        return cls(
            secret=(src.get(SECRET_ENV) or "").strip(),
            trusted_proxies=_parse_proxies(src.get(PROXIES_ENV) or ""),
        )

    @property
    def enabled(self) -> bool:
        """True if any call can be lifted at all. False is the shipped default."""
        return bool(self.secret or self.trusted_proxies)

    def is_paid(self, headers: Mapping[str, str], client_ip: str) -> bool:
        """Does this request carry a paid-tier grant this deployment accepts?"""
        lower = {k.lower(): v for k, v in headers.items()}
        if self.secret:
            offered = (lower.get(PAID_TIER_HEADER) or "").strip()
            # Constant-time: the secret is a bearer credential, and an oracle answers
            # fast enough that a length/prefix oracle is worth closing.
            if offered and _secrets_equal(offered, self.secret):
                return True
        if self.trusted_proxies and (lower.get(PAYMENT_CHANNEL_HEADER) or "").strip():
            if _ip_allowed(client_ip, self.trusted_proxies):
                return True
        return False


def _parse_proxies(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _ip_allowed(client_ip: str, allowed: Iterable[str]) -> bool:
    try:
        ip = ipaddress.ip_address(client_ip.strip())
    except ValueError:
        # "*" from the rate-limit fallback, or a hostname — not an address we can
        # place inside a network, so it is not trusted.
        return False
    for entry in allowed:
        try:
            if "/" in entry:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            # A malformed entry must not silently widen the allow-list, and must not
            # break the entries around it either.
            continue
    return False


def _secrets_equal(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
