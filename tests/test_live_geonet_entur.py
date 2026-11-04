"""Targeted checks for fresh coastal gauge observations and rail positions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_entur, live_geonet_coastal
from gaia.devices.base import DeviceOffline

_NOW = datetime(2026, 11, 4, 7, 57, 50, tzinfo=UTC)


class _CaptureTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW.astimezone(tz) if tz else _NOW.replace(tzinfo=None)


@pytest.fixture
def coastal(tmp_path, monkeypatch):
    monkeypatch.setattr(live_geonet_coastal, "datetime", _CaptureTime)
    return live_geonet_coastal.GeonetCoastalGauge(
        "geonet-coastal-auct", SimClock(realtime=True), gauge="AUCT", key_dir=str(tmp_path),
    )


def _series(*samples):
    return [{
        "series": {
            "domain": "coastal", "station": "AUCT", "name": "water-height",
            "sensorCode": "40", "method": "15s", "aspect": "nil",
        },
        "valueUnit": "m", "datum": "WGS84", "latitude": -36.8314, "longitude": 174.7865,
        "data": list(samples),
    }]


def _sample(seconds, value, qc=""):
    return {"ts": (_NOW + timedelta(seconds=seconds)).isoformat(), "val": value, "qc": qc}


def test_geonet_signs_only_fresh_observation_from_pinned_sensor(coastal, monkeypatch):
    raw = _series(_sample(-10, 6.3, "flagged"), _sample(-1, "NaN"), _sample(10, 5.6),
                  _sample(-60, 5.12345), _sample(-120, 5.5), _sample(-300, 5.9))

    def fetch(url):
        assert url.endswith("/coastal/AUCT/water-height/40/15s/nil/latest/6h")
        return raw

    monkeypatch.setattr(coastal, "_fetch", fetch)
    result = coastal.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["values"] == {
        "coastal_water_height_m": 5.1235, "observation_unix": (_NOW - timedelta(seconds=60)).timestamp(),
        "latitude": -36.8314, "longitude": 174.7865, "coastal_sensor_code": 40.0,
    }
    assert reading["units"]["coastal_water_height_m"] == "m above local pressure sensor"


@pytest.mark.parametrize("kind", ["channel", "units", "coordinates", "conflict", "expired"])
def test_geonet_refuses_incompatible_or_unusable_series(coastal, kind):
    raw = _series(_sample(-30, 5.4))
    if kind == "channel":
        raw[0]["series"]["sensorCode"] = "41"
    elif kind == "units":
        raw[0]["valueUnit"] = "mm"
    elif kind == "coordinates":
        raw[0]["longitude"] = 0.0
    elif kind == "conflict":
        raw[0]["data"].append(_sample(-30, 5.5))
    else:
        raw[0]["data"] = [_sample(-300, 5.5)]
    with pytest.raises(DeviceOffline):
        coastal.map(raw)


@pytest.fixture
def rail(tmp_path, monkeypatch):
    monkeypatch.setattr(live_entur, "datetime", _CaptureTime)
    monkeypatch.setattr(live_entur, "_SNAPSHOT_CACHE", (float("-inf"), None))
    return live_entur.EnturRail("entur-rail-vyg-01", SimClock(realtime=True), key_dir=str(tmp_path))


def _activity(ref, observed_s, end_s=600, *, line="VYG:Line:RE11", latitude="59.5", provider="VYG"):
    recorded = (_NOW + timedelta(seconds=observed_s)).isoformat()
    end = (_NOW + timedelta(seconds=end_s)).isoformat()
    return f"""
    <VehicleActivity>
        <RecordedAtTime>{recorded}</RecordedAtTime><ValidUntilTime>{end}</ValidUntilTime>
        <MonitoredVehicleJourney>
            <LineRef>{line}</LineRef><VehicleMode>rail</VehicleMode><DataSource>{provider}</DataSource>
            <VehicleLocation><Longitude>10.2</Longitude><Latitude>{latitude}</Latitude></VehicleLocation>
            <VehicleRef>{ref}</VehicleRef>
        </MonitoredVehicleJourney>
    </VehicleActivity>
    """


def _siri(*activities, more="false", published_s=-1):
    published = (_NOW + timedelta(seconds=published_s)).isoformat()
    return f"""
    <Siri xmlns="http://www.siri.org.uk/siri" version="2.0"><ServiceDelivery>
        <ProducerRef>ENT</ProducerRef><MoreData>{more}</MoreData>
        <VehicleMonitoringDelivery>
            <ResponseTimestamp>{published}</ResponseTimestamp>
            {''.join(activities)}
        </VehicleMonitoringDelivery>
    </ServiceDelivery></Siri>
    """


def test_entur_signs_latest_valid_positions_and_does_not_invent_speed(rail, monkeypatch):
    xml = _siri(
        _activity("train-123", -90, latitude="59.1"),
        _activity("train-123", -60, latitude="59.5"),
        _activity("expired-source", -10, -1),
        _activity("expired-age", -300),
        _activity("other-operator", -1, provider="NSB"),
        _activity("removed", -20),
        _activity("removed", -10, -1),
        _activity("future", 1),
    )
    monkeypatch.setattr(rail, "_fetch_text", lambda url, **kw: xml)
    result = rail.read()
    reading = result["reading"]
    [row] = reading["hotspots"]
    assert verify_reading(reading, result["attestation"])
    assert row["train_number"].startswith("entur-")
    assert row["line_ref"] == "VYG:Line:RE11"
    assert row["latitude"] == 59.5 and row["longitude"] == 10.2
    assert row["observed_at"] == (_NOW - timedelta(seconds=60)).isoformat()
    assert row["valid_until"] == (_NOW + timedelta(seconds=240)).isoformat()
    assert row["source_valid_until"] == (_NOW + timedelta(seconds=600)).isoformat()
    assert "speed_kmh" not in row


def test_entur_signs_fresh_authoritative_empty_but_rejects_partial_snapshot(rail, monkeypatch):
    monkeypatch.setattr(rail, "_cached_snapshot", lambda: (_siri(), _NOW))
    result = rail.read()
    assert verify_reading(result["reading"], result["attestation"])
    assert result["reading"]["hotspots"] == []
    assert result["reading"]["values"] == {"rail_position_count": 0.0}
    monkeypatch.setattr(rail, "_cached_snapshot", lambda: (_siri(more="true"), _NOW))
    with pytest.raises(DeviceOffline, match="incomplete"):
        rail.read()
    monkeypatch.setattr(rail, "_cached_snapshot", lambda: (_siri(published_s=-120), _NOW))
    with pytest.raises(DeviceOffline, match="stale"):
        rail.read()


def test_entur_coalesces_attempts_and_throttles_after_failure(rail, tmp_path, monkeypatch):
    second = live_entur.EnturRail("entur-rail-second", SimClock(realtime=True), key_dir=str(tmp_path))
    times = [0.0]
    monkeypatch.setattr(live_entur, "time", SimpleNamespace(monotonic=lambda: times[0]))
    calls = []

    def fetch(url, *, max_chars):
        calls.append(url)
        assert url.endswith("?datasetId=VYG")
        assert max_chars == 1_000_000
        if len(calls) == 1:
            raise DeviceOffline("test upstream failure")
        return _siri()

    monkeypatch.setattr(rail, "_fetch_text", fetch)
    monkeypatch.setattr(second, "_fetch_text", fetch)
    with pytest.raises(DeviceOffline, match="upstream failure"):
        rail.read()
    times[0] = 19
    with pytest.raises(DeviceOffline, match="retry interval"):
        second.read()
    assert len(calls) == 1
    times[0] = 20
    second.read()
    times[0] = 39
    rail.read()
    assert len(calls) == 2
