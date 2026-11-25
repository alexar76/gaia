"""Avalanche danger and mountain snow, plus Denmark's grid, as fixed points.

* NVE Varsom (Norway, NLOD): regional avalanche danger level for today.
* WSL Institute for Snow and Avalanche Research SLF (Switzerland, CC BY 4.0): regional
  avalanche danger from the current bulletin, and measured snow depth at IMIS stations.
* Energinet Energi Data Service (Denmark, CC BY 4.0): CO₂ intensity per price area.

A danger level is a published forecast for a region, never a measurement at the pin.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_travel import _iso, shared_snapshot
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

# ── Norway: Varsom regional danger ────────────────────────────────────────────

# region id → a point inside the region (checked against the API on 2026-11-22), region name.
VARSOM_REGIONS = {
    "3003": (78.22, 15.65, "Nordenskiöld Land"),
    "3010": (69.58, 20.22, "Lyngen"),
    "3011": (69.65, 18.96, "Tromsø"),
    "3014": (68.23, 14.57, "Lofoten og Vesterålen"),
    "3015": (68.44, 17.43, "Ofoten"),
    "3022": (62.60, 9.70, "Trollheimen"),
    "3023": (62.57, 7.69, "Romsdal"),
    "3024": (62.10, 6.60, "Sunnmøre"),
    "3028": (61.55, 8.30, "Jotunheimen"),
    "3029": (61.23, 7.10, "Indre Sogn"),
    "3031": (60.63, 6.42, "Voss"),
    "3032": (60.86, 8.55, "Hallingdal"),
    "3034": (60.07, 6.55, "Hardanger"),
}
_VARSOM_API = "https://api01.nve.no/hydrology/forecast/avalanche/v6.3.0/api/AvalancheWarningByRegion/Simple"
_OSLO = ZoneInfo("Europe/Oslo")


def _oslo(raw: Any) -> datetime | None:
    """Varsom writes Norwegian wall time without an offset."""
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?", raw):
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=_OSLO)
    except ValueError:
        return None


class VarsomAvalanche(LiveDevice):
    """Today's avalanche danger level for one Norwegian forecast region."""

    model = "GAIA-AVALANCHE (NVE Varsom)"
    policy_id = "nve_varsom"
    fields = {
        "avalanche_danger_level": "level", "avalanche_danger_level_tomorrow": "level",
        "source_published_unix": "s", "source_expires_unix": "s",
        "latitude": "deg", "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, region_id: str, **kw: Any) -> None:
        if region_id not in VARSOM_REGIONS:
            raise ValueError("unsupported Varsom region")
        self.region_id = region_id
        self.latitude, self.longitude, self.place = VARSOM_REGIONS[region_id]
        self.url = f"{_VARSOM_API}/{region_id}/2/"
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.source = (
            f"{self.url} (NVE Varsom avalanche warning service, Norway; NLOD. Regional danger level on the "
            f"European scale 1 to 5 for {self.place}; 0 means no rating is published, as outside the "
            "forecast season. A regional forecast, not a slope assessment.)"
        )
        super().__init__(device_id, clock, **kw)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, list):
            raise DeviceOffline(f"{self.device_id}: invalid Varsom response")
        now = datetime.now(UTC)
        today: dict[str, Any] | None = None
        tomorrow: float | None = None
        for row in payload:
            if not isinstance(row, dict) or str(row.get("RegionId")) != self.region_id:
                continue
            start, end = _oslo(row.get("ValidFrom")), _oslo(row.get("ValidTo"))
            level = _num(row.get("DangerLevel"))
            if start is None or end is None or level is None or not 0 <= level <= 5 or level != int(level):
                continue
            if start <= now <= end:
                today = {"level": level, "end": end, "published": _oslo(row.get("PublishTime"))}
            elif timedelta(0) < start - now <= timedelta(hours=24):
                tomorrow = level
        if today is None or today["published"] is None:
            raise DeviceOffline(f"{self.device_id}: no Varsom warning covers the current day")
        return {
            "avalanche_danger_level": today["level"],
            "avalanche_danger_level_tomorrow": tomorrow,
            "source_published_unix": today["published"].timestamp(),
            "source_expires_unix": today["end"].timestamp(),
            "latitude": self.latitude, "longitude": self.longitude,
        }


# ── Switzerland: SLF bulletin and IMIS snow stations ──────────────────────────

# warning-region id → centre of the region's bounding box in the SLF bulletin map, region name.
SLF_REGIONS = {
    "CH-1222": (46.455, 7.291, "Gstaad"),
    "CH-1242": (46.635, 8.000, "Grindelwald"),
    "CH-2122": (46.834, 8.432, "Engelberg"),
    "CH-2223": (46.642, 8.553, "Northern Urseren"),
    "CH-4115": (46.132, 7.225, "Martigny - Verbier"),
    "CH-4222": (46.058, 7.732, "Zermatt"),
    "CH-4223": (46.101, 7.938, "Saas Fee"),
    "CH-5123": (46.762, 9.854, "Davos"),
    "CH-5124": (46.822, 9.159, "Flims"),
    "CH-6111": (46.487, 8.533, "Val Bedretto"),
    "CH-7114": (46.520, 9.824, "St. Moritz"),
    "CH-7121": (46.917, 10.361, "Samnaun"),
}
_SLF_BULLETIN = "https://aws.slf.ch/api/bulletin/caaml/en/json"
_SLF_LEVELS = {"low": 1.0, "moderate": 2.0, "considerable": 3.0, "high": 4.0, "very_high": 5.0}


