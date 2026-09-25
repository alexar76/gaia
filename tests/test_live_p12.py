"""P12 relay mapper tests — fixtures only, no live HTTP."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p12 import (
    CHMU_MESH,
    GEOSPHERE_MESH,
    INMET_MESH,
    JMA_AMEDAS_MESH,
    KMA_MESH,
    LHMT_HYDRO_MESH,
    LHMT_WX_MESH,
    LVGMC_HYDRO_MESH,
    LVGMC_WX_MESH,
    OPW_MESH,
    P12_KEYED_COUNT,
    P12_OPEN_COUNT,
    RTE_MESH,
    VIC_MESH,
    ChmuWeather,
    GeosphereWeather,
    GfmFlood,
    InmetWeather,
    JmaAmedasWeather,
    JmaQuake,
    JmaTyphoon,
    KmaAsosWeather,
    LhmtRiver,
    LhmtWeather,
    LvgmcRiver,
    LvgmcWeather,
    OpwRiver,
    RteGrid,
    VicFlood,
    register_p12_relays,
)
from gaia.source_policy import require_approved_source


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_riga() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")


def _now_kst() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")


def test_p12_source_policies_are_commercial():
    expected = {
        "geosphere_at": "CC BY 4.0",
        "lhmt_lt": "CC BY-SA 4.0",
        "lvgmc_lv": "CC0-1.0",
        "vigicrues_fr": "Etalab Licence Ouverte 2.0",
        "rte_eco2mix": "Etalab Licence Ouverte 2.0",
        "opw_ie": "CC BY 4.0",
        "jma_amedas": "Public Data License 1.0",
        "jma_quake": "Public Data License 1.0",
        "jma_typhoon": "Public Data License 1.0",
        "inmet_br": "WMO core unrestricted",
        "chmu_cz": "CC BY 4.0",
        "kma_asos": "Public Nuri Type 1",
        "gfm_observed": "CC BY 4.0",
    }
    for source_id, licence in expected.items():
        policy = require_approved_source(source_id)
        assert policy.licence == licence
        assert "commercial" in policy.commercial_basis.lower()


def test_geosphere_map(tmp_path):
    station, lat, lon = GEOSPHERE_MESH[0][1], GEOSPHERE_MESH[0][2], GEOSPHERE_MESH[0][3]
    dev = GeosphereWeather(
        "at-wx-wien-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "timestamps": [_now_iso()],
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "station": station,
                "parameters": {
                    "TL": {"data": [12.3]},
                    "RF": {"data": [70]},
                    "P": {"data": [1015.2]},
                    "FF": {"data": [3.1]},
                },
            },
        }],
    })
    assert mapped["temperature_c"] == 12.3
    assert mapped["wind_mps"] == 3.1
    assert mapped["humidity_pct"] == 70.0


def test_geosphere_missing_station_offline(tmp_path):
    station, lat, lon = GEOSPHERE_MESH[0][1], GEOSPHERE_MESH[0][2], GEOSPHERE_MESH[0][3]
    dev = GeosphereWeather(
        "at-wx-wien-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map({"features": [{"properties": {"station": "99999", "parameters": {"TL": 1}}}]})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_lhmt_weather_map(tmp_path):
    station, lat, lon = LHMT_WX_MESH[0][1], LHMT_WX_MESH[0][2], LHMT_WX_MESH[0][3]
    dev = LhmtWeather(
        "lt-wx-vilnius-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "station": {"coordinates": {"latitude": lat, "longitude": lon}},
        "observations": [{
            "observationTimeUtc": _now_iso(),
            "airTemperature": 14.2,
            "relativeHumidity": 80,
            "seaLevelPressure": 1012,
            "windSpeed": 2.5,
        }],
    })
    assert mapped["temperature_c"] == 14.2
    assert mapped["wind_mps"] == 2.5


def test_lhmt_hydro_cm_to_metres(tmp_path):
    station, lat, lon = LHMT_HYDRO_MESH[0][1], LHMT_HYDRO_MESH[0][2], LHMT_HYDRO_MESH[0][3]
    dev = LhmtRiver(
        "lt-hydro-kaunas-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "observations": [{
            "observationTimeUtc": _now_iso(),
            "waterLevel": 245,
            "waterTemperature": 12.1,
        }],
    })
    assert mapped["gage_height_m"] == 2.45
    assert mapped["water_temperature_c"] == 12.1


def test_lvgmc_weather_map(tmp_path):
    station, lat, lon = LVGMC_WX_MESH[0][1], LVGMC_WX_MESH[0][2], LVGMC_WX_MESH[0][3]
    dev = LvgmcWeather(
        "lv-wx-riga-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    when = _now_riga()
    mapped = dev.map([
        {"STATION_ID": station, "ABBREVIATION": "TDRY", "DATETIME": when, "VALUE": "16.4"},
        {"STATION_ID": station, "ABBREVIATION": "RLH", "DATETIME": when, "VALUE": "72"},
        {"STATION_ID": station, "ABBREVIATION": "PRSL", "DATETIME": when, "VALUE": "1014"},
        {"STATION_ID": station, "ABBREVIATION": "WNS10", "DATETIME": when, "VALUE": "3.2"},
    ])
    assert mapped["temperature_c"] == 16.4
    assert mapped["wind_mps"] == 3.2


def test_lvgmc_stale_offline(tmp_path):
    station, lat, lon = LVGMC_WX_MESH[0][1], LVGMC_WX_MESH[0][2], LVGMC_WX_MESH[0][3]
    dev = LvgmcWeather(
        "lv-wx-riga-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map([
            {"STATION_ID": station, "ABBREVIATION": "TDRY", "DATETIME": "2020-01-01T00:00:00", "VALUE": "1"},
        ])
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_lvgmc_hydro_map(tmp_path):
    station, lat, lon = LVGMC_HYDRO_MESH[0][1], LVGMC_HYDRO_MESH[0][2], LVGMC_HYDRO_MESH[0][3]
    dev = LvgmcRiver(
        "lv-hydro-riga-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    when = _now_riga()
    mapped = dev.map([
        {"STATION_ID": station, "ABBREVIATION": "LIMEN", "DATETIME": when, "VALUE": "182"},
        {"STATION_ID": station, "ABBREVIATION": "WTEMD", "DATETIME": when, "VALUE": "11.4"},
    ])
    assert mapped["gage_height_m"] == 1.82
    assert mapped["water_temperature_c"] == 11.4


def test_vic_warning_map(tmp_path):
    territory, lat, lon = VIC_MESH[0][1], VIC_MESH[0][2], VIC_MESH[0][3]
    dev = VicFlood(
        "vic-meuse-01", SimClock(realtime=True),
        territory=territory, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "DtHrInfoVigiCru": _now_iso(),
        "features": [{
            "properties": {"cdensup_1": territory, "NivInfViCr": 3},
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        }],
    })
    assert mapped["severity_score"] == 3.0


def test_vic_green_is_not_all_clear(tmp_path):
    territory, lat, lon = VIC_MESH[0][1], VIC_MESH[0][2], VIC_MESH[0][3]
    dev = VicFlood(
        "vic-meuse-01", SimClock(realtime=True),
        territory=territory, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map({
            "DtHrInfoVigiCru": _now_iso(),
            "features": [{
                "properties": {"cdensup_1": territory, "NivInfViCr": 1},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }],
        })
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_rte_grid_map(tmp_path):
    lat, lon = RTE_MESH[0][1], RTE_MESH[0][2]
    dev = RteGrid(
        "rte-grid-01", SimClock(realtime=True),
        latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({"results": [{"date_heure": _now_iso(), "taux_co2": 31}]})
    assert mapped["carbon_intensity_gco2_kwh"] == 31.0
    assert mapped["latitude"] == lat
    assert mapped["longitude"] == lon


def test_opw_prefers_sensor_0001(tmp_path):
    station, lat, lon = OPW_MESH[0][1], OPW_MESH[0][2], OPW_MESH[0][3]
    dev = OpwRiver(
        "ie-river-athlone-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "features": [
            {
                "properties": {"station_ref": station, "value": 9.9, "datetime": _now_iso(), "sensor": "0002"},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            },
            {
                "properties": {"station_ref": station, "value": 1.23, "datetime": _now_iso(), "sensor": "0001"},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            },
        ],
    })
    assert mapped["gage_height_m"] == 1.23


def test_opw_rejects_high_station_ids(tmp_path):
    try:
        OpwRiver(
            "ie-river-bad-01", SimClock(realtime=True),
            station="41001", latitude=53.0, longitude=-8.0, key_dir=str(tmp_path),
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_jma_amedas_map(tmp_path):
    station, lat, lon = JMA_AMEDAS_MESH[0][1], JMA_AMEDAS_MESH[0][2], JMA_AMEDAS_MESH[0][3]
    dev = JmaAmedasWeather(
        "jp-wx-tokyo-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        station: {"temp": [22.1], "humidity": [60], "pressure": [1013], "wind": [2.0]},
    })
    assert mapped["temperature_c"] == 22.1
    assert mapped["wind_mps"] == 2.0


def test_jma_quake_map(tmp_path):
    dev = JmaQuake("jma-quake-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map([
        {"mag": 5.2, "cod": "+35.700+139.700-10000", "en_anm": "Tokyo"},
        {"mag": 3.1, "cod": "+34.000+135.000-5000", "en_anm": "Osaka"},
    ])
    assert mapped["magnitude"] == 5.2
    assert mapped["depth_km"] == 10.0
    assert mapped["latitude"] == 35.7
    assert mapped["longitude"] == 139.7


def test_jma_typhoon_map(tmp_path):
    dev = JmaTyphoon("jma-typhoon-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map([{
        "tropicalCyclone": "TC2630",
        "issue": _now_iso(),
        "specifications": [{
            "part": {"en": "Analysis"},
            "validtime": {"UTC": _now_iso()},
            "position": {"deg": [18.4, 135.2]},
            "maximumWind": {"sustained": {"kt": 45}},
            "pressure": 990,
        }],
    }])
    assert mapped["intensity_kn"] == 45.0
    assert mapped["latitude"] == 18.4
    assert mapped["longitude"] == 135.2


def test_jma_typhoon_empty_offline(tmp_path):
    dev = JmaTyphoon("jma-typhoon-01", SimClock(realtime=True), key_dir=str(tmp_path))
    try:
        dev.map([])
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_inmet_map(tmp_path):
    station, lat, lon = INMET_MESH[0][1], INMET_MESH[0][2], INMET_MESH[0][3]
    dev = InmetWeather(
        "br-wx-saopaulo-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "features": [
            {
                "properties": {
                    "traditional_station_identifier": station,
                    "name": "air_temperature",
                    "value": 24.5,
                    "phenomenonTime": _now_iso(),
                },
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            },
            {
                "properties": {
                    "traditional_station_identifier": station,
                    "name": "wind_speed",
                    "value": 3.1,
                    "phenomenonTime": _now_iso(),
                },
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            },
        ],
    })
    assert mapped["temperature_c"] == 24.5
    assert mapped["wind_mps"] == 3.1


def test_chmu_map(tmp_path):
    station, lat, lon = CHMU_MESH[0][1], CHMU_MESH[0][2], CHMU_MESH[0][3]
    dev = ChmuWeather(
        "cz-wx-prague-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    mapped = dev.map({
        "datumVytvoreni": when,
        "data": {"values": [
            [f"0-20000-0-{station}", "T", when, 15.2],
            [f"0-20000-0-{station}", "H", when, 68],
            [f"0-20000-0-{station}", "P", when, 1016],
            [f"0-20000-0-{station}", "F", when, 2.4],
        ]},
    })
    assert mapped["temperature_c"] == 15.2
    assert mapped["wind_mps"] == 2.4


def test_kma_map(tmp_path):
    station, lat, lon = KMA_MESH[0][1], KMA_MESH[0][2], KMA_MESH[0][3]
    dev = KmaAsosWeather(
        "kr-wx-seoul-01", SimClock(realtime=True),
        station=station, api_key="kma-test-key-12345",
        latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "response": {"body": {"items": {"item": [
            {"tm": _now_kst(), "ta": 21.0, "hm": 55, "pa": 1012.3, "ws": 1.8},
        ]}}},
    })
    assert mapped["temperature_c"] == 21.0
    assert mapped["wind_mps"] == 1.8


def test_gfm_empty_offline(tmp_path):
    dev = GfmFlood(
        "gfm-flood-01", SimClock(realtime=True),
        api_token="gfm-test-token-12345", key_dir=str(tmp_path),
    )
    try:
        dev.map({"features": []})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_gfm_map(tmp_path):
    dev = GfmFlood(
        "gfm-flood-01", SimClock(realtime=True),
        api_token="gfm-test-token-12345", key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "features": [{
            "properties": {"severity": 2},
            "geometry": {"type": "Point", "coordinates": [10.0, 50.0]},
        }],
    })
    assert mapped["severity_score"] == 2.0
    assert mapped["latitude"] == 50.0
    assert mapped["longitude"] == 10.0


class _Fleet:
    def __init__(self):
        self._devices = {}

    def add(self, device):
        self._devices[device.device_id] = device

    def get(self, device_id):
        if device_id not in self._devices:
            raise ValueError(device_id)
        return self._devices[device_id]

    def devices(self):
        return list(self._devices.values())

    @property
    def ids(self):
        return set(self._devices)


def test_register_p12_open_meshes(tmp_path, monkeypatch):
    from gaia.devices.live_p12_jma import P12_JMA_OPEN_COUNT, register_p12_jma

    for name in ("GAIA_KMA_SERVICE_KEY", "GAIA_GFM_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    fleet = _Fleet()
    n = register_p12_jma(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    n += register_p12_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == P12_OPEN_COUNT + P12_JMA_OPEN_COUNT
    assert P12_KEYED_COUNT == 8
    assert "at-wx-wien-01" in fleet.ids
    assert "lt-wx-vilnius-01" in fleet.ids
    assert "lt-hydro-kaunas-01" in fleet.ids
    assert "lv-wx-riga-01" in fleet.ids
    assert "lv-hydro-riga-01" in fleet.ids
    assert "vic-meuse-01" in fleet.ids
    assert "rte-grid-01" in fleet.ids
    assert "ie-river-athlone-01" in fleet.ids
    assert "jp-wx-tokyo-01" in fleet.ids
    assert "jma-quake-01" in fleet.ids
    assert "jma-typhoon-01" in fleet.ids
    assert "br-wx-saopaulo-01" not in fleet.ids
    assert "cz-wx-praha-01" in fleet.ids
    assert "kr-wx-seoul-01" not in fleet.ids
    assert "gfm-flood-01" not in fleet.ids


def test_register_p12_keyed_meshes(tmp_path, monkeypatch):
    from gaia.devices.live_p12_jma import P12_JMA_OPEN_COUNT, register_p12_jma

    monkeypatch.setenv("GAIA_KMA_SERVICE_KEY", "kma-test-key-12345")
    monkeypatch.setenv("GAIA_GFM_TOKEN", "gfm-test-token-12345")
    fleet = _Fleet()
    n = register_p12_jma(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    n += register_p12_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == P12_OPEN_COUNT + P12_JMA_OPEN_COUNT + P12_KEYED_COUNT
    assert "kr-wx-seoul-01" in fleet.ids
    assert "gfm-flood-01" in fleet.ids
    assert fleet._devices["kr-wx-seoul-01"].__class__.__name__ == "KmaAsosWeather"
    assert fleet._devices["gfm-flood-01"].__class__.__name__ == "GfmFlood"


def test_p12_mesh_ids_are_unique():
    from gaia.devices.live_p12_lv import LVGMC_HYDRO_MESH as LV_HYDRO
    from gaia.devices.live_p12_lv import LVGMC_WX_MESH as LV_WX

    ids = [
        row[0] for row in (
            *GEOSPHERE_MESH, *LHMT_WX_MESH, *LHMT_HYDRO_MESH,
            *LV_WX, *LV_HYDRO, *VIC_MESH,
            *OPW_MESH, *JMA_AMEDAS_MESH, *CHMU_MESH, *KMA_MESH,
        )
    ]
    ids.extend(["rte-grid-01", "jma-quake-01", "jma-typhoon-01", "gfm-flood-01"])
    assert len(ids) == len(set(ids))


def test_p12_atlas_catalog_parity():
    import sys
    from pathlib import Path

    atlas_root = Path(__file__).resolve().parents[2] / "atlas"
    if not (atlas_root / "atlas" / "stations.py").is_file():
        import pytest
        pytest.skip("atlas catalog not in this tree")
    if str(atlas_root) not in sys.path:
        sys.path.insert(0, str(atlas_root))
    from atlas.stations import STATION_CATALOG

    from gaia.devices.live_p12_cz import CHMU_MESH as CZ_MESH
    from gaia.devices.live_p12_lv import LVGMC_HYDRO_MESH as LV_HYDRO
    from gaia.devices.live_p12_lv import LVGMC_WX_MESH as LV_WX

    meshes = (
        GEOSPHERE_MESH, LHMT_WX_MESH, LHMT_HYDRO_MESH, LV_WX,
        LV_HYDRO, VIC_MESH, OPW_MESH, JMA_AMEDAS_MESH,
        CZ_MESH, KMA_MESH,
    )
    for mesh in meshes:
        for row in mesh:
            device_id = row[0]
            meta = STATION_CATALOG[device_id]
            assert meta["kind"] == "point"
            assert abs(float(meta["lat"]) - float(row[2])) < 1e-4
            assert abs(float(meta["lon"]) - float(row[3])) < 1e-4
    rte = STATION_CATALOG["rte-grid-01"]
    assert rte["kind"] == "point"
    assert abs(float(rte["lat"]) - RTE_MESH[0][1]) < 1e-4
    assert abs(float(rte["lon"]) - RTE_MESH[0][2]) < 1e-4


def test_p12_hub_spec_lists_prefixes(tmp_path, monkeypatch):
    from gaia.capabilities import build_spec

    monkeypatch.delenv("GAIA_KMA_SERVICE_KEY", raising=False)
    monkeypatch.delenv("GAIA_GFM_TOKEN", raising=False)
    from gaia.devices.live_p12_jma import register_p12_jma

    fleet = _Fleet()
    register_p12_jma(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    register_p12_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))

    class _Runtime:
        def __init__(self):
            self.fleet = fleet

    spec = build_spec(_Runtime(), public_url="http://gaia.test")
    by_id = {cap.capability_id: cap.description for cap in spec.capabilities}
    assert "at-wx-" in by_id["gaia.weather.read@v1"]
    assert "jp-wx-" in by_id["gaia.weather.read@v1"]
    assert "br-wx-" not in by_id["gaia.weather.read@v1"]
    assert "cz-wx-" in by_id["gaia.weather.read@v1"]
    assert "lt-hydro-" in by_id["gaia.river.read@v1"]
    assert "ie-river-" in by_id["gaia.river.read@v1"]
    assert "vic-" in by_id["gaia.flood.read@v1"]
    assert "rte-grid-01" in by_id["gaia.grid.read@v1"]
    assert "jma-quake-01" in by_id["gaia.quake.read@v1"]
    assert "jma-typhoon-01" in by_id["gaia.cyclone.read@v1"]
