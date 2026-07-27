"""Physics sanity + fault modes + determinism of the virtual devices."""

from __future__ import annotations

import pytest

from gaia.clock import SimClock
from gaia.devices import AirQualitySim, EnergyMeterSim, SiteWeather, WeatherStationSim
from gaia.devices.base import DeviceOffline


def _weather(tmp_path, seed=0):
    clock = SimClock()
    site = SiteWeather(clock, seed=seed)
    ws = WeatherStationSim("ws-t", clock, site, seed=seed, key_dir=tmp_path)
    return clock, site, ws


def test_weather_diurnal_cycle(tmp_path):
    clock, _, ws = _weather(tmp_path)
    temps_by_hour = {}
    for _ in range(24 * 12):  # 5-minute steps over a day
        clock.advance(300)
        v = ws.sample()
        temps_by_hour.setdefault(int(clock.hour_of_day()), []).append(v["temperature_c"])
    afternoon = sum(temps_by_hour[15]) / len(temps_by_hour[15])
    night = sum(temps_by_hour[3]) / len(temps_by_hour[3])
    assert afternoon > night + 4.0  # diurnal amplitude is visible through the noise


def test_weather_humidity_anticorrelates_with_temperature(tmp_path):
    clock, _, ws = _weather(tmp_path)
    pairs = []
    for _ in range(24 * 12):
        clock.advance(300)
        v = ws.sample()
        pairs.append((v["temperature_c"], v["humidity_pct"]))
    n = len(pairs)
    mt = sum(t for t, _ in pairs) / n
    mh = sum(h for _, h in pairs) / n
    cov = sum((t - mt) * (h - mh) for t, h in pairs) / n
    assert cov < 0  # warmer → drier


def test_colocated_stations_agree(tmp_path):
    clock = SimClock()
    site = SiteWeather(clock, seed=7)
    a = WeatherStationSim("ws-a", clock, site, seed=1, key_dir=tmp_path)
    b = WeatherStationSim("ws-b", clock, site, seed=2, key_dir=tmp_path)
    for _ in range(50):
        clock.advance(60)
        va, vb = a.sample(), b.sample()
        assert abs(va["temperature_c"] - vb["temperature_c"]) < 2.5
        assert abs(va["pressure_hpa"] - vb["pressure_hpa"]) < 2.0


def test_air_quality_rush_hour_and_pm_ordering(tmp_path):
    clock = SimClock()  # anchor is a Thursday
    aq = AirQualitySim("aq-t", clock, seed=3, key_dir=tmp_path)
    by_hour = {}
    for _ in range(24 * 12):
        clock.advance(300)
        v = aq.sample()
        assert v["pm10_ugm3"] >= v["pm2_5_ugm3"]  # coarse fraction is additive
        by_hour.setdefault(int(clock.hour_of_day()), []).append(v["pm2_5_ugm3"])
    rush = sum(by_hour[8]) / len(by_hour[8])
    calm = sum(by_hour[2]) / len(by_hour[2])
    assert rush > calm


def test_energy_register_is_monotonic(tmp_path):
    clock = SimClock()
    em = EnergyMeterSim("em-t", clock, seed=4, key_dir=tmp_path)
    last = -1.0
    for _ in range(200):
        clock.advance(60)
        v = em.sample()
        assert v["energy_wh"] >= last
        last = v["energy_wh"]
        assert 180.0 <= v["voltage_v"] <= 260.0
        assert v["power_w"] >= 0


def test_fault_spike_and_stuck_and_dropout(tmp_path):
    clock, _, ws = _weather(tmp_path)
    clock.advance(60)
    honest = ws.read()["reading"]["values"]["temperature_c"]

    ws.inject_fault("spike", fields=["temperature_c"], magnitude=40.0)
    clock.advance(60)
    spiked = ws.read()["reading"]["values"]["temperature_c"]
    assert spiked > honest + 30.0

    ws.inject_fault("stuck")
    clock.advance(60)
    first = ws.read()["reading"]["values"]
    clock.advance(600)
    second = ws.read()["reading"]["values"]
    assert first == second  # frozen

    ws.inject_fault("dropout")
    with pytest.raises(DeviceOffline):
        ws.read()

    ws.clear_fault()
    clock.advance(60)
    assert ws.read()["reading"]["values"]["temperature_c"] != spiked


def test_determinism_same_seed_same_world(tmp_path):
    def run(dirname):
        clock = SimClock()
        site = SiteWeather(clock, seed=42)
        ws = WeatherStationSim("ws-d", clock, site, seed=42, key_dir=tmp_path / dirname)
        out = []
        for _ in range(20):
            clock.advance(60)
            out.append(ws.sample())
        return out

    assert run("a") == run("b")