class SlfAvalanche(LiveDevice):
    """Danger level for one Swiss warning region while the bulletin is in force."""

    model = "GAIA-AVALANCHE (WSL SLF bulletin)"
    policy_id = "slf_avalanche"
    url = _SLF_BULLETIN
    timeout = 20.0
    fields = {
        "avalanche_danger_level": "level",
        "source_published_unix": "s", "source_expires_unix": "s",
        "latitude": "deg", "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, region_id: str, **kw: Any) -> None:
        if region_id not in SLF_REGIONS:
            raise ValueError("unsupported SLF warning region")
        self.region_id = region_id
        self.latitude, self.longitude, self.place = SLF_REGIONS[region_id]
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.source = (
            f"{_SLF_BULLETIN} (WSL Institute for Snow and Avalanche Research SLF, avalanche bulletin; "
            f"CC BY 4.0. Highest danger level of the day on the European scale 1 to 5 for warning region "
            f"{region_id} {self.place}; 0 means the bulletin gives the region no rating. A regional "
            "forecast, not a slope assessment.)"
        )
        super().__init__(device_id, clock, **kw)

    def sample(self) -> dict[str, float]:
        return self.map(shared_snapshot(self.url, lambda: self._fetch(self.url)))

    def map(self, payload: Any) -> dict[str, float]:
        bulletins = payload.get("bulletins") if isinstance(payload, dict) else None
        if not isinstance(bulletins, list):
            raise DeviceOffline(f"{self.device_id}: invalid SLF bulletin")
        now = datetime.now(UTC)
        for bulletin in bulletins:
            if not isinstance(bulletin, dict):
                continue
            regions = bulletin.get("regions")
            if not isinstance(regions, list) or not any(
                isinstance(region, dict) and region.get("regionID") == self.region_id for region in regions
            ):
                continue
            valid = bulletin.get("validTime")
            start, end = (_iso(valid.get(key)) for key in ("startTime", "endTime")) if isinstance(valid, dict) else (None, None)
            published = _iso(bulletin.get("publicationTime"))
            if start is None or end is None or published is None or not start <= now < end:
                continue
            ratings = bulletin.get("dangerRatings")
            levels = [
                _SLF_LEVELS[rating["mainValue"]] for rating in ratings or ()
                if isinstance(rating, dict) and rating.get("mainValue") in _SLF_LEVELS
            ] if isinstance(ratings, list) else []
            return {
                "avalanche_danger_level": max(levels, default=0.0),
                "source_published_unix": published.timestamp(),
                "source_expires_unix": end.timestamp(),
                "latitude": self.latitude, "longitude": self.longitude,
            }
        raise DeviceOffline(f"{self.device_id}: no SLF bulletin in force for the region")


# station code → coordinates and name from the SLF IMIS station list, read 2026-11-22.
SLF_IMIS_STATIONS = {
    "ARO2": (46.0288, 7.4459, "Les Fontanesses", 2847),
    "BER2": (46.4206, 10.0292, "Motta Bianca", 2447),
    "DAV2": (46.6989, 9.8194, "Bärentälli", 2558),
    "ENG2": (46.9928, 9.1894, "Fitteren", 2162),
    "GOR2": (45.9872, 7.7836, "Gornergratsee", 2953),
    "KLO2": (46.9091, 9.8739, "Madrisa", 2147),
    "LAU2": (46.4260, 7.3799, "Trüttlisbergpass", 1970),
    "SAA2": (46.1718, 7.8794, "Seetal", 2480),
    "SIM2": (46.4675, 8.9808, "Piano del Simano", 2450),
    "TIT2": (46.7860, 8.4095, "Titlisboden", 2149),
    "WFJ2": (46.8296, 9.8093, "Weissfluhjoch", 2536),
    "ZER2": (46.0422, 7.7274, "Triftchumme", 2752),
}
_SLF_IMIS_API = "https://measurement-api.slf.ch/public/api/imis/station"
_SLF_IMIS_MAX_AGE = timedelta(hours=3)


class SlfImisSnow(LiveDevice):
    """Measured snow depth and air temperature at one automatic IMIS station."""

    model = "GAIA-SNOW (WSL SLF IMIS)"
    policy_id = "slf_imis"
    fields = {
        "snow_depth_cm": "cm", "temperature_c": "cel", "observation_unix": "s",
        "latitude": "deg", "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, station_code: str, **kw: Any) -> None:
        if station_code not in SLF_IMIS_STATIONS:
            raise ValueError("unsupported SLF IMIS station")
        self.station_code = station_code
        self.latitude, self.longitude, self.place, elevation = SLF_IMIS_STATIONS[station_code]
        self.url = f"{_SLF_IMIS_API}/{station_code}/measurements"
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.source = (
            f"{self.url} (WSL Institute for Snow and Avalanche Research SLF, IMIS automatic station "
            f"{station_code} {self.place}, {elevation} m; CC BY 4.0. Unchecked half-hourly measurements: "
            "snow depth HS and 30-minute mean air temperature.)"
        )
        super().__init__(device_id, clock, **kw)

    def map(self, payload: Any) -> dict[str, float | None]:
        if not isinstance(payload, list):
            raise DeviceOffline(f"{self.device_id}: invalid IMIS response")
        now = datetime.now(UTC)
        latest: tuple[datetime, float, float | None] | None = None
        for row in payload:
            if not isinstance(row, dict) or row.get("station_code") != self.station_code:
                continue
            measured, depth = _iso(str(row.get("measure_date") or "").replace("Z", "+00:00")), _num(row.get("HS"))
            if measured is None or depth is None or not -5.0 <= depth <= 1500.0:
                continue
            if not timedelta(0) <= now - measured <= _SLF_IMIS_MAX_AGE:
                continue
            if latest is None or measured > latest[0]:
                # The ultrasonic sensor reads a few centimetres below zero over bare ground.
                latest = (measured, max(depth, 0.0), _num(row.get("TA_30MIN_MEAN")))
        if latest is None:
            raise DeviceOffline(f"{self.device_id}: no IMIS snow depth from the last 3 hours")
        measured, depth, air = latest
        return {
            "snow_depth_cm": depth,
            "temperature_c": air if air is not None and -60.0 <= air <= 40.0 else None,
            "observation_unix": measured.timestamp(),
            "latitude": self.latitude, "longitude": self.longitude,
        }


# ── Denmark: Energinet CO₂ intensity ──────────────────────────────────────────

ENERGINET_AREAS = {
    "dk-grid-dk1-01": ("DK1", 56.15, 9.50, "West Denmark (DK1)"),
    "dk-grid-dk2-01": ("DK2", 55.68, 12.57, "East Denmark (DK2)"),
}
_ENERGINET_API = "https://api.energidataservice.dk/dataset/CO2Emis"
_ENERGINET_MAX_AGE = timedelta(minutes=45)


class EnerginetGrid(LiveDevice):
    """Five-minute CO₂ intensity of electricity for one Danish price area."""

    model = "GAIA-GRID (Energinet Energi Data Service)"
    policy_id = "energinet_co2"
    fields = {
        "carbon_intensity_gco2_kwh": "gCO2/kWh", "observation_unix": "s",
        "latitude": "deg", "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, **kw: Any) -> None:
        if device_id not in ENERGINET_AREAS:
            raise ValueError("unsupported Energinet price area")
        self.price_area, self.latitude, self.longitude, self.place = ENERGINET_AREAS[device_id]
        # One request serves both areas: the service limits anonymous request rates.
        self.url = f"{_ENERGINET_API}?" + urlencode({"limit": len(ENERGINET_AREAS), "sort": "Minutes5UTC desc"})
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.source = (
            f"{_ENERGINET_API} (Energinet, Energi Data Service, dataset CO2Emis; CC BY 4.0. Near real-time "
            f"CO₂ emission of electricity in price area {self.price_area}, in g/kWh, at five-minute "
            "resolution. Energinet's own calculation; not comparable one-to-one with other operators' methods.)"
        )
        super().__init__(device_id, clock, **kw)

    def sample(self) -> dict[str, float]:
        return self.map(shared_snapshot(self.url, lambda: self._fetch(self.url)))

    def map(self, payload: Any) -> dict[str, float]:
        rows = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise DeviceOffline(f"{self.device_id}: invalid Energinet response")
        mine = [row for row in rows if isinstance(row, dict) and row.get("PriceArea") == self.price_area]
        if len(mine) != 1:
            raise DeviceOffline(f"{self.device_id}: price area missing from the latest Energinet interval")
        row = mine[0]
        raw = row.get("Minutes5UTC")
        measured = _iso(f"{raw}+00:00") if isinstance(raw, str) else None
        value = _num(row.get("CO2Emission"))
        if measured is None or value is None or not 0 <= value <= 1500:
            raise DeviceOffline(f"{self.device_id}: invalid Energinet CO₂ record")
        if not timedelta(minutes=-10) <= datetime.now(UTC) - measured <= _ENERGINET_MAX_AGE:
            raise DeviceOffline(f"{self.device_id}: Energinet CO₂ record is not current")
        return {
            "carbon_intensity_gco2_kwh": value, "observation_unix": measured.timestamp(),
            "latitude": self.latitude, "longitude": self.longitude,
        }
