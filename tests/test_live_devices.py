"""Live relay devices — mapping, failure semantics, SSRF, and new Tier A/B APIs.

All HTTP is mocked: ``gaia.devices.live.httpx.get`` is monkeypatched. Nothing
here touches the network.
"""

from __future__ import annotations

import httpx
import pytest

from gaia.clock import SimClock
from gaia.devices import live as livemod
from gaia.devices.base import DeviceOffline
from gaia.devices.live import (
    NDBCBuoy,
    NOAATideStation,
    NWSStation,
    OpenAQLocation,
    OpenMeteoAirQuality,
    OpenMeteoMarine,
    OpenMeteoWeather,
    OpenSenseMapBox,
    SensorThingsDatastream,
    UKCarbonIntensity,
    USGSEarthquake,
    USGSRiverGauge,
    _assert_url_allowed,
    build_live_fleet,
)
from gaia.fleet import Fleet
from gaia.plausibility import PlausibilityVerifier

# ── Canned upstream payloads ──────────────────────────────────────────────────

NWS_FULL = {
    "properties": {
        "temperature": {"unitCode": "wmoUnit:degC", "value": 12.4},
        "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 61.0},
        "barometricPressure": {"unitCode": "wmoUnit:Pa", "value": 101_800},
        "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 18.0},
    }
}

NWS_NULL_TEMP = {
    "properties": {
        "temperature": {"unitCode": "wmoUnit:degC", "value": None},
        "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 55.0},
        "barometricPressure": {"unitCode": "wmoUnit:Pa", "value": 101_300},
        "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 10.8},
    }
}

OSM_BOX = {
    "name": "demo box",
    "sensors": [
        {"title": "PM2.5", "unit": "µg/m³", "lastMeasurement": {"value": "7.5"}},
        {"title": "PM10", "unit": "µg/m³", "lastMeasurement": {"value": "12.0"}},
        {"title": "CO2", "unit": "ppm", "lastMeasurement": {"value": "615"}},
        {"title": "VOC", "unit": "index", "lastMeasurement": {"value": "120"}},
        {"title": "Temperature", "unit": "°C", "lastMeasurement": {"value": "13.2"}},
        {"title": "PM1", "unit": "µg/m³"},
    ],
}

STA_OBS = {
    "@iot.count": 1,
    "value": [
        {"@iot.id": 999, "phenomenonTime": "2026-07-16T10:00:00Z",
         "result": 8.3, "resultTime": "2026-07-16T10:00:05Z"}
    ],
}

OM_WEATHER = {
    "current": {
        "time": "2026-08-05T07:30",
        "temperature_2m": 24.7,
        "relative_humidity_2m": 80,
        "surface_pressure": 1007.9,
        "wind_speed_10m": 2.2,
    }
}

OM_AQ = {
    "current": {"time": "2026-08-05T07:00", "pm2_5": 14.4, "pm10": 17.9, "carbon_dioxide": 477}
}

UK_CI = {
    "data": [{
        "from": "2026-08-05T07:00Z", "to": "2026-08-05T07:30Z",
        "intensity": {"forecast": 48, "actual": 59, "index": "low"},
    }]
}

UK_CI_FORECAST_ONLY = {
    "data": [{"intensity": {"forecast": 72, "actual": None, "index": "moderate"}}]
}

USGS = {
    "features": [{
        "properties": {"mag": 5.2, "place": "Auckland Islands"},
        "geometry": {"coordinates": [164.0845, -49.5336, 10.0]},
    }]
}

NOAA_TIDE = {
    "metadata": {"id": "8518750", "name": "The Battery"},
    "data": [{"t": "2026-08-05 07:24", "v": "1.143", "s": "0.016", "f": "1,0,0,0", "q": "p"}],
}

OPENAQ = {
    "results": [
        {"value": 11.2, "parameter": {"name": "pm25"}},
        {"value": 18.0, "parameter": {"name": "pm10"}},
    ]
}

USGS_RIVER = {
    "value": {
        "timeSeries": [
            {
                "variable": {"variableCode": [{"value": "00060"}]},
                "values": [{"value": [{"value": "35300"}]}],
            },
            {
                "variable": {"variableCode": [{"value": "00065"}]},
                "values": [{"value": [{"value": "5.42"}]}],
            },
        ]
    }
}

NDBC_TXT = """#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft
2026 08 06 12 50 210  6.2  7.8   1.40   8.0   5.5 200 1015.2  22.1  21.4  18.0   MM   MM    MM
"""

OM_MARINE = {
    "current": {
        "time": "2026-08-06T12:00",
        "wave_height": 1.25,
        "sea_surface_temperature": 20.8,
    }
}


def _get_returning(payload, status: int = 200):
    def fake_get(url, headers=None, timeout=None, **kw):
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))
    return fake_get


def _get_returning_text(body: str, status: int = 200):
    def fake_get(url, headers=None, timeout=None, **kw):
        return httpx.Response(status, text=body, request=httpx.Request("GET", url))
    return fake_get


