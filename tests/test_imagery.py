"""Sentinel-2 at a point: COG range reads, the three traps, gaia.imagery.read@v1.

The rasters here are real tiled, deflate-compressed GeoTIFFs written by tifffile at test
time and served by a fake HTTP layer that honours Range — so the reader's actual path
(header in one request, one tile in another) runs without the network. The STAC items
reproduce what Earth Search returned on 2026-10-16: a newer scene whose MGRS tile does
not contain the point, a cloudy one, then a clear one; boa_offset_applied: true with
offset -0.1 still listed.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pytest
import tifffile
from fastapi.testclient import TestClient
from pyproj import Transformer

from gaia import cog as cogmod
from gaia import gazetteer as gz
from gaia.app import build_app
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.clock import SimClock
from gaia.cog import CogReader
from gaia.devices.base import DeviceOffline
from gaia.devices.live_imagery import Sentinel2Point, locate, reflectance
from gaia.fleet import Fleet
from gaia.imagery_capabilities import imagery_read_handler
from gaia.plausibility import PlausibilityVerifier

LAT, LON = 52.5, 15.0
EPSG = 32633
NOW = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc).timestamp()
COGS = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/test"
DN = {"green": 700, "red": 600, "nir": 3000, "swir16": 2000, "swir22": 1200}
GEONAMES = Path(__file__).parent / "fixtures" / "geonames"


def _cog(array: np.ndarray, tile: int) -> bytes:
    buf = io.BytesIO()
    tifffile.imwrite(buf, array, tile=(tile, tile), compression="zlib", predictor=True)
    return buf.getvalue()


def _raster(size: int, value_at: tuple[int, int], value: int, dtype, fill: int) -> np.ndarray:
    a = np.full((size, size), fill, dtype=dtype)
    a[value_at] = value
    return a


class Server:
    """Serves bytes by URL with Range support; counts requests and bytes."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.stac: list[dict] = []
        self.gets: list[tuple[str, str]] = []
        self.bytes = 0
        self.drop_next = 0

    def get(self, url, headers=None, timeout=None, follow_redirects=False):
        req = httpx.Request("GET", url)
        if self.drop_next:
            self.drop_next -= 1
            raise httpx.RemoteProtocolError("Server disconnected", request=req)
        rng = (headers or {}).get("Range", "")
        self.gets.append((url, rng))
        body = self.files.get(url)
        if body is None:
            return httpx.Response(404, request=req)
        if rng.startswith("bytes="):
            a, b = (int(x) for x in rng[6:].split("-"))
            part = body[a:b + 1]
            self.bytes += len(part)
            return httpx.Response(206, content=part, request=req, headers={
                "content-range": f"bytes {a}-{a + len(part) - 1}/{len(body)}"})
        self.bytes += len(body)
        return httpx.Response(200, content=body, request=req)

    def post(self, url, json=None, timeout=None, follow_redirects=False, headers=None):
        req = httpx.Request("POST", url)
        return httpx.Response(200, json={"type": "FeatureCollection", "features": self.stac},
                              request=req)


def _item(name: str, when: str, *, origin_shift_m: float = 0.0, scl_value: int = 4,
          cloud: float = 12.0, applied: bool = True, server: Server,
          lat: float = LAT, lon: float = LON, epsg: int = EPSG) -> dict:
    """A STAC item whose 10 m rasters put the point at (300, 300) (or off the raster)."""
    x, y = Transformer.from_crs(4326, epsg, always_xy=True).transform(lon, lat)
    x0, y0 = x - 300 * 10 - 5 + origin_shift_m, y + 300 * 10 + 5
    assets = {}
    for band, dn in DN.items():
        href = f"{COGS}/{name}/{band}.tif"
        server.files[href] = _cog(_raster(600, (300, 300), dn, np.uint16, 1500), 256)
        assets[band] = {"href": href, "proj:transform": [10, 0, x0, 0, -10, y0],
                        "proj:shape": [600, 600],
                        "raster:bands": [{"nodata": 0, "scale": 0.0001, "offset": -0.1}]}
    href = f"{COGS}/{name}/scl.tif"
    server.files[href] = _cog(_raster(300, (150, 150), scl_value, np.uint8, 4), 128)
    assets["scl"] = {"href": href, "proj:transform": [20, 0, x0, 0, -20, y0],
                     "proj:shape": [300, 300], "raster:bands": [{"nodata": 0}]}
    return {"id": name, "properties": {
        "datetime": when, "proj:epsg": epsg, "eo:cloud_cover": cloud, "grid:code": "MGRS-33UWU",
        "s2:processing_baseline": "05.13", "earthsearch:boa_offset_applied": applied},
        "assets": assets}


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(cogmod.httpx, "get", s.get)
    monkeypatch.setattr(cogmod.httpx, "post", s.post)
    return s


@pytest.fixture
def device(tmp_path, server):
    return Sentinel2Point("s2-l2a-01", SimClock(start_epoch=NOW), site="sentinel2",
                          key_dir=str(tmp_path), reader=CogReader())


# ── the COG reader ────────────────────────────────────────────────────────────


