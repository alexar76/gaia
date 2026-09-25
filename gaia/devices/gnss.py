"""Commercially clean GNSS station integrity relay.

The first production adapter uses the official EUREF Permanent Network station
list.  A station position is an inventory fact.  Availability/latency, when
the source publishes them, describe the observation delivery path — they do
*not* by themselves prove radio-frequency jamming.  The reading keeps that
boundary explicit in ``claim_class`` and ``cause``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from gaia.attestation import sign_reading
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _num
from gaia.source_policy import require_approved_source


_SAFE_STATION_ID = re.compile(r"^[A-Z0-9]{4}(?:[A-Z0-9]{5})?$")
_SPACE = re.compile(r"\s+")


class _TableParser(HTMLParser):
    """Tiny dependency-free HTML table extractor, tolerant of DataTables markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, list[list[str]]] = {}
        self._table = ""
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if tag == "table":
            if self._depth == 0:
                self._table = str(attrs_d.get("id") or attrs_d.get("class") or f"table{len(self.tables)}")
                self.tables.setdefault(self._table, [])
            self._depth += 1
        elif self._depth and tag == "tr":
            self._row = []
        elif self._depth and tag in ("th", "td") and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._depth and tag in ("th", "td") and self._cell is not None:
            text = _SPACE.sub(" ", "".join(self._cell)).strip()
            if self._row is not None:
                self._row.append(text)
            self._cell = None
        elif self._depth and tag == "tr" and self._row is not None:
            if any(self._row):
                self.tables.setdefault(self._table, []).append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self._table = ""


def _header_index(row: list[str], names: tuple[str, ...]) -> int | None:
    for i, value in enumerate(row):
        norm = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        words = set(norm.split())
        for name in names:
            wanted = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
            if (" " in wanted and wanted in norm) or (" " not in wanted and wanted in words):
                return i
    return None


def _pct_values(cells: list[str]) -> list[float]:
    out: list[float] = []
    for cell in cells:
        for raw in re.findall(r"(-?\d+(?:\.\d+)?)\s*%", cell):
            value = float(raw)
            if 0 <= value <= 100:
                out.append(value)
    return out


def _latency_values(cells: list[str]) -> list[float]:
    out: list[float] = []
    for cell in cells:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|min|mins|h|hr|hrs)\s*", cell, re.I)
        if not match:
            continue
        value, unit = float(match.group(1)), match.group(2).lower()
        if unit == "ms":
            value /= 1000.0
        elif unit.startswith("min"):
            value *= 60.0
        elif unit.startswith("h"):
            value *= 3600.0
        if 0 <= value <= 31_536_000:
            out.append(value)
    return out


def _station_id(cells: list[str]) -> str:
    for cell in cells[:4]:
        token = re.sub(r"[^A-Za-z0-9]", "", cell).upper()
        if _SAFE_STATION_ID.fullmatch(token):
            return token
    return ""