def _get_dispatch(by_needle: dict[str, dict]):
    def fake_get(url, headers=None, timeout=None, **kw):
        for needle, payload in by_needle.items():
            if needle in url:
                return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        return httpx.Response(404, json={}, request=httpx.Request("GET", url))
    return fake_get


# ── Legacy three ──────────────────────────────────────────────────────────────


def test_nws_mapper_units(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(NWS_FULL))
    v = NWSStation("nws-t", SimClock(), station="KNYC", key_dir=tmp_path).sample()
    assert v["temperature_c"] == pytest.approx(12.4)
    assert v["humidity_pct"] == pytest.approx(61.0)
    assert v["pressure_hpa"] == pytest.approx(1018.0)
    assert v["wind_mps"] == pytest.approx(5.0)


def test_nws_null_field_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(NWS_NULL_TEMP))
    v = NWSStation("nws-t", SimClock(), station="KNYC", key_dir=tmp_path).sample()
    assert "temperature_c" not in v
    assert set(v) == {"humidity_pct", "pressure_hpa", "wind_mps"}


def test_nws_rejects_injection_station_id(tmp_path):
    with pytest.raises(ValueError):
        NWSStation("nws-t", SimClock(), station="../etc/passwd", key_dir=tmp_path)
    with pytest.raises(ValueError):
        NWSStation("nws-t", SimClock(), station="KNYC?x=1", key_dir=tmp_path)


def test_opensensemap_matches_and_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(OSM_BOX))
    v = OpenSenseMapBox("osm-t", SimClock(), box_id="abc123xyz", key_dir=tmp_path).sample()
    assert v == {
        "pm2_5_ugm3": 7.5, "pm10_ugm3": 12.0, "co2_ppm": 615.0, "voc_index": 120.0,
    }


def test_sensorthings_extracts_result(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_dispatch({"Datastreams(42)": STA_OBS}))
    v = SensorThingsDatastream(
        "sta-t", SimClock(), datastreams={"42": "pm2_5_ugm3"}, key_dir=tmp_path,
    ).sample()
    assert v == {"pm2_5_ugm3": 8.3}


# ── Tier A ────────────────────────────────────────────────────────────────────


def test_open_meteo_weather(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(OM_WEATHER))
    v = OpenMeteoWeather("om", SimClock(), latitude=52.52, longitude=13.41,
                         key_dir=tmp_path).sample()
    assert v["temperature_c"] == pytest.approx(24.7)
    assert v["wind_mps"] == pytest.approx(2.2)  # already m/s from wind_speed_unit=ms
    assert v["pressure_hpa"] == pytest.approx(1007.9)


def test_open_meteo_air(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(OM_AQ))
    v = OpenMeteoAirQuality("om", SimClock(), key_dir=tmp_path).sample()
    assert v == {"pm2_5_ugm3": 14.4, "pm10_ugm3": 17.9, "co2_ppm": 477.0}


def test_open_meteo_rejects_bad_coords(tmp_path):
    with pytest.raises(ValueError):
        OpenMeteoWeather("om", SimClock(), latitude=99.0, longitude=0.0, key_dir=tmp_path)


def test_uk_carbon_prefers_actual(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(UK_CI))
    v = UKCarbonIntensity("uk", SimClock(), key_dir=tmp_path).sample()
    assert v == {"carbon_intensity_gco2_kwh": 59.0}


def test_uk_carbon_falls_back_to_forecast(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(UK_CI_FORECAST_ONLY))
    v = UKCarbonIntensity("uk", SimClock(), key_dir=tmp_path).sample()
    assert v == {"carbon_intensity_gco2_kwh": 72.0}


# ── Tier B ────────────────────────────────────────────────────────────────────


def test_usgs_quake(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(USGS))
    v = USGSEarthquake("q", SimClock(), key_dir=tmp_path).sample()
    assert v["magnitude"] == pytest.approx(5.2)
    assert v["depth_km"] == pytest.approx(10.0)
    assert v["latitude"] == pytest.approx(-49.5336)
    assert v["longitude"] == pytest.approx(164.0845)


def test_usgs_empty_feed_is_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning({"features": []}))
    with pytest.raises(DeviceOffline):
        USGSEarthquake("q", SimClock(), key_dir=tmp_path).sample()


def test_noaa_tide(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(NOAA_TIDE))
    v = NOAATideStation("t", SimClock(), station="8518750", key_dir=tmp_path).sample()
    assert v == {"water_level_m": pytest.approx(1.143)}


def test_noaa_rejects_bad_station(tmp_path):
    with pytest.raises(ValueError):
        NOAATideStation("t", SimClock(), station="../../x", key_dir=tmp_path)


def test_openaq_with_key(monkeypatch, tmp_path):
    seen = {}

    def fake_get(url, headers=None, timeout=None, **kw):
        seen["headers"] = headers or {}
        return httpx.Response(200, json=OPENAQ, request=httpx.Request("GET", url))

    monkeypatch.setattr(livemod.httpx, "get", fake_get)
    v = OpenAQLocation(
        "oa", SimClock(), location_id="2178", api_key="test-key-abcdef", key_dir=tmp_path,
    ).sample()
    assert v["pm2_5_ugm3"] == pytest.approx(11.2)
    assert v["pm10_ugm3"] == pytest.approx(18.0)
    assert seen["headers"].get("X-API-Key") == "test-key-abcdef"


