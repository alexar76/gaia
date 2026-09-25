from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
import gzip
import tarfile

import h5py
import numpy as np
import pytest

from gaia.clock import SimClock
from gaia.devices.live_p4 import (
    CamsAirComposition,
    CopernicusSoilWaterIndex,
    CopernicusSentinel3LandTemperature,
    EpaRadNetStation,
    NoaaDartGauge,
    NoaaHmsSmoke,
    NoaaNexradStatus,
    NasaPowerSolar,
    NoaaNohrscSnow,
    NoaaNsidcSeaIceIndex,
    UsgsWaterQuality,
    parse_hms_smoke_kml,
    parse_imerg_hdf5,
    parse_ndbc_dart,
    parse_sentinel_statistics,
    parse_sentinel_statistics_bands,
    parse_nasa_power_daily,
    parse_snodas_tar,
    parse_nsidc_ice_geotiff,
)
from gaia.source_policy import require_approved_source


KML = """<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><description><![CDATA[<div>Start Time: 2026238 1200UTC<br>
End Time: 2026238 1500UTC<br>Density: Heavy<br>Satellite: GOES-WEST</div>]]></description>
<styleUrl>#Smoke_Heavy_style</styleUrl><Polygon><outerBoundaryIs><LinearRing>
<coordinates>-100,40,0 -98,40,0 -98,42,0 -100,42,0 -100,40,0</coordinates>
</LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>"""


def test_hms_parser_maps_polygon_to_qualitative_hotspot():
    rows = parse_hms_smoke_kml(KML)
    assert len(rows) == 1
    assert rows[0]["severity_score"] == 90.0
    assert rows[0]["density"] == "heavy"
    assert rows[0]["satellite"] == "GOES-WEST"
    assert 40.0 <= rows[0]["latitude"] <= 42.0
    assert -100.0 <= rows[0]["longitude"] <= -98.0
    assert rows[0]["geometry_type"] == "Polygon centroid"


def test_hms_read_is_signed_cluster(monkeypatch, tmp_path):
    dev = NoaaHmsSmoke("hms-smoke-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch_text", lambda *args, **kwargs: KML)
    out = dev.read()
    assert out["reading"]["hotspot_count"] == 1
    assert out["reading"]["values"]["severity_score"] == 90.0
    assert out["reading"]["attribution"].startswith("NOAA/NESDIS")
    assert out["attestation"]["value"]


# One outer ring with a hole, plus a second polygon that crosses the
# antimeridian — the two shapes a centroid cannot describe.
KML_GEOMETRY = """<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><description><![CDATA[<div>Start Time: 2026238 1200UTC<br>
End Time: 2026238 1500UTC<br>Density: Heavy<br>Satellite: GOES-WEST</div>]]></description>
<Polygon>
<outerBoundaryIs><LinearRing>
<coordinates>-125,35,0 -100,35,0 -100,45,0 -125,45,0 -125,35,0</coordinates>
</LinearRing></outerBoundaryIs>
<innerBoundaryIs><LinearRing>
<coordinates>-123,37,0 -121,37,0 -121,39,0 -123,39,0 -123,37,0</coordinates>
</LinearRing></innerBoundaryIs>
</Polygon></Placemark>
<Placemark><description><![CDATA[<div>Start Time: 2026238 1200UTC<br>
End Time: 2026238 1500UTC<br>Density: Light<br>Satellite: GOES-EAST</div>]]></description>
<Polygon><outerBoundaryIs><LinearRing>
<coordinates>179,50,0 181,50,0 181,52,0 179,52,0 179,50,0</coordinates>
</LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>"""


def test_hms_parser_keeps_the_full_ring_and_its_holes():
    """The centroid is a map anchor; coverage needs the ring itself."""
    rows = parse_hms_smoke_kml(KML_GEOMETRY)
    assert len(rows) == 2
    heavy = next(row for row in rows if row["density"] == "heavy")
    outer, hole = heavy["geometry"]["coordinates"]
    assert heavy["geometry"]["type"] == "Polygon"
    assert outer[0] == outer[-1] == [-125.0, 35.0]
    assert hole[0] == [-123.0, 37.0]
    assert heavy["vertex_count"] == len(outer) + len(hole)
    assert heavy["bbox"] == [-125.0, 35.0, -100.0, 45.0]
    # The centroid of this ring sits in the middle of the hole, i.e. on ground
    # the polygon explicitly does NOT cover.
    assert (heavy["latitude"], heavy["longitude"]) == (40.0, -112.5)
    assert heavy["geometry_type"] == "Polygon centroid"
    assert heavy["polygon_id"].startswith("hms-")
    assert len(heavy["geometry_digest"]) == 64


