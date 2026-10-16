"""Pay-on-verified actuation: an agent pays to switch a device, and pays only for the effect.

Reading the physical world is half of what agents need from it; the other half is
changing it — turn on a heater, a pump, an EV charger for N minutes. Here the price
of a command is due only when the command's effect is MEASURED:

    1. safety gates  operator switch, kill switch, the operator's own registry,
                     allowed command, bounded duration, cooldown, one command at a time
    2. before        an attested meter reading of the plug
    3. command       a record (actuator, command, duration, expected effect, seq,
                     nonce, time) signed with the plug's key
    4. apply         a real Tasmota plug gets ON together with PulseTime, so it
                     switches itself off after the paid duration even if GAIA is gone
    5. after         readings until the expected effect shows or the window closes
    6. verdict       evaluate(): signatures by the pinned key, same device, ordered
                     in time, effect within the window, physics plausible

No effect → the plug is switched back off, the call answers ok:false (the hub debits
nothing for that) and the evidence stays in the journal. Effect → the whole bundle is
the result, and the hub's Pay-on-Verified escrow can send it to GAIA's /v1/verify (or
any verifier that runs evaluate()) before the money moves.

Off unless the operator says so (GAIA_ACTUATION_ENABLED=1) and only for actuators the
operator declared (GAIA_ACTUATORS_FILE, YAML). A caller names an actuator id; it can
never name an MQTT topic, a broker or a device the operator did not list.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from oracle_core.signing import Signer

from gaia.attestation import verify_reading
from gaia.devices.base import DeviceOffline
from gaia.devices.plug import SmartPlug
from gaia.plausibility import Check, PlausibilityVerifier, Verdict

log = logging.getLogger("gaia.actuation")

COMMANDS = ("on", "off")
#: Reading timestamps are whole seconds (SimClock.iso); command time is too.
_TS_SLACK_S = 1.0


def _truthy(val: str) -> bool:
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def actuation_enabled() -> bool:
    return _truthy(os.environ.get("GAIA_ACTUATION_ENABLED", ""))


@dataclass(frozen=True)
class ActuatorSpec:
    actuator_id: str
    label: str
    kind: str                      # sim-plug | tasmota-mqtt
    expect_on_w: float             # metered power that proves "on"
    off_max_w: float = 10.0        # metered power that proves "off"
    min_on_s: float = 10.0
    max_on_s: float = 900.0
    cooldown_s: float = 30.0
    effect_timeout_s: float = 20.0
    load_w: float | None = None    # sim-plug: the appliance behind the plug
    topic: str | None = None       # tasmota-mqtt: the plug's %topic%
    latitude: float | None = None
    longitude: float | None = None
    site: str = "actuation-site-1"

    def public(self) -> dict[str, Any]:
        out = {k: v for k, v in asdict(self).items() if v is not None and k != "topic"}
        out["commands"] = list(COMMANDS)
        return out


# ── Evidence: command records and the verdict over a bundle ───────────────────


def _expect_hash(expect: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(expect, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def command_canonical(record: dict[str, Any]) -> str:
    """What the plug's key signs for a command — every field the verdict depends on."""
    return (f"actuation:{record.get('actuator_id', '')}"
            f"|command:{record.get('command', '')}"
            f"|duration_s:{record.get('duration_s', 0)}"
            f"|seq:{record.get('seq', 0)}"
            f"|issued_at:{record.get('issued_at', '')}"
            f"|nonce:{record.get('nonce', '')}"
            f"|expect_sha256:{_expect_hash(record.get('expect') or {})}")


def sign_command(record: dict[str, Any], signer: Signer) -> dict[str, str]:
    return {"algorithm": "ed25519", "public_key": signer.public_key_b64,
            "value": signer.sign_canonical(command_canonical(record)),
            "canonical": "actuation|command|duration_s|seq|issued_at|nonce|expect_sha256"}


def _ts(iso: Any) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _holds(value: Any, op: str, bound: float) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return float(value) >= bound if op == ">=" else float(value) <= bound


