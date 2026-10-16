"""Smart plugs: a relay and the meter that measures only what the relay switches.

An actuation is paid for only when its effect is measured (gaia/actuation.py), so the
meter must see the switched load and nothing else. A whole-house meter would not do:
a kettle somewhere else would "prove" that a heater came on. A metering plug (Shelly
Plug, Tasmota with an energy chip) measures exactly its own socket, so "relay on" and
"power rose" are the same physical fact.

Two kinds, one interface (``set_relay`` + an attested ``read``):

* :class:`SmartPlugSim` — a simulated plug with a configured appliance load (the
  public demo's heater). Same physics contract as every GAIA simulator.
* :class:`TasmotaPlug` — a real Tasmota plug over MQTT. ON is sent together with
  ``PulseTime``, so the plug switches itself off after the paid duration even if GAIA
  is gone; the meter is read on demand with ``Status 8``. GAIA's key signs what the
  plug reported — custody of the report, as with every relay, not a secure element.

Both sign meter readings AND command records with the one device key: the plug is
one identity, whatever it is asked to do or report.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline, VirtualDevice

log = logging.getLogger("gaia.devices.plug")

PLUG_FIELDS = {
    "voltage_v": "V",
    "current_a": "A",
    "power_w": "W",
    "energy_wh": "Wh",
    "relay_on": "bool",
}


class SmartPlug(VirtualDevice):
    """What the actuation controller needs from a plug."""

    fields = dict(PLUG_FIELDS)

    def set_relay(self, on: bool, duration_s: float | None = None) -> None:  # pragma: no cover
        raise NotImplementedError

    @property
    def relay_is_on(self) -> bool:  # pragma: no cover
        raise NotImplementedError


class SmartPlugSim(SmartPlug):
    """A simulated metering plug with one appliance behind it."""

    model = "GAIA-PLUG1 (metering smart plug, simulated)"

    def __init__(self, device_id: str, clock: SimClock, *, load_w: float = 1500.0,
                 standby_w: float = 0.4, **kw: Any):
        super().__init__(device_id, clock, **kw)
        if not 1.0 <= float(load_w) <= 3680.0:  # a 16 A socket at 230 V
            raise ValueError("load_w must be from 1 to 3680 W (one 16 A socket)")
        self.load_w = float(load_w)
        self.standby_w = float(standby_w)
        self._on_until = 0.0
        self._energy_wh = 0.0
        self._last_t: float | None = None
        self._lock = threading.Lock()

    @property
    def relay_is_on(self) -> bool:
        return self.clock.now() < self._on_until

    def set_relay(self, on: bool, duration_s: float | None = None) -> None:
        with self._lock:
            self._integrate(self.clock.now())
            if on:
                if not duration_s or duration_s <= 0:
                    raise ValueError("ON needs a positive duration: plugs never stay on unbounded")
                self._on_until = self.clock.now() + float(duration_s)
            else:
                self._on_until = 0.0

    def _power(self, on: bool) -> float:
        if not on:
            return self.standby_w
        # A resistive appliance: ±1 % with mains wander, never negative.
        return max(0.0, self.load_w * (1.0 + 0.01 * self.rng.uniform(-1.0, 1.0)))

    def _integrate(self, t: float) -> None:
        if self._last_t is not None and t > self._last_t:
            # Piecewise: on until _on_until, off afterwards — so an auto-off that happened
            # between two reads is accounted for, not smeared.
            on_end = min(t, max(self._last_t, self._on_until))
            on_s = max(0.0, on_end - self._last_t)
            off_s = (t - self._last_t) - on_s
            self._energy_wh += (self.load_w * on_s + self.standby_w * off_s) / 3600.0
        self._last_t = t

    def sample(self) -> dict[str, float]:
        with self._lock:
            t = self.clock.now()
            self._integrate(t)
            on = t < self._on_until
            power = self._power(on)
            voltage = 230.0 + self.rng.uniform(-1.5, 1.5)
            return {
                "voltage_v": voltage,
                "current_a": power / voltage,
                "power_w": power,
                "energy_wh": self._energy_wh,
                "relay_on": 1.0 if on else 0.0,
            }


# ── Tasmota over MQTT ─────────────────────────────────────────────────────────


def pulse_time(duration_s: float) -> int:
    """Tasmota PulseTime for a duration: 1-111 are tenths of a second, 112-64900 are
    seconds plus 100. Rounded up, so the plug never switches off before it was paid to."""
    d = float(duration_s)
    if d <= 0:
        raise ValueError("duration must be positive")
    if d <= 11.1:
        return max(1, int(-(-d * 10 // 1)))
    seconds = int(-(-d // 1))
    if seconds > 64_800:
        raise ValueError("Tasmota PulseTime holds at most 64800 s (18 h)")
    return seconds + 100


class MqttLink:
    """One broker connection shared by the operator's plugs (paho-mqtt, optional extra)."""

    def __init__(self, url: str, *, username: str = "", password: str = "",
                 client_factory: Callable[[], Any] | None = None):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme not in ("mqtt", "mqtts") or not parts.hostname:
            raise ValueError("broker must be mqtt://host[:port] or mqtts://host[:port]")
        self.host = parts.hostname
        self.port = parts.port or (8883 if parts.scheme == "mqtts" else 1883)
        self.tls = parts.scheme == "mqtts"
        self._handlers: dict[str, list[Callable[[str, bytes], None]]] = {}
        self._lock = threading.Lock()
        if client_factory is None:
            import paho.mqtt.client as mqtt  # the gaia[mqtt] extra

            def client_factory():
                return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"gaia-actuation-{int(time.time())}")
        self.client = client_factory()
        if username:
            self.client.username_pw_set(username, password or None)
        if self.tls:
            self.client.tls_set()
        self.client.on_message = self._on_message
        self.client.on_connect = self._on_connect
        self._connected = False

    def start(self) -> None:
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = True
        with self._lock:
            topics = list(self._handlers)
        for topic in topics:
            client.subscribe(topic, qos=1)

    def _on_message(self, client, userdata, message):
        with self._lock:
            handlers = list(self._handlers.get(message.topic, ()))
        for handler in handlers:
            try:
                handler(message.topic, message.payload)
            except Exception:  # noqa: BLE001 — a bad payload must not kill the loop
                log.exception("MQTT handler for %s failed", message.topic)

    def subscribe(self, topic: str, handler: Callable[[str, bytes], None]) -> None:
        if any(c in topic for c in "#+\0"):
            raise ValueError("an actuator subscribes to exact topics, never wildcards")
        with self._lock:
            self._handlers.setdefault(topic, []).append(handler)
        if self._connected:
            self.client.subscribe(topic, qos=1)

    def publish(self, topic: str, payload: str) -> None:
        if any(c in topic for c in "#+\0"):
            raise ValueError("refusing to publish to a wildcard topic")
        self.client.publish(topic, payload, qos=1)