def test_hms_parser_wraps_longitudes_past_the_antimeridian():
    rows = parse_hms_smoke_kml(KML_GEOMETRY)
    light = next(row for row in rows if row["density"] == "light")
    ring = light["geometry"]["coordinates"][0]
    assert [point[0] for point in ring] == [179.0, -179.0, -179.0, 179.0, 179.0]
    # west > east is the RFC-7946 signal for an antimeridian-crossing bbox.
    west, south, east, north = light["bbox"]
    assert west == 179.0 and east == -179.0 and (south, north) == (50.0, 52.0)


def test_hms_polygon_ids_are_stable_and_distinct_per_polygon():
    first = parse_hms_smoke_kml(KML_GEOMETRY)
    again = parse_hms_smoke_kml(KML_GEOMETRY)
    assert [row["polygon_id"] for row in first] == [row["polygon_id"] for row in again]
    assert len({row["polygon_id"] for row in first}) == 2


def test_hms_reading_declares_a_complete_inventory(monkeypatch, tmp_path):
    dev = NoaaHmsSmoke("hms-smoke-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch_text", lambda *args, **kwargs: KML_GEOMETRY)
    reading = dev.read()["reading"]
    assert reading["inventory_total"] == 2
    assert reading["inventory_complete"] is True
    assert reading["hotspot_count"] == 2
    # Geometry survives the cluster projection instead of being flattened.
    assert reading["hotspots"][0]["geometry"]["coordinates"][0][0] == [-125.0, 35.0]
    assert reading["hotspots"][0]["bbox"] == [-125.0, 35.0, -100.0, 45.0]


def test_moving_one_polygon_vertex_breaks_the_attestation(monkeypatch, tmp_path):
    """The commercial claim: the ring is signed, not just its centroid.

    Signing only ``values`` would leave the geometry — the thing a buyer acts on
    — unattested and silently editable anywhere between GAIA and ATLAS.
    """
    from gaia.attestation import verify_reading

    dev = NoaaHmsSmoke("hms-smoke-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch_text", lambda *args, **kwargs: KML_GEOMETRY)
    out = dev.read()
    reading, attestation = out["reading"], out["attestation"]
    assert verify_reading(reading, attestation) is True
    assert "hotspots_sha256" in attestation["canonical"]

    tampered = json.loads(json.dumps(reading))
    tampered["hotspots"][0]["geometry"]["coordinates"][0][1][0] = -99.5
    assert verify_reading(tampered, attestation) is False

    dropped = json.loads(json.dumps(reading))
    dropped["hotspots"] = dropped["hotspots"][:1]
    assert verify_reading(dropped, attestation) is False

    untouched = json.loads(json.dumps(reading))
    assert verify_reading(untouched, attestation) is True


def test_hms_falls_back_to_the_previous_utc_day_product(monkeypatch, tmp_path):
    """Today's HMS product does not exist for most of the UTC day.

    NOAA writes one dated KML per UTC day and only once the first daytime analysis
    exists — at 06:00Z the current day 404s while the previous day is complete. Asking
    for today alone therefore left the whole layer, and the paid containment SKU on top
    of it, dark for a large part of every day.
    """
    from gaia.devices.base import DeviceOffline

    dev = NoaaHmsSmoke("hms-smoke-01", SimClock(realtime=True), key_dir=str(tmp_path))
    now = datetime.now(timezone.utc)
    today = f"hms_smoke{now:%Y%m%d}.kml"
    yesterday = f"hms_smoke{now - timedelta(days=1):%Y%m%d}.kml"
    asked: list[str] = []

    def _fetch(url, **kwargs):
        asked.append(url)
        if url.endswith(today):
            raise DeviceOffline("hms-smoke-01: upstream HTTP 404")
        return KML_GEOMETRY

    monkeypatch.setattr(dev, "_fetch_text", _fetch)
    reading = dev.read()["reading"]

    assert asked[0].endswith(today), "today must still be tried first"
    assert asked[1].endswith(yesterday)
    assert reading["hotspot_count"] == 2
    # The date travels with the reading and with every polygon, so nothing downstream
    # can present yesterday's analysis as a current satellite pass.
    assert reading["product_date"] == f"{now - timedelta(days=1):%Y-%m-%d}"
    assert reading["product_age_hours"] >= 24.0
    assert {row["product_date"] for row in reading["hotspots"]} == {reading["product_date"]}
    assert dev.url.endswith(yesterday)


