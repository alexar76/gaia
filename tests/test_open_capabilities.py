"""Open-relay capabilities + feeder ingest HTTP — free-to-commercialize SKUs."""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from gaia.app import build_app
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.devices import live as livemod
from gaia.devices.feeder import STORE as FEEDER_STORE
from gaia.devices.live_open import CyberNewsJamming, FirmsFireHotspot, SafecastRadiation


FIRMS_CSV = """\
latitude,longitude,bright_ti4,acq_date,acq_time,confidence
35.0,-120.0,380.0,2026-08-11,1205,h
"""

SAFECAST = [
    {"id": 1, "value": 88.5, "unit": "cpm", "latitude": 37.42, "longitude": 141.03},
]

CYBERNEWS = {
    "records": [
        {
            "event_id": "e1",
            "latitude": 56.0,
            "longitude": 20.0,
            "radius_km": 120.0,
            "severity": "high",
        }
    ]
}


def _get_dispatch(by_needle: dict):
    def fake_get(url, headers=None, timeout=None, **kw):
        for needle, payload in by_needle.items():
            if needle in url:
                if isinstance(payload, str):
                    return httpx.Response(
                        200, text=payload, request=httpx.Request("GET", url)
                    )
                return httpx.Response(
                    200, json=payload, request=httpx.Request("GET", url)
                )
        # Open-Meteo mesh + other default live hosts — empty-ish weather
        if "open-meteo.com" in url or "weather.gov" in url:
            return httpx.Response(
                200,
                json={"current": {"temperature_2m": 10.0, "relative_humidity_2m": 50,
                                  "surface_pressure": 1013.0, "wind_speed_10m": 1.0,
                                  "pm2_5": 5.0, "pm10": 8.0, "carbon_dioxide": 420}},
                request=httpx.Request("GET", url),
            )
        if "carbonintensity" in url:
            return httpx.Response(
                200,
                json={"data": [{"intensity": {"actual": 100}}]},
                request=httpx.Request("GET", url),
            )
        if "earthquake.usgs.gov" in url:
            return httpx.Response(
                200,
                json={"features": [{"properties": {"mag": 4.0},
                                    "geometry": {"coordinates": [10.0, 20.0, 5.0]}}]},
                request=httpx.Request("GET", url),
            )
        if "tidesandcurrents" in url:
            return httpx.Response(
                200, json={"data": [{"v": "1.0"}]}, request=httpx.Request("GET", url)
            )
        if "waterservices.usgs.gov" in url:
            return httpx.Response(
                200,
                json={"value": {"timeSeries": []}},
                request=httpx.Request("GET", url),
            )
        if "ndbc.noaa.gov" in url:
            return httpx.Response(
                200,
                text="#YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ATMP WTMP\n"
                     "2026 08 11 12 00 180 5.0 6.0 1.2 8.0 6.0 180 1013.0 20.0 18.0\n",
                request=httpx.Request("GET", url),
            )
        if "opensensemap" in url:
            return httpx.Response(
                200,
                json={"sensors": [{"title": "PM2.5", "lastMeasurement": {"value": "5"}}]},
                request=httpx.Request("GET", url),
            )
        if "ilt-dmz" in url or "SensorThings" in url or "frost" in url:
            return httpx.Response(
                200, json={"value": [{"result": 5.0}]}, request=httpx.Request("GET", url)
            )
        return httpx.Response(404, json={}, request=httpx.Request("GET", url))

    return fake_get


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    monkeypatch.setenv("GAIA_OM_MESH_ENABLED", "0")
    monkeypatch.setenv("GAIA_STA_ENABLED", "0")
    monkeypatch.setenv("GAIA_FEEDER_ENABLED", "1")
    monkeypatch.setenv("GAIA_FEEDER_TOKEN", "feeder-test-token")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    monkeypatch.delenv("GAIA_OPENAQ_API_KEY", raising=False)
    monkeypatch.setattr(
        livemod.httpx,
        "get",
        _get_dispatch(
            {
                "firms.modaps": FIRMS_CSV,
                "safecast.org": SAFECAST,
                "cybernews.space": CYBERNEWS,
            }
        ),
    )
    FEEDER_STORE.clear()
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys"), autotick=False)
    return runtime