def evaluate(bundle: dict[str, Any], *, pubkey: str,
             plausibility: PlausibilityVerifier | None = None) -> Verdict:
    """The verdict over one actuation bundle. Every check is hard: one false fact and
    nothing is owed. ``pubkey`` is the plug's PINNED key (fleet registry), never the
    key the bundle carries — a self-signed bundle proves only that it is consistent."""
    checks: list[Check] = []

    def check(name: str, ok: bool, detail: str) -> bool:
        checks.append(Check(f"actuation:{name}", ok, detail))
        return ok

    act = bundle.get("actuation") if isinstance(bundle, dict) else None
    before = bundle.get("before") if isinstance(bundle, dict) else None
    after = bundle.get("after") if isinstance(bundle, dict) else None
    if not (isinstance(act, dict) and isinstance(before, dict) and isinstance(after, dict)
            and isinstance(act.get("record"), dict) and isinstance(act.get("attestation"), dict)
            and isinstance(before.get("reading"), dict) and isinstance(after.get("reading"), dict)):
        check("bundle", False, "an actuation bundle needs actuation.record/attestation, "
                               "before.reading and after.reading")
        return Verdict(False, 0.0, checks, "malformed actuation bundle")
    record, att = act["record"], act["attestation"]
    b, a = before["reading"], after["reading"]
    actuator = str(record.get("actuator_id", ""))
    expect = record.get("expect") or {}

    signed = (att.get("public_key") == pubkey
              and Signer.verify(command_canonical(record), str(att.get("value", "")), pubkey))
    check("command_signature", signed, "command signed by the plug's pinned key" if signed
          else "command signature does not verify against the plug's pinned key")
    for label, block in (("before", before), ("after", after)):
        r = block["reading"]
        same = r.get("device_id") == actuator
        check(f"{label}_device", same, f"{label} reading is from {r.get('device_id')!r}")
        ok = isinstance(block.get("attestation"), dict) and verify_reading(
            r, block["attestation"], expected_pubkey=pubkey)
        check(f"{label}_signature", ok, f"{label} reading signed by the plug's pinned key"
              if ok else f"{label} reading signature does not verify")

    t_cmd, t_b, t_a = _ts(record.get("issued_at")), _ts(b.get("ts")), _ts(a.get("ts"))
    ordered = (None not in (t_cmd, t_b, t_a)
               and int(b.get("seq", 0)) < int(a.get("seq", 0))
               and t_b <= t_cmd + _TS_SLACK_S and t_cmd <= t_a + _TS_SLACK_S)
    check("order", ordered, "before → command → after, in sequence and time" if ordered
          else "readings and command are out of order")
    within = float(expect.get("within_s", 0) or 0)
    timely = ordered and t_a - t_cmd <= within + 2 * _TS_SLACK_S
    check("window", timely, f"effect read {0 if not ordered else t_a - t_cmd:.0f} s after the "
                            f"command (window {within:.0f} s)")

    field_name = str(expect.get("field", "power_w"))
    op = str(expect.get("op", ">="))
    bound = float(expect.get("value", 0) or 0)
    observed = (a.get("values") or {}).get(field_name)
    effect = op in (">=", "<=") and _holds(observed, op, bound)
    check("effect", effect, f"{field_name} {observed} {op} {bound:g}" if effect
          else f"{field_name} {observed} does not satisfy {op} {bound:g}")
    relay_want = 1.0 if record.get("command") == "on" else 0.0
    relay = (a.get("values") or {}).get("relay_on")
    relay_ok = relay is None or float(relay) == relay_want
    check("relay_state", relay_ok, f"relay_on={relay} after '{record.get('command')}'")

    if plausibility is not None and isinstance(after.get("attestation"), dict):
        pv = plausibility.check(a, after["attestation"])
        check("after_plausible", pv.verified, pv.summary)

    ok = all(c.ok for c in checks)
    failed = [c.name.split(":", 1)[1] for c in checks if not c.ok]
    return Verdict(ok, 1.0 if ok else 0.0, checks,
                   "effect verified: all actuation checks passed" if ok
                   else f"failed: {', '.join(failed[:6])}")


# ── The controller ────────────────────────────────────────────────────────────


@dataclass
class _State:
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_command_at: float = 0.0
    last_seq: int = 0
    on_generation: int = 0


