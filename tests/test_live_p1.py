"""P1 relay mappers — no upstream HTTP."""

from __future__ import annotations

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p1 import (
    DefraAurn,
    DwdBrightSky,
    EcccClimateHourly,
    EffisCurrentFires,
    GeoNetQuake,
    NwsFloodAlerts,
    UhslcTide,
    UsgsVolcano,
)


def test_nws_flood_cap_hotspots(tmp_path):
    clock = SimClock(realtime=True)
    dev = NwsFloodAlerts("nws-flood-01", clock, key_dir=str(tmp_path))
    payload = {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [-90.0, 35.0]},
                "properties": {
                    "severity": "Severe",
                    "event": "Flash Flood Warning",
                    "headline": "Flash Flood Warning",
                    "areaDesc": "TN",
                },
            }
        ]
    }
    hs = dev.collect_hotspots(payload)
    assert hs[0]["latitude"] == 35.0
    assert hs[0]["severity_score"] == 80.0


def test_effis_geojson(tmp_path):
    clock = SimClock(realtime=True)
    dev = EffisCurrentFires("effis-01", clock, key_dir=str(tmp_path))
    payload = {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [8.0, 40.0]},
                "properties": {"AREA_HA": 500, "FIREDATE": "2026-08-12"},
            }
        ]
    }
    hs = dev.collect_hotspots(payload)
    assert hs[0]["latitude"] == 40.0
    assert hs[0]["area_ha"] == 500


def test_volcano_alert_score(tmp_path):
    clock = SimClock(realtime=True)
    dev = UsgsVolcano("usgs-volcano-01", clock, key_dir=str(tmp_path))
    payload = [
        {
            "volcano_name": "Kilauea",
            "latitude": 19.4,
            "longitude": -155.2,
            "alert_level": "WATCH",
            "color_code": "ORANGE",
        }
    ]
    hs = dev.collect_hotspots(payload)
    assert hs[0]["severity_score"] == 80.0
    assert hs[0]["name"] == "Kilauea"


def test_dwd_brightsky_map(tmp_path):
    clock = SimClock(realtime=True)
    dev = DwdBrightSky("dwd-01", clock, key_dir=str(tmp_path))
    mapped = dev.map({
        "weather": {
            "temperature": 17.2,
            "relative_humidity": 70,
            "pressure_msl": 1013.2,
            "wind_speed": 18.0,
        }
    })
    assert mapped["temperature_c"] == 17.2
    assert abs(mapped["wind_mps"] - 5.0) < 0.01


def test_aurn_erg_map(tmp_path):
    clock = SimClock(realtime=True)
    dev = DefraAurn("aurn-01", clock, site_code="MY1", key_dir=str(tmp_path))
    payload = {
        "HourlyAirQualityIndex": {
            "LocalAuthority": {
                "Site": {
                    "@Latitude": "51.5225",
                    "@Longitude": "-0.1546",
                    "species": [
                        {"@SpeciesCode": "NO2", "@AirQualityIndex": "1", "@IndexSource": "Measurement"},
                        {"@SpeciesCode": "O3", "@AirQualityIndex": "2", "@IndexSource": "Measurement"},
                        {"@SpeciesCode": "SO2", "@AirQualityIndex": "0", "@IndexSource": "Measurement"},
                    ],
                }
            }
        }
    }
    mapped = dev.map(payload)
    assert mapped["air_quality_index"] == 2.0
    assert mapped["latitude"] == 51.5225


def test_eccc_pressure_is_normalized_from_kpa_to_hpa(tmp_path):
    dev = EcccClimateHourly("eccc-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map({
        "features": [{
            "geometry": {"type": "Point", "coordinates": [-75.7167, 45.3833]},
            "properties": {"TEMP": 17, "STATION_PRESSURE": 100.05, "WIND_SPEED": 5},
        }]
    })
    assert mapped["pressure_hpa"] == pytest.approx(1000.5)
    assert mapped["wind_mps"] == pytest.approx(5 / 3.6)


def test_uhslc_longitude_is_normalized_for_geojson(tmp_path):
    dev = UhslcTide("uhslc-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map({
        "table": {
            "columnNames": ["time", "sea_level", "latitude", "longitude"],
            "rows": [["2026-06-30T23:00:00Z", 1506, 21.3067, 202.1333]],
        }
    })
    assert mapped["water_level_m"] == pytest.approx(1.506)
    assert mapped["longitude"] == pytest.approx(-157.8667)


