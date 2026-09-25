"""P12 CHMU Czech relay tests — fixtures for map(); optional live probe."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p12_cz import (
    CAPABILITY,
    CHMU_MESH,
    HOSTS,
    P12_CZ_PIN_COUNT,
    SOURCE_POLICY_ID,
    ChmuWeather,
    atlas_rows,
    register_p12_cz,
)
from gaia.source_policy import require_approved_source


def test_chmu_source_policy_commercial():
    policy = require_approved_source(SOURCE_POLICY_ID)
    assert policy.licence == "CC BY 4.0"
    assert "commercial" in policy.commercial_basis.lower()
    assert HOSTS == frozenset({"opendata.chmi.cz"})


def test_chmu_mesh_eight_cities():
    assert len(CHMU_MESH) == 8
    assert P12_CZ_PIN_COUNT == 8
    ids = [row[0] for row in CHMU_MESH]
    assert len(ids) == len(set(ids))
    assert all(device_id.startswith("cz-wx-") for device_id in ids)
    wsis = {row[1] for row in CHMU_MESH}
    assert "0-203-0-11742" in wsis
    assert "0-203-0-11649" in wsis


def test_chmu_map(tmp_path):
    device_id, wsi, lat, lon = CHMU_MESH[0][0], CHMU_MESH[0][1], CHMU_MESH[0][2], CHMU_MESH[0][3]
    dev = ChmuWeather(
        device_id, SimClock(realtime=True),
        wsi=wsi, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mapped = dev.map({
        "datumVytvoreni": when,
        "data": {"values": [
            [wsi, "T", when, 15.2],
            [wsi, "H", when, 68],
            [wsi, "P", when, 1016],
            [wsi, "F", when, 2.4],
        ]},
    })
    assert mapped["temperature_c"] == 15.2
    assert mapped["wind_mps"] == 2.4
    assert mapped["latitude"] == lat


def test_chmu_203_wsi_map(tmp_path):
    device_id, wsi, lat, lon = CHMU_MESH[5][0], CHMU_MESH[5][1], CHMU_MESH[5][2], CHMU_MESH[5][3]
    dev = ChmuWeather(
        device_id, SimClock(realtime=True),
        wsi=wsi, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mapped = dev.map({
        "datumVytvoreni": when,
        "data": {"data": {"values": [
            [wsi, "T", when, 11.0],
            [wsi, "F", when, 1.1],
        ]}},
    })
    assert mapped["temperature_c"] == 11.0
    assert mapped["wind_mps"] == 1.1


def test_chmu_stale_offline(tmp_path):
    device_id, wsi, lat, lon = CHMU_MESH[0][0], CHMU_MESH[0][1], CHMU_MESH[0][2], CHMU_MESH[0][3]
    dev = ChmuWeather(
        device_id, SimClock(realtime=True),
        wsi=wsi, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    stale = (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(DeviceOffline):
        dev.map({
            "datumVytvoreni": stale,
            "data": {"values": [[wsi, "T", stale, 10.0]]},
        })


def test_atlas_rows_parity():
    rows = atlas_rows()
    assert len(rows) == P12_CZ_PIN_COUNT
    for (device_id, lat, lon, place, layer, cap, note), mesh in zip(rows, CHMU_MESH):
        assert device_id == mesh[0]
        assert lat == mesh[2]
        assert lon == mesh[3]
        assert place == mesh[4]
        assert layer == "weather"
        assert cap == CAPABILITY
        assert note


class _Fleet:
    def __init__(self) -> None:
        self.devices: list = []

    def add(self, device: object) -> None:
        self.devices.append(device)


def test_register_p12_cz(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_CHMU_ENABLED", "1")
    fleet = _Fleet()
    n = register_p12_cz(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == P12_CZ_PIN_COUNT
    assert len(fleet.devices) == P12_CZ_PIN_COUNT


@pytest.mark.live
def test_live_probe_chmu_praha(tmp_path):
    device_id, wsi, lat, lon = CHMU_MESH[0][0], CHMU_MESH[0][1], CHMU_MESH[0][2], CHMU_MESH[0][3]
    dev = ChmuWeather(
        device_id, SimClock(realtime=True),
        wsi=wsi, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        sample = dev.sample()
    except DeviceOffline as exc:
        pytest.skip(f"live probe offline: {exc}")
    assert sample.get("temperature_c") is not None or sample.get("wind_mps") is not None
