"""Transport event signatures preserve each source shape and its validity."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices.live_transport_events import AwcGairmet, DigitrafficRoadNotices


def test_gairmet_distinct_contours_share_a_tag(tmp_path, monkeypatch):
    device = AwcGairmet("awc-gairmet-01", SimClock(realtime=True), key_dir=str(tmp_path))
    now = datetime.now(UTC)
    props = {
        "product": "ZULU", "hazard": "FZLVL", "tag": "1C", "level": "080",
        "issueTime": (now - timedelta(hours=1)).isoformat(),
        "validTime": (now + timedelta(hours=1)).isoformat(),
    }
    first = {"properties": props, "geometry": {"type": "LineString", "coordinates": [[-101, 39], [-99, 42]]}}
    second = {"properties": props, "geometry": {"type": "LineString", "coordinates": [[-82, 34], [-79, 34]]}}
    expired = {**first, "properties": {**props, "validTime": (now - timedelta(minutes=1)).isoformat()}}
    later = {**first, "properties": {**props, "validTime": (now + timedelta(hours=2)).isoformat()}}

    def fetch(url):
        assert parse_qs(urlsplit(url).query) == {"format": ["geojson"], "fore": ["all"]}
        return {"features": [expired, later, first, second, first]}

    monkeypatch.setattr(device, "_fetch", fetch)
    result = device.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert len(reading["hotspots"]) == 2
    assert len({row["event_id"] for row in reading["hotspots"]}) == 2
    assert [row["geometry"] for row in reading["hotspots"]] == [first["geometry"], second["geometry"]]
    assert {row["valid_at"] for row in reading["hotspots"]} == {props["validTime"]}
    assert {row["level_ft"] for row in reading["hotspots"]} == {8_000}


def test_road_notice_validity_and_authoritative_empty(tmp_path, monkeypatch):
    device = DigitrafficRoadNotices(
        "fintraffic-roadworks-01", SimClock(realtime=True), key_dir=str(tmp_path), roadworks=True,
    )
    now = datetime.now(UTC)
    start = (now - timedelta(days=3)).isoformat()
    end = (now + timedelta(days=1)).isoformat()
    props = {
        "situationId": "GUID123", "situationType": "road work", "versionTime": start,
        "announcements": [{"title": "Road work", "timeAndDuration": {"startTime": start, "endTime": end}}],
    }
    geometry = {"type": "MultiLineString", "coordinates": [[[25, 60], [26, 60.2]]]}
    monkeypatch.setattr(device, "_fetch", lambda _: {"features": [{"properties": props, "geometry": geometry}]})
    result = device.read()
    reading = result["reading"]
    [row] = reading["hotspots"]
    assert verify_reading(reading, result["attestation"])
    assert row["geometry"] == geometry
    assert row["event_bbox"] == [25, 60, 26, 60.2]
    assert row["valid_until"] == end

    props["announcements"][0]["timeAndDuration"]["endTime"] = (now - timedelta(minutes=1)).isoformat()
    empty = device.read()
    assert verify_reading(empty["reading"], empty["attestation"])
    assert empty["reading"]["hotspots"] == []
    assert empty["reading"]["values"] == {"transport_notice_count": 0.0}
