"""Pay-on-verified actuation: safety gates, evidence, the verdict, Tasmota over MQTT.

What must hold, whatever else changes:
  * nothing switches unless the operator enabled actuation and listed the actuator;
  * "on" is always bounded, and a real plug carries its own off-timer (PulseTime);
  * the verdict trusts only the plug's PINNED key, and any one false fact fails it;
  * an unproven effect is switched back off and costs nothing;
  * one command settles once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gaia import actuation as act
from gaia.actuation import ActuationController, ActuatorSpec, evaluate, load_specs, sign_command
from gaia.app import build_app
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.plug import MqttLink, SmartPlugSim, TasmotaPlug, pulse_time
from gaia.fleet import Fleet
from gaia.plausibility import PlausibilityVerifier
from gaia.verifier import VerifierService
from gaia.wot import device_to_td
from oracle_core.signing import Signer

T0 = 1_792_145_220.0  # 2026-10-16T10:07:00Z


def _spec(**kw) -> ActuatorSpec:
    base = dict(actuator_id="plug-1", label="test heater", kind="sim-plug", expect_on_w=1000,
                off_max_w=10, min_on_s=10, max_on_s=900, cooldown_s=0, effect_timeout_s=2,
                load_w=1500)
    base.update(kw)
    return ActuatorSpec(**base)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_ACTUATION_KILL", raising=False)
    clock = SimClock(start_epoch=T0)
    fleet = Fleet()
    fleet.add(SmartPlugSim("plug-1", clock, load_w=1500, key_dir=str(tmp_path / "keys")))
    ctl = ActuationController(fleet, clock, [_spec()], journal_path=tmp_path / "j.jsonl",
                              kill_file=tmp_path / "KILL")
    ctl.POLL_S = 0.01
    return ctl


def _journal(ctl) -> list[dict]:
    return [json.loads(line) for line in ctl.journal_path.read_text().splitlines()]


# ── the plug ──────────────────────────────────────────────────────────────────


def test_a_sim_plug_meters_its_load_only_while_on_and_switches_itself_off(tmp_path):
    clock = SimClock(start_epoch=T0)
    plug = SmartPlugSim("p", clock, load_w=1500, key_dir=str(tmp_path))
    assert plug.sample()["power_w"] == pytest.approx(0.4)
    plug.set_relay(True, 60)
    on = plug.sample()
    assert on["relay_on"] == 1.0 and 1480 <= on["power_w"] <= 1520
    clock.advance(30)
    plug.sample()
    clock.advance(300)  # past the paid minute
    off = plug.sample()
    assert off["relay_on"] == 0.0 and off["power_w"] == pytest.approx(0.4)
    # the register counts the minute on and the rest at standby — not 330 s at 1500 W
    assert off["energy_wh"] == pytest.approx(1500 * 60 / 3600 + 0.4 * 270 / 3600, rel=1e-6)


def test_a_plug_never_turns_on_unbounded(tmp_path):
    plug = SmartPlugSim("p", SimClock(), key_dir=str(tmp_path))
    with pytest.raises(ValueError, match="positive duration"):
        plug.set_relay(True, None)
    with pytest.raises(ValueError, match="16 A socket"):
        SmartPlugSim("q", SimClock(), load_w=5000, key_dir=str(tmp_path))


@pytest.mark.parametrize("seconds, value", [(0.5, 5), (5, 50), (11.1, 111), (11.5, 112),
                                            (60, 160), (900, 1000), (64_800, 64_900)])
def test_pulse_time_rounds_up_into_tasmotas_two_ranges(seconds, value):
    assert pulse_time(seconds) == value


def test_pulse_time_refuses_what_tasmota_cannot_hold():
    with pytest.raises(ValueError):
        pulse_time(64_801)
    with pytest.raises(ValueError):
        pulse_time(0)


# ── the controller: the happy path ────────────────────────────────────────────


def test_on_switches_measures_and_returns_verified_evidence(rig):
    out = rig.actuate("plug-1", "on", 60, caller="buyer-1")
    assert out["effect"]["verified"] is True
    assert out["before"]["reading"]["values"]["power_w"] < 10
    assert out["after"]["reading"]["values"]["power_w"] >= 1000
    assert out["actuation"]["record"]["expect"] == {
        "field": "power_w", "op": ">=", "value": 1000, "within_s": 2}
    assert out["auto_off_at"] == "2026-10-16T10:08:00Z"
    assert out["simulated"] is True
    events = [e["event"] for e in _journal(rig)]
    assert events == ["command", "outcome"]
    assert rig.evidence(out["actuation"]["record"]["command_id"])["verdict"]["verified"]


def test_off_is_verified_by_the_power_dropping(rig):
    rig.actuate("plug-1", "on", 60)
    out = rig.actuate("plug-1", "off")
    assert out["effect"]["verified"] and out["after"]["reading"]["values"]["power_w"] <= 10
    assert out["auto_off_at"] is None


def test_the_evidence_verifies_independently_by_the_pinned_key(rig):
    out = rig.actuate("plug-1", "on", 60)
    assert evaluate(out, pubkey=rig.pubkey("plug-1")).verified


# ── the controller: safety gates ──────────────────────────────────────────────


@pytest.mark.parametrize("args, msg", [
    (("nope", "on", 60), "unknown actuator 'nope'.*plug-1"),
    (("plug-1", "explode", 60), "command must be one of on, off"),
    (("plug-1", "on", None), "needs duration_s from 10 to 900"),
    (("plug-1", "on", 5), "needs duration_s from 10 to 900"),
    (("plug-1", "on", 901), "needs duration_s from 10 to 900"),
    (("plug-1", "on", "soon"), "needs duration_s"),
])
def test_bad_commands_are_refused_before_anything_switches(rig, args, msg):
    with pytest.raises(ValueError, match=msg):
        rig.actuate(*args)
    assert not rig._plug("plug-1").relay_is_on


def test_the_kill_switch_refuses_and_switches_everything_off(rig):
    rig.actuate("plug-1", "on", 600)
    assert rig._plug("plug-1").relay_is_on
    out = rig.kill("test")
    assert out == {"killed": True, "switched_off": ["plug-1"]}
    assert not rig._plug("plug-1").relay_is_on and rig.kill_file.exists()
    with pytest.raises(ValueError, match=r"kill switch\); nothing charged"):
        rig.actuate("plug-1", "on", 60)
    rig.resume()
    assert not rig.killed
    rig.actuate("plug-1", "on", 60)


def test_the_kill_file_alone_stops_commands(rig):
    rig.kill_file.write_text("operator touched it")
    with pytest.raises(ValueError, match="kill switch"):
        rig.actuate("plug-1", "on", 60)


def test_the_kill_env_stops_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_ACTUATION_KILL", "1")
    fleet = Fleet()
    fleet.add(SmartPlugSim("plug-1", SimClock(), key_dir=str(tmp_path)))
    ctl = ActuationController(fleet, SimClock(), [_spec()])
    with pytest.raises(ValueError, match="kill switch"):
        ctl.actuate("plug-1", "on", 60)


def test_a_cooldown_is_enforced(tmp_path):
    clock = SimClock(start_epoch=T0)
    fleet = Fleet()
    fleet.add(SmartPlugSim("plug-1", clock, key_dir=str(tmp_path)))
    ctl = ActuationController(fleet, clock, [_spec(cooldown_s=30)])
    ctl.actuate("plug-1", "on", 60)
    with pytest.raises(ValueError, match="cooling down: retry in 30 s"):
        ctl.actuate("plug-1", "off")
    clock.advance(31)
    ctl.actuate("plug-1", "off")


def test_one_command_at_a_time(rig):
    rig._state["plug-1"].lock.acquire()
    try:
        with pytest.raises(ValueError, match="executing another command"):
            rig.actuate("plug-1", "on", 60)
    finally:
        rig._state["plug-1"].lock.release()


def test_an_unproven_on_is_switched_back_off_and_costs_nothing(rig):
    plug = rig._plug("plug-1")
    rig.fleet.read("plug-1")
    plug.inject_fault("stuck", fields=["power_w", "relay_on"])  # the meter shows nothing
    with pytest.raises(ValueError, match=r"effect not observed.*nothing charged.*"
                                         r"/actuation/v1/evidence/act_"):
        rig.actuate("plug-1", "on", 60)
    plug.clear_fault()
    assert not plug.relay_is_on
    outcome = _journal(rig)[-1]
    assert outcome["event"] == "outcome" and outcome["verified"] is False


def test_a_plug_that_stops_reporting_costs_nothing(rig):
    rig._plug("plug-1").inject_fault("dropout")
    with pytest.raises(DeviceOffline):
        rig.actuate("plug-1", "on", 60)


def test_command_seq_only_grows(rig):
    a = rig.actuate("plug-1", "on", 60)["actuation"]["record"]["seq"]
    b = rig.actuate("plug-1", "off")["actuation"]["record"]["seq"]
    assert b > a


# ── the verdict ───────────────────────────────────────────────────────────────


@pytest.fixture
def bundle(rig):
    return rig.actuate("plug-1", "on", 60)


def _tamper(bundle, path, value):
    b = json.loads(json.dumps(bundle))
    node = b
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return b


@pytest.mark.parametrize("path, value, failed", [
    (("after", "reading", "values", "power_w"), 1800.0, "after_signature"),
    (("actuation", "record", "duration_s"), 900, "command_signature"),
    (("actuation", "record", "expect"), {"field": "power_w", "op": ">=", "value": 1,
                                         "within_s": 2}, "command_signature"),
    (("after", "reading", "device_id"), "plug-2", "after_device"),
])
def test_any_one_false_fact_fails_the_verdict(rig, bundle, path, value, failed):
    v = evaluate(_tamper(bundle, path, value), pubkey=rig.pubkey("plug-1"))
    assert not v.verified and v.score == 0.0 and failed in v.summary


def test_a_bundle_signed_by_another_key_fails(rig, bundle, tmp_path):
    other = Signer(tmp_path / "other.key")
    forged = json.loads(json.dumps(bundle))
    forged["actuation"]["attestation"] = sign_command(forged["actuation"]["record"], other)
    assert not evaluate(forged, pubkey=rig.pubkey("plug-1")).verified
    # …even though it is consistent with the key it carries
    assert evaluate(forged, pubkey=other.public_key_b64).checks[0].ok


def test_an_effect_read_too_late_fails(rig, bundle):
    late = json.loads(json.dumps(bundle))
    late["actuation"]["record"]["expect"]["within_s"] = 0
    # re-sign honestly with the plug's own key so only the window is wrong
    late["actuation"]["attestation"] = sign_command(late["actuation"]["record"],
                                                    rig._plug("plug-1").signer)
    rig.clock.advance(30)
    later = rig.fleet.read("plug-1")
    late["after"] = later
    assert "window" in evaluate(late, pubkey=rig.pubkey("plug-1")).summary


def test_a_malformed_bundle_is_a_failed_verdict_not_a_crash(rig):
    v = evaluate({"actuation": {}}, pubkey=rig.pubkey("plug-1"))
    assert not v.verified and v.summary == "malformed actuation bundle"


# ── the escrow verifier (/v1/verify) ──────────────────────────────────────────


def _audit(delivered: dict) -> str:
    nonce = "a1b2c3d4"
    return ("Task (buyer intent): switch the heater on for a minute\n\n"
            f"Delivered result (JSON):\n<<<UNTRUSTED-DELIVERY-{nonce}>>>\n"
            f"{json.dumps(delivered)}\n<<</UNTRUSTED-DELIVERY-{nonce}>>>\n\n"
            "Judge whether the delivered result fulfils the task.")


def test_the_escrow_verifier_passes_a_real_effect_once(rig, bundle):
    service = VerifierService(PlausibilityVerifier(rig.fleet))
    env = service.verify(_audit(bundle), 0.7)
    assert env["verified"] and env["delivery_verdict"]["fulfils"]
    assert env["verify_performed"] and env["threshold"] == 0.7
    replay = service.verify(_audit(bundle), 0.7)
    assert not replay["verified"] and "replay rejected" in replay["answer"]


def test_the_escrow_verifier_fails_a_forged_effect(rig, bundle):
    service = VerifierService(PlausibilityVerifier(rig.fleet))
    forged = _tamper(bundle, ("after", "reading", "values", "power_w"), 1800.0)
    env = service.verify({"input": forged}, 0.7)
    assert env["verify_performed"] and not env["verified"]
    assert any("after_signature" in r for r in env["delivery_verdict"]["reasons"])


def test_the_escrow_verifier_refuses_an_actuator_it_does_not_know(rig, bundle):
    other = _tamper(bundle, ("actuation", "record", "actuator_id"), "someone-elses-plug")
    env = VerifierService(PlausibilityVerifier(rig.fleet)).verify(other, 0.7)
    assert not env["verified"] and env["answer"] == "unknown actuator"


# ── configuration ─────────────────────────────────────────────────────────────


def test_the_shipped_demo_config_loads():
    specs = load_specs(act.default_config_path())
    assert [s.actuator_id for s in specs] == ["plug-sim-01"]
    assert specs[0].max_on_s == 900 and specs[0].kind == "sim-plug"


def test_the_example_config_for_real_plugs_loads():
    specs = load_specs(act.default_config_path().with_name("actuators.example.yaml"))
    assert specs[0].kind == "tasmota-mqtt" and specs[0].topic == "tasmota_lab_heater"
    assert "topic" not in specs[0].public()  # a buyer never sees the operator's topics


@pytest.mark.parametrize("entry, msg", [
    ({"id": "x", "kind": "toaster", "expect_on_w": 1}, "kind must be"),
    ({"id": "bad id!", "kind": "sim-plug", "expect_on_w": 1, "load_w": 5}, "letters, digits"),
    ({"id": "x", "kind": "sim-plug", "expect_on_w": 10, "off_max_w": 20, "load_w": 50},
     "on and off look alike"),
    ({"id": "x", "kind": "sim-plug", "expect_on_w": 100, "load_w": 50}, "load_w >= expect_on_w"),
    ({"id": "x", "kind": "tasmota-mqtt", "expect_on_w": 100}, "needs its topic"),
    ({"id": "x", "kind": "sim-plug", "expect_on_w": 100, "load_w": 500, "max_on_s": 99999},
     "max_on_s <= 64800"),
])
def test_a_bad_config_is_refused_with_a_reason(tmp_path, entry, msg):
    p = tmp_path / "a.yaml"
    p.write_text(json.dumps({"actuators": [entry]}))
    with pytest.raises(ValueError, match=msg):
        load_specs(p)


def test_actuation_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_ACTUATION_ENABLED", raising=False)
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys"))
    assert runtime.actuation is None
    ids = {c.capability_id for c in build_spec(runtime).capabilities}
    assert not {"gaia.actuate@v1", "gaia.actuator.list@v1"} & ids
    client = TestClient(build_app(runtime, public_url="http://gaia.test"))
    assert client.get("/actuation/v1/status").status_code == 404


# ── Tasmota over MQTT ─────────────────────────────────────────────────────────


class FakeMqtt:
    """Records publishes; answers Status 8 like a Tasmota plug with a heater behind it."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.subscribed: list[str] = []
        self.on_message = self.on_connect = None
        self.relay = False
        self.answer = True
        self.voltage = 231.0

    def username_pw_set(self, *a):
        pass

    def tls_set(self):
        pass

    def connect(self, *a, **k):
        pass

    def loop_start(self):
        self.on_connect(self, None, None, 0)

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def _deliver(self, topic, payload):
        class M:
            pass
        m = M()
        m.topic, m.payload = topic, payload.encode()
        self.on_message(self, None, m)

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload))
        if topic.endswith("/Backlog") and "Power1 ON" in payload:
            self.relay = True
            self._deliver("stat/heater/RESULT", json.dumps({"POWER": "ON"}))
        elif topic.endswith("/Power1") and payload == "OFF":
            self.relay = False
            self._deliver("stat/heater/POWER", "OFF")
        elif topic.endswith("/Status") and payload == "8" and self.answer:
            power = 1987.0 if self.relay else 0.3
            self._deliver("stat/heater/STATUS8", json.dumps({"StatusSNS": {"ENERGY": {
                "Power": power, "Voltage": self.voltage if self.relay or self.voltage else 0,
                "Current": round(power / 231.0, 3), "Total": 12.345}}}))


