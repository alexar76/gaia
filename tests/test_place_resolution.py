"""A place, not a device id: weather/air reads for callers that never saw the fleet.

Walked as a stranger on 2026-09-25: `{"city": "Berlin"}` and plain coordinates both
came back ok:true from the demo-site simulator ws-01 (24 °C under a valid signature,
while Berlin read 14 °C). These pin the replacement: a place resolves to a live relay
or the call is refused, and the simulators answer only to their own device_id.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from gaia.app import build_app
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.devices import live as livemod

OM_FORECAST = {"current": {"temperature_2m": 14.3, "relative_humidity_2m": 68.0,
                           "surface_pressure": 1016.6, "wind_speed_10m": 2.65}}
OM_AIR = {"current": {"pm2_5": 41.0, "pm10": 63.0, "carbon_dioxide": 421.0,
                      "us_aqi": 112.0, "european_aqi": 57.0}}

# The hub's MCP search shows only this much of a description (mcp_gateway.py).
HUB_SEARCH_DESCRIPTION_CHARS = 240


def _fake_get(seen: list[str]):
    def fake_get(url, headers=None, timeout=None, **kw):
        seen.append(url)
        if "open-meteo.com/v1/forecast" in url:
            return httpx.Response(200, json=OM_FORECAST, request=httpx.Request("GET", url))
        if "open-meteo.com/v1/air-quality" in url:
            return httpx.Response(200, json=OM_AIR, request=httpx.Request("GET", url))
        return httpx.Response(503, request=httpx.Request("GET", url))
    return fake_get


@pytest.fixture
def seen():
    return []


@pytest.fixture
def live_runtime(tmp_path, monkeypatch, seen):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    monkeypatch.setenv("GAIA_OM_MESH_ENABLED", "1")
    monkeypatch.setenv("GAIA_SC_MESH_ENABLED", "0")
    monkeypatch.setenv("GAIA_STA_ENABLED", "0")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    for var in ("GAIA_OM_BASE_URL", "GAIA_OM_AQ_BASE_URL", "GAIA_OM_LAT", "GAIA_OM_LON",
                "AIFACTORY_CRYPTO_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(livemod.httpx, "get", _fake_get(seen))
    return GatewayRuntime(key_dir=str(tmp_path / "devices"))


@pytest.fixture
def sim_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    return GatewayRuntime(key_dir=str(tmp_path / "devices"))


def _handler(runtime, capability_id):
    spec = build_spec(runtime, public_url="http://gaia.test")
    return next(c for c in spec.capabilities if c.capability_id == capability_id).handler


# ── weather ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("inp, device", [
    ({"city": "Berlin"}, "om-wx-01"),
    ({"city": "Berlin, Germany"}, "om-wx-01"),
    ({"city": "берлин"}, "om-wx-01"),
    ({"city": "Токио"}, "om-wx-tokyo"),
    ({"location": "東京"}, "om-wx-tokyo"),
    ({"latitude": 35.69, "longitude": 139.70}, "om-wx-tokyo"),
    ({"lat": 52.50, "lon": 13.40}, "om-wx-01"),
])
def test_weather_place_reads_the_nearest_live_relay(live_runtime, inp, device):
    out = _handler(live_runtime, "gaia.weather.read@v1")(inp)
    assert out["reading"]["device_id"] == device
    assert out["reading"]["values"]["temperature_c"] == pytest.approx(14.3)
    assert out["resolved"]["device_id"] == device
    assert out["resolved"]["distance_km"] < 10


def test_weather_invoke_for_a_city_is_live_end_to_end(live_runtime, seen):
    with TestClient(build_app(live_runtime, public_url="http://gaia.test")) as client:
        r = client.post("/ai-market/v2/invoke", json={
            "capability_id": "gaia.weather.read@v1", "product_id": "gaia.gateway",
            "input": {"city": "Berlin"},
        })
    data = r.json()
    assert data["ok"] is True, data
    assert data["output"]["reading"]["site"] != "demo-site-1"
    assert data["output"]["reading"]["model"] == "GAIA-WS1 (Open-Meteo relay)"
    assert any("latitude=52.52000&longitude=13.41000" in u for u in seen), seen


def test_weather_without_a_place_is_refused_not_simulated(live_runtime):
    with pytest.raises(ValueError, match="needs a place.*ws-01"):
        _handler(live_runtime, "gaia.weather.read@v1")({})


def test_weather_far_from_every_relay_is_refused_with_the_nearest(live_runtime):
    with pytest.raises(ValueError, match=r"no live weather relay within 75 km.*device_id=om-wx-"):
        _handler(live_runtime, "gaia.weather.read@v1")({"latitude": 0.0, "longitude": -160.0})


def test_weather_unknown_city_is_refused(live_runtime):
    with pytest.raises(ValueError, match="unknown city 'Atlantis'"):
        _handler(live_runtime, "gaia.weather.read@v1")({"city": "Atlantis"})


def test_weather_half_a_coordinate_is_refused(live_runtime):
    with pytest.raises(ValueError, match="together"):
        _handler(live_runtime, "gaia.weather.read@v1")({"latitude": 52.5})


def test_weather_simulator_still_answers_to_its_own_id(live_runtime):
    out = _handler(live_runtime, "gaia.weather.read@v1")({"device_id": "ws-01"})
    assert out["reading"]["device_id"] == "ws-01"
    assert "resolved" not in out


# ── air ───────────────────────────────────────────────────────────────────────


def test_air_city_reads_that_citys_own_relay(live_runtime, seen):
    """A mesh city has its own relay — and its own history for the plausibility checks —
    so it is not routed through the shared coordinate relay."""
    out = _handler(live_runtime, "gaia.air.read@v1")({"city": "Delhi"})
    assert out["reading"]["device_id"] == "om-aq-delhi"
    assert out["reading"]["values"]["pm2_5_ugm3"] == pytest.approx(41.0)
    assert out["resolved"]["device_id"] == "om-aq-delhi"
    assert out["resolved"]["requested"] == "Delhi" and out["resolved"]["matched_place"] == "New Delhi"
    assert any("latitude=28.61390&longitude=77.20900" in u for u in seen), seen
    assert "latitude=52.52000&longitude=13.41000" in live_runtime.fleet.get("om-aq-01").url


def test_berlin_air_reads_the_legacy_relay_without_moving_it(live_runtime, seen):
    out = _handler(live_runtime, "gaia.air.read@v1")({"city": "Berlin"})
    assert out["reading"]["device_id"] == "om-aq-01"
    assert "@" not in out["reading"]["site"]


def test_air_coordinates_read_open_meteo_at_that_point(live_runtime, seen):
    out = _handler(live_runtime, "gaia.air.read@v1")({"latitude": 48.1351, "longitude": 11.582})
    assert out["reading"]["device_id"] == "om-aq-01"
    assert any("latitude=48.13510&longitude=11.58200" in u for u in seen), seen
    # Its own site, so the verifier never judges Munich against Berlin's history ...
    assert out["reading"]["site"].endswith("@48.1351,11.5820")
    relay = live_runtime.fleet.get("om-aq-01")
    # ... and the shared relay is back home afterwards.
    assert "@" not in relay.site and "latitude=52.52000&longitude=13.41000" in relay.url


def test_a_coordinate_reading_is_judged_only_against_its_own_point(live_runtime):
    air = _handler(live_runtime, "gaia.air.read@v1")
    berlin = [air({"device_id": "om-aq-01"}) for _ in range(3)]
    munich = air({"latitude": 48.1351, "longitude": 11.582})
    verifier = live_runtime.verifier
    prior = verifier._prior_history("om-aq-01", munich["reading"])
    assert prior == [], "Berlin's readings must not be the history of a Munich reading"
    assert len(verifier._prior_history("om-aq-01", berlin[-1]["reading"])) == 2


def test_air_without_a_place_is_refused_not_simulated(live_runtime):
    with pytest.raises(ValueError, match="needs a place.*aq-01"):
        _handler(live_runtime, "gaia.air.read@v1")({})


def test_air_simulator_still_answers_to_its_own_id(live_runtime):
    out = _handler(live_runtime, "gaia.air.read@v1")({"device_id": "aq-01"})
    assert out["reading"]["device_id"] == "aq-01"


def test_air_upstream_with_only_nulls_is_offline_not_an_empty_reading(live_runtime, monkeypatch):
    """Self-hosted Open-Meteo without a CAMS model answers 200 with every field null."""
    from gaia.devices.base import DeviceOffline

    nulls = {"current": {"pm2_5": None, "pm10": None, "carbon_dioxide": None,
                         "us_aqi": None, "european_aqi": None}}
    monkeypatch.setattr(livemod.httpx, "get", lambda url, **kw: httpx.Response(
        200, json=nulls, request=httpx.Request("GET", url)))
    with pytest.raises(DeviceOffline, match="no values"):
        _handler(live_runtime, "gaia.air.read@v1")({"city": "Delhi"})
    with pytest.raises(DeviceOffline, match="no values"):
        _handler(live_runtime, "gaia.air.read@v1")({"device_id": "om-aq-01"})


# ── simulator-only GAIA (tests, GAIA_ENABLE_LIVE unset) ──────────────────────


def test_sim_only_empty_call_keeps_the_simulator(sim_runtime):
    assert _handler(sim_runtime, "gaia.weather.read@v1")({})["reading"]["device_id"] == "ws-01"
    assert _handler(sim_runtime, "gaia.air.read@v1")({})["reading"]["device_id"] == "aq-01"


def test_sim_only_refuses_a_real_place(sim_runtime):
    with pytest.raises(ValueError, match="simulators only"):
        _handler(sim_runtime, "gaia.weather.read@v1")({"city": "Berlin"})
    with pytest.raises(ValueError, match="no live air-quality relay"):
        _handler(sim_runtime, "gaia.air.read@v1")({"latitude": 52.5, "longitude": 13.4})


# ── what a stranger's model reads ─────────────────────────────────────────────


@pytest.mark.parametrize("capability_id", ["gaia.weather.read@v1", "gaia.air.read@v1"])
def test_how_to_call_fits_in_the_hub_search_snippet(live_runtime, capability_id):
    spec = build_spec(live_runtime, public_url="http://gaia.test")
    cap = next(c for c in spec.capabilities if c.capability_id == capability_id)
    head = cap.description[:HUB_SEARCH_DESCRIPTION_CHARS]
    assert "latitude+longitude" in head and "city" in head, head
    props = cap.input_schema["properties"]
    assert {"latitude", "longitude", "city", "device_id"} <= set(props)


# --- review 2026-09-25 -------------------------------------------------------------------

@pytest.mark.parametrize("name, device", [
    ("Paris, France", "om-wx-paris"),
    ("Berlin, Germany", "om-wx-01"),
    ("Tokyo, Japan", "om-wx-tokyo"),
    ("New York, NY", "om-wx-newyork"),
    ("Los Angeles, CA, USA", "om-wx-losangeles"),
    ("Москва, Россия", "om-wx-moscow"),
])
def test_a_matching_qualifier_is_accepted(live_runtime, name, device):
    out = _handler(live_runtime, "gaia.weather.read@v1")({"city": name})
    assert out["reading"]["device_id"] == device
    assert out["resolved"]["requested"] == name


@pytest.mark.parametrize("name", ["Paris, Texas", "London, Ontario", "Melbourne, Florida",
                                  "Athens, Georgia", "Moscow, Idaho", "Santiago, Spain"])
def test_a_same_named_city_elsewhere_is_refused_not_answered(live_runtime, name):
    with pytest.raises(ValueError, match="not a city GAIA relays"):
        _handler(live_runtime, "gaia.weather.read@v1")({"city": name})


def test_every_relayed_city_has_its_qualifiers():
    from gaia.capabilities import _CITY_QUALIFIERS
    from gaia.devices.om_mesh import OM_MESH_CITIES

    missing = [c["slug"] for c in OM_MESH_CITIES if c["slug"] not in _CITY_QUALIFIERS]
    assert not missing, f"add a country/state for {missing} in capabilities._CITY_QUALIFIERS"


def test_place_schemas_give_example_builders_a_default():
    """Hephaestus builds its example chain from schema defaults; with none, its first
    hop called gaia.weather.read with {} and a live GAIA refused it."""
    from gaia.capabilities import _AIR_IN, _WEATHER_IN

    assert _WEATHER_IN["properties"]["city"]["default"] == "Berlin"
    assert _AIR_IN["properties"]["city"]["default"] == "Berlin"


def test_a_relay_with_only_its_coordinate_is_offline(live_runtime, monkeypatch):
    """gaia.atmosphere.read's map() fills in lat/lon, so an all-null CAMS answer still
    looked like a reading and was signed and billed."""
    from gaia.devices.base import DeviceOffline
    from gaia.devices.live_p4 import CamsAirComposition

    cams = next((d for d in live_runtime.fleet.devices() if isinstance(d, CamsAirComposition)), None)
    if cams is None:
        pytest.skip("no CAMS relay in this fleet")
    nulls = {"latitude": 52.5, "longitude": 13.4, "current": {
        "aerosol_optical_depth": None, "dust": None, "alder_pollen": None,
        "birch_pollen": None, "grass_pollen": None}}
    monkeypatch.setattr(livemod.httpx, "get", lambda url, **kw: httpx.Response(
        200, json=nulls, request=httpx.Request("GET", url)))
    with pytest.raises(DeviceOffline, match="no values"):
        live_runtime.read(cams.device_id)


def test_an_overridden_sample_that_brings_nothing_is_offline(tmp_path):
    """The guard lives in VirtualDevice.read, so a relay with its own sample() — a dozen
    copied the same null filter — cannot sign {} either."""
    from gaia.clock import SimClock
    from gaia.devices._live_base import LiveDevice
    from gaia.devices.base import DeviceOffline

    class Empty(LiveDevice):
        model = "test"
        fields = {"x": "u"}
        source = "https://example.org"

        def sample(self):
            return {}

    with pytest.raises(DeviceOffline, match="no values"):
        Empty("empty-01", SimClock(), key_dir=str(tmp_path)).read()


def test_live_without_open_meteo_does_not_fall_back_to_the_simulator(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    monkeypatch.setenv("GAIA_OM_WEATHER_ENABLED", "0")
    monkeypatch.setenv("GAIA_OM_AQ_ENABLED", "0")
    monkeypatch.setenv("GAIA_OM_MESH_ENABLED", "0")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    runtime = GatewayRuntime(key_dir=str(tmp_path / "devices"))
    weather = _handler(runtime, "gaia.weather.read@v1")
    with pytest.raises(ValueError, match="needs a place"):
        weather({})
    with pytest.raises(ValueError, match="no city weather relays"):
        weather({"city": "Berlin"})


def test_self_hosted_marine_asks_the_synced_wave_model_by_name(tmp_path, monkeypatch):
    from gaia.clock import SimClock
    from gaia.devices.live_om import OpenMeteoMarine

    monkeypatch.setenv("GAIA_OM_MARINE_BASE_URL", "http://open-meteo:8080")
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        cur = {"wave_height": 1.7} if "models=ncep_gfswave025" in url else {
            "wave_height": None, "sea_surface_temperature": 20.0}
        return httpx.Response(200, json={"current": cur}, request=httpx.Request("GET", url))

    monkeypatch.setattr(livemod.httpx, "get", fake_get)
    marine = OpenMeteoMarine("om-marine-test", SimClock(), latitude=40.25, longitude=-73.16,
                             key_dir=str(tmp_path))
    values = marine.sample()
    assert values == {"sst_c": 20.0, "wave_height_m": 1.7}
    assert len(seen) == 2
