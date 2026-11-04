"""RAQDPS must attest a fresh model run at the actual returned cell."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_eccc_smoke import EcccSmokeForecast


class CaptureTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 4, 7, 30, tzinfo=UTC).astimezone(tz)


def _forecast():
    return {
        "type": "FeatureCollection", "layer": "RAQDPS.Sfc_PM2.5-WildfireSmokePlume",
        "features": [{
            "type": "Feature", "geometry": {"type": "Point", "coordinates": [-122.2758, 47.6201]},
            "properties": {
                "title_en": "Total concentrations associated with forest fire and vegetation plumes: surface PM2.5 [kg/m³]",
                "value": 5.19316e-8, "time": "2026-11-04T08:00:00Z", "dim_reference_time": "2026-11-04T00:00:00Z",
            },
        }],
    }


def test_eccc_signs_actual_grid_cell_and_times(tmp_path, monkeypatch):
    monkeypatch.setattr("gaia.devices.live_eccc_smoke.datetime", CaptureTime)
    device = EcccSmokeForecast("eccc-smoke-seattle", SimClock(realtime=True), key_dir=tmp_path)
    payload = _forecast()

    def fetch(url):
        query = parse_qs(urlsplit(url).query)
        assert query["request"] == ["GetFeatureInfo"] and query["time"] == ["2026-11-04T08:00:00Z"]
        assert query["bbox"] == ["47.6000,-122.3500,47.6200,-122.3300"]
        assert "dim_reference_time" not in query
        return payload

    monkeypatch.setattr(device, "_fetch", fetch)
    result = device.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["values"] == {
        "forecast_smoke_pm2_5_ugm3": 51.9316,
        "forecast_run_unix": datetime(2026, 11, 4, tzinfo=UTC).timestamp(),
        "forecast_valid_unix": datetime(2026, 11, 4, 8, tzinfo=UTC).timestamp(),
        "latitude": 47.6201, "longitude": -122.2758,
    }
    payload["features"][0]["properties"]["value"] = 0
    assert device.sample()["forecast_smoke_pm2_5_ugm3"] == 0


@pytest.mark.parametrize("change", ["old-run", "wrong-hour", "wrong-cell", "wrong-unit"])
def test_eccc_rejects_misleading_forecasts(tmp_path, monkeypatch, change):
    monkeypatch.setattr("gaia.devices.live_eccc_smoke.datetime", CaptureTime)
    device = EcccSmokeForecast("eccc-smoke-seattle", SimClock(realtime=True), key_dir=tmp_path)
    payload = _forecast()
    feature = payload["features"][0]
    properties = feature["properties"]
    if change == "old-run":
        properties["dim_reference_time"] = "2026-11-03T12:00:00Z"
    elif change == "wrong-hour":
        properties["time"] = "2026-11-04T07:00:00Z"
    elif change == "wrong-cell":
        feature["geometry"]["coordinates"].reverse()
    elif change == "wrong-unit":
        properties["title_en"] = "surface PM2.5 [ug/m³]"
    monkeypatch.setattr(device, "_fetch", lambda _: payload)
    with pytest.raises(DeviceOffline):
        device.read()
