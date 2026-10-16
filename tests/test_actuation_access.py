"""Who may switch a plug: the access policy, the lease, the owner, the kill switch.

Found on 2026-10-16: buyer A paid $0.05 for "on, 900 s", and 30 s later (the cooldown) anyone
else could send "off" (paid, through a free hub trial, or straight at GAIA), so A's paid time
ended early although A was charged for a verified effect. Payment proves payment, not
permission. What must hold now:
  * a verified "on" leases the actuator to its caller until its auto-off; meanwhile only that
    caller, the owner or the kill switch may switch it, and anyone else is refused before
    anything happens, with "busy until <time>" and nothing charged;
  * an "on" without a vouched payer still holds its lease, and nobody but the owner or the
    kill switch can end it early;
  * a real plug answers only its owner and the owner's delegates (access: owner); access:
    public is for the simulated demo plug alone;
  * leases survive a restart; who holds one, who owns a plug and who its delegates are is
    never published.
"""

from __future__ import annotations

import json

import pytest

from gaia import actuation as act
from gaia.actuation import ActuationController, ActuatorSpec, load_specs
from gaia.clock import SimClock
from gaia.devices.plug import SmartPlugSim
from gaia.fleet import Fleet
from gaia.payer_identity import Caller

T0 = 1_792_145_220.0  # 2026-10-16T10:07:00Z
HUB = "https://hub.test"
A = Caller("0x" + "a1" * 20, vouched_by=HUB, rail="channel")
B = Caller("0x" + "b2" * 20, vouched_by=HUB, rail="x402")
OWNER = Caller("0x" + "0e" * 20, vouched_by=HUB, rail="channel")


def _spec(**kw) -> ActuatorSpec:
    base = dict(actuator_id="plug-1", label="test heater", kind="sim-plug", expect_on_w=1000,
                off_max_w=10, min_on_s=10, max_on_s=900, cooldown_s=0, effect_timeout_s=2,
                load_w=1500, access="public")
    base.update(kw)
    return ActuatorSpec(**base)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_ACTUATION_KILL", raising=False)
    clock = SimClock(start_epoch=T0)
    fleet = Fleet()
    fleet.add(SmartPlugSim("plug-1", clock, load_w=1500, key_dir=str(tmp_path / "keys")))

    def build(**spec_kw) -> ActuationController:
        ctl = ActuationController(fleet, clock, [_spec(**spec_kw)],
                                  journal_path=tmp_path / "j.jsonl", kill_file=tmp_path / "KILL",
                                  lease_path=tmp_path / "leases.json")
        ctl.POLL_S = 0.01
        return ctl

    return build


@pytest.fixture
def rig(world):
    return world(owner=OWNER.id)


def _relay(ctl) -> bool:
    return ctl._plug("plug-1").relay_is_on


def _events(ctl) -> list[dict]:
    if not ctl.journal_path.exists():
        return []
    return [json.loads(x) for x in ctl.journal_path.read_text().splitlines()]


# ── the lease ─────────────────────────────────────────────────────────────────


def test_a_paid_on_cannot_be_ended_by_anyone_else(world):
    """The 10-16 finding, with the demo plug's own 30 s cooldown."""
    ctl = world(cooldown_s=30)
    out = ctl.actuate("plug-1", "on", 900, caller=A)
    assert out["lease"] == {"holder": A.id, "until": "2026-10-16T10:22:00Z"}
    ctl.clock.advance(31)
    for who, command in ((B, "off"), (B, "on"), (None, "off")):
        with pytest.raises(ValueError, match=r"busy until 2026-10-16T10:22:00Z, nothing charged"):
            ctl.actuate("plug-1", command, 60, caller=who)
        assert _relay(ctl)
    ctl.actuate("plug-1", "off", caller=A)
    assert not _relay(ctl)
    ctl.clock.advance(31)
    assert ctl.actuate("plug-1", "on", 60, caller=B)["lease"]["holder"] == B.id


def test_an_anonymous_caller_is_told_why_it_is_not_the_holder(rig):
    rig.actuate("plug-1", "on", 60, caller=A)
    with pytest.raises(ValueError, match="no payer a trusted hub vouched for"):
        rig.actuate("plug-1", "off")


def test_a_refusal_switches_nothing_records_nothing_and_costs_no_cooldown(rig):
    rig.actuate("plug-1", "on", 60, caller=A)
    before = (len(_events(rig)), rig._state["plug-1"].last_command_at)
    with pytest.raises(ValueError, match="busy until"):
        rig.actuate("plug-1", "off", caller=B)
    assert (len(_events(rig)), rig._state["plug-1"].last_command_at) == before
    assert _relay(rig)


def test_the_lease_ends_at_the_auto_off(rig):
    rig.actuate("plug-1", "on", 60, caller=A)
    rig.clock.advance(60)
    assert rig.actuate("plug-1", "on", 60, caller=B)["lease"]["holder"] == B.id