def test_hms_refuses_when_the_whole_lookback_window_is_empty(monkeypatch, tmp_path):
    """Falling back is bounded: no product in the window is offline, not stale data."""
    from gaia.devices.base import DeviceOffline

    dev = NoaaHmsSmoke("hms-smoke-01", SimClock(realtime=True), key_dir=str(tmp_path))
    asked: list[str] = []

    def _fetch(url, **kwargs):
        asked.append(url)
        raise DeviceOffline("hms-smoke-01: upstream HTTP 404")

    monkeypatch.setattr(dev, "_fetch_text", _fetch)
    with pytest.raises(DeviceOffline):
        dev.read()
    assert len(asked) == NoaaHmsSmoke._MAX_LOOKBACK_DAYS + 1
    assert len(set(asked)) == len(asked), "each candidate day is asked once"


def test_hms_source_is_commercially_approved():
    policy = require_approved_source("noaa_hms_smoke")
    assert policy.licence == "U.S. Government public domain"
    policy.require_endpoint("https://www.ospo.noaa.gov/data/spl/kmlfiles/fire/hms_smoke20260826.kml")


def test_usgs_water_quality_selects_freshest_series(tmp_path):
    payload = {"features": [
        {"properties": {"parameter_code": "00400", "time": "2024-01-01T00:00:00Z", "value": "7.2"},
         "geometry": {"type": "Point", "coordinates": [-75.6, 39.7]}},
        {"properties": {"parameter_code": "00400", "time": "2026-08-27T12:36:00Z", "value": "7.1"},
         "geometry": {"type": "Point", "coordinates": [-75.6, 39.7]}},
        {"properties": {"parameter_code": "00010", "time": "2026-08-27T12:36:00Z", "value": "24.0"},
         "geometry": {"type": "Point", "coordinates": [-75.6, 39.7]}},
        {"properties": {"parameter_code": "00300", "time": "2026-08-27T12:36:00Z", "value": "4.9"},
         "geometry": {"type": "Point", "coordinates": [-75.6, 39.7]}},
        {"properties": {"parameter_code": "00095", "time": "2026-08-27T12:36:00Z", "value": "345"},
         "geometry": {"type": "Point", "coordinates": [-75.6, 39.7]}},
    ]}
    dev = UsgsWaterQuality("usgs-wq-01", SimClock(realtime=True), key_dir=str(tmp_path))
    assert dev.map(payload) == {
        "ph": 7.1, "water_temperature_c": 24.0, "dissolved_oxygen_mg_l": 4.9,
        "specific_conductance_us_cm": 345.0, "latitude": 39.7, "longitude": -75.6,
    }


def test_usgs_water_quality_source_is_commercially_approved():
    assert require_approved_source("usgs_water_quality").licence.startswith("U.S.")


def _registry_rows(station_ids):
    return {
        station_id: {
            "registry_id": f"USGS-{station_id}",
            "name": f"Registry station {station_id}",
            "agency_code": "USGS",
            "site_type": "Stream",
            "state_name": "Maryland",
            "county_name": "Example County",
            "country_name": "United States of America",
            "hydrologic_unit_code": "02070010",
            "registry_revision_modified": "2026-08-01T00:00:00Z",
        }
        for station_id in station_ids
    }


def _fresh_observed():
    return datetime.now(timezone.utc).isoformat()


