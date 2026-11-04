"""AOML OSMC observes individual drifters within a bounded UTC interval."""

from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_aoml_drifters import AomlDrifters

_HEADER = (
    "platform_code,platform_type,time,latitude,longitude,sst,slp\n"
    ",,UTC,degrees_north,degrees_east,Deg C,hPa\n"
)


class CaptureTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 4, 12, 7, 31, tzinfo=UTC).astimezone(tz)


def test_aoml_signs_latest_per_platform_and_filters_timestamps(tmp_path, monkeypatch):
    monkeypatch.setattr("gaia.devices.live_aoml_drifters.datetime", CaptureTime)
    device = AomlDrifters("aoml-drifters-01", SimClock(realtime=True), key_dir=tmp_path)
    data = _HEADER + (
        "21724,DRIFTING BUOYS (GENERIC),2026-11-04T11:00:00Z,15.32,127.04,29.6,NaN\n"
        "21724,DRIFTING BUOYS (GENERIC),2026-11-04T10:00:00Z,15.3,127.04,29.5,NaN\n"
        "1101570,DRIFTING BUOYS,2026-11-04T09:59:00Z,50.3634,-173.3895,0,1021.5\n"
        "1801946,DRIFTING BUOYS,2026-11-04T23:00:00Z,48.6309,-6.3125,14.31,NaN\n"
        "2802244,DRIFTING BUOYS,2026-11-04T06:00:00Z,62.7783,5.7638,11.88,1036.8\n"
        "2802245,SHIPS,2026-11-04T09:00:00Z,49.9523,-28.2343,12.54,NaN\n"
        "2802246,DRIFTING BUOYS,2026-11-04T09:00:00Z,95,-28.2343,12.54,NaN\n"
    )

    def fetch(url):
        query = unquote(urlsplit(url).query)
        assert "&time>=2026-11-04T06:07:31Z&time<=2026-11-04T12:07:31Z" in query
        assert query.endswith('&orderByMax("platform_code,time")')
        return data

    monkeypatch.setattr(device, "_fetch_csv", fetch)
    result = device.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["drifter_count"] == reading["hotspot_count"] == 2
    assert [row["platform_code"] for row in reading["hotspots"]] == ["21724", "1101570"]
    first, second = reading["hotspots"]
    assert first["sst_c"] == 29.6 and "sea_level_pressure_hpa" not in first
    assert first["observed_at"] == "2026-11-04T11:00:00+00:00"
    assert second["sst_c"] == 0 and second["sea_level_pressure_hpa"] == 1021.5
    assert device.retain_hotspots_in_history is False


def test_aoml_rejects_wrong_units_and_conflicting_tied_reports(tmp_path):
    device = AomlDrifters("aoml-drifters-01", SimClock(realtime=True), key_dir=tmp_path)
    now = CaptureTime.now(UTC)
    with pytest.raises(DeviceOffline, match="columns or units"):
        device._observations(_HEADER.replace("Deg C", "K"), now)
    with pytest.raises(DeviceOffline, match="conflicting OSMC observation"):
        device._observations(_HEADER + (
            "21724,DRIFTING BUOYS,2026-11-04T11:00:00Z,15.32,127.04,29.6,NaN\n"
            "21724,DRIFTING BUOYS,2026-11-04T11:00:00Z,15.32,127.04,29.7,NaN\n"
        ), now)
    with pytest.raises(DeviceOffline, match="no fresh"):
        device._observations(_HEADER, now)


def test_aoml_stream_aborts_oversized_body(tmp_path, monkeypatch):
    device = AomlDrifters("aoml-drifters-01", SimClock(realtime=True), key_dir=tmp_path)
    monkeypatch.setattr("gaia.devices.live_aoml_drifters._MAX_RESPONSE_BYTES", 50)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"x" * 60)))
    with httpx.Client(transport=transport) as client:
        monkeypatch.setattr("gaia.devices.live_aoml_drifters.httpx.stream", client.stream)
        with pytest.raises(DeviceOffline, match="size limit"):
            device._fetch_csv(device.url)
