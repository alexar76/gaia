"""P11 relay mapper tests — fixtures only, no live HTTP."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p11 import (
    AEMET_MESH,
    BAFU_MESH,
    CWA_MESH,
    ESTONIA_MESH,
    HK_AQHI_MESH,
    ICELAND_MESH,
    METEOFRANCE_MESH,
    METEOSWISS_MESH,
    P11_KEYED_COUNT,
    P11_OPEN_COUNT,
    AemetWeather,
    BafuRiver,
    CwaWeather,
    EstoniaWeather,
    HkAqhiAir,
    IcelandWeather,
    MeteoFranceWeather,
    MeteoSwissWeather,
    lv95_to_wgs84,
    register_p11_relays,
)
from gaia.source_policy import require_approved_source


def test_p11_source_policies_are_commercial():
    expected = {
        "aemet_es": "Spanish PSI reuse (Fuente: AEMET)",
        "meteoswiss_ch": "CC BY 4.0",
        "bafu_hydro": "OGD-CH Open-Use",
        "cwa_tw": "OGDL 1.0",
        "meteofrance_dpobs": "Etalab Licence Ouverte 2.0",
        "estonia_ews": "CC BY 4.0",
        "iceland_imo": "ODC-By",
        "hongkong_aqhi": "DATA.GOV.HK terms",
    }
    for source_id, licence in expected.items():
        policy = require_approved_source(source_id)
        assert policy.licence == licence
        assert "commercial" in policy.commercial_basis.lower()


def test_lv95_to_wgs84_lands_in_switzerland():
    # Arosa-ish LV95 from the MeteoSwiss GeoJSON CRS.
    lat, lon = lv95_to_wgs84(2_771_032.3, 1_184_823.0)
    assert 46.7 < lat < 46.9
    assert 9.6 < lon < 9.8


def test_aemet_map(tmp_path):
    station, lat, lon = AEMET_MESH[0][1], AEMET_MESH[0][2], AEMET_MESH[0][3]
    dev = AemetWeather(
        "aemet-wx-madrid-01", SimClock(realtime=True),
        station=station, api_key="aemet-test-key-12345",
        latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    mapped = dev.map([
        {"fint": "2020-01-01T00:00:00", "ta": 1.0, "vv": 1.0},
        {"fint": now, "ta": 21.5, "hr": 47, "pres": 1016.2, "vv": 3.1, "lat": lat, "lon": lon},
    ])
    assert mapped["temperature_c"] == 21.5
    assert mapped["wind_mps"] == 3.1
    assert mapped["humidity_pct"] == 47.0


def test_aemet_refuses_off_host_datos(tmp_path, monkeypatch):
    station, lat, lon = AEMET_MESH[0][1], AEMET_MESH[0][2], AEMET_MESH[0][3]
    dev = AemetWeather(
        "aemet-wx-madrid-01", SimClock(realtime=True),
        station=station, api_key="aemet-test-key-12345",
        latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    monkeypatch.setattr(dev, "_fetch", lambda url: {
        "estado": 200,
        "datos": "https://evil.example/datos",
    })
    try:
        dev.sample()
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_meteoswiss_map_converts_wind_kmh(tmp_path):
    name, lat, lon = METEOSWISS_MESH[0][1], METEOSWISS_MESH[0][2], METEOSWISS_MESH[0][3]
    dev = MeteoSwissWeather(
        "ch-wx-zurich-01", SimClock(realtime=True),
        station_name=name, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    feature = {
        "type": "Feature",
        "properties": {"station_name": name, "value": 18.0},
        "geometry": {"type": "Point", "coordinates": [2_683_200, 1_248_200]},
    }
    mapped = dev.map({
        "temperature_c": {"features": [{**feature, "properties": {"station_name": name, "value": 15.6}}]},
        "humidity_pct": {"features": [{**feature, "properties": {"station_name": name, "value": 70}}]},
        "pressure_hpa": {"features": [{**feature, "properties": {"station_name": name, "value": 1018}}]},
        "wind_kmh": {"features": [feature]},
    })
    assert mapped["temperature_c"] == 15.6
    assert abs(mapped["wind_mps"] - 5.0) < 1e-9
    assert 47.2 < mapped["latitude"] < 47.5
    assert 8.4 < mapped["longitude"] < 8.7


def test_meteoswiss_missing_station_offline(tmp_path):
    name, lat, lon = METEOSWISS_MESH[0][1], METEOSWISS_MESH[0][2], METEOSWISS_MESH[0][3]
    dev = MeteoSwissWeather(
        "ch-wx-zurich-01", SimClock(realtime=True),
        station_name=name, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    empty = {"features": [{"properties": {"station_name": "Elsewhere", "value": 1}}]}
    try:
        dev.map({
            "temperature_c": empty, "humidity_pct": empty,
            "pressure_hpa": empty, "wind_kmh": empty,
        })
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_bafu_map_geodetic_level(tmp_path):
    station, lat, lon = BAFU_MESH[0][1], BAFU_MESH[0][2], BAFU_MESH[0][3]
    dev = BafuRiver(
        "bafu-basel-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    mapped = dev.map({
        "https://environment.ld.admin.ch/foen/hydro/dimension/discharge": {
            "@value": "476.162",
        },
        "https://environment.ld.admin.ch/foen/hydro/dimension/waterLevel": {
            "@value": "244.965",
        },
        "https://environment.ld.admin.ch/foen/hydro/dimension/measurementTime": {
            "@value": now,
        },
    })
    assert mapped["discharge_m3s"] == 476.162
    assert mapped["gage_height_m"] == 244.965
    assert mapped["latitude"] == lat


def test_bafu_stale_offline(tmp_path):
    station, lat, lon = BAFU_MESH[0][1], BAFU_MESH[0][2], BAFU_MESH[0][3]
    dev = BafuRiver(
        "bafu-basel-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map({
            "discharge": 400,
            "waterLevel": 245,
            "measurementTime": "2020-01-01T00:00:00+00:00",
        })
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_cwa_map(tmp_path):
    station, lat, lon = CWA_MESH[0][1], CWA_MESH[0][2], CWA_MESH[0][3]
    dev = CwaWeather(
        "cwa-wx-taipei-01", SimClock(realtime=True),
        station=station, api_key="cwa-test-key-12345",
        latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now = datetime.now(timezone.utc).isoformat()
    mapped = dev.map({
        "records": {
            "Station": [{
                "StationId": station,
                "ObsTime": {"DateTime": now},
                "WeatherElement": {
                    "AirTemperature": 28.5,
                    "RelativeHumidity": 72,
                    "AirPressure": 1012.1,
                    "WindSpeed": 2.4,
                },
                "GeoInfo": {
                    "Coordinates": [{
                        "CoordinateName": "WGS84",
                        "StationLatitude": lat,
                        "StationLongitude": lon,
                    }]
                },
            }]
        }
    })
    assert mapped["temperature_c"] == 28.5
    assert mapped["wind_mps"] == 2.4


def test_meteofrance_kelvin_and_pascal(tmp_path):
    station, lat, lon = METEOFRANCE_MESH[0][1], METEOFRANCE_MESH[0][2], METEOFRANCE_MESH[0][3]
    dev = MeteoFranceWeather(
        "mf-wx-paris-01", SimClock(realtime=True),
        station=station, application_id="mf-app-id-12345",
        latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    mapped = dev.map({
        "t": 288.15,
        "u": 61,
        "pmer": 101325,
        "ff": 4.2,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    })
    assert abs(mapped["temperature_c"] - 15.0) < 1e-9
    assert abs(mapped["pressure_hpa"] - 1013.25) < 1e-9
    assert mapped["wind_mps"] == 4.2


def test_estonia_xml_map(tmp_path):
    name, lat, lon = ESTONIA_MESH[0][1], ESTONIA_MESH[0][2], ESTONIA_MESH[0][3]
    dev = EstoniaWeather(
        "ee-wx-tallinn-01", SimClock(realtime=True),
        station_name=name, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    xml = (
        f'<observations timestamp="{int(time.time())}">'
        f"<station><name>{name}</name>"
        f"<latitude>{lat}</latitude><longitude>{lon}</longitude>"
        "<airtemperature>12.4</airtemperature><relativehumidity>76</relativehumidity>"
        "<airpressure>1011.2</airpressure><windspeed>3.3</windspeed>"
        "</station></observations>"
    )
    mapped = dev.map(xml)
    assert mapped["temperature_c"] == 12.4
    assert mapped["wind_mps"] == 3.3


def test_iceland_map(tmp_path):
    station, lat, lon = ICELAND_MESH[0][1], ICELAND_MESH[0][2], ICELAND_MESH[0][3]
    dev = IcelandWeather(
        "is-wx-reykjavik-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    mapped = dev.map([
        {"station": int(station), "t": 11.1, "rh": 73, "p": 983.4, "f": 7.4, "time": now},
        {"station": 9999, "t": 0.0, "f": 0.0, "time": now},
    ])
    assert mapped["temperature_c"] == 11.1
    assert mapped["wind_mps"] == 7.4


def test_hk_aqhi_ten_plus(tmp_path):
    name, lat, lon = HK_AQHI_MESH[0][1], HK_AQHI_MESH[0][2], HK_AQHI_MESH[0][3]
    dev = HkAqhiAir(
        "hk-aqhi-centralwestern-01", SimClock(realtime=True),
        station_name=name, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now = datetime.now(timezone.utc).isoformat()
    mapped = dev.map([
        {"station": name, "aqhi": "10+", "publish_date": now},
        {"station": "Mong Kok", "aqhi": "3", "publish_date": now},
    ])
    assert mapped["air_quality_index"] == 11.0
    assert "pm2_5_ugm3" not in mapped


def test_hk_aqhi_naive_publish_date_is_hong_kong_local(tmp_path):
    """EPD publish_date is naive local time (HKT, UTC+8), not UTC."""
    from datetime import timedelta

    name, lat, lon = HK_AQHI_MESH[0][1], HK_AQHI_MESH[0][2], HK_AQHI_MESH[0][3]
    dev = HkAqhiAir(
        "hk-aqhi-centralwestern-01", SimClock(realtime=True),
        station_name=name, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    naive_hkt = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S")
    mapped = dev.map([{"station": name, "aqhi": 4, "publish_date": naive_hkt}])
    assert mapped["air_quality_index"] == 4.0


def test_hk_aqhi_missing_offline(tmp_path):
    name, lat, lon = HK_AQHI_MESH[0][1], HK_AQHI_MESH[0][2], HK_AQHI_MESH[0][3]
    dev = HkAqhiAir(
        "hk-aqhi-centralwestern-01", SimClock(realtime=True),
        station_name=name, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map([{"station": "Mong Kok", "aqhi": "3"}])
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


class _Fleet:
    def __init__(self):
        self._devices = {}

    def add(self, device):
        self._devices[device.device_id] = device

    @property
    def ids(self):
        return set(self._devices)


def test_register_p11_open_meshes(tmp_path, monkeypatch):
    for name in (
        "GAIA_AEMET_API_KEY", "GAIA_CWA_API_KEY", "GAIA_METEOFRANCE_APPLICATION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    fleet = _Fleet()
    n = register_p11_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == P11_OPEN_COUNT
    assert "ch-wx-zurich-01" in fleet.ids
    assert "bafu-basel-01" in fleet.ids
    assert "ee-wx-tallinn-01" in fleet.ids
    assert "is-wx-reykjavik-01" in fleet.ids
    assert "hk-aqhi-centralwestern-01" in fleet.ids
    assert "aemet-wx-madrid-01" not in fleet.ids
    assert "cwa-wx-taipei-01" not in fleet.ids
    assert "mf-wx-paris-01" not in fleet.ids


def test_register_p11_keyed_meshes(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_AEMET_API_KEY", "aemet-test-key-12345")
    monkeypatch.setenv("GAIA_CWA_API_KEY", "cwa-test-key-12345")
    monkeypatch.setenv("GAIA_METEOFRANCE_APPLICATION_ID", "mf-app-id-12345")
    fleet = _Fleet()
    n = register_p11_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == P11_OPEN_COUNT + P11_KEYED_COUNT
    assert "aemet-wx-madrid-01" in fleet.ids
    assert "cwa-wx-taipei-01" in fleet.ids
    assert "mf-wx-paris-01" in fleet.ids
    assert fleet._devices["aemet-wx-madrid-01"].__class__.__name__ == "AemetWeather"
    assert fleet._devices["bafu-basel-01"].__class__.__name__ == "BafuRiver"


def test_p11_mesh_ids_are_unique():
    ids = [
        row[0] for row in (
            *AEMET_MESH, *METEOSWISS_MESH, *BAFU_MESH, *CWA_MESH,
            *METEOFRANCE_MESH, *ESTONIA_MESH, *ICELAND_MESH, *HK_AQHI_MESH,
        )
    ]
    assert len(ids) == len(set(ids))


def test_p11_atlas_catalog_parity():
    import sys
    from pathlib import Path

    atlas_root = Path(__file__).resolve().parents[2] / "atlas"
    if not (atlas_root / "atlas" / "stations.py").is_file():
        import pytest
        pytest.skip("atlas catalog not in this tree")
    if str(atlas_root) not in sys.path:
        sys.path.insert(0, str(atlas_root))
    from atlas.stations import STATION_CATALOG

    meshes = (
        AEMET_MESH, METEOSWISS_MESH, BAFU_MESH, CWA_MESH,
        METEOFRANCE_MESH, ESTONIA_MESH, ICELAND_MESH, HK_AQHI_MESH,
    )
    for mesh in meshes:
        for row in mesh:
            device_id = row[0]
            meta = STATION_CATALOG[device_id]
            assert meta["kind"] == "point"
            assert abs(float(meta["lat"]) - float(row[2])) < 1e-4
            assert abs(float(meta["lon"]) - float(row[3])) < 1e-4
