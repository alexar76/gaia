"""Live relay devices — mapping, failure semantics, and end-to-end verification.

All HTTP is mocked: ``gaia.devices.live.httpx.get`` is monkeypatched to return
real :class:`httpx.Response` objects built from canned provider JSON (so
``raise_for_status``/``json`` behave exactly as in production) or to raise a
real transport error. Nothing here touches the network.
"""

from __future__ import annotations

import httpx
import pytest

from gaia.clock import SimClock
from gaia.devices import live as livemod
from gaia.devices.base import DeviceOffline
from gaia.devices.live import (
    NWSStation,
    OpenSenseMapBox,
    SensorThingsDatastream,
    build_live_fleet,
)
from gaia.fleet import Fleet
from gaia.plausibility import PlausibilityVerifier

# ── Canned upstream payloads (realistic shapes) ──────────────────────────────

NWS_FULL = {
    "properties": {
        "temperature": {"unitCode": "wmoUnit:degC", "value": 12.4},
        "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 61.0},
        "barometricPressure": {"unitCode": "wmoUnit:Pa", "value": 101_800},
        "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 18.0},  # → 5.0 m/s
    }
}

NWS_NULL_TEMP = {
    "properties": {
        "temperature": {"unitCode": "wmoUnit:degC", "value": None},  # dropped
        "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 55.0},
        "barometricPressure": {"unitCode": "wmoUnit:Pa", "value": 101_300},
        "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 10.8},  # → 3.0 m/s
    }
}

OSM_BOX = {
    "name": "demo box",
    "sensors": [
        {"title": "PM2.5", "unit": "µg/m³", "lastMeasurement": {"value": "7.5"}},
        {"title": "PM10", "unit": "µg/m³", "lastMeasurement": {"value": "12.0"}},
        {"title": "CO2", "unit": "ppm", "lastMeasurement": {"value": "615"}},
        {"title": "VOC", "unit": "index", "lastMeasurement": {"value": "120"}},
        {"title": "Temperature", "unit": "°C", "lastMeasurement": {"value": "13.2"}},  # unmapped
        {"title": "PM1", "unit": "µg/m³"},  # sensor present but never reported → skip
    ],
}

STA_OBS = {
    "@iot.count": 1,
    "value": [
        {
            "@iot.id": 999,
            "phenomenonTime": "2026-07-16T10:00:00Z",
            "result": 8.3,
            "resultTime": "2026-07-16T10:00:05Z",
        }
    ],
}


# ── httpx mocking helpers ─────────────────────────────────────────────────────


def _get_returning(payload, status: int = 200):
    def fake_get(url, headers=None, timeout=None, **kw):
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))
    return fake_get


def _get_dispatch(by_needle: dict[str, dict]):
    def fake_get(url, headers=None, timeout=None, **kw):
        for needle, payload in by_needle.items():
            if needle in url:
                return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        return httpx.Response(404, json={}, request=httpx.Request("GET", url))
    return fake_get


# ── (a) NWS mapper: units ─────────────────────────────────────────────────────


def test_nws_mapper_units(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(NWS_FULL))
    dev = NWSStation("nws-t", SimClock(), station="KNYC", key_dir=tmp_path)
    v = dev.sample()
    assert v["temperature_c"] == pytest.approx(12.4)      # already degC
    assert v["humidity_pct"] == pytest.approx(61.0)       # already percent
    assert v["pressure_hpa"] == pytest.approx(1018.0)     # 101800 Pa → hPa
    assert v["wind_mps"] == pytest.approx(5.0)            # 18 km/h → m/s


# ── (b) NWS null field is dropped, not NaN ────────────────────────────────────


def test_nws_null_field_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(NWS_NULL_TEMP))
    dev = NWSStation("nws-t", SimClock(), station="KNYC", key_dir=tmp_path)
    v = dev.sample()
    assert "temperature_c" not in v
    assert set(v) == {"humidity_pct", "pressure_hpa", "wind_mps"}
    assert all(x == x for x in v.values())  # no NaN slipped in


