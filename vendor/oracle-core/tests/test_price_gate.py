"""The price gate: a priced capability answers a call a trusted hub signed, or 402.

Found on 2026-10-16: GAIA and SYMMARTYS served $0.002 capabilities to an unpaid direct
POST while the hub that sells them answered 402 for the same call. See
oracle_core/price_gate.py for the wire format and the configuration.
"""

from __future__ import annotations

import base64
import logging

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from oracle_core import Capability, OracleSpec, PaidTierPolicy, create_app
from oracle_core.price_gate import (
    DOMAIN,
    HubRequestVerifier,
    PriceGate,
    input_sha256,
    normalize_url,
    parse_trusted_hubs,
    request_canonical,
    resolve_mode,
    sign_request,
)

HUB = "https://hub.example"
PUBLIC = "https://provider.example/oracle"
AUD = f"{PUBLIC}/ai-market/v2/invoke"


class _HubKey:
    def __init__(self, seed: bytes = b"\x07" * 32):
        self._sk = Ed25519PrivateKey.from_private_bytes(seed)
        raw = self._sk.public_key().public_bytes_raw()
        self.public_b64 = base64.b64encode(raw).decode()

    def sign_canonical(self, canonical: str) -> str:
        return base64.b64encode(self._sk.sign(canonical.encode())).decode()


HUB_KEY = _HubKey()
OTHER_KEY = _HubKey(b"\x09" * 32)


def _spec(tmp_path, public_url=PUBLIC):
    return OracleSpec(
        name="Gate Oracle", product_id="prod-gate", description="test",
        public_url=public_url, categories=["test"],
        signing_key_path=str(tmp_path / "key"),
        capabilities=[
            Capability("gate.priced@v1", "priced", handler=lambda d: {"echo": d.get("msg", "")},
                       price_per_call_usd=0.002),
            Capability("gate.free@v1", "free", handler=lambda d: {"free": True},
                       price_per_call_usd=0.0),
        ],
    )


def _client(tmp_path, env=None, policy=None, public_url=PUBLIC):
    gate = PriceGate.from_env(public_url, env or {})
    app = create_app(_spec(tmp_path, public_url), price_gate=gate,
                     paid_tier=policy or PaidTierPolicy())
    return TestClient(app)


def _enforcing(tmp_path, **kw):
    return _client(tmp_path, {"ORACLE_TRUSTED_HUBS": f"{HUB}={HUB_KEY.public_b64}"}, **kw)


def _signed(capability_id="gate.priced@v1", input_payload=None, *, key=HUB_KEY, hub=HUB,
            audience=AUD, now=None, nonce=None):
    return sign_request(
        key.sign_canonical, hub_url=hub, audience=audience, capability_id=capability_id,
        input_payload=input_payload if input_payload is not None else {"msg": "hi"},
        now=now, nonce=nonce,
    )


def _invoke(c, capability_id="gate.priced@v1", input_payload=None, headers=None):
    return c.post("/ai-market/v2/invoke", headers=headers or {}, json={
        "capability_id": capability_id,
        "input": input_payload if input_payload is not None else {"msg": "hi"},
    })


# ── Off unless configured: what this core did before ──────────────────────────────


def test_off_by_default_serves_priced_calls_and_keeps_the_well_known(tmp_path):
    c = _client(tmp_path)
    r = _invoke(c)
    assert r.status_code == 200 and r.json()["output"] == {"echo": "hi"}
    assert "price_gate" not in c.get("/.well-known/ai-market.json").json()


def test_create_app_reads_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_TRUSTED_HUBS", f"{HUB}={HUB_KEY.public_b64}")
    app = create_app(_spec(tmp_path))
    assert app.state.price_gate.mode == "enforce"
    assert _invoke(TestClient(app)).status_code == 402


# ── Enforce ───────────────────────────────────────────────────────────────────────


