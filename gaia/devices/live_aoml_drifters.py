"""Recent sea-surface observations from NOAA AOML OSMC drifting buoys."""

from __future__ import annotations

import csv
import re
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any
from urllib.parse import quote

import httpx

from gaia.devices._live_base import LiveDevice, _UA, _num
from gaia.devices._policy import _assert_url_allowed
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.live_water_ext import _utc_time
from gaia.source_policy import require_approved_source

_MAX_AGE = timedelta(hours=6)
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_ROWS = 2500
_PLATFORM_ID = re.compile(r"(?:[0-9]{5}|[0-9]{7})")
_PLATFORM_TYPES = {"DRIFTING BUOYS", "DRIFTING BUOYS (GENERIC)"}
_COLUMNS = ["platform_code", "platform_type", "time", "latitude", "longitude", "sst", "slp"]
_UNITS = ["", "", "UTC", "degrees_north", "degrees_east", "Deg C", "hPa"]


class AomlDrifters(LiveDevice):
    """One signed position and SST per recent, reporting drifter."""

    model = "GAIA-OCEAN-DRIFTERS (NOAA AOML OSMC)"
    policy_id = "noaa_aoml_drifters"
    retain_hotspots_in_history = False
    url = "https://osmc.noaa.gov/erddap/tabledap/OSMC_flattened.csv"
    attribution = "NOAA AOML / OSMC · CC0 1.0"
    source = (
        f"{url} (NOAA AOML / OSMC · CC0 1.0. Preliminary GTS drifting-buoy positions and "
        "sea-surface temperatures, latest qualifying observation per platform in six hours. "
        "Coordinates rounded, optional reported sea-level pressure. "
        "https://osmc.noaa.gov/erddap/info/OSMC_flattened/index.json)"
    )
    fields = {
        "sst_c": "°C",
        "sea_level_pressure_hpa": "hPa",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def read(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        start = (now - _MAX_AGE).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        query = (
            f'{",".join(_COLUMNS)}&platform_type=~"DRIFTING BUOYS.*"'
            f'&time>={start}&time<={end}&sst>=-2.5&sst<=40&orderByMax("platform_code,time")'
        )
        url = f"{self.url}?{quote(query, safe='=&,()')}"
        document = self._fetch_csv(url)
        points = self._observations(document, datetime.now(UTC))
        return signed_cluster_read(
            self, points, numeric_keys=tuple(self.fields), meta_keys=("platform_code", "observed_at"),
            reading_meta={"drifter_count": len(points)},
        )

    def _fetch_csv(self, url: str) -> str:
        require_approved_source(self.policy_id).require_endpoint(url)
        headers = {"User-Agent": _UA, "Accept": "text/csv", "Accept-Encoding": "identity"}
        payload = bytearray()
        try:
            with httpx.stream("GET", _assert_url_allowed(url), headers=headers, timeout=self.timeout,
                              follow_redirects=False) as response:
                if response.status_code != 200:
                    raise DeviceOffline(f"{self.device_id}: upstream HTTP {response.status_code}")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise DeviceOffline(f"{self.device_id}: unexpected OSMC content encoding")
                for chunk in response.iter_raw(chunk_size=16 * 1024):
                    if len(payload) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise DeviceOffline(f"{self.device_id}: OSMC CSV exceeds size limit")
                    payload.extend(chunk)
        except httpx.HTTPError as exc:
            raise DeviceOffline(f"{self.device_id}: OSMC upstream unreachable") from exc
        return payload.decode("iso-8859-1")

    def _observations(self, document: str, now: datetime) -> list[dict[str, Any]]:
        rows = csv.reader(StringIO(document), strict=True)
        by_platform: dict[str, dict[str, Any]] = {}
        try:
            if next(rows, None) != _COLUMNS or next(rows, None) != _UNITS:
                raise DeviceOffline(f"{self.device_id}: unexpected OSMC CSV columns or units")
            for count, row in enumerate(rows, start=1):
                if count > _MAX_ROWS or len(row) != len(_COLUMNS):
                    raise DeviceOffline(f"{self.device_id}: invalid or oversized OSMC CSV")
                point = self._observation(row, now)
                if point is None:
                    continue
                platform = point["platform_code"]
                previous = by_platform.get(platform)
                if previous is not None:
                    if point["observation_unix"] < previous["observation_unix"]:
                        continue
                    if point["observation_unix"] == previous["observation_unix"] and point != previous:
                        raise DeviceOffline(f"{self.device_id}: conflicting OSMC observation for {platform}")
                by_platform[platform] = point
        except csv.Error as exc:
            raise DeviceOffline(f"{self.device_id}: malformed OSMC CSV") from exc
        if not by_platform:
            raise DeviceOffline(f"{self.device_id}: no fresh OSMC drifter observations")
        return sorted(by_platform.values(), key=lambda point: (-point["observation_unix"], point["platform_code"]))

    @staticmethod
    def _observation(row: list[str], now: datetime) -> dict[str, Any] | None:
        platform, platform_type, when, lat_text, lon_text, temperature_text, pressure_text = row
        if not _PLATFORM_ID.fullmatch(platform) or platform_type not in _PLATFORM_TYPES:
            return None
        observed = _utc_time(when)
        latitude, longitude = _num(lat_text), _num(lon_text)
        temperature, pressure = _num(temperature_text), _num(pressure_text)
        if (observed is None or not timedelta(0) <= now - observed <= _MAX_AGE
                or latitude is None or not -90 <= latitude <= 90
                or longitude is None or not -180 <= longitude <= 180
                or temperature is None or not -2.5 <= temperature <= 40):
            return None
        point = {
            "platform_code": platform,
            "observed_at": observed.isoformat(),
            "observation_unix": observed.timestamp(),
            "latitude": latitude,
            "longitude": longitude,
            "sst_c": temperature,
        }
        if pressure is not None and 800 <= pressure <= 1100:
            point["sea_level_pressure_hpa"] = pressure
        return point
