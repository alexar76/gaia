"""P0 relay parsers and mappers — no upstream HTTP."""

from __future__ import annotations

import io
import gzip
from datetime import datetime, timedelta, timezone

import pytest

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import (
    ArgoFloat,
    CwopStation,
    EonetEvents,
    GoesGlmLightning,
    MetNorwayMetar,
    NwsCapAlerts,
    SensorCommunityAir,
    SwpcSpaceWeather,
    UsgsGeomag,
    _s3_nc_keys,
    geojson_centroid,
    resolve_wgs84,
    parse_glm_lcfa,
    parse_metar,
)


def test_parse_metar_engm():
    text = "ENGM 122150Z 18008KT 9999 FEW030 17/12 Q1013 NOSIG"
    out = parse_metar(text)
    assert out["temperature_c"] == 17.0
    assert abs(out["humidity_pct"] - 72.0) < 5.0
    assert out["pressure_hpa"] == 1013.0
    assert abs(out["wind_mps"] - 8 * 0.514444) < 0.01


def test_parse_metar_negative_temp():
    text = "ENGM 122150Z 00000KT 9999 M01/M03 Q0998"
    out = parse_metar(text)
    assert out["temperature_c"] == -1.0
    assert out["pressure_hpa"] == 998.0


def test_geojson_centroid_polygon():
    geom = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]],
    }
    lat, lon = geojson_centroid(geom)
    assert abs(lat - 0.8) < 0.5  # ring average, not area centroid
    assert abs(lon - 0.8) < 0.5


def test_resolve_wgs84_repairs_wfs_lat_lon_for_spain():
    # Live EFFIS WFS: Peñamellera Alta, Asturias. First axis is latitude.
    geom = {"type": "Point", "coordinates": [43.3433, -4.6893]}
    lat, lon = resolve_wgs84(geom, country="ES", prefer_lat_first_when_ambiguous=True)
    assert abs(lat - 43.3433) < 1e-6
    assert abs(lon + 4.6893) < 1e-6
    # Same fire already in RFC 7946 (lon, lat) must stay in Asturias.
    rfc = {"type": "Point", "coordinates": [-4.6893, 43.3433]}
    lat2, lon2 = resolve_wgs84(rfc, country="ES")
    assert abs(lat2 - 43.3433) < 1e-6
    assert abs(lon2 + 4.6893) < 1e-6


def test_eonet_collect_hotspots(tmp_path):
    clock = SimClock(realtime=True)
    dev = EonetEvents("eonet-01", clock, key_dir=str(tmp_path))
    payload = {
        "events": [
            {
                "id": "EONET_1",
                "title": "Kilauea",
                "categories": [{"id": "volcanoes", "title": "Volcanoes"}],
                "geometry": [{"type": "Point", "coordinates": [-155.2, 19.4], "magnitudeValue": 2}],
            },
            {
                "id": "EONET_2",
                "title": "Storm",
                "categories": [{"id": "severeStorms", "title": "Severe Storms"}],
                "geometry": [{"type": "Point", "coordinates": [-80.0, 25.0]}],
            },
        ]
    }
    hs = dev.collect_hotspots(payload)
    assert hs[0]["event_id"] == "EONET_1"
    assert hs[0]["latitude"] == 19.4
    assert hs[0]["severity_score"] > hs[1]["severity_score"]


def test_swpc_kp_map(tmp_path):
    clock = SimClock(realtime=True)
    dev = SwpcSpaceWeather("swpc-01", clock, key_dir=str(tmp_path))
    mapped = dev.map([{"time_tag": "t", "kp_index": 4.33, "estimated_kp": 4.3}])
    assert mapped["kp_index"] == 4.33
    assert mapped["latitude"] == 40.015


