"""One pixel from a Cloud-Optimized GeoTIFF, over HTTP range requests.

A Sentinel-2 band is a 100-250 MB file; a point read needs one compressed tile of it
(about 1-2 MB) and the header that says where that tile is. Two requests per band:
the first 256 KB (the IFD and tile index of every COG we read fit in it; anything
beyond is fetched by range on demand), then exactly the tile's bytes. tifffile
decodes the tile (deflate + horizontal predictor) without needing the whole file.

Every request goes through the GAIA host allowlist, follows no redirects, retries a
dropped connection twice (S3 does drop them under parallel load), and refuses a tile
larger than MAX_TILE_BYTES — a hostile or broken file cannot make GAIA download
gigabytes for one pixel.
"""

from __future__ import annotations

import io
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import httpx

from gaia.devices.base import DeviceOffline
from gaia.devices._policy import _assert_url_allowed

HEADER_BYTES = 1 << 18
MAX_TILE_BYTES = 8 << 20
RETRIES = 3
TIMEOUT_S = 30.0
_UA = "GAIA-oracle/0.1 (+https://iot.modelmarket.dev; contact@modelmarket.dev)"


def http_get(url: str, *, headers: dict[str, str] | None = None,
             timeout: float = TIMEOUT_S) -> httpx.Response:
    url = _assert_url_allowed(url)
    last: Exception | None = None
    for _ in range(RETRIES):
        try:
            resp = httpx.get(url, headers={"User-Agent": _UA, **(headers or {})},
                             timeout=timeout, follow_redirects=False)
        except httpx.TransportError as exc:
            last = exc
            continue
        if resp.status_code in (200, 206):
            return resp
        if resp.status_code in (500, 502, 503, 504):
            last = DeviceOffline(f"upstream HTTP {resp.status_code}")
            continue
        raise DeviceOffline(f"upstream HTTP {resp.status_code} for {url.split('?')[0][-80:]}")
    raise DeviceOffline(f"upstream unreachable ({type(last).__name__})")


def http_post_json(url: str, body: dict[str, Any], *, timeout: float = TIMEOUT_S) -> Any:
    url = _assert_url_allowed(url)
    last: Exception | None = None
    for _ in range(RETRIES):
        try:
            resp = httpx.post(url, json=body, timeout=timeout, follow_redirects=False,
                              headers={"User-Agent": _UA})
        except httpx.TransportError as exc:
            last = exc
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise DeviceOffline("upstream answered with invalid JSON") from exc
        if resp.status_code in (429, 500, 502, 503, 504):
            last = DeviceOffline(f"upstream HTTP {resp.status_code}")
            continue
        raise DeviceOffline(f"upstream HTTP {resp.status_code}")
    raise DeviceOffline(f"upstream unreachable ({type(last).__name__})")


class _HeadFile(io.RawIOBase):
    """A file-like view of a remote COG: the first HEADER_BYTES from one request, the
    rest by range when tifffile asks for it."""

    def __init__(self, url: str):
        self.url = url
        resp = http_get(url, headers={"Range": f"bytes=0-{HEADER_BYTES - 1}"})
        total = resp.headers.get("content-range", "").rsplit("/", 1)[-1]
        self.size = int(total) if total.isdigit() else len(resp.content)
        self.head = resp.content
        self.pos = 0

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else (self.pos + offset if whence == 1 else self.size + offset)
        return self.pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        if self.pos + n <= len(self.head):
            data = self.head[self.pos:self.pos + n]
        else:
            if n > MAX_TILE_BYTES:
                raise DeviceOffline("COG header asks for an implausibly large read")
            data = http_get(self.url, headers={"Range": f"bytes={self.pos}-{self.pos + n - 1}"}).content
        self.pos += len(data)
        return data

    def readinto(self, buf) -> int:
        data = self.read(len(buf))
        buf[:len(data)] = data
        return len(data)


@dataclass(frozen=True)
class _Layout:
    width: int
    height: int
    tile_w: int
    tile_h: int
    offsets: tuple[int, ...]
    counts: tuple[int, ...]


class CogReader:
    """Pixel reads with small LRU caches: layouts per file, decoded tiles per (file, tile)."""

    def __init__(self, layouts: int = 64, tiles: int = 12):
        self._layouts: OrderedDict[str, tuple[_Layout, Any]] = OrderedDict()
        self._tiles: OrderedDict[tuple[str, int], Any] = OrderedDict()
        self._max_layouts, self._max_tiles = layouts, tiles
        self._lock = threading.Lock()

    def _layout(self, url: str):
        with self._lock:
            hit = self._layouts.get(url)
            if hit:
                self._layouts.move_to_end(url)
                return hit
        import tifffile

        try:
            head = _HeadFile(url)
            tif = tifffile.TiffFile(head)
            page = tif.pages[0]
            if not page.is_tiled:
                raise DeviceOffline("not a tiled (cloud-optimized) GeoTIFF")
            layout = _Layout(int(page.imagewidth), int(page.imagelength), int(page.tilewidth),
                             int(page.tilelength), tuple(int(o) for o in page.dataoffsets),
                             tuple(int(c) for c in page.databytecounts))
            # Decoding a tile needs the page's codec settings, not the file: let the
            # 256 KB header go, or 64 cached layouts would pin 16 MB for nothing.
            head.head = b""
        except DeviceOffline:
            raise
        except Exception as exc:  # noqa: BLE001 — a malformed file is an offline source
            raise DeviceOffline(f"unreadable COG ({type(exc).__name__})") from exc
        with self._lock:
            self._layouts[url] = (layout, page)
            while len(self._layouts) > self._max_layouts:
                self._layouts.popitem(last=False)
        return layout, page

    def pixel(self, url: str, row: int, col: int) -> float:
        """The value at (row, col) of the first image of the COG at ``url``."""
        import numpy as np

        layout, page = self._layout(url)
        if not (0 <= row < layout.height and 0 <= col < layout.width):
            raise ValueError("pixel outside the raster")
        per_row = -(-layout.width // layout.tile_w)
        idx = (row // layout.tile_h) * per_row + (col // layout.tile_w)
        key = (url, idx)
        with self._lock:
            tile = self._tiles.get(key)
            if tile is not None:
                self._tiles.move_to_end(key)
        if tile is None:
            offset, count = layout.offsets[idx], layout.counts[idx]
            if count <= 0:
                raise DeviceOffline("the COG holds no data for this tile")
            if count > MAX_TILE_BYTES:
                raise DeviceOffline("COG tile larger than GAIA reads for one pixel")
            raw = http_get(url, headers={"Range": f"bytes={offset}-{offset + count - 1}"}).content
            try:
                tile = np.squeeze(page.decode(raw, idx)[0])
            except Exception as exc:  # noqa: BLE001
                raise DeviceOffline(f"undecodable COG tile ({type(exc).__name__})") from exc
            with self._lock:
                self._tiles[key] = tile
                while len(self._tiles) > self._max_tiles:
                    self._tiles.popitem(last=False)
        return float(tile[row % layout.tile_h, col % layout.tile_w])


__all__ = ["CogReader", "HEADER_BYTES", "MAX_TILE_BYTES", "http_get", "http_post_json"]
