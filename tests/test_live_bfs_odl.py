"""The BfS cluster signs one fresh validated dose rate per station."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_bfs_odl
from gaia.devices.base import DeviceOffline
from gaia.devices.live_bfs_odl import BfsGammaOdl

_NOW = datetime(2026, 11, 4, 7, 32, tzinfo=UTC)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW.astimezone(tz)


@pytest.fixture
def relay(monkeypatch, tmp_path):
    monkeypatch.setattr(live_bfs_odl, "datetime", _Clock)
    return BfsGammaOdl("bfs-odl-01", SimClock(realtime=True), key_dir=tmp_path)


def _feature(station="096721432", end_hour=7, value=0.129, **properties):
    end = _NOW.replace(hour=end_hour, minute=0)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [10.2, 50.16]},
        "properties": {
            "kenn": station, "name": "Rannungen", "site_status": 1, "validated": 1,
            "unit": "µSv/h", "nuclide": "Gamma-ODL-Brutto", "duration": "1h", "value": value,
            "start_measure": (end - timedelta(hours=1)).isoformat(), "end_measure": end.isoformat(),
            **properties,
        },
    }


def _attach(monkeypatch, relay, features, *, total=None):
    document = {
        "type": "FeatureCollection", "features": features,
        "totalFeatures": len(features) if total is None else total,
    }

    def fetch(url, *, max_bytes):
        assert "odlinfo_odl_1h_latest" in url and "EPSG%3A4326" in url
        assert max_bytes == 2_000_000
        return json.dumps(document).encode()

    monkeypatch.setattr(relay, "_fetch_bytes", fetch)


def test_bfs_latest_validated_cluster_preserves_identity_units_and_time(monkeypatch, relay):
    _attach(monkeypatch, relay, [
        _feature(end_hour=6, value=0.127), _feature(),
        _feature("051130004", name="Essen-Bredeney", value=0.106),
        _feature("000000001", end_hour=5), _feature("000000002", end_hour=8),
        _feature("000000003", validated=0), _feature("000000004", validated=True),
        _feature("000000005", site_status=0), _feature("000000006", unit="nSv/h"),
        _feature("000000007", value=float("nan")),
    ])
    result = relay.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["units"]["dose_equivalent_nsv_h"] == "nSv/h"
    assert reading["hotspot_count"] == 2
    assert "Strahlenschutz" in reading["attribution"]
    assert reading["hotspots"][0]["station_id"] == "051130004"
    assert reading["hotspots"][0]["dose_equivalent_nsv_h"] == 106.0
    assert reading["hotspots"][1] == {
        "station_id": "096721432", "station_name": "Rannungen", "dose_equivalent_nsv_h": 129.0,
        "observation_unix": _NOW.replace(minute=0).timestamp(), "observed_at": "2026-11-04T07:00:00+00:00",
        "longitude": 10.2, "latitude": 50.16,
    }


@pytest.mark.parametrize("features,total,reason", [
    ([_feature()], 2, "incomplete"),
    ([_feature(end_hour=5)], None, "last 2 hours"),
    ([_feature(), _feature(value=0.7)], None, "conflicting BfS"),
])
def test_bfs_rejects_partial_stale_and_conflicting_snapshots(monkeypatch, relay, features, total, reason):
    _attach(monkeypatch, relay, features, total=total)
    with pytest.raises(DeviceOffline, match=reason):
        relay.read()
