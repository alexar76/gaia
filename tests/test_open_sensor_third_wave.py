"""Fleet, capability, verification and regional paging for nine open sources."""

from datetime import UTC, datetime, timedelta

import pytest

from gaia.app import _read_cap_for
from gaia.attestation import verify_reading
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.devices.live_p0 import signed_cluster_read
from gaia.plausibility import PHYSICS
from gaia.source_policy import require_approved_source

_ROUTES = {
    "gaia.pollen.read@v1": tuple(
        f"ch-pollen-{code}" for code in (
            "pbe", "pbs", "pbu", "pcf", "pds", "pge", "plo", "pls",
            "plu", "plz", "pmu", "pne", "ppy", "psn", "pzh",
        )
    ),
    "gaia.marine.read@v1": (
        "ie-buoy-m2", "ie-buoy-m3", "ie-buoy-m5", "ie-buoy-m6",
        "au-wave-apollo-bay", "au-wave-wilsons-prom", "au-wave-wooli",
    ),
    "gaia.volcano.read@v1": ("geonet-volcano-01",),
    "gaia.ev.charging.read@v1": ("ndw-ev-charging-01",),
    "gaia.fire.perimeters.read@v1": ("bc-wildfire-01",),
    "gaia.storm.reports.read@v1": ("noaa-spc-storm-reports-01",),
    "gaia.volcanic.ash.read@v1": ("noaa-vaac-ash-01",),
    "gaia.fire.hotspots.read@v1": ("dea-hotspots-01",),
}
_POINTS = {
    "gaia.pollen.read@v1": {"birch_pollen_grains_m3": 0.0, "grass_pollen_grains_m3": 12.0},
    "gaia.marine.read@v1": {"wave_height_m": 1.3, "wave_period_s": 5.5, "wave_direction_deg": 280.0},
}
_CLUSTERS = {
    "gaia.volcano.read@v1": {"latitude": -37.52, "longitude": 177.18, "volcanic_alert_level": 2.0},
    "gaia.ev.charging.read@v1": {"charging_station_count": 2.0, "inventory_total": 2.0},
    "gaia.fire.perimeters.read@v1": {"wildfire_incident_count": 1.0, "inventory_total": 1.0},
    "gaia.storm.reports.read@v1": {"storm_report_count": 1.0, "hail_size_mm": 25.4},
    "gaia.volcanic.ash.read@v1": {"ash_shape_count": 1.0, "base_ft": 14_000.0, "top_ft": 22_000.0},
    "gaia.fire.hotspots.read@v1": {"latitude": -19.5, "longitude": 143.4, "brightness_k": 319.0},
}
_BBOX = {"west": 5.1, "south": 52.0, "east": 5.2, "north": 52.2}


@pytest.fixture
def live_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    for name in (
        "NDW_EV_CHARGING", "BC_WILDFIRE", "METEOSWISS_POLLEN", "IRISH_WEATHER_BUOYS",
        "AODN_WAVE_BUOYS", "GEONET_VOLCANO", "NOAA_SPC", "NOAA_VAAC", "DEA_HOTSPOTS",
    ):
        monkeypatch.setenv(f"GAIA_{name}_ENABLED", "1")
    runtime = GatewayRuntime(key_dir=str(tmp_path))
    return runtime, {cap.capability_id: cap for cap in build_spec(runtime).capabilities}


def _assert_verified(runtime, envelope):
    reading, attestation = envelope["reading"], envelope["attestation"]
    assert verify_reading(reading, attestation)
    verdict = runtime.verifier.check(reading, attestation)
    assert verdict.verified, verdict.summary


def test_28_devices_register_route_and_sign_only_through_their_own_capability(live_gateway, monkeypatch):
    runtime, caps = live_gateway
    expected_ids = {device_id for device_ids in _ROUTES.values() for device_id in device_ids}
    assert len(expected_ids) == 28
    for cap_id, ids in _ROUTES.items():
        capability = caps[cap_id]
        for device_id in ids:
            relay = runtime.fleet.get(device_id)
            require_approved_source(relay.policy_id).require_endpoint(relay.url)
            assert relay.fields.keys() <= PHYSICS.keys(), (device_id, relay.fields.keys() - PHYSICS.keys())
            assert device_id in capability.description
            assert _read_cap_for(relay) == cap_id
            if cap_id in _POINTS:
                monkeypatch.setattr(relay, "sample", lambda values=_POINTS[cap_id]: values.copy())
            else:
                monkeypatch.setattr(
                    relay, "read",
                    lambda device=relay, values=_CLUSTERS[cap_id]: signed_cluster_read(
                        device, [values], numeric_keys=tuple(device.fields),
                    ),
                )
            result = capability.handler({"device_id": device_id})
            assert result["reading"]["device_id"] == device_id
            _assert_verified(runtime, result)
            with pytest.raises(ValueError, match="must be read through"):
                caps["gaia.weather.read@v1"].handler({"device_id": device_id})
            with pytest.raises(ValueError, match="must be read through"):
                caps["gaia.window@v1"].handler({"device_id": device_id, "n": 2})


