"""End-to-end: a buyer pays the hub to switch a plug, and pays only for the measured effect.

    buyer → hub POST /ai-market/v2/invoke  gaia.actuate@v1, verify block, wait=true
          → hub → GAIA (real ASGI, X-Provider-Signature required): switch + evidence
          → hub escrow HOLD of $0.05 on the channel
          → hub Pay-on-Verified worker → GAIA /v1/verify: evaluate() by the pinned key
          → effect proven: capture | no effect: GAIA answered ok:false, nothing held

The price floor stays at the hub's production default ($0.05) so this proves the
SKU's price really clears it.

GAIA judges gaia.actuate@v1 through the hub's verifier ROUTE (AIMARKET_VERIFY_ROUTES), as a
hub that executes this capability is meant to be configured, not as the hub's default
verifier: the default here is a "Metis" that must never be asked. GAIA's /v1/verify is fail-closed (GAIA_VERIFY_AUTH=1, as
in production) and the hub sends the route's token as its Bearer.

Who may switch the plug next: the hub signs the payer it checked next to its admission of the
call (aimarket-hub-payer/v1, aimarket_hub/provider_request_auth.py), and GAIA leases the plug
to that payer until the auto-off (gaia/payer_identity.py, gaia/actuation.py). A demo-credit
channel names no payer, so most tests here buy anonymously; the last ones open channels the
way production does, with a deposit the chain reports and a payer proof.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from gaia.app import build_app
from gaia.capabilities import GatewayRuntime

pytest.importorskip("aimarket_hub")

import aimarket_hub.channels as channels_mod
import aimarket_hub.outbound_http as outbound_mod
import aimarket_hub.verified_settlement as vs_mod
from aimarket_hub.api import create_app
from aimarket_hub.channels import ChannelLedger
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer

METIS_URL = "http://metis.must-not-be-asked"
VERIFY_TOKEN = "gaia-verify-e2e-token"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_ALLOW_DEMO_CREDIT", "1")
    monkeypatch.setenv("AIMARKET_SKIP_SEED", "1")
    # Never crawl the real federation from a test: a seed host that hangs (admin-vps on
    # 2026-10-16) kept the TestClient waiting on the crawl thread at shutdown, forever.
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    monkeypatch.delenv("AIMARKET_VERIFY_MIN_PRICE_USD", raising=False)  # production floor
    monkeypatch.setenv("AIMARKET_VERIFY_RETRY_BACKOFF_S", "0.05")
    monkeypatch.setenv("AIMARKET_VERIFY_METIS_URL", METIS_URL)
    monkeypatch.delenv("AIMARKET_VERIFY_VERIFIER_ID", raising=False)
    monkeypatch.setenv("AIMARKET_VERIFY_ROUTES", json.dumps({"gaia.actuate@v1": {
        "url": "http://gaia.verify", "verifier_id": "gaia.verify@v1",
        "key_env": "AIMARKET_VERIFY_GAIA_TOKEN"}}))
    monkeypatch.setenv("AIMARKET_VERIFY_GAIA_TOKEN", VERIFY_TOKEN)
    monkeypatch.setenv("GAIA_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.setenv("GAIA_VERIFY_AUTH", "1")
    monkeypatch.setenv("AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG", "1")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gaia_gw.key"))
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    cfg = tmp_path / "actuators.yaml"
    cfg.write_text(json.dumps({"actuators": [{
        "id": "plug-sim-01", "kind": "sim-plug", "label": "demo heater", "load_w": 1500,
        "expect_on_w": 1000, "cooldown_s": 0, "effect_timeout_s": 2}]}))
    monkeypatch.setenv("GAIA_ACTUATION_ENABLED", "1")
    monkeypatch.setenv("GAIA_ACTUATORS_FILE", str(cfg))
    monkeypatch.setenv("GAIA_ACTUATION_DIR", str(tmp_path / "act"))
    monkeypatch.delenv("GAIA_ACTUATION_KILL", raising=False)

    # GAIA enforces its price gate with this hub pinned, as production will: every call
    # below reaches GAIA's priced capabilities only because the hub signed it
    # (oracle_core.price_gate). The hub's key is made first so GAIA can pin it.
    monkeypatch.setenv("AIMARKET_HUB_URL", "http://hub.test")
    hub_signer = Signer(str(tmp_path / "hub.key"))
    monkeypatch.setenv("ORACLE_TRUSTED_HUBS", f"http://hub.test={hub_signer.public_key_b64}")
    monkeypatch.setenv("ORACLE_PRICE_GATE", "enforce")
    runtime = GatewayRuntime(key_dir=str(tmp_path / "devkeys"), autotick=False)
    runtime.actuation.POLL_S = 0.01
    gaia_app = build_app(runtime, public_url="http://gaia.test")
    gaia_pubkey = gaia_app.state.protocol.signer.public_key_b64

    async def fake_safe_post(url, *, json=None, headers=None, timeout=30.0, invoke=False):
        transport = httpx.ASGITransport(app=gaia_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gaia.test") as c:
            return await c.post(httpx.URL(url).path, json=json, headers=headers or {})

    monkeypatch.setattr(outbound_mod, "safe_post", fake_safe_post)
    verify_calls: list[dict] = []

    class _VerifyClient:
        def __init__(self, *a, **k):
            self._c = httpx.AsyncClient(transport=httpx.ASGITransport(app=gaia_app),
                                        base_url="http://gaia.verify")

        async def __aenter__(self):
            await self._c.__aenter__()
            return self

        async def __aexit__(self, *a):
            return await self._c.__aexit__(*a)

        async def post(self, url, json=None, headers=None):
            verify_calls.append({"url": url, "authorization": (headers or {}).get("Authorization")})
            if not url.startswith("http://gaia.verify/"):
                # The default verifier: answering would let a routing bug pass unnoticed.
                return httpx.Response(503, json={"detail": "the default verifier was asked"})
            return await self._c.post(httpx.URL(url).path, json=json, headers=headers)

    monkeypatch.setattr(vs_mod, "httpx", SimpleNamespace(
        AsyncClient=_VerifyClient, RequestError=httpx.RequestError))
    monkeypatch.setattr(channels_mod, "_ledger",
                        ChannelLedger(db_path=str(tmp_path / "channels.db")))
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub.key")
    db = HubDatabase(config.db_path)
    db.upsert_capability(Capability(
        capability_id="gaia.actuate@v1", product_id="gaia.gateway",
        name="Switch a device; pay only for the measured effect", source_hub="local",
        invoke_url="http://gaia.test/ai-market/v2/invoke", price_per_call_usd=0.05,
        trust_score=0.9, publisher_id="gaia", provider_pubkey=gaia_pubkey,
    ))
    hub_app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
    with TestClient(hub_app) as client:
        yield SimpleNamespace(client=client, db=db, runtime=runtime, verify_calls=verify_calls,
                              gaia_app=gaia_app)


def _channel(client):
    ch = client.post("/ai-market/v2/channel/open", json={"deposit_usd": 5.0}).json()
    return ch["channel"]["channel_id"], ch["channel"]["channel_secret"]


def _switch(client, channel_id, secret, command="on", duration=60, verify=True):
    body = {"product_id": "gaia.gateway", "capability_id": "gaia.actuate@v1",
            "source_hub": "local",
            "input": {"actuator_id": "plug-sim-01", "command": command, "duration_s": duration}}
    if verify:
        # A bounded wait: a verdict that never comes fails the test in seconds.
        body["verify"] = {"requested": True, "wait": True, "wait_timeout_s": 30,
                          "intent": "Switch the demo heater on for one minute"}
    return client.post("/ai-market/v2/invoke", json=body, headers={
        "X-Payment-Channel": channel_id, "X-Payment-Channel-Secret": secret})


def _reputation(db):
    return [r["event_type"] for r in
            db._conn.execute("SELECT event_type FROM reputation_events").fetchall()]


def test_a_measured_effect_is_paid_through_escrow(world):
    channel_id, secret = _channel(world.client)
    r = _switch(world.client, channel_id, secret)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["effect"]["verified"] is True
    env = body["verification"]
    assert env["status"] == "settled" and env["verified"] is True
    assert env["verifier"] == "gaia.verify@v1"
    ch = channels_mod._ledger.get(channel_id)
    assert ch["used_usd"] == pytest.approx(0.05) and ch["balance_usd"] == pytest.approx(4.95)
    assert "verify_passed" in _reputation(world.db)
    assert world.runtime.fleet.get("plug-sim-01").relay_is_on
    # Judged at the route, with the route's token; the default verifier never asked.
    assert world.verify_calls == [{"url": "http://gaia.verify/v1/verify",
                                   "authorization": f"Bearer {VERIFY_TOKEN}"}]


def test_a_wrong_verify_token_charges_nothing(world, monkeypatch):
    """GAIA refuses a hub that holds the wrong token (401): the hub settles by policy —
    refunded, nobody blamed — and does not turn to its default verifier."""
    monkeypatch.setenv("AIMARKET_VERIFY_GAIA_TOKEN", "not-the-token")
    channel_id, secret = _channel(world.client)
    r = _switch(world.client, channel_id, secret)
    assert r.status_code == 200, r.text
    env = r.json()["verification"]
    assert env["status"] == "refunded" and env["verdict"] == "indeterminate"
    assert env["verifier"] == "gaia.verify@v1"
    ch = channels_mod._ledger.get(channel_id)
    assert ch["used_usd"] == pytest.approx(0.0) and ch["balance_usd"] == pytest.approx(5.0)
    assert _reputation(world.db) == []
    assert [c["url"] for c in world.verify_calls] == ["http://gaia.verify/v1/verify"]


def test_without_the_route_token_the_hub_refuses_before_switching(world, monkeypatch):
    monkeypatch.delenv("AIMARKET_VERIFY_GAIA_TOKEN")
    channel_id, secret = _channel(world.client)
    r = _switch(world.client, channel_id, secret)
    assert r.status_code == 400 and "verify_unavailable" in r.text
    assert not world.runtime.fleet.get("plug-sim-01").relay_is_on     # nothing was switched
    assert channels_mod._ledger.get(channel_id)["balance_usd"] == pytest.approx(5.0)
    assert world.verify_calls == []


def test_no_effect_costs_nothing(world):
    channel_id, secret = _channel(world.client)
    plug = world.runtime.fleet.get("plug-sim-01")
    world.runtime.fleet.read("plug-sim-01")
    plug.inject_fault("stuck", fields=["power_w", "relay_on"])
    r = _switch(world.client, channel_id, secret)
    assert r.status_code >= 400 or r.json().get("ok") is False
    assert "effect not observed" in r.text
    ch = channels_mod._ledger.get(channel_id)
    assert ch["balance_usd"] == pytest.approx(5.0) and ch["used_usd"] == pytest.approx(0.0)
    plug.clear_fault()
    assert not plug.relay_is_on


def test_the_kill_switch_costs_nothing(world):
    channel_id, secret = _channel(world.client)
    world.runtime.actuation.kill("e2e")
    r = _switch(world.client, channel_id, secret)
    assert "kill switch" in r.text
    assert channels_mod._ledger.get(channel_id)["balance_usd"] == pytest.approx(5.0)


def test_without_escrow_the_effect_still_decides_the_charge(world):
    channel_id, secret = _channel(world.client)
    ok = _switch(world.client, channel_id, secret, verify=False)
    assert ok.status_code == 200 and ok.json()["result"]["effect"]["verified"]
    used = channels_mod._ledger.get(channel_id)["used_usd"]
    assert used == pytest.approx(0.05)
    # A demo-credit channel names no payer, so the "on" holds an anonymous lease that nobody
    # can end early (gaia/actuation.py): the next command waits for its auto-off.
    world.runtime.clock.advance(61)
    world.runtime.fleet.get("plug-sim-01").inject_fault("stuck", fields=["power_w", "relay_on"])
    bad = _switch(world.client, channel_id, secret, command="off", verify=False)
    assert "effect not observed" in bad.text
    assert channels_mod._ledger.get(channel_id)["used_usd"] == pytest.approx(used)


# ── The lease belongs to the wallet that paid ─────────────────────────────────────

A = "0x" + "a1" * 20
B = "0x" + "B2" * 20                     # EIP-55-style case: GAIA sees it lowercased
PROOF = "0xproof-from-the-paying-wallet"


def _production_deposits(monkeypatch, senders):
    """Channels opened as production opens them: the chain reports who sent each deposit
    and the opener signs the payer challenge with that wallet. The recovery is stubbed
    (as in aimarket-hub/tests/test_channels.py) because GAIA's venv has no eth_account."""
    monkeypatch.setattr(channels_mod, "_is_production_mode", lambda: True)
    monkeypatch.setattr(channels_mod, "_VERIFY_STUB", False)
    monkeypatch.setattr(channels_mod, "_verify_tx_onchain",
                        lambda **kw: {"ok": True, "sender": senders[kw["tx_hash"]]})
    monkeypatch.setattr(channels_mod, "_recover_payer_address",
                        lambda *, payer, tx_hash, chain, deposit_usd, signature: (
                            senders[tx_hash] if signature == PROOF else None))


