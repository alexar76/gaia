"""Air-quality simulator — SDS011-class particulates + SCD30-class CO₂/VOC.

Patterns modelled:
  * PM2.5 baseline with morning/evening traffic humps (weekdays only) on top of
    a slow mean-reverting background;
  * PM10 tracks PM2.5 with a coarse-fraction multiplier;
  * CO₂ follows an occupancy cycle (office hours) over the ~420 ppm outdoor floor;
  * VOC loosely correlates with CO₂ (people and their solvents arrive together).
"""

from __future__ import annotations

import math

from gaia.clock import SimClock
from gaia.devices.base import OrnsteinUhlenbeck, VirtualDevice


def _hump(hour: float, center: float, width: float) -> float:
    """Gaussian bump on the daily axis."""
    return math.exp(-((hour - center) ** 2) / (2 * width * width))


class AirQualitySim(VirtualDevice):
    model = "GAIA-AQ1 (SDS011-class PM + SCD30-class CO2)"
    fields = {
        "pm2_5_ugm3": "ug/m3",
        "pm10_ugm3": "ug/m3",
        "co2_ppm": "ppm",
        "voc_index": "index",
    }

    def __init__(self, device_id: str, clock: SimClock, **kw):
        super().__init__(device_id, clock, **kw)
        self._pm_bg = OrnsteinUhlenbeck(self.rng, mean=9.0, theta=0.05, sigma=1.5)

    def sample(self) -> dict[str, float]:
        t = self.clock.now()
        hour = self.clock.hour_of_day()
        weekday = self.clock.day_of_week() < 5

        pm_bg = max(1.0, self._pm_bg.value(t))
        traffic = (8.0 * _hump(hour, 8.0, 1.3) + 6.5 * _hump(hour, 18.0, 1.6)) if weekday else 1.5 * _hump(hour, 14.0, 3.0)
        pm2_5 = max(0.5, pm_bg + traffic + self.noise(0.8))
        pm10 = max(pm2_5, pm2_5 * 1.6 + self.noise(1.5))

        # Occupancy: ramps in over office hours on weekdays.
        occupancy = _hump(hour, 13.0, 3.2) if weekday else 0.15 * _hump(hour, 15.0, 4.0)
        co2 = 420.0 + 420.0 * occupancy + self.noise(12.0)
        voc = max(0.0, 60.0 + 180.0 * occupancy + self.noise(8.0))

        return {"pm2_5_ugm3": pm2_5, "pm10_ugm3": pm10, "co2_ppm": co2, "voc_index": voc}
