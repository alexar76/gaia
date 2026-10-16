"""GAIA HTTP surface: AIMarket v2, verifier envelope, WoT bridge, sim control."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from oracle_core.signing import Signer

from gaia.app import build_app
from gaia.capabilities import GatewayRuntime
from gaia.middleware import bound_response_canonical
from gaia.wot import device_to_td, td_to_tools


@pytest.fixture
def rt(tmp_path):
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys"))
    runtime.warm_up(40)
    return runtime


@pytest.fixture
def client(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    monkeypatch.setenv("GAIA_SIM_TOKEN", "test-sim-token")
    app = build_app(rt, public_url="http://gaia.test")
    with TestClient(app) as c:
        c.app = app
        yield c


def test_well_known_and_signed_manifest(client):
    wk = client.get("/.well-known/ai-market.json").json()
    assert wk["protocol_version"] == "v2"
    assert wk["capabilities_count"] == 6
    man = client.get("/ai-market/v2/manifest").json()
    proto = client.app.state.protocol
    assert proto.signer.verify_manifest_signature(man)


def test_invoke_returns_attested_reading_and_provider_signature(client):
    body = {"capability_id": "gaia.weather.read@v1",
            "input": {"device_id": "ws-01"}, "product_id": "gaia.gateway"}
    r = client.post("/ai-market/v2/invoke", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["output"]["reading"]["device_id"] == "ws-01"
    assert data["receipt"]["signature"]["algorithm"] == "ed25519"
    # The response carries the hub supply-security handshake, bound to THIS request.
    sig = r.headers.get("x-provider-signature", "")
    assert sig
    proto = client.app.state.protocol
    canonical = bound_response_canonical(
        body["capability_id"], body["product_id"], body["input"], data["output"])
    assert Signer.verify(canonical, sig, proto.signer.public_key_b64)


def test_window_bundle(client):
    r = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.window@v1", "input": {"device_id": "aq-01", "n": 5}})
    out = r.json()["output"]
    assert out["count"] == 5
    seqs = [x["reading"]["seq"] for x in out["readings"]]
    assert seqs == sorted(seqs)


def test_verify_endpoint_metis_envelope_pass_and_fail(client, rt):
    out = rt.read("ws-01")
    composed = (
        "You are auditing a paid AI service delivery.\n"
        "Task (buyer intent):\nProvide a plausible weather reading from ws-01\n\n"
        f"Delivered result (JSON):\n{json.dumps(out, sort_keys=True)}\n\n"
        "Judge whether the delivered result correctly and completely fulfils the task."
    )
    env = client.post("/v1/verify", json={"input": composed, "min_verify_score": 0.7}).json()
    assert env["status"] == "success" and env["verified"] is True
    assert set(env) >= {"answer", "status", "verified", "verify_score", "route",
                        "depth", "iterations", "clarifications", "usage", "trace_id"}
    trace = client.get(f"/v1/traces/{env['trace_id']}").json()
    assert trace["device_id"] == "ws-01" and trace["verified"] is True

    rt.fleet.get("ws-01").inject_fault("spike", fields=["temperature_c"], magnitude=45.0)
    bad = rt.read("ws-01")
    composed_bad = composed.replace(json.dumps(out, sort_keys=True),
                                    json.dumps(bad, sort_keys=True))
    env2 = client.post("/v1/verify", json={"input": composed_bad}).json()
    assert env2["status"] == "success" and env2["verified"] is False


def test_verify_endpoint_error_envelope_on_garbage(client):
    env = client.post("/v1/verify", json={"input": "no delivered result here"}).json()
    assert env["status"] == "error" and env["verified"] is False
    assert env["error"] == "unparseable_input"


def test_wot_export_and_reimport_roundtrip(client):
    td = client.get("/wot/ws-01").json()
    assert td["id"] == "urn:dev:gaia:ws-01"
    assert set(td["properties"]) == {"temperature_c", "humidity_pct", "pressure_hpa", "wind_mps"}
    form = td["properties"]["temperature_c"]["forms"][0]
    assert form["aimarket:capability_id"] == "gaia.weather.read@v1"

    tools = td_to_tools(td, product_id="gaia.gateway")
    assert len(tools) == 4
    t = {x["capability_id"]: x for x in tools}["ws-01.temperature_c.read@v1"]
    assert t["price_per_call_usd"] == td["aimarket:price_per_call_usd"]
    assert t["output_schema"]["properties"]["temperature_c"]["unit"] == "cel"

    directory = client.get("/wot").json()
    assert directory["count"] == 4


def test_wot_forms_carry_the_catalogue_price_and_point_at_gaia_without_a_gate(client):
    """Every TD used to say $0.001; the catalogue is the price a buyer is charged."""
    tools = {t["capability_id"]: t for t in client.get("/ai-market/v2/manifest").json()["tools"]}
    for td in client.get("/wot").json()["things"]:
        for prop in td["properties"].values():
            (form,) = prop["forms"]
            price = tools[form["aimarket:capability_id"]]["price_per_call_usd"]
            assert form["href"] == "http://gaia.test/ai-market/v2/invoke"
            assert form["aimarket:price_per_call_usd"] == price == td["aimarket:price_per_call_usd"]
            assert "aimarket:source_hub" not in form
        assert td["description"].endswith("billed per invoke.")


def test_with_hubs_named_a_priced_form_points_at_each_hub(rt):
    device = rt.fleet.get("ws-01")
    hubs = ["https://hub-a.example", "https://hub-b.example/"]
    td = device_to_td(device, "https://iot.example/", "gaia.weather.read@v1", 0.001, hubs=hubs)
    forms = td["properties"]["temperature_c"]["forms"]
    assert [f["href"] for f in forms] == ["https://hub-a.example/ai-market/v2/invoke",
                                          "https://hub-b.example/ai-market/v2/invoke"]
    for f in forms:
        assert f["aimarket:product_id"] == "gaia.gateway"
        assert f["aimarket:source_hub"] == "https://iot.example"
        assert f["aimarket:capability_id"] == "gaia.weather.read@v1"
        assert f["aimarket:input"] == {"device_id": "ws-01"}
    assert "through https://hub-a.example" in td["description"]
    # The importer still reads one tool per property at the form's price.
    assert {t["price_per_call_usd"] for t in td_to_tools(td, product_id="gaia.gateway")} == {0.001}

    # A free capability is never gated, so it stays at GAIA even with hubs named.
    free = device_to_td(device, "https://iot.example", "gaia.free.read@v1", 0.0, hubs=hubs)
    assert [f["href"] for f in free["properties"]["temperature_c"]["forms"]] == [
        "https://iot.example/ai-market/v2/invoke"]


def test_with_the_price_gate_on_the_td_points_where_gaias_402_does(rt, tmp_path, monkeypatch):
    """A TD reader and a direct caller are sent to the same hub, with the same source_hub."""
    pytest.importorskip("oracle_core.price_gate")  # a core older than 0.5 has no gate
    hub_key = base64.b64encode(
        Ed25519PrivateKey.from_private_bytes(b"\x31" * 32).public_key().public_bytes_raw()).decode()
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    monkeypatch.setenv("ORACLE_TRUSTED_HUBS", f"https://hub.example={hub_key}")
    monkeypatch.delenv("ORACLE_PRICE_GATE", raising=False)
    monkeypatch.delenv("ORACLE_PAID_TIER_SECRET", raising=False)
    client = TestClient(build_app(rt, public_url="http://gaia.test"))

    td = client.get("/wot/ws-01").json()
    (form,) = td["properties"]["temperature_c"]["forms"]
    assert form["href"] == "https://hub.example/ai-market/v2/invoke"
    assert form["aimarket:source_hub"] == "http://gaia.test"
    assert form["aimarket:product_id"] == "gaia.gateway"

    direct = client.post("/ai-market/v2/invoke", json={
        "capability_id": form["aimarket:capability_id"], "input": form["aimarket:input"]})
    assert direct.status_code == 402, direct.text
    (way,) = direct.json()["payment_ways"]
    assert way["invoke_url"] == form["href"]
    assert way["source_hub"] == form["aimarket:source_hub"]


def test_sim_control_fault_and_clock(client):
    headers = {"X-Sim-Token": "test-sim-token"}
    r = client.post("/sim/fault", json={"device_id": "em-01", "kind": "dropout"}, headers=headers)
    assert r.json()["fault"] == "dropout"
    # Offline → either HTTP 503 (DeviceOffline handler) or oracle-core 200/{ok:false}.
    # Both mean the hub must not debit the buyer.
    r2 = client.post("/ai-market/v2/invoke",
                     json={"capability_id": "gaia.energy.read@v1", "input": {}})
    if r2.status_code == 503:
        pass
    else:
        assert r2.status_code == 200
        body = r2.json()
        assert body.get("ok") is False
        assert "offline" in str(body.get("error", "")).lower()
    client.post("/sim/fault", json={"device_id": "em-01", "kind": "none"}, headers=headers)
    before = client.app.state.runtime.clock.now()
    client.post("/sim/clock", json={"advance_s": 3600}, headers=headers)
    assert client.app.state.runtime.clock.now() == before + 3600


def test_free_status_capability_lists_pinned_pubkeys(client):
    r = client.post("/ai-market/v2/invoke",
                    json={"capability_id": "gaia.fleet.status@v1", "input": {}})
    out = r.json()["output"]
    assert out["count"] == 4
    assert all(d["device_pubkey"] for d in out["devices"])
