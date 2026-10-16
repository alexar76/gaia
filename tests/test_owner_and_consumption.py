"""A real plug answers only its owner; a stranger may at most buy its consumption, if the owner
put it on sale. The owner signs with their wallet (EIP-712) and pays nothing.
"""

from __future__ import annotations

import base64
import json
import secrets
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from eth_account import Account
from fastapi.testclient import TestClient

from gaia import owner_auth
from gaia.app import build_app
from gaia.capabilities import GatewayRuntime
from gaia.consumption import ConsumptionRecorder, ConsumptionStore, series_canonical
from oracle_core.storage import open_store

GAIA = "http://gaia.test"
OWNER = Account.create()
AGENT = Account.create()       # the owner's delegate
STRANGER = Account.create()


def _sign(acct, *, actuator, action, duration_s=0, from_ts=0, to_ts=0, gaia=GAIA, issued_at=None):
    now = int(time.time()) if issued_at is None else issued_at
    nonce = "0x" + secrets.token_hex(32)
    doc = owner_auth.typed_data(gaia=gaia, actuator=actuator, action=action, duration_s=duration_s,
                                from_ts=from_ts, to_ts=to_ts, issued_at=now, nonce=nonce)
    msg = dict(doc["message"])
    msg["nonce"] = bytes.fromhex(nonce[2:])
    sig = Account.sign_typed_data(acct.key, full_message={**doc, "message": msg}).signature.hex()
    return {"signer": acct.address, "issued_at": now, "nonce": nonce,
            "signature": sig if sig.startswith("0x") else "0x" + sig}


@pytest.fixture
def app(tmp_path, monkeypatch):
    cfg = tmp_path / "act.yaml"
    cfg.write_text(json.dumps({"actuators": [
        {"id": "home-heater", "kind": "sim-plug", "label": "home", "load_w": 1500,
         "expect_on_w": 1000, "cooldown_s": 0, "effect_timeout_s": 2,
         "access": "owner", "owner": OWNER.address, "delegates": [AGENT.address],
         "consumption": {"sale": True, "resolution": "day", "history_days": 7}},
        {"id": "private-plug", "kind": "sim-plug", "label": "private", "load_w": 1500,
         "expect_on_w": 1000, "cooldown_s": 0, "effect_timeout_s": 2,
         "access": "owner", "owner": OWNER.address},
        {"id": "plug-sim-01", "kind": "sim-plug", "label": "demo", "load_w": 1500,
         "expect_on_w": 1000, "cooldown_s": 0, "effect_timeout_s": 2, "access": "public",
         "consumption": {"sale": True}},
    ]}))
    monkeypatch.setenv("GAIA_ACTUATION_ENABLED", "1")
    monkeypatch.setenv("GAIA_ACTUATORS_FILE", str(cfg))
    monkeypatch.setenv("GAIA_ACTUATION_DIR", str(tmp_path / "act"))
    monkeypatch.delenv("GAIA_ACTUATION_KILL", raising=False)
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    monkeypatch.delenv("ORACLE_DATABASE_URL", raising=False)
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys"), autotick=False)
    runtime.actuation.POLL_S = 0.01
    return runtime, TestClient(build_app(runtime, public_url=GAIA))


def _invoke(client, cap, inp):
    return client.post("/ai-market/v2/invoke", json={"capability_id": cap, "input": inp})


# ── switching: the owner only ─────────────────────────────────────────────────


def test_the_owner_switches_their_plug_with_a_signature_and_pays_nothing(app):
    runtime, client = app
    tools = {t["capability_id"]: t for t in client.get("/ai-market/v2/manifest").json()["tools"]}
    assert tools["gaia.owner.command@v1"]["price_per_call_usd"] == 0.0
    r = _invoke(client, "gaia.owner.command@v1", {
        "actuator_id": "home-heater", "command": "on", "duration_s": 60,
        "auth": _sign(OWNER, actuator="home-heater", action="on", duration_s=60)})
    body = r.json()
    assert body["ok"] and body["output"]["effect"]["verified"]
    assert body["output"]["lease"]["holder"] == OWNER.address.lower()


def test_the_owners_delegate_may_switch_it_too(app):
    _, client = app
    body = _invoke(client, "gaia.owner.command@v1", {
        "actuator_id": "home-heater", "command": "on", "duration_s": 30,
        "auth": _sign(AGENT, actuator="home-heater", action="on", duration_s=30)}).json()
    assert body["ok"] and body["output"]["effect"]["verified"]


