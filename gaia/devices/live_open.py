"""Commercially clear LIVE relays — FIRMS, Safecast (CC0), CyberNews GNSS (CC BY).

These sources may be attested and sold pay-per-call without a separate commercial
deal. Sources outside the positive policy registry belong in the separate
quarantine document and must never become runtime fallbacks here.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices import live as livemod
from gaia.devices.live import (
    LiveDevice,
    _UA,
    _assert_url_allowed,
    _env,
    _lat_lon,
    _num,
)

# ── NASA FIRMS active fire ─────────────────────────────────────────────────────

_FIRMS_CSV_URL = (
    "https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
    "noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_24h.csv"
)
_FIRMS_AREA_TMPL = (
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
    "{key}/VIIRS_SNPP_NRT/world/1"
)

_CONF_RANK = {
    "h": 90.0,
    "high": 90.0,
    "n": 50.0,
    "nominal": 50.0,
    "l": 20.0,
    "low": 20.0,
}

_DEFAULT_HOTSPOT_LIMIT = 500
_MAX_HOTSPOT_LIMIT = 5000
# Max ranked rows kept for packetized delivery (one upstream fetch → many pages).
_DEFAULT_COLLECT_MAX = 250_000
_MAX_COLLECT_TOTAL = 250_000
# Hard ceiling while scanning the CSV so a pathological feed cannot OOM us.
# Real FIRMS 24h feeds are ~1e5 rows; we need the full scan for honest bbox /
# planetary coverage (a tiny cap silently missed most of the globe).
_CSV_SCAN_CAP = 250_000
# Download ceiling — a compromised upstream must not buffer arbitrary bytes.
_FETCH_BYTE_CAP = 128 * 1024 * 1024

log = logging.getLogger("gaia.devices.live_open")


def _confidence_score(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    ranked = _CONF_RANK.get(s.lower())
    if ranked is not None:
        return ranked
    return _num(s)


def _clamp_hotspot_limit(raw: Any, default: int = _DEFAULT_HOTSPOT_LIMIT) -> int:
    """Clamp a single-response / legacy ``limit`` (1–5000). Prefer collect+page."""
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, _MAX_HOTSPOT_LIMIT))


def _clamp_collect_total(raw: Any, default: int = _DEFAULT_COLLECT_MAX) -> int:
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, _MAX_COLLECT_TOTAL))


def _parse_bbox(data: dict[str, Any] | None) -> tuple[float, float, float, float] | None:
    """Optional buyer bbox filter (west, south, east, north). None = global."""
    if not isinstance(data, dict):
        return None
    keys = ("west", "south", "east", "north")
    if not all(k in data and data[k] is not None for k in keys):
        return None
    try:
        west, south, east, north = (float(data[k]) for k in keys)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        return None
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        return None
    if south > north:
        south, north = north, south
    return west, south, east, north


def _in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    west, south, east, north = bbox
    if lat < south or lat > north:
        return False
    if west <= east:
        return west <= lon <= east
    # antimeridian wrap
    return lon >= west or lon <= east


class FirmsFireHotspot(LiveDevice):
    """NASA FIRMS VIIRS — headline = brightest; ``hotspots`` = top-N cluster (same fetch).

    Buyer invoke may pass optional ``west/south/east/north`` + ``limit`` to filter the
    operator-fetched CSV (no client URLs — SSRF-safe). Anchors for fixed sensors stay
    operator-owned; fire is an event feed so geographic interest is client-selectable.
    """

    model = "GAIA-FIRE (NASA FIRMS)"
    fields = {
        "brightness_k": "K",
        "confidence": "pct",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://firms.modaps.eosdis.nasa.gov "
        "(NASA FIRMS VIIRS active fire; open data — cite NASA FIRMS / disclaimer)"
    )

    def __init__(self, device_id: str, clock: SimClock, *, map_key: str = "", **kw):
        super().__init__(device_id, clock, **kw)
        key = (map_key or "").strip()
        if key and all(c.isalnum() or c in "-_" for c in key) and len(key) <= 64:
            self.url = _FIRMS_AREA_TMPL.format(key=key)
        else:
            if key:
                log.warning(
                    "%s: GAIA_FIRMS_MAP_KEY malformed — falling back to the "
                    "keyless NOAA-20 24h CSV feed", device_id,
                )
            self.url = _FIRMS_CSV_URL
        self.timeout = 45.0
        self._lock = threading.Lock()
        # Held across set_query → read → clear_query so concurrent paid invokes
        # cannot interleave bbox state (buyer A must never get buyer B's filter).
        self.query_gate = threading.Lock()
        env_limit = _clamp_hotspot_limit(
            os.environ.get("GAIA_FIRMS_HOTSPOT_LIMIT"), _DEFAULT_HOTSPOT_LIMIT
        )
        self._default_limit = env_limit
        self._default_collect = _clamp_collect_total(
            os.environ.get("GAIA_FIRMS_COLLECT_MAX"), _DEFAULT_COLLECT_MAX
        )
        self._query_bbox: tuple[float, float, float, float] | None = None
        self._query_limit: int | None = None
        self._query_collect: int | None = None
        self._query_page_size: int | None = None

    def set_query(
        self,
        *,
        bbox: tuple[float, float, float, float] | None = None,
        limit: int | None = None,
        collect_max: int | None = None,
        page_size: int | None = None,
    ) -> None:
        from gaia.devices.hotspot_pages import clamp_page_size

        with self._lock:
            self._query_bbox = bbox
            self._query_limit = (
                _clamp_hotspot_limit(limit, self._default_limit) if limit is not None else None
            )
            self._query_collect = (
                _clamp_collect_total(collect_max, self._default_collect)
                if collect_max is not None
                else None
            )
            self._query_page_size = (
                clamp_page_size(page_size) if page_size is not None else None
            )

    def clear_query(self) -> None:
        with self._lock:
            self._query_bbox = None
            self._query_limit = None
            self._query_collect = None
            self._query_page_size = None

    def _fetch_text(self, url: str) -> str:
        url = _assert_url_allowed(url)
        headers = {"User-Agent": _UA, **(self.headers or {})}
        try:
            resp = livemod.httpx.get(
                url, headers=headers, timeout=self.timeout, follow_redirects=False
            )
            if resp.status_code != 200:
                raise DeviceOffline(
                    f"{self.device_id}: upstream HTTP {resp.status_code}"
                )
            if len(resp.content) > _FETCH_BYTE_CAP:
                raise DeviceOffline(
                    f"{self.device_id}: upstream body exceeds {_FETCH_BYTE_CAP} bytes"
                )
            return resp.text
        except DeviceOffline:
            raise
        except livemod.httpx.HTTPError as exc:
            raise DeviceOffline(
                f"{self.device_id}: upstream unreachable ({type(exc).__name__})"
            ) from exc

    def collect_hotspots(
        self,
        text: str,
        *,
        limit: int | None = None,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> tuple[list[dict[str, Any]], bool, int, dict[str, Any]]:
        """Parse FIRMS CSV → ranked non-low hotspots (brightest first).

        Returns ``(rows, truncated, hotspot_matched, best_global)`` where:
        - ``rows`` are bbox-filtered (if any) then capped by ``limit``
        - ``hotspot_matched`` is the **global** non-low count (ignores bbox) —
          honest Wildfire sidebar total even when the map only loads a viewport
        - ``truncated`` means more **in-scope** matches existed than ``limit``
        - ``best_global`` is the brightest detection of THIS fetch regardless of
          bbox — the attestable headline when the viewport itself is empty
        """
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        if len(lines) < 2:
            raise DeviceOffline(f"{self.device_id}: FIRMS feed empty")
        header = [h.strip().lower() for h in lines[0].split(",")]
        try:
            lat_i = header.index("latitude")
            lon_i = header.index("longitude")
        except ValueError as exc:
            raise DeviceOffline(f"{self.device_id}: FIRMS CSV missing lat/lon") from exc
        bright_i = next((i for i, h in enumerate(header) if "bright" in h), -1)
        conf_i = header.index("confidence") if "confidence" in header else -1

        def cell(cols: list[str], name: str) -> str:
            try:
                idx = header.index(name)
            except ValueError:
                return ""
            return cols[idx].strip() if idx < len(cols) else ""

        def provenance(cols: list[str]) -> dict[str, Any]:
            """Small, stable FIRMS provenance envelope — no arbitrary CSV echo."""
            date = cell(cols, "acq_date")
            raw_time = cell(cols, "acq_time")
            digits = "".join(ch for ch in raw_time if ch.isdigit()).zfill(4)[-4:]
            observed_at = ""
            if date and len(digits) == 4:
                observed_at = f"{date}T{digits[:2]}:{digits[2:]}:00Z"
            out: dict[str, Any] = {}
            for key in ("satellite", "instrument", "daynight", "version"):
                value = cell(cols, key)
                if value:
                    out[key] = value[:80]
            if date:
                out["acq_date"] = date[:16]
            if raw_time:
                out["acq_time_utc"] = digits
            if observed_at:
                out["observed_at"] = observed_at
            for source_key, target_key in (
                ("frp", "frp_mw"),
                ("scan", "scan_km"),
                ("track", "track_km"),
            ):
                value = _num(cell(cols, source_key))
                if value is not None:
                    out[target_key] = float(value)
            return out

        scored: list[tuple[float, dict[str, Any]]] = []
        matched_global = 0
        best_global: dict[str, Any] | None = None
        best_rank = float("-inf")
        for ln in lines[1:_CSV_SCAN_CAP + 1]:
            cols = ln.split(",")
            if max(lat_i, lon_i) >= len(cols):
                continue
            lat = _num(cols[lat_i])
            lon = _num(cols[lon_i])
            if lat is None or lon is None:
                continue
            conf_raw = cols[conf_i].strip() if conf_i >= 0 and conf_i < len(cols) else ""
            if conf_raw.lower() in ("l", "low"):
                continue
            conf = _confidence_score(conf_raw) if conf_raw else 50.0
            bright = (
                _num(cols[bright_i])
                if bright_i >= 0 and bright_i < len(cols)
                else None
            )
            if bright is None or conf is None:
                continue
            matched_global += 1
            rank = bright + conf * 0.01
            row = {
                "brightness_k": float(bright),
                "confidence": float(conf),
                "latitude": float(lat),
                "longitude": float(lon),
                **provenance(cols),
            }
            if rank > best_rank:
                best_rank = rank
                best_global = dict(row)
            if bbox is not None and not _in_bbox(lat, lon, bbox):
                continue
            scored.append((rank, row))
        if matched_global == 0 or best_global is None:
            raise DeviceOffline(f"{self.device_id}: FIRMS has no non-low hotspots")
        if not scored:
            # Global fires exist, but none in this viewport bbox.
            return [], False, matched_global, best_global
        scored.sort(key=lambda t: t[0], reverse=True)
        in_scope = len(scored)
        cap = _clamp_collect_total(limit, self._default_collect)
        truncated = in_scope > cap
        return [row for _, row in scored[:cap]], truncated, matched_global, best_global

    def sample(self) -> dict[str, float]:
        with self._lock:
            bbox = self._query_bbox
            limit = self._query_collect or self._query_limit or self._default_collect
        text = self._fetch_text(self.url)
        hotspots, _trunc, _matched, best = self.collect_hotspots(text, limit=limit, bbox=bbox)
        row = hotspots[0] if hotspots else best
        return {key: float(row[key]) for key in self.fields}

    def map(self, payload: Any) -> dict[str, float | None]:  # pragma: no cover
        raise NotImplementedError("FirmsFireHotspot uses collect_hotspots()")

    def map_text(self, text: str) -> dict[str, float | None]:
        """Back-compat: brightest row only."""
        rows, _trunc, _matched, best = self.collect_hotspots(text, limit=1)
        row = rows[0] if rows else best
        return {key: float(row[key]) for key in self.fields}

    def _round_cluster(self, hotspots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rounded: list[dict[str, Any]] = []
        metadata = (
            "observed_at", "acq_date", "acq_time_utc", "satellite",
            "instrument", "daynight", "version", "frp_mw", "scan_km", "track_km",
        )
        for h in hotspots:
            row: dict[str, Any] = {
                "brightness_k": round(float(h["brightness_k"]), 4),
                "confidence": round(float(h["confidence"]), 4),
                "latitude": round(float(h["latitude"]), 4),
                "longitude": round(float(h["longitude"]), 4),
            }
            for key in metadata:
                if h.get(key) is not None:
                    row[key] = h[key]
            rounded.append(row)
        return rounded

    def _sign_page(
        self,
        *,
        values: dict[str, float],
        cluster: list[dict[str, Any]],
        hotspot_total: int,
        hotspot_offset: int,
        page_size: int,
        next_cursor: str | None,
        fetch_id: str,
        truncated: bool,
        hotspot_matched: int = 0,
    ) -> dict[str, Any]:
        from gaia.attestation import sign_reading
        from gaia.devices.hotspot_pages import clamp_page_size

        with self._lock:
            self._seq += 1
            seq = self._seq
        reading = {
            "device_id": self.device_id,
            "model": self.model,
            "site": self.site,
            "firmware": self.firmware,
            "seq": seq,
            "ts": self.clock.iso(),
            "values": values,
            "units": dict(self.fields),
            "hotspots": cluster,
            "hotspot_count": len(cluster),
            "hotspot_total": int(hotspot_total),
            "hotspot_matched": int(hotspot_matched or hotspot_total),
            "hotspot_offset": int(hotspot_offset),
            "hotspot_page_size": clamp_page_size(page_size),
            "next_cursor": next_cursor,
            "fetch_id": fetch_id,
            "truncated": bool(truncated),
        }
        attestation = sign_reading(reading, self.signer)
        with self._lock:
            self._last_values = dict(values)
        return {"reading": reading, "attestation": attestation}

    def read_page_from_cursor(self, cursor: str, *, page_size: int | None = None) -> dict[str, Any]:
        """Idempotent resume — same cursor → same packet (safe after network failure).

        ``page_size=None`` keeps the page size the session was opened with.
        """
        from gaia.devices.hotspot_pages import STORE

        page = STORE.page(cursor=cursor, page_size=page_size, device_id=self.device_id)
        values = {k: round(float(v), 4) for k, v in page["values"].items()}
        cluster = self._round_cluster(page["hotspots"])
        return self._sign_page(
            values=values,
            cluster=cluster,
            hotspot_total=page["hotspot_total"],
            hotspot_offset=page["hotspot_offset"],
            page_size=page["hotspot_page_size"],
            next_cursor=page["next_cursor"],
            fetch_id=page["fetch_id"],
            truncated=page["truncated"],
            hotspot_matched=page.get("hotspot_matched") or page["hotspot_total"],
        )

    def read(self) -> dict[str, Any]:
        """Attested headline + first hotspot packet; more via ``next_cursor``."""
        from gaia.devices.hotspot_pages import STORE, clamp_page_size

        with self._lock:
            bbox = self._query_bbox
            collect = (
                self._query_collect
                or self._query_limit
                or self._default_collect
            )
            page_size = clamp_page_size(self._query_page_size)
        text = self._fetch_text(self.url)
        all_hs, truncated, matched, best_global = self.collect_hotspots(
            text, limit=collect, bbox=bbox
        )
        # Empty viewport: attest the brightest detection of THIS fetch (global)
        # — physics-valid and freshly relayed; hotspots=[] says "none in bbox".
        # Never re-sign stale _last_values or zero coordinates: zeros violate
        # the brightness_k physics bound and fail Pay-on-Verified escrow.
        honest_row = all_hs[0] if all_hs else best_global
        honest = {key: float(honest_row[key]) for key in self.fields}
        values = {k: round(v, 4) for k, v in self._faulted(honest).items()}
        rounded = self._round_cluster(all_hs)
        if len(rounded) <= page_size:
            # Whole result fits one packet — no cursor, no session to keep.
            return self._sign_page(
                values=values,
                cluster=rounded,
                hotspot_total=len(rounded),
                hotspot_offset=0,
                page_size=page_size,
                next_cursor=None,
                fetch_id=uuid.uuid4().hex,
                truncated=truncated,
                hotspot_matched=matched,
            )
        fetch_id = STORE.put(
            device_id=self.device_id,
            hotspots=rounded,
            values=values,
            bbox=bbox,
            truncated=truncated,
            hotspot_matched=matched,
            source=self.source,
            model=self.model,
            site=self.site,
            firmware=self.firmware,
            page_size=page_size,
        )
        page = STORE.page(fetch_id=fetch_id, offset=0, page_size=page_size)
        return self._sign_page(
            values=values,
            cluster=page["hotspots"],
            hotspot_total=page["hotspot_total"],
            hotspot_offset=0,
            page_size=page["hotspot_page_size"],
            next_cursor=page["next_cursor"],
            fetch_id=fetch_id,
            truncated=truncated,
            hotspot_matched=matched,
        )


# ── Safecast radiation (CC0) ───────────────────────────────────────────────────


class SafecastRadiation(LiveDevice):
    """Safecast citizen radiation — highest CPM near anchor + ``hotspots`` cluster."""

    model = "GAIA-RAD (Safecast)"
    fields = {
        "cpm": "cpm",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://api.safecast.org "
        "(Safecast measurements API; Creative Commons CC0 1.0 — public domain dedication)"
    )
    _default_limit = 2000

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        latitude: float = 37.42,
        longitude: float = 141.03,
        distance_m: int = 250_000,
        max_age_days: int = 30,
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        dist = max(1_000, min(int(distance_m), 1_000_000))
        self._distance_m = dist
        # 0 = no captured_after (citizen-survey archive: AU 2014 drive grids).
        self._max_age_days = max(0, min(int(max_age_days), 7300))
        self.url = (
            "https://api.safecast.org/measurements.json"
            f"?distance={dist}&latitude={self.latitude:.5f}"
            f"&longitude={self.longitude:.5f}&per_page=150"
        )

    def _page_budget(self) -> int:
        raw = os.environ.get("GAIA_SAFECAST_MAX_PAGES")
        if raw:
            try:
                return max(1, min(int(raw), 40))
            except ValueError:
                pass
        # Archive queries (no recency window) need more pages; Hub SKU stays light.
        return 40 if self._max_age_days == 0 else 5

    def _safecast_url(self, page: int) -> str:
        url = f"{self.url}&page={max(1, int(page))}"
        if self._max_age_days <= 0:
            return url
        # Recency window per read — Safecast holds 15+ years. The default Hub SKU
        # sells recent CPM; extra map anchors may set max_age_days=0 for archives.
        from datetime import datetime, timedelta, timezone

        after = (
            datetime.now(timezone.utc) - timedelta(days=self._max_age_days)
        ).strftime("%Y-%m-%d")
        return f"{url}&captured_after={after}"

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = payload if isinstance(payload, list) else []
        scored: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # The feed mixes units (status/celcius/battery volts share the same
            # ``value`` field) — only genuine CPM rows may enter the cluster.
            if str(row.get("unit") or "").strip().lower() != "cpm":
                continue
            cpm = _num(row.get("value"))
            lat = _num(row.get("latitude"))
            lon = _num(row.get("longitude"))
            if cpm is None or lat is None or lon is None:
                continue
            item: dict[str, Any] = {
                "cpm": float(cpm),
                "latitude": float(lat),
                "longitude": float(lon),
            }
            captured = str(row.get("captured_at") or "").strip()
            if captured:
                item["captured_at"] = captured[:40]
            scored.append(item)
        scored.sort(key=lambda h: h["cpm"], reverse=True)
        default_cap = 5000 if self._max_age_days == 0 else self._default_limit
        cap = max(1, min(int(limit or default_cap), 5000))
        # Dedup near-identical coords (citizen sensors often re-report).
        # Archive drive-grids need a finer grain so southern-AU survey rows survive.
        grain = 2000 if self._max_age_days == 0 else 200
        out: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        for h in scored:
            key = (int(h["latitude"] * grain), int(h["longitude"] * grain))
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
            if len(out) >= cap:
                break
        if not out:
            raise DeviceOffline(f"{self.device_id}: Safecast returned no measurements")
        return out

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {k: float(row[k]) for k in self.fields}

    def read(self) -> dict[str, Any]:
        # Safecast serves ≤150/page — pull several pages for a factual cluster.
        pages: list[Any] = []
        for page_i in range(1, self._page_budget() + 1):
            try:
                chunk = self._fetch(self._safecast_url(page_i))
            except DeviceOffline:
                if pages:
                    break  # degrade to the pages already fetched
                raise
            if not isinstance(chunk, list) or not chunk:
                break
            pages.extend(chunk)
            if len(chunk) < 150:
                break
        hotspots = self.collect_hotspots(pages)
        honest = {k: float(hotspots[0][k]) for k in self.fields}
        values = {k: round(v, 4) for k, v in self._faulted(honest).items()}
        self._seq += 1
        cluster = []
        for h in hotspots:
            item = {
                "cpm": round(float(h["cpm"]), 4),
                "latitude": round(float(h["latitude"]), 4),
                "longitude": round(float(h["longitude"]), 4),
            }
            if h.get("captured_at"):
                item["captured_at"] = h["captured_at"]
            cluster.append(item)
        reading = {
            "device_id": self.device_id,
            "model": self.model,
            "site": self.site,
            "firmware": self.firmware,
            "seq": self._seq,
            "ts": self.clock.iso(),
            "values": values,
            "units": dict(self.fields),
            "hotspots": cluster,
            "hotspot_count": len(cluster),
        }
        from gaia.attestation import sign_reading

        attestation = sign_reading(reading, self.signer)
        self._last_values = dict(values)
        return {"reading": reading, "attestation": attestation}


# ── CyberNews GNSS jamming (CC BY 4.0) ─────────────────────────────────────────

_SEVERITY_SCORE = {
    "critical": 95.0,
    "high": 80.0,
    "severe": 85.0,
    "medium": 55.0,
    "moderate": 55.0,
    "low": 30.0,
    "info": 20.0,
    "unknown": 40.0,
}


def _severity_score(raw: Any, confidence: Any = None) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        keyed = _SEVERITY_SCORE.get(raw.strip().lower())
        if keyed is not None:
            return keyed
        n = _num(raw)
        if n is not None:
            return n
    conf = _num(confidence)
    if conf is not None:
        # confidence often 0–1 or 0–100
        return conf * 100.0 if conf <= 1.0 else conf
    return 40.0


class CyberNewsJamming(LiveDevice):
    """Curated GNSS interference events from cybernews.space (CC BY 4.0)."""

    model = "GAIA-JAM (CyberNews GNSS)"
    fields = {
        "severity_score": "score",
        "radius_km": "km",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://www.cybernews.space "
        "(cybernews GNSS interference events; Creative Commons CC BY 4.0 — attribution required)"
    )
    url = "https://www.cybernews.space/api/data/gnss"
    _default_limit = 5000

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        records = (payload or {}).get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list) or not records:
            raise DeviceOffline(f"{self.device_id}: CyberNews GNSS feed empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in records:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "UNKNOWN").strip().upper()
            # The upstream registry includes resolved and historical records.
            # A live relay must fail closed instead of re-labelling those as active.
            if status not in {"ACTIVE", "MONITORING"}:
                continue
            lat = _num(row.get("latitude"))
            lon = _num(row.get("longitude"))
            if lat is None or lon is None:
                continue
            sev = _severity_score(row.get("severity"), row.get("confidence"))
            radius = _num(row.get("radius_km")) or 0.0
            event: dict[str, Any] = {
                "severity_score": float(sev),
                "radius_km": float(radius),
                "latitude": float(lat),
                "longitude": float(lon),
                "status": status,
            }
            for key in (
                "event_id", "type", "region", "start_date", "end_date",
                "severity", "attribution", "url",
            ):
                value = row.get(key)
                if value is not None and str(value).strip():
                    event[key] = str(value).strip()[:500]
            confidence = _num(row.get("confidence"))
            if confidence is not None:
                event["confidence_pct"] = float(confidence * 100.0 if confidence <= 1 else confidence)
            for key in ("affected_systems", "affected_sectors", "sources"):
                value = row.get(key)
                if isinstance(value, list):
                    event[key] = value[:20]
            scored.append((sev + radius * 0.001, event))
        if not scored:
            raise DeviceOffline(
                f"{self.device_id}: CyberNews has no ACTIVE/MONITORING geolocated events"
            )
        scored.sort(key=lambda t: t[0], reverse=True)
        # Keep the full curated feed — no decorative round sample.
        cap = max(1, min(int(limit or self._default_limit), 10_000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        row = self.collect_hotspots(payload, limit=1)[0]
        return {key: float(row[key]) for key in self.fields}

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        honest = {key: float(hotspots[0][key]) for key in self.fields}
        values = {k: round(v, 4) for k, v in self._faulted(honest).items()}
        self._seq += 1
        cluster: list[dict[str, Any]] = []
        metadata = (
            "event_id", "type", "region", "status", "start_date", "end_date",
            "severity", "confidence_pct", "attribution", "url", "affected_systems",
            "affected_sectors", "sources",
        )
        for h in hotspots:
            event: dict[str, Any] = {
                "severity_score": round(float(h["severity_score"]), 4),
                "radius_km": round(float(h["radius_km"]), 4),
                "latitude": round(float(h["latitude"]), 4),
                "longitude": round(float(h["longitude"]), 4),
            }
            for key in metadata:
                if h.get(key) is not None:
                    event[key] = h[key]
            cluster.append(event)
        reading = {
            "device_id": self.device_id,
            "model": self.model,
            "site": self.site,
            "firmware": self.firmware,
            "seq": self._seq,
            "ts": self.clock.iso(),
            "values": values,
            "units": dict(self.fields),
            "hotspots": cluster,
            "hotspot_count": len(cluster),
            "event_status_scope": ["ACTIVE", "MONITORING"],
            "feed_generated_at": (
                payload.get("generated_at") if isinstance(payload, dict) else None
            ),
            "license": "CC BY 4.0",
        }
        from gaia.attestation import sign_reading

        attestation = sign_reading(reading, self.signer)
        self._last_values = dict(values)
        return {"reading": reading, "attestation": attestation}


def register_open_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register FIRMS / Safecast / CyberNews when env toggles allow. Returns count."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_FIRMS_ENABLED", "1"):
        fleet.add(
            FirmsFireHotspot(
                "firms-fire-01",
                clock,
                map_key=_env("GAIA_FIRMS_MAP_KEY", ""),
                site="live-fire",
                key_dir=key_dir,
            )
        )
        n += 1
    if enabled("GAIA_SAFECAST_ENABLED", "1"):
        lat, lon = _lat_lon(
            _env("GAIA_SAFECAST_LAT"),
            _env("GAIA_SAFECAST_LON"),
            default_lat=37.42,
            default_lon=141.03,
        )
        try:
            max_age = int(_env("GAIA_SAFECAST_MAX_AGE_DAYS", "30"))
        except ValueError:
            max_age = 30
        fleet.add(
            SafecastRadiation(
                "safecast-01",
                clock,
                latitude=lat,
                longitude=lon,
                max_age_days=max_age,
                site="live-radiation",
                key_dir=key_dir,
            )
        )
        n += 1
    if enabled("GAIA_CYBERNEWS_ENABLED", "1"):
        fleet.add(
            CyberNewsJamming(
                "cybernews-jam-01",
                clock,
                site="live-jamming",
                key_dir=key_dir,
            )
        )
        n += 1
    return n


__all__ = [
    "FirmsFireHotspot",
    "SafecastRadiation",
    "CyberNewsJamming",
    "register_open_relays",
    "_parse_bbox",
    "_clamp_hotspot_limit",
]