def test_usgs_river_metric_units(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(USGS_RIVER))
    v = USGSRiverGauge("r", SimClock(), usgs_site="01646500", key_dir=tmp_path).sample()
    assert v["discharge_m3s"] == pytest.approx(35300 * 0.028316846592)
    assert v["gage_height_m"] == pytest.approx(5.42 * 0.3048)


def test_usgs_river_rejects_bad_site(tmp_path):
    with pytest.raises(ValueError):
        USGSRiverGauge("r", SimClock(), usgs_site="../x", key_dir=tmp_path)


def test_ndbc_buoy(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning_text(NDBC_TXT))
    v = NDBCBuoy("b", SimClock(), station="44025", key_dir=tmp_path).sample()
    assert v["wave_height_m"] == pytest.approx(1.40)
    assert v["sst_c"] == pytest.approx(21.4)
    assert v["wind_mps"] == pytest.approx(6.2)


def test_ndbc_rejects_bad_station(tmp_path):
    with pytest.raises(ValueError):
        NDBCBuoy("b", SimClock(), station="../../x", key_dir=tmp_path)


# ── Free-to-commercialize open relays ─────────────────────────────────────────

FIRMS_CSV = """\
latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight
34.1,-118.2,330.5,0.4,0.4,2026-08-11,1200,N,n,2.0NRT,290.1,5.2,D
35.0,-120.0,380.0,0.4,0.4,2026-08-11,1205,N,h,2.0NRT,300.0,12.0,D
33.0,-117.0,310.0,0.4,0.4,2026-08-11,1210,N,l,2.0NRT,280.0,1.0,D
"""

SAFECAST = [
    {"id": 1, "value": 42.0, "unit": "cpm", "latitude": 37.41, "longitude": 141.02},
    {"id": 2, "value": 88.5, "unit": "cpm", "latitude": 37.42, "longitude": 141.03},
    # The real feed mixes units in the same value field — these must be dropped,
    # never sold as CPM (status=28.1 is a device temperature, 999 is °C-ish junk).
    {"id": 3, "value": 999.0, "unit": "celcius", "latitude": 37.43, "longitude": 141.04},
    {"id": 4, "value": 500.0, "unit": "status", "latitude": 37.44, "longitude": 141.05},
]

CYBERNEWS = {
    "generated_at": "2026-08-11T12:00:00Z",
    "records": [
        {
            "event_id": "e1",
            "type": "JAMMING",
            "region": "Baltic",
            "latitude": 56.0,
            "longitude": 20.0,
            "radius_km": 120.0,
            "severity": "high",
            "confidence": 0.9,
            "status": "ACTIVE",
            "start_date": "2026-08-11T09:00:00Z",
            "affected_systems": ["GPS", "Galileo"],
            "sources": ["https://example.test/source-1"],
        },
        {
            "event_id": "e2",
            "type": "SPOOFING",
            "region": "Black Sea",
            "latitude": 44.0,
            "longitude": 34.0,
            "radius_km": 80.0,
            "severity": "medium",
            "status": "MONITORING",
        },
    ],
}


def test_firms_fire_picks_brightest_non_low(monkeypatch, tmp_path):
    from gaia.devices.live_open import FirmsFireHotspot

    monkeypatch.setattr(livemod.httpx, "get", _get_returning_text(FIRMS_CSV))
    v = FirmsFireHotspot("firms-fire-01", SimClock(), key_dir=tmp_path).sample()
    assert v["brightness_k"] == pytest.approx(380.0)
    assert v["confidence"] == pytest.approx(90.0)
    assert v["latitude"] == pytest.approx(35.0)
    assert v["longitude"] == pytest.approx(-120.0)


def test_firms_read_returns_hotspot_cluster(monkeypatch, tmp_path):
    from gaia.devices.live_open import FirmsFireHotspot

    monkeypatch.setattr(livemod.httpx, "get", _get_returning_text(FIRMS_CSV))
    payload = FirmsFireHotspot("firms-fire-01", SimClock(), key_dir=tmp_path).read()
    reading = payload["reading"]
    assert reading["values"]["brightness_k"] == pytest.approx(380.0)
    assert reading["hotspot_count"] == 2  # low-confidence row dropped
    assert len(reading["hotspots"]) == 2
    assert reading["hotspots"][0]["brightness_k"] == pytest.approx(380.0)
    assert reading["hotspots"][0]["observed_at"] == "2026-08-11T12:05:00Z"
    assert reading["hotspots"][0]["satellite"] == "N"
    assert reading["hotspots"][0]["daynight"] == "D"
    assert reading["hotspots"][1]["brightness_k"] == pytest.approx(330.5)
    assert reading["hotspot_total"] == 2
    assert reading["hotspot_matched"] == 2
    assert reading["next_cursor"] is None
    assert reading["fetch_id"]


