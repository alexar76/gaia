"""Who commands an actuator through a hub: whom a lease and ownership are checked against.

Payment proves payment, not permission. Before this, gaia.actuate@v1 switched any listed
plug for whoever reached it: buyer A paid $0.05 for "on, 900 s", and 30 s later anyone
else sent "off" (paid, through a hub's free trial, or straight at GAIA) and A's paid time
ended early. To refuse that, GAIA has to know who is asking, and it can only rely on an
identity somebody it trusts has checked.

What GAIA trusts
----------------
On the paid path, a payer that a trusted hub vouches for, and nothing else (the owner's own
free path is an EIP-712 signature, gaia/owner_auth.py). The hub knows who paid for the
call it admitted: the payment channel's payer (the address recovered from the signed
challenge when the channel was opened) or the x402 payer (``authorization.from``, checked
against its EIP-712 signature). The hub signs that payer with its federation key, together
with this exact call. The trusted hubs are the ones GAIA's price gate already pins
(``ORACLE_TRUSTED_HUBS``, see oracle_core.price_gate), so one pin covers both.

A caller's own claims are never an identity. An input field, ``X-Payment-Channel`` (a
channel id is a reference, not a secret), ``X-AIMarket-Buyer`` (an opaque rate-limit key)
and anything else unsigned can be typed by anyone who reaches GAIA. A call without a
vouched payer is anonymous. It may still command the public (simulated demo) actuator, but
it never matches a lease holder, an owner or a delegate.

The statement (aimarket-hub-payer/v1)
-------------------------------------
It rides next to the five price-gate headers of the same call and reuses four of them
(signer, audience, timestamp, nonce), so a payer statement belongs to exactly one
admitted call::

    X-AIMarket-Hub-Payer            the payer: 0x + 40 hex (EVM, lowercased) or base58 (Solana)
    X-AIMarket-Hub-Payer-Rail       channel | x402
    X-AIMarket-Hub-Payer-Signature  Ed25519 by the hub's key over :func:`payer_canonical`

The canonical binds the hub, the audience (GAIA's invoke URL), the capability, a hash of
the exact input, the time, the nonce, the payer and the rail. A statement for one call does
not name a payer on another call, at another provider, or a minute later. The
``aimarket-hub-payer/v1`` prefix keeps it apart from the hub's admission signature
(``aimarket-hub-request/v1``) and from everything else that key signs. The hub sends no
statement when it has no payer (a trial, demo credit).

A statement that is present and fails (unknown hub, stale, wrong audience, other call,
replayed nonce, a payer that is not an address) refuses the command, ok:false and nothing
charged. It is never quietly treated as anonymous: that would hide a broken hub or a
forgery behind a working call.

When it is checked: only when the capability's handler asks, which is after oracle-core's
price gate has accepted the call's admission headers. A call the gate refuses (402, 401)
never reaches the check and never takes a nonce from this module's replay cache. That cache
is GAIA's own: the gate spends the same (hub, nonce) slot in its separate cache.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import hashlib
import heapq
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from oracle_core.signing import Signer, has_small_order

log = logging.getLogger("gaia.payer_identity")

DOMAIN = "aimarket-hub-payer/v1"
INVOKE_PATH = "/ai-market/v2/invoke"

# Shared with the hub's admission signature (oracle_core.price_gate): the same call.
HUB_HEADER = "X-AIMarket-Hub-Signer"
AUDIENCE_HEADER = "X-AIMarket-Hub-Audience"
TIMESTAMP_HEADER = "X-AIMarket-Hub-Timestamp"
NONCE_HEADER = "X-AIMarket-Hub-Nonce"
# This statement's own.
PAYER_HEADER = "X-AIMarket-Hub-Payer"
RAIL_HEADER = "X-AIMarket-Hub-Payer-Rail"
PAYER_SIGNATURE_HEADER = "X-AIMarket-Hub-Payer-Signature"

RAILS = ("channel", "x402")
TRUSTED_HUBS_ENV = "ORACLE_TRUSTED_HUBS"
AUDIENCES_ENV = "ORACLE_HUB_AUDIENCES"
SKEW_ENV = "ORACLE_HUB_MAX_SKEW_S"
DEFAULT_MAX_SKEW_S = 120
MAX_NONCES = 100_000

_EVM = re.compile(r"0x[0-9a-fA-F]{40}")
# '0' is not in the base58 alphabet, so an EVM address can never also match this.
_BASE58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
_NONCE = re.compile(r"[0-9a-f]{32}")


def normalize_identity(raw: Any) -> str:
    """A wallet address in one spelling: EVM lowercased (EIP-55 case is a checksum, not a
    different address), base58 exactly as written (its case is significant)."""
    s = str(raw if raw is not None else "").strip()
    if _EVM.fullmatch(s):
        return s.lower()
    if _BASE58.fullmatch(s):
        return s
    raise ValueError(f"{s[:60]!r} is not a wallet address (0x + 40 hex, or base58)")


def short(identity: str) -> str:
    """For messages: enough to recognise your own address, not a full one to copy around."""
    return identity if len(identity) <= 14 else f"{identity[:6]}…{identity[-4:]}"


@dataclass(frozen=True)
class Caller:
    """An identity GAIA was able to check. ``vouched_by`` says who checked it."""

    id: str
    vouched_by: str
    rail: str = ""

    def journal(self) -> dict[str, str]:
        return {"id": self.id, "vouched_by": self.vouched_by, "rail": self.rail}


@dataclass(frozen=True)
class Resolution:
    """What the request said about its caller: a Caller, anonymous (both empty), or a
    statement that failed (``problem``), which refuses the command."""

    caller: Caller | None = None
    problem: str = ""


ANONYMOUS = Resolution()


# ── The wire format ───────────────────────────────────────────────────────────


def normalize_url(url: str) -> str:
    """The rule oracle_core.price_gate.normalize_url applies; both compare after it."""
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
    """The digest the price gate and the X-Provider-Signature handshake bind."""
    raw = json.dumps(input_payload or {}, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def payer_canonical(*, hub: str, audience: str, capability_id: str, input_digest: str,
                    timestamp: int, nonce: str, payer: str, rail: str) -> str:
    """What the hub's key signs. URLs and the payer are normalized here, so two sides
    that spell an address or a host differently still sign and check the same bytes."""
    body = json.dumps(
        {"aud": normalize_url(audience), "capability_id": capability_id,
         "hub": normalize_url(hub), "input_sha256": input_digest, "nonce": nonce,
         "payer": normalize_identity(payer), "rail": rail, "ts": int(timestamp)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{DOMAIN}\n{body}"


def sign_payer(sign_canonical: Callable[[str], str], *, hub_url: str, audience: str,
               capability_id: str, input_payload: Any, payer: str, rail: str,
               timestamp: int, nonce: str) -> dict[str, str]:
    """The three statement headers a hub adds, for the ``timestamp`` and ``nonce`` it
    already put on the admission headers. ``sign_canonical`` is the hub signer's."""
    who = normalize_identity(payer)
    if rail not in RAILS:
        raise ValueError(f"rail must be one of {', '.join(RAILS)}")
    canonical = payer_canonical(
        hub=hub_url, audience=audience, capability_id=capability_id,
        input_digest=input_sha256(input_payload), timestamp=timestamp, nonce=nonce,
        payer=who, rail=rail)
    return {PAYER_HEADER: who, RAIL_HEADER: rail,
            PAYER_SIGNATURE_HEADER: sign_canonical(canonical)}


