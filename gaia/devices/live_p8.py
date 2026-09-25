"""P8 commercially-clear LIVE relays — Italy quake, CDIP waves, ICOS GHG.

Licence gate (2026-09-08):

* INGV FDSN events — **CC BY 4.0** (INGV Open Data Portal legal notes; commercial OK).
  Cite INGV. Local Italian catalog — not a USGS/EMSC replacement.
* CDIP ERDDAP wave buoys — redistributable without restriction (USACE-sponsored);
  required acknowledgement + link to https://cdip.ucsd.edu/. In-situ Hs — not NDBC txt,
  not Open-Meteo Marine.
* ICOS ATC NRT CO₂ — **CC BY 4.0** (ICOS Data Licence; commercial OK). Cite ICOS +
  object PID. Programmatic download needs Carbon Portal auth after licence accept:
  prefer ``GAIA_ICOS_EMAIL`` + ``GAIA_ICOS_PASSWORD`` (auto-refresh ~28h cookie), or
  static ``GAIA_ICOS_CPAUTH_TOKEN``. New claim class ``gaia.ghg.read@v1``.

**Hold (not coded):** ENTSO-E Transparency Platform — free to view under EU Reg. 543/2013,
but Terms of Use do not grant a clear commercial redistributability right for a paid Hub
SKU (Neon/Jaeger open-data analysis). Revisit if ENTSO-E publishes CC BY / OGL-equivalent.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from gaia.clock import SimClock
from gaia.devices._live_base import _UA
from gaia.devices._policy import _assert_url_allowed
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num
from gaia.devices.live_p0 import signed_cluster_read
from gaia.source_policy import require_approved_source

log = logging.getLogger("gaia.devices.live_p8")

_SAFE_CDIP = re.compile(r"^[0-9]{2,4}$")
_SAFE_ICOS_STA = re.compile(r"^[A-Z]{3}$")
# Cookie token is a signed base64 blob; keep length bounded.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._+\/=-]{8,4096}$")
_SAFE_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,64}$")

_ICOS_SPARQL = "https://meta.icos-cp.eu/sparql"
_ICOS_NRT_SPEC = "http://meta.icos-cp.eu/resources/cpmeta/atcCo2NrtDataObject"
_ICOS_LOGIN_URL = "https://cpauth.icos-cp.eu/password/login"
# Carbon Portal documents 100_000 s (~27.8 h) cookie lifetime.
_ICOS_TOKEN_TTL_S = 100_000
_ICOS_REFRESH_MARGIN_S = 3_600


def _iso_epoch(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _geojson_lon_lat(geometry: Any) -> tuple[float | None, float | None]:
    if not isinstance(geometry, dict):
        return None, None
    coords = geometry.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None, None
    return _num(coords[0]), _num(coords[1])


# ── INGV FDSN ─────────────────────────────────────────────────────────────────


class IngvQuake(LiveDevice):
    """INGV Italian earthquake catalog (FDSN) — CC BY 4.0."""

    model = "GAIA-INGV (FDSN)"
    policy_id = "ingv_fdsn"
    fields = {
        "magnitude": "Mw",
        "depth_km": "km",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://webservices.ingv.it/fdsnws/event/1/query "
        "(INGV FDSN event service; CC BY 4.0 — cite Istituto Nazionale di Geofisica e "
        "Vulcanologia. Italian / regional catalog — complementary to USGS and EMSC, "
        "not a replacement. Parameters may be preliminary.)"
    )
    url = (
        "https://webservices.ingv.it/fdsnws/event/1/query"
        "?limit=100&format=json&orderby=time&minmagnitude=2.0"
    )
    _default_limit = 200
    timeout = 20.0

    def __init__(self, device_id: str, clock: SimClock, **kw: Any):
        super().__init__(device_id, clock, **kw)
        require_approved_source(self.policy_id).require_endpoint(self.url)
        # Prefer last 7 days so empty quiet weeks still fail closed honestly.
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        self.url = (
            "https://webservices.ingv.it/fdsnws/event/1/query"
            f"?starttime={quote(start.strftime('%Y-%m-%dT%H:%M:%S'))}"
            f"&endtime={quote(end.strftime('%Y-%m-%dT%H:%M:%S'))}"
            "&minmagnitude=2.0&format=json&orderby=time&limit=100"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def collect_hotspots(self, payload: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
        features = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise DeviceOffline(f"{self.device_id}: INGV FDSN feed empty")
        scored: list[tuple[float, dict[str, Any]]] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            lon, lat = _geojson_lon_lat(feat.get("geometry"))
            if lat is None:
                lat = _num(props.get("lat"))
            if lon is None:
                lon = _num(props.get("lon"))
            mag = _num(props.get("mag") if props.get("mag") is not None else props.get("magnitude"))
            depth = _num(props.get("depth"))
            coords = (
                ((feat.get("geometry") or {}).get("coordinates"))
                if isinstance(feat.get("geometry"), dict)
                else None
            )
            if depth is None and isinstance(coords, (list, tuple)) and len(coords) > 2:
                depth = _num(coords[2])
            if depth is not None:
                depth = abs(float(depth))
            if lat is None or lon is None or mag is None:
                continue
            item: dict[str, Any] = {
                "magnitude": float(mag),
                "depth_km": float(depth) if depth is not None else 0.0,
                "latitude": float(lat),
                "longitude": float(lon),
            }
            region = str(props.get("place") or props.get("flynn_region") or props.get("region") or "")[:160]
            if region:
                item["region"] = region
            scored.append((float(mag), item))
        if not scored:
            raise DeviceOffline(f"{self.device_id}: INGV had no geolocated events")
        scored.sort(key=lambda t: t[0], reverse=True)
        cap = max(1, min(int(limit or self._default_limit), 1000))
        return [h for _, h in scored[:cap]]

    def map(self, payload: Any) -> dict[str, float | None]:
        return dict(self.collect_hotspots(payload, limit=1)[0])

    def read(self) -> dict[str, Any]:
        payload = self._fetch(self.url)
        hotspots = self.collect_hotspots(payload)
        return signed_cluster_read(
            self,
            hotspots,
            numeric_keys=("magnitude", "depth_km", "latitude", "longitude"),
            meta_keys=("region",),
        )


# ── CDIP ERDDAP ───────────────────────────────────────────────────────────────


class CdipWave(LiveDevice):
    """CDIP / USACE in-situ wave buoy via ERDDAP — unrestricted + required attribution."""

    model = "GAIA-MARINE (CDIP)"
    policy_id = "cdip_erddap"
    fields = {
        "wave_height_m": "m",
        "wave_period_s": "s",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://erddap.cdip.ucsd.edu/erddap/tabledap/wave_agg "
        "(Coastal Data Information Program wave buoys; data may be redistributed and used "
        "without restriction — acknowledgement: Coastal Data Information Program, "
        "Integrative Oceanography Division, Scripps Institution of Oceanography, under "
        "sponsorship of the U.S. Army Corps of Engineers and the California Department of "
        "Parks and Recreation; link https://cdip.ucsd.edu/. In-situ Hs — not NDBC txt, "
        "not Open-Meteo Marine.)"
    )

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station_id: str,
        latitude: float,
        longitude: float,
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        sta = (station_id or "").strip()
        if not _SAFE_CDIP.fullmatch(sta):
            raise ValueError(f"invalid CDIP station id: {station_id!r}")
        self.station_id = sta
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = (
            "https://erddap.cdip.ucsd.edu/erddap/tabledap/wave_agg.json"
            f"?time,waveHs,waveTp,station_id,metaStationName,latitude,longitude"
            f"&station_id=%22{quote(sta, safe='')}%22"
            f"&time%3E=max(time)-2days&orderByMax(%22time%22)"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    @staticmethod
    def _table_rows(payload: Any) -> list[dict[str, Any]]:
        table = payload.get("table") if isinstance(payload, dict) else None
        if not isinstance(table, dict):
            return []
        names = table.get("columnNames") or []
        rows = table.get("rows") or []
        if not isinstance(names, list) or not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list):
                continue
            out.append({str(names[i]): row[i] for i in range(min(len(names), len(row)))})
        return out

    def map(self, payload: Any) -> dict[str, float | None]:
        rows = self._table_rows(payload)
        if not rows:
            raise DeviceOffline(f"{self.device_id}: CDIP wave_agg empty for {self.station_id}")
        row = rows[0]
        hs = _num(row.get("waveHs"))
        if hs is None:
            raise DeviceOffline(f"{self.device_id}: CDIP missing waveHs")
        return {
            "wave_height_m": float(hs),
            "wave_period_s": _num(row.get("waveTp")),
            "latitude": _num(row.get("latitude")) or self.latitude,
            "longitude": _num(row.get("longitude")) or self.longitude,
        }


# ── ICOS ATC NRT CO₂ ──────────────────────────────────────────────────────────


class IcosCpAuth:
    """Shared Carbon Portal cookie auth — password login auto-refreshes ~28h tokens."""

    def __init__(self, *, email: str = "", password: str = "", token: str = ""):
        email = (email or "").strip()
        password = (password or "").strip()
        token = (token or "").strip()
        if email and not _SAFE_EMAIL.fullmatch(email):
            raise ValueError("invalid ICOS email")
        if password and not (8 <= len(password) <= 256):
            raise ValueError("invalid ICOS password length")
        if token and not _SAFE_TOKEN.fullmatch(token):
            raise ValueError("invalid ICOS cpauth token")
        if not ((email and password) or token):
            raise ValueError("ICOS auth requires email+password or cpauth token")
        self._email = email
        self._password = password
        self._token = token
        # Static token: treat as fresh now; password path refreshes before margin.
        self._expires_at = time.time() + (_ICOS_TOKEN_TTL_S if token else 0.0)
        self._lock = threading.Lock()
        require_approved_source("icos_atc").require_endpoint(_ICOS_LOGIN_URL)

    def token(self) -> str:
        with self._lock:
            if self._password and (
                not self._token or time.time() >= self._expires_at - _ICOS_REFRESH_MARGIN_S
            ):
                self._login_unlocked()
            if not self._token:
                raise DeviceOffline("ICOS auth: no cpauth token")
            return self._token

    def invalidate(self) -> None:
        """Force refresh on next use (e.g. after a licence-gate redirect)."""
        with self._lock:
            if self._password:
                self._expires_at = 0.0

    def _login_unlocked(self) -> None:
        require_approved_source("icos_atc").require_endpoint(_ICOS_LOGIN_URL)
        url = _assert_url_allowed(_ICOS_LOGIN_URL)
        try:
            resp = httpx.post(
                url,
                data={"mail": self._email, "password": self._password},
                headers={
                    "User-Agent": _UA,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30.0,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise DeviceOffline(
                f"ICOS login unreachable ({type(exc).__name__})"
            ) from exc
        if resp.status_code not in (200, 204, 302, 303):
            raise DeviceOffline(f"ICOS login HTTP {resp.status_code}")
        cookie = resp.cookies.get("cpauthToken") or ""
        if not cookie:
            for key, value in resp.headers.multi_items():
                if key.lower() == "set-cookie" and "cpauthToken=" in value:
                    cookie = value.split("cpauthToken=", 1)[1].split(";", 1)[0].strip()
                    break
        if not cookie or not _SAFE_TOKEN.fullmatch(cookie):
            raise DeviceOffline(
                "ICOS login did not return cpauthToken (accept licence at "
                "https://cpauth.icos-cp.eu/home/ after creating the account)"
            )
        self._token = cookie
        self._expires_at = time.time() + _ICOS_TOKEN_TTL_S
        log.info("ICOS cpauth token refreshed for %s", self._email)


class IcosGhg(LiveDevice):
    """ICOS ATC near-real-time atmospheric CO₂ — CC BY 4.0 (token required)."""

    model = "GAIA-GHG (ICOS ATC NRT)"
    policy_id = "icos_atc"
    fields = {
        "co2_ppm": "ppm",
        "latitude": "deg",
        "longitude": "deg",
    }
    source = (
        "https://data.icos-cp.eu "
        "(ICOS Carbon Portal ATC NRT CO₂ mole fractions; CC BY 4.0 — cite ICOS and the "
        "object PID/DOI from metadata. Atmospheric GHG in-situ — not PM2.5 / OpenAQ. "
        "Licence: https://www.icos-cp.eu/data-services/data-portal/data-licence)"
    )
    timeout = 45.0

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        station_code: str,
        latitude: float,
        longitude: float,
        auth: IcosCpAuth | None = None,
        cpauth_token: str = "",
        **kw: Any,
    ):
        super().__init__(device_id, clock, **kw)
        code = (station_code or "").strip().upper()
        if not _SAFE_ICOS_STA.fullmatch(code):
            raise ValueError(f"invalid ICOS station code: {station_code!r}")
        if auth is None:
            auth = IcosCpAuth(token=cpauth_token)
        self.auth = auth
        self.station_code = code
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.url = "https://data.icos-cp.eu/objects/"
        require_approved_source(self.policy_id).require_endpoint(self.url)
        require_approved_source(self.policy_id).require_endpoint(_ICOS_SPARQL)

    def _sparql_latest_object(self) -> tuple[str, str]:
        # Match NRT CO2 zip whose filename contains the 3-letter station code.
        query = f"""