def test_usgs_water_quality_bbox_returns_every_station_as_a_signed_point(monkeypatch, tmp_path):
    def payload(url):
        code = url.split("parameter_code=", 1)[1].split("&", 1)[0]
        value = {"00010": "18.5", "00400": "7.4", "00300": "8.1", "00095": "420"}[code]
        return {"features": [{
            "properties": {
                "monitoring_location_id": "USGS-01234567",
                "monitoring_location_name": "Example River",
                "parameter_code": code,
                "time": _fresh_observed(),
                "value": value,
                "approval_status": "Provisional",
                "qualifier": "P",
            },
            "geometry": {"type": "Point", "coordinates": [-77.1, 38.9]},
        }]}

    dev = UsgsWaterQuality("usgs-wq-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch", payload)
    monkeypatch.setattr(dev, "_station_registry", _registry_rows)
    dev.set_query(bbox=(-78.0, 38.0, -76.0, 40.0))
    out = dev.read()
    dev.clear_query()
    assert out["reading"]["hotspot_count"] == 1
    point = out["reading"]["hotspots"][0]
    assert point["station_id"] == "01234567"
    assert (point["latitude"], point["longitude"]) == (38.9, -77.1)
    assert point["ph"] == 7.4
    assert point["approval_status"] == "Provisional"
    assert out["attestation"]["value"]


def test_usgs_water_quality_bbox_splits_at_antimeridian(monkeypatch, tmp_path):
    urls: list[str] = []

    def payload(url):
        urls.append(url)
        eastern_half = "bbox=170.000000" in url
        code = url.split("parameter_code=", 1)[1].split("&", 1)[0]
        return {"features": [{
            "properties": {
                "monitoring_location_id": "USGS-11111111" if eastern_half else "USGS-22222222",
                "parameter_code": code,
                "time": _fresh_observed(),
                "value": "18.5",
            },
            "geometry": {
                "type": "Point",
                "coordinates": [175.0 if eastern_half else -175.0, 52.0],
            },
        }]}

    dev = UsgsWaterQuality("usgs-wq-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch", payload)
    monkeypatch.setattr(dev, "_station_registry", _registry_rows)
    dev.set_query(bbox=(170.0, 50.0, -170.0, 55.0))
    rows = dev._network_rows()

    assert len(urls) == 8  # four parameters × two non-wrapping OGC bboxes
    assert {row["station_id"] for row in rows} == {"11111111", "22222222"}
    assert {row["longitude"] for row in rows} == {175.0, -175.0}


def test_usgs_water_quality_drains_registry_pages_and_preserves_review_metadata(
    monkeypatch, tmp_path,
):
    calls: list[str] = []

    def payload(url):
        calls.append(url)
        code = url.split("parameter_code=", 1)[1].split("&", 1)[0]
        if "page=2" in url:
            station = "USGS-22222222"
            next_links = []
        else:
            station = "USGS-11111111"
            next_links = [{"rel": "next", "href": f"https://api.waterdata.usgs.gov/page=2&parameter_code={code}"}]
        return {
            "numberMatched": 2,
            "links": next_links,
            "features": [{
                "properties": {
                    "monitoring_location_id": station,
                    "monitoring_location_name": f"Station {station[-2:]}",
                    "parameter_code": code,
                    "time": _fresh_observed(),
                    "value": {"00010": "18.5", "00400": "7.4"}[code],
                    "approval_status": "Provisional" if code == "00400" else "Approved",
                    "qualifier": "Ice" if code == "00400" else "",
                    "unit_of_measure": "deg C" if code == "00010" else "std units",
                    "time_series_id": f"{station[-8:]}-{code}",
                },
                "geometry": {"type": "Point", "coordinates": [-77.1, 38.9]},
            }],
        }

    dev = UsgsWaterQuality("usgs-wq-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch", payload)
    monkeypatch.setattr(dev, "_station_registry", _registry_rows)
    dev.set_query(
        bbox=(-78.0, 38.0, -76.0, 40.0),
        limit=1,
        parameters=["water_temperature_c", "00400"],
        require_all=True,
    )
    rows = dev._network_rows()

    assert len(calls) == 4  # two requested parameters x two OGC pages
    assert [row["station_id"] for row in rows] == ["11111111", "22222222"]
    assert all(row["available_parameters"] == ["ph", "water_temperature_c"] for row in rows)
    assert all(row["approval_status"] == "Provisional" for row in rows)
    assert all(row["qualifiers"] == ["Ice"] for row in rows)
    ph_meta = rows[0]["observation_metadata"]["ph"]
    assert ph_meta["parameter_code"] == "00400"
    assert ph_meta["approval_status"] == "Provisional"
    assert ph_meta["qualifier"] == "Ice"


