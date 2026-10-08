"""P9 relay mapper tests — fixtures only, no live HTTP."""

from __future__ import annotations

from datetime import datetime, timezone

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p9 import (
    EHYD_MESH,
    HUBEAU_MESH,
    SMHI_METOBS_MESH,
    EhydRiver,
    HubEauRiver,
    SmhiMetObs,
    register_p9_relays,
)
from gaia.source_policy import require_approved_source


def test_p9_source_policies_are_commercial():
    expected = {
        "hubeau_hydro": "Etalab Licence Ouverte 2.0",
        "ehyd_austria": "CC BY 4.0",
        "smhi_metobs": "CC BY 4.0",
    }
    for source_id, licence in expected.items():
        policy = require_approved_source(source_id)
        assert policy.licence == licence
        assert "commercial" in policy.commercial_basis.lower()


def test_hubeau_map_converts_mm_stage(tmp_path):
    station, lat, lon = HUBEAU_MESH[0][1], HUBEAU_MESH[0][2], HUBEAU_MESH[0][3]
    dev = HubEauRiver(
        "hubeau-paris-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now = datetime.now(timezone.utc).isoformat()
    q = {"data": [{"resultat_obs": 420.0, "date_obs": now,
                   "latitude": lat, "longitude": lon}]}
    h = {"data": [{"resultat_obs": 980.0, "date_obs": now,
                   "latitude": lat, "longitude": lon}]}
    mapped = dev.map((q, h))
    assert mapped["discharge_m3s"] == 0.42
    assert abs((mapped["gage_height_m"] or 0) - 0.98) < 1e-9


def test_hubeau_rejects_absurd_q(tmp_path):
    station, lat, lon = HUBEAU_MESH[0][1], HUBEAU_MESH[0][2], HUBEAU_MESH[0][3]
    dev = HubEauRiver(
        "hubeau-paris-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now = datetime.now(timezone.utc).isoformat()
    q = {"data": [{"resultat_obs": 26_000_000.0, "date_obs": now}]}
    h = {"data": [{"resultat_obs": 980.0, "date_obs": now}]}
    mapped = dev.map((q, h))
    assert mapped["discharge_m3s"] is None
    assert mapped["gage_height_m"] == 0.98


def test_hubeau_empty_is_offline(tmp_path):
    station, lat, lon = HUBEAU_MESH[0][1], HUBEAU_MESH[0][2], HUBEAU_MESH[0][3]
    dev = HubEauRiver(
        "hubeau-paris-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        dev.map(({"data": []}, {"data": []}))
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_hubeau_stale_observations_are_not_sold(tmp_path):
    station, lat, lon = HUBEAU_MESH[0][1], HUBEAU_MESH[0][2], HUBEAU_MESH[0][3]
    dev = HubEauRiver(
        "hubeau-paris-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    stale = "2020-01-01T00:00:00Z"
    try:
        dev.map((
            {"data": [{"resultat_obs": 420.0, "date_obs": stale}]},
            {"data": [{"resultat_obs": 980.0, "date_obs": stale}]},
        ))
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_ehyd_map_q(tmp_path):
    hzbnr, lat, lon = EHYD_MESH[0][1], EHYD_MESH[0][2], EHYD_MESH[0][3]
    dev = EhydRiver(
        "ehyd-achleiten-01", SimClock(realtime=True),
        hzbnr=hzbnr, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    payload = {
        "features": [{
            "properties": {
                "hzbnr": int(hzbnr), "wert": 641.0, "parameter": "Q",
                "einheit": "m³/s", "zeitpunkt": datetime.now(timezone.utc).isoformat(),
            },
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        }]
    }
    mapped = dev.map(payload)
    assert mapped["discharge_m3s"] == 641.0
    assert mapped["latitude"] == lat


def test_smhi_metobs_map(tmp_path):
    station, lat, lon = SMHI_METOBS_MESH[0][1], SMHI_METOBS_MESH[0][2], SMHI_METOBS_MESH[0][3]
    dev = SmhiMetObs(
        "smhi-wx-arlanda-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    readings = {
        "temperature_c": {"value": [{"date": now_ms, "value": "10.7"}]},
        "humidity_pct": {"value": [{"date": now_ms, "value": "72"}]},
        "pressure_hpa": {"value": [{"date": now_ms, "value": "1012"}]},
        "wind_mps": {"value": [{"date": now_ms, "value": "3.2"}]},
    }
    mapped = dev.map(readings)
    assert mapped["temperature_c"] == 10.7
    assert mapped["humidity_pct"] == 72.0
    assert mapped["pressure_hpa"] == 1012.0
    assert mapped["wind_mps"] == 3.2


def test_p9_relays_reject_missing_observation_times(tmp_path):
    hzbnr, lat, lon = EHYD_MESH[0][1], EHYD_MESH[0][2], EHYD_MESH[0][3]
    ehyd = EhydRiver(
        "ehyd-achleiten-01", SimClock(realtime=True),
        hzbnr=hzbnr, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        ehyd.map({"features": [{"properties": {
            "hzbnr": int(hzbnr), "wert": 641.0, "parameter": "Q", "einheit": "m³/s",
        }}]})
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass

    station = HUBEAU_MESH[0][1]
    hubeau = HubEauRiver(
        "hubeau-paris-01", SimClock(realtime=True), station=station,
        latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )
    try:
        hubeau.map((
            {"data": [{"resultat_obs": 420.0}]},
            {"data": [{"resultat_obs": 980.0}]},
        ))
        assert False, "expected DeviceOffline"
    except DeviceOffline:
        pass


def test_hubeau_accepts_http_206(tmp_path, monkeypatch):
    station, lat, lon = HUBEAU_MESH[8][1], HUBEAU_MESH[8][2], HUBEAU_MESH[8][3]
    dev = HubEauRiver(
        "hubeau-meuse-sm-01", SimClock(realtime=True),
        station=station, latitude=lat, longitude=lon, key_dir=str(tmp_path),
    )

    class _Resp:
        status_code = 206

        def json(self):
            return {"data": [{"resultat_obs": 1420.0, "date_obs": "2026-09-10T10:00:00Z",
                              "latitude": lat, "longitude": lon}]}

    monkeypatch.setattr("gaia.devices.live_p9.httpx.get", lambda *a, **k: _Resp())
    payload = dev._fetch(dev._q_url)
    assert payload["data"][0]["resultat_obs"] == 1420.0


def test_register_p9_meshes(tmp_path, monkeypatch):
    class Fleet:
        def __init__(self):
            self._devices = {}

        def add(self, device):
            self._devices[device.device_id] = device

        @property
        def ids(self):
            return set(self._devices)

    monkeypatch.setenv("GAIA_HUBEAU_ENABLED", "1")
    monkeypatch.setenv("GAIA_EHYD_ENABLED", "1")
    monkeypatch.setenv("GAIA_SMHI_METOBS_ENABLED", "1")
    fleet = Fleet()
    n = register_p9_relays(fleet, SimClock(realtime=True), key_dir=str(tmp_path))
    assert n == len(HUBEAU_MESH) + len(EHYD_MESH) + len(SMHI_METOBS_MESH)
    assert "hubeau-paris-01" in fleet.ids
    assert "ehyd-achleiten-01" in fleet.ids
    assert "smhi-wx-arlanda-01" in fleet.ids
