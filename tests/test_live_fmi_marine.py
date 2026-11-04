"""FMI buoy readings retain the observed time, location and data attribution."""

from datetime import UTC, datetime

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_fmi_marine
from gaia.devices.base import DeviceOffline
from gaia.devices.live_fmi_marine import FmiMarine


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 4, 7, tzinfo=UTC).astimezone(tz)


@pytest.fixture
def buoy(tmp_path, monkeypatch):
    monkeypatch.setattr(live_fmi_marine, "datetime", _Clock)
    return FmiMarine("fmi-marine-134220", SimClock(realtime=True), station_id="134220", key_dir=tmp_path)


def _series(parameter, samples, *, station="134220", position="59.24817 20.99833"):
    points = "".join(
        f"<wml:point><wml:MeasurementTVP><wml:time>{time}</wml:time>"
        f"<wml:value>{value}</wml:value></wml:MeasurementTVP></wml:point>"
        for time, value in samples
    )
    return f"""
    <wfs:member><omso:PointTimeSeriesObservation>
      <om:observedProperty xlink:href="https://opendata.fmi.fi/meta?observableProperty=observation&amp;param={parameter}&amp;language=eng"/>
      <om:featureOfInterest>
        <gml:identifier codeSpace="http://xml.fmi.fi/namespace/stationcode/fmisid">{station}</gml:identifier>
        <gml:Point srsName="http://www.opengis.net/def/crs/EPSG/0/4258"><gml:pos>{position}</gml:pos></gml:Point>
      </om:featureOfInterest>
      <om:result><wml:MeasurementTimeseries>{points}</wml:MeasurementTimeseries></om:result>
    </omso:PointTimeSeriesObservation></wfs:member>
    """


def _xml(*series):
    namespaces = " ".join(f'xmlns:{key}="{value}"' for key, value in live_fmi_marine._NS.items())
    return (f'<wfs:FeatureCollection {namespaces} xmlns:xlink="http://www.w3.org/1999/xlink">'
            + "".join(series) + "</wfs:FeatureCollection>")


def test_fmi_buoy_signs_latest_height_and_same_time_values(buoy, monkeypatch):
    observed = "2026-11-04T06:30:00Z"
    old = "2026-11-04T05:30:00Z"
    data = _xml(
        _series("WaveHs", [(observed, 0.6), (old, 1.1), ("2026-11-04T07:30:00Z", 2.2)]),
        _series("WTP", [(observed, 4.2)]),
        _series("ModalWDi", [(observed, 33.0)]),
        _series("TWATER", [(old, 11.8), (observed, "NaN"), ("2026-11-04T06:45:00Z", 12)]),
    )

    def fetch(url, *, max_chars):
        assert "storedquery_id=fmi::observations::wave::timevaluepair&fmisid=134220" in url
        assert "parameters=WaveHs,WTP,ModalWDi,TWATER&timestep=30" in url
        assert max_chars == 120_000
        return data

    monkeypatch.setattr(buoy, "_fetch_text", fetch)
    result = buoy.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["values"] == {
        "wave_height_m": 0.6,
        "wave_period_s": 4.2,
        "wave_direction_deg": 33.0,
        "observation_unix": datetime.fromisoformat(observed).timestamp(),
        "latitude": 59.2482,
        "longitude": 20.9983,
    }
    assert reading["units"]["wave_height_m"] == "m"
    assert "Finnish Meteorological Institute" in reading["attribution"]


@pytest.mark.parametrize("stamp,value,station,position", [
    ("2026-11-04T04:59:00Z", 1.1, "134220", "59.24817 20.99833"),
    ("2026-11-04T06:00:00Z", "NaN", "134220", "59.24817 20.99833"),
    ("2026-11-04T06:00:00Z", 1.1, "134221", "59.24817 20.99833"),
    ("2026-11-04T06:00:00Z", 1.1, "134220", "20.99833 59.24817"),
])
def test_fmi_buoy_requires_fresh_height_from_expected_location(buoy, stamp, value, station, position):
    data = _xml(_series("WaveHs", [(stamp, value)], station=station, position=position),
                _series("TWATER", [("2026-11-04T06:00:00Z", 11.8)]))
    with pytest.raises(DeviceOffline, match="no fresh FMI wave height"):
        buoy.map(data)


def test_fmi_buoy_preserves_calm_height_and_water_temperature(buoy):
    observed = "2026-11-04T06:30:00Z"
    values = buoy.map(_xml(_series("WaveHs", [(observed, 0)]), _series("TWATER", [(observed, 11.8)])))
    assert values["wave_height_m"] == 0
    assert values["sst_c"] == 11.8