def test_usgs_water_quality_require_all_filters_incomplete_stations(monkeypatch, tmp_path):
    def payload(url):
        code = url.split("parameter_code=", 1)[1].split("&", 1)[0]
        station_ids = ["USGS-11111111", "USGS-22222222"] if code == "00400" else ["USGS-11111111"]
        return {"features": [{
            "properties": {
                "monitoring_location_id": station_id,
                "parameter_code": code,
                "time": _fresh_observed(),
                "value": "7.4" if code == "00400" else "18.5",
            },
            "geometry": {"type": "Point", "coordinates": [-77.1, 38.9]},
        } for station_id in station_ids]}

    dev = UsgsWaterQuality("usgs-wq-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch", payload)
    monkeypatch.setattr(dev, "_station_registry", _registry_rows)
    dev.set_query(
        bbox=(-78.0, 38.0, -76.0, 40.0),
        parameters=["ph", "water_temperature_c"],
        require_all=True,
    )
    assert [row["station_id"] for row in dev._network_rows()] == ["11111111"]


def test_usgs_water_quality_excludes_stale_latest_known_series(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)

    def payload(_url):
        return {"features": [{
            "properties": {
                "monitoring_location_id": f"USGS-{station_id}",
                "parameter_code": "00400",
                "time": observed,
                "value": "7.4",
            },
            "geometry": {"type": "Point", "coordinates": [-77.1, 38.9]},
        } for station_id, observed in (
            ("11111111", now.isoformat()),
            ("22222222", (now - timedelta(days=7)).isoformat()),
        )]}

    dev = UsgsWaterQuality("usgs-wq-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch", payload)
    monkeypatch.setattr(dev, "_station_registry", _registry_rows)
    dev.set_query(
        bbox=(-78.0, 38.0, -76.0, 40.0), parameters=["ph"], max_age_hours=48,
    )
    assert [row["station_id"] for row in dev._network_rows()] == ["11111111"]


def test_usgs_water_quality_refuses_silently_truncated_registry(monkeypatch, tmp_path):
    from gaia.devices.base import DeviceOffline

    dev = UsgsWaterQuality("usgs-wq-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch", lambda _url: {"numberMatched": 2, "features": [{}]})
    dev.set_query(bbox=(-78.0, 38.0, -76.0, 40.0), parameters=["ph"])
    with pytest.raises(DeviceOffline, match="registry truncated"):
        dev._network_rows()


def test_usgs_water_quality_imports_official_station_registry_in_batches(monkeypatch, tmp_path):
    seen: list[str] = []

    def payload(url):
        seen.append(url)
        return {
            "numberMatched": None,
            "numberReturned": 2,
            "features": [{
                "id": f"USGS-{station_id}",
                "properties": {
                    "id": f"USGS-{station_id}",
                    "agency_code": "USGS",
                    "monitoring_location_name": f"River {station_id}",
                    "site_type": "Stream",
                    "state_name": "Maryland",
                    "county_name": "Example County",
                    "country_name": "United States of America",
                    "hydrologic_unit_code": "02070010",
                    "revision_modified": "2026-08-01T00:00:00Z",
                },
                "geometry": {"type": "Point", "coordinates": [-77.1, 38.9]},
            } for station_id in ("11111111", "22222222")],
        }

    dev = UsgsWaterQuality("usgs-wq-01", SimClock(realtime=True), key_dir=str(tmp_path))
    monkeypatch.setattr(dev, "_fetch", payload)
    registry = dev._station_registry(["22222222", "11111111"])

    assert len(seen) == 1
    assert "monitoring-locations/items" in seen[0]
    assert "filter=id%20IN%20%28" in seen[0]
    assert registry["11111111"]["name"] == "River 11111111"
    assert registry["11111111"]["site_type"] == "Stream"
    assert registry["11111111"]["latitude"] == 38.9