def test_the_holder_may_switch_again_and_the_lease_follows(rig):
    rig.actuate("plug-1", "on", 60, caller=A)
    rig.clock.advance(10)
    out = rig.actuate("plug-1", "on", 120, caller=A)
    assert out["lease"]["until"] == "2026-10-16T10:09:10Z"
    rig.clock.advance(100)  # past the first lease's end, inside the second
    with pytest.raises(ValueError, match="busy until 2026-10-16T10:09:10Z"):
        rig.actuate("plug-1", "off", caller=B)


def test_the_owner_always_passes_and_can_take_the_actuator(rig):
    rig.actuate("plug-1", "on", 600, caller=A)
    assert rig.actuate("plug-1", "off", caller=OWNER)["effect"]["verified"]
    assert rig.actuate("plug-1", "on", 600, caller=A)["lease"]["holder"] == A.id
    assert rig.actuate("plug-1", "on", 300, caller=OWNER)["lease"]["holder"] == OWNER.id
    with pytest.raises(ValueError, match="busy until"):
        rig.actuate("plug-1", "off", caller=A)


def test_an_on_without_a_vouched_payer_holds_a_lease_nobody_else_can_end(rig):
    out = rig.actuate("plug-1", "on", 600)
    assert out["lease"] == {"holder": None, "until": "2026-10-16T10:17:00Z"}
    for who in (None, A):
        with pytest.raises(ValueError, match="busy until"):
            rig.actuate("plug-1", "off", caller=who)
    rig.actuate("plug-1", "off", caller=OWNER)
    assert not _relay(rig)


def test_the_kill_switch_ends_every_lease(rig):
    rig.actuate("plug-1", "on", 600, caller=A)
    rig.kill("emergency")
    assert not _relay(rig)
    rig.resume()
    assert rig.actuate("plug-1", "on", 60, caller=B)["lease"]["holder"] == B.id
    ended = [e for e in _events(rig) if e["event"] == "lease_end"]
    assert ended[0]["reason"] == "kill switch"


def test_an_unproven_on_ends_the_lease_with_the_plug(rig, monkeypatch):
    rig.actuate("plug-1", "on", 600, caller=A)

    def lost(*args):
        raise ValueError("meter stopped reporting")
    monkeypatch.setattr(rig, "_await_effect", lost)
    with pytest.raises(ValueError, match="meter stopped"):
        rig.actuate("plug-1", "on", 600, caller=A)
    monkeypatch.undo()
    assert not _relay(rig)
    assert rig.actuate("plug-1", "on", 60, caller=B)["lease"]["holder"] == B.id


def test_a_caller_is_a_checked_identity_never_a_string(rig):
    with pytest.raises(TypeError, match="checked identity"):
        rig.actuate("plug-1", "on", 60, caller=A.id)


# ── across a restart ──────────────────────────────────────────────────────────


def test_a_lease_survives_a_restart_and_its_backstop_is_rearmed(world, monkeypatch):
    first = world()
    first.actuate("plug-1", "on", 900, caller=A)
    armed = []
    monkeypatch.setattr(ActuationController, "_backstop",
                        lambda self, aid, gen, seconds: armed.append((aid, seconds)))
    second = world()
    assert armed == [("plug-1", 900.0)]
    assert second.status()["actuators"][0]["leased_until"] == "2026-10-16T10:22:00Z"
    with pytest.raises(ValueError, match="busy until 2026-10-16T10:22:00Z"):
        second.actuate("plug-1", "off", caller=B)
    second.actuate("plug-1", "off", caller=A)
    assert json.loads(second.lease_path.read_text())["leases"] == {}


def test_an_expired_lease_is_not_restored(world):
    first = world()
    first.actuate("plug-1", "on", 60, caller=A)
    first.clock.advance(61)
    assert world()._leases == {}


def test_a_restored_lease_never_outlasts_what_one_on_can_buy(world):
    first = world()
    first.actuate("plug-1", "on", 900, caller=A)
    data = json.loads(first.lease_path.read_text())
    data["leases"]["plug-1"]["until"] += 3600  # as if the clock had gone back an hour
    first.lease_path.write_text(json.dumps(data))
    assert world()._leases == {}


def test_tidying_away_a_lease_that_ran_out_is_journaled_as_its_auto_off(rig):
    rig.actuate("plug-1", "on", 60, caller=A)
    rig.clock.advance(61)
    rig.actuate("plug-1", "off", caller=B)
    assert [e["reason"] for e in _events(rig) if e["event"] == "lease_end"] == ["auto-off"]


def test_an_unreadable_lease_file_starts_without_leases(world, caplog):
    ctl = world()
    ctl.lease_path.write_text("{not json")
    again = world()
    assert again._leases == {} and "unreadable" in caplog.text
    assert again.actuate("plug-1", "on", 60, caller=B)["lease"]["holder"] == B.id


# ── the access policy ─────────────────────────────────────────────────────────


@pytest.fixture
def owned(world):
    return world(access="owner", delegates=(A.id,), owner=OWNER.id)


