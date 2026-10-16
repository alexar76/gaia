"""End-to-end: a buyer pays the hub to switch a plug, and pays only for the measured effect.

    buyer → hub POST /ai-market/v2/invoke  gaia.actuate@v1, verify block, wait=true
          → hub → GAIA (real ASGI, X-Provider-Signature required): switch + evidence
          → hub escrow HOLD of $0.05 on the channel
          → hub Pay-on-Verified worker → GAIA /v1/verify: evaluate() by the pinned key
          → effect proven: capture | no effect: GAIA answered ok:false, nothing held

The price floor stays at the hub's production default ($0.05) so this proves the
SKU's price really clears it.
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
    monkeypatch.setenv("AIMARKET_VERIFY_METIS_URL", "http://gaia.verify")
    monkeypatch.setenv("AIMARKET_VERIFY_VERIFIER_ID", "gaia.verify@v1")
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

    runtime = GatewayRuntime(key_dir=str(tmp_path / "devkeys"), autotick=False)
    runtime.actuation.POLL_S = 0.01
    gaia_app = build_app(runtime, public_url="http://gaia.test")
    gaia_pubkey = gaia_app.state.protocol.signer.public_key_b64

    async def fake_safe_post(url, *, json=None, headers=None, timeout=30.0, invoke=False):
        transport = httpx.ASGITransport(app=gaia_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gaia.test") as c:
            return await c.post(httpx.URL(url).path, json=json, headers=headers or {})

    monkeypatch.setattr(outbound_mod, "safe_post", fake_safe_post)

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
        yield SimpleNamespace(client=client, db=db, runtime=runtime)


def _channel(client):
    ch = client.post("/ai-market/v2/channel/open", json={"deposit_usd": 5.0}).json()
    return ch["channel"]["channel_id"], ch["channel"]["channel_secret"]


def _switch(client, channel_id, secret, command="on", duration=60, verify=True):
    body = {"product_id": "gaia.gateway", "capability_id": "gaia.actuate@v1",
            "source_hub": "local",
            "input": {"actuator_id": "plug-sim-01", "command": command, "duration_s": duration}}
    if verify:
        body["verify"] = {"requested": True, "wait": True,
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
    world.runtime.fleet.get("plug-sim-01").inject_fault("stuck", fields=["power_w", "relay_on"])
    bad = _switch(world.client, channel_id, secret, command="off", verify=False)
    assert "effect not observed" in bad.text
    assert channels_mod._ledger.get(channel_id)["used_usd"] == pytest.approx(used)