def test_unsigned_priced_call_is_402_and_says_where_to_buy(tmp_path):
    r = _invoke(_enforcing(tmp_path))
    assert r.status_code == 402
    body = r.json()
    assert body["error"] == "payment_required" and body["ok"] is False
    assert body["needed"] == body["price_usd"] == 0.002
    assert body["payment_ways"] == [{
        "rail": "hub", "hub_url": HUB, "invoke_url": f"{HUB}/ai-market/v2/invoke",
        "source_hub": PUBLIC,
    }]
    assert "echo" not in r.text


def test_free_capability_stays_open(tmp_path):
    r = _invoke(_enforcing(tmp_path), "gate.free@v1")
    assert r.status_code == 200 and r.json()["output"] == {"free": True}


def test_unknown_capability_is_named_not_402(tmp_path):
    r = _invoke(_enforcing(tmp_path), "gate.nope@v1")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "Unknown capability: gate.nope@v1"}


def test_signed_by_the_trusted_hub_is_served(tmp_path):
    r = _invoke(_enforcing(tmp_path), headers=_signed())
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["output"] == {"echo": "hi"}


def test_well_known_names_the_gate_and_the_hubs(tmp_path):
    doc = _enforcing(tmp_path).get("/.well-known/ai-market.json").json()
    assert doc["price_gate"] == {"mode": "enforce", "hubs": [HUB], "scheme": DOMAIN}


@pytest.mark.parametrize("tamper, reason", [
    (lambda: _signed(key=OTHER_KEY), "does not verify"),
    (lambda: _signed(hub="https://other-hub.example", key=OTHER_KEY), "not one this provider trusts"),
    (lambda: _signed(input_payload={"msg": "something else"}), "does not verify"),
    (lambda: _signed(capability_id="gate.free@v1"), "does not verify"),
    (lambda: _signed(audience="https://elsewhere.example/ai-market/v2/invoke"), "signed for"),
    (lambda: _signed(now=1_000_000), "from this provider's clock"),
    (lambda: {**_signed(), "X-AIMarket-Hub-Nonce": "short"}, "32 hex"),
    (lambda: {**_signed(), "X-AIMarket-Hub-Timestamp": "soon"}, "unix seconds"),
    (lambda: {**_signed(), "X-AIMarket-Hub-Signature": "not base64!"}, "does not verify"),
])
def test_a_signature_that_does_not_hold_is_401_with_the_reason(tmp_path, tamper, reason):
    r = _invoke(_enforcing(tmp_path), headers=tamper())
    assert r.status_code == 401, r.text
    assert r.json()["error"] == "hub_signature_invalid"
    assert reason in r.json()["detail"]
    assert "echo" not in r.text


def test_the_same_signed_request_works_once(tmp_path):
    c = _enforcing(tmp_path)
    headers = _signed()
    assert _invoke(c, headers=headers).status_code == 200
    again = _invoke(c, headers=headers)
    assert again.status_code == 401 and "replayed" in again.json()["detail"]


def test_a_rotated_hub_key_is_accepted_alongside_the_old_one(tmp_path):
    env = {"ORACLE_TRUSTED_HUBS": f"{HUB}={HUB_KEY.public_b64},{HUB}={OTHER_KEY.public_b64}"}
    c = _client(tmp_path, env)
    assert _invoke(c, headers=_signed(key=HUB_KEY)).status_code == 200
    assert _invoke(c, headers=_signed(key=OTHER_KEY)).status_code == 200


def test_an_extra_audience_lets_a_hub_reach_the_provider_under_another_name(tmp_path):
    env = {"ORACLE_TRUSTED_HUBS": f"{HUB}={HUB_KEY.public_b64}",
           "ORACLE_HUB_AUDIENCES": "http://provider.internal:9351"}
    c = _client(tmp_path, env)
    inside = _signed(audience="http://provider.internal:9351/ai-market/v2/invoke")
    assert _invoke(c, headers=inside).status_code == 200
    assert _invoke(c, headers=_signed()).status_code == 200