@pytest.fixture
def tasmota(tmp_path):
    fake = FakeMqtt()
    link = MqttLink("mqtt://broker.local:1883", client_factory=lambda: fake)
    link.start()
    plug = TasmotaPlug("heater-1", SimClock(start_epoch=T0), link=link, topic="heater",
                       key_dir=str(tmp_path))
    return fake, plug


def test_tasmota_on_carries_its_own_off_timer(tasmota):
    fake, plug = tasmota
    plug.set_relay(True, 600)
    assert ("cmnd/heater/Backlog", "PulseTime1 700; Power1 ON") in fake.published
    assert plug.relay_is_on
    plug.set_relay(False)
    assert fake.published[-1] == ("cmnd/heater/Power1", "OFF")


def test_tasmota_is_read_on_demand(tasmota):
    fake, plug = tasmota
    plug.set_relay(True, 60)
    values = plug.sample()
    assert values["power_w"] == 1987.0 and values["relay_on"] == 1.0
    assert values["energy_wh"] == pytest.approx(12345.0)
    assert ("cmnd/heater/Status", "8") in fake.published


def test_a_silent_tasmota_is_offline(tasmota):
    fake, plug = tasmota
    fake.answer = False
    plug.STATUS_WAIT_S = 0.05
    with pytest.raises(DeviceOffline, match="no fresh energy report"):
        plug.sample()


