"""P3 relay mappers — no upstream HTTP."""

from __future__ import annotations

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p3 import (
    AdsbLolTraffic,
    EaFloodWarnings,
    EmscQuake,
    KystverketAis,
    NhcCyclone,
    PtwcTsunamiAlerts,
)
from gaia.source_policy import require_approved_source

PTWC_ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:georss="http://www.georss.org/georss">
  <entry>
    <title>Tsunami Warning</title>
    <summary>Hazardous tsunami waves are possible.</summary>
    <category term="Warning"/>
    <georss:point>19.5 -155.0</georss:point>
  </entry>
  <entry>
    <title>Earthquake Information</title>
    <summary>An earthquake occurred. This is information only.</summary>
    <georss:point>5.7 125.2</georss:point>
  </entry>
</feed>
"""

PTWC_INFO_ONLY = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:georss="http://www.georss.org/georss">
  <entry>
    <title>Earthquake Information</title>
    <summary>Information statement only.</summary>
    <georss:point>5.7 125.2</georss:point>
  </entry>
</feed>
"""


def test_p3_source_policies_are_commercial():
    assert require_approved_source("nhc_cyclone").licence.startswith("U.S.")
    assert require_approved_source("emsc_fdsn").licence == "CC BY 4.0"
    assert "Open Government" in require_approved_source("uk_ea_flood").licence
    assert require_approved_source("ptwc_tsunami").licence.startswith("U.S.")
    assert require_approved_source("kystverket_ais").licence == "NLOD 2.0"
    assert require_approved_source("kystverket_ais").requires_operator_account is True
    assert require_approved_source("adsb_lol").licence == "ODbL 1.0"
    assert require_approved_source("adsb_lol").hosts == ("api.adsb.lol",)


def test_nhc_cyclone_cluster(tmp_path):
    dev = NhcCyclone("nhc-cyclone-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots({
        "activeStorms": [
            {
                "id": "al052026",
                "name": "Testcane",
                "classification": "HU",
                "intensity": "90",
                "pressure": "960",
                "latitudeNumeric": 22.1,
                "longitudeNumeric": -75.0,
            },
            {
                "id": "wp012026",
                "name": "TyphoonShouldDrop",
                "classification": "TY",
                "intensity": "120",
                "latitudeNumeric": 15.0,
                "longitudeNumeric": 140.0,
            },
        ]
    })
    assert len(hs) == 1
    assert hs[0]["name"] == "Testcane"
    assert hs[0]["intensity_kn"] == 90.0
    assert hs[0]["pressure_hpa"] == 960.0
    assert "typhoon" in dev.source.lower() or "JTWC" in dev.source


def test_nhc_empty_season_is_offline(tmp_path):
    dev = NhcCyclone("nhc-cyclone-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline, match="empty"):
        dev.collect_hotspots({"activeStorms": []})


def test_emsc_quake_uses_positive_depth(tmp_path):
    dev = EmscQuake("emsc-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots({
        "features": [{
            "geometry": {"type": "Point", "coordinates": [12.0, 45.0, -10.0]},
            "properties": {"mag": 4.2, "depth": 10.0, "flynn_region": "ITALY"},
        }]
    })
    assert hs[0]["magnitude"] == 4.2
    assert hs[0]["depth_km"] == 10.0
    assert hs[0]["region"] == "ITALY"
    assert "cite EMSC" in dev.source or "CC BY 4.0" in dev.source


def test_ea_flood_england_only_and_skips_withdrawn(tmp_path):
    dev = EaFloodWarnings("ea-flood-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots({
        "items": [
            {
                "description": "Thames at Maidenhead",
                "severity": "Flood warning",
                "severityLevel": 2,
                "lat": 51.52,
                "long": -0.70,
                "floodArea": {"county": "Berkshire"},
            },
            {
                "description": "No longer in force",
                "severity": "Warning no longer in force",
                "severityLevel": 4,
                "lat": 51.5,
                "long": -0.1,
            },
        ]
    })
    assert len(hs) == 1
    assert hs[0]["severity_score"] == 80.0
    assert "England" in dev.source
    assert "SEPA" in dev.source