def test_the_operators_paid_tier_caller_passes_without_a_hub(tmp_path):
    """How the operator's own consumers (ATLAS reading GAIA) keep working."""
    policy = PaidTierPolicy.from_env({"ORACLE_PAID_TIER_SECRET": "s3cret"})
    c = _enforcing(tmp_path, policy=policy)
    assert _invoke(c, headers={"X-AIMarket-Paid-Tier": "s3cret"}).status_code == 200
    assert _invoke(c, headers={"X-AIMarket-Paid-Tier": "wrong"}).status_code == 402


def test_enforce_with_no_trusted_hub_refuses_every_priced_call(tmp_path):
    c = _client(tmp_path, {"ORACLE_PRICE_GATE": "enforce"})
    r = _invoke(c)
    assert r.status_code == 402 and r.json()["payment_ways"] == []
    assert _invoke(c, "gate.free@v1").status_code == 200


def test_the_gate_runs_before_the_cpu_budget_is_charged(tmp_path):
    spec = OracleSpec(
        name="Budget", product_id="prod-b", description="t", public_url=PUBLIC,
        categories=["t"], signing_key_path=str(tmp_path / "k"),
        capabilities=[Capability("b.work@v1", "w", handler=lambda d: {"did": 1},
                                 price_per_call_usd=0.01, cost_ms=lambda d: 600.0,
                                 cpu_budget_ms_per_min=1000.0)],
    )
    gate = PriceGate.from_env(PUBLIC, {"ORACLE_TRUSTED_HUBS": f"{HUB}={HUB_KEY.public_b64}"})
    c = TestClient(create_app(spec, price_gate=gate, paid_tier=PaidTierPolicy()))
    for _ in range(3):
        assert _invoke(c, "b.work@v1", {}).status_code == 402
    # The three refusals spent nothing: a signed call still fits the 1000 ms budget.
    assert _invoke(c, "b.work@v1", {}, headers=_signed("b.work@v1", {})).status_code == 200


# ── Report: verify and log, refuse nothing ────────────────────────────────────────


def test_report_mode_serves_everything_and_logs_what_it_would_refuse(tmp_path, caplog):
    env = {"ORACLE_TRUSTED_HUBS": f"{HUB}={HUB_KEY.public_b64}", "ORACLE_PRICE_GATE": "report"}
    gate = PriceGate.from_env(PUBLIC, env)
    gate.REPORT_EVERY_S = 0.0
    c = TestClient(create_app(_spec(tmp_path), price_gate=gate, paid_tier=PaidTierPolicy()))
    with caplog.at_level(logging.WARNING, logger="oracle_core.price_gate"):
        assert _invoke(c).status_code == 200
        assert _invoke(c, headers=_signed(key=OTHER_KEY)).status_code == 200
        assert _invoke(c, headers=_signed()).status_code == 200
    text = caplog.text
    assert "would refuse gate.priced@v1" in text and "does not verify" in text
    assert "unsigned=1" in text and "valid=1" in text
    assert c.get("/.well-known/ai-market.json").json()["price_gate"]["mode"] == "report"


# ── Pieces ────────────────────────────────────────────────────────────────────────


def test_the_wire_format_is_pinned():
    """The hub carries its own copy of this (aimarket_hub/provider_request_auth.py) and
    pins the same vector. If either side changes the canonical, both tests must."""
    canonical = request_canonical(
        hub="https://Hub.Example:443/", audience="https://provider.example/oracle/ai-market/v2/invoke",
        capability_id="gate.priced@v1", input_digest=input_sha256({"msg": "hi", "n": 1.5}),
        timestamp=1_792_000_000, nonce="0123456789abcdef0123456789abcdef",
    )
    assert canonical == (
        "aimarket-hub-request/v1\n"
        '{"aud":"https://provider.example/oracle/ai-market/v2/invoke",'
        '"capability_id":"gate.priced@v1","hub":"https://hub.example",'
        '"input_sha256":"e119189c8dcefbf9474d5d89322c15b79c9d1424a8a31a923ec1b3f93d39f41a",'
        '"nonce":"0123456789abcdef0123456789abcdef","ts":1792000000}'
    )
    assert HUB_KEY.sign_canonical(canonical) == GOLDEN_SIGNATURE


