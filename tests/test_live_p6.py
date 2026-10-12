"""P6 relay mapper tests — fixtures only, no live HTTP."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p6 import (
    EA_HYDROLOGY_MESH,
    RWS_RIVER_MESH,
    USACE_RESERVOIR_MESH,
    EaHydrologyRiver,
    EpaUvIndex,
    MetEireannObs,
    RwsRiver,
    UsaceReservoir,
    register_p6_relays,
)
from gaia.source_policy import require_approved_source


def test_p6_source_policies_are_commercial():
    expected = {
        "ea_hydrology": "Open Government Licence v3.0",
        "rws_water": "CC0",
        "usace_cwms": "U.S. Government public domain",
        "epa_uv": "U.S. Government public domain",
        "met_eireann": "CC BY 4.0",
    }
    for source_id, licence in expected.items():
        policy = require_approved_source(source_id)
        assert policy.licence == licence
        assert "commercial" in policy.commercial_basis.lower()


def test_ea_hydrology_map_and_empty(tmp_path):
    guid = EA_HYDROLOGY_MESH[0][1]
    dev = EaHydrologyRiver(
        "ea-kingston-01", SimClock(realtime=True), guid=guid,
        latitude=51.415482, longitude=-0.307629, key_dir=str(tmp_path),
    )
    station = {"items": [{"lat": 51.415482, "long": -0.307629}]}
    readings = {
        f"{guid}-flow-i-900-m3s-qualified": {
            "items": [{"dateTime": "2026-09-07T12:00:00Z", "value": 42.5}]
        },
        f"{guid}-level-i-900-m-qualified": {
            "items": [{"dateTime": "2026-09-07T12:15:00Z", "value": 1.23}]
        },
    }
    mapped = dev.map((station, readings))
    assert mapped["discharge_m3s"] == 42.5
    assert mapped["gage_height_m"] == 1.23
    assert mapped["latitude"] == 51.415482
    with pytest.raises(DeviceOffline):
        dev.map((station, {}))


def test_ea_measure_filter(tmp_path):
    guid = EA_HYDROLOGY_MESH[0][1]
    dev = EaHydrologyRiver(
        "ea-kingston-01", SimClock(realtime=True), guid=guid,
        latitude=51.4, longitude=-0.3, key_dir=str(tmp_path),
    )
    station = {"items": [{"measures": [
        {"notation": f"{guid}-flow-i-900-m3s-qualified"},
        {"notation": f"{guid}-flow-m-86400-m3s-qualified"},
        {"@id": f"http://example/{guid}-level-i-900-m-qualified"},
    ]}]}
    assert dev._measure_ids(station) == [
        f"{guid}-flow-i-900-m3s-qualified",
        f"{guid}-level-i-900-m-qualified",
    ]


def test_rws_newest_and_cm_conversion(tmp_path):
    dev = RwsRiver(
        "rws-lobith-01", SimClock(realtime=True),
        location_code="lobith.bovenrijn.tolkamer",
        latitude=51.8495, longitude=6.1024, key_dir=str(tmp_path),
    )
    now = datetime.now(timezone.utc)
    payload = {"WaarnemingenLijst": [
        {
            "AquoMetadata": {"Grootheid": {"Code": "WATHTE"}},
            "MetingenLijst": [
                {"Tijdstip": (now - timedelta(hours=2)).isoformat(), "Meetwaarde": {"Waarde_Numeriek": 111}},
                {"Tijdstip": (now - timedelta(hours=1)).isoformat(), "Meetwaarde": {"Waarde_Numeriek": 245}},
            ],
        },
        {
            "AquoMetadata": {"Grootheid": {"Code": "Q"}},
            "MetingenLijst": [
                {"Tijdstip": (now - timedelta(minutes=30)).isoformat(), "Meetwaarde": {"Waarde_Numeriek": 1234}},
            ],
        },
    ]}
    mapped = dev.map(payload)
    assert mapped["gage_height_m"] == pytest.approx(2.45)
    assert mapped["discharge_m3s"] == 1234

    stale = datetime.now(timezone.utc) - timedelta(days=4)
    with pytest.raises(DeviceOffline):
        dev.map({"WaarnemingenLijst": [{
            "AquoMetadata": {"Grootheid": {"Code": "Q"}},
            "MetingenLijst": [{"Tijdstip": stale.isoformat(), "Meetwaarde": {"Waarde_Numeriek": 1}}],
        }]})


def test_usace_last_non_null_and_units(tmp_path):
    dev = UsaceReservoir(
        "usace-keys-01", SimClock(realtime=True), name="KEYS",
        latitude=36.1517, longitude=-96.2517, key_dir=str(tmp_path),
    )
    mapped = dev.map((
        {"values": [[1, 700.0, 0], [2, None, 0]]},
        {"values": [[1, 1000.0, 0], [2, 1200.0, 0]]},
    ))
    assert mapped["pool_elev_m"] == pytest.approx(213.36)
    assert mapped["storage_m3"] == pytest.approx(1200 * 1233.4818375475)
    with pytest.raises(DeviceOffline):
        dev.map(({"values": []}, {"values": [[1, None, 0]]}))


def test_epa_uv_hotspots(tmp_path):
    dev = EpaUvIndex("epa-uv-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hotspots = dev.collect_hotspots({
        "10001": [{"UV_VALUE": "3", "UV_HOUR": "10"}, {"UV_VALUE": "7", "UV_HOUR": "13"}],
        "90012": [{"UV_VALUE": 9, "UV_HOUR": "12"}],
    })
    assert hotspots[0]["city"] == "Los Angeles"
    assert hotspots[0]["uv_index"] == 9
    assert hotspots[1]["hour_max"] == "13"
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots({})


MET_CSV = """Station,"Temperature (ºC)",Weather,"Wind Speed (Kts)","Wind Gust (Kts)","Wind Direction","Humidity (%)","Rainfall (mm)","Pressure (hPa)"
Dublin,18,Cloudy,10,20,SW,66,0.1,1013
Athenry,15,Rain,09,23,W,85,0.4,1011
"""


def test_met_eireann_csv_map(tmp_path):
    dev = MetEireannObs("met-ie-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hotspots = dev.collect_hotspots(MET_CSV)
    assert hotspots[0]["station"] == "Dublin"
    assert hotspots[0]["temperature_c"] == 18
    assert hotspots[0]["wind_mps"] == pytest.approx(5.14444)
    assert hotspots[0]["rainfall_mm"] == 0.1
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots("Station,Temperature\nUnknown,12\n")


def test_p6_mesh_registration_with_cluster_disables(tmp_path, monkeypatch):
    class _Fleet:
        def __init__(self):
            self.devices = []

        def add(self, device):
            self.devices.append(device)

    monkeypatch.setenv("GAIA_EA_HYDROLOGY_ENABLED", "1")
    monkeypatch.setenv("GAIA_RWS_WATER_ENABLED", "1")
    monkeypatch.setenv("GAIA_USACE_CWMS_ENABLED", "1")
    monkeypatch.setenv("GAIA_EPA_UV_ENABLED", "0")
    monkeypatch.setenv("GAIA_MET_EIREANN_ENABLED", "0")
    fleet = _Fleet()
    count = register_p6_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    expected = len(EA_HYDROLOGY_MESH) + len(RWS_RIVER_MESH) + len(USACE_RESERVOIR_MESH)
    assert count == expected
    assert {d.device_id for d in fleet.devices} == {
        *(row[0] for row in EA_HYDROLOGY_MESH),
        *(row[0] for row in RWS_RIVER_MESH),
        *(row[0] for row in USACE_RESERVOIR_MESH),
    }