def _metric_rows(tables: dict[str, list[list[str]]], needle: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for table_id, table in tables.items():
        key = table_id.lower()
        if needle not in key and not any(needle in " ".join(row).lower() for row in table[:3]):
            continue
        for row in table:
            sid = _station_id(row)
            if sid:
                rows[sid] = row
    return rows


def parse_euref_station_html(html: str) -> list[dict[str, Any]]:
    """Parse official EPN overview + optional availability/latency tables."""
    parser = _TableParser()
    parser.feed(html)
    availability = _metric_rows(parser.tables, "availability")
    # Official page uses tableDA/tableDL ids; keep those stable abbreviations too.
    for key, table in parser.tables.items():
        if key.lower() == "tableda":
            for row in table:
                sid = _station_id(row)
                if sid:
                    availability[sid] = row
    # Do not mix daily/hourly/real-time latency: their healthy baselines differ
    # by orders of magnitude. Only a column explicitly labelled real-time is
    # eligible for the near-live degradation score.
    latency: dict[str, float] = {}
    for key, table in parser.tables.items():
        if key.lower() != "tabledl" and "latency" not in " ".join(" ".join(r) for r in table[:3]).lower():
            continue
        header_i = next(
            (i for i, row in enumerate(table[:5]) if _header_index(row, ("real time", "realtime", "rt latency")) is not None),
            None,
        )
        if header_i is None:
            continue
        value_i = _header_index(table[header_i], ("real time", "realtime", "rt latency"))
        if value_i is None:
            continue
        for cells in table[header_i + 1 :]:
            sid = _station_id(cells)
            if not sid or value_i >= len(cells):
                continue
            vals = _latency_values([cells[value_i]])
            if vals:
                latency[sid] = vals[0]

    candidates = sorted(parser.tables.items(), key=lambda item: ("overview" not in item[0].lower(), -len(item[1])))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _table_id, rows in candidates:
        if not rows:
            continue
        header_i = next((i for i, row in enumerate(rows[:5]) if _header_index(row, ("lat", "latitude")) is not None and _header_index(row, ("lon", "longitude")) is not None), None)
        if header_i is None:
            continue
        header = rows[header_i]
        lat_i = _header_index(header, ("latitude", "lat"))
        lon_i = _header_index(header, ("longitude", "lon", "long"))
        name_i = _header_index(header, ("site name", "station name", "name"))
        country_i = _header_index(header, ("country",))
        status_i = _header_index(header, ("status", "operational"))
        if lat_i is None or lon_i is None:
            continue
        for cells in rows[header_i + 1 :]:
            sid = _station_id(cells)
            if not sid or sid in seen or max(lat_i, lon_i) >= len(cells):
                continue
            lat, lon = _num(cells[lat_i]), _num(cells[lon_i])
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            av = _pct_values(availability.get(sid, []))
            delay = latency.get(sid)
            row: dict[str, Any] = {
                "station_id": sid,
                "latitude": round(float(lat), 6),
                "longitude": round(float(lon), 6),
                "network": "EUREF EPN",
            }
            if name_i is not None and name_i < len(cells) and cells[name_i]:
                row["name"] = cells[name_i][:160]
            if country_i is not None and country_i < len(cells) and cells[country_i]:
                row["country"] = cells[country_i][:80]
            if status_i is not None and status_i < len(cells) and cells[status_i]:
                row["source_status"] = cells[status_i][:80]
            if av:
                # Conservative across products: the weakest published recent channel.
                row["availability_pct"] = round(min(av), 3)
            if delay is not None:
                row["latency_s"] = round(float(delay), 3)
            out.append(row)
            seen.add(sid)
        if out:
            break
    if not out:
        raise DeviceOffline("EUREF station page contained no parseable geolocated stations")
    return sorted(out, key=lambda row: str(row["station_id"]))


def _integrity(row: dict[str, Any]) -> dict[str, Any]:
    availability = _num(row.get("availability_pct"))
    latency = _num(row.get("latency_s"))
    components: list[float] = []
    if availability is not None:
        components.append(max(0.0, min(100.0, (100.0 - availability) * 4.0)))
    if latency is not None:
        # Delivery latency, logarithm-free and deliberately conservative.
        components.append(max(0.0, min(100.0, (latency - 5.0) / 295.0 * 100.0)))
    if not components:
        return {
            "state": "unknown", "claim_class": "inventory_only",
            "claim_level": "observed_metric",
            "cause": "unestablished", "confidence": 0.0,
        }
    score = round(max(components), 2)
    state = (
        "normal" if score < 25 else
        "mild_degradation" if score < 50 else
        "degraded" if score < 75 else
        "severe_degradation"
    )
    return {
        "state": state,
        "claim_class": "derived_degradation",
        "claim_level": "derived_degradation",
        "cause": "unestablished",
        "confidence": 0.72 if len(components) > 1 else 0.5,
        "degradation_score": score,
    }


class EurefGnssIntegrity(LiveDevice):
    """Global EPN station inventory with source-published delivery health."""

    model = "GAIA-GNSS-INTEGRITY (EUREF EPN)"
    fields = {
        "degradation_score": "score",
        "availability_pct": "percent",
        "latency_s": "s",
        "confidence": "ratio",
        "latitude": "deg",
        "longitude": "deg",
    }
    policy_id = "euref_epn"
    _URL = "https://www.epncb.oma.be/_networkdata/stationlist.php"
    _INVENTORY_URL = "https://gnss.be/epndata.php"
    timeout = 45.0

    def __init__(self, device_id: str, clock: SimClock, **kw: Any) -> None:
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("euref_epn")
        policy.require_endpoint(self._URL)
        policy.require_endpoint(self._INVENTORY_URL)
        self.source = f"{self._URL} · {self._INVENTORY_URL} ({policy.attribution})"
        self.url = self._URL
        self.query_gate = threading.Lock()
        self._state_lock = threading.Lock()
        self._query_station: str | None = None
        self._stations: list[dict[str, Any]] | None = None
        self._cached_at = 0.0
        self._meta: dict[str, Any] = {}
        self._ttl_s = max(300.0, min(float(os.environ.get("GAIA_GNSS_DIRECTORY_TTL_S", "900")), 86_400.0))
        raw_path = os.environ.get("GAIA_GNSS_CACHE_PATH", "").strip()
        self._cache_path = Path(raw_path) if raw_path else None
        self._load_cache()

    @staticmethod
    def validate_station_id(value: Any) -> str:
        station_id = str(value or "").strip().upper()
        if not _SAFE_STATION_ID.fullmatch(station_id):
            raise ValueError("station_id must be a 4- or 9-character EPN station id")
        return station_id

    def set_station(self, value: Any) -> None:
        with self._state_lock:
            self._query_station = self.validate_station_id(value)

    def clear_station(self) -> None:
        with self._state_lock:
            self._query_station = None

    def _load_cache(self) -> None:
        path = self._cache_path
        if path is None or not path.is_file():
            return
        try:
            if path.stat().st_size > 16 * 1024 * 1024:
                return
            body = json.loads(path.read_text(encoding="utf-8"))
            rows = body.get("stations") if isinstance(body, dict) else None
            if not isinstance(rows, list) or not rows:
                return
            self._stations = [row for row in rows if isinstance(row, dict)]
            self._meta = dict(body.get("meta") or {})
            self._cached_at = time.monotonic() - max(0.0, time.time() - path.stat().st_mtime)
        except (OSError, ValueError, json.JSONDecodeError):
            return

    def _save_cache(self, rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
        path = self._cache_path
        if path is None:
            return
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"version": 1, "stations": rows, "meta": meta}, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _snapshot(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        with self._state_lock:
            if self._stations is not None and time.monotonic() - self._cached_at < self._ttl_s:
                return list(self._stations), dict(self._meta)
            stale = list(self._stations or [])
            stale_meta = dict(self._meta)
        try:
            source_url = self._URL
            try:
                rows = parse_euref_station_html(
                    self._fetch_text(self._URL, max_chars=16_000_000)
                )
            except DeviceOffline:
                # Same EPN/ROB source identity, different official delivery
                # surface. The Open Data Portal provides station coordinates
                # but usually not current latency, so these rows honestly fall
                # back to inventory-only/unknown rather than fake green state.
                source_url = self._INVENTORY_URL
                rows = parse_euref_station_html(
                    self._fetch_text(self._INVENTORY_URL, max_chars=16_000_000)
                )
            rows = [{**row, "source_url": source_url} for row in rows]
            rows = [{**row, **_integrity(row)} for row in rows]
            meta = {"source_id": "euref_epn", "source_url": source_url, "station_count": len(rows), "stale": False}
        except DeviceOffline:
            if not stale:
                raise
            stale_meta["stale"] = True
            return stale, stale_meta
        with self._state_lock:
            self._stations, self._meta, self._cached_at = rows, meta, time.monotonic()
        self._save_cache(rows, meta)
        return list(rows), dict(meta)

    @staticmethod
    def _hotspot(row: dict[str, Any]) -> dict[str, Any]:
        out = {
            "point_id": f"gnss-station:euref:{row['station_id']}",
            "station_id": row["station_id"],
            "network": "EUREF EPN",
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "state": row.get("state", "unknown"),
            "claim_class": row.get("claim_class", "inventory_only"),
            "claim_level": row.get("claim_level", "observed_metric"),
            "cause": "unestablished",
            "measurement_basis": "delivery_path_proxy",
            "source_url": row.get("source_url") or EurefGnssIntegrity._URL,
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "EUREF Permanent GNSS Network / EPN Central Bureau",
            "modified": True,
        }
        for key in ("name", "country", "source_status", "availability_pct", "latency_s", "degradation_score", "confidence"):
            if row.get(key) is not None:
                out[key] = row[key]
        return out

    def read(self) -> dict[str, Any]:
        rows, meta = self._snapshot()
        with self._state_lock:
            station_id = self._query_station
        selected = next((row for row in rows if row.get("station_id") == station_id), None) if station_id else None
        if station_id and selected is None:
            raise DeviceOffline(f"{self.device_id}: EUREF station {station_id} not present in current inventory")
        ranked = sorted(rows, key=lambda row: float(row.get("degradation_score") or -1), reverse=True)
        head = selected or ranked[0]
        hotspots = [self._hotspot(selected)] if selected else [self._hotspot(row) for row in rows]
        values: dict[str, float] = {
            "latitude": float(head["latitude"]),
            "longitude": float(head["longitude"]),
            "confidence": float(head.get("confidence") or 0.0),
        }
        for key in ("availability_pct", "latency_s", "degradation_score"):
            value = _num(head.get(key))
            if value is not None:
                values[key] = value
        values = {key: round(value, 4) for key, value in self._faulted(values).items()}
        self._seq += 1
        reporting = sum(1 for row in rows if row.get("claim_class") == "derived_degradation")
        degraded = sum(1 for row in rows if row.get("state") == "degraded")
        reading = {
            "device_id": self.device_id,
            "model": self.model,
            "site": self.site,
            "firmware": self.firmware,
            "seq": self._seq,
            "ts": self.clock.iso(),
            "values": values,
            "units": {key: self.fields[key] for key in values if key in self.fields},
            "hotspots": hotspots,
            "hotspot_count": len(hotspots),
            "inventory_total": len(rows),
            "stations_reporting_now": reporting,
            "stations_degraded": degraded,
            "query_station_id": station_id,
            "claim_class": head.get("claim_class", "inventory_only"),
            "claim_level": head.get("claim_level", "observed_metric"),
            "state": head.get("state", "unknown"),
            "cause": "unestablished",
            "measurement_basis": "delivery_path_proxy",
            "evidence_boundary": (
                "Position is an EPN inventory fact. Availability/latency describe the observation "
                "delivery path; they do not independently prove RF jamming or spoofing."
            ),
            "source_id": "euref_epn",
            "source_url": meta.get("source_url") or head.get("source_url") or self._URL,
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "EUREF Permanent GNSS Network / EPN Central Bureau",
            "modified": True,
            "cache_stale": bool(meta.get("stale")),
        }
        attestation = sign_reading(reading, self.signer)
        self._last_values = dict(values)
        return {"reading": reading, "attestation": attestation}


def _deep_value(body: Any, wanted: tuple[str, ...]) -> Any:
    """Find a named leaf in small upstream metadata objects."""
    if isinstance(body, dict):
        normalized = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in body.items()}
        for key in wanted:
            value = normalized.get(re.sub(r"[^a-z0-9]", "", key.lower()))
            if value is not None:
                if isinstance(value, dict) and value.get("value") is not None:
                    return value["value"]
                return value
        for value in body.values():
            found = _deep_value(value, wanted)
            if found is not None:
                return found
    elif isinstance(body, list):
        for value in body:
            found = _deep_value(value, wanted)
            if found is not None:
                return found
    return None


def parse_ga_site_logs(payload: Any) -> list[dict[str, Any]]:
    """Parse Geoscience Australia's public HAL site-log collection."""
    embedded = payload.get("_embedded") if isinstance(payload, dict) else None
    raw = embedded.get("siteLogs") if isinstance(embedded, dict) else None
    if not isinstance(raw, list):
        # Some deployments expose a bare page/list; accept it without loosening
        # the coordinate/id validation below.
        raw = payload.get("siteLogs") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise DeviceOffline("Geoscience Australia site-log response has no siteLogs list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        ident = item.get("siteIdentification") if isinstance(item.get("siteIdentification"), dict) else item
        sid = str(_deep_value(ident, ("nineCharacterId", "fourCharacterId", "siteId")) or "").strip().upper()
        sid = re.sub(r"[^A-Z0-9]", "", sid)
        if not _SAFE_STATION_ID.fullmatch(sid) or sid in seen:
            continue
        location = item.get("siteLocation") if isinstance(item.get("siteLocation"), dict) else item
        lat = _num(_deep_value(location, ("latitude", "lat")))
        lon = _num(_deep_value(location, ("longitude", "lon")))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        row: dict[str, Any] = {
            "station_id": sid,
            "latitude": round(float(lat), 6),
            "longitude": round(float(lon), 6),
            "network": "Geoscience Australia",
        }
        name = _deep_value(ident, ("siteName", "monumentDescription"))
        country = _deep_value(location, ("country", "countryCode"))
        installed = _deep_value(item, ("dateInstalled", "datePrepared"))
        if name:
            row["name"] = str(name)[:160]
        if country:
            row["country"] = str(country)[:80]
        if installed:
            row["installed_at"] = str(installed)[:80]
        out.append({**row, **_integrity(row)})
        seen.add(sid)
    if not out:
        raise DeviceOffline("Geoscience Australia site-log response has no geolocated stations")
    return sorted(out, key=lambda row: str(row["station_id"]))


class GaGnssInventory(LiveDevice):
    """Geoscience Australia public GNSS station metadata (inventory claim)."""

    model = "GAIA-GNSS-INVENTORY (Geoscience Australia)"
    fields = {"confidence": "ratio", "latitude": "deg", "longitude": "deg"}
    policy_id = "ga_gnss"
    _URL = "https://gws.geodesy.ga.gov.au/siteLogs?size=2000"
    timeout = 60.0

    def __init__(self, device_id: str, clock: SimClock, **kw: Any) -> None:
        super().__init__(device_id, clock, **kw)
        policy = require_approved_source("ga_gnss")
        policy.require_endpoint(self._URL)
        self.source = f"{self._URL} ({policy.attribution})"
        self.url = self._URL
        self.query_gate = threading.Lock()
        self._state_lock = threading.Lock()
        self._query_station: str | None = None
        self._stations: list[dict[str, Any]] | None = None
        self._cached_at = 0.0
        self._ttl_s = max(900.0, min(float(os.environ.get("GAIA_GNSS_GA_TTL_S", "21600")), 86_400.0))
        raw_path = os.environ.get("GAIA_GNSS_GA_CACHE_PATH", "").strip()
        self._cache_path = Path(raw_path) if raw_path else None
        self._load_cache()

    def set_station(self, value: Any) -> None:
        with self._state_lock:
            self._query_station = EurefGnssIntegrity.validate_station_id(value)

    def clear_station(self) -> None:
        with self._state_lock:
            self._query_station = None

    def _load_cache(self) -> None:
        path = self._cache_path
        if path is None or not path.is_file():
            return
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            rows = body.get("stations") if isinstance(body, dict) else None
            if isinstance(rows, list) and rows:
                self._stations = [row for row in rows if isinstance(row, dict)]
                self._cached_at = time.monotonic() - max(0.0, time.time() - path.stat().st_mtime)
        except (OSError, ValueError, json.JSONDecodeError):
            return

    def _save_cache(self, rows: list[dict[str, Any]]) -> None:
        path = self._cache_path
        if path is None:
            return
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"version": 1, "stations": rows}, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _snapshot(self) -> tuple[list[dict[str, Any]], bool]:
        with self._state_lock:
            if self._stations is not None and time.monotonic() - self._cached_at < self._ttl_s:
                return list(self._stations), False
            stale = list(self._stations or [])
        try:
            first = self._fetch(self._URL)
            rows = parse_ga_site_logs(first)
            page = first.get("page") if isinstance(first, dict) else None
            try:
                total_pages = int(page.get("totalPages") or 1) if isinstance(page, dict) else 1
            except (TypeError, ValueError):
                total_pages = 1
            if not 1 <= total_pages <= 100:
                raise DeviceOffline("Geoscience Australia pagination exceeds safe page limit")
            for page_no in range(1, total_pages):
                rows.extend(parse_ga_site_logs(self._fetch(f"{self._URL}&page={page_no}")))
            rows = sorted(
                {str(row["station_id"]): row for row in rows}.values(),
                key=lambda row: str(row["station_id"]),
            )
        except DeviceOffline:
            if not stale:
                raise
            return stale, True
        with self._state_lock:
            self._stations, self._cached_at = rows, time.monotonic()
        self._save_cache(rows)
        return list(rows), False

    @staticmethod
    def _hotspot(row: dict[str, Any]) -> dict[str, Any]:
        out = {
            "point_id": f"gnss-station:ga:{row['station_id']}",
            "station_id": row["station_id"],
            "network": "Geoscience Australia",
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "state": "unknown",
            "claim_class": "inventory_only",
            "claim_level": "observed_metric",
            "cause": "unestablished",
            "measurement_basis": "station_inventory",
            "source_url": GaGnssInventory._URL,
            "license": "CC BY 3.0 Australia",
            "license_url": "https://creativecommons.org/licenses/by/3.0/au/",
            "attribution": "Geoscience Australia GNSS data",
            "modified": True,
        }
        for key in ("name", "country", "installed_at"):
            if row.get(key) is not None:
                out[key] = row[key]
        return out

    def read(self) -> dict[str, Any]:
        rows, stale = self._snapshot()
        with self._state_lock:
            station_id = self._query_station
        selected = next((row for row in rows if row.get("station_id") == station_id), None) if station_id else None
        if station_id and selected is None:
            raise DeviceOffline(f"{self.device_id}: GA station {station_id} not present in current inventory")
        head = selected or rows[0]
        hotspots = [self._hotspot(head)] if selected else [self._hotspot(row) for row in rows]
        values = {
            "latitude": float(head["latitude"]), "longitude": float(head["longitude"]),
            "confidence": 0.0,
        }
        self._seq += 1
        reading = {
            "device_id": self.device_id, "model": self.model, "site": self.site,
            "firmware": self.firmware, "seq": self._seq, "ts": self.clock.iso(),
            "values": values, "units": dict(self.fields), "hotspots": hotspots,
            "hotspot_count": len(hotspots), "inventory_total": len(rows),
            "stations_reporting_now": 0, "stations_degraded": 0,
            "query_station_id": station_id, "claim_class": "inventory_only",
            "claim_level": "observed_metric",
            "state": "unknown", "cause": "unestablished",
            "measurement_basis": "station_inventory",
            "evidence_boundary": (
                "This source publishes station metadata/position only in this adapter. "
                "No RF or current integrity state is inferred from inventory presence."
            ),
            "source_id": "ga_gnss", "source_url": self._URL,
            "license": "CC BY 3.0 Australia",
            "license_url": "https://creativecommons.org/licenses/by/3.0/au/",
            "attribution": "Geoscience Australia GNSS data",
            "modified": True,
            "cache_stale": stale,
        }
        attestation = sign_reading(reading, self.signer)
        self._last_values = dict(values)
        return {"reading": reading, "attestation": attestation}


def register_gnss_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    enabled = os.environ.get("GAIA_GNSS_EUREF_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    count = 0
    if enabled:
        fleet.add(EurefGnssIntegrity("gnss-euref-01", clock, site="live-gnss-integrity", key_dir=key_dir))
        count += 1
    ga_enabled = os.environ.get("GAIA_GNSS_GA_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    if ga_enabled:
        fleet.add(GaGnssInventory("gnss-ga-01", clock, site="live-gnss-inventory-au", key_dir=key_dir))
        count += 1
    return count


__all__ = [
    "EurefGnssIntegrity", "GaGnssInventory", "parse_euref_station_html",
    "parse_ga_site_logs", "register_gnss_relays",
]
