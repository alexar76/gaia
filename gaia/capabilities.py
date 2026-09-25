"""GAIA runtime + AIMarket capability spec.

The demo fleet: two co-located weather stations (shared site truth — the
sibling check needs a twin), one air-quality node, one energy meter. Four
priced capabilities plus a free fleet status:

    gaia.weather.read@v1  $0.001   one attested reading (ws-01/ws-02)
    gaia.air.read@v1      $0.001   one attested reading (aq-01)
    gaia.energy.read@v1   $0.001   one attested reading (em-01)
    gaia.window@v1        $0.05    bundle of N readings in one invoke — the
                                   micro-billing pattern: the hub ledger bills
                                   whole cents (ceil), so sub-cent readings are
                                   sold in bundles that clear both the 1¢
                                   quantum and the Pay-on-Verified price floor
    gaia.verify@v1        $0.002   plausibility verdict as a sellable good
                                   (same math the /v1/verify endpoint serves)
    gaia.fleet.status@v1  free     device registry incl. pinned device pubkeys
"""

from __future__ import annotations

import math
import os
from typing import Any

from oracle_core import Capability, OracleSpec

from gaia.clock import SimClock
from gaia.devices import AirQualitySim, EnergyMeterSim, SiteWeather, WeatherStationSim
from gaia.fleet import Fleet
from gaia.plausibility import PlausibilityVerifier
from gaia.verifier import VerifierService


class GatewayRuntime:
    """Everything the handlers close over: clock, fleet, verifier."""

    def __init__(
        self,
        *,
        key_dir: str = "data/devices",
        seed: int = 0,
        start_epoch: float = 1_767_225_600.0,
        tick_s: float = 60.0,
        autotick: bool = True,
    ):
        live = os.environ.get("GAIA_ENABLE_LIVE", "").strip().lower() in ("1", "true", "yes", "on")
        # Live relays stamp wall-clock fetch time; frozen sim-time would fail
        # freshness / rate checks against real upstream observations.
        self.clock = SimClock(start_epoch, realtime=live)
        self.tick_s = tick_s
        self.autotick = autotick
        self.fleet = Fleet()

        site = SiteWeather(self.clock, seed=seed)
        self.fleet.add(WeatherStationSim("ws-01", self.clock, site, site="demo-site-1",
                                         seed=seed, key_dir=key_dir))
        self.fleet.add(WeatherStationSim("ws-02", self.clock, site, site="demo-site-1",
                                         seed=seed + 1, key_dir=key_dir))
        self.fleet.add(AirQualitySim("aq-01", self.clock, site="demo-site-1",
                                     seed=seed + 2, key_dir=key_dir))
        self.fleet.add(EnergyMeterSim("em-01", self.clock, site="demo-site-1",
                                      seed=seed + 3, key_dir=key_dir))

        # Operator SIM extras from gaia/config/extra_sensors.yaml
        from gaia.devices.extra_sensors import register_sim_extras

        register_sim_extras(self.fleet, self.clock, key_dir=key_dir, seed=seed + 10)

        # Optional LIVE relays alongside the simulators (opt-in via GAIA_ENABLE_LIVE).
        # Each read hits a real public API (NWS, Open-Meteo, UK carbon, USGS, NOAA
        # tides, openSenseMap, SensorThings, optional OpenAQ) through the same
        # Ed25519 + plausibility path. Hosts are allowlisted (SSRF). Off by default
        # for deterministic tests; public demo sets GAIA_ENABLE_LIVE=1.
        if live:
            from gaia.devices.live import build_live_fleet

            for _dev in build_live_fleet(self.clock, key_dir=key_dir).devices():
                self.fleet.add(_dev)

        self.verifier = PlausibilityVerifier(self.fleet)
        self.service = VerifierService(self.verifier)

    def read(self, device_id: str) -> dict[str, Any]:
        """One reading; in autotick mode simulated time advances per read so
        consecutive reads see a moving world (like polling real hardware)."""
        if self.autotick:
            self.clock.advance(self.tick_s)
        return self.fleet.read(device_id)

    def warm_up(self, readings_per_device: int = 40) -> None:
        """Build enough history for z-scores/siblings before selling verdicts."""
        from gaia.devices.live import LiveDevice

        for _ in range(readings_per_device):
            self.clock.advance(self.tick_s)
            for device in self.fleet.devices():
                # Live relays build history from real reads over time — never hammer
                # a real public API with synthetic warm-up traffic.
                if isinstance(device, LiveDevice):
                    continue
                if device.fault.kind != "dropout":
                    self.fleet.read(device.device_id)


# ── Handlers ──────────────────────────────────────────────────────────────────


def _read_handler(runtime: GatewayRuntime, default_device: str):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        device_id = str(data.get("device_id") or default_device)
        return runtime.read(device_id)  # ValueError (unknown) -> {ok:false}
    return handler


# ── A place, not a device id ──────────────────────────────────────────────────
# A buyer's model asks for "weather in Berlin" or passes coordinates; it has never
# seen a fleet device id. Such a call used to fall through to the demo-site
# simulator and came back ok:true with invented numbers under a valid signature.
# Now a place resolves to a live relay, or the call is refused and says why. The
# simulators still answer, but only to their own device_id.

#: How far a city relay may be from the asked-for point and still be its weather.
_NEAREST_RELAY_MAX_KM = 75.0

#: Legacy relays that predate the city mesh: om-wx-01 / om-aq-01 default to Berlin.
_LEGACY_PLACES = (
    {"slug": "berlin", "place": "Berlin", "lat": 52.52, "lon": 13.41,
     "aliases": ["berlin", "берлин", "berlín", "柏林", "ベルリン"]},
)

#: What may follow the comma in "City, …" for each city GAIA relays: the country in the
#: common English and Russian spellings, its ISO codes, and for cities in federations the
#: state or province. "Paris, France" is Paris; "Paris, Texas" is not a place GAIA relays
#: and is refused instead of being answered with France's weather under a valid signature.
_US = ("usa", "us", "united states", "united states of america", "america", "сша")
_CA = ("canada", "ca", "can", "канада")
_AU = ("australia", "au", "aus", "австралия")
_CN = ("china", "cn", "chn", "prc", "китай")
_IN = ("india", "in", "ind", "индия")
_ZA = ("south africa", "za", "zaf", "rsa", "юар")
_CITY_QUALIFIERS: dict[str, tuple[str, ...]] = {
    "berlin": ("germany", "de", "deu", "deutschland", "германия"),
    "ottawa": _CA + ("ontario", "on"), "toronto": _CA + ("ontario", "on"),
    "vancouver": _CA + ("british columbia", "bc"),
    "delhi": _IN, "mumbai": _IN + ("maharashtra",),
    "tokyo": ("japan", "jp", "jpn", "япония"),
    "sydney": _AU + ("new south wales", "nsw"), "melbourne": _AU + ("victoria", "vic"),
    "saopaulo": ("brazil", "brasil", "br", "bra", "sp", "бразилия"),
    "lagos": ("nigeria", "ng", "nga", "нигерия"),
    "cairo": ("egypt", "eg", "egy", "египет"),
    "capetown": _ZA, "johannesburg": _ZA,
    "singapore": ("singapore", "sg", "sgp", "сингапур"),
    "dubai": ("uae", "united arab emirates", "ae", "are", "оаэ"),
    "losangeles": _US + ("california", "ca"), "anchorage": _US + ("alaska", "ak"),
    "newyork": _US + ("new york", "ny", "nyc"), "chicago": _US + ("illinois", "il"),
    "miami": _US + ("florida", "fl"),
    "mexicocity": ("mexico", "méxico", "mx", "mex", "cdmx", "мексика"),
    "moscow": ("russia", "ru", "rus", "russian federation", "россия", "рф"),
    "paris": ("france", "fr", "fra", "франция"),
    "reykjavik": ("iceland", "is", "isl", "исландия"),
    "buenosaires": ("argentina", "ar", "arg", "аргентина"),
    "jakarta": ("indonesia", "id", "idn", "индонезия"),
    "nairobi": ("kenya", "ke", "ken", "кения"),
    "london": ("uk", "united kingdom", "gb", "gbr", "england", "britain", "great britain",
               "великобритания", "англия"),
    "madrid": ("spain", "españa", "es", "esp", "испания"),
    "rome": ("italy", "italia", "it", "ita", "италия"),
    "istanbul": ("turkey", "türkiye", "turkiye", "tr", "tur", "турция"),
    "warsaw": ("poland", "polska", "pl", "pol", "польша"),
    "stockholm": ("sweden", "sverige", "se", "swe", "швеция"),
    "athens": ("greece", "gr", "grc", "греция"),
    "seoul": ("south korea", "korea", "republic of korea", "kr", "kor", "корея", "южная корея"),
    "shanghai": _CN, "beijing": _CN,
    "bangkok": ("thailand", "th", "tha", "таиланд"),
    "manila": ("philippines", "ph", "phl", "филиппины"),
    "hongkong": ("hong kong", "hk", "hkg", "sar") + _CN + ("гонконг",),
    "santiago": ("chile", "cl", "chl", "чили"),
    "lima": ("peru", "pe", "per", "перу"),
    "bogota": ("colombia", "co", "col", "колумбия"),
    "casablanca": ("morocco", "ma", "mar", "марокко"),
    "accra": ("ghana", "gh", "gha", "гана"),
    "addisababa": ("ethiopia", "et", "eth", "эфиопия"),
    "auckland": ("new zealand", "nz", "nzl", "новая зеландия"),
    "riyadh": ("saudi arabia", "sa", "sau", "ksa", "саудовская аравия"),
    "telaviv": ("israel", "il", "isr", "израиль"),
    "almaty": ("kazakhstan", "kz", "kaz", "казахстан"),
}


