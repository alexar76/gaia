"""Household energy-meter simulator — Shelly-EM-class V/A/W plus a Wh register.

Load model: a constant standby floor, a fridge compressor duty-cycle
(square wave), an evening activity curve, and stochastic appliance events
(kettle-class kilowatt bursts). Mains voltage wanders mean-revertingly around
230 V. The energy register integrates real power over simulated time — the one
field that must be MONOTONIC, which gives the plausibility verifier a nice
physics invariant to check.
"""

from __future__ import annotations

import math

from gaia.clock import SimClock
from gaia.devices.base import OrnsteinUhlenbeck, VirtualDevice


class EnergyMeterSim(VirtualDevice):
    model = "GAIA-EM1 (Shelly-EM-class)"
    fields = {
        "voltage_v": "V",
        "current_a": "A",
        "power_w": "W",
        "energy_wh": "Wh",
    }

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        self._voltage = OrnsteinUhlenbeck(self.rng, mean=230.0, theta=0.8, sigma=1.2)
        self._energy_wh = 0.0
        self._last_t: float | None = None
        self._appliance_until = 0.0
        self._appliance_w = 0.0

    def _load_w(self, t: float, hour: float) -> float:
        standby = 95.0
        # Fridge: ~55 W, 20 min on / 40 min off.
        fridge = 55.0 if (t % 3600.0) < 1200.0 else 0.0
        # Evening curve: cooking/lights/TV between ~17:00 and 23:00.
        evening = 240.0 * math.exp(-((hour - 20.0) ** 2) / (2 * 2.2 ** 2))
        # Stochastic appliance bursts (kettle/oven class), more likely in the evening.
        if t >= self._appliance_until and self.rng.random() < (0.02 + 0.05 * (17.0 <= hour <= 22.0)):
            self._appliance_w = self.rng.uniform(600.0, 2200.0)
            self._appliance_until = t + self.rng.uniform(120.0, 900.0)
        appliance = self._appliance_w if t < self._appliance_until else 0.0
        return standby + fridge + evening + appliance + self.noise(6.0)

    def sample(self) -> dict[str, float]:
        t = self.clock.now()
        hour = self.clock.hour_of_day()
        voltage = self._voltage.value(t)
        power = max(0.0, self._load_w(t, hour))
        current = power / max(1.0, voltage)
        if self._last_t is not None and t > self._last_t:
            self._energy_wh += power * (t - self._last_t) / 3600.0
        self._last_t = t
        return {
            "voltage_v": voltage,
            "current_a": current,
            "power_w": power,
            "energy_wh": self._energy_wh,
        }
