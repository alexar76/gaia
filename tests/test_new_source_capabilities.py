"""The new relays are discoverable while existing source defaults stay stable."""

import pytest

from gaia.capabilities import GatewayRuntime, build_spec
from gaia.devices.live import NOAATideStation, USGSRiverGauge
from gaia.devices.live_bfs_odl import BfsGammaOdl
from gaia.devices.live_chmi_hydro import ChmiRiverGauge
from gaia.devices.live_eccc_smoke import EcccSmokeForecast
from gaia.devices.live_entur import EnturRail
from gaia.devices.live_geonet_coastal import GeonetCoastalGauge
from gaia.devices.live_p5 import DigitrafficRail
from gaia.devices.live_smhi_ocean import SmhiOcean
from gaia.devices.live_swpc_drap import SwpcDRap


def test_new_capability_registration_and_legacy_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    runtime = GatewayRuntime(key_dir=str(tmp_path))
    monkeypatch.setattr(runtime, "read", lambda device_id: {"device_id": device_id})

    def registered_caps():
        return {cap.capability_id: cap for cap in build_spec(runtime).capabilities}

    new = {
        "gaia.radiation.ambient.read@v1": (BfsGammaOdl, "bfs-odl-01", "BfS Gamma ODL"),
        "gaia.hf.absorption.read@v1": (SwpcDRap, "swpc-drap-01", "8,100 native"),
        "gaia.ocean.observations.read@v1": (SmhiOcean, "smhi-ocean-flinten16-8m", "SMHI"),
        "gaia.smoke.forecast.read@v1": (EcccSmokeForecast, "eccc-smoke-toronto", "FireWork"),
    }
    assert not (new.keys() & registered_caps().keys())
    for relay_type, device_id, _ in new.values():
        runtime.fleet.add(relay_type(device_id, runtime.clock, key_dir=tmp_path))
    for cap_id, (_, device_id, term) in new.items():
        cap = registered_caps()[cap_id]
        assert term in cap.description
        assert cap.handler({}) == {"device_id": device_id}
        with pytest.raises(ValueError, match="unsupported device"):
            cap.handler({"device_id": "ws-01"})

    old_sources = (
        ("gaia.river.read@v1", USGSRiverGauge, "usgs-river-01", ChmiRiverGauge,
         "chmi-river-200100", {"station_id": "0-203-1-200100"}, "ČHMÚ"),
        ("gaia.tide.read@v1", NOAATideStation, "noaa-tide-01", GeonetCoastalGauge,
         "geonet-coastal-auct", {"gauge": "AUCT"}, "GeoNet"),
        ("gaia.rail.read@v1", DigitrafficRail, "fintraffic-rail-01", EnturRail,
         "entur-rail-vyg-01", {}, "Entur"),
    )
    for cap_id, old_type, old_id, new_type, new_id, inputs, term in old_sources:
        runtime.fleet.add(old_type(old_id, runtime.clock, key_dir=tmp_path))
        runtime.fleet.add(new_type(new_id, runtime.clock, key_dir=tmp_path, **inputs))
        cap = registered_caps()[cap_id]
        assert term in cap.description
        assert cap.handler({}) == {"device_id": old_id}
        assert cap.handler({"device_id": new_id}) == {"device_id": new_id}


def test_new_sources_can_only_be_read_through_their_advertised_sku(monkeypatch, tmp_path):
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    runtime = GatewayRuntime(key_dir=str(tmp_path))
    monkeypatch.setattr(runtime, "read", lambda device_id: {"device_id": device_id})
    runtime.fleet.add(USGSRiverGauge("usgs-river-01", runtime.clock, key_dir=tmp_path))
    runtime.fleet.add(NOAATideStation("noaa-tide-01", runtime.clock, key_dir=tmp_path))
    sources = (
        (BfsGammaOdl, "bfs-odl-01", {}, "gaia.radiation.ambient.read@v1"),
        (ChmiRiverGauge, "chmi-river-200100", {"station_id": "0-203-1-200100"}, "gaia.river.read@v1"),
        (EcccSmokeForecast, "eccc-smoke-toronto", {}, "gaia.smoke.forecast.read@v1"),
        (EnturRail, "entur-rail-vyg-01", {}, "gaia.rail.read@v1"),
        (GeonetCoastalGauge, "geonet-coastal-auct", {"gauge": "AUCT"}, "gaia.tide.read@v1"),
        (SmhiOcean, "smhi-ocean-flinten16-8m", {}, "gaia.ocean.observations.read@v1"),
        (SwpcDRap, "swpc-drap-01", {}, "gaia.hf.absorption.read@v1"),
    )
    for relay, device_id, kwargs, _ in sources:
        runtime.fleet.add(relay(device_id, runtime.clock, key_dir=tmp_path, **kwargs))
    handlers = {cap.capability_id: cap.handler for cap in build_spec(runtime).capabilities}
    legacy_routes = {"gaia.weather.read@v1", "gaia.river.read@v1", "gaia.tide.read@v1", "gaia.window@v1"}
    for _, device_id, _, correct in sources:
        assert handlers[correct]({"device_id": device_id}) == {"device_id": device_id}
        for wrong in legacy_routes - {correct}:
            with pytest.raises(ValueError, match="must be read through"):
                handlers[wrong]({"device_id": device_id, "n": 500})

    assert handlers["gaia.weather.read@v1"]({"device_id": "ws-01"}) == {"device_id": "ws-01"}
    assert handlers["gaia.tide.read@v1"]({}) == {"device_id": "noaa-tide-01"}
    assert handlers["gaia.river.read@v1"]({}) == {"device_id": "usgs-river-01"}
    assert handlers["gaia.window@v1"]({"n": 2})["count"] == 2
