"""P9 commercially-clear LIVE relays — FR/AT rivers + SE weather mesh.

* Hub'Eau hydrométrie — France ~6k stations (Etalab Licence Ouverte 2.0).
* eHYD / BMLUK pegel_aktuell — Austria (CC BY 4.0).
* SMHI MetObs — Sweden in-situ weather (CC BY 4.0; not hydroobs).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p9")

_SAFE_HUBEAU = re.compile(r"^[A-Z0-9]{8,12}$", re.I)
_SAFE_EHYD = re.compile(r"^[0-9]{5,7}$")
_SAFE_SMHI = re.compile(r"^[0-9]{3,8}$")

# Hub'Eau/SANDRE observations_tr publishes Q in litres/second and H in mm.
# Convert Q before exposing the GAIA m3/s field.  Keep the plausibility ceiling
# in the public unit so a perfectly ordinary 25+ m3/s river is not discarded.
_MAX_Q_M3S = 25000.0
_MAX_H_MM = 20000.0
_MAX_AGE_S = 6 * 3600


def _iso_epoch(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _latest_obs(payload: Any) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (value, epoch, lat, lon) from Hub'Eau observations_tr JSON."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return None, None, None, None
    newest: tuple[float, float, float | None, float | None] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _num(row.get("resultat_obs"))
        when = _iso_epoch(row.get("date_obs"))
        if value is None:
            continue
        candidate = (
            when if when is not None else 0.0,
            float(value),
            _num(row.get("latitude")),
            _num(row.get("longitude")),
        )
        if newest is None or candidate[0] > newest[0]:
            newest = candidate
    if newest is None:
        return None, None, None, None
    return newest[1], newest[0], newest[2], newest[3]


class HubEauRiver(LiveDevice):
    """Hub'Eau hydrométrie point gauge — France, Etalab Open Licence 2.0."""

    model = "GAIA-RIVER (Hub'Eau)"
    policy_id = "hubeau_hydro"
    fields = {
        "discharge_m3s": "m3/s",
        "gage_height_m": "m",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "Hub'Eau hydrométrie · Etalab Licence Ouverte 2.0. "
        "France in-situ stage/flow only — not Vigicrues warnings, not EA/SEPA/NRW."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip().upper()
        if not _SAFE_HUBEAU.fullmatch(code):
            raise ValueError(f"invalid Hub'Eau station code: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self._q_url = (
            "https://hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr"
            f"?code_entite={quote(code)}&grandeur_hydro=Q&size=1&sort=desc"
        )
        self._h_url = (
            "https://hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr"
            f"?code_entite={quote(code)}&grandeur_hydro=H&size=1&sort=desc"
        )
        policy.require_endpoint(self._q_url)
        policy.require_endpoint(self._h_url)

    def _fetch(self, url: str) -> Any:
        """Hub'Eau often answers 206 Partial Content for cursor pages — treat as OK."""
        from gaia.devices._live_base import _UA
        from gaia.devices._policy import _assert_url_allowed

        url = _assert_url_allowed(url)
        headers = {"User-Agent": _UA, **(self.headers or {})}
        try:
            resp = httpx.get(url, headers=headers, timeout=self.timeout, follow_redirects=False)
            if resp.status_code not in (200, 206):
                raise DeviceOffline(
                    f"{self.device_id}: upstream HTTP {resp.status_code}"
                )
            return resp.json()
        except DeviceOffline:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DeviceOffline(
                f"{self.device_id}: upstream unreachable ({type(exc).__name__})"
            ) from exc

    def map(self, payload: Any) -> dict[str, float | None]:
        q_payload, h_payload = payload if isinstance(payload, tuple) and len(payload) == 2 else ({}, {})
        flow_lps, q_epoch, qlat, qlon = _latest_obs(q_payload)
        stage_mm, h_epoch, hlat, hlon = _latest_obs(h_payload)
        now = time.time()
        if not q_epoch or now - q_epoch > _MAX_AGE_S or q_epoch - now > 3600:
            flow_lps = None
        if not h_epoch or now - h_epoch > _MAX_AGE_S or h_epoch - now > 3600:
            stage_mm = None
        flow = flow_lps / 1000.0 if flow_lps is not None else None
        if flow is not None and not (0.0 <= flow <= _MAX_Q_M3S):
            flow = None
        stage = None
        if stage_mm is not None and abs(stage_mm) <= _MAX_H_MM:
            stage = stage_mm / 1000.0  # Hub'Eau H is millimetres
        if flow is None and stage is None:
            raise DeviceOffline(f"{self.device_id}: Hub'Eau has no usable Q/H for {self.station}")
        lat = qlat or hlat or self.latitude
        lon = qlon or hlon or self.longitude
        return {
            "discharge_m3s": flow,
            "gage_height_m": stage,
            "latitude": lat,
            "longitude": lon,
        }

    def sample(self) -> dict[str, float]:
        q_payload = h_payload = {}
        try:
            q_payload = self._fetch(self._q_url)
        except DeviceOffline:
            pass
        try:
            h_payload = self._fetch(self._h_url)
        except DeviceOffline:
            pass
        return {k: v for k, v in self.map((q_payload, h_payload)).items() if v is not None}


class EhydRiver(LiveDevice):
    """eHYD / BMLUK current gauge — Austria, CC BY 4.0."""

    model = "GAIA-RIVER (eHYD)"
    policy_id = "ehyd_austria"
    fields = HubEauRiver.fields
    source = (
        "eHYD / BMLUK Hydrographie Österreich · CC BY 4.0 — "
        "Datenquelle: ehyd.gv.at. Austria in-situ only — not PEGELONLINE DE."
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        hzbnr: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (hzbnr or "").strip()
        if not _SAFE_EHYD.fullmatch(code):
            raise ValueError(f"invalid eHYD hzbnr: {hzbnr!r}")
        self.hzbnr = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = (
            "https://gis.lfrz.gv.at/api/geodata/i000501/ogc/features/v1/"
            "collections/i000501:pegel_aktuell/items"
            f"?filter={quote(f'hzbnr={code}')}&f=json&limit=1"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def map(self, payload: Any) -> dict[str, float | None]:
        feature: dict[str, Any] | None = None
        if isinstance(payload, dict):
            if isinstance(payload.get("properties"), dict):
                feature = payload
            else:
                features = payload.get("features")
                if isinstance(features, list):
                    for row in features:
                        if not isinstance(row, dict):
                            continue
                        props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
                        if str(props.get("hzbnr") or "") == self.hzbnr:
                            feature = row
                            break
                    if feature is None and features and isinstance(features[0], dict):
                        feature = features[0]
        if not feature:
            raise DeviceOffline(f"{self.device_id}: eHYD empty for {self.hzbnr}")
        props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        observed_at = _iso_epoch(props.get("zeitpunkt"))
        now = time.time()
        if not observed_at or now - observed_at > _MAX_AGE_S or observed_at - now > 3600:
            raise DeviceOffline(f"{self.device_id}: eHYD observation timestamp missing or stale")
        value = _num(props.get("wert"))
        param = str(props.get("parameter") or "").upper()
        unit = str(props.get("einheit") or "")
        flow = stage = None
        if value is not None:
            if param == "Q" or "m³/s" in unit or "m3/s" in unit.lower():
                flow = value
            elif param == "W" or unit.strip() in ("cm", "m", "mm"):
                if unit.strip() == "cm":
                    stage = value / 100.0
                elif unit.strip() == "mm":
                    stage = value / 1000.0
                else:
                    stage = value
            elif value > 50:
                flow = value
            else:
                stage = value
        if flow is None and stage is None:
            raise DeviceOffline(f"{self.device_id}: eHYD has no Q/W for {self.hzbnr}")
        coords = (feature.get("geometry") or {}).get("coordinates") if isinstance(feature.get("geometry"), dict) else None
        lon = lat = None
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon, lat = _num(coords[0]), _num(coords[1])
        return {
            "discharge_m3s": flow,
            "gage_height_m": stage,
            "latitude": lat if lat is not None else self.latitude,
            "longitude": lon if lon is not None else self.longitude,
        }


class SmhiMetObs(LiveDevice):
    """SMHI MetObs in-situ weather — Sweden, CC BY 4.0 (not hydroobs)."""

    model = "GAIA-WEATHER (SMHI MetObs)"
    policy_id = "smhi_metobs"
    fields = {
        "temperature_c": "cel",
        "humidity_pct": "percent",
        "pressure_hpa": "hPa",
        "wind_mps": "m/s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "SMHI Open Data MetObs · CC BY 4.0 — attribution: SMHI. "
        "Sweden in-situ stations only — not SMHI hydroobs and not a forecast."
    )

    _PARAMS = (
        (1, "temperature_c"),
        (6, "humidity_pct"),
        (9, "pressure_hpa"),
        (4, "wind_mps"),
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station or "").strip()
        if not _SAFE_SMHI.fullmatch(code):
            raise ValueError(f"invalid SMHI MetObs station: {station!r}")
        self.station = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        policy = require_approved_source(self.policy_id)
        self._urls = {
            field: (
                "https://opendata-download-metobs.smhi.se/api/version/1.0/"
                f"parameter/{param}/station/{quote(code)}/period/latest-hour/data.json"
            )
            for param, field in self._PARAMS
        }
        for url in self._urls.values():
            policy.require_endpoint(url)

    @staticmethod
    def _latest_value(payload: Any) -> float | None:
        rows = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        newest: tuple[float, float] | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = _num(row.get("value"))
            when_ms = _num(row.get("date"))
            if value is None or when_ms is None:
                continue
            when = float(when_ms) / 1000.0
            age = time.time() - when
            if age > _MAX_AGE_S or age < -3600:
                continue
            candidate = (when, float(value))
            if newest is None or candidate[0] > newest[0]:
                newest = candidate
        return newest[1] if newest else None

    def map(self, payload: Any) -> dict[str, float | None]:
        readings = payload if isinstance(payload, dict) else {}
        out: dict[str, float | None] = {
            "temperature_c": None,
            "humidity_pct": None,
            "pressure_hpa": None,
            "wind_mps": None,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
        for field, body in readings.items():
            out[field] = self._latest_value(body)
            if isinstance(body, dict):
                station = body.get("station") if isinstance(body.get("station"), dict) else {}
                # MetObs station block rarely has lat/lon on latest-hour; keep anchors.
                _ = station
        if all(out[k] is None for k in ("temperature_c", "humidity_pct", "pressure_hpa", "wind_mps")):
            raise DeviceOffline(f"{self.device_id}: SMHI MetObs empty for {self.station}")
        return out

    def sample(self) -> dict[str, float]:
        readings: dict[str, Any] = {}
        for field, url in self._urls.items():
            try:
                readings[field] = self._fetch(url)
            except DeviceOffline:
                continue
        return {k: v for k, v in self.map(readings).items() if v is not None}


# ── Dense meshes (operator-facing ATLAS pins) ─────────────────────────────────

HUBEAU_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("hubeau-paris-01", "F700000102", 48.8447, 2.3655, "Seine at Paris Austerlitz"),
    ("hubeau-orleans-01", "K435001010", 47.8980, 1.9045, "Loire at Orléans"),
    ("hubeau-lyon-rhone-01", "V300002001", 45.7683, 4.8384, "Rhône at Lyon Pont Morand"),
    ("hubeau-toulouse-01", "O200004001", 43.5980, 1.4400, "Garonne at Toulouse"),
    ("hubeau-bordeaux-01", "O972001001", 44.8600, -0.5527, "Garonne at Bordeaux"),
    ("hubeau-strasbourg-01", "A061005051", 48.5925, 7.8029, "Rhine at Strasbourg"),
    ("hubeau-verdun-01", "B301001002", 49.1621, 5.3867, "Meuse at Verdun"),
    ("hubeau-grenoble-01", "W141001001", 45.1930, 5.7250, "Isère at Grenoble"),
    ("hubeau-meuse-sm-01", "B222001001", 48.8710, 5.5306, "Meuse at Saint-Mihiel"),
    ("hubeau-dore-01", "K322021001", 45.7663, 2.8305, "Dore basin gauge K322"),
    ("hubeau-tarn-01", "O303101001", 44.3459, 3.6205, "Tarn headwaters O303"),
    ("hubeau-rhone-v352-01", "V352401001", 45.2060, 4.7866, "Rhône tributary V352"),
    ("hubeau-rhone-v501-01", "V501401001", 44.5426, 4.4098, "Rhône mid V501"),
    ("hubeau-rhone-v504-01", "V504881301", 44.4064, 4.2161, "Rhône mid V504"),
    ("hubeau-correze-01", "P392252001", 45.1641, 1.5406, "Corrèze basin P392"),
    ("hubeau-bretagne-01", "J132401001", 48.4903, -2.6061, "Brittany gauge J132"),
    ("hubeau-seine-f466-01", "F466000101", 48.7012, 2.2340, "Seine upstream F466"),
    ("hubeau-seine-f490-01", "F490000104", 48.7802, 2.4183, "Seine F490"),
    ("hubeau-saone-lyon-01", "U472002001", 45.7661, 4.8305, "Saône at Lyon"),
    ("hubeau-rouen-01", "H503011001", 49.4433, 1.0720, "Seine at Rouen"),
)

EHYD_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("ehyd-achleiten-01", "207019", 48.5827, 13.5033, "Donau at Achleiten"),
    ("ehyd-thebner-01", "207407", 48.1665, 16.9837, "Donau at Thebnerstraßl"),
    ("ehyd-wildungs-01", "207373", 48.1148, 16.8043, "Donau at Wildungsmauer"),
    ("ehyd-kienstock-01", "207357", 48.3823, 15.4626, "Donau at Kienstock"),
    ("ehyd-kirchbichl-01", "201889", 47.5228, 12.0933, "Inn at Kirchbichl"),
    ("ehyd-brixlegg-01", "201806", 47.4326, 11.8729, "Inn at Brixlegg"),
    ("ehyd-jenbach-01", "201681", 47.3889, 11.7892, "Inn at Jenbach-Rotholz"),
    ("ehyd-oberndorf-01", "203539", 47.9396, 12.9267, "Salzach at Oberndorf"),
    ("ehyd-salzburg-01", "204180", 47.7984, 13.0544, "Salzach at Salzburg-Nonntal"),
    ("ehyd-golling-01", "203323", 47.5958, 13.1634, "Salzach at Golling"),
    ("ehyd-bruck-mur-01", "211292", 47.4100, 15.2814, "Mur at Bruck"),
    ("ehyd-graz-01", "211326", 47.0783, 15.4329, "Mur at Graz"),
    ("ehyd-mureck-01", "211490", 46.7114, 15.7922, "Mur at Mureck"),
    ("ehyd-lavamuend-01", "213595", 46.6166, 14.9749, "Drau at Lavamünd Grenze"),
    ("ehyd-amlach-01", "213215", 46.7740, 13.5232, "Drau at Amlach"),
    ("ehyd-steyr-01", "205922", 48.0433, 14.4249, "Enns at Steyr"),
    ("ehyd-admont-01", "210823", 47.5812, 14.4617, "Enns at Admont"),
    ("ehyd-badischl-01", "205153", 47.7102, 13.6283, "Traun at Bad Ischl"),
    ("ehyd-bangs-01", "200014", 47.2737, 9.5348, "Rhein at Bangs"),
    ("ehyd-lustenau-01", "200196", 47.4516, 9.6627, "Rhein at Lustenau"),
    ("ehyd-hohenau-01", "207308", 48.6005, 16.9331, "March at Hohenau"),
    ("ehyd-angern-01", "207324", 48.3831, 16.8338, "March at Angern"),
    ("ehyd-deutschhaslau-01", "209007", 48.0476, 16.9431, "Leitha at Deutsch Haslau"),
    ("ehyd-kajetans-01", "201178", 46.9520, 10.5116, "Inn at Kajetansbrücke"),
)

SMHI_METOBS_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("smhi-wx-arlanda-01", "97400", 59.6519, 17.9186, "Stockholm-Arlanda"),
    ("smhi-wx-stockholm-01", "98230", 59.3537, 18.0635, "Stockholm Observatoriekullen"),
    ("smhi-wx-goteborg-01", "71420", 57.7156, 11.9924, "Göteborg A"),
    ("smhi-wx-malmo-01", "52350", 55.5729, 13.0729, "Malmö A"),
    ("smhi-wx-uppsala-01", "97510", 59.9000, 17.5914, "Uppsala Aut"),
    ("smhi-wx-umea-01", "140480", 63.7947, 20.2900, "Umeå Flygplats"),
    ("smhi-wx-lulea-01", "162860", 65.5430, 22.1219, "Luleå Flygplats"),
    ("smhi-wx-orebro-01", "95130", 59.2289, 15.0455, "Örebro Flygplats"),
    ("smhi-wx-linkoping-01", "85240", 58.4061, 15.5264, "Linköping Malmslätt"),
    ("smhi-wx-karlstad-01", "93220", 59.4447, 13.3375, "Karlstad Flygplats"),
    ("smhi-wx-visby-01", "78400", 57.6678, 18.3516, "Visby Flygplats"),
    ("smhi-wx-kiruna-01", "180940", 67.8219, 20.3369, "Kiruna Flygplats"),
    ("smhi-wx-sundsvall-01", "127310", 62.5303, 17.4422, "Sundsvall-Timrå"),
    ("smhi-wx-kalmar-01", "66420", 56.6853, 16.2875, "Kalmar Flygplats"),
    ("smhi-wx-abisko-01", "188790", 68.3538, 18.8164, "Abisko Aut"),
)


def register_p9_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P9 relays and return the number added."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_HUBEAU_ENABLED", "1"):
        for device_id, station, lat, lon, _place in HUBEAU_MESH:
            try:
                fleet.add(
                    HubEauRiver(
                        device_id,
                        clock,
                        station=station,
                        latitude=lat,
                        longitude=lon,
                        site=f"live-river-{device_id}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("Hub'Eau %s skipped: %s", device_id, exc)

    if enabled("GAIA_EHYD_ENABLED", "1"):
        for device_id, hzbnr, lat, lon, _place in EHYD_MESH:
            try:
                fleet.add(
                    EhydRiver(
                        device_id,
                        clock,
                        hzbnr=hzbnr,
                        latitude=lat,
                        longitude=lon,
                        site=f"live-river-{device_id}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("eHYD %s skipped: %s", device_id, exc)

    if enabled("GAIA_SMHI_METOBS_ENABLED", "1"):
        for device_id, station, lat, lon, _place in SMHI_METOBS_MESH:
            try:
                fleet.add(
                    SmhiMetObs(
                        device_id,
                        clock,
                        station=station,
                        latitude=lat,
                        longitude=lon,
                        site=f"live-weather-{device_id}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("SMHI MetObs %s skipped: %s", device_id, exc)
    return n


__all__ = [
    "HubEauRiver",
    "EhydRiver",
    "SmhiMetObs",
    "HUBEAU_MESH",
    "EHYD_MESH",
    "SMHI_METOBS_MESH",
    "register_p9_relays",
]
