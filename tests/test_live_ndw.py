"""DATEX II road events retain provider geometry and effective validity."""

from datetime import UTC, datetime, timedelta
import gzip
import xml.etree.ElementTree as ET

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_ndw
from gaia.devices.base import DeviceOffline

_NOW = datetime(2026, 11, 4, 6, 56, 30, tzinfo=UTC)
_PUBLISHED = _NOW - timedelta(seconds=30)
_POINT = """
    <loc:pointByCoordinates><loc:pointCoordinates>
        <loc:latitude>52.633644</loc:latitude><loc:longitude>5.8219943</loc:longitude>
    </loc:pointCoordinates></loc:pointByCoordinates>
"""
_LINE = """
    <loc:gmlLineString srsName="WGS 84">
        <loc:posList>52.58548 6.128689 52.584437 6.129386</loc:posList>
    </loc:gmlLineString>
"""
_SECOND_LINE = """
    <loc:gmlLineString srsName="WGS 84">
        <loc:posList>52.584314 6.1312 52.586561 6.16165</loc:posList>
    </loc:gmlLineString>
"""
_CLOSURE = """
    <sit:operatorActionStatus>implemented</sit:operatorActionStatus>
    <sit:roadOrCarriagewayOrLaneManagementType>carriagewayClosures</sit:roadOrCarriagewayOrLaneManagementType>
"""


def _xml(*records, publication=_PUBLISHED):
    situations = "".join(f"<sit:situation>{r}</sit:situation>" for r in records)
    return f"""
    <msg:messageContainer xmlns:msg="http://datex2.eu/schema/3/messageContainer"
        xmlns:com="http://datex2.eu/schema/3/common" xmlns:sit="http://datex2.eu/schema/3/situation"
        xmlns:loc="http://datex2.eu/schema/3/locationReferencing"
        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
        <msg:payload xsi:type="sit:SituationPublication">
            <com:publicationTime>{publication.isoformat()}</com:publicationTime>
            {situations}
        </msg:payload>
    </msg:messageContainer>
    """


def _record(event_id, geometry=_POINT, *, end=None, periods="", version=_PUBLISHED, extras=""):
    end_tag = f"<com:overallEndTime>{end.isoformat()}</com:overallEndTime>" if end else ""
    return f"""
    <sit:situationRecord id="{event_id}" xsi:type="sit:VehicleObstruction">
        <sit:situationRecordVersionTime>{version.isoformat()}</sit:situationRecordVersionTime>
        <sit:validity><com:validityStatus>definedByValidityTimeSpec</com:validityStatus>
            <com:validityTimeSpecification>
                <com:overallStartTime>2026-11-04T05:00:00.012345678Z</com:overallStartTime>
                {end_tag}{periods}
            </com:validityTimeSpecification>
        </sit:validity>
        <sit:locationReference>{geometry}</sit:locationReference>
        {extras}
    </sit:situationRecord>
    """


@pytest.fixture
def device_factory(tmp_path, monkeypatch):
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW

    monkeypatch.setattr(live_ndw, "datetime", FixedTime)

    def create(*, closures=False):
        device_id = "ndw-road-closures-01" if closures else "ndw-road-srti-01"
        return live_ndw.NdwRoadNotices(
            device_id, SimClock(realtime=True), key_dir=str(tmp_path), closures=closures,
        )

    return create


def test_ndw_srti_signs_points_and_open_ended_expiry(device_factory, monkeypatch):
    device = device_factory()
    monkeypatch.setattr(device, "_fetch_xml", lambda: ET.fromstring(_xml(_record("NDW-1"))))
    result = device.read()
    reading = result["reading"]
    [row] = reading["hotspots"]
    assert verify_reading(reading, result["attestation"])
    assert row["geometry"] == {"type": "Point", "coordinates": [5.82199, 52.63364]}
    assert row["event_bbox"] == [5.82199, 52.63364, 5.82199, 52.63364]
    assert row["title"] == "Vehicle Obstruction"
    assert row["source_published_at"] == reading["source_published_at"] == _PUBLISHED.isoformat()
    assert row["source_expires_at"] == (_PUBLISHED + timedelta(minutes=5)).isoformat()
    assert "valid_until" not in row


