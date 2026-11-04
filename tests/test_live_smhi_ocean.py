"""SMHI hourly readings are paired by station, depth, time and quality."""

from datetime import UTC, datetime

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_smhi_ocean import SmhiOcean


class CaptureTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 4, 7, 30, tzinfo=UTC).astimezone(tz)


def _row(hour, depth, value, quality="G"):
    measured = datetime(2026, 11, 4, hour, tzinfo=UTC)
    return {"date": int(measured.timestamp() * 1000), "depth": str(depth), "value": value, "quality": quality}


def _payload(device, code, unit, *rows):
    return {
        "parameter": {"key": code, "unit": unit},
        "station": {"key": device.pilot.station, "owner": device.pilot.owner},
        "period": {"key": "latest-day"},
        "position": [{"latitude": device.latitude, "longitude": device.longitude}],
        "value": list(rows),
    }


def test_smhi_pairs_salinity_and_oxygen_at_depth(tmp_path, monkeypatch):
    monkeypatch.setattr("gaia.devices.live_smhi_ocean.datetime", CaptureTime)
    device = SmhiOcean("smhi-ocean-flinten16-8m", SimClock(realtime=True), key_dir=tmp_path)
    salinity = _payload(device, "4", "PSU", _row(7, 8, 9.2), _row(6, 8, 7.28), _row(6, 2, 24))
    oxygen = _payload(device, "15", "ml/l", _row(7, 8, 7.4, "Y"), _row(7, 2, 8.0), _row(6, 8, 6.78))
    payloads = {device._series_url("4"): salinity, device._series_url("15"): oxygen}
    monkeypatch.setattr(device, "_fetch", payloads.__getitem__)
    result = device.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["values"] == {
        "salinity_psu": 7.28, "dissolved_oxygen_ml_l": 6.78, "measurement_depth_m": 8.0,
        "observation_unix": _row(6, 8, 0)["date"] / 1000, "latitude": 55.561, "longitude": 12.8095,
    }
    assert "Sjöfartsverket" in reading["attribution"]
    oxygen["value"] = [_row(4, 8, 6.81), _row(7, 2, 7.32)]
    with pytest.raises(DeviceOffline, match="no fresh same-depth"):
        device.read()


def test_smhi_zero_current_and_station_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("gaia.devices.live_smhi_ocean.datetime", CaptureTime)
    device = SmhiOcean("smhi-ocean-kalmar2-0m", SimClock(realtime=True), key_dir=tmp_path)
    speed = _payload(device, "3", "cm/s", _row(6, 0, 36.52), _row(7, 0, 0))
    direction = _payload(device, "2", "°", _row(6, 0, 104.21), _row(7, 0, 82.3))
    payloads = {device._series_url("3"): speed, device._series_url("2"): direction}
    monkeypatch.setattr(device, "_fetch", payloads.__getitem__)
    current = device.sample()
    assert current["current_speed_mps"] == 0
    assert "current_direction_deg" not in current
    speed["value"].pop()
    assert device.sample()["current_speed_mps"] == pytest.approx(0.3652)
    direction["station"]["owner"] = "Unexpected owner"
    with pytest.raises(DeviceOffline, match="identity or unit mismatch"):
        device.sample()