# ── Configuration (the same variables the price gate reads) ───────────────────


def _usable_key(key_b64: str) -> bool:
    try:
        raw = base64.b64decode(key_b64, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) == 32 and not has_small_order(raw)


def parse_trusted_hubs(raw: str) -> dict[str, tuple[str, ...]]:
    """``url=key,url=key`` → {normalized url: keys}; a malformed entry is dropped."""
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


def _invoke_audience(base_url: str) -> str:
    url = normalize_url(base_url)
    if not url:
        return ""
    return url if url.endswith(INVOKE_PATH) else f"{url}{INVOKE_PATH}"


# ── Verification ──────────────────────────────────────────────────────────────


class PayerVerifier:
    """Checks a payer statement against the pinned hubs; remembers nonces for the window."""

    def __init__(self, trusted: Mapping[str, Sequence[str]], audiences: Sequence[str], *,
                 max_skew_s: int = DEFAULT_MAX_SKEW_S, max_nonces: int = MAX_NONCES,
                 clock: Callable[[], float] = time.time):
        self.trusted = {normalize_url(h): tuple(k) for h, k in trusted.items()
                        if normalize_url(h)}
        self.audiences = frozenset(a for a in (_invoke_audience(x) for x in audiences) if a)
        self.max_skew_s = max(1, int(max_skew_s))
        self.max_nonces = max(1, int(max_nonces))
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str]] = set()
        self._expiry: list[tuple[float, tuple[str, str]]] = []

    @classmethod
    def from_env(cls, public_url: str, env: Mapping[str, str] | None = None) -> "PayerVerifier":
        src = os.environ if env is None else env
        extra = [a.strip() for a in (src.get(AUDIENCES_ENV) or "").split(",") if a.strip()]
        try:
            skew = int((src.get(SKEW_ENV) or "").strip() or DEFAULT_MAX_SKEW_S)
        except ValueError:
            skew = DEFAULT_MAX_SKEW_S
        return cls(parse_trusted_hubs(src.get(TRUSTED_HUBS_ENV) or ""),
                   [public_url, *extra], max_skew_s=skew)

    def resolve(self, headers: Mapping[str, str], capability_id: str,
                input_payload: Any) -> Resolution:
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        signature = h.get(PAYER_SIGNATURE_HEADER.lower(), "").strip()
        if not signature:
            return ANONYMOUS

        def bad(reason: str) -> Resolution:
            log.warning("payer statement refused for %s: %s", capability_id, reason)
            return Resolution(problem=f"payer statement refused: {reason}; nothing charged")

        hub = normalize_url(h.get(HUB_HEADER.lower(), ""))
        keys = self.trusted.get(hub)
        if not keys:
            return bad(f"hub {hub or '(none named)'} is not one this GAIA trusts")
        try:
            ts = int(h.get(TIMESTAMP_HEADER.lower(), "").strip())
        except ValueError:
            return bad("timestamp is not unix seconds")
        now = self._clock()
        if abs(now - ts) > self.max_skew_s:
            return bad(f"timestamp is {int(now - ts)} s from this GAIA's clock "
                       f"(limit ±{self.max_skew_s} s)")
        nonce = h.get(NONCE_HEADER.lower(), "").strip().lower()
        if not _NONCE.fullmatch(nonce):
            return bad("nonce must be 32 hex characters")
        audience = normalize_url(h.get(AUDIENCE_HEADER.lower(), ""))
        if audience not in self.audiences:
            return bad(f"signed for {audience or '(no audience)'}, not this GAIA")
        rail = h.get(RAIL_HEADER.lower(), "").strip()
        if rail not in RAILS:
            return bad(f"rail must be one of {', '.join(RAILS)}")
        try:
            payer = normalize_identity(h.get(PAYER_HEADER.lower(), ""))
        except ValueError as exc:
            return bad(str(exc))
        canonical = payer_canonical(
            hub=hub, audience=audience, capability_id=capability_id,
            input_digest=input_sha256(input_payload), timestamp=ts, nonce=nonce,
            payer=payer, rail=rail)
        if not any(Signer.verify(canonical, signature, key) for key in keys):
            return bad("signature does not verify for this hub, call and payer")
        with self._lock:
            while self._expiry and self._expiry[0][0] < now:
                _, old = heapq.heappop(self._expiry)
                self._seen.discard(old)
            slot = (hub, nonce)
            if slot in self._seen:
                return bad("replayed: this nonce was already used")
            if len(self._seen) >= self.max_nonces:
                return bad("replay cache is full; retry shortly")
            self._seen.add(slot)
            heapq.heappush(self._expiry, (ts + self.max_skew_s, slot))
        return Resolution(Caller(payer, vouched_by=hub, rail=rail))


