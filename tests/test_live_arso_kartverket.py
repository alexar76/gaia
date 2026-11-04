"""Preliminary Slovenian river and Norwegian sea level observations."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_arso_hydro, live_kartverket_tide
from gaia.devices.base import DeviceOffline
from gaia.devices.live_arso_hydro import ArsoRiverGauge
from gaia.devices.live_kartverket_tide import KartverketTideGauge
from gaia.fleet import Fleet
from gaia.plausibility import PlausibilityVerifier

_NOW = datetime(2026, 7, 6, 13, 5, tzinfo=UTC)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def clean_snapshots(monkeypatch):
    for module in (live_arso_hydro, live_kartverket_tide):
        monkeypatch.setattr(module, "datetime", _Clock)
        monkeypatch.setattr(module, "_SNAPSHOT", None)


def _arso_station(code="1060", lat="46.68124", lon="15.9953", cet="2026-07-06 12:30", discharge="46.1"):
    return f'''<postaja sifra="{code}" wgs84_sirina="{lat}" wgs84_dolzina="{lon}">
    <datum>2026-07-06 13:30</datum><datum_cet>{cet}</datum_cet>
    <vodostaj>56</vodostaj><pretok>{discharge}</pretok><temp_vode>9.6</temp_vode></postaja>'''


def _arso_document(*stations):
    rows = "".join(stations)
    return f'<arsopodatki verzija="1.5"><vir>Agencija RS za okolje</vir>{rows}</arsopodatki>'.encode()


def test_arso_signs_paired_station_readings_and_fetches_shared_snapshot_once(monkeypatch, tmp_path):
    clock = SimClock(realtime=True)
    first = ArsoRiverGauge("arso-river-1060", clock, station_id="1060", key_dir=tmp_path)
    second = ArsoRiverGauge("arso-river-3660", clock, station_id="3660", key_dir=tmp_path)
    calls = []

    def fetch(self, url, *, max_bytes):
        calls.append(url)
        return _arso_document(_arso_station(), _arso_station("3660", "46.05684", "14.82877", discharge="0"))

    monkeypatch.setattr(ArsoRiverGauge, "_fetch_bytes", fetch)
    result = first.read()
    assert verify_reading(result["reading"], result["attestation"])
    assert result["reading"]["values"] == {
        "gage_height_m": 0.56, "discharge_m3s": 46.1, "water_temperature_c": 9.6,
        "observation_unix": datetime(2026, 7, 6, 11, 30, tzinfo=UTC).timestamp(),
        "latitude": 46.6812, "longitude": 15.9953,
    }
    assert second.sample()["discharge_m3s"] == 0.0
    assert calls == [first.url]

    # Multiple buyers can get the same half-hour sample without a false stuck
    # failure in Pay-on-Verified settlement.
    fleet = Fleet()
    fleet.add(first)
    verifier = PlausibilityVerifier(fleet)
    for _ in range(8):
        result = fleet.read(first.device_id)
        verdict = verifier.check(result["reading"], result["attestation"])
        assert verdict.verified, verdict.summary


@pytest.mark.parametrize("station,reason", [
    (_arso_station(cet="2026-07-06 11:00"), "last 2 hours"),
    (_arso_station(cet="2026-07-06 14:30"), "last 2 hours"),
    (_arso_station(discharge=""), "paired"),
    (_arso_station(lat="46.75"), "coordinates"),
])
def test_arso_refuses_stale_future_missing_and_moved_gauge(monkeypatch, tmp_path, station, reason):
    gauge = ArsoRiverGauge("arso-river-1060", SimClock(), station_id="1060", key_dir=tmp_path)
    monkeypatch.setattr(gauge, "_fetch_bytes", lambda *_args, **_kw: _arso_document(station))
    with pytest.raises(DeviceOffline, match=reason):
        gauge.sample()


def _kart_row(stamp, value, flag="obs"):
    return f'<waterlevel time="2026-07-06T{stamp}:00+00:00" value="{value}" flag="{flag}"/>'


def _kart_document(*rows, datum="NN2000"):
    samples = "".join(rows)
    return f'''<tide><stationdata>
      <location code="BGO" latitude="60.398046" longitude="5.320487">
        <data type="prediction" unit="cm" reflevelcode="{datum}">{_kart_row("13:00", 99)}</data>
        <data type="observation" unit="cm" reflevelcode="{datum}">{samples}</data>
      </location>
      <location code="OSL" latitude="59.908559" longitude="10.734510">
        <data type="observation" unit="cm" reflevelcode="NN2000">{_kart_row("12:00", -7.3)}</data>
      </location>
    </stationdata></tide>'''.encode()


def test_kartverket_signs_latest_measured_nn2000_sample_and_shares_request(monkeypatch, tmp_path):
    clock = SimClock(realtime=True)
    bergen = KartverketTideGauge("kartverket-tide-bgo", clock, station_code="BGO", key_dir=tmp_path)
    oslo = KartverketTideGauge("kartverket-tide-osl", clock, station_code="OSL", key_dir=tmp_path)
    calls = []

    def fetch(self, url, *, max_bytes):
        calls.append(url)
        params = parse_qs(urlsplit(url).query)
        assert params["stationcode"] == ["BGO,OSL,SVG,TOS,TRD"]
        assert params["datatype"] == ["obs"] and params["refcode"] == ["nn2000"]
        assert params["tzone"] == ["0"] and params["interval"] == ["10"]
        return _kart_document(_kart_row("12:10", 99, "est"), _kart_row("12:00", -49.8), _kart_row("11:50", -49.9))

    monkeypatch.setattr(KartverketTideGauge, "_fetch_bytes", fetch)
    result = bergen.read()
    assert verify_reading(result["reading"], result["attestation"])
    assert result["reading"]["values"] == {
        "water_level_nn2000_m": -0.498, "observation_unix": datetime(2026, 7, 6, 12, tzinfo=UTC).timestamp(),
        "latitude": 60.398, "longitude": 5.3205,
    }
    assert result["reading"]["units"]["water_level_nn2000_m"] == "m above NN2000"
    assert oslo.sample()["water_level_nn2000_m"] == -0.073
    assert len(calls) == 1


@pytest.mark.parametrize("document,reason", [
    (_kart_document(_kart_row("12:00", -49.8), datum="CD"), "different datum"),
    (_kart_document(_kart_row("11:00", -49.8), _kart_row("12:00", -50, "est")), "last 2 hours"),
    (_kart_document(_kart_row("12:00", -49.8), _kart_row("12:00", 50)), "conflicting"),
    (b'<!DOCTYPE tide [<!ENTITY levels "bad">]><tide>&levels;</tide>', "invalid Kartverket XML"),
])
def test_kartverket_refuses_wrong_datum_stale_conflicting_and_dtd(monkeypatch, tmp_path, document, reason):
    gauge = KartverketTideGauge("kartverket-tide-bgo", SimClock(), station_code="BGO", key_dir=tmp_path)
    monkeypatch.setattr(gauge, "_fetch_bytes", lambda *_args, **_kw: document)
    with pytest.raises(DeviceOffline, match=reason):
        gauge.sample()
