"""Operational source boundaries for the NWPS and Environment Agency pilots."""

from datetime import UTC, datetime, timedelta

import pytest
from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_water_ext import EaTideGauge, NwsRiverForecast


def test_nwps_signs_future_peak_and_issuance(tmp_path, monkeypatch):
    device = NwsRiverForecast("nwps-pttp1", SimClock(realtime=True), gauge="PTTP1", key_dir=tmp_path)
    now = datetime.now(UTC)
    issue = now - timedelta(hours=4)
    peak_time = now + timedelta(hours=18)
    payload = {
        "pedts": "HGIFF",
        "primaryName": "Stage",
        "primaryUnits": "ft",
        "issuedTime": issue.isoformat(),
        "data": [
            {"primary": 100, "validTime": (now - timedelta(minutes=1)).isoformat()},
            {"primary": 18.2, "validTime": peak_time.isoformat()},
            {"primary": 18.2, "validTime": (peak_time + timedelta(hours=6)).isoformat()},
            {"primary": 100, "validTime": (now + timedelta(hours=73)).isoformat()},
            {"primary": -999, "validTime": (now + timedelta(hours=30)).isoformat()},
        ],
    }
    monkeypatch.setattr(device, "_fetch", lambda url: payload)
    result = device.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["values"]["forecast_stage_m"] == pytest.approx(5.5474)
    assert reading["values"]["forecast_valid_unix"] == pytest.approx(peak_time.timestamp(), abs=0.001)
    assert reading["values"]["forecast_issued_unix"] == pytest.approx(issue.timestamp(), abs=0.001)
    payload["issuedTime"] = (now - timedelta(hours=25)).isoformat()
    with pytest.raises(DeviceOffline, match="issue time"):
        device.read()


def test_ea_tide_signs_measured_maod_and_sample_time(tmp_path, monkeypatch):
    device = EaTideGauge("ea-tide-e72639", SimClock(realtime=True), gauge="E72639", key_dir=tmp_path)
    sample_time = datetime.now(UTC) - timedelta(minutes=30)
    sample = {"dateTime": sample_time.isoformat(), "value": -2.808, "measure": device.url}
    measure = {
        "notation": device.measure, "stationReference": "E72639", "unitName": "mAOD",
        "period": 900, "datumType": "http://environment.data.gov.uk/flood-monitoring/def/core/datumAOD",
        "latestReading": sample,
    }
    monkeypatch.setattr(device, "_fetch", lambda url: {"items": measure})
    result = device.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["units"]["water_level_maod"] == "mAOD"
    assert reading["values"]["water_level_maod"] == -2.808
    assert reading["values"]["observation_unix"] == pytest.approx(sample_time.timestamp(), abs=0.001)
    measure["unitName"] = "m"
    with pytest.raises(DeviceOffline, match="mAOD"):
        device.read()
    measure["unitName"] = "mAOD"
    sample["dateTime"] = (sample_time - timedelta(hours=2)).isoformat()
    with pytest.raises(DeviceOffline, match="stale"):
        device.read()
