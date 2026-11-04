"""NOAA Air Gap relays preserve source flags, station and measurement time."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_noaa_airgap import NoaaAirGap


class CaptureTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 4, 12, 12, 11, tzinfo=UTC).astimezone(tz)


def _ben_franklin():
    return {
        "metadata": {"id": "8545556", "name": "Ben Franklin Bridge Air Gap", "lat": "39.9528", "lon": "-75.1358"},
        "data": [{"t": "2026-11-04 12:06", "v": "41.387", "s": "0.012", "f": "1,0,0,0"}],
    }


def test_airgap_attests_outlier_count_as_count_not_quality_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("gaia.devices.live_noaa_airgap.datetime", CaptureTime)
    device = NoaaAirGap("noaa-airgap-8545556", SimClock(realtime=True), key_dir=tmp_path)

    def fetch(url):
        query = parse_qs(urlsplit(url).query)
        assert query["date"] == ["latest"] and query["product"] == ["air_gap"]
        assert query["station"] == ["8545556"] and query["units"] == ["metric"]
        assert query["time_zone"] == ["gmt"] and "datum" not in query
        return _ben_franklin()

    monkeypatch.setattr(device, "_fetch", fetch)
    result = device.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["values"] == {
        "air_gap_m": 41.387, "air_gap_sigma_m": 0.012, "air_gap_outlier_count": 1,
        "air_gap_flat_flag": 0, "air_gap_rate_flag": 0, "air_gap_limit_flag": 0,
        "observation_unix": datetime(2026, 11, 4, 12, 6, tzinfo=UTC).timestamp(),
        "latitude": 39.9528, "longitude": -75.1358,
    }
    assert "NOAA NOS CO-OPS" in reading["attribution"]


@pytest.mark.parametrize("flags", ["1,1,0,0", "0,0,1,0", "0,0,0,1", "0,0,0", "0,0,0,2"])
def test_airgap_rejects_alarm_and_invalid_flags(tmp_path, monkeypatch, flags):
    monkeypatch.setattr("gaia.devices.live_noaa_airgap.datetime", CaptureTime)
    device = NoaaAirGap("noaa-airgap-8545556", SimClock(realtime=True), key_dir=tmp_path)
    payload = _ben_franklin()
    payload["data"][0]["f"] = flags
    with pytest.raises(DeviceOffline, match="quality checks|flags"):
        device.map(payload)


def test_airgap_rejects_other_station_and_stale_or_future_observation(tmp_path, monkeypatch):
    monkeypatch.setattr("gaia.devices.live_noaa_airgap.datetime", CaptureTime)
    device = NoaaAirGap("noaa-airgap-8545556", SimClock(realtime=True), key_dir=tmp_path)
    payload = _ben_franklin()
    payload["metadata"]["id"] = "8726371"
    with pytest.raises(DeviceOffline, match="station unavailable or mismatched"):
        device.map(payload)
    payload = _ben_franklin()
    for when in ("2026-11-04 11:54", "2026-11-04 12:18"):
        payload["data"][0]["t"] = when
        with pytest.raises(DeviceOffline, match="not current"):
            device.map(payload)
