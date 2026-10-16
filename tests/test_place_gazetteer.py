"""Weather/air reads and gaia.place.resolve@v1 with the GeoNames gazetteer loaded.

Without a gazetteer GAIA keeps refusing every place it does not relay
(test_place_resolution.py pins that). With one, "Paris, Texas" stops being a refusal
and becomes the right answer: air quality at 33.66 N, 95.56 W — and weather says how
far the nearest relay is instead of "not a city GAIA relays".
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gaia import gazetteer as gz
from gaia.app import build_app
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.devices import live as livemod

FIXTURE = Path(__file__).parent / "fixtures" / "geonames"

OM_FORECAST = {"current": {"temperature_2m": 14.3, "relative_humidity_2m": 68.0,
                           "surface_pressure": 1016.6, "wind_speed_10m": 2.65}}
OM_AIR = {"current": {"pm2_5": 11.0, "pm10": 19.0, "carbon_dioxide": 421.0,
                      "us_aqi": 46.0, "european_aqi": 21.0}}


@pytest.fixture(scope="module")
def gazetteer_file(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("gaz") / "gazetteer.sqlite"
    gz.build_from_dir(FIXTURE, out)
    return out


@pytest.fixture
def seen():
    return []


def _env(monkeypatch, tmp_path, seen):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    monkeypatch.setenv("GAIA_OM_MESH_ENABLED", "1")
    monkeypatch.setenv("GAIA_SC_MESH_ENABLED", "0")
    monkeypatch.setenv("GAIA_STA_ENABLED", "0")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    for var in ("GAIA_OM_BASE_URL", "GAIA_OM_AQ_BASE_URL", "GAIA_OM_LAT", "GAIA_OM_LON",
                "AIFACTORY_CRYPTO_ENABLED"):
        monkeypatch.delenv(var, raising=False)

    def fake_get(url, headers=None, timeout=None, **kw):
        seen.append(url)
        if "open-meteo.com/v1/forecast" in url:
            return httpx.Response(200, json=OM_FORECAST, request=httpx.Request("GET", url))
        if "open-meteo.com/v1/air-quality" in url:
            return httpx.Response(200, json=OM_AIR, request=httpx.Request("GET", url))
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr(livemod.httpx, "get", fake_get)


@pytest.fixture
def geo_runtime(tmp_path, monkeypatch, seen, gazetteer_file):
    _env(monkeypatch, tmp_path, seen)
    monkeypatch.setenv("GAIA_GAZETTEER_PATH", str(gazetteer_file))
    runtime = GatewayRuntime(key_dir=str(tmp_path / "devices"))
    assert runtime.gazetteer is not None
    return runtime


@pytest.fixture
def plain_runtime(tmp_path, monkeypatch, seen):
    _env(monkeypatch, tmp_path, seen)
    monkeypatch.delenv("GAIA_GAZETTEER_PATH", raising=False)
    return GatewayRuntime(key_dir=str(tmp_path / "devices"))


def _cap(runtime, capability_id):
    spec = build_spec(runtime, public_url="http://gaia.test")
    return next(c for c in spec.capabilities if c.capability_id == capability_id)


# ── weather / air with a gazetteer ────────────────────────────────────────────


def test_air_for_paris_texas_reads_texas_not_france(geo_runtime, seen):
    out = _cap(geo_runtime, "gaia.air.read@v1").handler({"city": "Paris, Texas"})
    resolved = out["resolved"]
    assert resolved["matched_place"] == "Paris, Texas, US"
    assert resolved["place_source"] == "geonames"
    assert resolved["place_attribution"] == "GeoNames (geonames.org) · CC BY 4.0"
    assert resolved["device_id"] == "om-aq-01"
    assert round(resolved["latitude"], 2) == 33.66
    assert any("latitude=33.66094" in u for u in seen), seen


def test_weather_for_a_place_without_a_relay_names_the_nearest(geo_runtime):
    with pytest.raises(ValueError, match=r"no live weather relay within 75 km of Paris, Texas"
                                         r".*device_id=om-wx-"):
        _cap(geo_runtime, "gaia.weather.read@v1").handler({"city": "Paris, Texas"})


def test_an_ambiguous_place_is_refused_with_the_candidates(geo_runtime):
    with pytest.raises(ValueError, match=r"'Springfield' is ambiguous — Springfield, Missouri"):
        _cap(geo_runtime, "gaia.air.read@v1").handler({"city": "Springfield"})


def test_a_relayed_city_still_reads_its_own_relay(geo_runtime):
    out = _cap(geo_runtime, "gaia.weather.read@v1").handler({"city": "Paris, France"})
    assert out["reading"]["device_id"] == "om-wx-paris"
    assert out["resolved"]["place_source"] == "gaia-relay-list"
    assert "geonameid" not in out["resolved"]


def test_a_place_gaia_does_not_relay_reads_air_at_its_own_point(geo_runtime):
    # Kraków is no relayed city, and the qualifier is written in Russian: GeoNames finds
    # the place, and air quality is read at Kraków's own coordinate.
    out = _cap(geo_runtime, "gaia.air.read@v1").handler({"city": "Krakow, Польша"})
    assert out["resolved"]["matched_place"] == "Kraków, Lesser Poland, PL"


def test_without_a_gazetteer_nothing_changes(plain_runtime):
    with pytest.raises(ValueError, match="not a city GAIA relays"):
        _cap(plain_runtime, "gaia.weather.read@v1").handler({"city": "Paris, Texas"})
    ids = {c.capability_id for c in build_spec(plain_runtime).capabilities}
    assert "gaia.place.resolve@v1" not in ids


# ── gaia.place.resolve@v1 ─────────────────────────────────────────────────────


def test_resolve_is_free_and_advertised_only_with_a_gazetteer(geo_runtime):
    cap = _cap(geo_runtime, "gaia.place.resolve@v1")
    assert cap.price_per_call_usd == 0.0
    assert "CC BY 4.0" in cap.description and len(cap.description) <= 600


def test_resolve_a_name_and_what_covers_it(geo_runtime):
    out = _cap(geo_runtime, "gaia.place.resolve@v1").handler({"query": "Paris"})
    assert out["mode"] == "name" and out["chosen"]["label"] == "Paris, Île-de-France, FR"
    weather = out["readable_with"]["gaia.weather.read@v1"]
    assert weather["covered"] and weather["nearest_device_id"] == "om-wx-paris"
    assert out["readable_with"]["gaia.air.read@v1"]["call"]["latitude"] == 48.85341
    assert out["provenance"]["places"] == 65


def test_resolve_far_from_relays_says_so_and_how_to_read_anyway(geo_runtime):
    out = _cap(geo_runtime, "gaia.place.resolve@v1").handler({"query": "Paris, TX"})
    weather = out["readable_with"]["gaia.weather.read@v1"]
    assert weather["covered"] is False
    assert weather["call"] == {"device_id": weather["nearest_device_id"]}


def test_resolve_ambiguous_returns_candidates_not_a_guess(geo_runtime):
    out = _cap(geo_runtime, "gaia.place.resolve@v1").handler({"query": "Springfield", "limit": 3})
    assert out["ambiguous"] and out["chosen"] is None
    assert len(out["candidates"]) == 3 and out["readable_with"] == {}


def test_resolve_a_point(geo_runtime):
    out = _cap(geo_runtime, "gaia.place.resolve@v1").handler(
        {"latitude": 52.52, "longitude": 13.40})
    assert out["mode"] == "point" and out["chosen"]["name"] == "Berlin"
    assert out["chosen"]["distance_km"] < 2


@pytest.mark.parametrize("inp, msg", [
    ({}, "pass query"),
    ({"latitude": 1.0}, "together"),
    ({"latitude": "x", "longitude": 1}, "numbers"),
    ({"query": "Paris, Narnia"}, "names no country"),
    ({"query": "Paris", "limit": "many"}, "limit must be"),
])
def test_resolve_refuses_bad_input_with_a_reason(geo_runtime, inp, msg):
    with pytest.raises(ValueError, match=msg):
        _cap(geo_runtime, "gaia.place.resolve@v1").handler(inp)


def test_resolve_over_http_is_free_signed_and_in_the_manifest(geo_runtime):
    app = build_app(geo_runtime, public_url="http://gaia.test")
    client = TestClient(app)
    tools = {t["capability_id"]: t for t in client.get("/ai-market/v2/manifest").json()["tools"]}
    assert tools["gaia.place.resolve@v1"]["price_per_call_usd"] == 0.0
    resp = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.place.resolve@v1", "input": {"query": "東京"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["output"]["chosen"]["label"] == "Tokyo, JP"
    assert body["receipt"]["signature"]
    assert resp.headers.get("x-provider-signature")