def test_firms_empty_viewport_attests_global_anchor(monkeypatch, tmp_path):
    """Empty bbox → headline is this fetch's global brightest (physics-valid),
    never zeros (which fail the brightness_k lower bound) or stale values."""
    from gaia.devices.live_open import FirmsFireHotspot
    from gaia.plausibility import PHYSICS

    monkeypatch.setattr(livemod.httpx, "get", _get_returning_text(FIRMS_CSV))
    dev = FirmsFireHotspot("firms-fire-01", SimClock(), key_dir=tmp_path)
    # Ocean bbox with no detections in the fixture CSV.
    dev.set_query(bbox=(0.0, -10.0, 5.0, -5.0))
    try:
        reading = dev.read()["reading"]
    finally:
        dev.clear_query()
    assert reading["hotspots"] == []
    assert reading["hotspot_count"] == 0
    assert reading["hotspot_matched"] == 2
    assert reading["values"]["brightness_k"] >= PHYSICS["brightness_k"].lo
    assert reading["values"]["brightness_k"] == pytest.approx(380.0)


def test_firms_hotspot_pages_resume_idempotent(monkeypatch, tmp_path):
    from gaia.devices.hotspot_pages import STORE
    from gaia.devices.live_open import FirmsFireHotspot

    # Build a synthetic CSV with 1205 bright hotspots.
    lines = ["latitude,longitude,bright_ti4,confidence"]
    for i in range(1205):
        lines.append(f"{30 + i * 0.001:.5f},{-120 + (i % 50) * 0.01:.5f},{400 - (i % 100) * 0.1:.1f},h")
    csv = "\n".join(lines) + "\n"
    monkeypatch.setattr(livemod.httpx, "get", _get_returning_text(csv))
    # Tiny page store secret for stable tests (restored after the test).
    monkeypatch.setattr(STORE, "_secret", b"test-hotspot-cursor-secret")
    STORE._sessions.clear()

    dev = FirmsFireHotspot("firms-fire-01", SimClock(), key_dir=tmp_path)
    dev.set_query(collect_max=1200, page_size=500)
    first = dev.read()["reading"]
    assert first["hotspot_count"] == 500
    assert first["hotspot_total"] == 1200
    assert first["hotspot_offset"] == 0
    assert first["next_cursor"]
    cursor1 = first["next_cursor"]

    # Idempotent retry of the *next* page cursor
    page2a = dev.read_page_from_cursor(cursor1, page_size=500)["reading"]
    page2b = dev.read_page_from_cursor(cursor1, page_size=500)["reading"]
    assert page2a["hotspots"] == page2b["hotspots"]
    assert page2a["hotspot_offset"] == 500
    assert page2a["hotspot_count"] == 500
    cursor2 = page2a["next_cursor"]
    assert cursor2

    page3 = dev.read_page_from_cursor(cursor2, page_size=500)["reading"]
    assert page3["hotspot_offset"] == 1000
    assert page3["hotspot_count"] == 200
    assert page3["next_cursor"] is None
    # Headline stays the global brightest across pages
    assert page3["values"]["brightness_k"] == first["values"]["brightness_k"]


def test_firms_bbox_filters_cluster(monkeypatch, tmp_path):
    from gaia.devices.live_open import FirmsFireHotspot

    monkeypatch.setattr(livemod.httpx, "get", _get_returning_text(FIRMS_CSV))
    # Only the 34.1,-118.2 nominal hotspot is inside this box.
    dev = FirmsFireHotspot("firms-fire-01", SimClock(), key_dir=tmp_path)
    rows = dev.collect_hotspots(
        FIRMS_CSV,
        limit=10,
        bbox=(-119.0, 33.5, -117.5, 34.5),
    )
    assert isinstance(rows, tuple)
    hotspots, truncated, matched, best_global = rows
    assert best_global["brightness_k"] > 0
    assert truncated is False
    # matched = global non-low (2); rows = bbox-filtered (1)
    assert matched == 2
    assert len(hotspots) == 1
    assert hotspots[0]["latitude"] == pytest.approx(34.1)


def test_firms_empty_is_offline(monkeypatch, tmp_path):
    from gaia.devices.live_open import FirmsFireHotspot

    monkeypatch.setattr(
        livemod.httpx, "get", _get_returning_text("latitude,longitude,confidence\n")
    )
    with pytest.raises(DeviceOffline):
        FirmsFireHotspot("firms-fire-01", SimClock(), key_dir=tmp_path).sample()


def test_safecast_picks_highest_cpm(monkeypatch, tmp_path):
    from gaia.devices.live_open import SafecastRadiation

    monkeypatch.setattr(livemod.httpx, "get", _get_returning(SAFECAST))
    dev = SafecastRadiation("safecast-01", SimClock(), key_dir=tmp_path)
    v = dev.sample()
    # Non-CPM rows (celcius/status, values 999/500) must not win the headline.
    assert v["cpm"] == pytest.approx(88.5)
    assert v["latitude"] == pytest.approx(37.42)
    # Recency window is applied per read, not frozen at construction.
    assert "captured_after=" in dev._safecast_url(1)