# ── (c) openSenseMap: title/unit matching incl CO2/VOC, missing skipped ───────


def test_opensensemap_matches_and_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(OSM_BOX))
    dev = OpenSenseMapBox("osm-t", SimClock(), box_id="abc123", key_dir=tmp_path)
    v = dev.sample()
    assert v == {
        "pm2_5_ugm3": 7.5,
        "pm10_ugm3": 12.0,
        "co2_ppm": 615.0,
        "voc_index": 120.0,
    }  # Temperature is unmapped; PM1 (no lastMeasurement) is skipped


# ── (d) SensorThings: latest result → field ───────────────────────────────────


def test_sensorthings_extracts_result(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_dispatch({"Datastreams(42)": STA_OBS}))
    dev = SensorThingsDatastream("sta-t", SimClock(),
                                 datastreams={"42": "pm2_5_ugm3"}, key_dir=tmp_path)
    v = dev.sample()
    assert v == {"pm2_5_ugm3": 8.3}
    assert dev.fields == {"pm2_5_ugm3": "ug/m3"}  # advertised schema = mapped field


# ── (e) upstream failure → DeviceOffline (not a 500) ──────────────────────────


def test_transport_error_raises_device_offline(monkeypatch, tmp_path):
    def boom(url, headers=None, timeout=None, **kw):
        raise httpx.ConnectError("no route to host", request=httpx.Request("GET", url))
    monkeypatch.setattr(livemod.httpx, "get", boom)
    dev = NWSStation("nws-t", SimClock(), key_dir=tmp_path)
    with pytest.raises(DeviceOffline):
        dev.read()


def test_non_200_raises_device_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning({}, status=503))
    dev = NWSStation("nws-t", SimClock(), key_dir=tmp_path)
    with pytest.raises(DeviceOffline):
        dev.read()


# ── (f) a mapped reading is attested and PASSES the verifier ──────────────────


def test_live_reading_attested_and_verifies(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(NWS_FULL))
    clock = SimClock(realtime=False)
    fleet = Fleet()
    fleet.add(NWSStation("nws-01", clock, station="KNYC",
                         site="live-weather", key_dir=tmp_path))
    verifier = PlausibilityVerifier(fleet)

    out = None
    for _ in range(3):  # a couple readings so there is prior history
        clock.advance(60)
        out = fleet.read("nws-01")

    verdict = verifier.check(out["reading"], out["attestation"], require_attestation=True)
    assert verdict.verified, verdict.summary
    assert verdict.score >= 0.9


# ── (g) inject_fault(spike) on a live device is still caught ──────────────────


def test_spike_on_live_device_is_caught(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(NWS_FULL))
    clock = SimClock(realtime=False)
    fleet = Fleet()
    dev = NWSStation("nws-01", clock, station="KNYC",
                     site="live-weather", key_dir=tmp_path)
    fleet.add(dev)
    verifier = PlausibilityVerifier(fleet)

    clock.advance(60)
    fleet.read("nws-01")  # one honest reading for the rate/prev baseline
    dev.inject_fault("spike", fields=["temperature_c"], magnitude=60.0)
    clock.advance(60)
    out = fleet.read("nws-01")

    verdict = verifier.check(out["reading"], out["attestation"], require_attestation=True)
    assert not verdict.verified
    failed = {c.name for c in verdict.checks if not c.ok}
    assert any(n.startswith(("bounds:", "rate:", "zscore:")) for n in failed)


# ── factory + source provenance via fleet status ─────────────────────────────


def test_build_live_fleet_registers_relays_with_source(tmp_path):
    fleet = build_live_fleet(SimClock(realtime=True), key_dir=str(tmp_path))
    assert {d.device_id for d in fleet.devices()} == {"nws-01", "osm-01", "sta-01"}
    by_id = {d["device_id"]: d for d in fleet.status()["devices"]}
    # Every relay's upstream provenance (URL + licence) is surfaced via status.
    assert "api.weather.gov" in by_id["nws-01"]["source"]
    assert all(by_id[i]["source"] for i in ("nws-01", "osm-01", "sta-01"))