@pytest.mark.parametrize("who, msg", [
    (None, "only from its owner .and the owner's delegates., and this call carries neither an "
           "owner signature nor a payer a trusted hub vouched for; nothing charged"),
    (B, r"0xb2b2…b2b2 is not the owner; nothing charged"),
])
def test_an_owned_plug_refuses_everyone_else_before_anything_happens(owned, who, msg):
    with pytest.raises(ValueError, match=msg):
        owned.actuate("plug-1", "on", 60, caller=who)
    assert not _relay(owned) and _events(owned) == []


def test_an_owned_plug_takes_its_owner_and_its_delegates(owned):
    assert owned.actuate("plug-1", "on", 60, caller=A)["effect"]["verified"]
    assert owned.actuate("plug-1", "off", caller=OWNER)["effect"]["verified"]


def test_the_status_says_who_may_command_it_but_not_who_they_are(owned):
    owned.actuate("plug-1", "on", 600, caller=A)
    status = owned.status()
    row = status["actuators"][0]
    assert row["access"] == "owner" and row["leased_until"] == "2026-10-16T10:17:00Z"
    assert "owner" not in row and "delegates" not in row
    text = json.dumps(status)
    assert A.id not in text and OWNER.id not in text


# ── configuration ─────────────────────────────────────────────────────────────


def test_the_shipped_demo_plug_stays_public_and_sells_its_consumption():
    specs = load_specs(act.default_config_path())
    assert specs[0].actuator_id == "plug-sim-01" and specs[0].access == "public"
    assert specs[0].owner is None and specs[0].delegates == ()
    assert specs[0].consumption_sale is True and specs[0].consumption_resolution == "hour"


def test_the_example_real_plug_answers_only_its_owner():
    from pathlib import Path

    spec = load_specs(Path(act.default_config_path()).with_name("actuators.example.yaml"))[0]
    assert spec.kind == "tasmota-mqtt" and spec.access == "owner" and spec.owner
    assert spec.consumption_sale is False


def test_an_owner_and_delegates_in_the_operators_file(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text(f"""actuators:
  - id: lab-heater-01
    kind: tasmota-mqtt
    topic: lab_heater
    expect_on_w: 800
    owner: "0x{'0E' * 20}"
    delegates:
      - "0x{'A1' * 20}"
      - "0x{'a1' * 20}"
      - "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV"
""")
    spec = load_specs(p)[0]
    assert spec.access == "owner" and spec.owner == OWNER.id   # owner is the default for real plugs
    assert spec.delegates == (A.id, "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV")


def test_an_unquoted_address_in_yaml_is_refused_not_misread(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text(f"""actuators:
  - id: x
    kind: sim-plug
    load_w: 1500
    expect_on_w: 1000
    access: owner
    owner: "0x{'0e' * 20}"
    delegates: [0x{'a1' * 20}]
""")
    with pytest.raises(ValueError, match="quote wallet addresses"):
        load_specs(p)


REAL = {"kind": "tasmota-mqtt", "topic": "t", "expect_on_w": 800}


@pytest.mark.parametrize("plug, msg", [
    ({**REAL, "access": "public"}, "a real plug answers only its owner"),
    ({**REAL}, "access: owner needs owner"),
])
def test_a_real_plug_cannot_be_public_and_must_have_an_owner(tmp_path, plug, msg):
    p = tmp_path / "a.yaml"
    p.write_text(json.dumps({"actuators": [{"id": "x", **plug}]}))
    with pytest.raises(ValueError, match=msg):
        load_specs(p)


@pytest.mark.parametrize("extra, msg", [
    ({"access": "friends"}, "access must be owner or public"),
    ({"access": "owner"}, "access: owner needs owner"),
    ({"delegates": [A.id]}, "delegates are read only with access: owner"),
    ({"access": "owner", "owner": "0x" + "0e" * 20, "delegates": "0x" + "a1" * 20},
     "delegates must be a list"),
    ({"access": "owner", "owner": "0x" + "0e" * 20, "delegates": ["buyer-1"]}, "not a wallet address"),
    ({"owner": "me"}, "owner: 'me' is not a wallet address"),
    ({"access": "allowlist"}, "allowlist and allow: are gone"),
    ({"allow": [A.id]}, "allowlist and allow: are gone"),
    ({"consumption": {"sale": "yes"}}, "consumption.sale must be true or false"),
    ({"consumption": {"resolution": "minute"}}, "consumption.resolution must be hour or day"),
    ({"consumption": {"history_days": 0}}, "history_days must be 1-366"),
    ({"consumption": {"price": 1}}, "unknown key"),
])
def test_a_bad_access_policy_is_refused_with_a_reason(tmp_path, extra, msg):
    p = tmp_path / "a.yaml"
    p.write_text(json.dumps({"actuators": [{"id": "x", "kind": "sim-plug", "load_w": 1500,
                                            "expect_on_w": 1000, **extra}]}))
    with pytest.raises(ValueError, match=msg):
        load_specs(p)