def test_a_pixel_costs_a_header_and_one_tile(server):
    noise = np.random.default_rng(7).integers(1, 10_000, (600, 600), dtype=np.uint16)
    noise[300, 300] = 4242  # incompressible, so the file is bigger than the header read
    data = _cog(noise, 256)
    assert len(data) > cogmod.HEADER_BYTES
    server.files[f"{COGS}/a.tif"] = data
    reader = CogReader()
    assert reader.pixel(f"{COGS}/a.tif", 300, 300) == 4242
    assert len(server.gets) == 2 and server.bytes < len(data)
    assert reader.pixel(f"{COGS}/a.tif", 301, 300) == noise[301, 300]  # cached tile
    assert len(server.gets) == 2


def test_partial_edge_tiles_read_correctly(server):
    a = np.arange(600 * 600, dtype=np.uint32).reshape(600, 600)
    server.files[f"{COGS}/b.tif"] = _cog(a, 256)
    reader = CogReader()
    for row, col in ((599, 599), (0, 599), (599, 0), (512, 257)):
        assert reader.pixel(f"{COGS}/b.tif", row, col) == a[row, col]


def test_a_pixel_outside_the_raster_is_refused(server):
    server.files[f"{COGS}/c.tif"] = _cog(_raster(600, (0, 0), 1, np.uint16, 1), 256)
    with pytest.raises(ValueError, match="outside the raster"):
        CogReader().pixel(f"{COGS}/c.tif", 600, 10)


def test_a_dropped_connection_is_retried(server):
    server.files[f"{COGS}/d.tif"] = _cog(_raster(600, (5, 5), 77, np.uint16, 1), 256)
    server.drop_next = 2
    assert CogReader().pixel(f"{COGS}/d.tif", 5, 5) == 77


def test_a_missing_file_is_offline(server):
    with pytest.raises(DeviceOffline, match="HTTP 404"):
        CogReader().pixel(f"{COGS}/missing.tif", 0, 0)


def test_an_untiled_tiff_is_refused(server):
    buf = io.BytesIO()
    tifffile.imwrite(buf, np.ones((64, 64), dtype=np.uint16))
    server.files[f"{COGS}/strip.tif"] = buf.getvalue()
    with pytest.raises(DeviceOffline, match="not a tiled"):
        CogReader().pixel(f"{COGS}/strip.tif", 1, 1)


def test_a_huge_tile_is_never_downloaded(server, monkeypatch):
    server.files[f"{COGS}/e.tif"] = _cog(_raster(600, (5, 5), 9, np.uint16, 1), 256)
    monkeypatch.setattr(cogmod, "MAX_TILE_BYTES", 10)
    with pytest.raises(DeviceOffline, match="larger than GAIA reads"):
        CogReader().pixel(f"{COGS}/e.tif", 5, 5)


def test_hosts_outside_the_allowlist_are_refused():
    with pytest.raises(Exception, match="not allowed|allowlist|refus"):
        cogmod.http_get("https://evil.example.com/x.tif")


# ── the three traps ───────────────────────────────────────────────────────────


def test_reflectance_does_not_apply_the_offset_twice(server):
    item = _item("x", "2026-10-12T10:26:10Z", server=server)
    asset = item["assets"]["nir"]
    assert reflectance(7, asset, item) == pytest.approx(0.0007)  # Wannsee's NIR, not −0.0993
    item["properties"]["earthsearch:boa_offset_applied"] = False
    assert reflectance(1007, asset, item) == pytest.approx(0.0007)


def test_nodata_is_never_a_reflectance(server):
    item = _item("x", "2026-10-12T10:26:10Z", server=server)
    with pytest.raises(DeviceOffline, match="no data"):
        reflectance(0, item["assets"]["red"], item)


def test_a_raster_that_does_not_contain_the_point_is_not_read(server):
    edge = _item("edge", "2026-10-12T10:26:10Z", origin_shift_m=+9000, server=server)
    assert locate(edge, edge["assets"]["red"], LAT, LON) is None
    inside = _item("in", "2026-10-12T10:26:10Z", server=server)
    assert locate(inside, inside["assets"]["red"], LAT, LON) == (300, 300)
    assert locate(inside, inside["assets"]["scl"], LAT, LON) == (150, 150)


def test_the_newest_clear_view_wins_over_edges_and_clouds(device, server):
    server.stac = [
        _item("S2_EDGE", "2026-10-14T10:00:00Z", origin_shift_m=+9000, server=server),
        _item("S2_CLOUD", "2026-10-12T10:00:00Z", scl_value=9, server=server),
        _item("S2_CLEAR", "2026-10-07T10:00:00Z", server=server),
    ]
    obs = device.observe(LAT, LON, 30)
    v = obs["values"]
    assert obs["scene"]["id"] == "S2_CLEAR" and obs["scene"]["scene_class"] == "vegetation"
    assert obs["scene"]["tried"] == ["S2_CLOUD: SCL 9", "S2_CLEAR: SCL 4"]
    assert v["ndvi"] == pytest.approx((0.30 - 0.06) / (0.30 + 0.06))
    assert v["ndwi"] == pytest.approx((0.07 - 0.30) / (0.07 + 0.30))
    assert v["nbr"] == pytest.approx((0.30 - 0.12) / (0.30 + 0.12))
    assert v["ndmi"] == pytest.approx((0.30 - 0.20) / (0.30 + 0.20))
    assert v["observation_age_days"] == pytest.approx(9.0, abs=0.01)
    assert obs["scene"]["credit"] == "Contains modified Copernicus Sentinel data 2026"
    # a cloudy pixel costs its SCL tile only: no reflectance band of S2_CLOUD was read
    assert not any("S2_CLOUD/red" in url for url, _ in server.gets)


