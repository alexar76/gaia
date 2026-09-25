"""P2 relay mappers — no upstream HTTP."""

from __future__ import annotations

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p2 import (
    EcccHydrometric,
    FintrafficAis,
    FmiWeather,
    NwsTsunamiAlerts,
    SmhiHydrology,
)
from gaia.source_policy import require_approved_source

FMI_XML = """<?xml version="1.0"?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"
    xmlns:BsWfs="http://xml.fmi.fi/schema/wfs/2.0"
    xmlns:gml="http://www.opengis.net/gml/3.2">
  <wfs:member>
    <BsWfs:BsWfsElement>
      <BsWfs:Location><gml:Point><gml:pos>60.17523 24.94459</gml:pos></gml:Point></BsWfs:Location>
      <BsWfs:ParameterName>t2m</BsWfs:ParameterName>
      <BsWfs:ParameterValue>19.4</BsWfs:ParameterValue>
      <BsWfs:ParameterName>ws_10min</BsWfs:ParameterName>
      <BsWfs:ParameterValue>4.0</BsWfs:ParameterValue>
      <BsWfs:ParameterName>rh</BsWfs:ParameterName>
      <BsWfs:ParameterValue>50.0</BsWfs:ParameterValue>
      <BsWfs:ParameterName>p_sea</BsWfs:ParameterName>
      <BsWfs:ParameterValue>1025.7</BsWfs:ParameterValue>
    </BsWfs:BsWfsElement>
  </wfs:member>
</wfs:FeatureCollection>
"""


def test_source_policies_are_commercial(tmp_path):
    assert require_approved_source("fintraffic_ais").licence == "CC BY 4.0"
    assert "commercial" in require_approved_source("eccc_hydrometric").commercial_basis.lower()
    assert require_approved_source("fmi_opendata").licence == "CC BY 4.0"
    assert require_approved_source("smhi_hydro").licence == "CC BY 4.0"


def test_fintraffic_ais_cluster(tmp_path):
    dev = FintrafficAis("fintraffic-ais-01", SimClock(realtime=True), key_dir=str(tmp_path))
    payload = {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [24.9, 60.1]},
                "properties": {"mmsi": 230091290, "sog": 12.5, "cog": 90.0, "navStat": 0},
            },
            {
                "geometry": {"type": "Point", "coordinates": [25.0, 60.2]},
                "properties": {"mmsi": 230000001, "sog": 0.0, "cog": 10.0, "navStat": 5},
            },
        ]
    }
    hs = dev.collect_hotspots(payload)
    assert hs[0]["sog_knots"] == 12.5
    assert hs[0]["mmsi"] == "230091290"
    assert hs[0]["latitude"] == 60.1


def test_fintraffic_ais_treats_102_3_sog_as_unavailable(tmp_path):
    """A sentinel must vanish from the artifact, not become 0.0 kn / due north."""
    dev = FintrafficAis("fintraffic-ais-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots({
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [24.9, 60.1]},
                "properties": {"mmsi": 1, "sog": 102.3, "cog": 360.0, "navStat": 0},
            },
            {
                "geometry": {"type": "Point", "coordinates": [25.0, 60.2]},
                "properties": {"mmsi": 2, "sog": 8.0, "cog": 45.0, "navStat": 0},
            },
        ]
    })
    assert hs[0]["mmsi"] == "2"
    assert hs[0]["sog_knots"] == 8.0
    n_a = [h for h in hs if h["mmsi"] == "1"][0]
    assert "sog_knots" not in n_a, "unavailable SOG must be omitted, not sold as 0.0 kn"
    assert "cog_deg" not in n_a, "unavailable COG must be omitted, not sold as due north"
    assert n_a["latitude"] == 60.2 or n_a["latitude"] == 60.1
    assert "not global" in dev.source.lower() or "Finnish waters" in dev.source
    assert "own-edge" in dev.source.lower() or "own-edge" in FintrafficAis.source.lower()


def test_fintraffic_drops_implausible_speeds_but_keeps_the_vessel(tmp_path):
    """The live Baltic snapshot always carries a few broken encoders.

    The cluster is sorted by SOG, so admitting them makes the attested headline
    the worst transmitter in the feed every single time. Position is still good.
    """
    dev = FintrafficAis("fintraffic-ais-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots({
        "features": [
            # 102.2 = AIS "or higher" bucket; 90.2 = in-range but no ship does that.
            {
                "geometry": {"type": "Point", "coordinates": [24.9, 60.1]},
                "properties": {"mmsi": 7, "sog": 102.2, "cog": 90.0, "navStat": 0},
            },
            {
                "geometry": {"type": "Point", "coordinates": [25.1, 60.3]},
                "properties": {"mmsi": 8, "sog": 90.2, "cog": 100.0, "navStat": 0},
            },
            {
                "geometry": {"type": "Point", "coordinates": [25.0, 60.2]},
                "properties": {"mmsi": 9, "sog": 21.4, "cog": 45.0, "navStat": 0},
            },
        ]
    })
    assert hs[0]["mmsi"] == "9", "the fastest PLAUSIBLE vessel must lead the cluster"
    assert hs[0]["sog_knots"] == 21.4
    broken = {h["mmsi"]: h for h in hs if h["mmsi"] in ("7", "8")}
    assert len(broken) == 2, "a bad speed must not delete the vessel's position"
    for row in broken.values():
        assert "sog_knots" not in row
        assert row["latitude"] and row["longitude"]