def test_ea_empty_is_offline(tmp_path):
    dev = EaFloodWarnings("ea-flood-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline, match="empty"):
        dev.collect_hotspots({"items": []})


def test_ptwc_sells_warning_not_information(tmp_path):
    dev = PtwcTsunamiAlerts("ptwc-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots(PTWC_ATOM)
    assert len(hs) == 1
    assert hs[0]["severity_score"] == 95.0
    assert hs[0]["latitude"] == 19.5
    assert "warning product" in dev.source.lower() or "not a tide gauge" in dev.source.lower()


def test_ptwc_information_only_is_offline(tmp_path):
    dev = PtwcTsunamiAlerts("ptwc-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline, match="no warning"):
        dev.collect_hotspots(PTWC_INFO_ONLY)


def test_kystverket_requires_token(tmp_path):
    with pytest.raises(ValueError, match="TOKEN|client"):
        KystverketAis("kystverket-ais-01", SimClock(realtime=True), key_dir=str(tmp_path))


def test_kystverket_ais_cluster_and_sog_sentinel(tmp_path):
    dev = KystverketAis(
        "kystverket-ais-01",
        SimClock(realtime=True),
        token="test-token",
        key_dir=str(tmp_path),
    )
    hs = dev.collect_hotspots({
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [5.3, 60.4]},
                "properties": {"mmsi": 257000001, "sog": 102.3, "cog": 360.0, "navStat": 0},
            },
            {
                "geometry": {"type": "Point", "coordinates": [5.4, 60.5]},
                "properties": {"mmsi": 257000002, "sog": 12.0, "cog": 45.0, "navStat": 0},
            },
        ]
    })
    by_mmsi = {h["mmsi"]: h for h in hs}
    assert by_mmsi["257000002"]["sog_knots"] == 12.0
    assert "sog_knots" not in by_mmsi["257000001"]
    assert "cog_deg" not in by_mmsi["257000001"]
    assert "NLOD" in dev.source
    assert "not Finnish" in dev.source.lower() or "Norwegian" in dev.source


def test_adsb_lol_odbl_and_unit_conversion(tmp_path):
    dev = AdsbLolTraffic("adsb-lol-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots({
        "ac": [
            {
                "hex": "40621d",
                "flight": "BAW123",
                "lat": 51.47,
                "lon": -0.45,
                "alt_baro": 32000,
                "gs": 420.0,
            },
            {
                "hex": "abc123",
                "lat": 51.48,
                "lon": -0.46,
                "alt_baro": "ground",
                "gs": 12.0,
            },
        ]
    })
    assert hs[0]["icao"] == "40621d"
    assert hs[0]["altitude_m"] == pytest.approx(32000 * 0.3048)
    assert hs[0]["speed_mps"] == pytest.approx(420.0 * 0.514444)
    assert hs[1]["altitude_m"] == 0.0
    assert "ODbL" in dev.source
    assert "api.adsb.lol" in dev.url
    assert "OpenSky" in dev.source


def test_adsb_lol_empty_is_offline(tmp_path):
    dev = AdsbLolTraffic("adsb-lol-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline, match="empty"):
        dev.collect_hotspots({"ac": []})


def test_adsb_lol_rejects_buyer_coords(tmp_path):
    with pytest.raises(ValueError):
        AdsbLolTraffic(
            "adsb-lol-01",
            SimClock(realtime=True),
            latitude=999.0,
            longitude=0.0,
            key_dir=str(tmp_path),
        )


def test_kystverket_uses_the_live_ais_service_not_the_retired_bwapi(tmp_path):
    """The bwapi geodata path answers 401 even for a valid ais-scoped token.

    That failure mode reads exactly like bad credentials, and it cost a real
    hunt for keys that were correct all along. Pin the working endpoint.
    """
    from gaia.devices.live_p3 import KystverketAis

    dev = KystverketAis(
        "kystverket-ais-01", SimClock(realtime=True),
        client_id="user@example.com:client", client_secret="x",
        key_dir=str(tmp_path),
    )
    assert dev.url == "https://live.ais.barentswatch.no/v1/latest/combined"
    assert "bwapi" not in dev.url
    assert "live.ais.barentswatch.no" in dev.source


def test_kystverket_parses_the_live_field_names(tmp_path):
    """The live service spells out speedOverGround / courseOverGround."""
    from gaia.devices.live_p3 import KystverketAis

    dev = KystverketAis(
        "kystverket-ais-01", SimClock(realtime=True),
        client_id="user@example.com:client", client_secret="x",
        key_dir=str(tmp_path),
    )
    hs = dev.collect_hotspots([
        {"latitude": 57.86, "longitude": 6.61, "speedOverGround": 11.1,
         "courseOverGround": 89.8, "navigationalStatus": 0, "mmsi": 311002018},
        {"latitude": 58.0, "longitude": 7.0, "speedOverGround": 3.0,
         "courseOverGround": 10.0, "navigationalStatus": 5, "mmsi": 2},
    ])
    top = hs[0]
    assert top["sog_knots"] == 11.1
    assert top["cog_deg"] == 89.8
    assert top["mmsi"] == "311002018"
    # navigationalStatus 0 is "under way using engine" — the commonest value.
    # An `or ""` chain used to erase it.
    assert top["nav_stat"] == "0", "status 0 must survive as a real value"