# ── Carrying the statement from the HTTP request to the capability handler ────


class _Deferred:
    """One request's statement, checked the first time the handler asks for it."""

    def __init__(self, verifier: PayerVerifier, headers: Mapping[str, str],
                 capability_id: str, input_payload: Any):
        self._args = (headers, capability_id, input_payload)
        self._verifier = verifier
        self._lock = threading.Lock()
        self._result: Resolution | None = None

    def resolve(self) -> Resolution:
        with self._lock:
            if self._result is None:
                self._result = self._verifier.resolve(*self._args)
            return self._result


_current: contextvars.ContextVar[_Deferred | None] = contextvars.ContextVar(
    "gaia_payer_statement", default=None)


def current() -> Resolution:
    """The resolution for the invoke being handled, checked now (once per request).
    oracle-core runs sync handlers through asyncio.to_thread, which copies the context, so
    the handler sees what the middleware set for its own request and no other."""
    pending = _current.get()
    return ANONYMOUS if pending is None else pending.resolve()


class PayerIdentityMiddleware:
    """Hands an invoke's payer statement to the handler of a capability that needs one.

    Pure ASGI, like ProviderSignatureMiddleware: it buffers the body (the statement binds
    the input's hash), sets the statement for this request only, and replays the body. It
    checks nothing itself; :func:`current` does, when the handler asks. Other paths and other
    capabilities pass through untouched and stay anonymous.
    """

    def __init__(self, app, verifier: PayerVerifier, capabilities: Sequence[str]):
        self.app = app
        self.verifier = verifier
        self.capabilities = frozenset(capabilities)

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("path") != INVOKE_PATH
                or scope.get("method") != "POST"):
            return await self.app(scope, receive, send)
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        async def replay():
            return {"type": "http.request", "body": body, "more_body": False}

        pending = None
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            req = None
        if isinstance(req, dict) and str(req.get("capability_id", "")) in self.capabilities:
            headers = {k.decode("latin-1"): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            pending = _Deferred(self.verifier, headers, str(req["capability_id"]),
                                req.get("input"))
        token = _current.set(pending)
        try:
            return await self.app(scope, replay, send)
        finally:
            _current.reset(token)


__all__ = ["ANONYMOUS", "Caller", "DOMAIN", "PayerIdentityMiddleware", "PayerVerifier",
           "Resolution", "current", "input_sha256", "normalize_identity", "normalize_url",
           "parse_trusted_hubs", "payer_canonical", "short", "sign_payer"]