def _norm(name: str) -> str:
    return " ".join(str(name).casefold().split())


def _all_places():
    from gaia.devices.om_mesh import OM_MESH_CITIES

    return (*_LEGACY_PLACES, *OM_MESH_CITIES)


def _place_coordinate(name: str) -> tuple[float, float, str, str] | None:
    """City name → (lat, lon, place, slug), or None when it is no city GAIA relays.

    Matches the slug, the place or any alias in gaia/config/om_mesh_cities.yaml. A
    qualifier after a comma must name that city's country or state, else the whole
    name is refused: a same-named city elsewhere is not a near miss, it is a wrong answer.
    """
    full = _norm(name)
    head, _, rest = name.partition(",")
    qualifiers = [_norm(q) for q in rest.split(",") if q.strip()] if rest else []
    for city in _all_places():
        names = {_norm(n) for n in (city["slug"], city["place"], *city.get("aliases", []))}
        if full in names:
            return float(city["lat"]), float(city["lon"]), str(city["place"]), str(city["slug"])
        if qualifiers and _norm(head) in names:
            allowed = {_norm(q) for q in _CITY_QUALIFIERS.get(str(city["slug"]), ())}
            if any(q in allowed for q in qualifiers):
                return float(city["lat"]), float(city["lon"]), str(city["place"]), str(city["slug"])
            raise ValueError(
                f"{name.strip()[:80]!r} is not a city GAIA relays: {city['place']} is, but not "
                "with that qualifier. Pass latitude and longitude for any other place."
            )
    return None


def _known_places(limit: int = 12) -> str:
    places = [c["place"] for c in _all_places()]
    more = f", … ({len(places)} cities)" if len(places) > limit else ""
    return ", ".join(places[:limit]) + more