def test_safecast_archive_omits_captured_after(tmp_path):
    from gaia.devices.live_open import SafecastRadiation

    dev = SafecastRadiation(
        "safecast-melbourne", SimClock(), key_dir=tmp_path, max_age_days=0
    )
    url = dev._safecast_url(1)
    assert "captured_after=" not in url
    assert "page=1" in url
    assert dev._page_budget() == 40


def test_safecast_archive_drains_more_pages(monkeypatch, tmp_path):
    from gaia.devices.live_open import SafecastRadiation

    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None, **kw):
        calls["n"] += 1
        page = 1
        if "page=" in url:
            try:
                page = int(url.rsplit("page=", 1)[-1].split("&", 1)[0])
            except ValueError:
                page = 1
        if page > 8:
            payload = []
        else:
            payload = [
                {
                    "value": 30.0 + page,
                    "unit": "cpm",
                    "latitude": -38.0 - page * 0.01,
                    "longitude": 144.0 + page * 0.01,
                    "captured_at": "2014-03-29T08:57:39.000Z",
                }
                for _ in range(150)
            ]
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(livemod.httpx, "get", fake_get)
    dev = SafecastRadiation(
        "safecast-melbourne", SimClock(), key_dir=tmp_path, max_age_days=0
    )
    out = dev.read()
    assert calls["n"] == 9  # 8 full pages + 1 empty
    hs = out["reading"]["hotspots"]
    assert len(hs) >= 8
    assert hs[0]["captured_at"].startswith("2014-")


def test_extra_safecast_honors_max_age_zero(tmp_path, monkeypatch):
    from gaia.devices import extra_sensors as es
    from gaia.fleet import Fleet

    monkeypatch.setattr(
        es,
        "load_sensors",
        lambda: (
            {
                "kind": "safecast",
                "device_id": "safecast-melbourne",
                "lat": -37.8136,
                "lon": 144.9631,
                "place": "Melbourne",
                "params": {"max_age_days": 0, "distance_m": 500000},
            },
        ),
    )
    fleet = Fleet()
    assert es.register_live_extras(fleet, SimClock(), key_dir=str(tmp_path)) == 1
    dev = fleet.get("safecast-melbourne")
    assert dev._max_age_days == 0
    assert "captured_after=" not in dev._safecast_url(1)
    assert dev._distance_m == 500_000


def test_cybernews_picks_highest_severity(monkeypatch, tmp_path):
    from gaia.devices.live_open import CyberNewsJamming

    monkeypatch.setattr(livemod.httpx, "get", _get_returning(CYBERNEWS))
    v = CyberNewsJamming("cybernews-jam-01", SimClock(), key_dir=tmp_path).sample()
    assert v["severity_score"] == pytest.approx(80.0)
    assert v["latitude"] == pytest.approx(56.0)
    assert v["radius_km"] == pytest.approx(120.0)


def test_cybernews_filters_closed_events_and_preserves_provenance(monkeypatch, tmp_path):
    from gaia.devices.live_open import CyberNewsJamming

    payload = {
        "generated_at": "2026-08-11T12:00:00Z",
        "records": [
            {
                "event_id": "resolved-critical",
                "latitude": 55.0,
                "longitude": 19.0,
                "radius_km": 500,
                "severity": "critical",
                "status": "RESOLVED",
            },
            {
                "event_id": "active-high",
                "type": "JAMMING",
                "region": "Baltic",
                "latitude": 56.0,
                "longitude": 20.0,
                "radius_km": 120,
                "severity": "high",
                "confidence": 0.9,
                "status": "ACTIVE",
                "start_date": "2026-08-11T09:00:00Z",
                "affected_systems": ["GPS", "Galileo"],
                "sources": ["https://example.test/source-1"],
            },
        ],
    }
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(payload))
    reading = CyberNewsJamming(
        "cybernews-jam-01", SimClock(), key_dir=tmp_path
    ).read()["reading"]
    assert reading["hotspot_count"] == 1
    event = reading["hotspots"][0]
    assert event["event_id"] == "active-high"
    assert event["status"] == "ACTIVE"
    assert event["start_date"] == "2026-08-11T09:00:00Z"
    assert event["affected_systems"] == ["GPS", "Galileo"]
    assert reading["feed_generated_at"] == "2026-08-11T12:00:00Z"
    assert reading["license"] == "CC BY 4.0"


def test_cybernews_fails_closed_when_only_historical(monkeypatch, tmp_path):
    from gaia.devices.live_open import CyberNewsJamming

    payload = {
        "records": [
            {
                "event_id": "old",
                "latitude": 56.0,
                "longitude": 20.0,
                "radius_km": 120,
                "severity": "critical",
                "status": "HISTORICAL",
            }
        ]
    }
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(payload))
    with pytest.raises(DeviceOffline, match="ACTIVE/MONITORING"):
        CyberNewsJamming("cybernews-jam-01", SimClock(), key_dir=tmp_path).read()