def test_live_spec_advertises_open_relay_skus(live_env):
    spec = build_spec(live_env, public_url="http://gaia.test")
    ids = {c.capability_id for c in spec.capabilities}
    assert "gaia.fire.read@v1" in ids
    assert "gaia.radiation.read@v1" in ids
    assert "gaia.jamming.read@v1" in ids
    assert "gaia.adsb.read@v1" in ids
    assert "gaia.ais.read@v1" in ids
    assert "gaia.quake.read@v1" in ids


def test_well_known_lists_open_relay_tools(live_env, monkeypatch):
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", os.environ["GAIA_SIGNING_KEY_PATH"])
    app = build_app(live_env, public_url="http://gaia.test")
    with TestClient(app) as client:
        wk = client.get("/.well-known/ai-market.json").json()
        assert wk["capabilities_count"] >= 11
        man = client.get("/ai-market/v2/manifest").json()
        tool_names = {t.get("name") or t.get("capability_id") for t in man.get("tools") or []}
        # oracle-core may use name == capability_id
        flat = set()
        for t in man.get("tools") or []:
            flat.add(t.get("capability_id") or t.get("name"))
        assert "gaia.fire.read@v1" in flat or any("fire" in str(x) for x in flat)
        assert any("fire" in str(x) for x in flat) or "gaia.fire.read@v1" in tool_names


def test_invoke_fire_radiation_jamming(live_env, monkeypatch):
    app = build_app(live_env, public_url="http://gaia.test")
    with TestClient(app) as client:
        for cap, device in (
            ("gaia.fire.read@v1", "firms-fire-01"),
            ("gaia.radiation.read@v1", "safecast-01"),
            ("gaia.jamming.read@v1", "cybernews-jam-01"),
        ):
            r = client.post(
                "/ai-market/v2/invoke",
                json={
                    "capability_id": cap,
                    "product_id": "gaia.gateway",
                    "input": {"device_id": device},
                },
            )
            assert r.status_code == 200, (cap, r.text)
            data = r.json()
            assert data["ok"] is True
            vals = data["output"]["reading"]["values"]
            assert "latitude" in vals and "longitude" in vals


def test_feeder_ingest_then_adsb_invoke(live_env):
    app = build_app(live_env, public_url="http://gaia.test")
    with TestClient(app) as client:
        bad = client.post(
            "/feeder/v1/ingest",
            json={
                "device_id": "feeder-adsb-01",
                "fields": {"latitude": 40.7, "longitude": -74.0, "altitude_m": 1000},
            },
        )
        assert bad.status_code == 401

        ok = client.post(
            "/feeder/v1/ingest",
            headers={"Authorization": "Bearer feeder-test-token"},
            json={
                "device_id": "feeder-adsb-01",
                "fields": {
                    "latitude": 40.7,
                    "longitude": -74.0,
                    "altitude_m": 3000,
                    "speed_mps": 120,
                },
            },
        )
        assert ok.status_code == 200
        assert ok.json()["ok"] is True

        r = client.post(
            "/ai-market/v2/invoke",
            json={
                "capability_id": "gaia.adsb.read@v1",
                "product_id": "gaia.gateway",
                "input": {"device_id": "feeder-adsb-01"},
            },
        )
        assert r.status_code == 200
        vals = r.json()["output"]["reading"]["values"]
        assert vals["altitude_m"] == pytest.approx(3000.0)


def test_open_devices_carry_license_provenance(tmp_path):
    assert "FIRMS" in FirmsFireHotspot.source or "firms" in FirmsFireHotspot.source.lower()
    assert "CC0" in SafecastRadiation.source
    assert "CC BY" in CyberNewsJamming.source
