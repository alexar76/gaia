"""P5 relay mappers — no upstream HTTP."""

from __future__ import annotations

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p5 import (
    AviationMetar,
    DigitrafficRail,
    DigitrafficRoadWeather,
    GeoShakeQuake,
    NaadAlerts,
    NasaDonki,
    PegelonlineRiver,
    SwpcGoesXray,
    SwpcSolarWind,
    UsDroughtMonitor,
)
from gaia.source_policy import require_approved_source

NAAD_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:georss="http://www.georss.org/georss"
      xmlns:cap="urn:oasis:names:tc:emergency:cap:1.1">
  <title>NAAD</title>
  <entry>
    <title>Severe Thunderstorm Warning</title>
    <author><name>Environment Canada</name></author>
    <georss:polygon>45.42 -75.70 45.43 -75.69 45.42 -75.68 45.41 -75.69 45.42 -75.70</georss:polygon>
  </entry>
  <entry>
    <title>Special Weather Statement</title>
    <author><name>ECCC</name></author>
    <georss:point>43.65 -79.38</georss:point>
  </entry>
</feed>
"""

GEOSHAKE_FDSN = """#EventID|Time|Latitude|Longitude|Depth/km|Author|Catalog|Contributor|ContributorID|MagType|Magnitude|MagAuthor|EventLocationName
gs1|2026-09-01T12:00:00|37.5|-122.1|8.0|GeoShake|GS|GS|1|ml|4.2|GS|Bay Area
gs2|2026-09-01T11:00:00|36.0|-118.0|12.0|GeoShake|GS|GS|2|ml|3.1|GS|Sierra
"""


def test_source_policies_are_commercial(tmp_path):
    assert require_approved_source("pegelonline").licence == "DL-DE-Zero-2.0"
    assert "commercial" in require_approved_source("pegelonline").commercial_basis.lower()
    assert require_approved_source("aviation_metar").licence.startswith("U.S. Government")
    assert require_approved_source("digitraffic_road").licence == "CC BY 4.0"
    assert require_approved_source("digitraffic_rail").licence == "CC BY 4.0"
    assert "NDMC" in require_approved_source("usdm").attribution
    assert require_approved_source("geoshake").licence == "CC BY 4.0"
    assert "CAP-CP" in require_approved_source("naad_alerts").licence
    assert require_approved_source("nasa_donki").licence == "NASA open data"
    assert require_approved_source("noaa_swpc").licence.startswith("U.S. Government")


def test_pegelonline_bonn_map(tmp_path):
    dev = PegelonlineRiver("pegel-bonn-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map({
        "latitude": 50.7374,
        "longitude": 7.0982,
        "timeseries": [
            {
                "shortname": "Q",
                "unit": "m³/s",
                "currentMeasurement": {"value": 1240.0},
            },
            {
                "shortname": "W",
                "unit": "cm",
                "currentMeasurement": {"value": 287.0},
            },
        ],
    })
    assert mapped["discharge_m3s"] == 1240.0
    assert mapped["gage_height_m"] == pytest.approx(2.87)
    assert mapped["latitude"] == 50.7374
    assert mapped["longitude"] == 7.0982


def test_pegelonline_empty_is_offline(tmp_path):
    dev = PegelonlineRiver("pegel-bonn-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.map({})
    with pytest.raises(DeviceOffline):
        dev.map({"latitude": 50.7, "longitude": 7.1, "timeseries": []})


def test_aviation_metar_map(tmp_path):
    dev = AviationMetar("metar-kjfk-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map([{
        "temp": 18.0,
        "dewp": 12.0,
        "wspd": 10.0,
        "altim": 1013.2,
        "lat": 40.6398,
        "lon": -73.7789,
    }])
    assert mapped["temperature_c"] == 18.0
    assert mapped["dewpoint_c"] == 12.0
    assert mapped["wind_mps"] == pytest.approx(5.14444)
    assert mapped["pressure_hpa"] == 1013.2
    assert mapped["latitude"] == 40.6398


def test_aviation_metar_empty_is_offline(tmp_path):
    dev = AviationMetar("metar-kjfk-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.map([])
    with pytest.raises(DeviceOffline):
        dev.map([{"lat": 40.6, "lon": -73.7}])


def test_digitraffic_road_hotspots(tmp_path):
    dev = DigitrafficRoadWeather(
        "fintraffic-road-01", SimClock(realtime=True), key_dir=str(tmp_path)
    )
    meta = {
        "features": [
            {
                "id": 1001,
                "properties": {"id": 1001},
                "geometry": {"type": "Point", "coordinates": [24.94, 60.17]},
            },
            {
                "id": 1002,
                "properties": {"id": 1002},
                "geometry": {"type": "Point", "coordinates": [25.00, 60.20]},
            },
        ]
    }
    data = {
        "stations": [
            {
                "id": 1001,
                "sensorValues": [
                    {"name": "ILMA", "value": -12.5},
                    {"name": "KESKITUULI", "value": 3.2},
                ],
            },
            {
                "id": 1002,
                "sensorValues": [
                    {"name": "ILMA", "value": 4.0},
                    {"name": "KESKITUULI", "value": 1.0},
                ],
            },
        ]
    }
    hs = dev.collect_hotspots((meta, data))
    assert hs[0]["station_id"] == "1001"
    assert hs[0]["temperature_c"] == -12.5
    assert hs[0]["wind_mps"] == 3.2
    assert hs[0]["latitude"] == 60.17
    assert hs[0]["longitude"] == 24.94


def test_digitraffic_road_empty_is_offline(tmp_path):
    dev = DigitrafficRoadWeather(
        "fintraffic-road-01", SimClock(realtime=True), key_dir=str(tmp_path)
    )
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots(({"features": []}, {"stations": []}))


def test_digitraffic_rail_hotspots(tmp_path):
    dev = DigitrafficRail(
        "fintraffic-rail-01", SimClock(realtime=True), key_dir=str(tmp_path)
    )
    hs = dev.collect_hotspots([
        {
            "trainNumber": 42,
            "speed": 120.0,
            "location": {"type": "Point", "coordinates": [24.9, 60.2]},
        },
        {
            "trainNumber": 7,
            "speed": 40.0,
            "location": {"type": "Point", "coordinates": [25.1, 60.3]},
        },
    ])
    assert hs[0]["train_number"] == "42"
    assert hs[0]["speed_kmh"] == 120.0
    assert hs[0]["latitude"] == 60.2
    assert hs[0]["longitude"] == 24.9


def test_digitraffic_rail_empty_is_offline(tmp_path):
    dev = DigitrafficRail(
        "fintraffic-rail-01", SimClock(realtime=True), key_dir=str(tmp_path)
    )
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots([])


def test_usdm_hotspots(tmp_path):
    dev = UsDroughtMonitor("usdm-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots([
        {
            "_fips": "06",
            "mapDate": "2026-09-01",
            "d0": 10.0,
            "d1": 20.0,
            "d2": 5.0,
            "d3": 0.0,
            "d4": 0.0,
        },
        {
            "_fips": "48",
            "mapDate": "2026-09-01",
            "d0": 0.0,
            "d1": 0.0,
            "d2": 0.0,
            "d3": 15.0,
            "d4": 2.0,
        },
    ])
    assert hs[0]["state"] == "TX"
    assert hs[0]["category"] == "D4"
    assert hs[0]["severity_score"] == 4.0
    assert hs[0]["drought_pct"] == pytest.approx(2.0)
    assert hs[1]["state"] == "CA"
    assert hs[1]["drought_pct"] == pytest.approx(5.0)


def test_usdm_empty_is_offline(tmp_path):
    dev = UsDroughtMonitor("usdm-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots([])
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots([{"_fips": "06", "d0": 0, "d1": 0, "d2": 0, "d3": 0, "d4": 0}])


def test_geoshake_list_hotspots(tmp_path):
    dev = GeoShakeQuake("geoshake-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots([
        {"lat": 37.5, "lon": -122.1, "mag": 4.2, "depth": 8.0, "place": "Bay Area"},
        {"latitude": 36.0, "longitude": -118.0, "magnitude": 3.1, "depth_km": 12.0},
    ])
    assert hs[0]["magnitude"] == 4.2
    assert hs[0]["depth_km"] == 8.0
    assert hs[0]["region"] == "Bay Area"
    assert hs[1]["magnitude"] == 3.1


def test_geoshake_fdsn_text_hotspots(tmp_path):
    dev = GeoShakeQuake("geoshake-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots(GEOSHAKE_FDSN)
    assert hs[0]["magnitude"] == 4.2
    assert hs[0]["latitude"] == 37.5
    assert hs[0]["longitude"] == -122.1
    assert hs[0]["depth_km"] == 8.0


def test_geoshake_empty_is_offline(tmp_path):
    dev = GeoShakeQuake("geoshake-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots([])
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots("#EventID|Time\n")


def test_naad_atom_hotspots(tmp_path):
    dev = NaadAlerts("naad-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots(NAAD_ATOM)
    assert hs[0]["severity_score"] == 3.0
    assert "Thunderstorm" in hs[0]["headline"]
    assert hs[0]["issuer"] == "Environment Canada"
    assert hs[0]["latitude"] == pytest.approx(45.42, abs=0.02)
    assert hs[0]["longitude"] == pytest.approx(-75.692, abs=0.02)


def test_naad_empty_is_offline(tmp_path):
    dev = NaadAlerts("naad-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots("")
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots(
            '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            "<title>empty</title></feed>"
        )


def test_swpc_solar_wind_map(tmp_path):
    dev = SwpcSolarWind("swpc-solarwind-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map([
        {"proton_speed": 380.0},
        {"proton_speed": 420.5},
    ])
    assert mapped["solar_wind_kms"] == 420.5
    assert mapped["latitude"] == pytest.approx(40.0150)
    assert mapped["longitude"] == pytest.approx(-105.2705)


def test_swpc_solar_wind_empty_is_offline(tmp_path):
    dev = SwpcSolarWind("swpc-solarwind-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.map([])


def test_swpc_goes_xray_map(tmp_path):
    dev = SwpcGoesXray("swpc-xray-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map([
        {"flux": 1.2e-6},
        {"flux": 3.4e-6},
        {"other": 1},
    ])
    assert mapped["xray_flux"] == 3.4e-6
    assert mapped["latitude"] == pytest.approx(40.0150)


def test_swpc_goes_xray_empty_is_offline(tmp_path):
    dev = SwpcGoesXray("swpc-xray-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.map([])
    with pytest.raises(DeviceOffline):
        dev.map([{"time_tag": "x"}])


def test_nasa_donki_hotspots(tmp_path):
    dev = NasaDonki("donki-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots([
        {"messageType": "Report", "messageID": "20260901-R1"},
        {"messageType": "CME", "messageID": "20260901-CME-001"},
    ])
    assert hs[0]["message_type"] == "CME"
    assert hs[0]["severity_score"] == 3.0
    assert hs[0]["message_id"] == "20260901-CME-001"
    assert hs[1]["severity_score"] == 2.0


def test_nasa_donki_empty_is_offline(tmp_path):
    dev = NasaDonki("donki-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots([])


def test_metar_and_pegel_meshes_register_point_devices(tmp_path, monkeypatch):
    """Airport/Rhine meshes are one clickable Hub device per coordinate."""
    from gaia.devices.live_p5 import METAR_MESH, PEGEL_MESH, register_p5_relays

    class _Fleet:
        def __init__(self):
            self.devices = []

        def add(self, device):
            self.devices.append(device)

    # Keep the test fast: only mesh + skip remote cluster feeds.
    for env in (
        "GAIA_DIGITRAFFIC_ROAD_ENABLED",
        "GAIA_DIGITRAFFIC_RAIL_ENABLED",
        "GAIA_USDM_ENABLED",
        "GAIA_GEOSHAKE_ENABLED",
        "GAIA_NAAD_ENABLED",
        "GAIA_SWPC_SOLARWIND_ENABLED",
        "GAIA_SWPC_XRAY_ENABLED",
        "GAIA_DONKI_ENABLED",
    ):
        monkeypatch.setenv(env, "0")
    monkeypatch.setenv("GAIA_PEGELONLINE_ENABLED", "1")
    monkeypatch.setenv("GAIA_METAR_ENABLED", "1")

    fleet = _Fleet()
    n = register_p5_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    ids = {d.device_id for d in fleet.devices}
    assert n == len(METAR_MESH) + len(PEGEL_MESH)
    assert {row[0] for row in METAR_MESH} <= ids
    assert {row[0] for row in PEGEL_MESH} <= ids