class TasmotaPlug(SmartPlug):
    """A real Tasmota metering plug on the operator's broker (topic = its %topic%)."""

    model = "GAIA-PLUG (Tasmota over MQTT)"
    #: A meter report older than this is not evidence of the plug's present state.
    MAX_REPORT_AGE_S = 15.0
    STATUS_WAIT_S = 4.0

    def __init__(self, device_id: str, clock: SimClock, *, link: MqttLink, topic: str,
                 **kw: Any):
        super().__init__(device_id, clock, **kw)
        if not topic or any(c in topic for c in "#+/\0 "):
            raise ValueError("topic is the plug's Tasmota %topic%: one word, no '/' or wildcards")
        self.topic = topic
        self.link = link
        self.source = (f"Tasmota plug '{topic}' via the operator's MQTT broker "
                       f"{link.host}:{link.port}; GAIA signs what the plug reported")
        self._state: dict[str, Any] = {}
        self._fresh = threading.Event()
        link.subscribe(f"stat/{topic}/STATUS8", self._on_status)
        link.subscribe(f"tele/{topic}/SENSOR", self._on_status)
        link.subscribe(f"stat/{topic}/RESULT", self._on_result)
        link.subscribe(f"stat/{topic}/POWER", self._on_power)

    # MQTT callbacks (broker thread) ────────────────────────────────────────

    def _on_status(self, _topic: str, payload: bytes) -> None:
        data = json.loads(payload.decode("utf-8"))
        energy = (data.get("StatusSNS") or data).get("ENERGY")
        if not isinstance(energy, dict):
            return
        self._state.update({
            "power_w": energy.get("Power"), "voltage_v": energy.get("Voltage"),
            "current_a": energy.get("Current"), "total_kwh": energy.get("Total"),
            "at": time.time(),
        })
        self._fresh.set()

    def _on_result(self, _topic: str, payload: bytes) -> None:
        data = json.loads(payload.decode("utf-8"))
        state = data.get("POWER", data.get("POWER1"))
        if state in ("ON", "OFF"):
            self._state["relay"] = state == "ON"

    def _on_power(self, _topic: str, payload: bytes) -> None:
        text = payload.decode("utf-8").strip()
        if text in ("ON", "OFF"):
            self._state["relay"] = text == "ON"

    # Commands ──────────────────────────────────────────────────────────────

    def set_relay(self, on: bool, duration_s: float | None = None) -> None:
        if on:
            if not duration_s or duration_s <= 0:
                raise ValueError("ON needs a positive duration: plugs never stay on unbounded")
            # PulseTime first, in the same Backlog: the plug's own timer is the hard stop.
            self.link.publish(f"cmnd/{self.topic}/Backlog",
                              f"PulseTime1 {pulse_time(duration_s)}; Power1 ON")
        else:
            self.link.publish(f"cmnd/{self.topic}/Power1", "OFF")

    @property
    def relay_is_on(self) -> bool:
        return bool(self._state.get("relay"))

    def sample(self) -> dict[str, float]:
        self._fresh.clear()
        self.link.publish(f"cmnd/{self.topic}/Status", "8")
        self._fresh.wait(self.STATUS_WAIT_S)
        st = dict(self._state)
        age = time.time() - float(st.get("at") or 0)
        if age > self.MAX_REPORT_AGE_S or st.get("power_w") is None:
            raise DeviceOffline(f"{self.device_id}: no fresh energy report from the plug")
        power = float(st["power_w"])
        voltage = float(st.get("voltage_v") or 0.0)
        current = float(st.get("current_a") or 0.0)
        total_wh = float(st.get("total_kwh") or 0.0) * 1000.0
        relay = st.get("relay")
        out = {"power_w": power, "energy_wh": total_wh, "relay_on": 1.0 if relay else 0.0}
        # Some plugs measure mains only through the closed relay and report 0 V when
        # off. That is not a 0 V mains (the physics table would convict it as one), so
        # voltage and current travel only when the plug actually measured them.
        if voltage > 0:
            out.update({"voltage_v": voltage, "current_a": current})
        return out


__all__ = ["PLUG_FIELDS", "MqttLink", "SmartPlug", "SmartPlugSim", "TasmotaPlug", "pulse_time"]