@pytest.mark.parametrize("who, msg", [
    ("stranger", "no owner or delegate"),
    ("unsigned", "needs auth"),
    ("other-gaia", "another wallet or for another request"),
])
def test_nobody_else_switches_an_owned_plug(app, who, msg):
    runtime, client = app
    inp = {"actuator_id": "home-heater", "command": "on", "duration_s": 60}
    if who == "stranger":
        inp["auth"] = _sign(STRANGER, actuator="home-heater", action="on", duration_s=60)
    elif who == "other-gaia":
        inp["auth"] = _sign(OWNER, actuator="home-heater", action="on", duration_s=60,
                            gaia="https://another-gaia.example")
    body = _invoke(client, "gaia.owner.command@v1", inp).json()
    assert body["ok"] is False and msg in body["error"]
    assert runtime.actuation._plug("home-heater").relay_is_on is False


def test_the_paid_path_does_not_open_an_owned_plug_to_strangers(app):
    runtime, client = app
    body = _invoke(client, "gaia.actuate@v1", {"actuator_id": "home-heater", "command": "on",
                                               "duration_s": 60}).json()
    assert body["ok"] is False and "only from its owner" in body["error"]
    assert runtime.actuation._plug("home-heater").relay_is_on is False


def test_the_demo_plug_is_switched_with_the_paid_command_not_the_owner_one(app):
    _, client = app
    body = _invoke(client, "gaia.owner.command@v1", {
        "actuator_id": "plug-sim-01", "command": "on", "duration_s": 30,
        "auth": _sign(STRANGER, actuator="plug-sim-01", action="on", duration_s=30)}).json()
    assert body["ok"] is False and "public demo plug has none" in body["error"]
    assert _invoke(client, "gaia.actuate@v1", {"actuator_id": "plug-sim-01", "command": "on",
                                               "duration_s": 30}).json()["ok"]


def test_a_signed_command_cannot_be_replayed(app):
    _, client = app
    inp = {"actuator_id": "home-heater", "command": "off",
           "auth": _sign(OWNER, actuator="home-heater", action="off")}
    assert _invoke(client, "gaia.owner.command@v1", inp).json()["ok"]
    again = _invoke(client, "gaia.owner.command@v1", inp).json()
    assert again["ok"] is False and "already used" in again["error"]


def test_an_old_signature_is_refused(app):
    _, client = app
    body = _invoke(client, "gaia.owner.command@v1", {
        "actuator_id": "home-heater", "command": "off",
        "auth": _sign(OWNER, actuator="home-heater", action="off",
                      issued_at=int(time.time()) - 600)}).json()
    assert body["ok"] is False and "±120 s" in body["error"]


# ── consumption: the owner always, others only where it is on sale ────────────


