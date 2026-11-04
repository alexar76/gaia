"""Observed sea levels from the Norwegian Hydrographic Service."""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_water_ext import _utc_time
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

KARTVERKET_TIDE_GAUGES = {
    "BGO": (60.398046, 5.320487, "Bergen"),
    "OSL": (59.908559, 10.734510, "Oslo"),
    "SVG": (58.974339, 5.730121, "Stavanger"),
    "TOS": (69.646110, 18.954790, "Tromsø"),
    "TRD": (63.436484, 10.391669, "Trondheim"),
}
_MAX_AGE = timedelta(hours=2)
_CACHE_TTL_S = 120.0
_LOCK = threading.Lock()
_SNAPSHOT: tuple[float, ET.Element] | None = None


class KartverketTideGauge(LiveDevice):
    """One permanent gauge's measured water level relative to NN2000."""

    model = "GAIA-TIDE (Kartverket Norway)"
    policy_id = "kartverket_tide"
    url = "https://vannstand.kartverket.no/tideapi.php"
    attribution = "Kartverket (Norwegian Hydrographic Service) · CC BY 4.0"
    fields = {
        "water_level_nn2000_m": "m above NN2000",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, station_code: str, **kw: Any):
        if station_code not in KARTVERKET_TIDE_GAUGES:
            raise ValueError("unsupported Kartverket pilot station")
        require_approved_source(self.policy_id).require_endpoint(self.url)
        super().__init__(device_id, clock, **kw)
        self.station_code = station_code
        self.latitude, self.longitude, _ = KARTVERKET_TIDE_GAUGES[station_code]
        self.source = (
            f"{self.url}?tide_request=stationdata&stationcode={station_code}&datatype=obs&refcode=nn2000 "
            "(Kartverket, Norwegian Hydrographic Service, CC BY 4.0. Measured water levels, "
            "converted from centimetres to metres relative to NN2000. https://vannstand.kartverket.no/tideapi_en.html)"
        )

    def _snapshot(self) -> ET.Element:
        global _SNAPSHOT
        with _LOCK:
            if _SNAPSHOT is not None and time.monotonic() - _SNAPSHOT[0] < _CACHE_TTL_S:
                return _SNAPSHOT[1]
            end = datetime.now(UTC)
            query = urlencode({
                "tide_request": "stationdata", "stationcode": ",".join(KARTVERKET_TIDE_GAUGES),
                "datatype": "obs", "refcode": "nn2000", "interval": "10", "tzone": "0",
                "fromtime": (end - _MAX_AGE).strftime("%Y-%m-%dT%H:%M"),
                "totime": end.strftime("%Y-%m-%dT%H:%M"), "lang": "en",
            })
            raw = self._fetch_bytes(f"{self.url}?{query}", max_bytes=65_000)
            try:
                text = raw.decode("utf-8-sig")
                if "<!DOCTYPE" in text:
                    raise ValueError("unexpected XML doctype")
                root = ET.fromstring(text)
            except (UnicodeError, ValueError, ET.ParseError) as exc:
                raise DeviceOffline(f"{self.device_id}: invalid Kartverket XML") from exc
            if root.tag != "tide" or root.find("stationdata") is None:
                raise DeviceOffline(f"{self.device_id}: unexpected Kartverket product")
            _SNAPSHOT = time.monotonic(), root
            return root

    def sample(self) -> dict[str, float]:
        stations = self._snapshot().findall(f"./stationdata/location[@code='{self.station_code}']")
        if len(stations) != 1:
            raise DeviceOffline(f"{self.device_id}: Kartverket station unavailable or ambiguous")
        station = stations[0]
        latitude, longitude = _num(station.get("latitude")), _num(station.get("longitude"))
        if (latitude is None or not abs(latitude - self.latitude) < 0.002
                or longitude is None or not abs(longitude - self.longitude) < 0.002):
            raise DeviceOffline(f"{self.device_id}: Kartverket station coordinates changed")
        series = station.findall("./data[@type='observation']")
        if len(series) != 1 or series[0].get("unit") != "cm" or series[0].get("reflevelcode") != "NN2000":
            raise DeviceOffline(f"{self.device_id}: Kartverket observations missing or in a different datum")

        now = datetime.now(UTC)
        observed: dict[datetime, float] = {}
        for row in series[0].findall("./waterlevel[@flag='obs']"):
            stamp, height = _utc_time(row.get("time")), _num(row.get("value"))
            if (stamp is None or not timedelta(0) <= now - stamp < _MAX_AGE
                    or height is None or not -2_000 <= height <= 2_000):
                continue
            if observed.setdefault(stamp, height) != height:
                raise DeviceOffline(f"{self.device_id}: conflicting Kartverket observations")
        if not observed:
            raise DeviceOffline(f"{self.device_id}: no measured Kartverket water level from the last 2 hours")
        measured = max(observed)
        return {
            "water_level_nn2000_m": observed[measured] / 100,
            "observation_unix": measured.timestamp(),
            "latitude": latitude,
            "longitude": longitude,
        }
