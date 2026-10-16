"""Priced capabilities answer a call a trusted hub admitted, or 402.

Every provider on this core published a price and served every caller for it. The hub
holds and captures that price for a peer it sells for (``AIMARKET_SELLS_FOR``), but the
hub's 402 was the only door, and the provider's own URL is public. Measured on
2026-10-16: a plain ``POST https://iot.modelmarket.dev/ai-market/v2/invoke`` for
``gaia.power.window@v1`` and the same against EMPORION for ``emporion.vat.check@v1``
($0.002 each) answered ``ok: true`` with the full output and no payment, while the
same calls through https://modelmarket.dev answered 402 at the catalogue price.

ATLAS closed the same hole by metering itself (``atlas/payment_gate.py``: an allowance
per caller, a credit ledger, a 402). That works for one service. Repeated in every
oracle it would mean a ledger per provider for money that already has one at the hub.
So this module does not take payment. It lets a provider tell a call a hub has
admitted from one that walked around the hub, and refuses the second kind for a priced
capability.

What a hub sends (v1)
---------------------
A hub that admits a call (it took the payment, or chose to give a trial) signs it with
its federation key, the one in its ``/.well-known/ai-market.json`` as
``signer_public_key``::

    X-AIMarket-Hub-Signer     the hub's URL, as pinned here
    X-AIMarket-Hub-Audience   the invoke URL the hub posted to
    X-AIMarket-Hub-Timestamp  unix seconds
    X-AIMarket-Hub-Nonce      32 hex characters, fresh per call
    X-AIMarket-Hub-Signature  Ed25519 over :func:`request_canonical`, base64

The canonical binds the capability and a hash of the exact input, so a signature for
one call does not buy a different one, and the audience, so a signature sent to one
provider does not buy the same call at another provider that trusts the same hub. The
``aimarket-hub-request/v1`` prefix keeps it from colliding with anything else the hub
signs with that key: receipts, manifests, objects and events all start with a field
name or ``{``.

What the provider does
----------------------
For a capability priced above zero, unless the caller is an operator-nominated paid
caller (``ORACLE_PAID_TIER_SECRET`` and friends, see :mod:`oracle_core.tiers`):

    no signature           402, naming the hubs that sell it (``payment_ways``)
    signature that fails   401 with the reason (unknown hub, stale, wrong audience,
                           wrong capability or input, replayed nonce)
    signature that holds   served

A hub maps a provider's 401 to 502 and releases the buyer's hold, so a broken
signature costs the buyer nothing. Free capabilities are never gated.

Configuration
-------------
``ORACLE_TRUSTED_HUBS``
    ``url=key,url=key``: the hubs whose admission this provider accepts, each with its
    pinned Ed25519 public key (base64, from that hub's well-known). A URL may appear
    more than once, for a key rotation. A third-party operator pins its own hub here.
``ORACLE_PRICE_GATE``
    ``enforce``, ``report`` or ``off``. Unset, it is ``enforce`` when a hub is trusted and
    ``off`` when none is, which is what this core did before. ``report`` checks every
    priced call and logs what it would have refused, without refusing: the step to run
    on production before ``enforce``. Any other value is read as ``enforce``.
``ORACLE_HUB_AUDIENCES``
    Extra base URLs this provider answers at, beyond its ``public_url``, for a hub that
    reaches it under another name.
``ORACLE_HUB_MAX_SKEW_S``
    How far a timestamp may be from this host's clock, 120 s by default.

Limits, stated rather than hidden: replay protection is per process (the nonce cache
is in memory), so a provider run with several workers accepts a replay once per
worker within the skew window. A trusted hub is trusted with its whole policy: what it
gives away as a trial, it gives away from this provider.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import heapq
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from oracle_core.signing import Signer, has_small_order


log = logging.getLogger(__name__)

DOMAIN = "aimarket-hub-request/v1"
INVOKE_PATH = "/ai-market/v2/invoke"

HUB_HEADER = "X-AIMarket-Hub-Signer"
AUDIENCE_HEADER = "X-AIMarket-Hub-Audience"
TIMESTAMP_HEADER = "X-AIMarket-Hub-Timestamp"
NONCE_HEADER = "X-AIMarket-Hub-Nonce"
SIGNATURE_HEADER = "X-AIMarket-Hub-Signature"

TRUSTED_HUBS_ENV = "ORACLE_TRUSTED_HUBS"
MODE_ENV = "ORACLE_PRICE_GATE"
AUDIENCES_ENV = "ORACLE_HUB_AUDIENCES"
SKEW_ENV = "ORACLE_HUB_MAX_SKEW_S"

DEFAULT_MAX_SKEW_S = 120
#: Far more than the nonces that can be live at once: a hub would have to sustain
#: ~400 admitted calls a second to this one provider to fill it inside the window.
MAX_NONCES = 100_000

_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_OFF = {"off", "0", "false", "no", "disabled"}
_REPORT = {"report", "shadow", "log", "dry-run", "dryrun"}
_ENFORCE = {"enforce", "enforced", "on", "1", "true", "yes"}


# ── The wire format, shared with the hub (aimarket_hub/provider_request_auth.py) ─────


def normalize_url(url: str) -> str:
    """``scheme://host[:port]/path``: lowercased, default port and trailing slash dropped.

    Both sides compare URLs only after this, so ``https://Hub.example/`` and
    ``https://hub.example:443`` are one hub. Query and fragment are dropped: nothing a
    hub or an invoke endpoint is named by lives there. Unparseable input gives "".
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    if port is not None and (scheme, port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}{parts.path.rstrip('/')}"