def _requested_location(raw: dict[str, Any]) -> dict[str, Any] | None:
    """latitude+longitude (lat/lon/lng accepted) or city/place/location, else None.

    Returns {lat, lon, requested, place, slug}: `requested` is what the caller sent,
    verbatim, so the answer can show it next to what it was matched to.
    """
    lat = raw.get("latitude", raw.get("lat"))
    lon = raw.get("longitude", raw.get("lon", raw.get("lng")))
    if (lat is None) != (lon is None):
        raise ValueError("latitude and longitude must be supplied together")
    if lat is not None:
        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError) as exc:
            raise ValueError("latitude and longitude must be numbers") from exc
        if not (math.isfinite(lat_f) and math.isfinite(lon_f)
                and -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
            raise ValueError("latitude must be in [-90, 90] and longitude in [-180, 180]")
        return {"lat": lat_f, "lon": lon_f, "requested": f"{lat_f:.4f},{lon_f:.4f}",
                "place": None, "slug": None}
    for key in ("city", "place", "location"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            found = _place_coordinate(value)
            if found is None:
                raise ValueError(
                    f"unknown {key} {value.strip()[:60]!r}: pass latitude and longitude, "
                    f"or a city GAIA relays ({_known_places()}), or an exact device_id"
                )
            lat_f, lon_f, place, slug = found
            return {"lat": lat_f, "lon": lon_f, "requested": value.strip()[:120],
                    "place": place, "slug": slug}
    return None


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def _runs_live(runtime: GatewayRuntime) -> bool:
    """Any live relay registered — not "any Open-Meteo relay", which a live GAIA can lack."""
    from gaia.devices._live_base import LiveDevice

    return any(isinstance(d, LiveDevice) for d in runtime.fleet.devices())


def _weather_read_handler(runtime: GatewayRuntime, sim_default: str = "ws-01"):
    """gaia.weather.read: exact device_id, else the nearest live city relay to a place."""
    from gaia.devices.live_om import OpenMeteoWeather

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        device_id = str(raw.get("device_id") or "").strip()
        if device_id:
            return runtime.read(device_id)
        where = _requested_location(raw)
        if not _runs_live(runtime):
            if where is not None:
                raise ValueError(
                    "this GAIA runs simulators only, so it has no weather for a real place; "
                    f"device_id={sim_default} reads the demo-site simulator"
                )
            return runtime.read(sim_default)  # sim-only fleet: the sim is all there is
        if where is None:
            raise ValueError(
                "gaia.weather.read needs a place: latitude and longitude, or city "
                "(e.g. \"Tokyo\"), or an exact device_id. The demo-site simulator "
                f"answers only to device_id={sim_default}."
            )
        relays = [d for d in runtime.fleet.devices() if isinstance(d, OpenMeteoWeather)]
        if not relays:
            raise ValueError(
                "this GAIA has no city weather relays to match a place against; pass an "
                "exact device_id from gaia.fleet.status"
            )
        device, km = min(
            ((d, _km(where["lat"], where["lon"], d.latitude, d.longitude)) for d in relays),
            key=lambda pair: pair[1],
        )
        if km > _NEAREST_RELAY_MAX_KM:
            raise ValueError(
                f"no live weather relay within {_NEAREST_RELAY_MAX_KM:.0f} km of "
                f"{where['requested']}; the nearest is {device.device_id} at {km:.0f} km — pass "
                f"device_id={device.device_id} to read it anyway"
            )
        out = runtime.read(device.device_id)
        return {**out, "resolved": {
            "requested": where["requested"], "matched_place": where["place"],
            "device_id": device.device_id,
            "relay_latitude": device.latitude, "relay_longitude": device.longitude,
            "distance_km": round(km, 1),
        }}

    return handler


def _air_read_handler(runtime: GatewayRuntime, sim_default: str = "aq-01",
                      point_relay: str = "om-aq-01"):
    """gaia.air.read: exact device_id as before, else live air quality at the place.

    A city GAIA relays reads that city's own relay (om-aq-{slug}, its own history for the
    plausibility checks); any other point reads om-aq-01 moved to that coordinate.
    """
    from gaia.devices.live_om import OpenMeteoAirQuality

    geo = _p4_geo_handler(runtime, sim_default)

    def _city_relay(slug: str | None, lat: float, lon: float) -> str | None:
        if not slug:
            return None
        for candidate in (f"om-aq-{slug}", point_relay):
            try:
                device = runtime.fleet.get(candidate)
            except ValueError:
                continue
            anchor = getattr(device, "_default_coordinate", None)
            if isinstance(device, OpenMeteoAirQuality) and anchor and _km(lat, lon, *anchor) < 5.0:
                return candidate
        return None

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        if str(raw.get("device_id") or "").strip():
            return geo(raw)
        where = _requested_location(raw)
        try:
            runtime.fleet.get(point_relay)
        except ValueError:
            if where is not None:
                raise ValueError(
                    "this GAIA has no live air-quality relay for an arbitrary place; pass an "
                    "exact device_id from gaia.fleet.status"
                    + ("" if _runs_live(runtime) else f" (device_id={sim_default} is the simulator)")
                ) from None
            if _runs_live(runtime):
                raise ValueError("gaia.air.read needs a place or an exact device_id") from None
            return geo(raw)  # sim-only fleet: the sim is all there is
        if where is None:
            raise ValueError(
                "gaia.air.read needs a place: latitude and longitude, or city "
                "(e.g. \"Delhi\"), or an exact device_id. The demo-site simulator "
                f"answers only to device_id={sim_default}."
            )
        own = _city_relay(where["slug"], where["lat"], where["lon"])
        if own is not None:
            out = geo({"device_id": own})
            resolved = {"device_id": own}
        else:
            out = geo({"device_id": point_relay, "latitude": where["lat"], "longitude": where["lon"]})
            resolved = {"device_id": point_relay}
        return {**out, "resolved": {
            "requested": where["requested"], "matched_place": where["place"], **resolved,
            "latitude": where["lat"], "longitude": where["lon"],
        }}

    return handler


def _fire_read_handler(runtime: GatewayRuntime, default_device: str):
    """Fire SKU: device_id + optional bbox + packetized hotspots (cursor resume)."""

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        from gaia.devices.hotspot_pages import CursorError, clamp_page_size
        from gaia.devices.live_open import (
            FirmsFireHotspot,
            _clamp_collect_total,
            _clamp_grid_deg,
            _parse_bbox,
        )

        device_id = str(data.get("device_id") or default_device)
        device = runtime.fleet.get(device_id)
        if not isinstance(device, FirmsFireHotspot):
            return runtime.read(device_id)

        raw = data if isinstance(data, dict) else {}
        cursor = str(raw.get("cursor") or "").strip() or None
        page_size = (
            clamp_page_size(raw.get("page_size"))
            if raw.get("page_size") is not None
            else None
        )

        # Resume path — no upstream re-fetch; same cursor is idempotent.
        if cursor:
            try:
                return device.read_page_from_cursor(cursor, page_size=page_size)
            except CursorError as exc:
                raise ValueError(str(exc)) from exc

        bbox = _parse_bbox(raw)
        # Collect ceiling for the ranked session (may span many pages).
        # ``max_total`` preferred; ``limit`` kept for back-compat (= collect max).
        # Explicit ``max_total: null`` must still fall back to ``limit``.
        collect_raw = raw.get("max_total")
        if collect_raw is None:
            collect_raw = raw.get("limit")
        collect_max = (
            _clamp_collect_total(collect_raw) if collect_raw is not None else None
        )
        stratified = bool(raw.get("stratified"))
        densify_mode = str(raw.get("densify_mode") or "").strip().lower()
        if densify_mode == "stratified":
            stratified = True
        grid_deg = _clamp_grid_deg(raw.get("grid_deg"))

        # Gate the whole set→read→clear window: handlers run concurrently
        # (asyncio.to_thread), and interleaved set_query would hand buyer A a
        # reading filtered by buyer B's bbox.
        with device.query_gate:
            device.set_query(
                bbox=bbox,
                collect_max=collect_max,
                page_size=page_size,
                limit=None,
                stratified=stratified,
                grid_deg=grid_deg,
            )
            try:
                return runtime.read(device_id)
            finally:
                device.clear_query()

    return handler


def _argo_read_handler(runtime: GatewayRuntime, default_device: str):
    """Argo SKU: global active-float directory or one addressed WMO profile."""

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        from gaia.devices.live_p0 import ArgoFloat

        raw = data if isinstance(data, dict) else {}
        device_id = str(raw.get("device_id") or default_device)
        wmo = str(raw.get("wmo") or "").strip()
        # Friendly virtual address for agents; the canonical input remains
        # {device_id: argo-01, wmo: 690...} and uses one pinned relay key.
        if not wmo and device_id.startswith("argo-wmo-"):
            wmo = device_id.removeprefix("argo-wmo-")
            device_id = default_device
        if device_id != default_device:
            return runtime.read(device_id)
        device = runtime.fleet.get(default_device)
        if not isinstance(device, ArgoFloat) or not wmo:
            return runtime.read(default_device)
        # Keep set → read → clear atomic across concurrent paid invokes.
        with device.query_gate:
            device.set_wmo(wmo)
            try:
                return runtime.read(default_device)
            finally:
                device.clear_wmo()

    return handler


def _gnss_integrity_handler(runtime: GatewayRuntime, default_device: str):
    """GNSS SKU: official network directory or one exact station id."""

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        from gaia.devices.gnss import EurefGnssIntegrity, GaGnssInventory

        raw = data if isinstance(data, dict) else {}
        device_id = str(raw.get("device_id") or default_device)
        station_id = str(raw.get("station_id") or "").strip().upper()
        if not station_id and device_id.startswith("gnss-station:euref:"):
            station_id = device_id.removeprefix("gnss-station:euref:")
            device_id = "gnss-euref-01"
        elif not station_id and device_id.startswith("gnss-station:ga:"):
            station_id = device_id.removeprefix("gnss-station:ga:")
            device_id = "gnss-ga-01"
        device = runtime.fleet.get(device_id)
        if not isinstance(device, (EurefGnssIntegrity, GaGnssInventory)) or not station_id:
            return runtime.read(device_id)
        with device.query_gate:
            device.set_station(station_id)
            try:
                return runtime.read(device_id)
            finally:
                device.clear_station()

    return handler


def _p4_geo_handler(runtime: GatewayRuntime, default_device: str):
    """P4 network/grid reads: fixed device by default, complete bbox on demand."""

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        from gaia.devices.live_om import OpenMeteoAirQuality
        from gaia.devices.live_p0 import SensorCommunityAir
        from gaia.devices.live_p4 import (
            CamsAirComposition,
            CopernicusSoilWaterIndex,
            NasaImergPrecipitation,
            NasaPowerSolar,
            NoaaNohrscSnow,
            NoaaNsidcSeaIceIndex,
            UsgsWaterQuality,
        )

        raw = data if isinstance(data, dict) else {}
        device_id = str(raw.get("device_id") or default_device)
        device = runtime.fleet.get(device_id)
        keys = ("west", "south", "east", "north")
        bbox_fields = [raw.get(k) is not None for k in keys]
        if any(bbox_fields) and not all(bbox_fields):
            raise ValueError("west, south, east and north must be supplied together")
        if not isinstance(device, UsgsWaterQuality) or not all(bbox_fields):
            point_types = (
                OpenMeteoAirQuality, SensorCommunityAir, CamsAirComposition,
                CopernicusSoilWaterIndex, NasaImergPrecipitation,
                NasaPowerSolar, NoaaNohrscSnow, NoaaNsidcSeaIceIndex,
            )
            has_lat = raw.get("latitude") is not None
            has_lon = raw.get("longitude") is not None
            if has_lat != has_lon:
                raise ValueError("latitude and longitude must be supplied together")
            if not isinstance(device, point_types):
                return runtime.read(device_id)
            if not has_lat:
                # The gate here too: a coordinate read elsewhere moves this device's URL,
                # and an ungated plain read in that window fetched the other city's data.
                with device.query_gate:
                    return runtime.read(device_id)
            lat, lon = float(raw["latitude"]), float(raw["longitude"])
            with device.query_gate:
                home_site = device.site
                device.set_coordinate(lat, lon)
                # Its own site per coordinate, so the verifier's history for this reading
                # is readings of THIS point — never another city's (plausibility.py).
                device.site = f"{home_site}@{lat:.4f},{lon:.4f}"
                try:
                    return runtime.read(device_id)
                finally:
                    device.site = home_site
                    device.clear_coordinate()
        bbox = tuple(float(raw[k]) for k in keys)
        limit = int(raw.get("limit") or 10_000)
        with device.query_gate:
            device.set_query(
                bbox=bbox,
                limit=limit,
                parameters=raw.get("parameters"),
                require_all=bool(raw.get("require_all")),
                max_age_hours=float(raw.get("max_age_hours") or 48.0),
            )
            try:
                return runtime.read(device_id)
            finally:
                device.clear_query()

    return handler


def _window_handler(runtime: GatewayRuntime):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        device_id = str(data.get("device_id") or "ws-01")
        n = int(data.get("n") or 10)
        if not 1 <= n <= 500:
            raise ValueError("n must be in [1, 500]")
        readings = [runtime.read(device_id) for _ in range(n)]
        return {"device_id": device_id, "count": n, "readings": readings}
    return handler


def _verify_handler(runtime: GatewayRuntime):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        reading = data.get("reading")
        if not isinstance(reading, dict):
            raise ValueError("input must carry a 'reading' object")
        attestation = data.get("attestation") if isinstance(data.get("attestation"), dict) else None
        min_score = data.get("min_verify_score")
        verdict = runtime.verifier.check(
            reading, attestation,
            min_score=float(min_score) if min_score is not None else None,
        )
        return verdict.to_dict()
    return handler


def _status_handler(runtime: GatewayRuntime):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        return runtime.fleet.status()
    return handler


# ── Spec assembly ─────────────────────────────────────────────────────────────

_READING_OUT = {
    "type": "object",
    "properties": {
        "reading": {"type": "object", "description": "device_id/model/site/seq/ts/values/units"},
        "attestation": {"type": "object", "description": "Ed25519 device signature over the reading canonical"},
    },
}

_DEVICE_IN = {
    "type": "object",
    "properties": {"device_id": {"type": "string", "description": "fleet device id"}},
}

_ARGO_IN = {
    "type": "object",
    "properties": {
        "device_id": {
            "type": "string",
            "description": "relay id (default argo-01; argo-wmo-{WMO} also accepted)",
        },
        "wmo": {
            "type": "string",
            "pattern": "^[0-9]{5,8}$",
            "description": (
                "optional Argo WMO platform number. Omit for the official global "
                "active-float directory; set it for that float's latest profile"
            ),
        },
    },
}

_GNSS_IN = {
    "type": "object",
    "properties": {
        "device_id": {
            "type": "string",
            "description": (
                "relay id gnss-euref-01 or gnss-ga-01; exact virtual ids "
                "gnss-station:euref:{STATION_ID} and gnss-station:ga:{STATION_ID} "
                "are also accepted"
            ),
        },
        "station_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9]{4}([A-Za-z0-9]{5})?$",
            "description": "optional source station id; omit for the selected network inventory",
        },
    },
}

_P4_GEO_IN = {
    "type": "object",
    "properties": {
        "device_id": {"type": "string", "description": "fleet device id"},
        "latitude": {
            "type": "number", "minimum": -90, "maximum": 90,
            "description": "optional arbitrary source coordinate",
        },
        "longitude": {
            "type": "number", "minimum": -180, "maximum": 180,
            "description": "optional arbitrary source coordinate",
        },
        "west": {"type": "number", "minimum": -180, "maximum": 180},
        "south": {"type": "number", "minimum": -90, "maximum": 90},
        "east": {"type": "number", "minimum": -180, "maximum": 180},
        "north": {"type": "number", "minimum": -90, "maximum": 90},
        "limit": {
            "type": "integer", "minimum": 1, "maximum": 10000,
            "description": "maximum source rows per parameter in a bbox query",
        },
    },
}

_PLACE_PROPS = {
    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
    "city": {
        "type": "string",
        "description": "city name in any listed spelling, e.g. Tokyo / Токио / 東京",
        # A default so tools that build example calls from defaults (Hephaestus Studio's
        # example chain) send a place: an empty call is refused on a live GAIA.
        "default": "Berlin",
    },
}

_WEATHER_IN = {
    "type": "object",
    "properties": {
        **_PLACE_PROPS,
        "device_id": {
            "type": "string",
            "description": "exact relay id; overrides the place (sim ws-01/ws-02 only this way)",
        },
    },
}

_AIR_IN = {
    **_P4_GEO_IN,
    "properties": {
        **_P4_GEO_IN["properties"],
        **_PLACE_PROPS,
        "device_id": {
            "type": "string",
            "description": "exact relay id; overrides the place (sim aq-01 only this way)",
        },
    },
}

_WATER_QUALITY_IN = {
    **_P4_GEO_IN,
    "properties": {
        **_P4_GEO_IN["properties"],
        "limit": {
            "type": "integer", "minimum": 1, "maximum": 10000,
            "description": (
                "USGS OGC page size per parameter; GAIA follows every next link "
                "and refuses a silently truncated station inventory"
            ),
        },
        "parameters": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "enum": [
                    "water_temperature_c", "ph", "dissolved_oxygen_mg_l",
                    "specific_conductance_us_cm", "00010", "00400", "00300", "00095",
                ],
            },
            "description": "required USGS parameter fields/codes; omitted means all supported",
        },
        "require_all": {
            "type": "boolean",
            "description": "true keeps only stations currently reporting every requested parameter",
        },
        "max_age_hours": {
            "type": "number", "minimum": 1, "maximum": 720, "default": 48,
            "description": "maximum age of a sellable latest observation; stale series are excluded",
        },
    },
}

