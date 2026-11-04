"""Provider geometry, source freshness and scoped signatures for infrastructure relays."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
import gzip
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_wave_infra as infra
from gaia.devices.base import DeviceOffline

NOW = datetime(2026, 11, 4, 12, 48, 0, tzinfo=UTC)


class FixedTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def devices(monkeypatch, tmp_path):
    monkeypatch.setattr(infra, "datetime", FixedTime)
    clock = SimClock(realtime=True)
    return (
        infra.NdwEvCharging("ndw-ev-charging-01", clock, key_dir=str(tmp_path)),
        infra.BcWildfireService("bc-wildfire-01", clock, key_dir=str(tmp_path)),
    )


def feature_collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def ndw(station, lon=5.15, lat=52.09, *, updated=NOW - timedelta(days=1), available=0):
    return {
        "type": "Feature", "id": station,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "country": "NLD", "address": "Utrecht station", "operator_name": "Sample operator",
            "last_updated": updated.isoformat(), "owner_name": "excluded owner",
            "availabilities": [
                {"total": 2, "available": available, "connector_type": "IEC_62196_T2",
                 "power_type": "AC3", "connector_format": "SOCKET", "power_max": 11040,
                 "tariff_ids": ["unjoined-tariff"]},
            ],
        },
    }


def verified(result):
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    return reading


def test_ndw_native_pilot_uses_complete_bbox_and_preserves_zero(devices, monkeypatch):
    charger, _ = devices
    headers = httpx.Headers({"Date": format_datetime(NOW, usegmt=True), "X-Total-Count": "1"})

    def fetch(url, *, max_bytes):
        assert url.startswith(charger.api_url)
        return feature_collection(ndw("NL-SAMPLE-1")), headers

    monkeypatch.setattr(charger, "_json", fetch)
    r = verified(charger.read())
    [site] = r["hotspots"]
    assert r["coverage_complete"] and r["values"]["coverage_complete"] == 1
    assert r["next_offset"] is None and r["values"]["next_offset"] == -1
    assert r["values"]["bbox_west_deg"] == infra._NDW_PILOT[0]
    assert r["source_published_at"] == NOW.isoformat()
    assert site["valid_until"] == (NOW + timedelta(minutes=4)).isoformat()
    assert site["station_id"] == "NL-SAMPLE-1" and site["station_key"] == "NL-SAMPLE-1"
    assert site["observed_at"] == (NOW - timedelta(days=1)).isoformat()
    assert site["available_count"] == 0 and site["connector_count"] == 2 and site["max_power_kw"] == 11.04
    assert site["connectors"] == [
        {"type": "IEC_62196_T2", "format": "SOCKET", "power_type": "AC3", "total": 2, "available": 0, "power_kw": 11.04},
    ]


def test_ndw_bulk_fallback_reuses_version_and_paginates_valid_rows(devices, monkeypatch):
    charger, _ = devices
    features = feature_collection(
        ndw("NL-SAMPLE-2", available=1),
        ndw("NL-SAMPLE*1", available=2),
        ndw("NL-SAMPLE-3", lon=4.5),
        ndw("NL-SAMPLE-4", updated=NOW + timedelta(hours=1)),
    )
    headers = httpx.Headers({"Last-Modified": format_datetime(NOW - timedelta(seconds=30), usegmt=True)})
    calls = []

    def fetch(url, *, max_bytes):
        calls.append(url)
        return gzip.compress(json.dumps(features).encode()), headers

    monkeypatch.setattr(charger, "_body", fetch)
    monkeypatch.setattr(charger, "_json", lambda *args, **kwargs: (
        feature_collection(ndw("NL-SAMPLE-2")),
        httpx.Headers({"X-Total-Count": "2", "Link": "<next>; rel=next"}),
    ))
    pilot = verified(charger.read())
    assert pilot["inventory_total"] == 2 and pilot["coverage_complete"]
    bbox = 5.1, 52.0, 5.2, 52.2
    first = verified(charger.read_bbox(*bbox, limit=1))
    assert first["inventory_total"] == 2 and first["next_offset"] == 1 and not first["coverage_complete"]
    assert first["hotspots"][0]["station_id"] == "NL-SAMPLE*1"
    assert len(first["hotspots"][0]["station_key"]) == 32
    second = verified(charger.read_bbox(*bbox, limit=1, offset=1, snapshot_unix=first["snapshot_unix"]))
    assert second["hotspots"][0]["station_id"] == "NL-SAMPLE-2"
    assert second["next_offset"] is None and not second["coverage_complete"]
    with pytest.raises(ValueError, match="snapshot changed"):
        charger.read_bbox(*bbox, limit=1, offset=1, snapshot_unix=first["snapshot_unix"] - 1)
    with pytest.raises(ValueError, match="required"):
        charger.read_bbox(*bbox, limit=1, offset=1)
    with pytest.raises(ValueError, match="4 square degrees"):
        charger.read_bbox(1, 51, 8, 54)
    with pytest.raises(ValueError, match="finite"):
        charger.read_bbox(float("nan"), 51, 8, 54)
    assert calls == [charger.url]


def test_ndw_rejects_stale_bulk(devices, monkeypatch):
    charger, _ = devices
    headers = httpx.Headers({"Last-Modified": format_datetime(NOW - timedelta(minutes=5), usegmt=True)})
    monkeypatch.setattr(charger, "_body", lambda *a, **k: (gzip.compress(b'{}'), headers))
    with pytest.raises(DeviceOffline, match="stale NDW"):
        charger.read_bbox(5.1, 52.0, 5.2, 52.2)


def ms(time):
    return int(time.timestamp() * 1000)


def fire(oid=1, name="C10001", **kwargs):
    properties = {
        "OBJECTID": oid, "FIRE_NUMBER": name, "FIRE_YEAR": 2026, "CURRENT_SIZE": 7.9,
        "IGNITION_DATE": ms(NOW - timedelta(days=8)), "FIRE_OUT_DATE": None,
        "FIRE_STATUS": "Under Control", "INCIDENT_NAME": "Sample Creek",
        **kwargs,
    }
    return {"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": [-123.9, 53.1]}}


def perimeter(oid=1, *, track_days=3, version=10):
    return {
        "type": "Feature",
        "properties": {
            "OBJECTID": oid, "FIRE_NUMBER": "C10001", "FIRE_YEAR": 2026, "VERSION_NUMBER": version,
            "TRACK_DATE": ms(NOW - timedelta(days=track_days)), "FIRE_SIZE_HECTARES": 8.1,
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-123.8, 53.2], [-123.7, 53.2], [-123.7, 53.3], [-123.8, 53.2]]],
        },
    }


def test_bc_preserves_independent_incident_and_perimeter_dates(devices, monkeypatch):
    _, bc = devices
    point_stamp = NOW - timedelta(minutes=2)
    perimeter_stamp = NOW - timedelta(minutes=3)
    point_data = feature_collection(
        fire(), fire(2, "C10002"), fire(3, "C10003", FIRE_OUT_DATE=ms(NOW)), fire(4, "C10004", IGNITION_DATE=ms(NOW + timedelta(days=1))),
    )
    perimeter_data = feature_collection(perimeter(2, track_days=4, version=1), perimeter(), perimeter(3, track_days=-1, version=11))

    def fetch(url, *, max_bytes=0):
        path = urlsplit(url).path
        polygon_layer = "FirePerimeters" in path
        if path.endswith("/query"):
            params = parse_qs(urlsplit(url).query)
            assert params["outSR"] == ["4326"]
            assert "Under Control" in params["where"][0]
            assert ("maxAllowableOffset" in params) == polygon_layer
            return (perimeter_data if polygon_layer else point_data), httpx.Headers()
        stamp = perimeter_stamp if polygon_layer else point_stamp
        return {"editingInfo": {"dataLastEditDate": ms(stamp)}}, httpx.Headers()

    monkeypatch.setattr(bc, "_json", fetch)
    reading = verified(bc.read())
    poly, point = reading["hotspots"]
    assert reading["inventory_total"] == 2 and reading["coverage_complete"]
    assert reading["source_published_at"] == perimeter_stamp.isoformat()
    assert poly["event_id"] == "2026-C10001" and poly["status"] == "Under Control"
    assert poly["geometry"]["type"] == "Polygon" and poly["latitude"] == 53.1
    assert poly["perimeter_observed_at"] == (NOW - timedelta(days=3)).isoformat()
    assert poly["perimeter_published_unix"] == perimeter_stamp.timestamp()
    assert poly["area_ha"] == 7.9 and poly["perimeter_area_ha"] == 8.1
    assert point["geometry"]["type"] == "Point" and "perimeter_observed_at" not in point
    scoped = verified(bc.read_bbox(-123.85, 53.18, -123.75, 53.25))
    assert [row["event_id"] for row in scoped["hotspots"]] == ["2026-C10001"]


def test_bc_rejects_duplicate_arcgis_page_ids(devices, monkeypatch):
    _, bc = devices
    monkeypatch.setattr(bc, "_json", lambda *a, **k: (
        feature_collection(fire(), fire()), httpx.Headers(),
    ))
    with pytest.raises(ValueError, match="duplicate ArcGIS"):
        bc._layer_features(bc.url, where="1=1", fields="OBJECTID", perimeters=False)


def test_bc_signs_empty_authoritative_scope(devices, monkeypatch):
    _, bc = devices
    monkeypatch.setattr(bc, "_snapshot", lambda: (NOW, []))
    r = verified(bc.read())
    assert r["coverage_complete"] and r["hotspots"] == []
    assert r["values"]["wildfire_incident_count"] == 0
    assert r["values"]["source_expires_unix"] == (NOW + timedelta(hours=1)).timestamp()