def _proven_channel(client, tx_hash):
    ch = client.post("/ai-market/v2/channel/open", json={
        "deposit_usd": 5.0, "tx_hash": tx_hash, "payer_signature": PROOF}).json()
    return ch["channel"]["channel_id"], ch["channel"]["channel_secret"]


def test_a_paid_on_is_leased_to_the_payer_and_only_the_payer_ends_it(world, monkeypatch):
    _production_deposits(monkeypatch, {"0xdep-a": A, "0xdep-b": B})
    a_id, a_secret = _proven_channel(world.client, "0xdep-a")
    b_id, b_secret = _proven_channel(world.client, "0xdep-b")
    plug = world.runtime.fleet.get("plug-sim-01")

    on = _switch(world.client, a_id, a_secret, duration=600)
    assert on.status_code == 200, on.text
    body = on.json()
    assert body["verification"]["status"] == "settled"
    lease = body["result"]["lease"]
    assert lease["holder"] == A and lease["until"]
    assert channels_mod._ledger.get(a_id)["used_usd"] == pytest.approx(0.05)

    # Another buyer, who paid for their own call: refused before anything switches, unpaid.
    other = _switch(world.client, b_id, b_secret, command="off", verify=False)
    assert "busy until" in other.text and "nothing charged" in other.text
    assert plug.relay_is_on
    assert channels_mod._ledger.get(b_id)["used_usd"] == pytest.approx(0.0)

    # The payer ends their own "on" early.
    mine = _switch(world.client, a_id, a_secret, command="off", verify=False)
    assert mine.status_code == 200, mine.text
    assert not plug.relay_is_on
    journal = [json.loads(x) for x in world.runtime.actuation.journal_path.read_text().splitlines()]
    callers = [e["caller"] for e in journal if e.get("caller")]
    assert {"id": A, "vouched_by": "http://hub.test", "rail": "channel"} in callers