def test_cap_collect(tmp_path):
    clock = SimClock(realtime=True)
    dev = NwsCapAlerts("nws-alerts-01", clock, key_dir=str(tmp_path))
    payload = {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [-97.5, 35.5]},
                "properties": {
                    "severity": "Extreme",
                    "event": "Tornado Warning",
                    "headline": "Tornado Warning for …",
                    "areaDesc": "Oklahoma",
                },
            },
            {
                "geometry": None,
                "properties": {"severity": "Severe", "event": "No geom"},
            },
        ]
    }
    hs = dev.collect_hotspots(payload)
    assert len(hs) == 1
    assert hs[0]["severity_score"] == 95.0
    assert hs[0]["event"] == "Tornado Warning"


def test_sensor_community_map(tmp_path):
    clock = SimClock(realtime=True)
    dev = SensorCommunityAir("sc-01", clock, key_dir=str(tmp_path))
    payload = [
        {
            "location": {"latitude": "52.52", "longitude": "13.41"},
            "sensordatavalues": [
                {"value_type": "P2", "value": "12.5"},
                {"value_type": "P1", "value": "18.0"},
                {"value_type": "temperature", "value": "16.2"},
            ],
        }
    ]
    mapped = dev.map(payload)
    assert mapped["pm2_5_ugm3"] == 12.5
    assert mapped["pm10_ugm3"] == 18.0
    assert mapped["temperature_c"] == 16.2


def test_cwop_map_tmpf(tmp_path):
    clock = SimClock(realtime=True)
    dev = CwopStation("cwop-01", clock, station="EW1156", key_dir=str(tmp_path))
    mapped = dev.map({
        "data": [{
            "tmpf": 68.0,
            "relh": 55.0,
            "mslp": 1013.2,
            "sknt": 4.0,
            "lat": 40.22,
            "lon": -74.01,
        }]
    })
    assert abs(mapped["temperature_c"] - 20.0) < 0.1
    assert mapped["humidity_pct"] == 55.0
    assert mapped["pressure_hpa"] == 1013.2


def test_cwop_map_tmpc(tmp_path):
    clock = SimClock(realtime=True)
    dev = CwopStation("cwop-01", clock, station="EW1156", key_dir=str(tmp_path))
    mapped = dev.map({"last": [{"tmpc": 11.0, "relh": 80.0, "lat": 1.0, "lon": 2.0}]})
    assert mapped["temperature_c"] == 11.0


def test_argo_erddap_map(tmp_path):
    clock = SimClock(realtime=True)
    dev = ArgoFloat("argo-01", clock, wmo="4902911", key_dir=str(tmp_path))
    payload = {
        "table": {
            "columnNames": ["time", "latitude", "longitude", "pres", "temp", "psal"],
            "rows": [["2026-08-12T00:00:00Z", 25.1, -70.2, 8.0, 26.4, 36.1]],
        }
    }
    mapped = dev.map(payload)
    assert mapped["temperature_c"] == 26.4
    assert mapped["salinity_psu"] == 36.1
    assert mapped["latitude"] == 25.1


def test_argo_erddap_chooses_shallowest_sample_from_latest_profile(tmp_path):
    clock = SimClock(realtime=True)
    dev = ArgoFloat("argo-01", clock, key_dir=str(tmp_path))
    payload = {
        "table": {
            "columnNames": ["time", "latitude", "longitude", "pres", "temp", "psal"],
            "rows": [
                ["2026-08-12T00:00:00Z", 25.1, -70.2, 18.0, 25.0, 36.2],
                ["2026-08-12T00:00:00Z", 25.1, -70.2, 2.0, 26.7, 36.0],
                ["2026-08-11T00:00:00Z", 25.0, -70.0, 1.0, 27.0, 35.9],
            ],
        }
    }
    mapped = dev.map(payload)
    assert mapped["pressure_dbar"] == 2.0
    assert mapped["temperature_c"] == 26.7


def test_argo_erddap_rejects_bad_qc_instead_of_showing_fake_physics(tmp_path):
    clock = SimClock(realtime=True)
    dev = ArgoFloat("argo-01", clock, key_dir=str(tmp_path))
    payload = {
        "table": {
            "columnNames": [
                "time", "latitude", "longitude", "pres", "pres_qc",
                "temp", "temp_qc", "psal", "psal_qc",
            ],
            "rows": [
                ["2026-08-12T00:00:00Z", 1.626, 44.637, -0.2, "4", 34.851, "4", 0.015, "4"],
            ],
        }
    }
    mapped = dev.map(payload)
    assert mapped["latitude"] == 1.626
    assert mapped["temperature_c"] is None
    assert mapped["salinity_psu"] is None
    assert mapped["pressure_dbar"] is None