PREFIX cpmeta: <http://meta.icos-cp.eu/ontologies/cpmeta/>
SELECT ?dobj ?fn WHERE {{
  ?dobj cpmeta:hasObjectSpec <{_ICOS_NRT_SPEC}> ;
        cpmeta:hasName ?fn .
  FILTER(CONTAINS(?fn, "_{self.station_code}_") && CONTAINS(LCASE(?fn), "co2"))
}}
ORDER BY DESC(?fn)
LIMIT 1
"""
        require_approved_source(self.policy_id).require_endpoint(_ICOS_SPARQL)
        try:
            resp = httpx.post(
                _ICOS_SPARQL,
                data={"query": query},
                headers={"Accept": "application/sparql-results+json", "User-Agent": _UA},
                timeout=self.timeout,
                follow_redirects=False,
            )
            if resp.status_code != 200:
                raise DeviceOffline(f"{self.device_id}: ICOS SPARQL HTTP {resp.status_code}")
            payload = resp.json()
        except DeviceOffline:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DeviceOffline(
                f"{self.device_id}: ICOS SPARQL unreachable ({type(exc).__name__})"
            ) from exc
        bindings = (((payload or {}).get("results") or {}).get("bindings")) or []
        if not bindings or not isinstance(bindings[0], dict):
            raise DeviceOffline(f"{self.device_id}: no ICOS NRT CO2 object for {self.station_code}")
        dobj = ((bindings[0].get("dobj") or {}).get("value")) or ""
        fn = ((bindings[0].get("fn") or {}).get("value")) or ""
        # https://meta.icos-cp.eu/objects/<id> → download host data.icos-cp.eu/objects/<id>
        obj_id = dobj.rsplit("/", 1)[-1]
        if not obj_id or "/" in obj_id:
            raise DeviceOffline(f"{self.device_id}: bad ICOS object id")
        return obj_id, fn

    def _download_zip(self, object_id: str) -> bytes:
        url = f"https://data.icos-cp.eu/objects/{quote(object_id, safe='')}"
        require_approved_source(self.policy_id).require_endpoint(url)
        url = _assert_url_allowed(url)
        try:
            token = self.auth.token()
            resp = httpx.get(
                url,
                headers={"User-Agent": _UA, "Cookie": f"cpauthToken={token}"},
                timeout=self.timeout,
                follow_redirects=False,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                # Licence gate or expired cookie — one forced refresh then retry.
                self.auth.invalidate()
                token = self.auth.token()
                resp = httpx.get(
                    url,
                    headers={"User-Agent": _UA, "Cookie": f"cpauthToken={token}"},
                    timeout=self.timeout,
                    follow_redirects=False,
                )
            if resp.status_code in (301, 302, 303, 307, 308):
                raise DeviceOffline(
                    f"{self.device_id}: ICOS licence gate (accept "
                    "https://data.icos-cp.eu/licence in the CP profile for this account)"
                )
            if resp.status_code != 200:
                raise DeviceOffline(f"{self.device_id}: ICOS download HTTP {resp.status_code}")
            data = resp.content
            if len(data) > 8 * 1024 * 1024:
                raise DeviceOffline(f"{self.device_id}: ICOS object too large")
            return data
        except DeviceOffline:
            raise
        except httpx.HTTPError as exc:
            raise DeviceOffline(
                f"{self.device_id}: ICOS download unreachable ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _latest_co2_from_zip(blob: bytes) -> float | None:
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                # Live ATC NRT objects ship as ``*.CO2`` (semicolon ATC table).
                names = [
                    n
                    for n in zf.namelist()
                    if n.lower().endswith((".co2", ".csv", ".txt"))
                ]
                if not names:
                    return None
                text = zf.read(names[0]).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, KeyError, UnicodeError):
            return None
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        if not lines:
            return None
        newest: tuple[float, float] | None = None

        # ATC NRT continuous series: Site;Height;Y;M;D;H;Min;DecimalDate;Value;…;Flag;…
        if ";" in lines[0] and "," not in lines[0].split(";")[0]:
            for ln in lines:
                parts = ln.split(";")
                if len(parts) < 12:
                    continue
                flag = parts[11].strip().upper()
                if flag and flag not in ("O", "U", "R"):
                    continue
                co2 = _num(parts[8])
                if co2 is None or co2 < 0:
                    continue
                try:
                    y, mo, d, h, mi = (int(parts[i]) for i in range(2, 7))
                    when = datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()
                except (TypeError, ValueError, OSError):
                    when = _num(parts[7]) or 0.0
                candidate = (float(when), float(co2))
                if newest is None or candidate[0] >= newest[0]:
                    newest = candidate
            return newest[1] if newest else None

        reader = csv.DictReader(io.StringIO("\n".join(lines)))
        for row in reader:
            if not isinstance(row, dict):
                continue
            # Prefer quality flag O / U when present.
            flag = str(row.get("Flag") or row.get("flag") or "O").strip().upper()
            if flag and flag not in ("O", "U", "R", ""):
                continue
            co2 = None
            for key in ("co2", "CO2", "co2_ppm", "mole_fraction_of_carbon_dioxide_in_air"):
                if key in row:
                    co2 = _num(row.get(key))
                    break
            if co2 is None:
                continue
            when = 0.0
            for key in ("TIMESTAMP", "DateTime", "datetime", "time", "Date"):
                if key in row:
                    when = _iso_epoch(row.get(key)) or 0.0
                    break
            candidate = (when, float(co2))
            if newest is None or candidate[0] >= newest[0]:
                newest = candidate
        return newest[1] if newest else None

    def map(self, payload: Any) -> dict[str, float | None]:
        # sample() passes co2 float directly; tests may pass dict.
        if isinstance(payload, dict):
            co2 = _num(payload.get("co2_ppm"))
        else:
            co2 = _num(payload)
        if co2 is None:
            raise DeviceOffline(f"{self.device_id}: ICOS CO2 missing")
        return {
            "co2_ppm": float(co2),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def sample(self) -> dict[str, float]:
        object_id, _fn = self._sparql_latest_object()
        blob = self._download_zip(object_id)
        co2 = self._latest_co2_from_zip(blob)
        if co2 is None:
            raise DeviceOffline(f"{self.device_id}: ICOS zip had no usable CO2")
        return {k: v for k, v in self.map(co2).items() if v is not None}


# ── Meshes + registration ─────────────────────────────────────────────────────

CDIP_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("cdip-pointreyes-01", "029", 37.9415, -123.4645, "Point Reyes, CA"),
    ("cdip-santamonica-01", "028", 33.8599, -118.6411, "Santa Monica Bay, CA"),
    ("cdip-torreypines-01", "100", 32.9300, -117.3920, "Torrey Pines Outer, CA"),
    ("cdip-capemendocino-01", "094", 40.2915, -124.7475, "Cape Mendocino, CA"),
    ("cdip-pointsur-01", "157", 36.3416, -122.1096, "Point Sur, CA"),
)

# Station codes + anchors from ICOS ATC network (Europe).
ICOS_MESH: tuple[tuple[str, str, float, float, str], ...] = (
    ("icos-htm-01", "HTM", 56.0976, 13.4189, "Hyltemossa, SE"),
    ("icos-zsf-01", "ZSF", 47.4165, 10.9796, "Zugspitze, DE"),
    ("icos-lin-01", "LIN", 52.1663, 14.1226, "Lindenberg, DE"),
)


def register_p8_relays(fleet: Any, clock: SimClock, *, key_dir: str) -> int:
    """Register P8 relays and return the number added."""

    def enabled(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() in ("1", "true", "yes", "on")

    n = 0
    if enabled("GAIA_INGV_ENABLED", "1"):
        try:
            fleet.add(IngvQuake("ingv-01", clock, site="live-quake-ingv", key_dir=key_dir))
            n += 1
        except ValueError as exc:
            log.warning("INGV skipped: %s", exc)

    if enabled("GAIA_CDIP_ENABLED", "1"):
        for device_id, station_id, lat, lon, _place in CDIP_MESH:
            try:
                fleet.add(
                    CdipWave(
                        device_id,
                        clock,
                        station_id=station_id,
                        latitude=lat,
                        longitude=lon,
                        site=f"live-marine-{device_id}",
                        key_dir=key_dir,
                    )
                )
                n += 1
            except ValueError as exc:
                log.warning("CDIP %s skipped: %s", device_id, exc)

    icos_email = _env("GAIA_ICOS_EMAIL")
    icos_password = _env("GAIA_ICOS_PASSWORD")
    icos_token = _env("GAIA_ICOS_CPAUTH_TOKEN")
    if enabled("GAIA_ICOS_ENABLED", "1") and ((icos_email and icos_password) or icos_token):
        try:
            auth = IcosCpAuth(email=icos_email, password=icos_password, token=icos_token)
        except ValueError as exc:
            log.warning("ICOS auth skipped: %s", exc)
            auth = None
        if auth is not None:
            for device_id, code, lat, lon, _place in ICOS_MESH:
                try:
                    fleet.add(
                        IcosGhg(
                            device_id,
                            clock,
                            station_code=code,
                            auth=auth,
                            latitude=lat,
                            longitude=lon,
                            site=f"live-ghg-{device_id}",
                            key_dir=key_dir,
                        )
                    )
                    n += 1
                except ValueError as exc:
                    log.warning("ICOS %s skipped: %s", device_id, exc)
    elif enabled("GAIA_ICOS_ENABLED", "1"):
        log.info(
            "ICOS skipped (set GAIA_ICOS_EMAIL+GAIA_ICOS_PASSWORD after accepting "
            "https://data.icos-cp.eu/licence, or GAIA_ICOS_CPAUTH_TOKEN from "
            "cpauth.icos-cp.eu/home/)"
        )

    return n


__all__ = [
    "IngvQuake",
    "CdipWave",
    "IcosCpAuth",
    "IcosGhg",
    "CDIP_MESH",
    "ICOS_MESH",
    "register_p8_relays",
]
