"""P7 relay mapper tests — fixtures only, no live HTTP."""

from __future__ import annotations

import base64

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p7 import (
    DMI_MESH,
    FROST_MESH,
    NRW_MESH,
    SEPA_MESH,
    DmiMetObs,
    MetNorwayFrost,
    NrwRiver,
    SepaRiver,
    register_p7_relays,
)
from gaia.source_policy import require_approved_source


def test_p7_source_policies_are_commercial():
    expected = {
        "met_norway_frost": "CC BY 4.0 + NLOD",
        "dmi_metobs": "CC BY 4.0",
        "sepa_kiwis": "Open Government Licence",
        "nrw_river": "Open Government Licence",
    }
    for source_id, licence in expected.items():
        policy = require_approved_source(source_id)
        assert policy.licence == licence
        assert "commercial" in policy.commercial_basis.lower()


def test_frost_map(tmp_path):
    dev = MetNorwayFrost(
        "frost-blindern-01",
        SimClock(realtime=True),
        client_id="test-client-id",
        source_id="SN18700",
        key_dir=str(tmp_path),
    )
    assert "Authorization" in dev.headers
    assert dev.headers["Authorization"].startswith("Basic ")
    payload = {
        "data": [
            {
                "sourceId": "SN18700:0",
                "referenceTime": "2026-09-08T12:00:00.000Z",
                "observations": [
                    {"elementId": "air_temperature", "value": 11.2, "unit": "degC"},
                    {"elementId": "relative_humidity", "value": 72.0, "unit": "percent"},
                    {"elementId": "air_pressure_at_sea_level", "value": 1012.0, "unit": "hPa"},
                    {"elementId": "wind_speed", "value": 3.4, "unit": "m/s"},
                ],
            }
        ]
    }
    mapped = dev.map(payload)
    assert mapped["temperature_c"] == 11.2
    assert mapped["humidity_pct"] == 72.0
    assert mapped["pressure_hpa"] == 1012.0
    assert mapped["wind_mps"] == 3.4
    with pytest.raises(DeviceOffline):
        dev.map({"data": []})


def test_dmi_map_and_sample_payload(tmp_path):
    dev = DmiMetObs(
        "dmi-cph-01",
        SimClock(realtime=True),
        station_id="06180",
        key_dir=str(tmp_path),
    )
    payloads = {
        "temp_dry": {
            "features": [
                {
                    "geometry": {"coordinates": [12.6454, 55.614]},
                    "properties": {"parameterId": "temp_dry", "value": 14.1},
                }
            ]
        },
        "humidity": {
            "features": [
                {
                    "geometry": {"coordinates": [12.6454, 55.614]},
                    "properties": {"parameterId": "humidity", "value": 95.0},
                }
            ]
        },
        "pressure_at_sea": {
            "features": [
                {
                    "geometry": {"coordinates": [12.6454, 55.614]},
                    "properties": {"parameterId": "pressure_at_sea", "value": 1004.4},
                }
            ]
        },
        "wind_speed": {
            "features": [
                {
                    "geometry": {"coordinates": [12.6454, 55.614]},
                    "properties": {"parameterId": "wind_speed", "value": 2.57},
                }
            ]
        },
    }
    mapped = dev.map(payloads)
    assert mapped["temperature_c"] == 14.1
    assert mapped["humidity_pct"] == 95.0
    assert mapped["pressure_hpa"] == 1004.4
    assert mapped["wind_mps"] == 2.57
    assert mapped["latitude"] == 55.614


def test_sepa_map_stage_and_flow(tmp_path):
    sta = SEPA_MESH[0][1]
    dev = SepaRiver(
        "sepa-abington-01",
        SimClock(realtime=True),
        station_no=sta,
        latitude=55.4869,
        longitude=-3.6906,
        key_dir=str(tmp_path),
    )
    stage = [
        {
            "station_no": sta,
            "station_latitude": "55.48691533",
            "station_longitude": "-3.69056827",
            "data": [["2026-09-08T19:45:00.000Z", 0.591]],
        }
    ]
    flow = [
        {
            "station_no": sta,
            "data": [["2026-09-08T19:00:00.000Z", 4.791]],
        }
    ]
    mapped = dev.map((stage, flow))
    assert mapped["gage_height_m"] == 0.591
    assert mapped["discharge_m3s"] == 4.791
    with pytest.raises(DeviceOffline):
        dev.map(([], []))


def test_nrw_map_flexible(tmp_path):
    loc = NRW_MESH[0][1]
    dev = NrwRiver(
        "nrw-4016-01",
        SimClock(realtime=True),
        api_key="test-nrw-key",
        location=loc,
        latitude=51.48,
        longitude=-3.18,
        key_dir=str(tmp_path),
    )
    assert "Ocp-Apim-Subscription-Key" in dev.headers
    mapped = dev.map(
        [
            {
                "Location": loc,
                "latitude": 51.4816,
                "longitude": -3.1791,
                "parameters": [
                    {"parameterNameEN": "Level", "latestValue": 1.23},
                    {"parameterNameEN": "Flow", "latestValue": 8.5},
                ],
            }
        ]
    )
    assert mapped["gage_height_m"] == 1.23
    assert mapped["discharge_m3s"] == 8.5
    with pytest.raises(DeviceOffline):
        dev.map([])


def test_register_p7_respects_env(tmp_path, monkeypatch):
    class Fleet:
        def __init__(self):
            self.ids: list[str] = []

        def add(self, device):
            self.ids.append(device.device_id)

    fleet = Fleet()
    monkeypatch.delenv("GAIA_FROST_CLIENT_ID", raising=False)
    monkeypatch.delenv("GAIA_NRW_API_KEY", raising=False)
    monkeypatch.setenv("GAIA_DMI_ENABLED", "1")
    monkeypatch.setenv("GAIA_SEPA_ENABLED", "1")
    monkeypatch.setenv("GAIA_FROST_ENABLED", "1")
    monkeypatch.setenv("GAIA_NRW_ENABLED", "1")
    count = register_p7_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert count == len(DMI_MESH) + len(SEPA_MESH)
    assert all(d.startswith(("dmi-", "sepa-")) for d in fleet.ids)

    fleet2 = Fleet()
    monkeypatch.setenv("GAIA_FROST_CLIENT_ID", "client-xyz")
    monkeypatch.setenv("GAIA_NRW_API_KEY", "nrw-key")
    count2 = register_p7_relays(fleet2, SimClock(realtime=True), key_dir=str(tmp_path))
    assert count2 == len(FROST_MESH) + len(DMI_MESH) + len(SEPA_MESH) + len(NRW_MESH)
    assert "frost-blindern-01" in fleet2.ids
    assert "nrw-4016-01" in fleet2.ids


def test_frost_basic_auth_encoding():
    token = base64.b64encode(b"abc:").decode("ascii")
    assert token == "YWJjOg=="