@pytest.mark.parametrize(("options", "message"), [
    ({"west": 5.1}, "must be supplied together"),
    ({**_BBOX, "west": True}, "must be supplied together"),
    ({**_BBOX, "west": "5.1"}, "must be supplied together"),
    ({**_BBOX, "west": float("nan")}, "invalid bounding box"),
    ({**_BBOX, "east": 5.1}, "invalid bounding box"),
    ({**_BBOX, "north": 91}, "invalid bounding box"),
    ({"west": 0.0, "south": 50.0, "east": 4.0, "north": 54.0}, "limited to 4 square degrees"),
    ({"device_id": "ws-01", **_BBOX}, "unsupported device"),
])
def test_invalid_ndw_bbox_cannot_reach_provider(live_gateway, monkeypatch, options, message):
    runtime, caps = live_gateway
    relay = runtime.fleet.get("ndw-ev-charging-01")

    def should_not_fetch():
        pytest.fail("invalid regional input reached the NDW provider")

    monkeypatch.setattr(relay, "_bulk", should_not_fetch)
    with pytest.raises(ValueError, match=message):
        caps["gaia.ev.charging.read@v1"].handler(options)


def _charge_point(suffix, lat, lon):
    return {
        "station_id": f"NL-AB-{suffix}", "station_key": f"ab-{suffix}",
        "title": "Public EV charging site", "latitude": lat, "longitude": lon,
        "connector_count": 3, "available_count": 2, "max_power_kw": 50,
    }


def test_ndw_version_paging_reaches_adapter_and_preserves_attestations(live_gateway, monkeypatch):
    runtime, caps = live_gateway
    relay = runtime.fleet.get("ndw-ev-charging-01")
    published = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    inventory = [_charge_point("01", 52.1, 5.15), _charge_point("02", 52.11, 5.16),
                 _charge_point("03", 53.1, 6.15)]
    monkeypatch.setattr(relay, "_bulk", lambda: (published, inventory))
    handler = caps["gaia.ev.charging.read@v1"].handler
    first = handler({**_BBOX, "limit": 1})
    page = first["reading"]
    assert [row["station_id"] for row in page["hotspots"]] == ["NL-AB-01"]
    assert page["snapshot_unix"] == published.timestamp()
    assert page["next_offset"] == 1 and page["coverage_complete"] is False
    assert page["values"]["page_offset"] == 0
    _assert_verified(runtime, first)

    options = {**_BBOX, "offset": page["next_offset"], "limit": 1}
    with pytest.raises(ValueError, match="snapshot_unix is required"):
        handler(options)
    with pytest.raises(ValueError, match="source snapshot changed"):
        handler({**options, "snapshot_unix": page["snapshot_unix"] - 1})
    second = handler({**options, "snapshot_unix": page["snapshot_unix"]})
    assert [row["station_id"] for row in second["reading"]["hotspots"]] == ["NL-AB-02"]
    assert second["reading"]["next_offset"] is None
    assert second["reading"]["values"]["page_offset"] == 1
    _assert_verified(runtime, second)

    regional = handler({"west": 6.1, "south": 53.0, "east": 6.2, "north": 53.2})
    assert [row["station_id"] for row in regional["reading"]["hotspots"]] == ["NL-AB-03"]
    assert regional["reading"]["coverage_complete"] is True
    _assert_verified(runtime, regional)
    assert len(runtime.fleet.history(relay.device_id)) == 3
    assert all("hotspots" not in reading for reading in runtime.fleet.history(relay.device_id))


def test_dea_regional_handler_preserves_signed_bounds_and_validates_input(live_gateway, monkeypatch):
    runtime, caps = live_gateway
    relay = runtime.fleet.get("dea-hotspots-01")
    handler = caps["gaia.fire.hotspots.read@v1"].handler
    calls = []

    def read_bbox(west, south, east, north):
        calls.append((west, south, east, north))
        values = {
            "bbox_west_deg": west, "bbox_south_deg": south,
            "bbox_east_deg": east, "bbox_north_deg": north,
            "coverage_complete": 1.0, "hotspot_matched": 0.0,
        }
        return signed_cluster_read(relay, [], numeric_keys=tuple(relay.fields), empty_values=values)

    monkeypatch.setattr(relay, "read_bbox", read_bbox)
    with pytest.raises(ValueError, match="must be supplied together"):
        handler({"west": 129.0})
    with pytest.raises(ValueError, match="invalid bounding box"):
        handler({"west": 170.0, "south": -25.0, "east": -175.0, "north": -15.0})
    for bounds in [(129.0, -24.0, 135.0, -14.0), (143.0, -42.0, 148.0, -36.0)]:
        result = handler(dict(zip(("west", "south", "east", "north"), bounds)))
        _assert_verified(runtime, result)
    assert calls == [(129.0, -24.0, 135.0, -14.0), (143.0, -42.0, 148.0, -36.0)]
