"""USDA NRCS SNOTEL hourly snow depth and snow water equivalent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock


# AWDB station metadata reports dataTimeZone=-8 for both Colorado pilot sites.
# NRCS uses Pacific Standard Time year-round, including during daylight saving.
SNOTEL_STATIONS = {
    "335:CO:SNTL": (39.80364, -105.77786, "Berthoud Summit"),
    "415:CO:SNTL": (39.48917, -106.17154, "Copper Mountain"),
}
_AWDB_TIMEZONE = timezone(timedelta(hours=-8))
_MAX_AGE = timedelta(hours=6)
_AWDB_HOUR = "%Y-%m-%d %H:%M"
_IN_TO_CM = 2.54
_SNOW_ELEMENTS = {
    "SNWD": ("snow_depth_cm", 2000.0),
    "WTEQ": ("snow_water_equivalent_cm", 1000.0),
}


class UsdaSnotel(LiveDevice):
    """Paired, automated-QC-valid readings from fixed in-situ pilot stations."""

    model = "GAIA-SNOW (USDA NRCS SNOTEL)"
    policy_id = "usda_nrcs_snotel"
    url = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data"
    fields = {
        "snow_depth_cm": "cm",
        "snow_water_equivalent_cm": "cm",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, station_triplet: str, **kw: Any):
        if station_triplet not in SNOTEL_STATIONS:
            raise ValueError("unsupported SNOTEL pilot station")
        require_approved_source(self.policy_id).require_endpoint(self.url)
        super().__init__(device_id, clock, **kw)
        self.station_triplet = station_triplet
        self.latitude, self.longitude, _ = SNOTEL_STATIONS[station_triplet]
        report_station = quote(f'{station_triplet}|id=""|name', safe="")
        self.source = (
            "https://wcc.sc.egov.usda.gov/reportGenerator/view/customSingleStationReport/hourly/"
            f"{report_station}/-1%2C0/WTEQ%3A%3Avalue%2CSNWD%3A%3Avalue "
            "(USDA NRCS Air and Water Database, SNOTEL; U.S. public domain. "
            "Paired hourly measurements pass automated QC and may await human review.)"
        )

    def sample(self) -> dict[str, float]:
        now = datetime.now(UTC)
        local = now.astimezone(_AWDB_TIMEZONE)
        since = local - _MAX_AGE
        query = urlencode({
            "stationTriplets": self.station_triplet,
            "elements": "WTEQ,SNWD",
            "duration": "HOURLY",
            "beginDate": since.replace(minute=0).strftime(_AWDB_HOUR),
            "endDate": local.replace(minute=0).strftime(_AWDB_HOUR),
            "periodRef": "END",
            "centralTendencyType": "NONE",
            "returnFlags": "true",
            "returnOriginalValues": "false",
            "returnSuspectData": "false",
        })
        payload = self._fetch(f"{self.url}?{query}")
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise DeviceOffline(f"{self.device_id}: invalid AWDB station response")
        station = payload[0]
        if station.get("stationTriplet") != self.station_triplet or not isinstance(station.get("data"), list):
            raise DeviceOffline(f"{self.device_id}: AWDB station identity or data mismatch")

        series: dict[str, dict[datetime, float]] = {}
        for element in station["data"]:
            if not isinstance(element, dict) or not isinstance(element.get("stationElement"), dict):
                continue
            meta = element["stationElement"]
            code = meta.get("elementCode")
            if not isinstance(code, str) or code not in _SNOW_ELEMENTS or meta.get("ordinal") != 1:
                continue
            values = element.get("values")
            if (code in series or meta.get("durationName") != "HOURLY"
                    or meta.get("storedUnitCode") != "in" or meta.get("derivedData") is not False
                    or not isinstance(values, list)):
                raise DeviceOffline(f"{self.device_id}: invalid AWDB hourly {code} series")
            series[code] = self._hourly_values(values, now, _SNOW_ELEMENTS[code][1])

        paired_hours = series.get("SNWD", {}).keys() & series.get("WTEQ", {}).keys()
        if not paired_hours:
            raise DeviceOffline(f"{self.device_id}: no QC-valid paired SNOTEL readings from the last 6 hours")
        measured = max(paired_hours)
        return {
            **{field: series[code][measured] for code, (field, _) in _SNOW_ELEMENTS.items()},
            "observation_unix": measured.timestamp(),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def _hourly_values(self, values: list[Any], now: datetime, maximum_cm: float) -> dict[datetime, float]:
        hourly = {}
        for row in values:
            # V is automated Valid. R (raw), P (provisional), and A (approved)
            # describe separate stages of human review, not validity of the sensor reading.
            if (not isinstance(row, dict) or row.get("qcFlag") != "V"
                    or row.get("qaFlag") not in ("R", "P", "A")):
                continue
            try:
                measured = datetime.strptime(row["date"], _AWDB_HOUR).replace(tzinfo=_AWDB_TIMEZONE)
            except (KeyError, TypeError, ValueError):
                continue
            if measured.minute != 0 or not timedelta(0) <= now - measured <= _MAX_AGE:
                continue
            inches = _num(row.get("value"))
            if inches is None or not 0 <= inches <= maximum_cm / _IN_TO_CM:
                continue
            cm = inches * _IN_TO_CM
            if measured in hourly and hourly[measured] != cm:
                raise DeviceOffline(f"{self.device_id}: conflicting AWDB values for one measurement hour")
            hourly[measured] = cm
        return hourly
