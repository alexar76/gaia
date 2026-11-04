"""Measured CHMI time series are signed without mixing forecast or sample times."""

from datetime import UTC, datetime

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_chmi_hydro
from gaia.devices.base import DeviceOffline
from gaia.devices.live_chmi_hydro import ChmiRiverGauge

_NOW = datetime(2026, 11, 4, 7, 32, tzinfo=UTC)
_OBS = "2026-11-04T07:10:00Z"


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW.astimezone(tz)


@pytest.fixture
def gauge(monkeypatch, tmp_path):
    monkeypatch.setattr(live_chmi_hydro, "datetime", _Clock)
    return ChmiRiverGauge("chmi-river-200100", SimClock(realtime=True), station_id="0-203-1-200100", key_dir=tmp_path)


def _series(code, unit, *values):
    return {"tsConID": code, "unit": unit, "tsData": [{"dt": stamp, "value": value} for stamp, value in values]}


def _payload(*series, station="0-203-1-200100"):
    return {"objList": [{"objID": station, "tsList": list(series)}]}


def test_chmi_signs_latest_paired_observation_and_ignores_forecast(monkeypatch, gauge):
    next_stamp = "2026-11-04T07:20:00Z"
    document = _payload(
        _series("H", "CM", (next_stamp, 42), (_OBS, 41)),
        _series("Q", "M3_S", (_OBS, 35.2408), (next_stamp, None)),
        _series("TH", "0C", (next_stamp, 9.9), (_OBS, 9.8)),
        _series("H_F", "CM", (next_stamp, 600)), _series("Q_F", "M3_S", (next_stamp, 6000)),
    )

    def fetch(url):
        assert url == "https://opendata.chmi.cz/hydrology/now/data/0-203-1-200100.json"
        return document

    monkeypatch.setattr(gauge, "_fetch", fetch)
    result = gauge.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["values"] == {
        "gage_height_m": 0.41, "discharge_m3s": 35.2408, "water_temperature_c": 9.8,
        "observation_unix": datetime.fromisoformat(_OBS).timestamp(),
        "latitude": 50.0275, "longitude": 14.3969,
    }
    assert reading["units"]["gage_height_m"] == "m"


def test_chmi_does_not_mix_temperature_from_another_time(gauge):
    values = gauge.map(_payload(
        _series("H", "CM", (_OBS, 41)), _series("Q", "M3_S", (_OBS, 35.2408)),
        _series("TH", "0C", ("2026-11-04T07:20:00Z", 9.8)),
    ))
    assert "water_temperature_c" not in values


@pytest.mark.parametrize("document,reason", [
    (_payload(station="0-203-1-042000"), "identity"),
    (_payload(_series("H", "M", (_OBS, 41)), _series("Q", "M3_S", (_OBS, 35.2408))), "last 2 hours"),
    (_payload(_series("H", "CM", ("2026-11-04T05:20:00Z", 41)),
              _series("Q", "M3_S", ("2026-11-04T05:20:00Z", 35.2408))), "last 2 hours"),
    (_payload(_series("H", "CM", (_OBS, 41), (_OBS, 42))), "conflicting CHMI H"),
])
def test_chmi_fails_closed_on_bad_identity_units_stale_or_conflicting_measurements(gauge, document, reason):
    with pytest.raises(DeviceOffline, match=reason):
        gauge.map(document)
