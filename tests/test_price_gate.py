"""The price gate on GAIA, without a hub: who gets a priced reading, and who gets 402.

Seen 2026-10-16: an unpaid POST to https://iot.modelmarket.dev/ai-market/v2/invoke got a
full `gaia.power.window@v1` answer. The gate is oracle-core's (oracle_core/price_gate.py);
the hub-and-GAIA version of these is in test_hub_e2e.py. This file runs in the standalone
GAIA repository too, where there is no hub.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

gate_mod = pytest.importorskip("oracle_core.price_gate")  # a vendored core older than 0.5 has none

from oracle_core.signing import Signer  # noqa: E402

from gaia.app import build_app  # noqa: E402
from gaia.capabilities import GatewayRuntime  # noqa: E402
from gaia.middleware import bound_response_canonical  # noqa: E402

PUBLIC = "https://iot.example"
HUB = "https://hub.example"
_HUB_SK = Ed25519PrivateKey.from_private_bytes(b"\x21" * 32)
HUB_KEY = base64.b64encode(_HUB_SK.public_key().public_bytes_raw()).decode()
READ = {"capability_id": "gaia.weather.read@v1", "input": {"device_id": "ws-01"}}


def _signed(capability_id, input_payload, audience=f"{PUBLIC}/ai-market/v2/invoke"):
    return gate_mod.sign_request(
        lambda c: base64.b64encode(_HUB_SK.sign(c.encode())).decode(),
        hub_url=HUB, audience=audience, capability_id=capability_id, input_payload=input_payload,
    )


@pytest.fixture
def gaia(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    monkeypatch.setenv("ORACLE_TRUSTED_HUBS", f"{HUB}={HUB_KEY}")
    # The operator's own consumer (ATLAS) is nominated with the paid-tier secret.
    monkeypatch.setenv("ORACLE_PAID_TIER_SECRET", "atlas-reads-gaia")
    monkeypatch.delenv("ORACLE_PRICE_GATE", raising=False)
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    runtime = GatewayRuntime(key_dir=str(tmp_path / "devkeys"), autotick=False)
    runtime.warm_up(3)
    return TestClient(build_app(runtime, public_url=PUBLIC))


def test_an_unpaid_reading_is_402(gaia):
    r = gaia.post("/ai-market/v2/invoke", json=READ)
    assert r.status_code == 402, r.text
    assert r.json()["payment_ways"][0]["invoke_url"] == f"{HUB}/ai-market/v2/invoke"
    assert "reading" not in r.text


def test_a_reading_the_hub_signed_is_served_with_gaias_own_signature(gaia):
    r = gaia.post("/ai-market/v2/invoke", json=READ, headers=_signed(READ["capability_id"], READ["input"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["output"]["reading"]["device_id"] == "ws-01"
    pub = gaia.get("/.well-known/ai-market.json").json()["signer_public_key"]
    canonical = bound_response_canonical(READ["capability_id"], "", READ["input"], body["output"])
    assert Signer.verify(canonical, r.headers["x-provider-signature"], pub)


def test_the_operators_own_reader_passes_with_the_paid_tier_secret(gaia):
    """How ATLAS keeps reading GAIA directly once the gate is on (ATLAS_GAIA_PAID_TIER_SECRET)."""
    r = gaia.post("/ai-market/v2/invoke", json=READ, headers={"X-AIMarket-Paid-Tier": "atlas-reads-gaia"})
    assert r.status_code == 200, r.text
    assert gaia.post("/ai-market/v2/invoke", json=READ,
                     headers={"X-AIMarket-Paid-Tier": "guess"}).status_code == 402


def test_the_free_fleet_registry_is_open(gaia):
    r = gaia.post("/ai-market/v2/invoke", json={"capability_id": "gaia.fleet.status@v1", "input": {}})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_a_signature_addressed_to_another_gaia_is_refused(gaia):
    """A second operator's GAIA that trusts the same hub cannot replay a call made to this one."""
    elsewhere = _signed(READ["capability_id"], READ["input"],
                        audience="https://other-gaia.example/ai-market/v2/invoke")
    r = gaia.post("/ai-market/v2/invoke", json=READ, headers=elsewhere)
    assert r.status_code == 401 and "signed for" in r.json()["detail"]
