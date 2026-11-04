"""NOAA SWPC D-RAP modeled global HF absorption frequency thresholds."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from gaia.devices._live_base import LiveDevice
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.source_policy import require_approved_source

_URL = "https://services.swpc.noaa.gov/text/drap_global_frequencies.txt"
_MAX_AGE = timedelta(minutes=15)
_LATITUDES = tuple(range(89, -90, -2))
_LONGITUDES = tuple(range(-178, 180, 4))
_VALID = re.compile(r"^# Product Valid At\s*:\s*(\d{4}-\d\d-\d\d \d\d:\d\d) UTC\s*$", re.M)


def parse_drap(document: str, now: datetime) -> list[dict[str, Any]]:
    """Decode the complete 90×90 source grid without resampling."""
    times = _VALID.findall(document)
    if len(times) != 1 or not document.startswith("# DRAP Tabular Values"):
        raise ValueError("unexpected D-RAP bulletin")
    valid = datetime.strptime(times[0], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    if not timedelta(0) <= now - valid < _MAX_AGE:
        raise ValueError("D-RAP bulletin is not current")
    lines = [line.strip() for line in document.splitlines() if line.strip() and not line.startswith("#")]
    if (len(lines) != 92 or tuple(map(int, lines[0].split())) != _LONGITUDES
            or set(lines[1]) != {"-"}):
        raise ValueError("unexpected D-RAP grid")
    points = []
    for latitude, line in zip(_LATITUDES, lines[2:], strict=True):
        label, values = line.split("|")
        row = [float(value) for value in values.split()]
        if int(label) != latitude or len(row) != 90 or any(
            not math.isfinite(x) or not 0 <= x <= 100 for x in row
        ):
            raise ValueError("invalid D-RAP grid row")
        points.extend({
            "latitude": latitude, "longitude": longitude,
            "drap_frequency_mhz": frequency, "forecast_valid_unix": valid.timestamp(),
        } for longitude, frequency in zip(_LONGITUDES, row, strict=True))
    # The signed parent headline is the maximum across the complete grid.
    return sorted(points, key=lambda row: -row["drap_frequency_mhz"])


class SwpcDRap(LiveDevice):
    model = "GAIA-HF-ABSORPTION (NOAA SWPC D-RAP)"
    retain_hotspots_in_history = False
    policy_id = "swpc_drap"
    url = _URL
    attribution = "NOAA Space Weather Prediction Center · U.S. Government public domain"
    source = f"{_URL} (NOAA SWPC D-RAP; modeled 1 dB threshold, native 2° × 4° grid)"
    fields = {
        "drap_frequency_mhz": "MHz", "forecast_valid_unix": "s",
        "latitude": "deg", "longitude": "deg",
    }

    def read(self) -> dict[str, Any]:
        require_approved_source(self.policy_id).require_endpoint(self.url)
        raw = self._fetch_text(self.url, max_chars=64_000)
        try:
            points = parse_drap(raw, datetime.now(UTC))
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: {exc}") from exc
        return signed_cluster_read(self, points, numeric_keys=tuple(self.fields))
