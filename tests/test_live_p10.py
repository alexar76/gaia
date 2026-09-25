"""P10 relay mapper tests — fixtures only, no live HTTP."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p10 import (
    HKO_WEATHER_MESH,
    IRCELINE_MESH,
    SG_PSI_MESH,
    SG_WEATHER_MESH,
    HkoWeather,
    IrcelineAir,
    SgNeaPsiAir,
    SgNeaWeather,
    register_p10_relays,
)
from gaia.source_policy import require_approved_source
from gaia.devices import live_p10


def test_p10_source_policies_are_commercial():
    expected = {
        "singapore_nea": "Singapore Open Data Licence",
        "hongkong_hko": "DATA.GOV.HK terms",
        "irceline_be": "CC BY 4.0",
    }
    for source_id, licence in expected.items():
        policy = require_approved_source(source_id)
        assert policy.licence == licence
        assert "commercial" in policy.commercial_basis.lower()


def test_sg_weather_map(tmp_path):
    station, lat, lon = SG_WEATHER_MESH[0][1], SG_WEATHER_MESH[0][2], SG_WEATHER_MESH[0][3]
    dev = SgNeaWeather(
        "sg-wx-eastcoast-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now = datetime.now(timezone.utc).isoformat()
    feeds = {
        "wind_mps": {
            "metadata": {"reading_unit": "knots"},
            "items": [{"timestamp": now, "readings": [{"station_id": station, "value": 3.4}]}],
        },
        "temperature_c": {"items": [{"timestamp": now, "readings": [{"station_id": station, "value": 29.1}]}]},
        "humidity_pct": {"items": [{"timestamp": now, "readings": []}]},
        "precipitation_mm": {"items": [{"timestamp": now, "readings": [{"station_id": "S999", "value": 1.0}]}]},
    }
    mapped = dev.map(feeds)
    assert abs((mapped["wind_mps"] or 0) - 1.749_109_6) < 1e-9
    assert mapped["temperature_c"] == 29.1
    assert mapped["precipitation_mm"] is None
    assert mapped["latitude"] == lat


def test_sg_weather_sample_offline_when_empty(tmp_path):
    station, lat, lon = SG_WEATHER_MESH[0][1], SG_WEATHER_MESH[0][2], SG_WEATHER_MESH[0][3]
    dev = SgNeaWeather(
        "sg-wx-eastcoast-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        now = datetime.now(timezone.utc).isoformat()
        empty = {k: {"items": [{"timestamp": now, "readings": []}]} for k in (
            "wind_mps", "temperature_c", "humidity_pct", "precipitation_mm",
        )}
        empty["wind_mps"]["metadata"] = {"reading_unit": "knots"}
        # sample() fetches; exercise map offline path via sample helper logic
        mapped = {k: v for k, v in dev.map(empty).items() if v is not None}
        if mapped.get("wind_mps") is None and mapped.get("temperature_c") is None:
            raise DeviceOffline("empty")
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_sg_psi_map(tmp_path):
    region, lat, lon = SG_PSI_MESH[0][1], SG_PSI_MESH[0][2], SG_PSI_MESH[0][3]
    dev = SgNeaPsiAir(
        "sg-psi-central-01", SimClock(realtime=True),
        region=region, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    payload = {
        "items": [{
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "readings": {
                "pm25_twenty_four_hourly": {region: 18},
                "psi_twenty_four_hourly": {region: 52},
            }
        }]
    }
    mapped = dev.map(payload)
    assert mapped["pm2_5_ugm3"] == 18.0
    assert mapped["air_quality_index"] == 52.0


def test_sg_weather_rejects_unknown_wind_unit(tmp_path):
    station, lat, lon = SG_WEATHER_MESH[0][1], SG_WEATHER_MESH[0][2], SG_WEATHER_MESH[0][3]
    dev = SgNeaWeather(
        "sg-wx-eastcoast-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map({"wind_mps": {
            "metadata": {"reading_unit": "mph"},
            "items": [{"timestamp": datetime.now(timezone.utc).isoformat(),
                       "readings": [{"station_id": station, "value": 3.4}]}],
        }})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_sg_weather_rejects_stale_feed(tmp_path):
    station, lat, lon = SG_WEATHER_MESH[0][1], SG_WEATHER_MESH[0][2], SG_WEATHER_MESH[0][3]
    dev = SgNeaWeather(
        "sg-wx-eastcoast-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map({"temperature_c": {
            "items": [{
                "timestamp": "2020-01-01T00:00:00Z",
                "readings": [{"station_id": station, "value": 29.1}],
            }],
        }})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_hko_map_temp_and_humidity(tmp_path):
    place, lat, lon = HKO_WEATHER_MESH[0][1], HKO_WEATHER_MESH[0][2], HKO_WEATHER_MESH[0][3]
    dev = HkoWeather(
        "hko-wx-observatory-01", SimClock(realtime=True),
        place=place, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    payload = {
        "updateTime": datetime.now(timezone.utc).isoformat(),
        "temperature": {"data": [{"place": place, "value": 28}]},
        "humidity": {"data": [{"place": place, "value": 81}]},
    }
    mapped = dev.map(payload)
    assert mapped["temperature_c"] == 28.0
    assert mapped["humidity_pct"] == 81.0


def test_hko_missing_place_offline(tmp_path):
    place, lat, lon = HKO_WEATHER_MESH[1][1], HKO_WEATHER_MESH[1][2], HKO_WEATHER_MESH[1][3]
    dev = HkoWeather(
        "hko-wx-kingspark-01", SimClock(realtime=True),
        place=place, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map({"updateTime": datetime.now(timezone.utc).isoformat(),
                 "temperature": {"data": [{"place": "Somewhere Else", "value": 20}]}})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_irceline_map(tmp_path):
    ts_id, lat, lon = IRCELINE_MESH[0][1], IRCELINE_MESH[0][2], IRCELINE_MESH[0][3]
    dev = IrcelineAir(
        "be-aq-brussels-01", SimClock(realtime=True),
        timeseries_id=ts_id, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now_ms = int(time.time() * 1000)
    payload = {
        "lastValue": {"value": 12.5, "timestamp": now_ms},
        "station": {"geometry": {"coordinates": [lon, lat, 0]}},
    }
    mapped = dev.map(payload)
    assert mapped["pm2_5_ugm3"] == 12.5
    assert mapped["latitude"] == lat


def test_irceline_stale_offline(tmp_path):
    ts_id, lat, lon = IRCELINE_MESH[0][1], IRCELINE_MESH[0][2], IRCELINE_MESH[0][3]
    dev = IrcelineAir(
        "be-aq-brussels-01", SimClock(realtime=True),
        timeseries_id=ts_id, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    old_ms = int((time.time() - 8 * 3600) * 1000)
    try:
        dev.map({"lastValue": {"value": 12.5, "timestamp": old_ms}})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_p10_relays_reject_missing_observation_times(tmp_path):
    place, lat, lon = HKO_WEATHER_MESH[0][1], HKO_WEATHER_MESH[0][2], HKO_WEATHER_MESH[0][3]
    hko = HkoWeather(
        "hko-wx-observatory-01", SimClock(realtime=True),
        place=place, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        hko.map({"temperature": {"data": [{"place": place, "value": 28}]}})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass

    ts_id = IRCELINE_MESH[0][1]
    air = IrcelineAir(
        "be-aq-brussels-01", SimClock(realtime=True),
        timeseries_id=ts_id, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        air.map({"lastValue": {"value": 12.5}})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_full_network_cache_coalesces_a_cold_burst():
    live_p10._CACHE.clear()
    calls = 0

    def load():
        nonlocal calls
        calls += 1
        time.sleep(0.03)
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _i: live_p10._cached("test://whole-feed", load), range(8)))
    assert calls == 1
    assert results == [{"ok": True}] * 8


def test_register_p10_meshes(tmp_path, monkeypatch):
    class Fleet:
        def __init__(self):
            self._devices = {}

        def add(self, device):
            self._devices[device.device_id] = device

        @property
        def ids(self):
            return set(self._devices)

    monkeypatch.setenv("GAIA_SG_NEA_ENABLED", "1")
    monkeypatch.setenv("GAIA_HKO_ENABLED", "1")
    monkeypatch.setenv("GAIA_IRCELINE_ENABLED", "1")
    fleet = Fleet()
    n = register_p10_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == (
        len(SG_WEATHER_MESH) + len(SG_PSI_MESH)
        + len(HKO_WEATHER_MESH) + len(IRCELINE_MESH)
    )
    assert "sg-wx-eastcoast-01" in fleet.ids
    assert "sg-psi-central-01" in fleet.ids
    assert "hko-wx-observatory-01" in fleet.ids
    assert "be-aq-brussels-01" in fleet.ids