def test_uhslc_stale_archive_row_is_not_republished_as_live(tmp_path, monkeypatch):
    dev = UhslcTide("uhslc-01", SimClock(realtime=True), key_dir=str(tmp_path))
    payload = {
        "table": {
            "columnNames": ["time", "sea_level", "latitude", "longitude"],
            "rows": [["2020-01-01T00:00:00Z", 1506, 21.3067, 202.1333]],
        }
    }
    monkeypatch.setattr(dev, "_fetch", lambda _url: payload)
    with pytest.raises(DeviceOffline, match="stale"):
        dev.sample()


def test_geonet_quakes(tmp_path):
    clock = SimClock(realtime=True)
    dev = GeoNetQuake("geonet-01", clock, key_dir=str(tmp_path))
    payload = {
        "features": [
            {
                "properties": {"magnitude": 4.2, "depth": 12.0},
                "geometry": {"coordinates": [174.0, -41.3, 12.0]},
            }
        ]
    }
    hs = dev.collect_hotspots(payload)
    assert hs[0]["magnitude"] == 4.2
    assert hs[0]["latitude"] == -41.3


def test_effis_polygon_centroid_and_empty_fail_closed(tmp_path):
    clock = SimClock(realtime=True)
    dev = EffisCurrentFires("effis-01", clock, key_dir=str(tmp_path))
    poly = {
        "features": [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[8.0, 40.0], [9.0, 40.0], [9.0, 41.0], [8.0, 41.0], [8.0, 40.0]]],
                },
                "properties": {"AREA_HA": 800, "FIREDATE": "2026-08-12"},
            }
        ]
    }
    hs = dev.collect_hotspots(poly)
    assert hs[0]["area_ha"] == 800
    assert 40.0 <= hs[0]["latitude"] <= 41.0
    assert 8.0 <= hs[0]["longitude"] <= 9.0
    with pytest.raises(DeviceOffline, match="empty"):
        dev.collect_hotspots({"features": []})
    with pytest.raises(DeviceOffline, match="no geometry"):
        dev.collect_hotspots({"features": [{"geometry": None, "properties": {}}]})


def test_effis_wfs_lat_lon_lands_in_asturias(tmp_path):
    clock = SimClock(realtime=True)
    dev = EffisCurrentFires("effis-01", clock, key_dir=str(tmp_path))
    payload = {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [43.3433, -4.6893]},
                "properties": {
                    "AREA_HA": 52,
                    "COUNTRY": "ES",
                    "PROVINCE": "Asturias",
                    "COMMUNE": "Peñamellera Alta",
                    "FIREDATE": "2026-08-11 19:49:00",
                },
            }
        ]
    }
    hs = dev.collect_hotspots(payload)
    assert abs(hs[0]["latitude"] - 43.3433) < 1e-4
    assert abs(hs[0]["longitude"] + 4.6893) < 1e-4
    assert hs[0]["area_ha"] == 52


def test_flood_and_volcano_empty_fail_closed(tmp_path):
    clock = SimClock(realtime=True)
    flood = NwsFloodAlerts("nws-flood-01", clock, key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline, match="empty"):
        flood.collect_hotspots({"features": []})
    with pytest.raises(DeviceOffline, match="no geometry"):
        flood.collect_hotspots({"features": [{"geometry": None, "properties": {"severity": "Severe"}}]})
    volcano = UsgsVolcano("usgs-volcano-01", clock, key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline, match="empty"):
        volcano.collect_hotspots([])
    with pytest.raises(DeviceOffline, match="no coordinates"):
        volcano.collect_hotspots([{"volcano_name": "Nowhere", "alert_level": "WATCH"}])


def test_p1_license_strings():
    assert "public domain" in NwsFloodAlerts.source.lower() or "PD" in NwsFloodAlerts.source
    assert "GloFAS" in NwsFloodAlerts.source
    assert "WaterWatch" in NwsFloodAlerts.source
    assert "CC BY 4.0" in EffisCurrentFires.source
    assert "public domain" in UsgsVolcano.source.lower()
    assert "CC BY 4.0" in DwdBrightSky.source
    assert "OGL" in DefraAurn.source or "Open Government" in DefraAurn.source
    assert "CC BY 3.0" in GeoNetQuake.source
