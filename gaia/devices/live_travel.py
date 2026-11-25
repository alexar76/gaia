"""Travel conditions an agent plans around: fuel prices, airport programs, border waits.

Three public feeds, each published as fixed points:

* France, Prix des carburants (DGCCRF, Licence Ouverte 2.0): station pump prices.
* FAA National Airspace System status (U.S. Government work): ground stops and delays.
* CBP Border Wait Times (U.S. Government work): land-crossing delays.

Every device reads the publisher's whole snapshot and reports only its own point, so a
snapshot is shared between the devices of one feed for a short time.
"""

from __future__ import annotations

import re
import statistics
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from collections.abc import Callable

    from gaia.clock import SimClock

_SNAPSHOT_TTL_S = 120.0
_SNAPSHOTS: dict[str, tuple[float, Any]] = {}
_SNAPSHOT_LOCK = Lock()


def shared_snapshot(key: str, loader: Callable[[], Any]) -> Any:
    """One upstream fetch for all devices of a feed within two minutes."""
    now = time.monotonic()
    with _SNAPSHOT_LOCK:
        hit = _SNAPSHOTS.get(key)
        if hit and now - hit[0] < _SNAPSHOT_TTL_S:
            return hit[1]
    payload = loader()
    with _SNAPSHOT_LOCK:
        _SNAPSHOTS[key] = (now, payload)
    return payload


# ── France: pump prices ───────────────────────────────────────────────────────

FR_FUEL_CITIES = {
    "paris": (48.8566, 2.3522, "Paris"),
    "lyon": (45.7640, 4.8357, "Lyon"),
    "marseille": (43.2965, 5.3698, "Marseille"),
    "toulouse": (43.6047, 1.4442, "Toulouse"),
    "bordeaux": (44.8378, -0.5792, "Bordeaux"),
    "lille": (50.6292, 3.0573, "Lille"),
    "nantes": (47.2184, -1.5536, "Nantes"),
    "strasbourg": (48.5734, 7.7521, "Strasbourg"),
    "nice": (43.7102, 7.2620, "Nice"),
    "rennes": (48.1173, -1.6778, "Rennes"),
    "montpellier": (43.6108, 3.8767, "Montpellier"),
    "grenoble": (45.1885, 5.7245, "Grenoble"),
}
_FR_FUEL_API = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "prix-des-carburants-en-france-flux-instantane-v2/records"
)
_FR_FUEL_RADIUS_KM = 5
_FR_FUEL_PAGE = 100
_FR_FUEL_MAX_AGE = timedelta(days=7)
# Upstream column prefix → our field stem. Gazole is road diesel; E10 and SP98 are petrol grades.
_FR_FUELS = {"gazole": "diesel", "e10": "petrol_e10", "sp98": "petrol_sp98"}