def test_an_x402_payer_holds_the_lease_too(world, monkeypatch):
    """Paid on chain to GAIA's payout wallet: the payer is the authorizer the chain settled."""
    from aimarket_hub import settle

    payout = "0x" + "5e" * 20
    monkeypatch.setenv("AIMARKET_X402_ACCEPT", "1")
    monkeypatch.setenv("AIMARKET_PAYMENT_RECIPIENT", payout)
    world.db.upsert_capability(Capability(
        capability_id="gaia.actuate@v1", product_id="gaia.gateway",
        name="Switch a device; pay only for the measured effect", source_hub="local",
        invoke_url="http://gaia.test/ai-market/v2/invoke", price_per_call_usd=0.05,
        trust_score=0.9, publisher_id="gaia", payout_address=payout,
        provider_pubkey=world.gaia_app.state.protocol.signer.public_key_b64,
    ))

    def _settled(*, tx_hash, terms, timeout=10.0, max_age_s=0, paid_before=None,
                 require_nonce, rpc_url=""):
        return {"tx_hash": tx_hash, "paid_units": settle.to_units(0.05, terms.decimals),
                "authorizer": B}

    monkeypatch.setattr(settle, "verify_transfer", _settled)
    body = {"product_id": "gaia.gateway", "capability_id": "gaia.actuate@v1",
            "source_hub": "local",
            "input": {"actuator_id": "plug-sim-01", "command": "on", "duration_s": 600}}
    quote = world.client.post("/ai-market/v2/invoke", json=body)
    assert quote.status_code == 402, quote.text
    paid = world.client.post("/ai-market/v2/invoke", json=body, headers={
        "X-Payment": "0x" + "11" * 32, "X-Payment-Nonce": quote.json()["nonce"],
        "X-Payment-Secret": quote.json()["payment_secret"]})
    assert paid.status_code == 200, paid.text
    assert paid.json()["result"]["lease"]["holder"] == B.lower()


def test_a_demo_credit_buyer_stays_anonymous(world):
    """No proof at open, no payer: the lease has no holder, and its buyer cannot end it."""
    channel_id, secret = _channel(world.client)
    on = _switch(world.client, channel_id, secret, verify=False)
    assert on.status_code == 200, on.text
    assert on.json()["result"]["lease"]["holder"] is None
    off = _switch(world.client, channel_id, secret, command="off", verify=False)
    assert "busy until" in off.text