def test_only_clouds_is_offline_and_says_what_it_saw(device, server):
    server.stac = [_item("S2_A", "2026-10-12T10:00:00Z", scl_value=8, server=server),
                   _item("S2_B", "2026-10-07T10:00:00Z", scl_value=3, server=server)]
    with pytest.raises(DeviceOffline, match=r"no clear Sentinel-2 view.*S2_A: SCL 8; S2_B: SCL 3"):
        device.observe(LAT, LON, 30)


def test_no_scene_is_offline(device, server):
    server.stac = []
    with pytest.raises(DeviceOffline, match="no Sentinel-2 scene"):
        device.observe(LAT, LON, 10)


def test_unstaged_reads_are_refused(device):
    with pytest.raises(DeviceOffline, match="need a coordinate"):
        device.sample()


# ── the capability ────────────────────────────────────────────────────────────


@pytest.fixture
def runtime(tmp_path, monkeypatch, server):
    out = tmp_path / "g.sqlite"
    gz.build_from_dir(GEONAMES, out)
    monkeypatch.setenv("GAIA_GAZETTEER_PATH", str(out))
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    monkeypatch.setenv("GAIA_SMARD_ENABLED", "0")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    rt = GatewayRuntime(key_dir=str(tmp_path / "keys"), autotick=False)
    dev = rt.fleet.get("s2-l2a-01")
    dev.clock = SimClock(start_epoch=NOW)
    server.stac = [_item("S2_CLEAR", "2026-10-07T10:00:00Z", server=server)]
    return rt


def test_the_sku_is_listed_priced_and_attests(runtime):
    cap = next(c for c in build_spec(runtime).capabilities
               if c.capability_id == "gaia.imagery.read@v1")
    assert cap.price_per_call_usd == 0.005 and len(cap.description) <= 600
    out = cap.handler({"latitude": LAT, "longitude": LON})
    assert out["scene"]["id"] == "S2_CLEAR"
    assert out["reading"]["site"] == "sentinel2@52.5000,15.0000"
    verdict = PlausibilityVerifier(runtime.fleet).check(out["reading"], out["attestation"])
    assert verdict.verified, verdict.summary
    assert out["reading"]["attribution"].startswith("Contains modified Copernicus Sentinel data")
    assert runtime.fleet.get("s2-l2a-01").site == "sentinel2"  # restored after the read


def test_a_place_name_goes_through_the_gazetteer(runtime, server):
    server.stac = [_item("S2_KRK", "2026-10-07T10:00:00Z", server=server,
                         lat=50.06143, lon=19.93658, epsg=32634)]
    out = imagery_read_handler(runtime)({"city": "Kraków"})
    assert out["scene"]["id"] == "S2_KRK"
    assert out["resolved"]["matched_place"] == "Kraków, Lesser Poland, PL"
    assert out["resolved"]["place_source"] == "geonames"


@pytest.mark.parametrize("inp, msg", [
    ({}, "needs a place"),
    ({"latitude": 1}, "together"),
    ({"latitude": LAT, "longitude": LON, "max_age_days": 0}, "max_age_days must be"),
    ({"latitude": LAT, "longitude": LON, "max_age_days": "old"}, "must be a number"),
])
def test_bad_input_is_refused_with_a_reason(runtime, inp, msg):
    with pytest.raises(ValueError, match=msg):
        imagery_read_handler(runtime)(inp)


def test_over_http_it_is_paid_signed_and_offline_costs_nothing(runtime, server):
    client = TestClient(build_app(runtime, public_url="http://gaia.test"))
    r = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.imagery.read@v1", "input": {"latitude": LAT, "longitude": LON}})
    assert r.status_code == 200 and r.json()["receipt"]["price_usd"] == 0.005
    assert r.headers.get("x-provider-signature")
    server.stac = []
    r = client.post("/ai-market/v2/invoke", json={
        "capability_id": "gaia.imagery.read@v1", "input": {"latitude": LAT, "longitude": LON}})
    body = r.json()
    # ok:false — the hub debits nothing for it (hub_routes/invoke.py) and signs no receipt
    assert body["ok"] is False and "no Sentinel-2 scene" in body["error"]
    assert "receipt" not in body


def test_licence_and_hosts_are_registered():
    from gaia.devices._policy import ALLOWED_HOSTS
    from gaia.source_policy import require_approved_source

    policy = require_approved_source("copernicus_sentinel2")
    for host in ("earth-search.aws.element84.com", "sentinel-cogs.s3.us-west-2.amazonaws.com"):
        assert host in ALLOWED_HOSTS and host in policy.hosts