def test_a_tasmota_that_reports_zero_volts_when_off_is_not_convicted(tasmota):
    fake, plug = tasmota
    fake.voltage = 0
    values = plug.sample()
    assert "voltage_v" not in values and values["power_w"] == 0.3


def test_tasmota_end_to_end_through_the_controller(tasmota, tmp_path):
    fake, plug = tasmota
    fleet = Fleet()
    fleet.add(plug)
    ctl = ActuationController(fleet, plug.clock, [_spec(actuator_id="heater-1",
                                                        kind="tasmota-mqtt", topic="heater",
                                                        load_w=None)])
    out = ctl.actuate("heater-1", "on", 120)
    assert out["effect"]["verified"] and out["after"]["reading"]["values"]["power_w"] == 1987.0
    assert out["simulated"] is False
    assert ("cmnd/heater/Backlog", "PulseTime1 220; Power1 ON") in fake.published


@pytest.mark.parametrize("topic", ["a/b", "x#", "+", "", "two words"])
def test_a_plug_topic_is_one_word_never_a_wildcard(tmp_path, topic):
    link = MqttLink("mqtt://b:1883", client_factory=FakeMqtt)
    with pytest.raises(ValueError, match="one word"):
        TasmotaPlug("p", SimClock(), link=link, topic=topic, key_dir=str(tmp_path))