class ActuationController:
    """Runs commands against the operator's actuators, with every safety gate in front."""

    POLL_S = 1.0

    def __init__(self, fleet: Any, clock: Any, specs: list[ActuatorSpec], *,
                 journal_path: str | Path | None = None, kill_file: str | Path | None = None,
                 plausibility: PlausibilityVerifier | None = None):
        self.fleet = fleet
        self.clock = clock
        self.specs = {s.actuator_id: s for s in specs}
        self._state = {s.actuator_id: _State() for s in specs}
        self.journal_path = Path(journal_path) if journal_path else None
        self.kill_file = Path(kill_file) if kill_file else None
        self._killed = _truthy(os.environ.get("GAIA_ACTUATION_KILL", ""))
        self._journal_lock = threading.Lock()
        self._command_gate = threading.Lock()
        self._recent: dict[str, dict[str, Any]] = {}
        self.plausibility = plausibility or PlausibilityVerifier(fleet)

    # Kill switch ─────────────────────────────────────────────────────────────

    @property
    def killed(self) -> bool:
        return self._killed or bool(self.kill_file and self.kill_file.exists())

    def kill(self, reason: str = "") -> dict[str, Any]:
        """Stop everything: refuse new commands and switch every actuator off now."""
        # Serialize emergency stop with the final check and physical command.
        with self._command_gate:
            self._killed = True
        if self.kill_file:
            self.kill_file.parent.mkdir(parents=True, exist_ok=True)
            self.kill_file.write_text(f"{self.clock.iso()} {reason}\n", encoding="utf-8")
        switched = []
        for aid in self.specs:
            try:
                self._plug(aid).set_relay(False)
                switched.append(aid)
            except Exception as exc:  # noqa: BLE001 — keep switching the rest off
                log.error("kill: could not switch %s off: %s", aid, exc)
        self._journal({"event": "kill", "at": self.clock.iso(), "reason": reason[:200],
                       "switched_off": switched})
        return {"killed": True, "switched_off": switched}

    def resume(self) -> dict[str, Any]:
        self._killed = False
        if self.kill_file and self.kill_file.exists():
            self.kill_file.unlink()
        self._journal({"event": "resume", "at": self.clock.iso()})
        return {"killed": self.killed}

    # Registry / status ───────────────────────────────────────────────────────

    def _plug(self, actuator_id: str) -> SmartPlug:
        device = self.fleet.get(actuator_id)
        if not isinstance(device, SmartPlug):
            raise ValueError(f"{actuator_id} is not an actuator")
        return device

    def pubkey(self, actuator_id: str) -> str:
        return self._plug(actuator_id).signer.public_key_b64

    def status(self) -> dict[str, Any]:
        now = self.clock.now()
        out = []
        for aid, spec in self.specs.items():
            st = self._state[aid]
            try:
                plug = self._plug(aid)
                relay = plug.relay_is_on
                pub = plug.signer.public_key_b64
            except ValueError:
                relay, pub = None, None
            out.append({**spec.public(), "relay_on": relay, "device_pubkey": pub,
                        "busy": st.lock.locked(),
                        "cooldown_remaining_s": max(0.0, round(
                            st.last_command_at + spec.cooldown_s - now, 1))})
        return {"enabled": True, "killed": self.killed, "actuators": out, "count": len(out)}

    def evidence(self, command_id: str) -> dict[str, Any] | None:
        return self._recent.get(command_id)

    # The command ─────────────────────────────────────────────────────────────

    def actuate(self, actuator_id: str, command: str, duration_s: float | None = None,
                *, caller: str = "") -> dict[str, Any]:
        spec = self.specs.get(str(actuator_id))
        if spec is None:
            raise ValueError(f"unknown actuator {str(actuator_id)[:60]!r}; this GAIA's "
                             f"actuators: {', '.join(self.specs) or 'none'}")
        if self.killed:
            raise ValueError("actuation is stopped by the operator (kill switch); nothing charged")
        command = str(command or "").strip().lower()
        if command not in COMMANDS:
            raise ValueError(f"command must be one of {', '.join(COMMANDS)}")
        duration: float | None = None
        if command == "on":
            try:
                duration = float(duration_s) if duration_s is not None else None
            except (TypeError, ValueError):
                duration = None
            if duration is None or not (spec.min_on_s <= duration <= spec.max_on_s):
                raise ValueError(f"'on' needs duration_s from {spec.min_on_s:g} to "
                                 f"{spec.max_on_s:g} s for {actuator_id}: nothing stays on unbounded")
        st = self._state[spec.actuator_id]
        if not st.lock.acquire(blocking=False):
            raise ValueError(f"{actuator_id} is executing another command; retry in a few seconds")
        on_attempted = False
        try:
            now = self.clock.now()
            wait = st.last_command_at + spec.cooldown_s - now
            if st.last_command_at and wait > 0:
                raise ValueError(f"{actuator_id} is cooling down: retry in {wait:.0f} s "
                                 f"(cooldown {spec.cooldown_s:g} s between commands)")
            plug = self._plug(spec.actuator_id)
            before = self.fleet.read(spec.actuator_id)

            expect = ({"field": "power_w", "op": ">=", "value": spec.expect_on_w,
                       "within_s": spec.effect_timeout_s} if command == "on" else
                      {"field": "power_w", "op": "<=", "value": spec.off_max_w,
                       "within_s": spec.effect_timeout_s})
            seq = max(st.last_seq + 1, int(time.time() * 1000))
            st.last_seq = seq
            record = {
                "command_id": f"act_{secrets.token_hex(8)}",
                "actuator_id": spec.actuator_id, "command": command,
                "duration_s": duration or 0, "seq": seq, "issued_at": self.clock.iso(),
                "nonce": secrets.token_hex(12), "expect": expect,
            }
            attestation = sign_command(record, plug.signer)
            self._journal({"event": "command", "record": record, "caller": caller[:64]})
            with self._command_gate:
                if self.killed:
                    raise ValueError("actuation is stopped by the operator (kill switch); nothing charged")
                on_attempted = command == "on"
                on_deadline = self.clock.now() + duration if on_attempted else None
                plug.set_relay(command == "on", duration)
            st.last_command_at = self.clock.now()
            if command == "on":
                st.on_generation += 1
                self._backstop(spec.actuator_id, st.on_generation, float(duration or 0))
            else:
                st.on_generation += 1  # an OFF cancels any pending backstop's meaning

            after = self._await_effect(spec, expect)
            bundle = {"actuation": {"record": record, "attestation": attestation},
                      "before": before, "after": after}
            verdict = evaluate(bundle, pubkey=plug.signer.public_key_b64,
                               plausibility=self.plausibility)
            self._recent[record["command_id"]] = {**bundle, "verdict": verdict.to_dict()}
            while len(self._recent) > 500:
                self._recent.pop(next(iter(self._recent)))
            self._journal({"event": "outcome", "command_id": record["command_id"],
                           "verified": verdict.verified, "summary": verdict.summary,
                           "observed_w": (after.get("reading", {}).get("values") or {}).get("power_w")})
            if not verdict.verified:
                raise ValueError(
                    f"effect not observed for {spec.actuator_id} '{command}': {verdict.summary}; "
                    f"nothing charged. Evidence: GET /actuation/v1/evidence/{record['command_id']}")
            return {
                **bundle,
                "effect": {"verified": True, "summary": verdict.summary,
                           "expected": expect,
                           "observed": (after["reading"].get("values") or {}).get(expect["field"])},
                # A demo plug's appliance exists only in software; say so in the answer
                # itself, not only in the actuator list.
                "simulated": spec.kind == "sim-plug",
                "auto_off_at": (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(on_deadline))
                                if command == "on" else None),
                "verify_with": "POST this result to GAIA /v1/verify, or let the hub's "
                               "Pay-on-Verified escrow do it (invoke with a verify block)",
            }
        except Exception:
            # Includes missing meter reports, verifier/journal failures, and a
            # transport error after the plug may already have accepted ON.
            if on_attempted:
                try:
                    plug.set_relay(False)
                except Exception as exc:  # noqa: BLE001
                    log.error("could not switch %s back off: %s", spec.actuator_id, exc)
            raise
        finally:
            st.lock.release()

    def _await_effect(self, spec: ActuatorSpec, expect: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + spec.effect_timeout_s
        while True:
            try:
                after = self.fleet.read(spec.actuator_id)
            except DeviceOffline:
                after = None
            if after is not None and _holds((after["reading"].get("values") or {}).get(
                    expect["field"]), expect["op"], float(expect["value"])):
                return after
            if time.monotonic() >= deadline:
                if after is None:
                    raise ValueError(f"{spec.actuator_id} stopped reporting after the command; "
                                     "nothing charged")
                return after
            time.sleep(self.POLL_S)

    def _backstop(self, actuator_id: str, generation: int, duration: float) -> None:
        """A second, GAIA-side OFF at the paid deadline — the plug's own timer is the
        first. Skipped if a later command superseded this ON."""
        def fire() -> None:
            st = self._state[actuator_id]
            if st.on_generation != generation:
                return
            try:
                self._plug(actuator_id).set_relay(False)
                self._journal({"event": "auto_off", "actuator_id": actuator_id,
                               "at": self.clock.iso()})
            except Exception as exc:  # noqa: BLE001
                log.error("backstop OFF for %s failed: %s", actuator_id, exc)

        timer = threading.Timer(duration + 2.0, fire)
        timer.daemon = True
        timer.start()

    def _journal(self, entry: dict[str, Any]) -> None:
        if self.journal_path is None:
            return
        with self._journal_lock:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")


# ── Configuration ─────────────────────────────────────────────────────────────


def _spec_from(raw: dict[str, Any]) -> ActuatorSpec:
    kind = str(raw.get("kind", ""))
    if kind not in ("sim-plug", "tasmota-mqtt"):
        raise ValueError(f"actuator kind must be sim-plug or tasmota-mqtt, not {kind!r}")
    aid = str(raw.get("id", ""))
    if not aid or len(aid) > 64 or not all(c.isalnum() or c in "-_" for c in aid):
        raise ValueError("actuator id: 1-64 letters, digits, '-' or '_'")
    spec = ActuatorSpec(
        actuator_id=aid, label=str(raw.get("label", aid))[:120], kind=kind,
        expect_on_w=float(raw["expect_on_w"]), off_max_w=float(raw.get("off_max_w", 10.0)),
        min_on_s=float(raw.get("min_on_s", 10.0)), max_on_s=float(raw.get("max_on_s", 900.0)),
        cooldown_s=float(raw.get("cooldown_s", 30.0)),
        effect_timeout_s=float(raw.get("effect_timeout_s", 20.0)),
        load_w=float(raw["load_w"]) if raw.get("load_w") is not None else None,
        topic=str(raw["topic"]) if raw.get("topic") else None,
        latitude=float(raw["latitude"]) if raw.get("latitude") is not None else None,
        longitude=float(raw["longitude"]) if raw.get("longitude") is not None else None,
        site=str(raw.get("site", "actuation-site-1")),
    )
    if not (0 < spec.min_on_s <= spec.max_on_s <= 64_800):
        raise ValueError(f"{aid}: need 0 < min_on_s <= max_on_s <= 64800")
    if not (0 < spec.off_max_w < spec.expect_on_w):
        raise ValueError(f"{aid}: need 0 < off_max_w < expect_on_w, or on and off look alike")
    if not (1 <= spec.effect_timeout_s <= 120 and spec.cooldown_s >= 0):
        raise ValueError(f"{aid}: effect_timeout_s 1-120 s, cooldown_s >= 0")
    if kind == "sim-plug" and (spec.load_w is None or spec.load_w < spec.expect_on_w):
        raise ValueError(f"{aid}: a sim-plug needs load_w >= expect_on_w")
    if kind == "tasmota-mqtt" and not spec.topic:
        raise ValueError(f"{aid}: a tasmota-mqtt actuator needs its topic")
    return spec


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "actuators.yaml"


def load_specs(path: str | Path | None = None) -> list[ActuatorSpec]:
    import yaml

    p = Path(path or os.environ.get("GAIA_ACTUATORS_FILE", "") or default_config_path())
    if not p.is_file():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    items = raw.get("actuators") or []
    if not isinstance(items, list):
        raise ValueError(f"{p}: 'actuators' must be a list")
    specs = [_spec_from(item) for item in items]
    ids = [s.actuator_id for s in specs]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{p}: duplicate actuator id")
    return specs


def build_controller(fleet: Any, clock: Any, *, key_dir: str) -> ActuationController | None:
    """Register the operator's plugs and return the controller, or None when off."""
    if not actuation_enabled():
        return None
    from gaia.devices.plug import MqttLink, SmartPlugSim, TasmotaPlug

    specs = load_specs()
    link: MqttLink | None = None
    registered: list[ActuatorSpec] = []
    for spec in specs:
        try:
            if spec.kind == "sim-plug":
                fleet.add(SmartPlugSim(spec.actuator_id, clock, load_w=float(spec.load_w or 0),
                                       site=spec.site, key_dir=key_dir))
            else:
                if link is None:
                    broker = os.environ.get("GAIA_MQTT_BROKER", "").strip()
                    if not broker:
                        raise ValueError("GAIA_MQTT_BROKER is not set")
                    link = MqttLink(broker, username=os.environ.get("GAIA_MQTT_USERNAME", ""),
                                    password=os.environ.get("GAIA_MQTT_PASSWORD", ""))
                    link.start()
                fleet.add(TasmotaPlug(spec.actuator_id, clock, link=link,
                                      topic=str(spec.topic), site=spec.site, key_dir=key_dir))
            registered.append(spec)
        except Exception as exc:  # noqa: BLE001 — one bad plug must not take GAIA down
            log.error("actuator %s not registered: %s", spec.actuator_id, exc)
    data_dir = Path(os.environ.get("GAIA_ACTUATION_DIR", "") or Path(key_dir).parent)
    return ActuationController(fleet, clock, registered,
                               journal_path=data_dir / "actuation.jsonl",
                               kill_file=data_dir / "ACTUATION_KILL")


__all__ = ["ActuatorSpec", "ActuationController", "COMMANDS", "actuation_enabled",
           "build_controller", "command_canonical", "evaluate", "load_specs", "sign_command"]