def test_argo_global_index_keeps_latest_position_per_active_wmo():
    text = "\n".join(
        [
            "# Title : Profile directory file of the Argo Global Data Assembly Center",
            "# Date of update : 20260813092427",
            "file,date,latitude,longitude,ocean,profiler_type,institution,date_update",
            "aoml/4902911/profiles/R4902911_100.nc,20260801010000,10.0,-30.0,A,846,AO,20260801020000",
            "aoml/4902911/profiles/R4902911_101.nc,20260812010000,11.0,-31.0,A,846,AO,20260812020000",
            "coriolis/6901234/profiles/R6901234_007.nc,20260720010000,-42.0,80.0,I,838,IF,20260720020000",
            "coriolis/6909999/profiles/R6909999_001.nc,20260601010000,-20.0,30.0,I,838,IF,20260601020000",
        ]
    )
    rows, meta = ArgoFloat.parse_gdac_index(
        gzip.compress(text.encode("utf-8")),
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    assert meta["active_float_count"] == 2
    assert meta["active_window_days"] == 30
    by_wmo = {row["wmo"]: row for row in rows}
    assert by_wmo["4902911"]["latitude"] == 11.0
    assert by_wmo["4902911"]["profile_url"].endswith("R4902911_101.nc")
    assert "6909999" not in by_wmo


def test_geomag_map_last_non_null(tmp_path):
    clock = SimClock(realtime=True)
    dev = UsgsGeomag("usgs-geomag-01", clock, observatory="BOU", key_dir=str(tmp_path))
    payload = {
        "metadata": {"intermagnet": {"imo": {"coordinates": [-105.2372, 40.1375, 1682]}}},
        "values": [{"id": "F", "values": [None, 51234.1, None]}],
    }
    mapped = dev.map(payload)
    assert mapped["field_nt"] == 51234.1
    assert abs(mapped["latitude"] - 40.1375) < 1e-4


def test_geomag_uses_station_fallback_and_reports_observation_age(tmp_path):
    clock = SimClock(realtime=True)
    dev = UsgsGeomag(
        "usgs-geomag-brw",
        clock,
        observatory="BRW",
        latitude=71.322,
        longitude=-156.622,
        key_dir=str(tmp_path),
    )
    observed = datetime.now(timezone.utc) - timedelta(seconds=90)
    payload = {
        "metadata": {},
        "times": [observed.isoformat().replace("+00:00", "Z"), None],
        "values": [{"id": "F", "values": [57114.076, None]}],
    }
    mapped = dev.map(payload)
    assert mapped["field_nt"] == 57114.076
    assert mapped["latitude"] == 71.322
    assert mapped["longitude"] == -156.622
    assert 85 <= mapped["observation_age_s"] <= 100
    assert "id=BRW" in dev._request_url()


def test_metno_sample_from_text(tmp_path, monkeypatch):
    clock = SimClock(realtime=True)
    dev = MetNorwayMetar("metno-01", clock, icao="ENGM", key_dir=str(tmp_path))
    monkeypatch.setattr(
        dev,
        "_fetch_text",
        lambda url: "ENGM 122150Z 18008KT 9999 FEW030 17/12 Q1013 NOSIG\n",
    )
    sample = dev.sample()
    assert sample["temperature_c"] == 17.0
    assert sample["pressure_hpa"] == 1013.0


def test_s3_nc_keys_from_list_xml():
    xml = """<?xml version="1.0"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Contents><Key>GLM-L2-LCFA/2026/225/04/OR_GLM-L2-LCFA_G19_s20262250400000.nc</Key></Contents>
      <Contents><Key>GLM-L2-LCFA/2026/225/04/OR_GLM-L2-LCFA_G19_s20262250401000.nc</Key></Contents>
      <Contents><Key>not-a-netcdf.txt</Key></Contents>
    </ListBucketResult>"""
    keys = _s3_nc_keys(xml)
    assert keys[-1].endswith("s20262250401000.nc")
    assert all(k.endswith(".nc") for k in keys)


def test_parse_glm_lcfa_energy_fj():
    h5py = pytest.importorskip("h5py")
    import numpy as np

    buf = io.BytesIO()
    with h5py.File(buf, "w") as handle:
        handle.create_dataset("flash_lat", data=np.array([25.0, 30.0], dtype="f4"))
        handle.create_dataset("flash_lon", data=np.array([-80.0, -90.0], dtype="f4"))
        handle.create_dataset("flash_energy", data=np.array([2.5e-14, 1.0e-15], dtype="f8"))
    flashes = parse_glm_lcfa(buf.getvalue())
    assert flashes[0]["energy_fj"] == pytest.approx(25.0)
    assert flashes[0]["latitude"] == pytest.approx(25.0)
    assert flashes[1]["energy_fj"] == pytest.approx(1.0)


def test_parse_glm_lcfa_packed_scale_factor():
    h5py = pytest.importorskip("h5py")
    import numpy as np

    buf = io.BytesIO()
    with h5py.File(buf, "w") as handle:
        handle.create_dataset("flash_lat", data=np.array([25.0], dtype="f4"))
        handle.create_dataset("flash_lon", data=np.array([-80.0], dtype="f4"))
        energy = handle.create_dataset("flash_energy", data=np.array([2500], dtype="u2"))
        energy.attrs["scale_factor"] = 1.0e-15
        energy.attrs["add_offset"] = 0.0
    flashes = parse_glm_lcfa(buf.getvalue())
    assert flashes[0]["energy_fj"] == pytest.approx(2500.0)


def test_num_accepts_numpy_scalars():
    np = pytest.importorskip("numpy")
    from gaia.devices.live import _num

    assert _num(np.float32(25.0)) == pytest.approx(25.0)
    assert _num(np.float64(2.5e-14)) == pytest.approx(2.5e-14)
    assert _num(True) is None
    assert _num(" 12.5 ") == 12.5


def test_glm_map_uses_energy_fj(tmp_path):
    clock = SimClock(realtime=True)
    dev = GoesGlmLightning("glm-01", clock, key_dir=str(tmp_path))
    mapped = dev.map([{"latitude": 25.0, "longitude": -80.0, "energy_fj": 12.5}])
    assert mapped["energy_fj"] == 12.5
    assert mapped["latitude"] == 25.0


def test_eonet_cap_glm_empty_fail_closed(tmp_path):
    clock = SimClock(realtime=True)
    eonet = EonetEvents("eonet-01", clock, key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline, match="no open events"):
        eonet.collect_hotspots({"events": []})
    cap = NwsCapAlerts("nws-alerts-01", clock, key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline, match="empty"):
        cap.collect_hotspots({"features": []})
    glm = GoesGlmLightning("glm-01", clock, key_dir=str(tmp_path))
    with pytest.raises(DeviceOffline, match="no flashes"):
        glm.collect_hotspots([])


def test_p0_devices_carry_license_provenance():
    assert "EONET" in EonetEvents.source
    assert "public domain" in SwpcSpaceWeather.source.lower() or "PD" in SwpcSpaceWeather.source
    assert "CAP" in NwsCapAlerts.source or "weather.gov" in NwsCapAlerts.source
    assert "ODbL" in SensorCommunityAir.source
    assert "CWOP" in CwopStation.source
    assert "10.17882/42182" in ArgoFloat.source
    assert "CC BY 4.0" in MetNorwayMetar.source
    assert "INTERMAGNET" in UsgsGeomag.source
    assert "not INTERMAGNET" in UsgsGeomag.source.lower() or "Not INTERMAGNET" in UsgsGeomag.source
    assert "GOES-19" in GoesGlmLightning.source
    assert "Blitzortung" in GoesGlmLightning.source