def test_a_broker_url_must_be_mqtt():
    with pytest.raises(ValueError, match="mqtt://"):
        MqttLink("http://broker", client_factory=FakeMqtt)


# ── over HTTP: capabilities, WoT actions, the kill switch API ─────────────────


@pytest.fixture
def act_app(tmp_path, monkeypatch):
    cfg = tmp_path / "act.yaml"
    cfg.write_text(json.dumps({"actuators": [{
        "id": "plug-sim-01", "kind": "sim-plug", "label": "demo", "load_w": 1500,
        "expect_on_w": 1000, "cooldown_s": 0, "effect_timeout_s": 2}]}))
    monkeypatch.setenv("GAIA_ACTUATION_ENABLED", "1")
    monkeypatch.setenv("GAIA_ACTUATORS_FILE", str(cfg))
    monkeypatch.setenv("GAIA_ACTUATION_DIR", str(tmp_path / "act"))
    monkeypatch.setenv("GAIA_ACTUATION_TOKEN", "op-secret")
    monkeypatch.delenv("GAIA_ACTUATION_KILL", raising=False)
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys"), autotick=False)
    runtime.actuation.POLL_S = 0.01
    return runtime, TestClient(build_app(runtime, public_url="http://gaia.test"))


def test_actuation_is_listed_priced_and_buyable(act_app):
    runtime, client = act_app
    tools = {t["capability_id"]: t for t in client.get("/ai-market/v2/manifest").json()["tools"]}
    assert tools["gaia.actuate@v1"]["price_per_call_usd"] == 0.05
    assert tools["gaia.actuator.list@v1"]["price_per_call_usd"] == 0.0
    listed = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.actuator.list@v1", "input": {}}).json()
    assert listed["output"]["actuators"][0]["actuator_id"] == "plug-sim-01"
    r = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.actuate@v1",
        "input": {"actuator_id": "plug-sim-01", "command": "on", "duration_s": 60}})
    body = r.json()
    assert r.status_code == 200 and body["ok"] and body["output"]["effect"]["verified"]
    assert body["receipt"]["price_usd"] == 0.05 and r.headers.get("x-provider-signature")
    cid = body["output"]["actuation"]["record"]["command_id"]
    assert client.get(f"/actuation/v1/evidence/{cid}").json()["verdict"]["verified"]
    assert client.get("/actuation/v1/status").json()["actuators"][0]["relay_on"] is True