def test_ndw_closures_intersect_valid_period_and_preserve_each_gml_line(device_factory, monkeypatch):
    device = device_factory(closures=True)
    start = _NOW - timedelta(minutes=3)
    end = _NOW + timedelta(minutes=2)
    periods = f"""
        <com:validPeriod><com:startOfPeriod>{start.isoformat()}</com:startOfPeriod>
            <com:endOfPeriod>{end.isoformat()}</com:endOfPeriod></com:validPeriod>
        <com:validPeriod><com:startOfPeriod>2026-11-04T07:10:00Z</com:startOfPeriod></com:validPeriod>
    """
    record = _record("closure-1", _LINE + _SECOND_LINE, periods=periods,
                     extras=_CLOSURE, end=_NOW + timedelta(hours=1))
    monkeypatch.setattr(device, "_fetch_xml", lambda: ET.fromstring(_xml(record)))
    result = device.read()
    [row] = result["reading"]["hotspots"]
    assert verify_reading(result["reading"], result["attestation"])
    assert row["geometry"] == {"type": "MultiLineString", "coordinates": [
        [[6.12869, 52.58548], [6.12939, 52.58444]],
        [[6.1312, 52.58431], [6.16165, 52.58656]],
    ]}
    assert row["valid_from"] == start.isoformat()
    assert row["valid_until"] == end.isoformat()
    assert row["notice_text"] == "NDW action status: implemented."


def test_ndw_omits_unsupported_schedules_and_uses_latest_record(device_factory, monkeypatch):
    device = device_factory(closures=True)
    unsupported = ("<com:validPeriod><com:startOfPeriod>2026-11-04T06:00:00Z</com:startOfPeriod>"
                   "<com:recurringTimePeriodOfDay /></com:validPeriod>")
    inactive = "<com:validPeriod><com:startOfPeriod>2026-11-04T07:00:00Z</com:startOfPeriod></com:validPeriod>"
    records = [
        _record("replaced", _LINE, version=_PUBLISHED - timedelta(minutes=1), extras=_CLOSURE),
        _record("replaced", _LINE, extras=_CLOSURE, end=_NOW - timedelta(seconds=1)),
        _record("unsupported-schedule", _LINE, periods=unsupported, extras=_CLOSURE),
        _record("not-currently-active", _LINE, periods=inactive, extras=_CLOSURE),
        _record("wrong-crs", _LINE.replace("WGS 84", "RD New"), extras=_CLOSURE),
        _record("not-a-closure", _LINE, extras=_CLOSURE.replace("carriagewayClosures", "useOfSpecifiedLanesOrCarriagewaysAllowed")),
    ]
    monkeypatch.setattr(device, "_fetch_xml", lambda: ET.fromstring(_xml(*records)))
    result = device.read()
    assert verify_reading(result["reading"], result["attestation"])
    assert result["reading"]["hotspots"] == []
    assert result["reading"]["values"] == {"transport_notice_count": 0.0}


@pytest.mark.parametrize("offset", [timedelta(minutes=-5), timedelta(seconds=1)])
def test_ndw_refuses_stale_or_future_publication(device_factory, monkeypatch, offset):
    device = device_factory()
    monkeypatch.setattr(device, "_fetch_xml", lambda: ET.fromstring(_xml(publication=_NOW + offset)))
    with pytest.raises(DeviceOffline, match="publication time"):
        device.read()


def test_ndw_refuses_oversized_gzip_or_expanded_xml(device_factory, monkeypatch):
    device = device_factory()
    monkeypatch.setattr(live_ndw, "_assert_url_allowed", lambda url: url)
    monkeypatch.setattr(live_ndw, "require_approved_source", lambda policy: type(
        "Policy", (), {"require_endpoint": lambda self, url: None},
    )())
    payload = gzip.compress(_xml().encode())

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def iter_raw(self, **kwargs):
            yield payload

    monkeypatch.setattr(live_ndw.httpx, "stream", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(live_ndw, "_MAX_XML_BYTES", 10)
    with pytest.raises(DeviceOffline, match="XML body exceeds limit"):
        device.read()
    monkeypatch.setattr(live_ndw, "_MAX_GZIP_BYTES", 10)
    with pytest.raises(DeviceOffline, match="compressed NDW body exceeds limit"):
        device.read()
