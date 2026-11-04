"""SNOTEL's station identity, paired hours, and quality gates."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_snotel import UsdaSnotel


class SummerTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 7, 11, 7, 30, tzinfo=UTC).astimezone(tz)


def _series(code, *values):
    return {
        "stationElement": {
            "elementCode": code, "ordinal": 1, "durationName": "HOURLY",
            "storedUnitCode": "in", "derivedData": False,
        },
        "values": list(values),
    }


def _reading(hour, value, *, qc="V", qa="R"):
    return {"date": f"2026-07-10 {hour}:00", "value": value, "qcFlag": qc, "qaFlag": qa}


@pytest.fixture
def snotel(tmp_path, monkeypatch):
    monkeypatch.setattr("gaia.devices.live_snotel.datetime", SummerTime)
    return UsdaSnotel("snotel-335", SimClock(realtime=True), station_triplet="335:CO:SNTL", key_dir=tmp_path)


def test_snotel_signs_newest_paired_hour_in_fixed_pst(snotel, monkeypatch):
    payload = [{"stationTriplet": snotel.station_triplet, "data": [
        _series("SNWD", _reading("20", 5), _reading("22", 0), _reading("23", 9, qc="S")),
        _series("WTEQ", _reading("20", 1), _reading("22", 0.6), _reading("23", 0.8)),
    ]}]

    def fetch(url):
        query = parse_qs(urlsplit(url).query)
        assert query["stationTriplets"] == [snotel.station_triplet]
        assert query["elements"] == ["WTEQ,SNWD"] and query["duration"] == ["HOURLY"]
        assert query["returnFlags"] == ["true"] and query["returnSuspectData"] == ["false"]
        assert query["beginDate"] == ["2026-07-10 17:00"]
        assert query["endDate"] == ["2026-07-10 23:00"]
        return payload

    monkeypatch.setattr(snotel, "_fetch", fetch)
    result = snotel.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["values"] == {
        "snow_depth_cm": 0.0,
        "snow_water_equivalent_cm": 1.524,
        "observation_unix": datetime(2026, 7, 11, 6, tzinfo=UTC).timestamp(),
        "latitude": 39.8036,
        "longitude": -105.7779,
    }


def test_snotel_rejects_stale_or_unpaired_valid_values(snotel, monkeypatch):
    payload = [{"stationTriplet": snotel.station_triplet, "data": [
        _series("SNWD", _reading("16", 5), _reading("22", 0), _reading("23", -99.9)),
        _series("WTEQ", _reading("16", 1), _reading("22", 0.6, qa="U"), _reading("23", 0.8)),
    ]}]
    monkeypatch.setattr(snotel, "_fetch", lambda _: payload)
    with pytest.raises(DeviceOffline, match="no QC-valid paired SNOTEL readings"):
        snotel.read()


def test_snotel_refuses_wrong_identity_and_units(snotel, monkeypatch):
    snow_depth = _series("SNWD", _reading("22", 0))
    station = {"stationTriplet": "415:CO:SNTL", "data": [snow_depth, _series("WTEQ", _reading("22", 0))]}
    monkeypatch.setattr(snotel, "_fetch", lambda _: [station])
    with pytest.raises(DeviceOffline, match="station identity"):
        snotel.read()
    station["stationTriplet"] = snotel.station_triplet
    snow_depth["stationElement"]["storedUnitCode"] = "cm"
    with pytest.raises(DeviceOffline, match="invalid AWDB hourly SNWD series"):
        snotel.read()
