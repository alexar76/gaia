"""Open-relay capabilities + feeder ingest HTTP — free-to-commercialize SKUs."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from gaia.app import build_app
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.devices import live as livemod
from gaia.devices.feeder import STORE as FEEDER_STORE
from gaia.devices.live_open import CyberNewsJamming, FirmsFireHotspot, SafecastRadiation


FIRMS_CSV = """\
latitude,longitude,bright_ti4,acq_date,acq_time,confidence
35.0,-120.0,380.0,2026-08-11,1205,h
"""

SAFECAST = [
    {"id": 1, "value": 88.5, "unit": "cpm", "latitude": 37.42, "longitude": 141.03},
]

CYBERNEWS = {
    "records": [
        {
            "event_id": "e1",
            "latitude": 56.0,
            "longitude": 20.0,
            "radius_km": 120.0,
            "severity": "high",
            "status": "ACTIVE",
        }
    ]
}

EONET = {
    "events": [
        {
            "id": "EONET_X",
            "title": "Test volcano",
            "categories": [{"id": "volcanoes", "title": "Volcanoes"}],
            "geometry": [{"type": "Point", "coordinates": [-155.2, 19.4]}],
        }
    ]
}

SWPC_KP = [{"time_tag": "2026-08-12T00:00:00Z", "kp_index": 3.0, "estimated_kp": 3.0}]
SWPC_OVATION = {"coordinates": [[-100.0, 60.0, 40.0], [-90.0, 65.0, 80.0]]}

CAP = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [-97.5, 35.5]},
            "properties": {
                "severity": "Severe",
                "event": "Thunderstorm Warning",
                "headline": "Severe Thunderstorm Warning",
                "areaDesc": "OK",
            },
        }
    ]
}

SENSOR_COMMUNITY = [
    {
        "location": {"latitude": "52.52", "longitude": "13.41"},
        "sensordatavalues": [
            {"value_type": "P2", "value": "9.0"},
            {"value_type": "P1", "value": "14.0"},
        ],
    }
]

CWOP = {"data": [{"tmpc": 18.0, "relh": 60.0, "mslp": 1012.0, "sknt": 2.0, "lat": 40.2, "lon": -74.0}]}

ARGO = [{"geolocation": {"coordinates": [-70.0, 25.0]}, "data": {"temperature": [26.0], "salinity": [36.0], "pressure_dbar": [10.0]}}]

GEOMAG = {
    "metadata": {"intermagnet": {"imo": {"coordinates": [-105.24, 40.14, 1682]}}},
    "values": [{"id": "F", "values": [51200.0]}],
}

FLOOD = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [-90.0, 35.0]},
            "properties": {"severity": "Severe", "event": "Flash Flood Warning"},
        }
    ]
}
TSUNAMI = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [-155.0, 19.5]},
            "properties": {
                "severity": "Extreme",
                "event": "Tsunami Warning",
                "headline": "Tsunami Warning",
                "areaDesc": "HI",
            },
        }
    ]
}
AIS = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [24.94, 60.17]},
            "properties": {"mmsi": 230091290, "sog": 8.0, "cog": 45.0, "navStat": 0},
        }
    ]
}
ECCC_HYDRO = {
    "features": [{
        "geometry": {"type": "Point", "coordinates": [-79.52, 43.70]},
        "properties": {"DISCHARGE": 6.04, "LEVEL": 2.33},
    }]
}
FMI_XML = (
    "<?xml version='1.0'?>"
    "<wfs:FeatureCollection xmlns:wfs='http://www.opengis.net/wfs/2.0' "
    "xmlns:BsWfs='http://xml.fmi.fi/schema/wfs/2.0' "
    "xmlns:gml='http://www.opengis.net/gml/3.2'>"
    "<BsWfs:ParameterName>t2m</BsWfs:ParameterName>"
    "<BsWfs:ParameterValue>11.0</BsWfs:ParameterValue>"
    "<BsWfs:ParameterName>ws_10min</BsWfs:ParameterName>"
    "<BsWfs:ParameterValue>3.0</BsWfs:ParameterValue>"
    "<BsWfs:ParameterName>rh</BsWfs:ParameterName>"
    "<BsWfs:ParameterValue>70.0</BsWfs:ParameterValue>"
    "<BsWfs:ParameterName>p_sea</BsWfs:ParameterName>"
    "<BsWfs:ParameterValue>1012.0</BsWfs:ParameterValue>"
    "<gml:pos>60.17 24.94</gml:pos>"
    "</wfs:FeatureCollection>"
)
SMHI = {
    "position": [{"latitude": 68.19, "longitude": 19.98}],
    "value": [{"value": 12.0}],
}
EFFIS = {"features": [{"geometry": {"type": "Point", "coordinates": [8.0, 40.0]}, "properties": {"AREA_HA": 100}}]}
VOLCANO = [{"volcano_name": "Kilauea", "vnum": "332010", "alert_level": "WATCH"}]
GET_VOLCANO = {"volcano_name": "Kilauea", "latitude": 19.4, "longitude": -155.2}
DWD = {"weather": {"temperature": 16.0, "relative_humidity": 70, "pressure_msl": 1013.0, "wind_speed": 10.0}}
ECCC = {"features": [{"geometry": {"type": "Point", "coordinates": [-75.7, 45.4]}, "properties": {"TEMP": 12.0, "RELATIVE_HUMIDITY": 80, "WIND_SPEED": 15}}]}
AURN = {"HourlyAirQualityIndex": {"LocalAuthority": {"Site": {"@Latitude": "51.52", "@Longitude": "-0.15", "species": [{"@SpeciesCode": "NO2", "@AirQualityIndex": "2", "@IndexSource": "Measurement"}]}}}}
GEONET = {"features": [{"properties": {"magnitude": 4.0}, "geometry": {"coordinates": [174.0, -41.0, 10.0]}}]}
UHSLC = {
    "table": {
        "columnNames": ["time", "sea_level", "latitude", "longitude"],
        "rows": [[datetime.now(timezone.utc).isoformat(), 450.0, 21.3, -157.8]],
    }
}
METAR_TXT = "ENGM 122150Z 18008KT 9999 FEW030 17/12 Q1013 NOSIG\n"
NHC = {
    "activeStorms": [{
        "id": "al052026", "name": "Testcane", "classification": "HU",
        "intensity": "90", "pressure": "960",
        "latitudeNumeric": 22.1, "longitudeNumeric": -75.0,
    }]
}
EMSC = {
    "features": [{
        "geometry": {"coordinates": [12.0, 45.0, 10.0]},
        "properties": {"mag": 4.2, "depth": 10.0, "flynn_region": "ITALY"},
    }]
}
EA_FLOOD = {
    "items": [{
        "description": "Thames at Maidenhead",
        "severity": "Flood warning",
        "severityLevel": 2,
        "lat": 51.52,
        "long": -0.70,
        "floodArea": {"county": "Berkshire"},
    }]
}
PTWC_ATOM = (
    "<?xml version='1.0'?>"
    "<feed xmlns='http://www.w3.org/2005/Atom' "
    "xmlns:georss='http://www.georss.org/georss'>"
    "<entry><title>Tsunami Warning</title>"
    "<summary>Hazardous waves</summary>"
    "<georss:point>19.5 -155.0</georss:point></entry></feed>"
)
ADSB_LOL = {
    "ac": [{
        "hex": "40621d", "flight": "BAW123", "lat": 51.47, "lon": -0.45,
        "alt_baro": 32000, "gs": 420.0,
    }]
}


def _get_dispatch(by_needle: dict):
    def fake_get(url, headers=None, timeout=None, **kw):
        for needle, payload in by_needle.items():
            if needle in url:
                if isinstance(payload, str):
                    return httpx.Response(
                        200, text=payload, request=httpx.Request("GET", url)
                    )
                return httpx.Response(
                    200, json=payload, request=httpx.Request("GET", url)
                )
        # Open-Meteo mesh + other default live hosts — empty-ish weather
        # Open-Meteo mesh + NWS *station* observations — not CAP alerts.
        if "open-meteo.com" in url or ("weather.gov" in url and "alerts" not in url):
            return httpx.Response(
                200,
                json={"current": {"temperature_2m": 10.0, "relative_humidity_2m": 50,
                                  "surface_pressure": 1013.0, "wind_speed_10m": 1.0,
                                  "pm2_5": 5.0, "pm10": 8.0, "carbon_dioxide": 420}},
                request=httpx.Request("GET", url),
            )
        if "carbonintensity" in url:
            return httpx.Response(
                200,
                json={"data": [{"intensity": {"actual": 100}}]},
                request=httpx.Request("GET", url),
            )
        if "earthquake.usgs.gov" in url:
            return httpx.Response(
                200,
                json={"features": [{"properties": {"mag": 4.0},
                                    "geometry": {"coordinates": [10.0, 20.0, 5.0]}}]},
                request=httpx.Request("GET", url),
            )
        if "tidesandcurrents" in url:
            return httpx.Response(
                200, json={"data": [{"v": "1.0"}]}, request=httpx.Request("GET", url)
            )
        if "waterservices.usgs.gov" in url:
            return httpx.Response(
                200,
                json={"value": {"timeSeries": []}},
                request=httpx.Request("GET", url),
            )
        if "ndbc.noaa.gov" in url:
            return httpx.Response(
                200,
                text="#YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ATMP WTMP\n"
                     "2026 08 11 12 00 180 5.0 6.0 1.2 8.0 6.0 180 1013.0 20.0 18.0\n",
                request=httpx.Request("GET", url),
            )
        if "opensensemap" in url:
            return httpx.Response(
                200,
                json={"sensors": [{"title": "PM2.5", "lastMeasurement": {"value": "5"}}]},
                request=httpx.Request("GET", url),
            )
        if "ilt-dmz" in url or "SensorThings" in url or "frost" in url:
            return httpx.Response(
                200, json={"value": [{"result": 5.0}]}, request=httpx.Request("GET", url)
            )
        return httpx.Response(404, json={}, request=httpx.Request("GET", url))

    return fake_get


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    monkeypatch.setenv("GAIA_OM_MESH_ENABLED", "0")
    monkeypatch.setenv("GAIA_STA_ENABLED", "0")
    monkeypatch.setenv("GAIA_FEEDER_ENABLED", "1")
    monkeypatch.setenv("GAIA_FEEDER_TOKEN", "feeder-test-token")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    monkeypatch.delenv("GAIA_OPENAQ_API_KEY", raising=False)
    monkeypatch.setattr(
        livemod.httpx,
        "get",
        _get_dispatch(
            {
                "firms.modaps": FIRMS_CSV,
                "safecast.org": SAFECAST,
                "cybernews.space": CYBERNEWS,
                "eonet.gsfc.nasa.gov": EONET,
                "planetary_k_index": SWPC_KP,
                "ovation_aurora": SWPC_OVATION,
                "hydrometric-realtime": ECCC_HYDRO,
                "meri.digitraffic.fi": AIS,
                "opendata.fmi.fi": FMI_XML,
                "opendata-download-hydroobs.smhi.se": SMHI,
                "code=TSW": TSUNAMI,
                "code=FLW": FLOOD,
                "alerts/active": CAP,
                "sensor.community": SENSOR_COMMUNITY,
                "mesonet.agron.iastate.edu": CWOP,
                "argovis-api.colorado.edu": ARGO,
                "erddap.ifremer.fr": ARGO,
                "geomag.usgs.gov": GEOMAG,
                "getVolcano/": GET_VOLCANO,
                "volcanoes.usgs.gov": VOLCANO,
                "tafmetar": METAR_TXT,
                "modis.ba.poly": EFFIS,
                "brightsky.dev": DWD,
                "api.weather.gc.ca": ECCC,
                "api.erg.ic.ac.uk": AURN,
                "api.geonet.org.nz": GEONET,
                "uhslc.soest.hawaii.edu": UHSLC,
                "CurrentStorms.json": NHC,
                "seismicportal.eu": EMSC,
                "flood-monitoring/id/floods": EA_FLOOD,
                "PHEBAtom": PTWC_ATOM,
                "api.adsb.lol": ADSB_LOL,
                "s3.amazonaws.com": (
                    '<?xml version="1.0"?><ListBucketResult>'
                    "<Name>noaa-goes19</Name></ListBucketResult>"
                ),
            }
        ),
    )
    FEEDER_STORE.clear()
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys"), autotick=False)
    return runtime


def test_live_spec_advertises_open_relay_skus(live_env):
    spec = build_spec(live_env, public_url="http://gaia.test")
    ids = {c.capability_id for c in spec.capabilities}
    assert "gaia.fire.read@v1" in ids
    assert "gaia.radiation.read@v1" in ids
    assert "gaia.jamming.read@v1" in ids
    assert "gaia.adsb.read@v1" in ids
    assert "gaia.ais.read@v1" in ids
    assert "gaia.iot.read@v1" in ids
    assert "gaia.quake.read@v1" in ids
    assert "gaia.events.read@v1" in ids
    assert "gaia.spacewx.read@v1" in ids
    from gaia.devices.live_p0 import glm_available
    if glm_available():
        assert "gaia.lightning.read@v1" in ids
    else:
        assert "gaia.lightning.read@v1" not in ids
    assert "gaia.alerts.read@v1" in ids
    assert "gaia.argo.read@v1" in ids
    assert "gaia.geomag.read@v1" in ids
    assert "gaia.flood.read@v1" in ids
    assert "gaia.effis.read@v1" in ids
    assert "gaia.volcano.read@v1" in ids
    assert "gaia.ais.public.read@v1" in ids
    assert "gaia.tsunami.read@v1" in ids
    assert "gaia.cyclone.read@v1" in ids
    assert "gaia.adsb.public.read@v1" in ids


def test_well_known_lists_open_relay_tools(live_env, monkeypatch):
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", os.environ["GAIA_SIGNING_KEY_PATH"])
    app = build_app(live_env, public_url="http://gaia.test")
    with TestClient(app) as client:
        wk = client.get("/.well-known/ai-market.json").json()
        assert wk["capabilities_count"] >= 11
        man = client.get("/ai-market/v2/manifest").json()
        tool_names = {t.get("name") or t.get("capability_id") for t in man.get("tools") or []}
        # oracle-core may use name == capability_id
        flat = set()
        for t in man.get("tools") or []:
            flat.add(t.get("capability_id") or t.get("name"))
        assert "gaia.fire.read@v1" in flat or any("fire" in str(x) for x in flat)
        assert any("fire" in str(x) for x in flat) or "gaia.fire.read@v1" in tool_names


def test_invoke_fire_radiation_jamming(live_env, monkeypatch):
    app = build_app(live_env, public_url="http://gaia.test")
    with TestClient(app) as client:
        for cap, device in (
            ("gaia.fire.read@v1", "firms-fire-01"),
            ("gaia.radiation.read@v1", "safecast-01"),
            ("gaia.jamming.read@v1", "cybernews-jam-01"),
        ):
            r = client.post(
                "/ai-market/v2/invoke",
                json={
                    "capability_id": cap,
                    "product_id": "gaia.gateway",
                    "input": {"device_id": device},
                },
            )
            assert r.status_code == 200, (cap, r.text)
            data = r.json()
            assert data["ok"] is True
            vals = data["output"]["reading"]["values"]
            assert "latitude" in vals and "longitude" in vals


def test_feeder_ingest_then_adsb_invoke(live_env):
    app = build_app(live_env, public_url="http://gaia.test")
    with TestClient(app) as client:
        bad = client.post(
            "/feeder/v1/ingest",
            json={
                "device_id": "feeder-adsb-01",
                "fields": {"latitude": 40.7, "longitude": -74.0, "altitude_m": 1000},
            },
        )
        assert bad.status_code == 401

        ok = client.post(
            "/feeder/v1/ingest",
            headers={"Authorization": "Bearer feeder-test-token"},
            json={
                "device_id": "feeder-adsb-01",
                "fields": {
                    "latitude": 40.7,
                    "longitude": -74.0,
                    "altitude_m": 3000,
                    "speed_mps": 120,
                },
            },
        )
        assert ok.status_code == 200
        assert ok.json()["ok"] is True

        r = client.post(
            "/ai-market/v2/invoke",
            json={
                "capability_id": "gaia.adsb.read@v1",
                "product_id": "gaia.gateway",
                "input": {"device_id": "feeder-adsb-01"},
            },
        )
        assert r.status_code == 200
        vals = r.json()["output"]["reading"]["values"]
        assert vals["altitude_m"] == pytest.approx(3000.0)


def test_invoke_p0_p1_capabilities(live_env):
    app = build_app(live_env, public_url="http://gaia.test")
    cases = (
        ("gaia.events.read@v1", "eonet-01"),
        ("gaia.spacewx.read@v1", "swpc-01"),
        ("gaia.alerts.read@v1", "nws-alerts-01"),
        ("gaia.air.read@v1", "sc-01"),
        ("gaia.weather.read@v1", "cwop-01"),
        ("gaia.weather.read@v1", "metno-01"),
        ("gaia.argo.read@v1", "argo-01"),
        ("gaia.geomag.read@v1", "usgs-geomag-01"),
        ("gaia.flood.read@v1", "nws-flood-01"),
        ("gaia.effis.read@v1", "effis-01"),
        ("gaia.volcano.read@v1", "usgs-volcano-01"),
        ("gaia.weather.read@v1", "dwd-01"),
        ("gaia.weather.read@v1", "eccc-01"),
        ("gaia.air.read@v1", "aurn-01"),
        ("gaia.quake.read@v1", "geonet-01"),
        ("gaia.tide.read@v1", "uhslc-01"),
        ("gaia.ais.public.read@v1", "fintraffic-ais-01"),
        ("gaia.tsunami.read@v1", "nws-tsunami-01"),
        ("gaia.river.read@v1", "eccc-hydro-01"),
        ("gaia.river.read@v1", "smhi-hydro-01"),
        ("gaia.weather.read@v1", "fmi-01"),
        ("gaia.cyclone.read@v1", "nhc-cyclone-01"),
        ("gaia.quake.read@v1", "emsc-01"),
        ("gaia.flood.read@v1", "ea-flood-01"),
        ("gaia.tsunami.read@v1", "ptwc-01"),
        ("gaia.adsb.public.read@v1", "adsb-lol-01"),
    )
    with TestClient(app) as client:
        for cap, device in cases:
            r = client.post(
                "/ai-market/v2/invoke",
                json={
                    "capability_id": cap,
                    "product_id": "gaia.gateway",
                    "input": {"device_id": device},
                },
            )
            assert r.status_code == 200, (cap, device, r.text)
            data = r.json()
            assert data["ok"] is True, (cap, device, data)
            vals = data["output"]["reading"]["values"]
            assert vals, (cap, device)


def test_invoke_argo_addresses_specific_wmo(live_env):
    app = build_app(live_env, public_url="http://gaia.test")
    with TestClient(app) as client:
        r = client.post(
            "/ai-market/v2/invoke",
            json={
                "capability_id": "gaia.argo.read@v1",
                "product_id": "gaia.gateway",
                "input": {"device_id": "argo-01", "wmo": "4902911"},
            },
        )
        assert r.status_code == 200
        reading = r.json()["output"]["reading"]
        assert reading["wmo"] == "4902911"
        assert reading["subject_id"] == "argo:wmo:4902911"
        assert reading["values"]


def test_invoke_lightning_fail_closed_when_nodd_empty(live_env):
    ids = {d.device_id for d in live_env.fleet.devices()}
    if "glm-01" not in ids:
        pytest.skip("h5py not installed — GLM not registered")
    app = build_app(live_env, public_url="http://gaia.test")
    with TestClient(app) as client:
        r = client.post(
            "/ai-market/v2/invoke",
            json={
                "capability_id": "gaia.lightning.read@v1",
                "product_id": "gaia.gateway",
                "input": {"device_id": "glm-01"},
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is False
    err = str(data.get("error") or data.get("detail") or "")
    assert "glm-01" in err or "GLM" in err or "NODD" in err or "HTTP" in err
    assert "energy_fj" not in err
    assert not (data.get("output") or {}).get("reading")


def test_open_devices_carry_license_provenance(tmp_path):
    assert "FIRMS" in FirmsFireHotspot.source or "firms" in FirmsFireHotspot.source.lower()
    assert "CC0" in SafecastRadiation.source
    assert "CC BY" in CyberNewsJamming.source