def test_ndbc_dart_parser_uses_newest_row_and_skips_fill():
    text = """#YY MM DD hh mm ss T HEIGHT
2026 08 27 12 00 00 1 3265.748
2026 08 27 11 45 00 1 3265.832
"""
    assert parse_ndbc_dart(text) == {
        "water_column_height_m": 3265.748,
        "measurement_type": 1.0,
    }


def test_ndbc_dart_device_and_policy(tmp_path):
    dev = NoaaDartGauge("noaa-dart-01", SimClock(realtime=True), key_dir=str(tmp_path))
    assert dev.url.endswith("/46407.dart")
    assert require_approved_source("noaa_dart").licence.startswith("U.S.")


def test_imerg_hdf5_returns_one_observation_per_coordinate():
    buf = BytesIO()
    with h5py.File(buf, "w") as doc:
        grid = doc.create_group("Grid")
        grid.create_dataset("lat", data=np.array([10.0, 20.0], dtype="f4"))
        grid.create_dataset("lon", data=np.array([30.0, 40.0], dtype="f4"))
        grid.create_dataset(
            "precipitation",
            data=np.array([[[1.0, 2.0], [3.0, 4.0]]], dtype="f4"),
        )
    rows = parse_imerg_hdf5(buf.getvalue(), points=(("a", 10.0, 30.0), ("b", 20.0, 40.0)))
    assert [(row["anchor"], row["latitude"], row["longitude"]) for row in rows] == [
        ("a", 10.0, 30.0), ("b", 20.0, 40.0),
    ]
    assert [row["precipitation_mm_h"] for row in rows] == [1.0, 4.0]


def test_imerg_policy_requires_operator_account():
    policy = require_approved_source("nasa_imerg")
    assert policy.requires_operator_account
    assert "commercial" in policy.commercial_basis.lower()


def test_imerg_accepts_any_query_coordinate(tmp_path):
    from gaia.devices.live_p4 import NasaImergPrecipitation

    dev = NasaImergPrecipitation(
        "imerg-01", SimClock(realtime=True), token="token", key_dir=str(tmp_path),
    )
    dev.set_coordinate(51.5, -0.12)
    assert dev._query_points == (("query", 51.5, -0.12),)
    dev.clear_coordinate()
    assert dev._query_points is None


def test_nexrad_status_keeps_one_radar_per_coordinate(tmp_path):
    payload = {"features": [{
        "type": "Feature", "geometry": {"type": "Point", "coordinates": [-94.36, 35.29]},
        "properties": {
            "id": "KSRX", "name": "Western Arkansas", "stationType": "WSR-88D",
            "latency": {"current": {"value": 0.25}},
            "rda": {"properties": {
                "status": "Operate", "operabilityStatus": "RDA - On-line",
                "volumeCoveragePattern": "R35",
                "averageTransmitterPower": {"value": 1008},
                "reflectivityCalibrationCorrection": {"value": -0.12},
            }},
        },
    }]}
    dev = NoaaNexradStatus("nexrad-status-01", SimClock(realtime=True), key_dir=str(tmp_path))
    rows = dev.collect_hotspots(payload)
    assert len(rows) == 1
    assert (rows[0]["radar_id"], rows[0]["latitude"], rows[0]["longitude"]) == (
        "KSRX", 35.29, -94.36,
    )
    assert rows[0]["radar_latency_s"] == 0.25


def test_nexrad_status_source_is_commercially_approved():
    assert require_approved_source("noaa_nexrad_status").licence.startswith("U.S.")


def test_cams_point_maps_one_coordinate(monkeypatch, tmp_path):
    monkeypatch.delenv("GAIA_PAYMENTS_ENABLED", raising=False)
    dev = CamsAirComposition(
        "cams-berlin", SimClock(realtime=True), latitude=52.52, longitude=13.405,
        key_dir=str(tmp_path),
    )
    values = dev.map({"current": {
        "aerosol_optical_depth": 0.11, "dust": 2.0, "alder_pollen": 0.0,
        "birch_pollen": 1.0, "grass_pollen": 0.6,
    }})
    assert (values["latitude"], values["longitude"]) == (52.52, 13.405)
    assert values["dust_ugm3"] == 2.0
    dev.set_coordinate(35.6762, 139.6503)
    assert "latitude=35.67620" in dev.url and "longitude=139.65030" in dev.url
    dev.clear_coordinate()
    assert (dev.latitude, dev.longitude) == (52.52, 13.405)