_FIRE_IN = {
    "type": "object",
    "properties": {
        "device_id": {
            "type": "string",
            "description": "fleet device id (default firms-fire-01)",
        },
        "west": {"type": "number", "description": "optional bbox west (°lon)"},
        "south": {"type": "number", "description": "optional bbox south (°lat)"},
        "east": {"type": "number", "description": "optional bbox east (°lon)"},
        "north": {"type": "number", "description": "optional bbox north (°lat)"},
        "limit": {
            "type": "integer",
            "description": (
                "max hotspots to collect into the session (1–250000; default "
                "GAIA_FIRMS_COLLECT_MAX). Delivered in pages of page_size; use "
                "next_cursor to resume. Alias of max_total."
            ),
        },
        "max_total": {
            "type": "integer",
            "description": "same as limit — max ranked hotspots kept for paging",
        },
        "page_size": {
            "type": "integer",
            "description": "hotspots per packet (1–2000; default 500)",
        },
        "cursor": {
            "type": "string",
            "description": (
                "opaque continuation from previous next_cursor — idempotent retry "
                "safe; no upstream re-fetch"
            ),
        },
        "stratified": {
            "type": "boolean",
            "description": (
                "when true with a bbox, pick top brightest per grid cell before "
                "global top-N (wide-map geographic spread)"
            ),
        },
        "densify_mode": {
            "type": "string",
            "enum": ["brightest", "stratified"],
            "description": "alias for stratified=true when set to stratified",
        },
        "grid_deg": {
            "type": "number",
            "description": "grid cell size in degrees for stratified mode (0.5–30; default 5)",
        },
    },
}

_FIRE_OUT = {
    "type": "object",
    "properties": {
        "reading": {
            "type": "object",
            "description": (
                "device_id/model/site/seq/ts/values/units plus hotspot packet fields: "
                "hotspots[] (this page), hotspot_count, hotspot_total, hotspot_offset, "
                "hotspot_page_size, next_cursor (null when done), fetch_id, truncated"
            ),
        },
        "attestation": {
            "type": "object",
            "description": "Ed25519 device signature over the reading canonical (values hash)",
        },
    },
}

_GNSS_OUT = {
    "type": "object",
    "properties": {
        "reading": {
            "type": "object",
            "description": (
                "Signed network inventory or exact station reading. hotspots[] exposes stable "
                "point_id, coordinates, network, claim_class, state, cause, source_url and "
                "license; availability/latency are included only when the source publishes them."
            ),
        },
        "attestation": {
            "type": "object",
            "description": "Ed25519 relay signature over the reading canonical values hash",
        },
    },
}


