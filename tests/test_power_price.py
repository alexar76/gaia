"""Day-ahead electricity prices: SMARD relays, gaia.power.price.read@v1, gaia.power.window@v1.

The fixtures are real SMARD series (CC BY 4.0) cut to a few days: DE-LU across the
weekly chunk boundary of 2026-10-12 (a reading on Sunday night needs both chunks) and
CH, which clears hourly. No network: httpx.get is replaced by a reader of those files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gaia import gazetteer as gz
from gaia.app import _READ_CAP_BY_TYPE, build_app
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.clock import SimClock
from gaia.devices import _live_base
from gaia.devices import live_power as lp
from gaia.devices.base import DeviceOffline
from gaia.devices.live_power import PriceSeries, SmardDayAheadPrice
from gaia.fleet import Fleet
from gaia.plausibility import PlausibilityVerifier
from gaia.power_capabilities import cheapest_window, price_read_handler, resolve_zone, window_handler

FIX = Path(__file__).parent / "fixtures" / "smard"
GEONAMES = Path(__file__).parent / "fixtures" / "geonames"

T_FRI_1007 = 1792145220  # 2026-10-16T10:07:00Z — inside the latest chunk
T_SUN_2300 = 1791759600 - 3600 * 2  # 2026-10-11T20:00:00Z — Sunday, before the boundary
BOUNDARY = 1791756000  # 2026-10-11T22:00:00Z: the chunk that starts Monday 00:00 CEST


def _series(fid: int) -> dict[int, float]:
    out: dict[int, float] = {}
    for f in FIX.glob(f"{fid}_DE_quarterhour_*.json"):
        for t, v in json.loads(f.read_text())["series"]:
            if v is not None:
                out[t // 1000] = v
    return out


@pytest.fixture
def smard_http(monkeypatch):
    calls: list[str] = []
    status = {"code": 200}

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        req = httpx.Request("GET", url)
        if status["code"] != 200:
            return httpx.Response(status["code"], request=req)
        name = url.rsplit("/", 1)[-1]
        fid = url.split("/chart_data/")[1].split("/")[0]
        path = FIX / (f"{fid}_{name}" if name.startswith("index_") else name)
        if not path.exists():
            return httpx.Response(404, request=req)
        return httpx.Response(200, content=path.read_bytes(), request=req,
                              headers={"content-type": "application/json"})

    monkeypatch.setattr(_live_base.httpx, "get", fake_get)
    lp.clear_cache()
    yield {"calls": calls, "status": status}
    lp.clear_cache()


def _device(zone: str, t: float, tmp_path) -> SmardDayAheadPrice:
    return SmardDayAheadPrice(lp.device_id_for(zone), SimClock(start_epoch=t), zone=zone,
                              site=f"power-zone-{zone.lower()}", key_dir=str(tmp_path))


# ── the relay ─────────────────────────────────────────────────────────────────


def test_reading_is_the_published_price_of_the_current_quarter_hour(smard_http, tmp_path):
    values = _device("DE-LU", T_FRI_1007, tmp_path).sample()
    published = _series(4169)
    start = T_FRI_1007 - T_FRI_1007 % 900
    assert values["price_eur_mwh"] == published[start]
    assert values["interval_start_unix"] == start
    ahead = [p for t, p in published.items() if start <= t < T_FRI_1007 + 86400]
    assert values["price_min_24h_eur_mwh"] == min(ahead)
    assert values["price_max_24h_eur_mwh"] == max(ahead)
    assert values["price_mean_24h_eur_mwh"] == pytest.approx(sum(ahead) / len(ahead))
    assert values["resolution_min"] == 15.0
    assert values["horizon_end_unix"] == max(published) + 900


def test_sunday_night_reads_across_the_weekly_chunk_boundary(smard_http, tmp_path):
    device = _device("DE-LU", T_SUN_2300, tmp_path)
    series = device.series()
    assert series.points[0][0] < BOUNDARY < series.points[-1][0]
    assert len(series.sources) == 2
    values = device.sample()
    # the next 24 h span both chunks: 2 h before the boundary, 22 h after
    assert values["price_eur_mwh"] == _series(4169)[T_SUN_2300]


def test_an_hourly_market_is_still_read_per_quarter_hour(smard_http, tmp_path):
    values = _device("CH", T_FRI_1007, tmp_path).sample()
    published = _series(259)
    hour = T_FRI_1007 - T_FRI_1007 % 3600
    assert values["price_eur_mwh"] == published[hour] == published[hour + 900]


def test_prices_that_end_before_now_are_stale_and_offline(smard_http, tmp_path):
    late = max(_series(4169)) + 900 + 60
    with pytest.raises(DeviceOffline, match="stale"):
        _device("DE-LU", late, tmp_path).sample()


@pytest.mark.parametrize("code", [404, 429, 503])
def test_an_upstream_error_is_offline_never_a_reading(smard_http, tmp_path, code):
    smard_http["status"]["code"] = code
    with pytest.raises(DeviceOffline, match=f"HTTP {code}"):
        _device("DE-LU", T_FRI_1007, tmp_path).sample()


def test_series_are_cached_for_fifteen_minutes(smard_http, tmp_path):
    device = _device("DE-LU", T_FRI_1007, tmp_path)
    device.sample()
    n = len(smard_http["calls"])
    device.sample()
    device.series()
    assert len(smard_http["calls"]) == n


def test_the_attested_reading_passes_plausibility(smard_http, tmp_path):
    fleet = Fleet()
    fleet.add(_device("DE-LU", T_FRI_1007, tmp_path))
    out = fleet.read("smard-price-de-lu")
    verdict = PlausibilityVerifier(fleet).check(out["reading"], out["attestation"])
    assert verdict.verified and verdict.score == 1.0, verdict.summary
    assert out["reading"]["attribution"] == "Bundesnetzagentur | SMARD.de · CC BY 4.0"


def test_a_price_beyond_the_market_limits_fails_plausibility(smard_http, tmp_path):
    fleet = Fleet()
    device = _device("DE-LU", T_FRI_1007, tmp_path)
    fleet.add(device)
    device.inject_fault("spike", fields=["price_eur_mwh"], magnitude=50_000)
    out = fleet.read("smard-price-de-lu")
    verdict = PlausibilityVerifier(fleet).check(out["reading"], out["attestation"])
    assert not verdict.verified and "bounds:price_eur_mwh" in verdict.summary


def test_every_zone_is_licensed_and_allowlisted(tmp_path):
    from gaia.devices._policy import ALLOWED_HOSTS
    from gaia.source_policy import require_approved_source

    policy = require_approved_source("smard")
    assert "www.smard.de" in ALLOWED_HOSTS and policy.licence == "CC BY 4.0"
    assert not ({z.upper() for z in lp.SMARD_ZONES} & lp.RESTRICTED_ZONES)
    for zone in lp.SMARD_ZONES:
        SmardDayAheadPrice(lp.device_id_for(zone), SimClock(), zone=zone,
                           key_dir=str(tmp_path))  # the constructor checks the endpoint


def test_registration_follows_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_SMARD_ZONES", "de-lu, FR,nope")
    fleet = Fleet()
    assert lp.register_power_prices(fleet, SimClock(), key_dir=str(tmp_path)) == 2
    monkeypatch.setenv("GAIA_SMARD_ENABLED", "0")
    assert lp.register_power_prices(Fleet(), SimClock(), key_dir=str(tmp_path)) == 0


def test_wot_maps_the_relay_to_its_capability():
    assert _READ_CAP_BY_TYPE["SmardDayAheadPrice"] == "gaia.power.price.read@v1"


# ── naming a zone ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def gazetteer(tmp_path_factory):
    out = tmp_path_factory.mktemp("gaz") / "g.sqlite"
    gz.build_from_dir(GEONAMES, out)
    return gz.Gazetteer(out)


class _Rt:
    def __init__(self, gazetteer=None):
        self.gazetteer = gazetteer


@pytest.mark.parametrize("raw, zone", [
    ({"zone": "de-lu"}, "DE-LU"),
    ({"zone": "IT-NORTH"}, "IT-North"),
    ({"device_id": "smard-price-dk2"}, "DK2"),
    ({"country": "DE"}, "DE-LU"),
    ({"country": "lu"}, "DE-LU"),
    ({"country": "France"}, "FR"),
    ({"country": "Германия"}, "DE-LU"),
    ({"city": "Paris"}, "FR"),
    ({"city": "Köln"}, "DE-LU"),
    ({"city": "Krakow, Польша"}, "PL"),
])
def test_a_zone_can_be_named_many_ways(gazetteer, raw, zone):
    assert resolve_zone(raw, _Rt(gazetteer)) == zone


@pytest.mark.parametrize("raw, msg", [
    ({"country": "DK"}, "several bidding zones.*DK1, DK2"),
    ({"country": "Italy"}, "several bidding zones.*IT-North"),
    ({"zone": "SE3"}, "not offered.*private and internal use only"),
    ({"zone": "it-south"}, "not offered"),
    ({"country": "ES"}, "not offered"),
    ({"zone": "XX"}, "unknown bidding zone"),
    ({"city": "Toronto"}, "no relayed bidding zone"),
    ({"device_id": "om-wx-01"}, "not a power price relay"),
    ({}, "name the bidding zone"),
])
def test_a_zone_that_cannot_be_sold_is_refused_with_the_reason(gazetteer, raw, msg):
    with pytest.raises(ValueError, match=msg):
        resolve_zone(raw, _Rt(gazetteer))


def test_a_city_needs_a_gazetteer():
    with pytest.raises(ValueError, match="no gazetteer"):
        resolve_zone({"city": "Lyon"}, _Rt(None))


# ── the cheapest window (pure) ────────────────────────────────────────────────


def _syn(prices: list[float], start: int = 1_800_000_000, step: int = 900,
         gaps: tuple[int, ...] = ()) -> PriceSeries:
    pts, t = [], start
    for i, p in enumerate(prices):
        if i in gaps:
            t += step
        pts.append((t, p))
        t += step
    return PriceSeries("DE-LU", tuple(pts), step, ("synthetic",))


def test_the_window_with_the_lowest_mean_wins():
    s = _syn([100, 90, 50, 40, 45, 120, 30, 200])
    w = cheapest_window(s, now=s.points[0][0] + 10, duration_s=3 * 900)
    assert w["best_start"] == s.points[2][0] and w["best_mean"] == pytest.approx(45.0)
    assert w["run_now_mean"] == pytest.approx(80.0)


def test_ties_go_to_the_earlier_window():
    s = _syn([10, 10, 10, 10])
    w = cheapest_window(s, now=s.points[0][0], duration_s=1800)
    assert w["best_start"] == s.points[0][0]


def test_negative_prices_are_the_cheapest():
    s = _syn([50, 40, -5, -20, 60])
    w = cheapest_window(s, now=s.points[0][0], duration_s=1800)
    assert w["best_mean"] == pytest.approx(-12.5)


def test_a_duration_rounds_up_to_whole_intervals():
    s = _syn([10, 20, 30, 40])
    assert cheapest_window(s, now=s.points[0][0], duration_s=901)["k"] == 2


def test_a_gap_in_the_series_breaks_a_window():
    s = _syn([5, 5, 100, 100], gaps=(1,))  # a missing interval between the two 5s
    w = cheapest_window(s, now=s.points[0][0], duration_s=1800)
    assert w["best_mean"] == pytest.approx(52.5)


def test_the_window_starts_at_the_current_interval_not_in_the_past():
    s = _syn([1, 1, 50, 60, 70])
    w = cheapest_window(s, now=s.points[2][0] + 1, duration_s=900)
    assert w["best_start"] == s.points[2][0]


def test_within_hours_limits_the_search():
    s = _syn([50, 50, 50, 50, 1, 1])
    w = cheapest_window(s, now=s.points[0][0], duration_s=900, within_s=4 * 900)
    assert w["best_mean"] == 50


def test_a_window_longer_than_the_horizon_is_refused():
    s = _syn([1, 2, 3])
    with pytest.raises(ValueError, match="published horizon holds 0.75 h.*13:00 CET"):
        cheapest_window(s, now=s.points[0][0], duration_s=3600)


# ── the capabilities ──────────────────────────────────────────────────────────


@pytest.fixture
def power_runtime(tmp_path, monkeypatch, smard_http):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    monkeypatch.setenv("GAIA_SMARD_ZONES", "DE-LU,CH")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    monkeypatch.delenv("GAIA_GAZETTEER_PATH", raising=False)
    runtime = GatewayRuntime(key_dir=str(tmp_path / "devices"), autotick=False)
    runtime.clock = SimClock(start_epoch=T_FRI_1007)
    for d in runtime.fleet.devices():
        d.clock = runtime.clock
    return runtime


def _cap(runtime, cid):
    return next(c for c in build_spec(runtime).capabilities if c.capability_id == cid)


def test_both_skus_are_listed_with_their_prices(power_runtime):
    read = _cap(power_runtime, "gaia.power.price.read@v1")
    window = _cap(power_runtime, "gaia.power.window@v1")
    assert read.price_per_call_usd == 0.001 and window.price_per_call_usd == 0.002
    assert "DE-LU, CH" in read.description
    assert len(read.description) <= 600 and len(window.description) <= 600


def test_the_price_read_names_the_zone(power_runtime):
    out = price_read_handler(power_runtime)({"zone": "CH"})
    assert out["reading"]["device_id"] == "smard-price-ch"
    assert out["zone"]["smard_series"] == 259 and out["zone"]["timezone"] == "Europe/Zurich"


def test_a_zone_not_enabled_here_is_refused(power_runtime):
    with pytest.raises(ValueError, match="FR is not enabled"):
        price_read_handler(power_runtime)({"zone": "FR"})


def test_the_window_answer_can_be_recomputed_by_the_buyer(power_runtime):
    out = window_handler(power_runtime)({"zone": "DE-LU", "duration_hours": 2, "load_kw": 11})
    series = out["series"]
    assert hashlib.sha256(json.dumps(series, separators=(",", ":")).encode()).hexdigest() \
        == out["series_sha256"]
    k = out["best"]["intervals"]
    assert k == 8
    means = [sum(p for _t, p in series[i:i + k]) / k for i in range(len(series) - k + 1)]
    assert out["best"]["mean_eur_mwh"] == pytest.approx(min(means), abs=1e-4)
    assert out["run_now"]["mean_eur_mwh"] == pytest.approx(means[0], abs=1e-4)
    # 11 kW for 2 h at the window's prices, EUR/MWh → EUR
    assert out["best"]["energy_cost_eur_wholesale"] == pytest.approx(
        min(means) * 11 * 2 / 1000, abs=1e-3)
    assert out["saving_eur_mwh"] == pytest.approx(means[0] - min(means), abs=1e-3)
    assert out["source"]["attribution"].endswith("CC BY 4.0")
    assert out["best"]["start_local"] is None or out["best"]["start_local"].endswith("+02:00")


@pytest.mark.parametrize("inp, msg", [
    ({"zone": "DE-LU", "duration_hours": 0}, "duration_hours must be"),
    ({"zone": "DE-LU", "duration_hours": 30}, "duration_hours must be"),
    ({"zone": "DE-LU", "within_hours": 100}, "within_hours must be"),
    ({"zone": "DE-LU", "load_kw": -1}, "load_kw must be"),
    ({"zone": "DE-LU", "duration_hours": "long"}, "must be numbers"),
    ({"zone": "DE-LU", "duration_hours": 20}, "published horizon"),
])
def test_window_refuses_bad_input_with_a_reason(power_runtime, inp, msg):
    with pytest.raises(ValueError, match=msg):
        window_handler(power_runtime)(inp)


def test_without_relays_the_skus_are_not_advertised(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    ids = {c.capability_id for c in build_spec(GatewayRuntime(key_dir=str(tmp_path))).capabilities}
    assert not {"gaia.power.price.read@v1", "gaia.power.window@v1"} & ids


def test_over_http_the_price_is_paid_signed_and_in_the_manifest(power_runtime):
    client = TestClient(build_app(power_runtime, public_url="http://gaia.test"))
    tools = {t["capability_id"]: t for t in client.get("/ai-market/v2/manifest").json()["tools"]}
    assert tools["gaia.power.window@v1"]["price_per_call_usd"] == 0.002
    resp = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.power.price.read@v1", "input": {"zone": "DE-LU"}})
    assert resp.status_code == 200 and resp.json()["ok"]
    assert resp.json()["receipt"]["price_usd"] == 0.001
    assert resp.headers.get("x-provider-signature")
    wot = client.get("/wot/smard-price-de-lu").json()
    assert "gaia.power.price.read@v1" in json.dumps(wot)
