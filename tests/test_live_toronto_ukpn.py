"""Focused source contract and signing tests for Toronto and UK Power Networks."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_toronto, live_ukpn
from gaia.devices.base import DeviceOffline

_NOW = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)  # Wednesday 02:00 in Toronto


class _CaptureTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW.astimezone(tz) if tz else _NOW.replace(tzinfo=None)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    for module in (live_toronto, live_ukpn):
        monkeypatch.setattr(module, "datetime", _CaptureTime)
        monkeypatch.setattr(module, "require_approved_source", lambda _: SimpleNamespace(require_endpoint=lambda _: None))
    clock = SimClock(realtime=True)
    return (
        live_toronto.TorontoRoadRestrictions("toronto-road-restrictions-01", clock, key_dir=str(tmp_path)),
        live_toronto.TorontoBikeShare("toronto-bikeshare-01", clock, key_dir=str(tmp_path)),
        live_ukpn.UkpnLiveFaults("ukpn-live-faults-01", clock, key_dir=str(tmp_path)),
    )


def _epoch(delta=0, *, milliseconds=False):
    return int((_NOW + timedelta(seconds=delta)).timestamp() * (1000 if milliseconds else 1))


def _road(event_id, **overrides):
    return {
        "id": event_id, "lastUpdated": str(_epoch(-3600, milliseconds=True)),
        "startTime": str(_epoch(-86400, milliseconds=True)), "endTime": str(_epoch(86400, milliseconds=True)),
        "type": "ROAD_CLOSED", "road": "Pape Ave", "expired": 0, "currImpact": "High",
        "workPeriod": "Weekdays", "scheduleTuesday": "21:00-05:00", "scheduleWednesday": "08:00-16:00",
        "geoPolyline": "[-79.341,43.67],[-79.342,43.671]", "directionsAffected": "BOTH_DIRECTIONS",
        **overrides,
    }


def _verified(device):
    result = device.read()
    assert verify_reading(result["reading"], result["attestation"])
    return result["reading"]


def test_toronto_road_overnight_schedule_geometry_and_current_impact(sources, monkeypatch):
    roads, _, _ = sources
    payload = {"Closure": [
        _road("good"), _road("daytime", scheduleTuesday=None), _road("no-current-impact", currImpact="None"),
        _road("outdated-duplicate"), _road("outdated-duplicate", lastUpdated=str(_epoch(-60, milliseconds=True)), expired=1),
        _road("one-vertex", geoPolyline="[43.672,-79.339]"),
    ]}
    published = _NOW - timedelta(seconds=15)
    monkeypatch.setattr(roads, "_snapshot", lambda: (payload, published))
    reading = _verified(roads)
    line, point = reading["hotspots"]
    assert line["event_id"] == "good" and line["title"] == "Road closure · Pape Ave"
    assert line["geometry"] == {"type": "LineString", "coordinates": [[-79.341, 43.67], [-79.342, 43.671]]}
    assert line["event_bbox"] == [-79.342, 43.67, -79.341, 43.671]
    assert line["valid_from"] == "2026-11-04T02:00:00+00:00"
    assert line["valid_until"] == "2026-11-04T10:00:00+00:00"
    assert point["geometry"] == {"type": "Point", "coordinates": [-79.339, 43.672]}
    assert reading["source_expires_at"] == (published + timedelta(minutes=15)).isoformat()
    assert line["source_expires_at"] == reading["source_expires_at"]
    assert line["source_published_at"] == published.isoformat()
    assert "Contains information licensed under the Open Government Licence – Toronto." in roads.source


def test_toronto_road_empty_snapshot_and_bad_http_date(sources, monkeypatch):
    roads, _, _ = sources
    monkeypatch.setattr(roads, "_snapshot", lambda: ({"Closure": []}, _NOW))
    assert _verified(roads)["values"] == {"transport_notice_count": 0.0, "source_published_unix": _epoch(),
                                          "source_expires_unix": _epoch(900)}
    monkeypatch.setattr(roads, "_snapshot", lambda: ({"Closure": []}, _NOW - timedelta(minutes=15)))
    with pytest.raises(DeviceOffline, match="response Date"):
        roads.read()


def test_toronto_road_no_stale_current_impact_across_schedule_start(sources, monkeypatch):
    roads, _, _ = sources
    payload = {"Closure": [_road("just-started", scheduleTuesday=None, scheduleWednesday="02:00-03:00")]}
    monkeypatch.setattr(roads, "_snapshot", lambda: (payload, _NOW - timedelta(seconds=1)))
    assert _verified(roads)["hotspots"] == []


def _gbfs(rows, delta=-2):
    return {"last_updated": _epoch(delta), "data": {"stations": rows}}


def _bike_status(station_id, delta=-35, **overrides):
    return {
        "station_id": station_id, "last_reported": _epoch(delta), "num_bikes_available": 0,
        "num_docks_available": 6, "num_bikes_available_types": {"ebike": 0},
        "is_installed": 1, "is_renting": 0, "is_returning": 1, **overrides,
    }


def test_toronto_bikeshare_joins_coordinates_and_preserves_zero_counts(sources, monkeypatch):
    _, bikes, _ = sources
    info = _gbfs([{"station_id": key, "name": "Fort York", "lat": 43.6397, "lon": -79.3961, "obcn": "123"}
                  for key in ("7000", "7001", "7002")])
    status = _gbfs([_bike_status("7000"), _bike_status("7001", -300), _bike_status("7002", 1)])
    monkeypatch.setattr(bikes, "_fetch", lambda url: info if url == bikes.info_url else status)
    [row] = _verified(bikes)["hotspots"]
    assert row["station_id"] == "7000" and row["title"] == "Fort York"
    assert row["latitude"] == 43.6397 and row["longitude"] == -79.3961
    assert row["bikes_available"] == row["ebikes_available"] == row["is_renting"] == 0
    assert row["docks_available"] == 6 and row["is_returning"] == 1
    assert row["valid_until"] == (_NOW + timedelta(seconds=265)).isoformat()
    assert "obcn" not in row
    assert "Contains information licensed under the Open Government Licence – Toronto." in bikes.source


def test_toronto_bikeshare_declines_stale_and_duplicate_feed(sources, monkeypatch):
    _, bikes, _ = sources
    info = _gbfs([{"station_id": "7000"}])
    status = _gbfs([], -300)
    monkeypatch.setattr(bikes, "_fetch", lambda url: info if url == bikes.info_url else status)
    with pytest.raises(DeviceOffline, match="stale"):
        bikes.read()
    status = _gbfs([_bike_status("7000"), _bike_status("7000")])
    with pytest.raises(DeviceOffline, match="invalid"):
        bikes.read()


def _metadata(count, delta=-180):
    return {"dataset_id": "ukpn-live-faults", "metas": {"default": {
        "data_processed": (_NOW + timedelta(seconds=delta)).isoformat(), "records_count": count,
    }}}


def _outage(ref, **overrides):
    return {
        "incidentreference": ref, "powercuttype": "Unplanned", "statusid": 1,
        "creationdatetime": "2026-11-04T06:20:00", "restoreddatetime": None,
        "geopoint": {"lat": 51.397567, "lon": 0.123456}, "nocustomeraffected": 11,
        "operatingzone": "CHELMSFORD", "estimatedrestorationdate": "2026-11-04T08:00:00",
        "fullpostcodedata": "private", "mainmessage": "not exposed", **overrides,
    }


def test_ukpn_coarse_active_incidents_and_estimate(sources, monkeypatch):
    _, _, outages = sources
    rows = [
        _outage("INCD-1-A"), _outage("INCD-2-A", restoreddatetime="2026-11-04T06:30:00"),
        _outage("INCD-3-A", powercuttype="Planned", planneddate="2026-11-04T09:30:00"),
        _outage("MULT-123", powercuttype="Multiple"), _outage("INCD-4-A", statusid=8),
    ]
    monkeypatch.setattr(outages, "_fetch", lambda url: _metadata(len(rows)) if url == outages.url else rows)
    reading = _verified(outages)
    [row] = reading["hotspots"]
    assert row["incident_id"] == "INCD-1-A" and row["incident_status"] == "In progress"
    assert row["latitude"] == 51.4 and row["longitude"] == 0.12
    assert row["customers_affected"] == 11
    assert row["estimated_restoration_local"] == "2026-11-04T08:00:00"
    assert row["valid_until"] == (_NOW + timedelta(minutes=27)).isoformat()
    assert row["source_expires_at"] == row["valid_until"]
    assert "fullpostcodedata" not in row and "mainmessage" not in row and "estimated_restoration_at" not in row


def test_ukpn_authoritative_empty_and_count_mismatch(sources, monkeypatch):
    _, _, outages = sources
    calls = []

    def fetch(url):
        calls.append(url)
        return _metadata(1 if len(calls) == 1 else 0) if url == outages.url else []

    monkeypatch.setattr(outages, "_fetch", fetch)
    assert _verified(outages)["values"] == {"power_outage_count": 0.0, "source_published_unix": _epoch(-180),
                                            "source_expires_unix": _epoch(1620)}
    assert calls == [outages.url, outages.records_url] * 2
    monkeypatch.setattr(outages, "_fetch", lambda url: _metadata(1) if url == outages.url else [])
    with pytest.raises(DeviceOffline, match="count"):
        outages.read()
    monkeypatch.setattr(outages, "_fetch", lambda url: _metadata(0, delta=-1800))
    with pytest.raises(DeviceOffline, match="stale"):
        outages.read()
