"""P8 relay mapper tests — fixtures only, no live HTTP."""

from __future__ import annotations

import io
import zipfile

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p8 import (
    CDIP_MESH,
    ICOS_MESH,
    CdipWave,
    IcosCpAuth,
    IcosGhg,
    IngvQuake,
    register_p8_relays,
)
from gaia.source_policy import require_approved_source


def test_p8_source_policies_are_commercial():
    expected = {
        "ingv_fdsn": "CC BY 4.0",
        "cdip_erddap": "Unrestricted (USACE-sponsored) + attribution",
        "icos_atc": "CC BY 4.0",
    }
    for source_id, licence in expected.items():
        policy = require_approved_source(source_id)
        assert policy.licence == licence
        assert "commercial" in policy.commercial_basis.lower() or "redistrib" in policy.commercial_basis.lower()
    assert "cpauth.icos-cp.eu" in require_approved_source("icos_atc").hosts


def test_ingv_map_and_hotspots(tmp_path):
    dev = IngvQuake("ingv-01", SimClock(realtime=True), key_dir=str(tmp_path))
    payload = {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [13.4, 42.3, 10.0]},
                "properties": {
                    "mag": 4.1,
                    "place": "Central Italy",
                    "depth": 10.0,
                },
            },
            {
                "geometry": {"type": "Point", "coordinates": [15.0, 40.0, 5.0]},
                "properties": {"mag": 3.2, "flynn_region": "Southern Italy"},
            },
        ]
    }
    mapped = dev.map(payload)
    assert mapped["magnitude"] == 4.1
    assert mapped["latitude"] == 42.3
    assert mapped["longitude"] == 13.4
    assert mapped["depth_km"] == 10.0
    hotspots = dev.collect_hotspots(payload)
    assert len(hotspots) == 2
    assert hotspots[0]["magnitude"] >= hotspots[1]["magnitude"]
    with pytest.raises(DeviceOffline):
        dev.map({"features": []})


def test_cdip_table_map(tmp_path):
    dev = CdipWave(
        "cdip-santamonica-01",
        SimClock(realtime=True),
        station_id="028",
        latitude=33.86,
        longitude=-118.64,
        key_dir=str(tmp_path),
    )
    assert "Coastal Data Information Program" in dev.source
    assert "cdip.ucsd.edu" in dev.source
    assert "USACE" in dev.source or "Army Corps" in dev.source
    payload = {
        "table": {
            "columnNames": [
                "time",
                "waveHs",
                "waveTp",
                "station_id",
                "metaStationName",
                "latitude",
                "longitude",
            ],
            "rows": [
                [
                    "2026-09-08T12:00:00Z",
                    1.85,
                    11.2,
                    "028",
                    "Santa Monica Bay",
                    33.8599,
                    -118.6411,
                ]
            ],
        }
    }
    mapped = dev.map(payload)
    assert mapped["wave_height_m"] == 1.85
    assert mapped["wave_period_s"] == 11.2
    assert mapped["latitude"] == 33.8599
    with pytest.raises(DeviceOffline):
        dev.map({"table": {"columnNames": ["waveHs"], "rows": []}})


def test_icos_zip_parse_and_map(tmp_path):
    csv_body = (
        "# ICOS ATC NRT\n"
        "TIMESTAMP,co2,Flag\n"
        "2026-09-07T00:00:00Z,420.1,O\n"
        "2026-09-08T00:00:00Z,421.5,O\n"
        "2026-09-08T06:00:00Z,419.0,N\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("HTM_co2_nrt.csv", csv_body)
    co2 = IcosGhg._latest_co2_from_zip(buf.getvalue())
    assert co2 == 421.5

    # Live ATC objects use semicolon tables inside ``*.CO2``.
    atc_body = (
        "# HEADER LINES: 2\n"
        "# STATION CODE: HTM\n"
        "HTM;70;2026;09;07;10;00;2026.68;419.436;0.155;10;U;1238;;;0\n"
        "HTM;70;2026;09;07;12;00;2026.68;-999.990;-9.990;0;N;1238;;;0\n"
        "HTM;70;2026;09;07;14;00;2026.68;420.100;0.190;16;U;1238;;;0\n"
    )
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zf:
        zf.writestr("ICOS_ATC_NRT_HTM.CO2", atc_body)
    assert IcosGhg._latest_co2_from_zip(buf2.getvalue()) == 420.1

    dev = IcosGhg(
        "icos-htm-01",
        SimClock(realtime=True),
        station_code="HTM",
        cpauth_token="test-token-abcdef",
        latitude=56.1,
        longitude=13.4,
        key_dir=str(tmp_path),
    )
    mapped = dev.map({"co2_ppm": 421.5})
    assert mapped["co2_ppm"] == 421.5
    assert mapped["latitude"] == 56.1
    assert "CC BY 4.0" in dev.source


def test_icos_auth_password_login(monkeypatch):
    class FakeResp:
        status_code = 200
        cookies = {"cpauthToken": "WzFakeTokenValueForUnitTest=="}
        headers = type("H", (), {"multi_items": staticmethod(lambda: [])})()

    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResp()

    monkeypatch.setattr("gaia.devices.live_p8.httpx.post", fake_post)
    auth = IcosCpAuth(email="ops@example.com", password="long-enough-secret")
    assert auth.token() == "WzFakeTokenValueForUnitTest=="
    assert calls and "password/login" in calls[0]["url"]
    assert calls[0]["data"]["mail"] == "ops@example.com"


def test_register_p8_respects_env(tmp_path, monkeypatch):
    class Fleet:
        def __init__(self):
            self.ids: list[str] = []

        def add(self, device):
            self.ids.append(device.device_id)

    fleet = Fleet()
    monkeypatch.delenv("GAIA_ICOS_CPAUTH_TOKEN", raising=False)
    monkeypatch.delenv("GAIA_ICOS_EMAIL", raising=False)
    monkeypatch.delenv("GAIA_ICOS_PASSWORD", raising=False)
    monkeypatch.setenv("GAIA_INGV_ENABLED", "1")
    monkeypatch.setenv("GAIA_CDIP_ENABLED", "1")
    monkeypatch.setenv("GAIA_ICOS_ENABLED", "1")
    count = register_p8_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert count == 1 + len(CDIP_MESH)
    assert "ingv-01" in fleet.ids
    assert all(any(d.startswith(p) for p in ("ingv-", "cdip-")) for d in fleet.ids)

    fleet2 = Fleet()
    monkeypatch.setenv("GAIA_ICOS_EMAIL", "ops@example.com")
    monkeypatch.setenv("GAIA_ICOS_PASSWORD", "long-enough-secret")
    count2 = register_p8_relays(fleet2, SimClock(realtime=True), key_dir=str(tmp_path))
    assert count2 == 1 + len(CDIP_MESH) + len(ICOS_MESH)
    assert "icos-htm-01" in fleet2.ids
    assert "icos-zsf-01" in fleet2.ids
