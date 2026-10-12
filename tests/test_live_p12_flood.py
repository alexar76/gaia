"""P12 Vigicrues flood WARNING tests — fixtures only."""

from __future__ import annotations

from datetime import datetime, timezone

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p12_flood import (
    P12_FLOOD_COUNT,
    VIGICRUES_MESH,
    VigicruesFlood,
    atlas_rows,
    register_p12_flood,
)
from gaia.source_policy import require_approved_source


def test_vigicrues_policy_commercial():
    policy = require_approved_source("vigicrues_fr")
    assert policy.licence == "Etalab Licence Ouverte 2.0"
    assert "commercial" in policy.commercial_basis.lower()


def test_vigicrues_green_offline(tmp_path):
    dev = VigicruesFlood(
        "vic-corse-01", SimClock(realtime=True),
        territory="26", latitude=41.93, longitude=8.74, key_dir=str(tmp_path),
    )
    try:
        dev.map({
            "DtHrInfoVigiCru": datetime.now(timezone.utc).isoformat(),
            "features": [{
                "properties": {"cdensup_1": "26", "NivInfViCr": 1},
                "geometry": {"type": "Point", "coordinates": [8.74, 41.93]},
            }],
        })
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_vigicrues_empty_offline(tmp_path):
    dev = VigicruesFlood(
        "vic-meuse-01", SimClock(realtime=True),
        territory="2", latitude=49.12, longitude=6.18, key_dir=str(tmp_path),
    )
    try:
        dev.map({
            "DtHrInfoVigiCru": datetime.now(timezone.utc).isoformat(),
            "features": [],
        })
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_vigicrues_warning_centroid(tmp_path):
    dev = VigicruesFlood(
        "vic-corse-01", SimClock(realtime=True),
        territory="26", latitude=41.93, longitude=8.74, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "DtHrInfoVigiCru": datetime.now(timezone.utc).isoformat(),
        "features": [{
            "properties": {"cdensup_1": "26", "NivInfViCr": 3},
            "geometry": {"type": "MultiLineString", "coordinates": [[[8.7, 41.9], [8.8, 42.0]]]},
        }],
    })
    assert mapped["severity_score"] == 3.0
    assert 41.8 < mapped["latitude"] < 42.1
    assert 8.6 < mapped["longitude"] < 8.9


def test_vigicrues_sanitize_device_id(tmp_path):
    try:
        VigicruesFlood(
            "not-a-vic-pin", SimClock(realtime=True),
            territory="26", latitude=41.93, longitude=8.74, key_dir=str(tmp_path),
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


class _Fleet:
    def __init__(self):
        self._devices = {}

    def add(self, device):
        self._devices[device.device_id] = device


def test_register_p12_flood(tmp_path):
    fleet = _Fleet()
    n = register_p12_flood(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == P12_FLOOD_COUNT
    assert len(fleet._devices) == 19


def test_mesh_cap_and_atlas_rows():
    assert len(VIGICRUES_MESH) == 19
    assert P12_FLOOD_COUNT == 19
    rows = dict(atlas_rows())
    assert len(rows) == 19
    assert rows["vic-meuse-01"]["capability"] == "gaia.flood.read@v1"
    ids = [row[0] for row in VIGICRUES_MESH]
    assert len(ids) == len(set(ids))