def test_the_gaia_verify_sku_judges_actuation_bundles(act_app):
    runtime, client = act_app
    out = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.actuate@v1",
        "input": {"actuator_id": "plug-sim-01", "command": "on", "duration_s": 60}}).json()
    verdict = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.verify@v1", "input": out["output"]}).json()["output"]
    assert verdict["verified"] is True


def test_the_plug_is_a_wot_thing_with_actions(act_app):
    runtime, client = act_app
    td = client.get("/wot/plug-sim-01").json()
    assert set(td["actions"]) == {"switchOn", "switchOff"}
    form = td["actions"]["switchOn"]["forms"][0]
    assert form["aimarket:capability_id"] == "gaia.actuate@v1" and form["op"] == ["invokeaction"]


def test_the_kill_switch_api_is_operator_only(act_app):
    runtime, client = act_app
    assert client.post("/actuation/v1/kill").status_code == 401
    assert client.post("/actuation/v1/kill",
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.post("/actuation/v1/kill", headers={"Authorization": "Bearer op-secret"})
    assert r.status_code == 200 and r.json()["killed"]
    refused = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.actuate@v1",
        "input": {"actuator_id": "plug-sim-01", "command": "on", "duration_s": 60}}).json()
    assert refused["ok"] is False and "kill switch" in refused["error"]
    client.post("/actuation/v1/resume", headers={"Authorization": "Bearer op-secret"})
    assert client.get("/actuation/v1/status").json()["killed"] is False


