"""Open pollen, wave-buoy and volcano observations."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.live_water_ext import _utc_time
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

log = logging.getLogger(__name__)

METEOSWISS_POLLEN_STATIONS = {
    "pbe": (46.950342, 7.424661, "Bern"),
    "pbs": (47.5618, 7.583931, "Basel"),
    "pbu": (47.173267, 9.472614, "Buchs (SG)"),
    "pcf": (47.113514, 6.832, "La Chaux-de-Fonds"),
    "pds": (46.829092, 9.855489, "Davos / Wolfgang"),
    "pge": (46.191969, 6.147544, "Genève"),
    "plo": (46.172547, 8.787389, "Locarno / Monti"),
    "pls": (46.524103, 6.644825, "Lausanne"),
    "plu": (46.004231, 8.960631, "Lugano"),
    "plz": (47.057678, 8.296803, "Luzern"),
    "pmu": (47.630206, 9.236878, "Münsterlingen"),
    "pne": (47.000269, 6.949828, "Neuchâtel"),
    "ppy": (46.813403, 6.942939, "Payerne"),
    "psn": (46.235403, 7.384606, "Sion"),
    "pzh": (47.378225, 8.565644, "Zürich"),
}

IRISH_WEATHER_BUOYS = {
    "M2": (53.4836, -5.4302, "M2"),
    "M3": (51.215956, -10.548261, "M3"),
    "M5": (51.690425, -6.704336, "M5"),
    "M6": (53.07482, -15.88135, "M6"),
}

AODN_WAVE_BUOYS = {
    "APOLLO-BAY": (-38.75393, 143.72365, "Apollo Bay"),
    "WILSONS-PROM": (-39.54340, 146.46467, "Wilsons Prom"),
    "WOOLI": (-29.85698, 153.31385, "Wooli"),
}

_OBS_MAX_AGE = timedelta(hours=2)
_STATION_FIELDS = {"observation_unix": "s", "latitude": "deg", "longitude": "deg"}
_WAVE_FIELDS = {"wave_height_m": "m", "wave_period_s": "s", "wave_direction_deg": "deg", **_STATION_FIELDS}
_POLLEN_COLUMNS = {
    "kabetuh0": "birch_pollen_grains_m3",
    "khpoach0": "grass_pollen_grains_m3",
    "kaalnuh0": "alder_pollen_grains_m3",
    "kacoryh0": "hazel_pollen_grains_m3",
    "kafaguh0": "beech_pollen_grains_m3",
    "kafraxh0": "ash_pollen_grains_m3",
    "kaquerh0": "oak_pollen_grains_m3",
}
_POLLEN_ROOT = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-pollen"
_IRISH_CSV = "https://erddap.marine.ie/erddap/tabledap/IWBNetwork.csv"
_IRISH_COLUMNS = (
    "station_id", "longitude", "latitude", "time", "WaveHeight", "WavePeriod",
    "MeanWaveDirection", "SeaTemperature", "WindSpeed", "WindDirection", "QC_Flag",
)
_IRISH_UNITS = [
    "", "degrees_east", "degrees_north", "UTC", "meters", "seconds",
    "degrees_true", "degrees_C", "knots", "degrees true", "",
]
_IRISH_LOCK = threading.Lock()
_IRISH_CACHE: tuple[float, str] | None = None
_AODN_ROOT = (
    "https://thredds.aodn.org.au/thredds/dodsC/IMOS/"
    "COASTAL-WAVE-BUOYS/WAVE-BUOYS/REALTIME/WAVE-PARAMETERS/"
)
_AODN_COLUMNS = ("TIME", "LATITUDE", "LONGITUDE", "WSSH", "WPPE", "WPDI", "WAVE_quality_control")
_AODN_EPOCH = datetime(1950, 1, 1, tzinfo=UTC)
_AODN_CREDIT = (
    "Data was sourced from Australia’s Integrated Marine Observing System (IMOS) - "
    "IMOS is enabled by the National Collaborative Research Infrastructure Strategy (NCRIS)"
)
_GEONET_CAMERAS_ROOT = "https://images.geonet.org.nz/volcano/cameras/"
_GEONET_CAMERAS_URL = _GEONET_CAMERAS_ROOT + "all.json"
_GEONET_VALIDITY = timedelta(minutes=30)
_GEONET_CAMERAS_AGE = timedelta(minutes=30)
_GEONET_CAMERA_ID = re.compile(r"[a-z0-9]{1,48}")
_GEONET_LOCAL_STAMP = re.compile(r"(\d{1,2}:\d{2} [ap]m) \((NZDT|NZST)\) (\d{1,2} [A-Z][a-z]{2} \d{4})")
_GEONET_AVIATION_COLORS = {"Green", "Yellow", "Orange", "Red", "Unassigned"}


def _bounded_num(value: Any, lo: float, hi: float) -> float | None:
    number = _num(value)
    return number if number is not None and lo <= number <= hi else None


def _remember_observation(
    samples: dict[datetime, dict[str, float]], stamp: datetime, values: dict[str, float], device_id: str,
) -> None:
    if samples.setdefault(stamp, values) != values:
        raise DeviceOffline(f"{device_id}: conflicting upstream observations at {stamp.isoformat()}")


def _latest(samples: dict[datetime, dict[str, float]], device_id: str) -> dict[str, float]:
    if not samples:
        raise DeviceOffline(f"{device_id}: no usable observation in the past 2 hours")
    observed = max(samples)
    return {**samples[observed], "observation_unix": observed.timestamp()}


class MeteoSwissPollen(LiveDevice):
    """Hourly in-situ pollen concentrations in grains per cubic metre."""

    model = "GAIA-POLLEN (MeteoSwiss)"
    policy_id = "meteoswiss_pollen"
    fields = {**{key: "grains/m3" for key in _POLLEN_COLUMNS.values()}, **_STATION_FIELDS}

    def __init__(self, device_id: str, clock: SimClock, *, station_code: str, **kwargs: Any):
        if station_code not in METEOSWISS_POLLEN_STATIONS:
            raise ValueError("unsupported MeteoSwiss pollen station")
        self.station_code = station_code
        self.latitude, self.longitude, _ = METEOSWISS_POLLEN_STATIONS[station_code]
        self.url = f"{_POLLEN_ROOT}/{station_code}/ogd-pollen_{station_code}_h_now.csv"
        self.source = f"{self.url} (Source: MeteoSwiss. CC BY 4.0. Hourly observed pollen concentrations.)"
        require_approved_source(self.policy_id).require_endpoint(self.url)
        super().__init__(device_id, clock, **kwargs)

    def sample(self) -> dict[str, float]:
        data = self._fetch_bytes(self.url, max_bytes=64_000)
        now = datetime.now(UTC)
        reader = csv.DictReader(io.StringIO(data.decode("cp1252")), delimiter=";")
        header = reader.fieldnames or []
        required = {"station_abbr", "reference_timestamp", *_POLLEN_COLUMNS}
        if not required <= set(header) or len(header) != len(set(header)):
            raise DeviceOffline(f"{self.device_id}: unrecognized MeteoSwiss hourly CSV")
        samples: dict[datetime, dict[str, float]] = {}
        for row in reader:
            if row.get("station_abbr") != self.station_code.upper() or None in row:
                raise DeviceOffline(f"{self.device_id}: malformed or different MeteoSwiss station")
            try:
                stamp = datetime.strptime(row.get("reference_timestamp", ""), "%d.%m.%Y %H:%M").replace(tzinfo=UTC)
            except (TypeError, ValueError):
                continue
            if not timedelta(0) <= now - stamp < _OBS_MAX_AGE:
                continue
            values = {
                field: count for column, field in _POLLEN_COLUMNS.items()
                if (count := _bounded_num(row.get(column), 0, 100_000)) is not None
            }
            if values:
                _remember_observation(samples, stamp, values, self.device_id)
        return {**_latest(samples, self.device_id), "latitude": self.latitude, "longitude": self.longitude}


class IrishWeatherBuoy(LiveDevice):
    """A selected moored station of the Irish Weather Buoy Network."""

    model = "GAIA-WAVE-BUOY (Irish Marine Institute)"
    policy_id = "irish_weather_buoys"
    url = _IRISH_CSV
    fields = {
        **_WAVE_FIELDS, "sea_temperature_c": "cel", "wind_mps": "m/s",
        "wind_direction_deg": "deg", "wave_qc_flag": "code",
    }
    source = (
        f"{url} (Marine Institute and Met Éireann, Irish Weather Buoy Network, CC BY 4.0. "
        "Preliminary hourly observations. Quality flag 0 is unknown and 1 is good.)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, station_code: str, **kwargs: Any):
        if station_code not in IRISH_WEATHER_BUOYS:
            raise ValueError("unsupported Irish weather buoy")
        self.station_code = station_code
        self.latitude, self.longitude, _ = IRISH_WEATHER_BUOYS[station_code]
        require_approved_source(self.policy_id).require_endpoint(self.url)
        super().__init__(device_id, clock, **kwargs)

    def _snapshot(self, now: datetime) -> str:
        global _IRISH_CACHE
        with _IRISH_LOCK:
            if _IRISH_CACHE is not None and time.monotonic() - _IRISH_CACHE[0] < 60:
                return _IRISH_CACHE[1]
            since = (now - _OBS_MAX_AGE).strftime("%Y-%m-%dT%H:%M:%SZ")
            stations = "|".join(IRISH_WEATHER_BUOYS)
            constraint = f'time>={since}&station_id=~"{stations}"'
            url = f"{self.url}?{','.join(_IRISH_COLUMNS)}&{quote(constraint, safe='&:')}"
            raw = self._fetch_bytes(url, max_bytes=32_000).decode("utf-8-sig")
            _IRISH_CACHE = time.monotonic(), raw
            return raw

    def sample(self) -> dict[str, float]:
        now = datetime.now(UTC)
        rows = csv.reader(io.StringIO(self._snapshot(now)))
        if tuple(next(rows, ())) != _IRISH_COLUMNS or next(rows, None) != _IRISH_UNITS:
            raise DeviceOffline(f"{self.device_id}: unexpected Marine Institute columns or units")
        samples: dict[datetime, dict[str, float]] = {}
        for cells in rows:
            if len(cells) != len(_IRISH_COLUMNS):
                raise DeviceOffline(f"{self.device_id}: malformed Marine Institute CSV")
            row = dict(zip(_IRISH_COLUMNS, cells))
            if row["station_id"] != self.station_code:
                continue
            stamp = _utc_time(row["time"])
            if stamp is None or not timedelta(0) <= now - stamp < _OBS_MAX_AGE or row["QC_Flag"] not in {"0", "1"}:
                continue
            height = _bounded_num(row["WaveHeight"], 0, 30)
            lat = _bounded_num(row["latitude"], self.latitude - 0.1, self.latitude + 0.1)
            lon = _bounded_num(row["longitude"], self.longitude - 0.1, self.longitude + 0.1)
            if height is None or lat is None or lon is None:
                continue
            values = {"wave_height_m": height, "latitude": lat, "longitude": lon, "wave_qc_flag": float(row["QC_Flag"])}
            for field, column, lo, hi, factor in (
                ("wave_period_s", "WavePeriod", 0, 40, 1),
                ("wave_direction_deg", "MeanWaveDirection", 0, 360, 1),
                ("sea_temperature_c", "SeaTemperature", -2, 45, 1),
                ("wind_mps", "WindSpeed", 0, 200, 1852 / 3600),
                ("wind_direction_deg", "WindDirection", 0, 360, 1),
            ):
                number = _bounded_num(row[column], lo, hi)
                if number is not None:
                    values[field] = number * factor
            _remember_observation(samples, stamp, values, self.device_id)
        return _latest(samples, self.device_id)


class AodnWaveBuoy(LiveDevice):
    """IMOS buoy observations from the official monthly OPeNDAP product."""

    model = "GAIA-WAVE-BUOY (Australian IMOS)"
    policy_id = "aodn_wave_buoys"
    url = _AODN_ROOT
    fields = {**_WAVE_FIELDS, "wave_qc_flag": "code"}
    source = (
        f"{url} ({_AODN_CREDIT}. CC BY 4.0. Real-time wave observations via AODN, "
        "source quality flag 1. Direction is from true north at the spectral peak.)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, station_code: str, **kwargs: Any):
        if station_code not in AODN_WAVE_BUOYS:
            raise ValueError("unsupported AODN wave buoy")
        self.station_code = station_code
        self.latitude, self.longitude, _ = AODN_WAVE_BUOYS[station_code]
        require_approved_source(self.policy_id).require_endpoint(self.url)
        super().__init__(device_id, clock, **kwargs)

    def _monthly(self, month: datetime) -> dict[str, list[str]]:
        station = self.station_code
        path = f"{station}/{month:%Y}/IMOS_COASTAL-WAVE-BUOYS_{month:%Y%m}01_{station}_RT_WAVE-PARAMETERS_monthly.nc.ascii"
        raw = self._fetch_bytes(f"{self.url}{path}?{','.join(_AODN_COLUMNS)}", max_bytes=550_000).decode("ascii")
        header, separator, data = raw.partition("---------------------------------------------")
        if not separator or f"}} IMOS/COASTAL-WAVE-BUOYS/WAVE-BUOYS/REALTIME/WAVE-PARAMETERS/{path[:-6]};" not in header:
            raise DeviceOffline(f"{self.device_id}: unexpected AODN OPeNDAP product")
        arrays: dict[str, list[str]] = {}
        for block in re.split(r"\n\s*\n", data.strip()):
            name, linebreak, values = block.partition("\n")
            found = re.fullmatch(r"([A-Z_a-z]+)\[(\d{1,5})\]", name.strip())
            if not linebreak or found is None:
                raise DeviceOffline(f"{self.device_id}: malformed AODN OPeNDAP array")
            field, count = found.groups()
            elements = [element.strip() for element in values.strip().split(",")]
            if field not in _AODN_COLUMNS or field in arrays or int(count) != len(elements):
                raise DeviceOffline(f"{self.device_id}: inconsistent AODN OPeNDAP arrays")
            arrays[field] = elements
        if set(arrays) != set(_AODN_COLUMNS) or len({len(a) for a in arrays.values()}) != 1:
            raise DeviceOffline(f"{self.device_id}: missing or misaligned AODN OPeNDAP arrays")
        return arrays

    def sample(self) -> dict[str, float]:
        now = datetime.now(UTC)
        previous = now - _OBS_MAX_AGE
        months = [now] if previous.month == now.month else [now, previous]
        samples: dict[datetime, dict[str, float]] = {}
        for month in months:
            try:
                arrays = self._monthly(month)
            except DeviceOffline:
                if len(months) == 1:
                    raise
                continue
            for days, lat_raw, lon_raw, height_raw, period_raw, dir_raw, qc_raw in zip(
                *(arrays[field] for field in _AODN_COLUMNS), strict=True,
            ):
                day = _bounded_num(days, 0, 100_000)
                if day is None or _num(qc_raw) != 1:
                    continue
                stamp = _AODN_EPOCH + timedelta(seconds=round(day * 86_400))
                if not timedelta(0) <= now - stamp < _OBS_MAX_AGE:
                    continue
                height = _bounded_num(height_raw, 0, 30)
                lat = _bounded_num(lat_raw, self.latitude - 0.1, self.latitude + 0.1)
                lon = _bounded_num(lon_raw, self.longitude - 0.1, self.longitude + 0.1)
                if height is None or lat is None or lon is None:
                    continue
                values = {"wave_height_m": height, "latitude": lat, "longitude": lon, "wave_qc_flag": 1.0}
                period = _bounded_num(period_raw, 0, 40)
                direction = _bounded_num(dir_raw, 0, 360)
                if period is not None:
                    values["wave_period_s"] = period
                if direction is not None:
                    values["wave_direction_deg"] = direction
                _remember_observation(samples, stamp, values, self.device_id)
            if samples:
                break
        return _latest(samples, self.device_id)


def _camera_stamp(value: Any) -> datetime | None:
    match = _GEONET_LOCAL_STAMP.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None
    hour, zone, day = match.groups()
    offset = timezone(timedelta(hours=13 if zone == "NZDT" else 12))
    try:
        return datetime.strptime(f"{day} {hour.upper()}", "%d %b %Y %I:%M %p").replace(tzinfo=offset).astimezone(UTC)
    except ValueError:
        return None


def _point(feature: dict[str, Any], *, lat_first: bool = False) -> tuple[float, float] | None:
    geometry = feature.get("geometry")
    coords = geometry.get("coordinates") if isinstance(geometry, dict) and geometry.get("type") == "Point" else None
    if not isinstance(coords, list) or len(coords) != 2:
        return None
    lat_raw, lon_raw = coords if lat_first else reversed(coords)
    lat, lon = _bounded_num(lat_raw, -90, 90), _bounded_num(lon_raw, -180, 180)
    return (lat, lon) if lat is not None and lon is not None else None


class GeonetVolcano(LiveDevice):
    """Current New Zealand volcano alert levels and independently timed camera links."""

    model = "GAIA-VOLCANO (GeoNet New Zealand)"
    policy_id = "geonet_volcano"
    url = "https://api.geonet.org.nz/volcano/val"
    headers = {"Accept": "application/vnd.geo+json;version=2", "Cache-Control": "no-cache"}
    fields = {
        "latitude": "deg", "longitude": "deg", "volcanic_alert_level": "code",
        "source_retrieved_unix": "s", "source_expires_unix": "s",
    }
    retain_hotspots_in_history = False
    source = (
        f"{url} (Earth Sciences New Zealand, GeoNet and programme sponsors NHC, LINZ, NEMA and MBIE. "
        "CC BY 3.0 NZ. Current published alert level, checked at retrieval time. "
        "Camera images: https://doi.org/10.21420/vq6s-w222.)"
    )

    def __init__(self, device_id: str, clock: SimClock, **kwargs: Any):
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(self.url)
        policy.require_endpoint(_GEONET_CAMERAS_URL)
        super().__init__(device_id, clock, **kwargs)

    def _cameras(self, now: datetime) -> dict[str, list[dict[str, Any]]]:
        cameras: dict[str, dict[str, dict[str, Any]]] = {}
        data = json.loads(self._fetch_bytes(_GEONET_CAMERAS_URL, max_bytes=64_000))
        if not isinstance(data, list) or len(data) > 32:
            raise DeviceOffline(f"{self.device_id}: unexpected GeoNet camera inventory")
        for collection in data:
            features = collection.get("features") if isinstance(collection, dict) else None
            if not isinstance(features, list) or len(features) > 32:
                raise DeviceOffline(f"{self.device_id}: unexpected GeoNet camera collection")
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                cid, props, volcanoes = feature.get("id"), feature.get("properties"), feature.get("volcano-id")
                coords = _point(feature, lat_first=True)
                if (not isinstance(cid, str) or _GEONET_CAMERA_ID.fullmatch(cid) is None
                        or not isinstance(props, dict) or not coords or not isinstance(volcanoes, list)):
                    continue
                stamp = _camera_stamp(props.get("latest-timestamp"))
                title = props.get("title")
                relative = f"latest/t-{cid}.jpg"
                if (stamp is None or not timedelta(0) <= now - stamp < _GEONET_CAMERAS_AGE
                        or props.get("latest-image-thumb") != relative or not isinstance(title, str) or not title):
                    continue
                camera = {
                    "camera_id": cid, "latitude": coords[0], "longitude": coords[1], "caption": title[:160],
                    "observed_at": stamp.isoformat(), "observation_unix": stamp.timestamp(),
                    "url": f"{_GEONET_CAMERAS_ROOT}{relative}?v={int(stamp.timestamp())}",
                }
                for vid in volcanoes:
                    if isinstance(vid, str) and _GEONET_CAMERA_ID.fullmatch(vid):
                        cameras.setdefault(vid, {})[cid] = camera
        return {vid: [entries[cid] for cid in sorted(entries)] for vid, entries in cameras.items()}

    def read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._fetch_bytes(self.url, max_bytes=80_000))
        except (ValueError, UnicodeError) as exc:
            raise DeviceOffline(f"{self.device_id}: invalid GeoNet alert snapshot") from exc
        features = payload.get("features") if isinstance(payload, dict) and payload.get("type") == "FeatureCollection" else None
        if not isinstance(features, list) or not 1 <= len(features) <= 100:
            raise DeviceOffline(f"{self.device_id}: unexpected GeoNet alert inventory")
        retrieved = datetime.now(UTC)
        expires = retrieved + _GEONET_VALIDITY
        timing = {
            "source_retrieved_unix": retrieved.timestamp(), "source_expires_unix": expires.timestamp(),
            "source_retrieved_at": retrieved.isoformat(), "valid_until": expires.isoformat(),
        }
        try:
            cameras = self._cameras(retrieved)
        except (DeviceOffline, ValueError) as exc:
            log.warning("%s: GeoNet camera inventory unavailable: %s", self.device_id, exc)
            cameras = {}

        rows: dict[str, dict[str, Any]] = {}
        for feature in features:
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict):
                raise DeviceOffline(f"{self.device_id}: malformed GeoNet alert")
            coords = _point(feature)
            vid, title, level, color = (props.get(key) for key in ("volcanoID", "volcanoTitle", "level", "acc"))
            if (coords is None or not isinstance(vid, str) or _GEONET_CAMERA_ID.fullmatch(vid) is None
                    or not isinstance(title, str) or not title or not isinstance(level, int) or isinstance(level, bool)
                    or not 0 <= level <= 5 or not isinstance(color, str) or color not in _GEONET_AVIATION_COLORS
                    or vid in rows):
                raise DeviceOffline(f"{self.device_id}: invalid or duplicate GeoNet alert")
            rows[vid] = {
                **timing, "volcano_id": vid, "title": title[:160], "latitude": coords[0], "longitude": coords[1],
                "volcanic_alert_level": float(level), "aviation_color_code": color,
                "cameras": cameras.get(vid, []),
            }
        ordered = sorted(rows.values(), key=lambda row: (-row["volcanic_alert_level"], row["volcano_id"]))
        return signed_cluster_read(
            self, ordered, numeric_keys=tuple(self.fields),
            meta_keys=("volcano_id", "title", "aviation_color_code", "source_retrieved_at", "valid_until"),
            structured_keys=("cameras",),
            reading_meta={"source_read_at": retrieved.isoformat(), "source_expires_at": expires.isoformat()},
        )
