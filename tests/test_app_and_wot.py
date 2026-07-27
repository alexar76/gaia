"""GAIA HTTP surface: AIMarket v2, verifier envelope, WoT bridge, sim control."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from oracle_core.signing import Signer

from gaia.app import build_app
from gaia.capabilities import GatewayRuntime
from gaia.middleware import bound_response_canonical
from gaia.wot import td_to_tools


@pytest.fixture
def rt(tmp_path):
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys"))
    runtime.warm_up(40)
    return runtime


@pytest.fixture
def client(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
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


def test_sim_control_fault_and_clock(client):
    r = client.post("/sim/fault", json={"device_id": "em-01", "kind": "dropout"})
    assert r.json()["fault"] == "dropout"
    # An offline device fails the invoke with a 5xx — the hub then never debits.
    r2 = client.post("/ai-market/v2/invoke",
                     json={"capability_id": "gaia.energy.read@v1", "input": {}})
    assert r2.status_code == 503
    client.post("/sim/fault", json={"device_id": "em-01", "kind": "none"})
    before = client.app.state.runtime.clock.now()
    client.post("/sim/clock", json={"advance_s": 3600})
    assert client.app.state.runtime.clock.now() == before + 3600


def test_free_status_capability_lists_pinned_pubkeys(client):
    r = client.post("/ai-market/v2/invoke",
                    json={"capability_id": "gaia.fleet.status@v1", "input": {}})
    out = r.json()["output"]
    assert out["count"] == 4
    assert all(d["device_pubkey"] for d in out["devices"])
