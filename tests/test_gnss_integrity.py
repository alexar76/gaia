"""GNSS station integrity contract — real shapes, no network."""

from __future__ import annotations

from gaia.capabilities import _gnss_integrity_handler
from gaia.clock import SimClock
from gaia.devices.gnss import (
    EurefGnssIntegrity,
    GaGnssInventory,
    parse_euref_station_html,
    parse_ga_site_logs,
)
from gaia.source_policy import APPROVED_SOURCES, require_approved_source


EPN_HTML = """
<html><body>
<table id="tableoverview">
  <tr><th>Station</th><th>Station name</th><th>Country</th><th>Status</th>
      <th>Latitude</th><th>Longitude</th><th>Elevation</th></tr>
  <tr><td>BRUX00BEL</td><td>Brussels</td><td>Belgium</td><td>Operational</td>
      <td>50.7981</td><td>4.3586</td><td>158</td></tr>
  <tr><td>ONSA00SWE</td><td>Onsala</td><td>Sweden</td><td>Operational</td>
      <td>57.3953</td><td>11.9255</td><td>45</td></tr>
</table>
<table id="tableDA">
  <tr><th>Station</th><th>Agency</th><th>Daily availability</th><th>Hourly availability</th></tr>
  <tr><td>BRUX00BEL</td><td>ROB</td><td>99.8%</td><td>98.0%</td></tr>
  <tr><td>ONSA00SWE</td><td>OSO</td><td>91.0%</td><td>82.0%</td></tr>
</table>
<table id="tableDL">
  <tr><th>Station</th><th>Agency</th><th>Daily latency</th><th>Real-time latency</th></tr>
  <tr><td>BRUX00BEL</td><td>ROB</td><td>30 s</td><td>4 s</td></tr>
  <tr><td>ONSA00SWE</td><td>OSO</td><td>12 min</td><td>180 s</td></tr>
</table>
</body></html>
"""

GA_SITE_LOGS = {
    "_embedded": {
        "siteLogs": [
            {
                "siteIdentification": {
                    "nineCharacterId": "ALIC00AUS",
                    "siteName": "Alice Springs",
                },
                "siteLocation": {
                    "country": "Australia",
                    "geodeticPosition": {"latitude": -23.6701, "longitude": 133.8855},
                },
                "dateInstalled": "1992-01-01",
            }
        ]
    },
    "page": {"totalElements": 1979},
}


def test_only_reviewed_sources_enter_policy_registry():
    assert "euref_epn" in APPROVED_SOURCES
    assert "adsbexchange" not in APPROVED_SOURCES
    assert require_approved_source("euref_epn").licence == "CC BY 4.0"
    require_approved_source("euref_epn").require_endpoint(
        "https://www.epncb.oma.be/_networkdata/stationlist.php"
    )


def test_source_policy_rejects_unreviewed_host_and_plain_http():
    policy = require_approved_source("euref_epn")
    for url in ("https://example.com/stations", "http://www.epncb.oma.be/stations"):
        try:
            policy.require_endpoint(url)
        except ValueError:
            pass
        else:  # pragma: no cover - makes the policy failure explicit
            raise AssertionError(f"policy accepted unsafe endpoint: {url}")


def test_euref_uses_same_provider_open_portal_as_inventory_fallback(monkeypatch, tmp_path):
    device = EurefGnssIntegrity(
        "gnss-euref-01", SimClock(1_767_225_600), site="test", key_dir=tmp_path,
    )
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        if url == device._URL:
            from gaia.devices.base import DeviceOffline
            raise DeviceOffline("primary unavailable")
        return EPN_HTML

    monkeypatch.setattr(device, "_fetch_text", fetch)
    reading = device.read()["reading"]
    assert calls == [device._URL, device._INVENTORY_URL]
    assert reading["source_url"] == device._INVENTORY_URL
    assert all(row["source_url"] == device._INVENTORY_URL for row in reading["hotspots"])


def test_euref_parser_preserves_coordinates_and_real_metrics():
    rows = parse_euref_station_html(EPN_HTML)
    assert [row["station_id"] for row in rows] == ["BRUX00BEL", "ONSA00SWE"]
    assert rows[0]["latitude"] == 50.7981
    assert rows[0]["availability_pct"] == 98.0
    assert rows[0]["latency_s"] == 4.0
    assert rows[1]["availability_pct"] == 82.0
    assert rows[1]["latency_s"] == 180.0