def _fill(runtime, actuator_id, hours=6, watts=1500.0, t0=None):
    """Six hours of samples, one a minute, the heater on for the first half of each hour."""
    store = runtime.consumption
    t0 = (int(time.time()) // 3600 - hours) * 3600 if t0 is None else t0
    for i in range(hours * 60):
        on = (i % 60) < 30
        store.add(actuator_id, t0 + i * 60, energy_wh=(watts / 60.0 if on else 0.0),
                  power_w=watts if on else 0.4, on_s=60.0 if on else 0.0)
    return t0


def test_a_stranger_buys_consumption_only_where_it_is_on_sale(app):
    runtime, client = app
    yesterday = (int(time.time()) // 86400 - 1) * 86400
    _fill(runtime, "home-heater", t0=yesterday + 3600)     # a day-only sale shows finished days
    _fill(runtime, "private-plug")
    off_sale = _invoke(client, "gaia.consumption.read@v1", {"actuator_id": "private-plug",
                                                            "resolution": "day"}).json()
    assert off_sale["ok"] is False and "not put its consumption on sale" in off_sale["error"]
    finer = _invoke(client, "gaia.consumption.read@v1", {"actuator_id": "home-heater",
                                                         "resolution": "hour"}).json()
    assert finer["ok"] is False and "only daily totals" in finer["error"]
    too_old = _invoke(client, "gaia.consumption.read@v1", {
        "actuator_id": "home-heater", "resolution": "day",
        "from": int(time.time()) - 30 * 86400}).json()
    assert too_old["ok"] is False and "at most 7 days back" in too_old["error"]
    day = _invoke(client, "gaia.consumption.read@v1", {"actuator_id": "home-heater",
                                                       "resolution": "day"}).json()
    assert day["ok"], day
    out = day["output"]
    assert out["totals"]["energy_wh"] == pytest.approx(6 * 750.0, rel=1e-6)
    assert out["totals"]["on_minutes"] == pytest.approx(6 * 30.0)
    assert out["simulated"] is True


def test_a_sold_series_never_holds_finer_data_than_the_owner_sells(app):
    runtime, client = app
    now = int(time.time())
    yesterday = (now // 86400 - 1) * 86400
    _fill(runtime, "home-heater", t0=yesterday + 3600)
    one_hour = _invoke(client, "gaia.consumption.read@v1", {   # a "day" over a one-hour window
        "actuator_id": "home-heater", "resolution": "day",
        "from": yesterday + 2 * 3600, "to": yesterday + 3 * 3600}).json()
    assert one_hour["ok"] is False and "no finished day" in one_hour["error"]
    runtime.consumption.add("plug-sim-01", now, energy_wh=5.0, power_w=300.0, on_s=60.0)
    _fill(runtime, "plug-sim-01", hours=2)
    out = _invoke(client, "gaia.consumption.read@v1", {"actuator_id": "plug-sim-01",
                                                       "to": now + 3600}).json()["output"]
    this_hour = time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(now))
    assert out["buckets"] and all(b["start"] != this_hour for b in out["buckets"])  # still filling
    assert out["to"] <= this_hour


def test_a_sold_series_is_signed_by_the_plugs_device_key(app):
    runtime, client = app
    _fill(runtime, "plug-sim-01")
    out = _invoke(client, "gaia.consumption.read@v1", {"actuator_id": "plug-sim-01"}).json()["output"]
    doc = {k: out[k] for k in ("actuator_id", "resolution", "from", "to", "buckets")}
    att = out["attestation"]
    assert att["public_key"] == runtime.actuation.pubkey("plug-sim-01")
    Ed25519PublicKey.from_public_bytes(base64.b64decode(att["public_key"])).verify(
        base64.b64decode(att["value"]), series_canonical(doc).encode())


def test_the_owner_reads_any_of_their_plugs_for_free(app):
    runtime, client = app
    t0 = _fill(runtime, "private-plug")
    now = int(time.time())
    body = _invoke(client, "gaia.owner.consumption@v1", {
        "actuator_id": "private-plug", "from_ts": t0, "to_ts": now, "resolution": "hour",
        "auth": _sign(OWNER, actuator="private-plug", action="consumption", from_ts=t0, to_ts=now)}).json()
    assert body["ok"], body
    assert len(body["output"]["buckets"]) == 6
    assert body["output"]["buckets"][0]["mean_w"] == pytest.approx((30 * 1500 + 30 * 0.4) / 60)
    stranger = _invoke(client, "gaia.owner.consumption@v1", {
        "actuator_id": "private-plug", "from_ts": t0, "to_ts": now,
        "auth": _sign(STRANGER, actuator="private-plug", action="consumption", from_ts=t0, to_ts=now)}).json()
    assert stranger["ok"] is False and "not its owner" in stranger["error"]


def test_the_list_says_what_is_for_sale_but_not_who_owns_it(app):
    _, client = app
    out = _invoke(client, "gaia.actuator.list@v1", {}).json()["output"]
    rows = {a["actuator_id"]: a for a in out["actuators"]}
    assert rows["home-heater"]["consumption"] == {"for_sale": True, "resolution": "day", "history_days": 7}
    assert rows["private-plug"]["consumption"] == {"for_sale": False}
    assert rows["home-heater"]["access"] == "owner" and rows["plug-sim-01"]["access"] == "public"
    text = json.dumps(out)
    assert OWNER.address.lower() not in text.lower() and AGENT.address.lower() not in text.lower()


# ── the recorder ──────────────────────────────────────────────────────────────


class _Meter:
    def __init__(self):
        self.energy, self.power, self.relay = 0.0, 0.0, 0.0

    def read(self, _aid):
        return {"reading": {"values": {"energy_wh": self.energy, "power_w": self.power,
                                       "relay_on": self.relay}}}


def test_the_recorder_uses_the_plugs_register_and_survives_a_reset(tmp_path):
    store = ConsumptionStore(open_store(f"sqlite:///{tmp_path / 'c.sqlite'}"))
    meter, clock = _Meter(), [1_792_144_800.0]       # 2026-10-16T10:00:00Z
    rec = ConsumptionRecorder(store, meter.read, ["p"], now=lambda: clock[0])
    rec.sample_once()                                  # baseline
    meter.power, meter.relay = 2000.0, 1.0
    for _ in range(30):                                # 30 min at 2 kW
        clock[0] += 60
        meter.energy += 2000.0 / 60
        rec.sample_once()
    clock[0] += 60
    meter.energy = 5.0                                 # the plug's register was reset
    rec.sample_once()
    clock[0] += 3600                                   # GAIA was down for an hour: a gap, not energy
    rec.sample_once()
    rows = store.series("p", 1_792_144_800, 1_792_144_800 + 3 * 3600, "hour")
    assert rows[0]["energy_wh"] == pytest.approx(1000.0 + 2000.0 / 60, rel=1e-6)
    assert rows[0]["on_minutes"] == pytest.approx(31.0)
    assert sum(r["energy_wh"] for r in rows) == pytest.approx(rows[0]["energy_wh"])


def test_a_plug_that_cannot_be_read_is_a_gap_not_a_crash(tmp_path):
    store = ConsumptionStore(open_store(f"sqlite:///{tmp_path / 'c.sqlite'}"))

    def down(_aid):
        raise RuntimeError("meter offline")

    ConsumptionRecorder(store, down, ["p"]).sample_once()
    assert store.series("p", 0, 2**31, "hour") == []


# ── the owner's page ──────────────────────────────────────────────────────────


def test_the_owner_page_is_served_with_a_strict_policy(app):
    _, client = app
    for path in ("/owner", "/owner/"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert '<script src="/owner/owner.js"></script>' in r.text
        assert "<script>" not in r.text                     # no inline script for the CSP to allow
    js = client.get("/owner/owner.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert "eth_signTypedData_v4" in js.text and "innerHTML" not in js.text


def test_the_page_signs_exactly_the_typed_data_the_server_checks(app):
    """The browser builds the EIP-712 document by hand; it must match owner_auth field for field."""
    import re

    _, client = app
    js = client.get("/owner/owner.js").text
    block = js[js.index("OwnerRequest: ["):]
    fields = re.findall(r'\{ name: "(\w+)", type: "(\w+)" \}', block[:block.index("],")])
    assert fields == [(f["name"], f["type"]) for f in owner_auth.TYPES["OwnerRequest"]]
    assert 'domain: { name: "GAIA", version: "1", chainId: 8453 }' in js
    assert set(owner_auth.DOMAIN) == {"name", "version", "chainId"}
    assert (owner_auth.DOMAIN["name"], owner_auth.DOMAIN["version"], owner_auth.DOMAIN["chainId"]) \
        == ("GAIA", "1", 8453)


def test_a_wallet_signature_over_the_json_document_verifies(app):
    """What eth_signTypedData_v4 receives: JSON with the nonce as a hex string, not bytes."""
    runtime, client = app
    now, nonce = int(time.time()), "0x" + secrets.token_hex(32)
    doc = owner_auth.typed_data(gaia=GAIA, actuator="home-heater", action="off", duration_s=0,
                                from_ts=0, to_ts=0, issued_at=now, nonce=nonce)
    as_sent = json.loads(json.dumps(doc))                 # exactly what the page stringifies
    sig = Account.sign_typed_data(OWNER.key, full_message=as_sent).signature.hex()
    r = _invoke(client, "gaia.owner.command@v1", {
        "actuator_id": "home-heater", "command": "off",
        "auth": {"signer": OWNER.address, "issued_at": now, "nonce": nonce,
                 "signature": sig if sig.startswith("0x") else "0x" + sig}})
    assert r.json()["ok"] is True, r.json()


def test_the_public_status_hides_owners_too(app):
    _, client = app
    body = client.get("/actuation/v1/status").text
    assert OWNER.address.lower() not in body.lower() and AGENT.address.lower() not in body.lower()


def test_main_serves_the_app_it_built_not_a_second_import(monkeypatch):
    """`python -m gaia.main` once ran uvicorn on "gaia.main:app", which imported the module a
    second time: two runtimes, two consumption samplers, one of them sampling an idle fleet."""
    import uvicorn

    monkeypatch.setenv("GAIA_CONSUMPTION_SAMPLER", "0")
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda target, **kw: seen.setdefault("target", target))
    from gaia import main as gaia_main

    gaia_main.main()
    assert seen["target"] is gaia_main.app
