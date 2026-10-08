"""P12 Latvia LVGMC relay tests — fixtures for map(); optional live probe."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p12_lv import (
    LVGMC_HYDRO_MESH,
    LVGMC_WX_MESH,
    P12_LV_COUNT,
    LvgmcRiver,
    LvgmcWeather,
    _HYDRO_PINS,
    _WX_PINS,
    _csv_rows,
    _mesh_from_stacijas,
    _stacija_coords,
    atlas_rows,
    register_p12_lv,
)
from gaia.source_policy import require_approved_source

_METEO_STACIJAS_FIXTURE = """\
STATION_ID,NAME,WMO_ID,BEGIN_DATE,END_DATE,LATITUDE,LONGITUDE,GAUSS1,GAUSS2,GEOGR1,GEOGR2,ELEVATION,ELEVATION_PRESSURE
"RIGASLU","Rīga Universitāte"," ","1945.01.01 00:00:00","3999.12.31 23:59:00","565722","0240628","476940.94","294474.38","24.104686","56.954797","3.18"," "
"RIDM99MS","Daugavpils"," ","1945.01.01 00:00:00","3999.12.31 23:59:00","553720","0263703","729456.56","273536.72","26.6175","55.87","120"," "
"RILP99PA","Liepāja"," ","1945.01.01 00:00:00","3999.12.31 23:59:00","562851","0210124","316868.86","264058.63","21.020641","56.475447","3"," "
"""

_HYDRO_STACIJAS_FIXTURE = """\
STATION_ID,NAME,BEGIN_DATE,END_DATE,LATITUDE,LONGITUDE,GAUSS1,GAUSS2,GEOGR1,GEOGR2,ELEVATION
"HD073810","Rīga","2011.09.08 00:00:00","3999.12.31 23:59:00","570118","0240802","551605.75","336076.09","24.133889","57.0325","0.07"
"HD073141","Daugavpils","2011.09.08 00:00:00","3999.12.31 23:59:00","553108","0263114","729456.56","273536.72","26.5211","55.8614","98.5"
"HD073151","Jēkabpils","2011.09.08 00:00:00","3999.12.31 23:59:00","562951","0255349","551605.75","336076.09","25.8914","56.4978","82.5"
"HD073401","Ogre","2011.09.08 00:00:00","3999.12.31 23:59:00","564845","0243846","551605.75","336076.09","24.6417","56.8128","25.5"
"HD073904","Pļaviņas","2011.09.08 00:00:00","3999.12.31 23:59:00","563659","0254357","551605.75","336076.09","25.7297","56.6164","85.5"
"HD073801","Jelgava","2011.09.08 00:00:00","3999.12.31 23:59:00","563318","0234406","551605.75","336076.09","23.735","56.655","3.5"
"""


def test_lvgmc_source_policy_commercial():
    policy = require_approved_source("lvgmc_lv")
    assert policy.licence == "CC0-1.0"
    assert "commercial" in policy.commercial_basis.lower()


def test_stacija_join_fixture():
    meteo = _csv_rows(_METEO_STACIJAS_FIXTURE)
    lat, lon, name = _stacija_coords(meteo, "RIGASLU")
    assert abs(lat - 56.954797) < 1e-5
    assert abs(lon - 24.104686) < 1e-5
    assert "Rīga" in name


def test_mesh_from_stacijas_matches_catalog():
    meteo = _csv_rows(_METEO_STACIJAS_FIXTURE)
    hydro = _csv_rows(_HYDRO_STACIJAS_FIXTURE)
    wx = _mesh_from_stacijas(_WX_PINS[:3], meteo)
    assert wx[0][0] == "lv-wx-riga-01"
    assert abs(wx[0][2] - LVGMC_WX_MESH[0][2]) < 1e-4
    hydro_mesh = _mesh_from_stacijas(_HYDRO_PINS, hydro)
    for built, catalog in zip(hydro_mesh, LVGMC_HYDRO_MESH):
        assert built[0] == catalog[0]
        assert built[1] == catalog[1]
        assert abs(built[2] - catalog[2]) < 1e-4
        assert abs(built[3] - catalog[3]) < 1e-4


def test_lvgmc_stale_offline(tmp_path):
    dev = LvgmcWeather(
        "lv-wx-riga-01", SimClock(realtime=True),
        station=LVGMC_WX_MESH[0][1], latitude=LVGMC_WX_MESH[0][2],
        longitude=LVGMC_WX_MESH[0][3], key_dir=str(tmp_path),
    )
    with pytest.raises(DeviceOffline):
        dev.map([
            {"STATION_ID": "RIGASLU", "ABBREVIATION": "TDRY",
             "DATETIME": "2020.01.01 00:00:00", "VALUE": "10"},
        ])


def test_lvgmc_weather_map(tmp_path):
    now = datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M:%S")
    dev = LvgmcWeather(
        "lv-wx-riga-01", SimClock(realtime=True),
        station="RIGASLU", latitude=56.95, longitude=24.10, key_dir=str(tmp_path),
    )
    mapped = dev.map([
        {"STATION_ID": "RIGASLU", "ABBREVIATION": "TDRY", "DATETIME": now, "VALUE": "11.2"},
        {"STATION_ID": "RIGASLU", "ABBREVIATION": "RLH", "DATETIME": now, "VALUE": "80"},
        {"STATION_ID": "RIGASLU", "ABBREVIATION": "PRSL", "DATETIME": now, "VALUE": "1013"},
        {"STATION_ID": "RIGASLU", "ABBREVIATION": "WNS10", "DATETIME": now, "VALUE": "3.4"},
    ])
    assert mapped["temperature_c"] == 11.2
    assert mapped["wind_mps"] == 3.4


def test_lvgmc_hydro_map(tmp_path):
    now = datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M:%S")
    dev = LvgmcRiver(
        "lv-hydro-riga-01", SimClock(realtime=True),
        station="HD073810", latitude=57.03, longitude=24.13, key_dir=str(tmp_path),
    )
    mapped = dev.map([
        {"STATION_ID": "HD073810", "ABBREVIATION": "LIMEN", "DATETIME": now, "VALUE": "125"},
        {"STATION_ID": "HD073810", "ABBREVIATION": "WTEMD", "DATETIME": now, "VALUE": "14.2"},
    ])
    assert abs(mapped["gage_height_m"] - 1.25) < 1e-9


def test_p12_lv_mesh_counts():
    assert len(LVGMC_WX_MESH) == 8
    assert len(LVGMC_HYDRO_MESH) == 6
    assert P12_LV_COUNT == 14


def test_atlas_rows_parity():
    rows = atlas_rows()
    assert len(rows) == P12_LV_COUNT
    ids = [r[0] for r in rows]
    assert len(ids) == len(set(ids))
    for device_id, lat, lon, place, layer, cap, note in rows:
        assert device_id.startswith("lv-")
        assert -90 <= lat <= 90
        assert -180 <= lon <= 180
        assert cap in ("gaia.weather.read@v1", "gaia.river.read@v1")
        assert note


class _Fleet:
    def __init__(self) -> None:
        self.devices: list = []

    def add(self, device: object) -> None:
        self.devices.append(device)


def test_register_p12_lv(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_LVGMC_ENABLED", "1")
    fleet = _Fleet()
    n = register_p12_lv(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == P12_LV_COUNT
    assert len(fleet.devices) == P12_LV_COUNT


@pytest.mark.live
def test_live_probe_lvgmc_riga(tmp_path):
    """Optional live HTTP — operative CSV may be >6h stale (fail-closed offline)."""
    station, lat, lon = LVGMC_WX_MESH[0][1], LVGMC_WX_MESH[0][2], LVGMC_WX_MESH[0][3]
    dev = LvgmcWeather(
        "lv-wx-riga-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        sample = dev.sample()
    except DeviceOffline as exc:
        pytest.skip(f"live probe offline (stale upstream expected): {exc}")
    assert sample.get("temperature_c") is not None or sample.get("wind_mps") is not None
