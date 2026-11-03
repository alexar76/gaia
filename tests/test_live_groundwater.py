from datetime import datetime, timedelta, timezone

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_groundwater import UsgsGroundwater


def test_groundwater_relay_signs_only_fresh_vetted_depths(monkeypatch, tmp_path):
    relay = UsgsGroundwater("usgs-groundwater-01", SimClock(realtime=True), key_dir=str(tmp_path))
    now = datetime.now(timezone.utc)
    current = {
        "monitoring_location_id": "USGS-445227067520101",
        "parameter_code": "72019",
        "statistic_id": "00011",
        "unit_of_measure": "ft",
        "value": "6.69",
        "approval_status": "Provisional",
        "time": (now - timedelta(minutes=15)).isoformat(),
    }
    stale = {**current, "monitoring_location_id": "USGS-424452076081902",
             "time": (now - timedelta(hours=25)).isoformat()}
    invalid = [
        stale,
        {**current, "monitoring_location_id": "USGS-373715086300503", "qualifier": ["TEST"]},
        {**current, "monitoring_location_id": "OTHER-445227067520101"},
        {**current, "unit_of_measure": "m"},
        {**current, "time": (now + timedelta(minutes=15)).isoformat()},
    ]
    payload = {"features": [{"properties": props} for props in [current, *invalid]]}
    monkeypatch.setattr(relay, "_fetch", lambda _url: payload)
    result = relay.read()
    [row] = result["reading"]["hotspots"]
    assert row["station_id"] == "USGS-445227067520101"
    assert row["groundwater_depth_m"] == pytest.approx(2.0391)
    assert row["latitude"] == 44.8742
    assert row["observed_at"] == current["time"]
    assert row["approval_status"] == "Provisional"
    assert result["attestation"]["value"]

    payload["features"] = [{"properties": props} for props in invalid]
    with pytest.raises(DeviceOffline, match="last 24 hours"):
        relay.read()