def build_spec(runtime: GatewayRuntime, public_url: str | None = None) -> OracleSpec:
    url = public_url or os.environ.get("GAIA_PUBLIC_URL", "http://localhost:9320")
    product = "gaia.gateway"

    def _has(device_id: str) -> bool:
        try:
            runtime.fleet.get(device_id)
            return True
        except ValueError:
            return False

    caps = [
        Capability(
            capability_id="gaia.weather.read@v1",
            description="Current weather (temperature, humidity, pressure, wind) at a place: "
                        "pass latitude+longitude or city (e.g. \"Tokyo\"); GAIA reads the "
                        "nearest live Open-Meteo relay within 75 km, or refuses. "
                        "Ed25519-attested. An exact device_id overrides the place: "
                        "om-wx-01, nws-01, Met Éireann met-ie-01, SMHI MetObs "
                        "smhi-wx-*-01 (CC BY 4.0), MET Norway Frost frost-*-01 "
                        "(in-situ, not METAR), DMI dmi-*-01, Singapore NEA "
                        "sg-wx-*-01 (Open Data Licence), HKO hko-wx-*-01 "
                        "(DATA.GOV.HK attribution), AEMET aemet-wx-*-01 (Fuente: AEMET), "
                        "MeteoSwiss ch-wx-*-01 (CC BY 4.0), CWA cwa-wx-*-01 (OGDL 1.0), "
                        "Météo-France mf-wx-*-01 (Etalab OL 2.0), Estonia ee-wx-*-01 "
                        "(CC BY 4.0), Iceland is-wx-*-01 (ODC-By), GeoSphere Austria "
                        "at-wx-*-01 (CC BY 4.0), Lithuania lt-wx-*-01 (CC BY-SA 4.0), "
                        "Latvia lv-wx-*-01 (CC0-1.0), JMA AMeDAS jp-wx-*-01 "
                        "(Public Data License), CHMU cz-wx-*-01 (CC BY 4.0), "
                        "or KMA kr-wx-*-01 "
                        "(Public Nuri Type 1; needs GAIA_KMA_SERVICE_KEY) when live. "
                        "Demo-site simulators ws-01/ws-02 answer only to their device_id "
                        "(and to an empty call on a simulator-only GAIA).",
            handler=_weather_read_handler(runtime, "ws-01"),
            product_id=product, input_schema=_WEATHER_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=15,
        ),
        Capability(
            capability_id="gaia.air.read@v1",
            description="Current air quality (PM2.5, PM10, US/EU AQI) at a place: pass "
                        "latitude+longitude or city (e.g. \"Delhi\"); GAIA reads live "
                        "Open-Meteo at that point (om-aq-01). Ed25519-attested. An exact "
                        "device_id overrides the place. "
                        "om-aq-01 accepts any latitude/longitude through the commercial or "
                        "self-hosted Open-Meteo licence gate. sc-01 / sc-{slug} accept "
                        "latitude/longitude as a Sensor.Community area query (ODbL — cite; "
                        "empty area → offline; crowd density ≠ global). Also Singapore PSI "
                        "sg-psi-*-01 (Open Data Licence), IRCELINE be-aq-*-01 (CC BY 4.0), "
                        "and Hong Kong EPD AQHI hk-aqhi-*-01 (DATA.GOV.HK; health index, not PM2.5). "
                        "Other device_ids remain fixed. Demo-site simulator aq-01 answers "
                        "only to its device_id (and to an empty call on a simulator-only GAIA).",
            handler=_air_read_handler(runtime, "aq-01"),
            product_id=product, input_schema=_AIR_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=15,
        ),
        Capability(
            capability_id="gaia.energy.read@v1",
            description="One attested energy-meter reading (V/A/W + monotonic Wh register). "
                        "Sim device_id=em-01 (no public live grid meter yet).",
            handler=_read_handler(runtime, "em-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=15,
        ),
    ]
    # Live-only SKUs: only advertised when the relay device is actually registered
    # (GAIA_ENABLE_LIVE=1). Keeps the deterministic test fleet at six caps.
    if _has("uk-grid-01") or _has("rte-grid-01"):
        default_grid = "uk-grid-01" if _has("uk-grid-01") else "rte-grid-01"
        caps.append(Capability(
            capability_id="gaia.grid.read@v1",
            description="Live grid carbon-intensity relay (gCO₂/kWh): UK National Grid ESO "
                        "Carbon Intensity (uk-grid-01) and/or RTE éCO2mix France national "
                        "production-only CO₂ rte-grid-01 (Etalab OL 2.0; excludes "
                        "imports/lifecycle). Default "
                        f"device_id={default_grid}. Ed25519-attested.",
            handler=_read_handler(runtime, default_grid),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=80,
        ))
    if (
        _has("usgs-quake-01") or _has("emsc-01") or _has("geonet-01")
        or _has("geoshake-01") or _has("ingv-01") or _has("jma-quake-01")
    ):
        default_quake = (
            "usgs-quake-01" if _has("usgs-quake-01")
            else ("emsc-01" if _has("emsc-01") else ("geonet-01" if _has("geonet-01") else ("geoshake-01" if _has("geoshake-01") else ("ingv-01" if _has("ingv-01") else "jma-quake-01"))))
        )
        caps.append(Capability(
            capability_id="gaia.quake.read@v1",
            description="Live earthquake relay — USGS GeoJSON M≥2.5, EMSC FDSN (CC BY 4.0, "
                        "cite EMSC; preliminary), GeoNet NZ, GeoShake (CC BY 4.0), "
                        "INGV Italy FDSN (CC BY 4.0, cite INGV), and/or JMA official "
                        "list.json jma-quake-01 (Public Data License; cite JMA; not p2pquake). Default "
                        f"device_id={default_quake}. Distinct pins; local catalogs do not replace USGS.",
            handler=_read_handler(runtime, default_quake),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=120,
        ))
    from gaia.devices.live import NDBCBuoy, NOAATideStation, OpenMeteoMarine, USGSRiverGauge
    from gaia.devices.live_p1 import UhslcTide
    from gaia.devices.live_p2 import EcccHydrometric, SmhiHydrology
    from gaia.devices.live_p5 import AviationMetar, PegelonlineRiver
    from gaia.devices.live_p6 import EaHydrologyRiver, RwsRiver, UsaceReservoir
    from gaia.devices.live_p7 import NrwRiver, SepaRiver
    from gaia.devices.live_p8 import CdipWave, IcosGhg
    from gaia.devices.live_p9 import EhydRiver, HubEauRiver
    from gaia.devices.live_p11 import BafuRiver
    from gaia.devices.live_p12 import GfmFlood, LhmtRiver, OpwRiver
    from gaia.devices.live_p12_flood import VicFlood
    from gaia.devices.live_p12_lv import LvgmcRiver

    tide_ids = [
        d.device_id
        for d in runtime.fleet.devices()
        if isinstance(d, (NOAATideStation, UhslcTide))
    ]
    if tide_ids:
        caps.append(Capability(
            capability_id="gaia.tide.read@v1",
            description="Live tide-gauge relay — NOAA CO-OPS (MLLW metres) and/or UHSLC "
                        f"fast-delivery. Default device_id={tide_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, tide_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=100,
        ))

    river_ids = [
        d.device_id
        for d in runtime.fleet.devices()
        if isinstance(
            d,
            (
                USGSRiverGauge, EcccHydrometric, SmhiHydrology, PegelonlineRiver,
                EaHydrologyRiver, RwsRiver, SepaRiver, NrwRiver, HubEauRiver, EhydRiver,
                BafuRiver, LhmtRiver, LvgmcRiver, OpwRiver,
            ),
        )
    ]
    if river_ids:
        caps.append(Capability(
            capability_id="gaia.river.read@v1",
            description="Live river-gauge relay — USGS NWIS, ECCC hydrometric (End-use "
                        "Licence + attribution), SMHI hydroobs (CC BY 4.0), WSV "
                        "PEGELONLINE (DL-DE-Zero-2.0), EA Hydrology (OGL v3), "
                        "Rijkswaterstaat (CC0), SEPA KiWIS (OGL, Scotland), "
                        "NRW River Levels (OGL, Wales), Hub'Eau France (Etalab OL 2.0), "
                        "eHYD Austria (CC BY 4.0), BAFU/FOEN Switzerland "
                        "(OGD-CH Open-Use; geodetic water level, not USGS stage), "
                        "Lithuania LHMT lt-hydro-*-01 (CC BY-SA 4.0), Latvia LVGMC "
                        "lv-hydro-*-01 (CC0-1.0), and/or Ireland OPW ie-river-*-01 "
                        "(CC BY 4.0; stations >41000 excluded). "
                        "Discharge (m³/s) + stage when the "
                        f"upstream publishes it. Default device_id={river_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, river_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=120,
        ))
    reservoir_ids = [
        d.device_id for d in runtime.fleet.devices() if isinstance(d, UsaceReservoir)
    ]
    if reservoir_ids:
        caps.append(Capability(
            capability_id="gaia.reservoir.read@v1",
            description="Live USACE CWMS reservoir pool elevation and storage (U.S. public "
                        f"domain). Default device_id={reservoir_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, reservoir_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=180,
        ))
    if _has("epa-uv-01"):
        caps.append(Capability(
            capability_id="gaia.uv.read@v1",
            description="U.S. EPA Envirofacts hourly UV forecast cluster (U.S. public domain). "
                        "Forecast, not an in-situ pyranometer. device_id=epa-uv-01.",
            handler=_read_handler(runtime, "epa-uv-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=500,
        ))
    marine_ids = [
        d.device_id
        for d in runtime.fleet.devices()
        if isinstance(d, (NDBCBuoy, OpenMeteoMarine, CdipWave))
    ]
    if marine_ids:
        caps.append(Capability(
            capability_id="gaia.marine.read@v1",
            description="Live marine relay — wave height (m) from NOAA NDBC, Open-Meteo Marine, "
                        "and/or CDIP ERDDAP West Coast buoys (acknowledge CDIP/Scripps/USACE). "
                        f"Default device_id={marine_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, marine_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=120,
        ))
    ghg_ids = [d.device_id for d in runtime.fleet.devices() if isinstance(d, IcosGhg)]
    if ghg_ids:
        caps.append(Capability(
            capability_id="gaia.ghg.read@v1",
            description="Live atmospheric greenhouse-gas relay — ICOS ATC NRT CO₂ mole fraction "
                        f"(CC BY 4.0 — cite ICOS + object PID). Default device_id={ghg_ids[0]}. "
                        "Not PM2.5 / air-quality. Ed25519-attested.",
            handler=_read_handler(runtime, ghg_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=800,
        ))

    if _has("firms-fire-01"):
        caps.append(Capability(
            capability_id="gaia.fire.read@v1",
            description="Live NASA FIRMS VIIRS active-fire relay — attested headline is the "
                        "brightest non-low hotspot; reading.hotspots[] is the top-N cluster "
                        "from the same fetch (map layer). Optional buyer bbox "
                        "(west/south/east/north) + limit/max_total + page_size; resume with "
                        "cursor (idempotent). No client URLs. device_id=firms-fire-01. "
                        "Cite NASA FIRMS. Ed25519-attested.",
            handler=_fire_read_handler(runtime, "firms-fire-01"),
            product_id=product, input_schema=_FIRE_IN, output_schema=_FIRE_OUT,
            price_per_call_usd=0.002, p50_latency_ms=400,
        ))
    if _has("safecast-01"):
        caps.append(Capability(
            capability_id="gaia.radiation.read@v1",
            description="Live Safecast radiation relay — highest recent CPM near the operator "
                        "anchor (CC0). device_id=safecast-01. Ed25519-attested.",
            handler=_read_handler(runtime, "safecast-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    if _has("cybernews-jam-01"):
        caps.append(Capability(
            capability_id="gaia.jamming.read@v1",
            description="Live relay of ACTIVE/MONITORING records from the curated CyberNews "
                        "GNSS interference registry (CC BY 4.0 — attribution required). This "
                        "is source-attributed threat intelligence, not raw RF sensing or an "
                        "independently confirmed alert. device_id=cybernews-jam-01. "
                        "Ed25519-attested relay envelope.",
            handler=_read_handler(runtime, "cybernews-jam-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=150,
        ))
    if _has("gnss-euref-01") or _has("gnss-ga-01"):
        gnss_default = "gnss-euref-01" if _has("gnss-euref-01") else "gnss-ga-01"
        caps.append(Capability(
            capability_id="gaia.gnss.integrity.read@v1",
            description=(
                "Commercially-approved GNSS station inventories: EUREF EPN (CC BY 4.0) "
                "and Geoscience Australia (CC BY 3.0 AU), plus source-published EPN "
                "data-path availability/latency when present. Omit station_id for a "
                "network inventory; set device_id + station_id for one exact "
                "stations; set station_id (or use gnss-station:euref:{id}) for one exact "
                "station. Derived degradation means observation delivery degraded; cause "
                "is unestablished and this is not independent proof of RF jamming. "
                "EPN Central Bureau CC BY 4.0. Ed25519-attested relay envelope."
            ),
            handler=_gnss_integrity_handler(runtime, gnss_default),
            product_id=product, input_schema=_GNSS_IN, output_schema=_GNSS_OUT,
            price_per_call_usd=0.002, p50_latency_ms=350,
        ))

    if _has("eonet-01"):
        caps.append(Capability(
            capability_id="gaia.events.read@v1",
            description="Live NASA EONET open natural events (wildfire, volcano, storm, …). "
                        "Headline is the highest-scored geolocated event; hotspots[] is the "
                        "open-event cluster. Cite NASA EONET; no NASA endorsement. "
                        "device_id=eonet-01. Ed25519-attested.",
            handler=_read_handler(runtime, "eonet-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    if _has("swpc-01") or _has("swpc-solarwind-01") or _has("swpc-xray-01") or _has("donki-01"):
        default_spacewx = (
            "swpc-01" if _has("swpc-01")
            else (
                "swpc-solarwind-01" if _has("swpc-solarwind-01")
                else ("swpc-xray-01" if _has("swpc-xray-01") else "donki-01")
            )
        )
        caps.append(Capability(
            capability_id="gaia.spacewx.read@v1",
            description="Live space-weather relay — NOAA SWPC planetary Kp + OVATION aurora, "
                        "solar-wind / GOES X-ray summaries (U.S. PD), and/or NASA DONKI "
                        "notifications (open data — cite NASA/CCMC). Default "
                        f"device_id={default_spacewx}. Ed25519-attested.",
            handler=_read_handler(runtime, default_spacewx),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    if _has("glm-01"):
        caps.append(Capability(
            capability_id="gaia.lightning.read@v1",
            description="Live GOES-19/18 GLM lightning flashes from NOAA Open Data Dissemination "
                        "(U.S. public domain — not Blitzortung). device_id=glm-01. "
                        "Ed25519-attested.",
            handler=_read_handler(runtime, "glm-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=400,
        ))
    if _has("nws-alerts-01") or _has("naad-01"):
        default_alerts = "nws-alerts-01" if _has("nws-alerts-01") else "naad-01"
        caps.append(Capability(
            capability_id="gaia.alerts.read@v1",
            description="Live weather/public alerts — NWS CAP (U.S. PD) and/or Canada NAAD "
                        "Atom CAP-CP (cite issuing authority). Empty ≠ all-clear. Default "
                        f"device_id={default_alerts}. Ed25519-attested.",
            handler=_read_handler(runtime, default_alerts),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=250,
        ))
    if _has("argo-01"):
        caps.append(Capability(
            capability_id="gaia.argo.read@v1",
            description="Official GDAC global directory of active Argo floats (last "
                        "transmission within 30 days), or latest near-surface T/S/P for "
                        "a specific WMO via input.wmo. Unrestricted; cite DOI "
                        "10.17882/42182. device_id=argo-01. Ed25519-attested.",
            handler=_argo_read_handler(runtime, "argo-01"),
            product_id=product, input_schema=_ARGO_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=600,
        ))
    if _has("usgs-geomag-01"):
        caps.append(Capability(
            capability_id="gaia.geomag.read@v1",
            description="Live USGS geomagnetic-observatory network total field F (nT): all "
                        "14 official stations, with source coordinates and observation age. "
                        "U.S. public domain — not INTERMAGNET. Default device_id="
                        "usgs-geomag-01 (BOU); select another usgs-geomag-* fleet id. "
                        "Ed25519-attested.",
            handler=_read_handler(runtime, "usgs-geomag-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    flood_ids = [
        d.device_id for d in runtime.fleet.devices() if isinstance(d, (VicFlood, GfmFlood))
    ]
    if _has("nws-flood-01") or _has("ea-flood-01") or flood_ids:
        default_flood = (
            "nws-flood-01" if _has("nws-flood-01")
            else ("ea-flood-01" if _has("ea-flood-01") else flood_ids[0])
        )
        caps.append(Capability(
            capability_id="gaia.flood.read@v1",
            description="Live flood warning products: NWS CAP (U.S. PD), UK Environment "
                        "Agency OGL (England only — not SEPA/NRW), Vigicrues VIC "
                        "vic-*-01 (Etalab OL 2.0; WARNING, not Hub'Eau; empty ≠ all-clear), "
                        "and/or Copernicus GFM gfm-flood-01 (CC BY 4.0; needs "
                        "GAIA_GFM_TOKEN; not GloFAS WMS). Not an in-situ river gauge. "
                        f"Default device_id={default_flood}. Ed25519-attested.",
            handler=_read_handler(runtime, default_flood),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=250,
        ))
    if _has("effis-01"):
        caps.append(Capability(
            capability_id="gaia.effis.read@v1",
            description="Live Copernicus EFFIS current fires (CC BY 4.0 — cite Copernicus "
                        "EMS / JRC). device_id=effis-01. Ed25519-attested.",
            handler=_read_handler(runtime, "effis-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=300,
        ))
    if _has("usgs-volcano-01"):
        caps.append(Capability(
            capability_id="gaia.volcano.read@v1",
            description="Live USGS elevated volcanoes (alert / aviation color). U.S. public "
                        "domain. device_id=usgs-volcano-01. Ed25519-attested.",
            handler=_read_handler(runtime, "usgs-volcano-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    if _has("fintraffic-ais-01") or _has("kystverket-ais-01"):
        default_ais = "fintraffic-ais-01" if _has("fintraffic-ais-01") else "kystverket-ais-01"
        caps.append(Capability(
            capability_id="gaia.ais.public.read@v1",
            description="Live public AIS snapshot: Fintraffic Digitraffic (Finnish waters, "
                        "CC BY 4.0) and/or Kystverket via BarentsWatch (Norwegian waters, "
                        "NLOD 2.0). Geography-bound pins — not one Europe blob, not GFW, not "
                        f"own-edge gaia.ais.read@v1. Default device_id={default_ais}.",
            handler=_read_handler(runtime, default_ais),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=400,
        ))
    if _has("nws-tsunami-01") or _has("ptwc-01"):
        default_tsu = "nws-tsunami-01" if _has("nws-tsunami-01") else "ptwc-01"
        caps.append(Capability(
            capability_id="gaia.tsunami.read@v1",
            description="Live tsunami warning product: NWS CAP (U.S.) and/or PTWC Atom "
                        "(Pacific). Not a tide gauge. Empty feed → offline / no debit. "
                        f"Default device_id={default_tsu}. Ed25519-attested.",
            handler=_read_handler(runtime, default_tsu),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=250,
        ))
    if _has("nhc-cyclone-01") or _has("jma-typhoon-01"):
        default_cyclone = "nhc-cyclone-01" if _has("nhc-cyclone-01") else "jma-typhoon-01"
        caps.append(Capability(
            capability_id="gaia.cyclone.read@v1",
            description="Live tropical cyclone relay — NOAA NHC/CPHC CurrentStorms "
                        "(U.S. PD; Atlantic + East Pacific + Central Pacific; not JTWC) "
                        "and/or JMA typhoon jma-typhoon-01 (Public Data License; NW Pacific "
                        "only; cite JMA; not NHC/JTWC). Empty season → offline / no debit. "
                        f"Default device_id={default_cyclone}. Ed25519-attested.",
            handler=_read_handler(runtime, default_cyclone),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=200,
        ))
    aviation_ids = [
        d.device_id for d in runtime.fleet.devices() if isinstance(d, AviationMetar)
    ]
    if aviation_ids:
        caps.append(Capability(
            capability_id="gaia.aviation.read@v1",
            description="Live airport METAR mesh from NOAA Aviation Weather Center (U.S. public "
                        "domain). Click an airport pin for in-situ instruments — not an NWS land "
                        f"ASOS pin, not a TAF. Default device_id={aviation_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, aviation_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.001, p50_latency_ms=150,
        ))
    if _has("fintraffic-road-01"):
        caps.append(Capability(
            capability_id="gaia.road.read@v1",
            description="Live Fintraffic Digitraffic road-weather station cluster (CC BY 4.0 — "
                        "credit Fintraffic). Finnish roads only — not AIS, not EU traffic. "
                        "device_id=fintraffic-road-01. Ed25519-attested.",
            handler=_read_handler(runtime, "fintraffic-road-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=300,
        ))
    if _has("fintraffic-rail-01"):
        caps.append(Capability(
            capability_id="gaia.rail.read@v1",
            description="Live Fintraffic Digitraffic train-location cluster (CC BY 4.0 — credit "
                        "Fintraffic). Finnish rail only — not EU rail, not road. "
                        "device_id=fintraffic-rail-01. Ed25519-attested.",
            handler=_read_handler(runtime, "fintraffic-rail-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=300,
        ))
    if _has("usdm-01"):
        caps.append(Capability(
            capability_id="gaia.drought.read@v1",
            description="Live U.S. Drought Monitor weekly state statistics (open data — attribute "
                        "NDMC / USDA / NOAA / NASA). Classification product — not in-situ soil "
                        "moisture. device_id=usdm-01. Ed25519-attested.",
            handler=_read_handler(runtime, "usdm-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=400,
        ))
    if _has("hms-smoke-01"):
        caps.append(Capability(
            capability_id="gaia.smoke.read@v1",
            description="Live NOAA/NESDIS HMS qualitative smoke polygons (U.S. public domain). "
                        "Light/medium/heavy polygon centroids; not an in-situ PM2.5 sensor. "
                        "Empty pre-analysis product → offline / no debit. device_id=hms-smoke-01.",
            handler=_read_handler(runtime, "hms-smoke-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=250,
        ))
    if _has("usgs-wq-01"):
        caps.append(Capability(
            capability_id="gaia.water_quality.read@v1",
            description="Live USGS monitoring-locations registry joined to latest-continuous "
                        "(public domain): water temperature, pH, dissolved oxygen and specific "
                        "observations. Pass bbox and optional parameters/require_all; GAIA drains "
                        "every OGC page, keeps only fresh qualified stations (48h default; "
                        "max_age_hours is configurable), and "
                        "returns per-parameter approval_status, qualifiers and timestamps. One "
                        "signed row is one station coordinate. device_id=usgs-wq-01.",
            handler=_p4_geo_handler(runtime, "usgs-wq-01"),
            product_id=product, input_schema=_WATER_QUALITY_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=250,
        ))
    if _has("noaa-dart-01"):
        caps.append(Capability(
            capability_id="gaia.dart.read@v1",
            description="All active NOAA/NDBC DART deep-ocean water-column gauges (public domain; "
                        "gross-error checked real-time data). Gauge observation, not a tsunami "
                        "warning. Select a registered dart-* device; default device_id=noaa-dart-01.",
            handler=_read_handler(runtime, "noaa-dart-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=180,
        ))
    if _has("imerg-01"):
        caps.append(Capability(
            capability_id="gaia.precipitation.read@v1",
            description="NASA GPM IMERG Early Run V07 half-hour precipitation grid samples. "
                        "Pass latitude/longitude for any source cell; preliminary near-real-time "
                        "product. Free Earthdata token required. device_id=imerg-01.",
            handler=_p4_geo_handler(runtime, "imerg-01"),
            product_id=product, input_schema=_P4_GEO_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.003, p50_latency_ms=900,
        ))
    if _has("nexrad-status-01"):
        caps.append(Capability(
            capability_id="gaia.radar.status.read@v1",
            description="Live NOAA/NWS NEXRAD WSR-88D station health: one coordinate per "
                        "radar with Level-II latency, calibration correction, transmitter "
                        "power and status. Not reflectivity pixels. device_id=nexrad-status-01.",
            handler=_read_handler(runtime, "nexrad-status-01"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=300,
        ))
    if _has("cams-berlin"):
        caps.append(Capability(
            capability_id="gaia.atmosphere.read@v1",
            description="CAMS-derived aerosol optical depth, dust and pollen at configured "
                        "coordinates via commercially licensed/self-hosted Open-Meteo. One "
                        "signed point per returned source coordinate; arbitrary latitude/longitude "
                        "is accepted. Default device_id=cams-berlin.",
            handler=_p4_geo_handler(runtime, "cams-berlin"),
            product_id=product, input_schema=_P4_GEO_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=220,
        ))
    if _has("radnet-birmingham"):
        caps.append(Capability(
            capability_id="gaia.radnet.read@v1",
            description="All 140 U.S. EPA RadNet monitor coordinates with approved hourly "
                        "dose-equivalent rate and derived R02–R09 gamma channel total. "
                        "Default device_id=radnet-birmingham.",
            handler=_read_handler(runtime, "radnet-birmingham"),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=350,
        ))
    if _has("soil-berlin"):
        caps.append(Capability(
            capability_id="gaia.soil_moisture.read@v1",
            description="Copernicus CLMS global daily Soil Water Index SWI020 (%) at one "
                        "requested coordinate. Free CDSE OAuth client required; arbitrary "
                        "latitude/longitude is accepted. "
                        "Default device_id=soil-berlin.",
            handler=_p4_geo_handler(runtime, "soil-berlin"),
            product_id=product, input_schema=_P4_GEO_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.003, p50_latency_ms=700,
        ))
    if _has("solar-berlin"):
        caps.append(Capability(
            capability_id="gaia.solar.read@v1",
            description="NASA POWER daily all-sky and clear-sky surface irradiation at one "
                        "requested coordinate. The signed reading includes the "
                        "source observation date. Default device_id=solar-berlin.",
            handler=_p4_geo_handler(runtime, "solar-berlin"),
            product_id=product, input_schema=_P4_GEO_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=450,
        ))
    if _has("snow-rainier"):
        caps.append(Capability(
            capability_id="gaia.snow.read@v1",
            description="NOAA/NWS NOHRSC assimilated SNODAS snow depth and snow-water "
                        "equivalent. Any requested CONUS coordinate resolves to its exact "
                        "1-km model cell; not an "
                        "in-situ gauge. Default device_id=snow-rainier.",
            handler=_p4_geo_handler(runtime, "snow-rainier"),
            product_id=product, input_schema=_P4_GEO_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=400,
        ))
    if _has("nsidc-ice-01"):
        caps.append(Capability(
            capability_id="gaia.sea_ice.read@v1",
            description="Current NOAA/NSIDC Sea Ice Index v4 daily 25-km concentration "
                        "cells. Any requested Arctic coordinate is anchored to the sampled "
                        "EPSG:3411 cell centre; "
                        "not for navigation. device_id=nsidc-ice-01.",
            handler=_p4_geo_handler(runtime, "nsidc-ice-01"),
            product_id=product, input_schema=_P4_GEO_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.003, p50_latency_ms=650,
        ))
    if _has("lst-berlin"):
        caps.append(Capability(
            capability_id="gaia.land_temperature.read@v1",
            description="Copernicus Sentinel-3 SLSTR Level-2 1-km land-surface temperature "
                        "and retrieval uncertainty at any requested coordinate; "
                        "CDSE OAuth required. Default device_id=lst-berlin.",
            handler=_p4_geo_handler(runtime, "lst-berlin"),
            product_id=product, input_schema=_P4_GEO_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.003, p50_latency_ms=750,
        ))
    from gaia.devices.live_p3 import AdsbLolTraffic

    adsb_public_ids = [
        d.device_id for d in runtime.fleet.devices() if isinstance(d, AdsbLolTraffic)
    ]
    if adsb_public_ids:
        caps.append(Capability(
            capability_id="gaia.adsb.public.read@v1",
            description="Live ADSB.lol area snapshot (ODbL 1.0 — cite ADSB.lol; share-alike "
                        "applies to a derived public database). Operator-anchored lat/lon. "
                        "Not own-edge gaia.adsb.read@v1, not OpenSky, not ADSBx. Default "
                        f"device_id={adsb_public_ids[0]}. Ed25519-attested.",
            handler=_read_handler(runtime, adsb_public_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=300,
        ))

    from gaia.devices.feeder import FeederDevice

    adsb_ids = [
        d.device_id for d in runtime.fleet.devices()
        if isinstance(d, FeederDevice) and d.kind == "adsb"
    ]
    ais_ids = [
        d.device_id for d in runtime.fleet.devices()
        if isinstance(d, FeederDevice) and d.kind == "ais"
    ]
    iot_ids = [
        d.device_id for d in runtime.fleet.devices()
        if isinstance(d, FeederDevice) and d.kind == "iot"
    ]
    if adsb_ids:
        caps.append(Capability(
            capability_id="gaia.adsb.read@v1",
            description="Own-edge ADS-B feeder reading (operator dump1090 push). Offline until "
                        f"ingest. Default device_id={adsb_ids[0]}. Not a third-party aggregator. "
                        "Ed25519-attested.",
            handler=_read_handler(runtime, adsb_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=20,
        ))
    if ais_ids:
        caps.append(Capability(
            capability_id="gaia.ais.read@v1",
            description="Own-edge AIS feeder reading (operator AIS receiver push). Offline until "
                        f"ingest. Default device_id={ais_ids[0]}. Not a third-party aggregator. "
                        "Ed25519-attested.",
            handler=_read_handler(runtime, ais_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=20,
        ))
    if iot_ids:
        caps.append(Capability(
            capability_id="gaia.iot.read@v1",
            description="Own-edge IoT feeder (Tasmota / TTN / SenML-like T/RH/P/PM2.5). Offline "
                        f"until ingest. Default device_id={iot_ids[0]}. Not a third-party "
                        "aggregator. Ed25519-attested.",
            handler=_read_handler(runtime, iot_ids[0]),
            product_id=product, input_schema=_DEVICE_IN, output_schema=_READING_OUT,
            price_per_call_usd=0.002, p50_latency_ms=20,
        ))

    caps.extend([
        Capability(
            capability_id="gaia.window@v1",
            description="Bundle of N attested readings from one device in a single invoke "
                        "(micro-billing: clears the hub's 1-cent ledger quantum and the "
                        "Pay-on-Verified price floor).",
            handler=_window_handler(runtime),
            product_id=product,
            input_schema={"type": "object", "properties": {
                "device_id": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 500},
            }},
            output_schema={"type": "object", "properties": {
                "device_id": {"type": "string"}, "count": {"type": "integer"},
                "readings": {"type": "array"},
            }},
            price_per_call_usd=0.05, p50_latency_ms=60,
        ),
        Capability(
            capability_id="gaia.verify@v1",
            description="Statistical plausibility verdict over a GAIA reading "
                        "(bounds, z-score, rate, sibling agreement, attestation) — "
                        "the same math the /v1/verify escrow endpoint serves.",
            handler=_verify_handler(runtime),
            product_id=product,
            input_schema={"type": "object", "properties": {
                "reading": {"type": "object"},
                "attestation": {"type": "object"},
                "min_verify_score": {"type": "number", "minimum": 0, "maximum": 1},
            }, "required": ["reading"]},
            output_schema={"type": "object", "properties": {
                "verified": {"type": "boolean"}, "score": {"type": "number"},
                "summary": {"type": "string"}, "checks": {"type": "array"},
            }},
            price_per_call_usd=0.002, p50_latency_ms=5,
        ),
        Capability(
            capability_id="gaia.fleet.status@v1",
            description="Device registry: models, sites, pinned device pubkeys, fault state.",
            handler=_status_handler(runtime),
            product_id=product,
            output_schema={"type": "object", "properties": {
                "devices": {"type": "array"}, "count": {"type": "integer"},
            }},
            price_per_call_usd=0.0, p50_latency_ms=5,
        ),
    ])
    return OracleSpec(
        name="GAIA — physical-world oracle gateway",
        product_id=product,
        description="Virtual IoT devices (weather ×2, air quality, energy) plus live "
                    "public-API relays (Open-Meteo, NWS, UK carbon, USGS, NOAA tides/rivers/"
                    "marine, NASA FIRMS/EONET, SWPC, GOES GLM, NWS CAP, Safecast, CyberNews, "
                    "Sensor.Community, CWOP, Argo, MET Norway, USGS geomag, Copernicus EFFIS, "
                    "optional own-edge ADS-B/AIS/IoT feeders, NHC cyclones, public ADS-B) "
                    "sold as signed AIMarket capabilities, with a Metis-envelope statistical "
                    "verifier for Pay-on-Verified escrow.",
        public_url=url,
        categories=["iot", "sensors", "physical-data", "verification", "weather",
                    "air-quality", "energy", "seismic", "tides", "fire", "radiation",
                    "gnss", "traffic", "space-weather", "lightning", "alerts", "ocean",
                    "geomagnetism", "flood", "volcano", "cyclone", "adsb",
                    "aviation", "road", "rail", "drought", "reservoir", "uv"],
        capabilities=caps,
        signing_key_path=os.environ.get("GAIA_SIGNING_KEY_PATH", "data/gaia_signing_key"),
        version="0.1.0",
        related=["aimarket-hub", "metis", "oracle-family"],
    )
