"""Capability, pricing boundary, and WoT registration for seven open feeds."""

import pytest

from gaia.app import _read_cap_for
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.devices.live_p0 import signed_cluster_read
from gaia.plausibility import PHYSICS


def test_seven_sources_register_and_only_use_their_own_read_capability(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    runtime = GatewayRuntime(key_dir=str(tmp_path))
    monkeypatch.setattr(runtime, "read", lambda device_id: {"device_id": device_id})
    specs = {cap.capability_id: cap for cap in build_spec(runtime).capabilities}
    routes = {
        "gaia.river.read@v1": [f"arso-river-{code}" for code in ("1060", "3660", "5078", "8080")],
        "gaia.tide.read@v1": [f"kartverket-tide-{code}" for code in ("bgo", "osl", "svg", "tos", "trd")],
        "gaia.road.events.read@v1": ["toronto-road-restrictions-01"],
        "gaia.bikeshare.read@v1": ["toronto-bikeshare-01"],
        "gaia.power.outages.read@v1": ["ukpn-live-faults-01"],
        "gaia.ocean.drifters.read@v1": ["aoml-drifters-01"],
        "gaia.bridge.clearance.read@v1": [
            f"noaa-airgap-{code}" for code in ("8726371", "8545556", "8575432")
        ],
    }
    for cap_id, device_ids in routes.items():
        capability = specs[cap_id]
        assert capability.price_per_call_usd in (0.001, 0.002)
        for device_id in device_ids:
            device = runtime.fleet.get(device_id)
            assert device_id in capability.description
            assert _read_cap_for(device) == cap_id
            assert device.fields.keys() <= PHYSICS.keys()
            assert capability.handler({"device_id": device_id}) == {"device_id": device_id}
            with pytest.raises(ValueError, match="must be read through"):
                specs["gaia.weather.read@v1"].handler({"device_id": device_id})
            with pytest.raises(ValueError, match="must be read through"):
                specs["gaia.window@v1"].handler({"device_id": device_id, "n": 500})

    assert specs["gaia.river.read@v1"].handler({}) == {"device_id": "usgs-river-01"}
    assert specs["gaia.tide.read@v1"].handler({}) == {"device_id": "noaa-tide-01"}
    assert specs["gaia.road.events.read@v1"].handler({}) == {"device_id": "fintraffic-road-announcements-01"}
    for cap_id in routes.keys() - {"gaia.river.read@v1", "gaia.tide.read@v1"}:
        with pytest.raises(ValueError, match="unsupported device"):
            specs[cap_id].handler({"device_id": "ws-01"})


def test_drifter_headline_can_change_to_another_buoy_without_failing_verification(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    runtime = GatewayRuntime(key_dir=str(tmp_path))
    relay = runtime.fleet.get("aoml-drifters-01")
    rows = iter([
        {"latitude": 20, "longitude": -34, "sst_c": 26.4, "sea_level_pressure_hpa": 1012},
        {"latitude": -72, "longitude": 120, "sst_c": -2.2, "sea_level_pressure_hpa": 981},
    ])
    monkeypatch.setattr(relay, "read", lambda: signed_cluster_read(relay, [next(rows)], numeric_keys=tuple(relay.fields)))
    for _ in range(2):
        result = runtime.read(relay.device_id)
        verdict = runtime.verifier.check(result["reading"], result["attestation"])
        assert verdict.verified, verdict.summary