class FrFuelPrices(LiveDevice):
    """Median and lowest pump price among stations within 5 km of a city centre."""

    model = "GAIA-FUEL (France, Prix des carburants)"
    policy_id = "fr_fuel_prices"
    timeout = 20.0
    fields = {
        "diesel_eur_l": "EUR/L", "diesel_min_eur_l": "EUR/L",
        "petrol_e10_eur_l": "EUR/L", "petrol_e10_min_eur_l": "EUR/L",
        "petrol_sp98_eur_l": "EUR/L", "petrol_sp98_min_eur_l": "EUR/L",
        "fuel_station_count": "count", "observation_unix": "s",
        "latitude": "deg", "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, city: str, **kw: Any) -> None:
        if city not in FR_FUEL_CITIES:
            raise ValueError("unsupported French fuel-price city")
        self.latitude, self.longitude, self.place = FR_FUEL_CITIES[city]
        select = ["id", *(f"{fuel}_{part}" for fuel in _FR_FUELS for part in ("prix", "maj"))]
        self.url = f"{_FR_FUEL_API}?" + urlencode({
            "select": ",".join(select),
            "where": (f"within_distance(geom, geom'POINT({self.longitude} {self.latitude})', "
                      f"{_FR_FUEL_RADIUS_KM}km)"),
            "limit": _FR_FUEL_PAGE,
        })
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.source = (
            f"{_FR_FUEL_API} (Prix des carburants en France, flux instantané; DGCCRF, Licence Ouverte 2.0. "
            f"Stations within {_FR_FUEL_RADIUS_KM} km of {self.place} centre; prices older than "
            f"{_FR_FUEL_MAX_AGE.days} days are left out. Median and lowest price per fuel, in EUR per litre.)"
        )
        super().__init__(device_id, clock, **kw)

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = payload.get("results") if isinstance(payload, dict) else None
        total = payload.get("total_count") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or type(total) is not int:
            raise DeviceOffline(f"{self.device_id}: invalid fuel-price response")
        if total > _FR_FUEL_PAGE:
            raise DeviceOffline(f"{self.device_id}: more stations than one page; refusing a partial median")
        now = datetime.now(UTC)
        prices: dict[str, list[float]] = {stem: [] for stem in _FR_FUELS.values()}
        stations: set[Any] = set()
        newest: datetime | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            for fuel, stem in _FR_FUELS.items():
                price, updated = _num(row.get(f"{fuel}_prix")), _iso(row.get(f"{fuel}_maj"))
                if price is None or updated is None or not 0.3 <= price <= 5.0:
                    continue
                if not timedelta(0) <= now - updated <= _FR_FUEL_MAX_AGE:
                    continue
                prices[stem].append(price)
                stations.add(row.get("id"))
                newest = updated if newest is None or updated > newest else newest
        if not stations or newest is None:
            raise DeviceOffline(f"{self.device_id}: no station price updated in the last 7 days")
        values: dict[str, float | None] = {
            "fuel_station_count": float(len(stations)),
            "observation_unix": newest.timestamp(),
            "latitude": self.latitude, "longitude": self.longitude,
        }
        for stem, series in prices.items():
            if series:
                values[f"{stem}_eur_l"] = round(statistics.median(series), 3)
                values[f"{stem}_min_eur_l"] = min(series)
        return values


def _iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not 19 <= len(raw) <= 35:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


# ── FAA: airport programs ─────────────────────────────────────────────────────

# Coordinates from aviationweather.gov station info (NOAA), read 2026-11-22.
FAA_AIRPORTS = {
    "ATL": (33.6297, -84.4422, "Atlanta Hartsfield-Jackson"),
    "AUS": (30.1831, -97.6806, "Austin-Bergstrom"),
    "BNA": (36.1105, -86.6881, "Nashville"),
    "BOS": (42.3606, -71.0097, "Boston Logan"),
    "BWI": (39.1733, -76.6840, "Baltimore-Washington"),
    "CLT": (35.2225, -80.9543, "Charlotte Douglas"),
    "DCA": (38.8472, -77.0345, "Washington Reagan National"),
    "DEN": (39.8466, -104.6562, "Denver"),
    "DFW": (32.8974, -97.0220, "Dallas-Fort Worth"),
    "DTW": (42.2311, -83.3312, "Detroit Metro"),
    "EWR": (40.6828, -74.1692, "Newark Liberty"),
    "FLL": (26.0787, -80.1622, "Fort Lauderdale-Hollywood"),
    "IAD": (38.9348, -77.4473, "Washington Dulles"),
    "IAH": (29.9844, -95.3607, "Houston Bush"),
    "JFK": (40.6392, -73.7639, "New York JFK"),
    "LAS": (36.0719, -115.1634, "Las Vegas Reid"),
    "LAX": (33.9382, -118.3866, "Los Angeles"),
    "LGA": (40.7794, -73.8803, "New York LaGuardia"),
    "MCO": (28.4182, -81.3241, "Orlando"),
    "MDW": (41.7841, -87.7552, "Chicago Midway"),
    "MIA": (25.7881, -80.3169, "Miami"),
    "MSP": (44.8852, -93.2313, "Minneapolis-St Paul"),
    "ORD": (41.9602, -87.9316, "Chicago O'Hare"),
    "PHL": (39.8733, -75.2268, "Philadelphia"),
    "PHX": (33.4278, -112.0037, "Phoenix Sky Harbor"),
    "SAN": (32.7364, -117.1973, "San Diego"),
    "SEA": (47.4447, -122.3144, "Seattle-Tacoma"),
    "SFO": (37.6196, -122.3656, "San Francisco"),
    "SLC": (40.7707, -111.9650, "Salt Lake City"),
    "TPA": (27.9633, -82.5400, "Tampa"),
}
_FAA_URL = "https://nasstatus.faa.gov/api/airport-status-information"
_FAA_MAX_AGE = timedelta(minutes=30)
_DURATION = re.compile(r"(?:(\d{1,2})\s*hours?)?(?:\s*and\s*)?(?:(\d{1,3})\s*minutes?)?", re.I)


def _minutes(raw: str | None) -> float | None:
    """'1 hour and 26 minutes' → 86.0; None when the text carries no duration."""
    text = (raw or "").strip()
    match = _DURATION.fullmatch(text) if text else None
    if not match or not any(match.groups()):
        return None
    return float(int(match.group(1) or 0) * 60 + int(match.group(2) or 0))


class FaaAirportStatus(LiveDevice):
    """FAA traffic-management programs in effect at one airport, as published."""

    model = "GAIA-AIRPORT-STATUS (FAA NAS Status)"
    policy_id = "faa_nas_status"
    url = _FAA_URL
    source = (
        f"{_FAA_URL} (FAA National Airspace System Status; U.S. Government work. Programs in effect at "
        "the airport: ground stop, ground delay, and arrival and departure delays. All zero means the "
        "FAA lists no program for the airport. The feed's closure notices are left out: most restrict "
        "general aviation only. Not a per-flight delay; check with the airline.)"
    )
    fields = {
        "delay_program_active": "bool", "ground_stop": "bool",
        "ground_delay_avg_min": "min", "ground_delay_max_min": "min",
        "arrival_delay_max_min": "min", "departure_delay_max_min": "min",
        "source_published_unix": "s",
        "latitude": "deg", "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, airport: str, **kw: Any) -> None:
        if airport not in FAA_AIRPORTS:
            raise ValueError("unsupported FAA airport")
        self.airport = airport
        self.latitude, self.longitude, self.place = FAA_AIRPORTS[airport]
        require_approved_source(self.policy_id).require_endpoint(self.url)
        super().__init__(device_id, clock, **kw)

    def sample(self) -> dict[str, float]:
        text = shared_snapshot(self.url, lambda: self._fetch_text(self.url, max_chars=500_000))
        return self.map(text)

    def map(self, payload: Any) -> dict[str, float]:
        try:
            root = ET.fromstring(payload)
        except (ET.ParseError, TypeError) as exc:
            raise DeviceOffline(f"{self.device_id}: invalid FAA status document") from exc
        if root.tag != "AIRPORT_STATUS_INFORMATION":
            raise DeviceOffline(f"{self.device_id}: unexpected FAA status document")
        try:
            published = datetime.strptime(
                (root.findtext("Update_Time") or "").strip(), "%a %b %d %H:%M:%S %Y GMT",
            ).replace(tzinfo=UTC)
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: invalid FAA update time") from exc
        if not timedelta(minutes=-2) <= datetime.now(UTC) - published <= _FAA_MAX_AGE:
            raise DeviceOffline(f"{self.device_id}: FAA status is not current")
        values = {
            "delay_program_active": 0.0, "ground_stop": 0.0,
            "source_published_unix": published.timestamp(),
            "latitude": self.latitude, "longitude": self.longitude,
        }

        def mine(node: ET.Element) -> bool:
            return (node.findtext("ARPT") or "").strip().upper() == self.airport

        for node in root.iterfind(".//Ground_Stop_List/Program"):
            if mine(node):
                values["ground_stop"] = values["delay_program_active"] = 1.0
        for node in root.iterfind(".//Ground_Delay_List/Ground_Delay"):
            if mine(node):
                values["delay_program_active"] = 1.0
                for field, tag in (("ground_delay_avg_min", "Avg"), ("ground_delay_max_min", "Max")):
                    minutes = _minutes(node.findtext(tag))
                    if minutes is not None and minutes <= 1440:
                        values[field] = minutes
        for node in root.iterfind(".//Arrival_Departure_Delay_List/Delay"):
            if not mine(node):
                continue
            values["delay_program_active"] = 1.0
            for part in node.iterfind("Arrival_Departure"):
                kind = (part.get("Type") or "").strip().lower()
                minutes = _minutes(part.findtext("Max"))
                if kind in ("arrival", "departure") and minutes is not None and minutes <= 1440:
                    field = f"{kind}_delay_max_min"
                    values[field] = max(values.get(field, 0.0), minutes)
        return values


# ── CBP: land-border waits ────────────────────────────────────────────────────

# port_number → approximate crossing location (0.01°), crossing, country across the border, and the
# port's time zone: the feed stamps each row in the port's local time.
CBP_CROSSINGS = {
    "250401": (32.54, -117.03, "San Ysidro", "Mexico", "America/Los_Angeles"),
    "250601": (32.55, -116.94, "Otay Mesa (passenger)", "Mexico", "America/Los_Angeles"),
    "250302": (32.66, -115.50, "Calexico West", "Mexico", "America/Los_Angeles"),
    "260401": (31.33, -110.94, "Nogales DeConcini", "Mexico", "America/Phoenix"),
    "240201": (31.76, -106.45, "El Paso Bridge of the Americas", "Mexico", "America/Denver"),
    "230402": (27.50, -99.50, "Laredo Bridge II", "Mexico", "America/Chicago"),
    "230501": (26.10, -98.27, "Hidalgo", "Mexico", "America/Chicago"),
    "535504": (25.90, -97.50, "Brownsville Gateway", "Mexico", "America/Chicago"),
    "380001": (42.31, -83.07, "Detroit Ambassador Bridge", "Canada", "America/New_York"),
    "380201": (43.00, -82.42, "Port Huron Blue Water Bridge", "Canada", "America/New_York"),
    "090101": (42.91, -78.91, "Buffalo Peace Bridge", "Canada", "America/New_York"),
    "071201": (45.01, -73.45, "Champlain", "Canada", "America/New_York"),
    "300402": (49.00, -122.76, "Blaine Peace Arch", "Canada", "America/Los_Angeles"),
    "300401": (49.00, -122.74, "Blaine Pacific Highway", "Canada", "America/Los_Angeles"),
}
_CBP_URL = "https://bwt.cbp.gov/api/bwtnew"
_CBP_MAX_AGE = timedelta(minutes=45)
_CBP_LANES = {
    "passenger": ("passenger_vehicle_lanes", "standard_lanes"),
    "commercial": ("commercial_vehicle_lanes", "standard_lanes"),
    "pedestrian": ("pedestrian_lanes", "standard_lanes"),
}


class CbpBorderWait(LiveDevice):
    """CBP-reported delay and open lanes at one U.S. land crossing (standard lanes)."""

    model = "GAIA-BORDER-WAIT (CBP Border Wait Times)"
    policy_id = "cbp_border_wait"
    url = _CBP_URL
    timeout = 20.0
    fields = {
        "port_open": "bool",
        "passenger_delay_min": "min", "passenger_lanes_open": "count",
        "commercial_delay_min": "min", "commercial_lanes_open": "count",
        "pedestrian_delay_min": "min", "pedestrian_lanes_open": "count",
        "source_published_unix": "s", "latitude": "deg", "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, port_number: str, **kw: Any) -> None:
        if port_number not in CBP_CROSSINGS:
            raise ValueError("unsupported CBP crossing")
        self.port_number = port_number
        self.latitude, self.longitude, self.place, _, zone = CBP_CROSSINGS[port_number]
        self.zone = ZoneInfo(zone)
        require_approved_source(self.policy_id).require_endpoint(self.url)
        self.source = (
            f"{_CBP_URL} (U.S. Customs and Border Protection Border Wait Times; U.S. Government work. "
            f"Port {port_number}, standard lanes, entering the United States. Delays are CBP estimates; "
            "a lane type without a published figure is left out. Map position is approximate.)"
        )
        super().__init__(device_id, clock, **kw)

    def sample(self) -> dict[str, float]:
        return self.map(shared_snapshot(self.url, lambda: self._fetch(self.url)))

    def map(self, payload: Any) -> dict[str, float]:
        if not isinstance(payload, list):
            raise DeviceOffline(f"{self.device_id}: invalid CBP response")
        rows = [row for row in payload if isinstance(row, dict) and row.get("port_number") == self.port_number]
        if len(rows) != 1:
            raise DeviceOffline(f"{self.device_id}: crossing missing from the CBP feed")
        row = rows[0]
        try:
            published = datetime.strptime(
                f"{row.get('date')} {row.get('time')}", "%m/%d/%Y %H:%M:%S",
            ).replace(tzinfo=self.zone)
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: invalid CBP feed time") from exc
        if not timedelta(minutes=-2) <= datetime.now(UTC) - published <= _CBP_MAX_AGE:
            raise DeviceOffline(f"{self.device_id}: CBP feed is not current")
        status = row.get("port_status")
        if status not in ("Open", "Closed"):
            raise DeviceOffline(f"{self.device_id}: unknown CBP port status")
        values = {
            "port_open": 1.0 if status == "Open" else 0.0,
            "source_published_unix": published.timestamp(),
            "latitude": self.latitude, "longitude": self.longitude,
        }
        for stem, (group, lane) in _CBP_LANES.items():
            node = row.get(group)
            node = node.get(lane) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                continue
            delay, lanes = _whole(node.get("delay_minutes"), 1440), _whole(node.get("lanes_open"), 100)
            if delay is not None and lanes is not None:
                values[f"{stem}_delay_min"] = delay
                values[f"{stem}_lanes_open"] = lanes
        return values


def _whole(raw: Any, maximum: int) -> float | None:
    text = raw.strip() if isinstance(raw, str) else ""
    if not text.isdigit() or int(text) > maximum:
        return None
    return float(int(text))