def test_feeder_ingest_and_sample(tmp_path):
    from gaia.devices.feeder import FeederDevice, FeederStore, ingest

    store = FeederStore()
    clock = SimClock()
    dev = FeederDevice(
        "feeder-adsb-01", clock, kind="adsb", store=store, key_dir=tmp_path
    )
    with pytest.raises(DeviceOffline):
        dev.sample()
    ingest(
        "feeder-adsb-01",
        {"latitude": 40.7, "longitude": -74.0, "altitude_m": 3000, "speed_mps": 120},
        allowed_devices={"feeder-adsb-01": dev},
    )
    v = dev.sample()
    assert v["latitude"] == pytest.approx(40.7)
    assert v["altitude_m"] == pytest.approx(3000.0)
    assert "edge feeder" in (dev.source or "")


def test_open_meteo_marine(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(OM_MARINE))
    v = OpenMeteoMarine(
        "m", SimClock(), latitude=40.70, longitude=-74.01, key_dir=tmp_path,
    ).sample()
    assert v == {"wave_height_m": 1.25, "sst_c": 20.8}


# ── SSRF / fetch hardening ────────────────────────────────────────────────────


def test_assert_url_allowed_blocks_ssrf():
    with pytest.raises(DeviceOffline):
        _assert_url_allowed("http://api.weather.gov/x")  # not https
    with pytest.raises(DeviceOffline):
        _assert_url_allowed("https://evil.example/steal")
    with pytest.raises(DeviceOffline):
        _assert_url_allowed("https://user:pass@api.weather.gov/x")
    assert _assert_url_allowed("https://api.open-meteo.com/v1/forecast").startswith("https://")
    assert _assert_url_allowed("https://waterservices.usgs.gov/nwis/iv/").startswith("https://")
    assert _assert_url_allowed("https://www.ndbc.noaa.gov/data/realtime2/44025.txt").startswith(
        "https://"
    )
    assert _assert_url_allowed(
        "https://marine-api.open-meteo.com/v1/marine"
    ).startswith("https://")