def test_euref_read_emits_clickable_inventory_and_honest_claim(monkeypatch, tmp_path):
    device = EurefGnssIntegrity(
        "gnss-euref-01", SimClock(1_767_225_600, realtime=False),
        site="test", key_dir=tmp_path,
    )
    monkeypatch.setattr(device, "_fetch_text", lambda *_args, **_kw: EPN_HTML)
    out = device.read()
    reading = out["reading"]
    assert reading["hotspot_count"] == 2
    assert reading["hotspots"][0]["point_id"].startswith("gnss-station:euref:")
    assert reading["evidence_boundary"].startswith("Position is an EPN inventory fact")
    assert reading["cause"] == "unestablished"
    assert reading["license_url"].endswith("/licenses/by/4.0/")
    assert reading["modified"] is True
    assert out["attestation"]["algorithm"] == "ed25519"
    assert out["attestation"]["value"]

    device.set_station("BRUX00BEL")
    exact = device.read()["reading"]
    device.clear_station()
    assert exact["query_station_id"] == "BRUX00BEL"
    assert exact["hotspot_count"] == 1
    assert exact["claim_class"] == "derived_degradation"


def test_ga_inventory_is_clickable_but_never_invents_integrity(monkeypatch, tmp_path):
    parsed = parse_ga_site_logs(GA_SITE_LOGS)
    assert parsed[0]["station_id"] == "ALIC00AUS"
    assert parsed[0]["claim_class"] == "inventory_only"

    device = GaGnssInventory(
        "gnss-ga-01", SimClock(1_767_225_600, realtime=False),
        site="test", key_dir=tmp_path,
    )
    monkeypatch.setattr(device, "_fetch", lambda *_args, **_kw: GA_SITE_LOGS)
    reading = device.read()["reading"]
    assert reading["hotspots"][0]["point_id"] == "gnss-station:ga:ALIC00AUS"
    assert reading["state"] == "unknown"
    assert reading["stations_reporting_now"] == 0
    assert "No RF or current integrity state" in reading["evidence_boundary"]


def test_ga_inventory_fetches_every_declared_page(monkeypatch, tmp_path):
    first = {
        **GA_SITE_LOGS,
        "page": {"totalPages": 2, "number": 0},
    }
    second = {
        "_embedded": {"siteLogs": [{
            "siteIdentification": {"fourCharacterId": "YARR", "siteName": "Yarragadee"},
            "siteLocation": {
                "country": "Australia",
                "geodeticPosition": {"latitude": -29.0464, "longitude": 115.3467},
            },
        }]},
        "page": {"totalPages": 2, "number": 1},
    }
    device = GaGnssInventory(
        "gnss-ga-01", SimClock(1_767_225_600), site="test", key_dir=tmp_path,
    )
    calls = []

    def fetch(url):
        calls.append(url)
        return second if "page=1" in url else first

    monkeypatch.setattr(device, "_fetch", fetch)
    reading = device.read()["reading"]
    assert reading["inventory_total"] == 2
    assert {p["station_id"] for p in reading["hotspots"]} == {"ALIC00AUS", "YARR"}
    assert calls == [device._URL, f"{device._URL}&page=1"]


def test_exact_ga_virtual_point_reads_ga_not_default_euref(tmp_path):
    euref = EurefGnssIntegrity(
        "gnss-euref-01", SimClock(1_767_225_600), site="test", key_dir=tmp_path,
    )
    ga = GaGnssInventory(
        "gnss-ga-01", SimClock(1_767_225_600), site="test", key_dir=tmp_path,
    )

    class _Fleet:
        devices = {"gnss-euref-01": euref, "gnss-ga-01": ga}

        def get(self, device_id):
            return self.devices[device_id]

    class _Runtime:
        fleet = _Fleet()

        def __init__(self):
            self.read_ids = []

        def read(self, device_id):
            self.read_ids.append(device_id)
            return {"device_id": device_id, "station_id": self.fleet.get(device_id)._query_station}

    runtime = _Runtime()
    out = _gnss_integrity_handler(runtime, "gnss-euref-01")({
        "device_id": "gnss-station:ga:ALIC00AUS",
    })
    assert out == {"device_id": "gnss-ga-01", "station_id": "ALIC00AUS"}
    assert runtime.read_ids == ["gnss-ga-01"]
    assert ga._query_station is None


def test_jamming_capability_copy_denies_raw_rf_sensing():
    """gaia.jamming.read@v1 is curated intel; keep that sentence in the catalog."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "gaia" / "capabilities.py").read_text(
        encoding="utf-8"
    )
    assert "not raw RF sensing" in src
    assert "not independent proof of RF jamming" in src