def test_fintraffic_headline_stays_inside_the_plausibility_envelope(tmp_path):
    """Whatever we attest must pass GAIA's own physics table."""
    from gaia.plausibility import PHYSICS

    dev = FintrafficAis("fintraffic-ais-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hs = dev.collect_hotspots({
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [24.9, 60.1]},
                "properties": {"mmsi": 7, "sog": 102.2, "cog": 90.0, "navStat": 0},
            },
            {
                "geometry": {"type": "Point", "coordinates": [25.0, 60.2]},
                "properties": {"mmsi": 9, "sog": 30.0, "cog": 45.0, "navStat": 0},
            },
        ]
    })
    assert hs[0]["sog_knots"] <= PHYSICS["sog_knots"].hi


def test_fintraffic_sentinel_never_reaches_the_signed_cluster(tmp_path):
    """End-to-end: the attested payload must not carry a fabricated 0.0."""
    dev = FintrafficAis("fintraffic-ais-01", SimClock(realtime=True), key_dir=str(tmp_path))
    hotspots = dev.collect_hotspots({
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [25.0, 60.2]},
                "properties": {"mmsi": 2, "sog": 8.0, "cog": 45.0, "navStat": 0},
            },
            {
                "geometry": {"type": "Point", "coordinates": [24.9, 60.1]},
                "properties": {"mmsi": 1, "sog": 102.3, "cog": 360.0, "navStat": 0},
            },
        ]
    })
    from gaia.devices.live_p0 import signed_cluster_read

    out = signed_cluster_read(
        dev, hotspots,
        numeric_keys=("sog_knots", "cog_deg", "latitude", "longitude"),
        meta_keys=("mmsi", "nav_stat"),
    )
    row = [h for h in out["reading"]["hotspots"] if h["mmsi"] == "1"][0]
    assert "sog_knots" not in row and "cog_deg" not in row
    assert out["reading"]["attribution"].startswith("Fintraffic")


def test_fintraffic_empty_is_offline(tmp_path):
    dev = FintrafficAis("fintraffic-ais-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots({"features": []})


def test_eccc_hydrometric_map(tmp_path):
    dev = EcccHydrometric("eccc-hydro-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map({
        "features": [{
            "geometry": {"type": "Point", "coordinates": [-79.52, 43.70]},
            "properties": {"DISCHARGE": 6.04, "LEVEL": 2.33, "STATION_NUMBER": "02HC003"},
        }]
    })
    assert mapped["discharge_m3s"] == 6.04
    assert mapped["gage_height_m"] == 2.33
    assert mapped["latitude"] == 43.70


def test_eccc_geodetic_level_is_accepted(tmp_path):
    dev = EcccHydrometric("eccc-hydro-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map({
        "features": [{
            "geometry": {"type": "Point", "coordinates": [-75.8, 45.4]},
            "properties": {"DISCHARGE": 934.0, "LEVEL": 58.104},
        }]
    })
    assert mapped["gage_height_m"] == 58.104


def test_fmi_weather_xml(tmp_path):
    dev = FmiWeather("fmi-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map(FMI_XML)
    assert mapped["temperature_c"] == 19.4
    assert mapped["wind_mps"] == 4.0
    assert mapped["humidity_pct"] == 50.0
    assert mapped["pressure_hpa"] == 1025.7
    assert mapped["latitude"] == 60.17523


def test_nws_tsunami_is_cap_not_gauge(tmp_path):
    dev = NwsTsunamiAlerts("nws-tsunami-01", SimClock(realtime=True), key_dir=str(tmp_path))
    assert "code=TSW" in dev.url
    assert "tide gauge" in dev.source.lower() or "not a tide" in dev.source.lower()
    payload = {
        "features": [{
            "geometry": {"type": "Point", "coordinates": [-155.0, 19.5]},
            "properties": {
                "severity": "Extreme",
                "event": "Tsunami Warning",
                "headline": "Tsunami Warning",
                "areaDesc": "HI",
            },
        }]
    }
    hs = dev.collect_hotspots(payload)
    assert hs[0]["latitude"] == 19.5
    assert hs[0]["event"] == "Tsunami Warning"


def test_nws_tsunami_empty_offline(tmp_path):
    dev = NwsTsunamiAlerts("nws-tsunami-01", SimClock(realtime=True), key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline):
        dev.collect_hotspots({"features": []})


def test_smhi_discharge(tmp_path):
    dev = SmhiHydrology("smhi-hydro-01", SimClock(realtime=True), key_dir=str(tmp_path))
    mapped = dev.map({
        "parameter": {"key": "2", "name": "Vattenföring (15 min)", "unit": "m³/s"},
        "station": {"name": "ABISKO"},
        "position": [{"latitude": 68.1936, "longitude": 19.9859}],
        "value": [{"date": 1, "value": 12.3, "quality": "O"}],
    })
    assert mapped["discharge_m3s"] == 12.3
    assert mapped["latitude"] == 68.1936
