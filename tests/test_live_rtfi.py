"""RT-FI must bind each surveyed point to a fresh matching USGS gage series."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_rtfi import UsgsFloodImpacts


def test_rtfi_checks_exact_fresh_gage_series(tmp_path, monkeypatch):
    device = UsgsFloodImpacts("usgs-rtfi-01", SimClock(realtime=True), key_dir=str(tmp_path))
    observed = datetime.now(UTC) - timedelta(minutes=15)
    series_id = "1d9203dc672042e79bb8d05f456a14c7"
    point = {
        "properties": {
            "id": 18127, "active": True, "is_flooding": True, "plot_param": "62614",
            "plot_series_id": series_id, "nwis_id": "04224000", "rp_elevation": 584,
            "unit": "ft",
        },
        "geometry": {"type": "Point", "coordinates": [-79.1, 42.2]},
    }
    sample = {
        "time_series_id": series_id, "monitoring_location_id": "USGS-04224000",
        "parameter_code": "62614", "statistic_id": "00011", "value": "588.68",
        "unit_of_measure": "ft", "time": observed.isoformat(), "approval_status": "Provisional",
    }

    def fetch(url):
        if url == device.url:
            return {"type": "FeatureCollection", "features": [point]}
        query = parse_qs(urlsplit(url).query)
        assert query["filter-lang"] == ["cql2-text"]
        assert query["filter"] == [f"time_series_id IN ('{series_id}')"]
        return {"type": "FeatureCollection", "features": [{"properties": sample}]}

    monkeypatch.setattr(device, "_fetch", fetch)
    result = device.read()
    reading = result["reading"]
    [pin] = reading["hotspots"]
    assert verify_reading(reading, result["attestation"])
    assert pin["rtfi_point_id"] == 18127
    assert pin["longitude"] == -79.1 and pin["latitude"] == 42.2
    assert pin["flood_exceedance_m"] == pytest.approx(1.4265)
    assert pin["observed_at"] == observed.isoformat()
    assert pin["claim_class"] == "surveyed_flood_threshold_exceedance"

    for field, replacement in (
        ("time", (observed - timedelta(hours=3)).isoformat()),
        ("monitoring_location_id", "USGS-08165500"),
        ("parameter_code", "00065"),
        ("value", "583"),
        ("qualifier", "TEST"),
    ):
        original = sample.get(field)
        sample[field] = replacement
        with pytest.raises(DeviceOffline):
            device.read()
        sample[field] = original

    monkeypatch.setattr(device, "_fetch", lambda _: {"type": "FeatureCollection", "features": []})
    with pytest.raises(DeviceOffline):
        device.read()