class TestOpenMeteoOrigin:
    """Self-hosted Open-Meteo — the fix for a non-commercial hosted ToS.

    The DATA is CC BY 4.0 and resellable; the hosted FREE endpoint is not. GAIA
    therefore has to be pointable at an operator-run instance, and must refuse to
    bill readings fetched from the free tier.
    """

    def test_default_origin_is_unchanged_hosted_api(self, monkeypatch, tmp_path):
        for var in ("GAIA_OM_BASE_URL", "GAIA_OM_AQ_BASE_URL", "GAIA_OM_MARINE_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("AIFACTORY_CRYPTO_ENABLED", raising=False)
        wx = OpenMeteoWeather("om", SimClock(), key_dir=tmp_path)
        assert wx.url.startswith("https://api.open-meteo.com/v1/forecast")
        # Unchanged attribution when nothing was reconfigured.
        assert "operator-run" not in wx.source

    def test_self_host_origin_is_used_and_disclosed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GAIA_OM_BASE_URL", "http://open-meteo:8080")
        wx = OpenMeteoWeather("om", SimClock(), key_dir=tmp_path)
        assert wx.url.startswith("http://open-meteo:8080/v1/forecast")
        # Provenance is never implied in this fleet: the receipt says where the
        # bytes came from, and still carries the CC BY 4.0 attribution.
        assert "operator-run Open-Meteo instance http://open-meteo:8080" in wx.source
        assert "CC BY 4.0" in wx.source

    def test_ssrf_guard_admits_only_the_configured_self_host_origin(self, monkeypatch):
        monkeypatch.setenv("GAIA_OM_BASE_URL", "http://open-meteo:8080")
        # The operator-configured origin may be plain http (internal, no TLS).
        assert _assert_url_allowed("http://open-meteo:8080/v1/forecast")
        # Any OTHER internal target stays refused — this must not become a
        # general-purpose http escape hatch.
        with pytest.raises(DeviceOffline):
            _assert_url_allowed("http://169.254.169.254/latest/meta-data/")
        with pytest.raises(DeviceOffline):
            _assert_url_allowed("http://open-meteo:9999/v1/forecast")  # wrong port
        with pytest.raises(DeviceOffline):
            _assert_url_allowed("http://user:pass@open-meteo:8080/v1/forecast")

    def test_refuses_to_sell_readings_from_the_free_tier(self, monkeypatch, tmp_path):
        """Payments on + hosted free API = billing against a non-commercial ToS."""
        monkeypatch.delenv("GAIA_OM_BASE_URL", raising=False)
        monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
        with pytest.raises(ValueError, match="non-commercial"):
            OpenMeteoWeather("om", SimClock(), key_dir=tmp_path)
        for cls in (OpenMeteoAirQuality, OpenMeteoMarine):
            with pytest.raises(ValueError, match="non-commercial"):
                cls("om", SimClock(), key_dir=tmp_path)

    def test_self_host_lets_a_paid_deployment_boot(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
        for var in ("GAIA_OM_BASE_URL", "GAIA_OM_AQ_BASE_URL", "GAIA_OM_MARINE_BASE_URL"):
            monkeypatch.setenv(var, "http://open-meteo:8080")
        assert OpenMeteoWeather("om", SimClock(), key_dir=tmp_path).url.startswith("http://open-meteo:8080")
        assert OpenMeteoAirQuality("om", SimClock(), key_dir=tmp_path).url.startswith("http://open-meteo:8080")
        assert OpenMeteoMarine("om", SimClock(), key_dir=tmp_path).url.startswith("http://open-meteo:8080")

    def test_commercial_plan_override_permits_hosted_origin(self, monkeypatch, tmp_path):
        """A paid plan serves from a customer endpoint; the operator asserts it."""
        monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
        monkeypatch.delenv("GAIA_OM_BASE_URL", raising=False)
        monkeypatch.setenv("GAIA_OM_ALLOW_HOSTED_COMMERCIAL", "1")
        monkeypatch.setenv("GAIA_OM_API_KEY", "plan-key-123")
        wx = OpenMeteoWeather("om", SimClock(), key_dir=tmp_path)
        assert wx.url.startswith("https://api.open-meteo.com/v1/forecast")
        assert "apikey=plan-key-123" in wx.url

    def test_api_key_is_url_encoded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GAIA_OM_BASE_URL", "http://open-meteo:8080")
        monkeypatch.setenv("GAIA_OM_API_KEY", "a b&c=d")
        wx = OpenMeteoWeather("om", SimClock(), key_dir=tmp_path)
        assert "apikey=a%20b%26c%3Dd" in wx.url

    def test_bearer_goes_to_our_node_and_never_to_open_meteo(self, monkeypatch, tmp_path):
        """A cross-host instance needs auth; that secret must not leak upstream.

        The failure this guards is mundane and likely: someone sets the token but
        leaves GAIA_OM_BASE_URL unset (or reverts it), and GAIA then sends our
        bearer to a third party on every reading.
        """
        monkeypatch.setenv("GAIA_OM_AUTH_TOKEN", "s3cret-node-token")

        monkeypatch.setenv("GAIA_OM_BASE_URL", "https://om.example.dev")
        ours = OpenMeteoWeather("om", SimClock(), key_dir=tmp_path)
        assert ours.headers.get("Authorization") == "Bearer s3cret-node-token"

        # Origin falls back to the hosted API → no Authorization header at all.
        monkeypatch.delenv("GAIA_OM_BASE_URL", raising=False)
        monkeypatch.delenv("AIFACTORY_CRYPTO_ENABLED", raising=False)
        hosted = OpenMeteoWeather("om", SimClock(), key_dir=tmp_path)
        assert hosted.url.startswith("https://api.open-meteo.com")
        assert "Authorization" not in hosted.headers

    def test_bearer_is_not_shared_between_device_instances(self, monkeypatch, tmp_path):
        """headers is a CLASS attribute on the device base — mutating it in place
        would bleed our node token onto every other relay in the fleet."""
        monkeypatch.setenv("GAIA_OM_AUTH_TOKEN", "s3cret-node-token")
        monkeypatch.setenv("GAIA_OM_BASE_URL", "https://om.example.dev")
        OpenMeteoWeather("om", SimClock(), key_dir=tmp_path)
        assert "Authorization" not in NWSStation("nws", SimClock(), key_dir=tmp_path).headers
        assert "Authorization" not in OpenMeteoWeather.headers


def test_fetch_refuses_redirect_to_foreign_host(monkeypatch, tmp_path):
    """follow_redirects=False — a 302 to an internal IP must not be followed."""
    def fake_get(url, headers=None, timeout=None, follow_redirects=None, **kw):
        assert follow_redirects is False
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(livemod.httpx, "get", fake_get)
    with pytest.raises(DeviceOffline):
        NWSStation("nws-t", SimClock(), key_dir=tmp_path).sample()


def test_transport_error_raises_device_offline(monkeypatch, tmp_path):
    def boom(url, headers=None, timeout=None, **kw):
        raise httpx.ConnectError("no route", request=httpx.Request("GET", url))
    monkeypatch.setattr(livemod.httpx, "get", boom)
    with pytest.raises(DeviceOffline):
        NWSStation("nws-t", SimClock(), key_dir=tmp_path).read()


def test_non_200_raises_device_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning({}, status=503))
    with pytest.raises(DeviceOffline):
        NWSStation("nws-t", SimClock(), key_dir=tmp_path).read()


# ── Attestation + verifier ────────────────────────────────────────────────────


def test_live_reading_attested_and_verifies(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(NWS_FULL))
    clock = SimClock(realtime=False)
    fleet = Fleet()
    fleet.add(NWSStation("nws-01", clock, station="KNYC",
                         site="live-weather", key_dir=tmp_path))
    verifier = PlausibilityVerifier(fleet)
    out = None
    for _ in range(3):
        clock.advance(60)
        out = fleet.read("nws-01")
    verdict = verifier.check(out["reading"], out["attestation"], require_attestation=True)
    assert verdict.verified, verdict.summary


def test_uk_carbon_verifies(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(UK_CI))
    clock = SimClock(realtime=False)
    fleet = Fleet()
    fleet.add(UKCarbonIntensity("uk-grid-01", clock, site="live-grid-uk", key_dir=tmp_path))
    verifier = PlausibilityVerifier(fleet)
    out = None
    for _ in range(3):
        clock.advance(1800)
        out = fleet.read("uk-grid-01")
    verdict = verifier.check(out["reading"], out["attestation"], require_attestation=True)
    assert verdict.verified, verdict.summary


def test_spike_on_live_device_is_caught(monkeypatch, tmp_path):
    monkeypatch.setattr(livemod.httpx, "get", _get_returning(NWS_FULL))
    clock = SimClock(realtime=False)
    fleet = Fleet()
    dev = NWSStation("nws-01", clock, station="KNYC",
                     site="live-weather", key_dir=tmp_path)
    fleet.add(dev)
    verifier = PlausibilityVerifier(fleet)
    clock.advance(60)
    fleet.read("nws-01")
    dev.inject_fault("spike", fields=["temperature_c"], magnitude=60.0)
    clock.advance(60)
    out = fleet.read("nws-01")
    verdict = verifier.check(out["reading"], out["attestation"], require_attestation=True)
    assert not verdict.verified


# ── Factory ───────────────────────────────────────────────────────────────────


def test_build_live_fleet_registers_tier_a_b(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_OPENAQ_API_KEY", raising=False)
    monkeypatch.delenv("GAIA_FEEDER_ENABLED", raising=False)
    fleet = build_live_fleet(SimClock(realtime=True), key_dir=str(tmp_path))
    ids = {d.device_id for d in fleet.devices()}
    assert {"nws-01", "osm-01", "sta-01", "om-wx-01", "om-aq-01",
            "uk-grid-01", "usgs-quake-01", "noaa-tide-01",
            "usgs-river-01", "ndbc-01", "om-marine-01",
            "firms-fire-01", "safecast-01", "cybernews-jam-01",
            "eonet-01", "swpc-01", "nws-alerts-01",
            "sc-01", "cwop-01", "argo-01", "metno-01", "usgs-geomag-01",
            "nws-flood-01", "effis-01", "usgs-volcano-01",
            "dwd-01", "eccc-01", "aurn-01", "geonet-01", "uhslc-01",
            "fintraffic-ais-01", "eccc-hydro-01", "fmi-01",
            "nws-tsunami-01", "smhi-hydro-01",
            "nhc-cyclone-01", "emsc-01", "ea-flood-01", "ptwc-01",
            "adsb-lol-01"} <= ids
    assert "kystverket-ais-01" not in ids  # needs BarentsWatch token
    assert "openaq-01" not in ids  # no key
    assert "feeder-adsb-01" not in ids  # feeder opt-in
    assert "om-wx-ottawa" in ids
    assert "om-aq-delhi" in ids
    by_id = {d["device_id"]: d for d in fleet.status()["devices"]}
    assert "open-meteo.com" in by_id["om-wx-01"]["source"]
    assert "open-meteo.com" in by_id["om-wx-ottawa"]["source"]
    assert "carbonintensity.org.uk" in by_id["uk-grid-01"]["source"]
    assert "earthquake.usgs.gov" in by_id["usgs-quake-01"]["source"]
    assert "waterservices.usgs.gov" in by_id["usgs-river-01"]["source"]
    assert "ndbc.noaa.gov" in by_id["ndbc-01"]["source"]
    assert "open-meteo.com" in by_id["om-marine-01"]["source"]
    assert "firms.modaps.eosdis.nasa.gov" in by_id["firms-fire-01"]["source"]
    assert "safecast.org" in by_id["safecast-01"]["source"]
    assert "cybernews.space" in by_id["cybernews-jam-01"]["source"]


def test_build_live_fleet_feeders_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_OPENAQ_API_KEY", raising=False)
    monkeypatch.setenv("GAIA_FEEDER_ENABLED", "1")
    fleet = build_live_fleet(SimClock(realtime=True), key_dir=str(tmp_path))
    ids = {d.device_id for d in fleet.devices()}
    assert "feeder-adsb-01" in ids
    assert "feeder-ais-01" in ids
    assert "feeder-iot-01" in ids
    by_id = {d["device_id"]: d for d in fleet.status()["devices"]}
    assert by_id["feeder-adsb-01"]["source"]
    assert "aggregator" in by_id["feeder-adsb-01"]["source"]


def test_build_live_fleet_mesh_can_disable(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_OPENAQ_API_KEY", raising=False)
    monkeypatch.setenv("GAIA_OM_MESH_ENABLED", "0")
    fleet = build_live_fleet(SimClock(realtime=True), key_dir=str(tmp_path))
    ids = {d.device_id for d in fleet.devices()}
    assert "om-wx-01" in ids
    assert "om-wx-ottawa" not in ids


def test_build_live_fleet_openaq_when_keyed(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_OPENAQ_API_KEY", "demo-key-xxxxxxxxxxxx")
    monkeypatch.setenv("GAIA_OPENAQ_LOCATION_ID", "2178")
    fleet = build_live_fleet(SimClock(realtime=True), key_dir=str(tmp_path))
    assert "openaq-01" in {d.device_id for d in fleet.devices()}
