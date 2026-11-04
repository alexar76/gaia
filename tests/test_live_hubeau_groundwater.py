from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from gaia.attestation import verify_reading
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_hubeau_groundwater import HubEauGroundwater
from gaia.devices.live_groundwater import UsgsGroundwater


def test_hubeau_groundwater_keeps_distinct_datums_and_rejects_bad_telemetry(monkeypatch, tmp_path):
    relay = HubEauGroundwater("hubeau-groundwater-01", SimClock(realtime=True), key_dir=str(tmp_path))
    now = datetime.now(timezone.utc)
    observed = now - timedelta(hours=1)
    current = {
        "bss_id": "BSS001VZGZ",
        "latitude": 45.2297273884,
        "longitude": -0.7490575762,
        "date_mesure": observed.isoformat(),
        "date_maj": now.date().isoformat(),
        "profondeur_nappe": -15.55,
        "niveau_eau_ngf": 21.53,
    }
    bad_rows = [
        {**current, "date_mesure": (now - timedelta(hours=25)).isoformat()},
        {**current, "date_mesure": (now + timedelta(hours=1)).isoformat()},
        {**current, "longitude": -0.7},
        {**current, "bss_id": "BSS0000000"},
        {**current, "niveau_eau_ngf": float("nan")},
        {**current, "date_maj": "2020-01-01"},
    ]
    payload = {"count": len(bad_rows) + 1, "next": None, "data": [*bad_rows, current]}

    def fetch(url):
        params = parse_qs(urlsplit(url).query)
        assert current["bss_id"] in params["bss_id"][0].split(",")
        assert params["date_debut_mesure"] == [(now - timedelta(hours=24)).date().isoformat()]
        return payload

    monkeypatch.setattr(relay, "_fetch", fetch)
    result = relay.read()
    reading = result["reading"]
    [well] = reading["hotspots"]
    assert well["groundwater_reference_depth_m"] == -15.55
    assert well["groundwater_level_ngf_m"] == 21.53
    assert "groundwater_depth_m" not in well
    assert well["source_updated_date"] == current["date_maj"]
    assert well["observed_at"] == observed.isoformat()
    assert well["approval_status"] == "Raw telemetry"
    assert verify_reading(reading, result["attestation"])

    payload.update(count=len(bad_rows), data=bad_rows)
    with pytest.raises(DeviceOffline, match="last 24 hours"):
        relay.read()
    payload["next"] = "https://example.invalid/"
    with pytest.raises(DeviceOffline, match="incomplete"):
        relay.read()


def test_groundwater_capability_selects_only_registered_groundwater_devices(monkeypatch, tmp_path):
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    runtime = GatewayRuntime(key_dir=str(tmp_path))
    france = "hubeau-groundwater-01"
    usa = "usgs-groundwater-01"
    runtime.fleet.add(HubEauGroundwater(france, runtime.clock, key_dir=str(tmp_path)))
    monkeypatch.setattr(runtime, "read", lambda device_id: {"device_id": device_id})

    def capability():
        return next(cap for cap in build_spec(runtime).capabilities
                    if cap.capability_id == "gaia.groundwater.read@v1")

    assert capability().handler({}) == {"device_id": france}
    with pytest.raises(ValueError, match="unsupported device"):
        capability().handler({"device_id": "ws-01"})
    runtime.fleet.add(UsgsGroundwater(usa, runtime.clock, key_dir=str(tmp_path)))
    assert capability().handler({}) == {"device_id": usa}
    assert capability().handler({"device_id": france}) == {"device_id": france}