def test_radnet_parser_uses_latest_approved_row(tmp_path):
    text = """LOCATION_NAME,SAMPLE COLLECTION TIME,DOSE EQUIVALENT RATE (nSv/h),GAMMA COUNT RATE R02 (CPM),GAMMA COUNT RATE R03 (CPM),GAMMA COUNT RATE R04 (CPM),GAMMA COUNT RATE R05 (CPM),GAMMA COUNT RATE R06 (CPM),GAMMA COUNT RATE R07 (CPM),GAMMA COUNT RATE R08 (CPM),GAMMA COUNT RATE R09 (CPM),STATUS
AL: BIRMINGHAM,08/27/2026 11:30:00,54,1,2,3,4,5,6,7,8,APPROVED
AL: BIRMINGHAM,08/27/2026 12:30:00,55,10,20,30,40,50,60,70,80,APPROVED
"""
    dev = EpaRadNetStation(
        "radnet-birmingham", SimClock(realtime=True), state="AL", city="BIRMINGHAM",
        latitude=33.5186, longitude=-86.8104, key_dir=str(tmp_path),
    )
    assert dev.map_csv(text) == {
        "dose_equivalent_nsv_h": 55.0, "gamma_count_total_cpm": 360.0,
        "latitude": 33.5186, "longitude": -86.8104,
    }
    assert require_approved_source("epa_radnet").licence.startswith("U.S.")


def test_sentinel_statistics_and_soil_policy():
    payload = {"data": [{"outputs": {"data": {"bands": {"B0": {"stats": {"mean": 47.5}}}}}}]}
    assert parse_sentinel_statistics(payload) == 47.5
    policy = require_approved_source("copernicus_clms_swi")
    assert policy.requires_operator_account
    assert "any purpose" in policy.commercial_basis


def test_soil_device_is_one_configured_coordinate(tmp_path):
    dev = CopernicusSoilWaterIndex(
        "soil-berlin", SimClock(realtime=True), client_id="id", client_secret="secret",
        latitude=52.52, longitude=13.405, key_dir=str(tmp_path),
    )
    assert (dev.latitude, dev.longitude) == (52.52, 13.405)
    dev.set_coordinate(-1.2921, 36.8219)
    assert (dev.latitude, dev.longitude) == (-1.2921, 36.8219)
    dev.clear_coordinate()
    assert (dev.latitude, dev.longitude) == (52.52, 13.405)


def test_nasa_power_uses_latest_non_fill_day_and_one_coordinate(tmp_path):
    payload = {"properties": {"parameter": {
        "ALLSKY_SFC_SW_DWN": {"20260825": 4.2, "20260826": -999.0},
        "CLRSKY_SFC_SW_DWN": {"20260825": 6.1, "20260826": -999.0},
    }}}
    assert parse_nasa_power_daily(payload) == {
        "solar_irradiation_kwh_m2_day": 4.2,
        "clear_sky_irradiation_kwh_m2_day": 6.1,
        "solar_observation_yyyymmdd": 20260825.0,
    }
    dev = NasaPowerSolar(
        "solar-berlin", SimClock(realtime=True), latitude=52.52, longitude=13.405,
        key_dir=str(tmp_path),
    )
    values = dev.map(payload)
    assert (values["latitude"], values["longitude"]) == (52.52, 13.405)
    payload["geometry"] = {"type": "Point", "coordinates": [13.0, 53.0]}
    values = dev.map(payload)
    assert (values["latitude"], values["longitude"]) == (53.0, 13.0)
    assert require_approved_source("nasa_power_solar").licence.startswith("NASA open")


