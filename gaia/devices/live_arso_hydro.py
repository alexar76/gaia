"""Preliminary river measurements from the Slovenian Environment Agency."""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

ARSO_RIVER_GAUGES = {
    "1060": (46.68124, 15.9953, "Gornja Radgona I"),
    "3660": (46.05684, 14.82877, "Litija"),
    "5078": (46.05542, 14.54443, "Moste I"),
    "8080": (46.24748, 13.58641, "Kobarid I"),
}
_CET = timezone(timedelta(hours=1))  # ARSO's datum_cet is fixed UTC+1 all year.
_MAX_AGE = timedelta(hours=2)
_CACHE_TTL_S = 300.0
_LOCK = threading.Lock()
_SNAPSHOT: tuple[float, ET.Element] | None = None


class ArsoRiverGauge(LiveDevice):
    """Same-row stage and discharge from a fixed Slovenian river gauge."""

    model = "GAIA-RIVER (ARSO Slovenia)"
    policy_id = "arso_hydrometry"
    url = "https://www.arso.gov.si/xml/vode/hidro_podatki_zadnji.xml"
    attribution = "Agencija Republike Slovenije za okolje (ARSO)"
    source = (
        f"{url} (Agencija Republike Slovenije za okolje (ARSO); commercial reuse with attribution. "
        "Preliminary river observations, stage converted from cm to metres above local gauge zero. "
        "https://kazalci.arso.gov.si/en/content/legal-note)"
    )
    fields = {
        "gage_height_m": "m",
        "discharge_m3s": "m3/s",
        "water_temperature_c": "cel",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, station_id: str, **kw: Any):
        if station_id not in ARSO_RIVER_GAUGES:
            raise ValueError("unsupported ARSO pilot station")
        require_approved_source(self.policy_id).require_endpoint(self.url)
        super().__init__(device_id, clock, **kw)
        self.station_id = station_id
        self.latitude, self.longitude, _ = ARSO_RIVER_GAUGES[station_id]

    def _snapshot(self) -> ET.Element:
        global _SNAPSHOT
        with _LOCK:
            if _SNAPSHOT is not None and time.monotonic() - _SNAPSHOT[0] < _CACHE_TTL_S:
                return _SNAPSHOT[1]
            raw = self._fetch_bytes(self.url, max_bytes=250_000)
            try:
                text = raw.decode("utf-8-sig")
                if "<!DOCTYPE" in text:
                    raise ValueError("unexpected XML doctype")
                root = ET.fromstring(text)
            except (UnicodeError, ValueError, ET.ParseError) as exc:
                raise DeviceOffline(f"{self.device_id}: invalid ARSO XML") from exc
            if (root.tag != "arsopodatki" or root.get("verzija") != "1.5"
                    or root.findtext("vir") != "Agencija RS za okolje"):
                raise DeviceOffline(f"{self.device_id}: unexpected ARSO product")
            _SNAPSHOT = time.monotonic(), root
            return root

    def sample(self) -> dict[str, float]:
        rows = self._snapshot().findall(f"./postaja[@sifra='{self.station_id}']")
        if len(rows) != 1:
            raise DeviceOffline(f"{self.device_id}: ARSO station unavailable or ambiguous")
        station = rows[0]
        latitude = _num(station.get("wgs84_sirina"))
        longitude = _num(station.get("wgs84_dolzina"))
        if (latitude is None or not abs(latitude - self.latitude) < 0.002
                or longitude is None or not abs(longitude - self.longitude) < 0.002):
            raise DeviceOffline(f"{self.device_id}: ARSO station coordinates changed")
        try:
            measured = datetime.strptime(station.findtext("datum_cet", ""), "%Y-%m-%d %H:%M").replace(tzinfo=_CET)
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: invalid ARSO observation time") from exc
        if not timedelta(0) <= datetime.now(UTC) - measured < _MAX_AGE:
            raise DeviceOffline(f"{self.device_id}: no ARSO observation from the last 2 hours")

        stage, discharge = _num(station.findtext("vodostaj")), _num(station.findtext("pretok"))
        if stage is None or not -500 <= stage <= 20_000 or discharge is None or not 0 <= discharge <= 200_000:
            raise DeviceOffline(f"{self.device_id}: no paired ARSO stage and discharge")
        values = {
            "gage_height_m": stage / 100,
            "discharge_m3s": discharge,
            "observation_unix": measured.timestamp(),
            "latitude": latitude,
            "longitude": longitude,
        }
        temperature = _num(station.findtext("temp_vode"))
        if temperature is not None and -2 <= temperature <= 45:
            values["water_temperature_c"] = temperature
        return values