def input_sha256(input_payload: Any) -> str:
    """The same digest the X-Provider-Signature handshake binds (sorted, compact JSON)."""
    raw = json.dumps(input_payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_canonical(
    *, hub: str, audience: str, capability_id: str, input_digest: str, timestamp: int, nonce: str,
) -> str:
    body = json.dumps(
        {
            "aud": normalize_url(audience),
            "capability_id": capability_id,
            "hub": normalize_url(hub),
            "input_sha256": input_digest,
            "nonce": nonce,
            "ts": int(timestamp),
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return f"{DOMAIN}\n{body}"


def sign_request(
    sign_canonical: Callable[[str], str],
    *,
    hub_url: str,
    audience: str,
    capability_id: str,
    input_payload: Any,
    now: float | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """The headers a hub sends with a call it admitted. ``sign_canonical`` is its signer's."""
    ts = int(time.time() if now is None else now)
    nonce = nonce or secrets.token_hex(16)
    hub, aud = normalize_url(hub_url), normalize_url(audience)
    canonical = request_canonical(
        hub=hub, audience=aud, capability_id=capability_id,
        input_digest=input_sha256(input_payload), timestamp=ts, nonce=nonce,
    )
    return {
        HUB_HEADER: hub,
        AUDIENCE_HEADER: aud,
        TIMESTAMP_HEADER: str(ts),
        NONCE_HEADER: nonce,
        SIGNATURE_HEADER: sign_canonical(canonical),
    }


# ── Configuration ─────────────────────────────────────────────────────────────────


def _usable_key(key_b64: str) -> bool:
    try:
        raw = base64.b64decode(key_b64, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) == 32 and not has_small_order(raw)


def parse_trusted_hubs(raw: str) -> dict[str, tuple[str, ...]]:
    """``url=key,url=key`` → {normalized url: keys}. The key's own ``=`` padding survives:
    the split is on the FIRST ``=``, and a URL has none. A malformed entry is dropped and
    logged; it never widens the list and never breaks the entries around it."""
    out: dict[str, list[str]] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        url, sep, key = chunk.partition("=")
        hub, key = normalize_url(url), key.strip()
        if not sep or not hub or not _usable_key(key):
            log.error("%s: ignoring an entry that is not url=<ed25519 key, base64>: %r",
                      TRUSTED_HUBS_ENV, url.strip()[:80])
            continue
        keys = out.setdefault(hub, [])
        if key not in keys:
            keys.append(key)
    return {hub: tuple(keys) for hub, keys in out.items()}


def resolve_mode(raw: str | None, *, has_hubs: bool) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return "enforce" if has_hubs else "off"
    if value in _OFF:
        return "off"
    if value in _REPORT:
        return "report"
    if value not in _ENFORCE:
        # Fail closed. A typo in the one setting that decides whether priced work is
        # given away must not give it away.
        log.error("%s=%r is not off/report/enforce; enforcing", MODE_ENV, raw)
    return "enforce"


def invoke_audience(base_url: str) -> str:
    url = normalize_url(base_url)
    if not url:
        return ""
    return url if url.endswith(INVOKE_PATH) else f"{url}{INVOKE_PATH}"


# ── Verification ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    status: str  # "absent" | "valid" | "invalid" | "busy"
    reason: str = ""
    hub: str = ""


class HubRequestVerifier:
    """Checks the five headers against the pinned hubs; remembers nonces for the window."""

    def __init__(
        self,
        trusted: Mapping[str, Sequence[str]],
        audiences: Sequence[str],
        *,
        max_skew_s: int = DEFAULT_MAX_SKEW_S,
        max_nonces: int = MAX_NONCES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.trusted = {normalize_url(h): tuple(k) for h, k in trusted.items() if normalize_url(h)}
        self.audiences = frozenset(a for a in (invoke_audience(x) for x in audiences) if a)
        self.max_skew_s = max(1, int(max_skew_s))
        self.max_nonces = max(1, int(max_nonces))
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str]] = set()
        self._expiry: list[tuple[float, tuple[str, str]]] = []

    def verify(self, headers: Mapping[str, str], capability_id: str, input_payload: Any) -> Verdict:
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        signature = h.get(SIGNATURE_HEADER.lower(), "").strip()
        if not signature:
            return Verdict("absent")
        hub = normalize_url(h.get(HUB_HEADER.lower(), ""))
        keys = self.trusted.get(hub)
        if not keys:
            return Verdict("invalid", f"hub {hub or '(none named)'} is not one this provider trusts", hub)
        try:
            ts = int(h.get(TIMESTAMP_HEADER.lower(), "").strip())
        except ValueError:
            return Verdict("invalid", "timestamp is not unix seconds", hub)
        now = self._clock()
        if abs(now - ts) > self.max_skew_s:
            return Verdict(
                "invalid",
                f"timestamp is {int(now - ts)} s from this provider's clock (limit ±{self.max_skew_s} s)",
                hub,
            )
        nonce = h.get(NONCE_HEADER.lower(), "").strip().lower()
        if not _NONCE_RE.fullmatch(nonce):
            return Verdict("invalid", "nonce must be 32 hex characters", hub)
        audience = normalize_url(h.get(AUDIENCE_HEADER.lower(), ""))
        if audience not in self.audiences:
            return Verdict(
                "invalid",
                f"signed for {audience or '(no audience)'}; this provider answers at "
                f"{', '.join(sorted(self.audiences)) or '(no public_url)'}",
                hub,
            )
        canonical = request_canonical(
            hub=hub, audience=audience, capability_id=capability_id,
            input_digest=input_sha256(input_payload), timestamp=ts, nonce=nonce,
        )
        if not any(Signer.verify(canonical, signature, key) for key in keys):
            return Verdict("invalid", "signature does not verify for this hub, capability and input", hub)
        # Only a call that verified may take a slot, so unsigned junk cannot fill the cache.
        with self._lock:
            while self._expiry and self._expiry[0][0] < now:
                _, old = heapq.heappop(self._expiry)
                self._seen.discard(old)
            slot = (hub, nonce)
            if slot in self._seen:
                return Verdict("invalid", "replayed: this nonce was already used", hub)
            if len(self._seen) >= self.max_nonces:
                return Verdict("busy", "replay cache is full; retry shortly", hub)
            self._seen.add(slot)
            heapq.heappush(self._expiry, (ts + self.max_skew_s, slot))
        return Verdict("valid", hub=hub)


# ── The gate ─────────────────────────────────────────────────────────────────────


class PriceGate:
    """Decides, per priced invoke, between serving it and answering 402/401."""

    #: How often report mode summarises what it saw, in seconds.
    REPORT_EVERY_S = 60.0

    def __init__(
        self,
        mode: str,
        verifier: HubRequestVerifier,
        *,
        source_url: str = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.mode = mode
        self.verifier = verifier
        self.source_url = normalize_url(source_url)
        self._clock = clock
        self._counts: Counter[str] = Counter()
        self._absent_by_caller: Counter[str] = Counter()
        self._last_report = clock()

    @classmethod
    def from_env(cls, public_url: str, env: Mapping[str, str] | None = None) -> "PriceGate":
        src = os.environ if env is None else env
        trusted = parse_trusted_hubs(src.get(TRUSTED_HUBS_ENV) or "")
        mode = resolve_mode(src.get(MODE_ENV), has_hubs=bool(trusted))
        extra = [a.strip() for a in (src.get(AUDIENCES_ENV) or "").split(",") if a.strip()]
        try:
            skew = int((src.get(SKEW_ENV) or "").strip() or DEFAULT_MAX_SKEW_S)
        except ValueError:
            skew = DEFAULT_MAX_SKEW_S
        verifier = HubRequestVerifier(trusted, [public_url, *extra], max_skew_s=skew)
        if mode == "enforce" and not trusted:
            log.warning(
                "price gate: enforcing with no %s, so no hub can buy a priced capability here; "
                "only operator-nominated paid callers are served", TRUSTED_HUBS_ENV)
        return cls(mode, verifier, source_url=public_url)

    @property
    def hubs(self) -> list[str]:
        return sorted(self.verifier.trusted)

    def describe(self) -> dict[str, Any]:
        """For the well-known: whether priced calls are gated, and where to buy them."""
        return {
            "mode": self.mode,
            "hubs": self.hubs,
            "scheme": DOMAIN,
        }

    def payment_required(self, capability_id: str, price: float) -> dict[str, Any]:
        ways = [
            {
                "rail": "hub",
                "hub_url": hub,
                "invoke_url": f"{hub}{INVOKE_PATH}",
                "source_hub": self.source_url,
            }
            for hub in self.hubs
        ]
        where = (
            "through a hub that sells it: " + ", ".join(self.hubs)
            if ways else "through a hub; this provider names none"
        )
        return {
            "ok": False,
            "error": "payment_required",
            "detail": (
                f"{capability_id} is priced at ${price:g} per call, and this provider does not "
                f"take payment itself. Invoke it {where}, with source_hub={self.source_url or '(this provider)'}. "
                "A hub's free trial covers a few calls."
            ),
            "capability_id": capability_id,
            "price_usd": price,
            "needed": price,
            "payment_ways": ways,
            "protocol_version": "v2",
        }

    def check(
        self,
        capability_id: str,
        price: float,
        input_payload: Any,
        headers: Mapping[str, str],
        *,
        paid: bool = False,
        caller: str = "",
        note: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]] | None:
        """``None`` to serve the call, or ``(status, body)`` to refuse it. ``note``, when
        given, receives the verdict (``status``, ``hub``) — the call log's "who called",
        read once here because verifying again would find the nonce spent."""
        if self.mode == "off" or price <= 0 or paid:
            return None
        verdict = self.verifier.verify(headers, capability_id, input_payload)
        if note is not None:
            note["status"], note["hub"] = verdict.status, verdict.hub
        if self.mode == "report":
            self._note(verdict, capability_id, caller)
            return None
        if verdict.status == "valid":
            return None
        if verdict.status == "absent":
            return 402, self.payment_required(capability_id, price)
        if verdict.status == "busy":
            return 429, {"ok": False, "error": "rate_limited", "detail": verdict.reason}
        log.warning("price gate: refused %s from %s (hub %s): %s",
                    capability_id, caller or "?", verdict.hub or "-", verdict.reason)
        return 401, {
            "ok": False,
            "error": "hub_signature_invalid",
            "detail": verdict.reason,
            "capability_id": capability_id,
            "protocol_version": "v2",
        }

    def _note(self, verdict: Verdict, capability_id: str, caller: str) -> None:
        """Report mode: count, log each bad signature, summarise the rest once a minute."""
        self._counts[verdict.status] += 1
        if verdict.status == "absent":
            self._absent_by_caller[caller or "?"] += 1
        elif verdict.status != "valid":
            log.warning("price gate (report): would refuse %s from %s (hub %s): %s",
                        capability_id, caller or "?", verdict.hub or "-", verdict.reason)
        now = self._clock()
        if now - self._last_report < self.REPORT_EVERY_S:
            return
        top = ", ".join(f"{c} x{n}" for c, n in self._absent_by_caller.most_common(5)) or "-"
        log.warning(
            "price gate (report): last %.0f s, priced calls: valid=%d unsigned=%d invalid=%d busy=%d; "
            "unsigned by caller: %s",
            now - self._last_report, self._counts["valid"], self._counts["absent"],
            self._counts["invalid"], self._counts["busy"], top,
        )
        self._counts.clear()
        self._absent_by_caller.clear()
        self._last_report = now


__all__ = [
    "DOMAIN",
    "HubRequestVerifier",
    "PriceGate",
    "Verdict",
    "input_sha256",
    "normalize_url",
    "parse_trusted_hubs",
    "request_canonical",
    "resolve_mode",
    "sign_request",
]