def test_snodas_archive_maps_documented_scaled_cell_coordinate(tmp_path):
    header = """Data units: Meters / 1000.000000
Number of columns: 2
Number of rows: 2
Benchmark x-axis coordinate: 10.0
Benchmark y-axis coordinate: 20.0
X-axis resolution: 0.01
Y-axis resolution: 0.01
""".encode()
    archive_buf = BytesIO()
    with tarfile.open(fileobj=archive_buf, mode="w") as archive:
        for code, values in (
            ("11036", np.array([[685, 0], [0, 0]], dtype=">i2")),
            ("11034", np.array([[123, 0], [0, 0]], dtype=">i2")),
        ):
            for suffix, content in (("txt.gz", gzip.compress(header)),
                                    ("dat.gz", gzip.compress(values.tobytes()))):
                info = tarfile.TarInfo(f"us_ssmv{code}tS__sample.{suffix}")
                info.size = len(content)
                archive.addfile(info, BytesIO(content))
    rows = parse_snodas_tar(
        archive_buf.getvalue(), points=(("cell-a", 20.0, 10.0),)
    )
    assert rows == [{
        "snow_depth_cm": 68.5,
        "snow_water_equivalent_cm": 12.3,
        "latitude": 20.0, "longitude": 10.0, "anchor": "cell-a",
    }]
    dev = NoaaNohrscSnow(
        "snow-rainier", SimClock(realtime=True), latitude=46.8523, longitude=-121.7603,
        key_dir=str(tmp_path),
    )
    assert (dev.latitude, dev.longitude) == (46.8523, -121.7603)
    dev.set_coordinate(39.17, -120.14)
    assert (dev.latitude, dev.longitude) == (39.17, -120.14)
    dev.clear_coordinate()
    assert (dev.latitude, dev.longitude) == (46.8523, -121.7603)
    assert NoaaNohrscSnow._latest_name('<a href="SNODAS_20260827.tar">x</a>') == (
        "SNODAS_20260827.tar", "20260827",
    )
    assert require_approved_source("noaa_nohrsc_snow").licence.startswith("U.S.")


def test_nsidc_geotiff_uses_exact_cell_centres_and_filters_land():
    import tifffile
    from pyproj import Transformer

    wanted_lat, wanted_lon = 74.0, -145.0
    x, y = Transformer.from_crs(4326, 3411, always_xy=True).transform(wanted_lon, wanted_lat)
    buf = BytesIO()
    tifffile.imwrite(
        buf, np.array([[803, 2540], [0, 500]], dtype="u2"),
        extratags=[
            (33550, "d", 3, (25000.0, 25000.0, 0.0), False),
            (33922, "d", 6, (0.0, 0.0, 0.0, x-12500.0, y+12500.0, 0.0), False),
        ],
    )
    rows = parse_nsidc_ice_geotiff(buf.getvalue(), points=(("beaufort", wanted_lat, wanted_lon),))
    assert len(rows) == 1
    assert rows[0]["sea_ice_concentration_pct"] == 80.3
    assert abs(rows[0]["latitude"] - wanted_lat) < 0.01
    assert abs(rows[0]["longitude"] - wanted_lon) < 0.01
    assert require_approved_source("noaa_nsidc_sea_ice").licence.startswith("U.S.")


def test_nsidc_latest_index_name():
    html = '<a href="N_20260825_concentration_v4.0.tif">x</a><a href="N_20260826_concentration_v4.0.tif">y</a>'
    assert NoaaNsidcSeaIceIndex._latest_name(html) == (
        "N_20260826_concentration_v4.0.tif", "20260826",
    )


def test_nsidc_accepts_any_arctic_query_coordinate(tmp_path):
    dev = NoaaNsidcSeaIceIndex(
        "nsidc-ice-01", SimClock(realtime=True), key_dir=str(tmp_path),
    )
    dev.set_coordinate(80.0, 25.0)
    assert dev._query_points == (("query", 80.0, 25.0),)
    dev.clear_coordinate()
    assert dev._query_points is None


def test_sentinel3_lst_stats_and_one_coordinate(tmp_path):
    payload = {"data": [{"outputs": {"data": {"bands": {
        "B0": {"stats": {"mean": 31.25}},
        "B1": {"stats": {"mean": 1.4}},
    }}}}]}
    assert parse_sentinel_statistics_bands(payload) == {"B0": 31.25, "B1": 1.4}
    dev = CopernicusSentinel3LandTemperature(
        "lst-berlin", SimClock(realtime=True), client_id="id", client_secret="secret",
        latitude=52.52, longitude=13.405, key_dir=str(tmp_path),
    )
    assert (dev.latitude, dev.longitude) == (52.52, 13.405)
    policy = require_approved_source("copernicus_s3_lst")
    assert policy.requires_operator_account
    assert "commercial" in policy.commercial_basis.lower()
