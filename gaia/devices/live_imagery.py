"""Sentinel-2 L2A at a point: the latest cloud-free look at any coordinate on land.

GAIA's other relays are stations; this one is a satellite. For a coordinate it finds
the newest Sentinel-2 Level-2A scenes (Element84 Earth Search STAC, keyless), takes one
whose raster actually contains the point, reads the scene classification there first
(20 m, ~130 KB) and only for a clear pixel the five reflectance bands (10/20 m, one
compressed tile each, in parallel), and derives:

    NDVI = (NIR − red) / (NIR + red)          vegetation vigour
    NDWI = (green − NIR) / (green + NIR)      open water (McFeeters)
    NBR  = (NIR − SWIR2) / (NIR + SWIR2)      burn scars (fire severity via dNBR)
    NDMI = (NIR − SWIR1) / (NIR + SWIR1)      vegetation moisture

**Licence.** Copernicus Sentinel data are free, full and open, commercial use included,
with the credit "Contains modified Copernicus Sentinel data <year>".

**Three facts the code depends on, each found the hard way on 2026-10-16:**

* Earth Search sets ``earthsearch:boa_offset_applied: true`` and still publishes
  ``offset: -0.1`` in ``raster:bands``. The offset is already in the numbers: applying it
  again turned Wannsee's near-infrared (DN 7, i.e. 0.0007 — water) into −0.0993.
  So reflectance = DN × scale when the flag is set; the offset applies only when not.
* A point near a UTM zone edge lies in two MGRS tiles, and the STAC search returns both
  — but one raster may not contain it (Müggelsee sits east of tile 32UQD's last column).
  A pixel is read only from a raster whose bounds hold it.
* Clouds are per pixel, not per scene: a 9 % cloudy scene put SCL 8 (cloud) on
  Grunewald forest. Only scene classes 2/4/5/6/7/11 count as a view of the ground.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from gaia.clock import SimClock
from gaia.cog import CogReader, http_post_json
from gaia.devices.base import DeviceOffline
from gaia.devices._live_base import LiveDevice
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_imagery")

STAC_SEARCH = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-l2a"
BANDS = ("green", "red", "nir", "swir16", "swir22")

#: Sentinel-2 scene classification (SCL). A view of the ground: dark area, vegetation,
#: bare soil, water, unclassified, snow. Not: no data, saturated, cloud shadow, clouds,
#: thin cirrus.
SCL_CLEAR = {2: "dark area", 4: "vegetation", 5: "bare soil", 6: "water",
             7: "unclassified", 11: "snow or ice"}
SCL_BLOCKED = {0: "no data", 1: "saturated or defective", 3: "cloud shadow",
               8: "cloud (medium probability)", 9: "cloud (high probability)",
               10: "thin cirrus"}

MAX_ITEMS_TRIED = 6
MAX_AGE_DAYS_LIMIT = 120


def _epsg(item: dict[str, Any]) -> int:
    props = item.get("properties") or {}
    code = props.get("proj:epsg") or str(props.get("proj:code", "")).split(":")[-1]
    try:
        return int(code)
    except (TypeError, ValueError):
        raise DeviceOffline("STAC item has no projection") from None


def _transformer(epsg: int):
    from pyproj import Transformer

    return Transformer.from_crs(4326, epsg, always_xy=True)


def locate(item: dict[str, Any], asset: dict[str, Any], lat: float, lon: float) -> tuple[int, int] | None:
    """(row, col) of the point in this asset's raster, or None when the raster does not
    contain it — the MGRS-overlap trap."""
    tr, shape = asset.get("proj:transform"), asset.get("proj:shape")
    if not (isinstance(tr, list) and len(tr) >= 6 and isinstance(shape, list) and len(shape) == 2):
        return None
    x, y = _transformer(_epsg(item)).transform(lon, lat)
    col, row = int((x - tr[2]) // tr[0]), int((y - tr[5]) // tr[4])
    if 0 <= row < int(shape[0]) and 0 <= col < int(shape[1]):
        return row, col
    return None


def reflectance(dn: float, asset: dict[str, Any], item: dict[str, Any]) -> float:
    """Surface reflectance from a digital number — see the module notes on the offset."""
    band = ((asset.get("raster:bands") or [{}])[0]) or {}
    nodata = band.get("nodata", 0)
    if dn == nodata:
        raise DeviceOffline("no data at this pixel")
    scale = float(band.get("scale", 0.0001) or 0.0001)
    applied = (item.get("properties") or {}).get("earthsearch:boa_offset_applied")
    offset = 0.0 if applied is True else float(band.get("offset", 0.0) or 0.0)
    return dn * scale + offset


def _ratio(a: float, b: float) -> float | None:
    s = a + b
    return None if abs(s) < 1e-6 else (a - b) / s


class Sentinel2Point(LiveDevice):
    """Point-queried Sentinel-2 L2A relay: stage an observation, then sign it."""

    model = "GAIA-EO (Sentinel-2 L2A, Earth Search)"
    policy_id = "copernicus_sentinel2"
    timeout = 30.0
    fields = {
        "ndvi": "index", "ndwi": "index", "nbr": "index", "ndmi": "index",
        "reflectance_green": "reflectance", "reflectance_red": "reflectance",
        "reflectance_nir": "reflectance", "reflectance_swir16": "reflectance",
        "reflectance_swir22": "reflectance", "scene_class": "SCL",
        "scene_cloud_pct": "percent", "observation_unix": "s", "observation_age_days": "d",
        "latitude": "deg", "longitude": "deg",
    }
    source = (
        "Copernicus Sentinel-2 Level-2A surface reflectance via Element84 Earth Search "
        "(STAC) and the AWS Open Data sentinel-cogs bucket. Contains modified Copernicus "
        "Sentinel data. One 10-20 m pixel at the requested point, the latest view the "
        "scene classification calls clear; not a field average."
    )
    requires_values = True

    def __init__(self, device_id: str, clock: SimClock, *, reader: CogReader | None = None,
                 **kw: Any):
        super().__init__(device_id, clock, **kw)
        self.url = STAC_SEARCH
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(STAC_SEARCH)
        self._policy = policy
        self.reader = reader or CogReader()
        self.query_gate = threading.Lock()
        self._staged: dict[str, float] | None = None
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="s2")

    # Network: no shared state, safe to run concurrently for different callers ────

    def search(self, lat: float, lon: float, max_age_days: float) -> list[dict[str, Any]]:
        now = datetime.fromtimestamp(self.clock.now(), timezone.utc)
        start = now - timedelta(days=max_age_days)
        body = {
            "collections": [COLLECTION],
            "intersects": {"type": "Point", "coordinates": [lon, lat]},
            "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{now:%Y-%m-%dT%H:%M:%SZ}",
            "query": {"eo:cloud_cover": {"lt": 90}},
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
            "limit": 20,
        }
        payload = http_post_json(STAC_SEARCH, body)
        items = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise DeviceOffline(f"{self.device_id}: STAC search answered without features")
        return [i for i in items if isinstance(i, dict)]

    def _pixel(self, item: dict[str, Any], key: str, lat: float, lon: float) -> float:
        asset = (item.get("assets") or {}).get(key)
        if not isinstance(asset, dict) or not asset.get("href"):
            raise DeviceOffline(f"scene lacks the {key} band")
        href = str(asset["href"])
        self._policy.require_endpoint(href)
        where = locate(item, asset, lat, lon)
        if where is None:
            raise ValueError("point outside this raster")
        return self.reader.pixel(href, *where)

    def observe(self, lat: float, lon: float, max_age_days: float = 30.0) -> dict[str, Any]:
        """The newest clear view of (lat, lon): values plus the scene it came from."""
        items = self.search(lat, lon, max_age_days)
        if not items:
            raise DeviceOffline(f"no Sentinel-2 scene of this point in the last "
                                f"{max_age_days:g} days")
        tried: list[str] = []
        seen_times: set[str] = set()
        for item in items:
            if len(tried) >= MAX_ITEMS_TRIED:
                break
            props = item.get("properties") or {}
            when = str(props.get("datetime", ""))
            assets = item.get("assets") or {}
            if when[:19] in seen_times or "scl" not in assets:
                continue
            if locate(item, assets["scl"], lat, lon) is None:
                continue  # the other MGRS tile of this pass holds the point
            seen_times.add(when[:19])
            scl = int(self._pixel(item, "scl", lat, lon))
            tried.append(f"{item.get('id')}: SCL {scl}")
            if scl not in SCL_CLEAR:
                continue
            dns = dict(zip(BANDS, self._pool.map(
                lambda k: self._pixel(item, k, lat, lon), BANDS)))
            refl = {k: reflectance(v, assets[k], item) for k, v in dns.items()}
            ts = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
            values = {
                "reflectance_green": refl["green"], "reflectance_red": refl["red"],
                "reflectance_nir": refl["nir"], "reflectance_swir16": refl["swir16"],
                "reflectance_swir22": refl["swir22"],
                "scene_class": float(scl),
                "scene_cloud_pct": float(props.get("eo:cloud_cover") or 0.0),
                "observation_unix": float(int(ts)),
                "observation_age_days": max(0.0, (self.clock.now() - ts) / 86400.0),
                "latitude": float(lat), "longitude": float(lon),
            }
            for name, (a, b) in {"ndvi": ("nir", "red"), "ndwi": ("green", "nir"),
                                 "nbr": ("nir", "swir22"), "ndmi": ("nir", "swir16")}.items():
                r = _ratio(refl[a], refl[b])
                if r is not None:
                    values[name] = max(-1.0, min(1.0, r))
            return {"values": values, "scene": {
                "id": item.get("id"), "datetime": when,
                "mgrs_tile": props.get("grid:code") or props.get("mgrs:grid_square"),
                "processing_baseline": props.get("s2:processing_baseline"),
                "scene_class": SCL_CLEAR[scl], "tried": tried,
                "credit": f"Contains modified Copernicus Sentinel data {when[:4]}",
            }}
        raise DeviceOffline(
            f"no clear Sentinel-2 view of this point in the last {max_age_days:g} days "
            f"(clouds or shadow at the pixel: {'; '.join(tried) or 'no covering scene'})")

    # Signing: staged values become one attested reading under the query gate ──────

    def stage(self, values: dict[str, float]) -> None:
        self._staged = dict(values)

    def unstage(self) -> None:
        self._staged = None

    def sample(self) -> dict[str, float]:
        if self._staged is None:
            raise DeviceOffline(f"{self.device_id}: Sentinel-2 reads need a coordinate")
        return dict(self._staged)


def register_imagery(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    import os

    if os.environ.get("GAIA_S2_ENABLED", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return 0
    try:
        fleet.add(Sentinel2Point("s2-l2a-01", clock, site="sentinel2", key_dir=key_dir))
    except ValueError as exc:
        log.warning("Sentinel-2 relay skipped: %s", exc)
        return 0
    return 1


__all__ = ["BANDS", "SCL_BLOCKED", "SCL_CLEAR", "STAC_SEARCH", "Sentinel2Point", "locate",
           "reflectance", "register_imagery"]