def test_the_kill_switch_api_fails_closed_without_a_token(act_app, monkeypatch):
    monkeypatch.delenv("GAIA_ACTUATION_TOKEN")
    runtime, client = act_app
    assert client.post("/actuation/v1/kill",
                       headers={"Authorization": "Bearer anything"}).status_code == 503


def test_wot_td_for_a_sensor_has_no_actions(tmp_path):
    from gaia.devices.energy import EnergyMeterSim

    td = device_to_td(EnergyMeterSim("em", SimClock(), key_dir=str(tmp_path)),
                      "http://g", "gaia.energy.read@v1", 0.001)
    assert "actions" not in td


# Regression cases found in the 2026-10-16 change audit.
def test_lost_meter_after_on_switches_back_off(rig, monkeypatch):
    def missing(*args):
        raise ValueError("meter stopped reporting")
    monkeypatch.setattr(rig, "_await_effect", missing)
    with pytest.raises(ValueError, match="meter stopped"):
        rig.actuate("plug-1", "on", 60)
    assert not rig._plug("plug-1").relay_is_on


def test_kill_during_before_read_prevents_later_on(rig, monkeypatch):
    read = rig.fleet.read
    def kill_after_read(device_id):
        value = read(device_id)
        rig.kill("operator emergency stop")
        return value
    monkeypatch.setattr(rig.fleet, "read", kill_after_read)
    with pytest.raises(ValueError, match="kill switch"):
        rig.actuate("plug-1", "on", 60)
    assert not rig._plug("plug-1").relay_is_on


def test_auto_off_deadline_does_not_move_while_waiting_for_meter(rig, monkeypatch):
    wait = rig._await_effect
    def delayed(*args):
        rig.clock.advance(1)
        return wait(*args)
    monkeypatch.setattr(rig, "_await_effect", delayed)
    out = rig.actuate("plug-1", "on", 60)
    assert out["auto_off_at"] == "2026-10-16T10:08:00Z"
