"""P12 JMA fixture tests — no live HTTP in pytest."""

from __future__ import annotations

from datetime import datetime, timezone

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p12_jma import (
    ATLAS_ROWS,
    HOSTS,
    JMA_AMEDAS_MESH,
    P12_JMA_KEYED_COUNT,
    P12_JMA_OPEN_COUNT,
    SOURCE_POLICIES,
    JmaAmedasWeather,
    JmaQuake,
    JmaTyphoon,
    register_p12_jma,
)


class _Fleet:
    def __init__(self):
        self._devices = {}

    def add(self, device):
        self._devices[device.device_id] = device

    @property
    def ids(self):
        return set(self._devices)


def test_jma_hosts_and_commercial_policies():
    assert HOSTS == frozenset({"www.jma.go.jp"})
    for source_id, policy in SOURCE_POLICIES.items():
        assert policy["source_id"] == source_id
        assert policy["licence"] == "Public Data License 1.0"
        assert "commercial" in policy["commercial_basis"].lower()
        assert "www.jma.go.jp" in policy["hosts"]


def test_amedas_mesh_is_13_wgs84_cities():
    assert len(JMA_AMEDAS_MESH) == 13
    ids = [row[0] for row in JMA_AMEDAS_MESH]
    assert len(ids) == len(set(ids))
    codes = {row[1] for row in JMA_AMEDAS_MESH}
    assert codes == {
        "44132", "62078", "14163", "82182", "51106", "61286", "67437",
        "34392", "91197", "54232", "88317", "56227", "72086",
    }
    tokyo = JMA_AMEDAS_MESH[0]
    assert tokyo[0] == "jp-wx-tokyo-01"
    assert 35.65 < tokyo[2] < 35.75
    assert 139.70 < tokyo[3] < 139.80


def test_amedas_map(tmp_path):
    station, lat, lon = JMA_AMEDAS_MESH[0][1], JMA_AMEDAS_MESH[0][2], JMA_AMEDAS_MESH[0][3]
    dev = JmaAmedasWeather(
        "jp-wx-tokyo-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        station: {"temp": [21.5, 0], "humidity": [100, 0], "pressure": [992.7, 0], "wind": [4.1, 0]},
        "99999": {"temp": [0.0, 0], "wind": [0.0, 0]},
    })
    assert mapped["temperature_c"] == 21.5
    assert mapped["wind_mps"] == 4.1
    assert mapped["latitude"] == lat
    assert mapped["longitude"] == lon


def test_amedas_missing_offline(tmp_path):
    station, lat, lon = JMA_AMEDAS_MESH[0][1], JMA_AMEDAS_MESH[0][2], JMA_AMEDAS_MESH[0][3]
    dev = JmaAmedasWeather(
        "jp-wx-tokyo-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map({"00000": {"temp": [1.0, 0], "wind": [1.0, 0]}})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_amedas_rejects_bad_station():
    try:
        JmaAmedasWeather(
            "jp-wx-bad", SimClock(realtime=True),
            station="../x", latitude=35.0, longitude=139.0, key_dir="/tmp",
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_jma_quake_cod(tmp_path):
    dev = JmaQuake("jma-quake-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map([
        {"mag": "3.5", "cod": "+36.6+141.0-40000/", "en_anm": "Off Ibaraki"},
        {"mag": "5.1", "cod": "+35.7+140.1-10000/", "en_anm": "Chiba"},
    ])
    assert mapped["magnitude"] == 5.1
    assert mapped["latitude"] == 35.7
    assert mapped["longitude"] == 140.1
    assert mapped["depth_km"] == 10.0


def test_jma_quake_empty_offline(tmp_path):
    dev = JmaQuake("jma-quake-01", SimClock(realtime=True), key_dir=str(tmp_path))
    try:
        dev.map([])
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_jma_typhoon_empty_offline(tmp_path):
    dev = JmaTyphoon("jma-typhoon-01", SimClock(realtime=True), key_dir=str(tmp_path))
    try:
        dev.map([])
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_jma_typhoon_analysis(tmp_path):
    dev = JmaTyphoon("jma-typhoon-01", SimClock(realtime=True), key_dir=str(tmp_path))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mapped = dev.map([{
        "tropicalCyclone": "TC2630",
        "issue": now,
        "specifications": [{
            "part": {"en": "Analysis"},
            "position": {"deg": [35.8, 142.8]},
            "maximumWind": {"sustained": {"kt": "75"}},
            "pressure": "960",
            "validtime": {"UTC": now},
        }],
    }])
    assert mapped["intensity_kn"] == 75.0
    assert mapped["pressure_hpa"] == 960.0
    assert mapped["latitude"] == 35.8
    assert mapped["longitude"] == 142.8


def test_register_p12_jma(tmp_path):
    fleet = _Fleet()
    n = register_p12_jma(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == P12_JMA_OPEN_COUNT == 15
    assert P12_JMA_KEYED_COUNT == 0
    assert "jp-wx-tokyo-01" in fleet.ids
    assert "jp-wx-takamatsu-01" in fleet.ids
    assert "jma-quake-01" in fleet.ids
    assert "jma-typhoon-01" in fleet.ids
    assert fleet._devices["jp-wx-tokyo-01"].__class__.__name__ == "JmaAmedasWeather"


def test_atlas_rows_point_pins():
    assert len(ATLAS_ROWS) == 15
    weather = [row for row in ATLAS_ROWS if row["capability_id"] == "gaia.weather.read@v1"]
    assert len(weather) == 13
    for row, mesh in zip(weather, JMA_AMEDAS_MESH):
        assert row["device_id"] == mesh[0]
        assert row["kind"] == "point"
        assert row["layer"] == "weather"
        assert abs(float(row["lat"]) - mesh[2]) < 1e-4
        assert abs(float(row["lon"]) - mesh[3]) < 1e-4
    by_id = {row["device_id"]: row for row in ATLAS_ROWS}
    assert by_id["jma-quake-01"]["capability_id"] == "gaia.quake.read@v1"
    assert by_id["jma-typhoon-01"]["capability_id"] == "gaia.cyclone.read@v1"
