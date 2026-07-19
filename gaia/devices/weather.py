"""Weather station simulator — BME280-class T/RH/P plus an anemometer.

Physics kept simple but honest:
  * diurnal temperature: sinusoid peaking mid-afternoon (~15:00);
  * weather fronts: one shared Ornstein-Uhlenbeck process per SITE moves the
    day's baseline temperature and barometric pressure together;
  * humidity anti-correlates with temperature around the front's moisture level;
  * wind: mean-reverting breeze plus occasional gusts.

Two stations on the same :class:`SiteWeather` see the SAME afternoon and the
SAME front — they differ only by per-sensor noise and a small calibration bias.
That shared truth is exactly what the plausibility verifier's sibling check
leans on: a lying station disagrees with its co-located twin.
"""

from __future__ import annotations

import math
import random

from gaia.clock import SimClock
from gaia.devices.base import OrnsteinUhlenbeck, VirtualDevice


class SiteWeather:
    """Shared ground truth for every station at one site."""

    def __init__(self, clock: SimClock, *, seed: int = 0,
                 t_mean: float = 12.0, t_diurnal_amp: float = 6.0,
                 rh_base: float = 65.0):
        self.clock = clock
        rng = random.Random(f"site-weather:{seed}")
        self.t_mean = t_mean
        self.t_diurnal_amp = t_diurnal_amp
        self.rh_base = rh_base
        # Front processes: slow (days-scale) excursions.
        self._t_front = OrnsteinUhlenbeck(rng, mean=0.0, theta=0.02, sigma=1.2)
        self._p_front = OrnsteinUhlenbeck(rng, mean=1013.25, theta=0.03, sigma=2.5)
        self._wind = OrnsteinUhlenbeck(rng, mean=3.0, theta=0.6, sigma=1.8)
        self._gust_rng = rng

    def truth(self) -> dict[str, float]:
        t = self.clock.now()
        hour = self.clock.hour_of_day()
        # Peak at 15:00, trough at 03:00.
        diurnal = self.t_diurnal_amp * math.sin(math.pi * (hour - 9.0) / 12.0)
        t_front = self._t_front.value(t)
        temperature = self.t_mean + diurnal + t_front
        pressure = self._p_front.value(t)
        # Warmer than the daily mean → drier; front humidity rides pressure lows.
        humidity = self.rh_base - 1.8 * (diurnal + t_front) - 0.35 * (pressure - 1013.25)
        humidity = max(5.0, min(100.0, humidity))
        wind = max(0.0, self._wind.value(t))
        if self._gust_rng.random() < 0.04:  # occasional gust on top of the breeze
            wind += self._gust_rng.uniform(2.0, 6.0)
        return {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "wind_mps": wind,
        }


class WeatherStationSim(VirtualDevice):
    model = "GAIA-WS1 (BME280-class + anemometer)"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
    }

    def __init__(self, device_id: str, clock: SimClock, site_weather: SiteWeather, **kw):
        super().__init__(device_id, clock, **kw)
        self.site_weather = site_weather
        # Small fixed calibration bias per unit — real co-located sensors never agree exactly.
        self._bias = {
            "temperature_c": self.rng.uniform(-0.3, 0.3),
            "humidity_pct": self.rng.uniform(-2.0, 2.0),
            "pressure_hpa": self.rng.uniform(-0.5, 0.5),
            "wind_mps": 0.0,
        }

    def sample(self) -> dict[str, float]:
        truth = self.site_weather.truth()
        sigma = {"temperature_c": 0.12, "humidity_pct": 1.2, "pressure_hpa": 0.15, "wind_mps": 0.4}
        return {
            k: truth[k] + self._bias[k] + self.noise(sigma[k])
            for k in self.fields
        }