GOLDEN_SIGNATURE = (
    "fmyP68mgVGsglcunDqMxC5lgRpp1pGTdeGNEWDcfRX9UpoKofYudoc+oV+JaNbz8MgPL1shastKc8GRhl0xcDg=="
)


@pytest.mark.parametrize("raw, want", [
    ("https://Hub.Example/", "https://hub.example"),
    ("https://hub.example:443/x/", "https://hub.example/x"),
    ("http://hub.example:80", "http://hub.example"),
    ("http://hub.example:8080/a?q=1#f", "http://hub.example:8080/a"),
    ("http://[::1]:9351/", "http://[::1]:9351"),
    ("ftp://hub.example", ""),
    ("hub.example", ""),
    ("http://hub.example:99999", ""),
    ("", ""),
])
def test_normalize_url(raw, want):
    assert normalize_url(raw) == want


def test_parse_trusted_hubs_keeps_padding_and_drops_what_is_not_a_key(caplog):
    small_order = base64.b64encode(bytes(32)).decode()  # the identity point
    raw = (f" {HUB}/ = {HUB_KEY.public_b64} , https://bad.example=nope,"
           f"https://zero.example={small_order},no-equals-sign,,https://b.example={OTHER_KEY.public_b64}")
    with caplog.at_level(logging.ERROR):
        got = parse_trusted_hubs(raw)
    assert got == {HUB: (HUB_KEY.public_b64,), "https://b.example": (OTHER_KEY.public_b64,)}
    assert HUB_KEY.public_b64.endswith("=")
    assert caplog.text.count("ignoring an entry") == 3


@pytest.mark.parametrize("raw, has_hubs, want", [
    (None, False, "off"), ("", True, "enforce"), ("off", True, "off"), ("0", True, "off"),
    ("report", False, "report"), ("SHADOW", True, "report"), ("on", False, "enforce"),
    ("enforced", True, "enforce"), ("enfroce", True, "enforce"),
])
def test_resolve_mode(raw, has_hubs, want):
    assert resolve_mode(raw, has_hubs=has_hubs) == want


def test_nonces_expire_with_the_window_and_the_cache_is_bounded():
    clock = [1_800_000_000.0]
    v = HubRequestVerifier({HUB: (HUB_KEY.public_b64,)}, [PUBLIC], max_skew_s=60,
                           max_nonces=2, clock=lambda: clock[0])
    sign = lambda n: _signed(now=clock[0], nonce=f"{n:032x}")  # noqa: E731
    assert v.verify(sign(1), "gate.priced@v1", {"msg": "hi"}).status == "valid"
    assert v.verify(sign(2), "gate.priced@v1", {"msg": "hi"}).status == "valid"
    assert v.verify(sign(3), "gate.priced@v1", {"msg": "hi"}).status == "busy"
    clock[0] += 61  # the first two can no longer be replayed (stale), so they are forgotten
    assert v.verify(sign(4), "gate.priced@v1", {"msg": "hi"}).status == "valid"
    assert len(v._seen) == 1


def test_unsigned_calls_never_touch_the_nonce_cache():
    v = HubRequestVerifier({HUB: (HUB_KEY.public_b64,)}, [PUBLIC], max_nonces=1)
    for n in range(5):
        forged = _signed(key=OTHER_KEY, nonce=f"{n:032x}")
        assert v.verify(forged, "gate.priced@v1", {"msg": "hi"}).status == "invalid"
    assert v.verify(_signed(), "gate.priced@v1", {"msg": "hi"}).status == "valid"
