"""End-to-end: the AIMarket hub sells GAIA readings under Pay-on-Verified escrow.

Full protocol path, in process, nothing stubbed out of existence:

    buyer → hub POST /ai-market/v2/invoke (verify block, wait=true)
          → hub → GAIA /ai-market/v2/invoke   (real ASGI: attested reading,
                                               X-Provider-Signature handshake ON)
          → hub escrow HOLD on the channel
          → hub PoV worker → GAIA /v1/verify  (statistical verdict, no Metis)
          → pass: capture (debit recorded)  |  spike: release (refund + signed
                                               rejection receipt + verify_failed
                                               reputation event)

This is the physical-oracle thesis in one test: a sensor gets PAID because an
independent-interface verifier judged its reading plausible — and a lying
sensor automatically refunds the buyer.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from gaia.app import build_app
from gaia.capabilities import GatewayRuntime

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
    """One hub + one GAIA gateway wired over in-process ASGI transports."""
    # ── Hub env (before HubConfig: crypto_enabled is captured at construction)
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_ALLOW_DEMO_CREDIT", "1")
    monkeypatch.setenv("AIMARKET_SKIP_SEED", "1")
    monkeypatch.setenv("AIMARKET_VERIFY_MIN_PRICE_USD", "0.0005")  # verify sub-cent reads
    monkeypatch.setenv("AIMARKET_VERIFY_RETRY_BACKOFF_S", "0.05")
    monkeypatch.setenv("AIMARKET_VERIFY_METIS_URL", "http://gaia.verify")
    monkeypatch.setenv("AIMARKET_VERIFY_VERIFIER_ID", "gaia.verify@v1")
    # Exercise the full supply-security handshake — GAIA must sign responses.
    monkeypatch.setenv("AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG", "1")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gaia_gw.key"))

    # ── GAIA gateway with a warmed fleet (verifier needs history)
    runtime = GatewayRuntime(key_dir=str(tmp_path / "devkeys"))
    runtime.warm_up(40)
    gaia_app = build_app(runtime, public_url="http://gaia.test")
    gaia_pubkey = gaia_app.state.protocol.signer.public_key_b64

    # ── Route hub-outbound HTTP into the GAIA ASGI app (both provider invokes
    #    and the Pay-on-Verified verifier calls)
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

    from types import SimpleNamespace
    monkeypatch.setattr(vs_mod, "httpx", SimpleNamespace(
        AsyncClient=_VerifyClient, RequestError=httpx.RequestError))

    # ── Fresh channels ledger + hub app
    monkeypatch.setattr(channels_mod, "_ledger",
                        ChannelLedger(db_path=str(tmp_path / "channels.db")))
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub.key")
    db = HubDatabase(config.db_path)
    for cap_id, name in (("gaia.weather.read@v1", "GAIA weather reading"),
                         ("gaia.energy.read@v1", "GAIA energy reading")):
        db.upsert_capability(Capability(
            capability_id=cap_id, product_id="gaia.gateway", name=name,
            source_hub="local", invoke_url="http://gaia.test/ai-market/v2/invoke",
            price_per_call_usd=0.001, trust_score=0.9,
            publisher_id="gaia", provider_pubkey=gaia_pubkey,
        ))
    hub_app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))

    with TestClient(hub_app) as client:
        yield SimpleNamespace(client=client, db=db, runtime=runtime)


def _open_channel(client):
    ch = client.post("/ai-market/v2/channel/open", json={"deposit_usd": 5.0}).json()
    return ch["channel"]["channel_id"], ch["channel"]["channel_secret"]


def _buy_reading(client, channel_id, secret, capability="gaia.weather.read@v1",
                 device="ws-01"):
    return client.post(
        "/ai-market/v2/invoke",
        headers={"X-Payment-Channel": channel_id, "X-Payment-Channel-Secret": secret},
        json={
            "product_id": "gaia.gateway", "capability_id": capability,
            "source_hub": "local", "input": {"device_id": device},
            "verify": {"requested": True, "wait": True,
                       "intent": f"Provide one plausible, honest sensor reading from device {device}"},
        },
    )


def _reputation(db):
    rows = db._conn.execute("SELECT event_type FROM reputation_events").fetchall()
    return [r["event_type"] for r in rows]


def test_honest_sensor_reading_is_paid_after_verification(world):
    channel_id, secret = _open_channel(world.client)
    r = _buy_reading(world.client, channel_id, secret)
    assert r.status_code == 200, r.text
    body = r.json()

    # The buyer got a real attested reading…
    reading = body["result"]["reading"]
    assert reading["device_id"] == "ws-01"
    assert body["result"]["attestation"]["algorithm"] == "ed25519"

    # …and money moved ONLY through the verified-escrow path.
    env = body["verification"]
    assert env["status"] == "settled" and env["verified"] is True
    assert env["verifier"] == "gaia.verify@v1"  # the verdict is honestly attributed
    assert env["trace_id"].startswith("gaia_")
    assert env["signature"]["algorithm"] == "ed25519"

    ch = channels_mod._ledger.get(channel_id)
    assert ch["used_usd"] == pytest.approx(0.01)   # $0.001 billed at the 1¢ ledger quantum
    assert ch["balance_usd"] == pytest.approx(4.99)
    assert "verify_passed" in _reputation(world.db)

    # The verdict is auditable end-to-end at the verifier's trace endpoint.
    trace = world.runtime.service.trace(env["trace_id"])
    assert trace and trace["verified"] is True and trace["device_id"] == "ws-01"


def test_lying_sensor_is_refunded_and_loses_reputation(world):
    channel_id, secret = _open_channel(world.client)
    world.runtime.fleet.get("ws-01").inject_fault(
        "spike", fields=["temperature_c"], magnitude=45.0)

    r = _buy_reading(world.client, channel_id, secret)
    assert r.status_code == 200, r.text  # output delivered; the MONEY came back
    body = r.json()
    env = body["verification"]
    assert env["status"] == "refunded" and env["verified"] is False
    assert env["reason"] == "verify_failed"

    rejection = body["rejection_receipt"]
    assert rejection["type"] == "verification_rejection"
    assert rejection["trace_id"].startswith("gaia_")
    assert rejection["signature"]["algorithm"] == "ed25519"

    ch = channels_mod._ledger.get(channel_id)
    assert ch["used_usd"] == pytest.approx(0.0)    # hold released — buyer kept their money
    assert ch["balance_usd"] == pytest.approx(5.0)
    assert "verify_failed" in _reputation(world.db)

    # The trace names the physics that convicted the sensor.
    trace = world.runtime.service.trace(env["trace_id"])
    failed = {c["name"] for c in trace["checks"] if not c["ok"]}
    assert failed & {"zscore:temperature_c", "rate:temperature_c",
                     "sibling:temperature_c", "bounds:temperature_c"}


def test_offline_sensor_costs_nothing(world):
    channel_id, secret = _open_channel(world.client)
    world.runtime.fleet.get("em-01").inject_fault("dropout")
    r = _buy_reading(world.client, channel_id, secret,
                     capability="gaia.energy.read@v1", device="em-01")
    assert r.status_code == 502  # provider fault surfaced
    ch = channels_mod._ledger.get(channel_id)
    assert ch["balance_usd"] == pytest.approx(5.0)  # no service — no debit, no hold
