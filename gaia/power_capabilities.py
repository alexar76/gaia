"""Electricity price capabilities over the SMARD day-ahead relays (devices/live_power.py).

    gaia.power.price.read@v1  $0.001  attested reading for one bidding zone: the price of
                                      the current quarter-hour, the min/mean/max of the
                                      next 24 h, how many of those intervals are negative
    gaia.power.window@v1      $0.002  the cheapest contiguous window of N hours in the
                                      published horizon, against starting now, with the
                                      prices it was computed from and their SHA-256 —
                                      the buyer can recompute the answer

A zone is named directly ("DE-LU", "FR"), by a country that is one zone ("DE",
"Germany"), by a city in such a country through the gazetteer ("Lyon"), or by an exact
device_id. Countries split into several zones (DK, NO, SE, IT) need the zone, and are
told which ones GAIA relays.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import TYPE_CHECKING, Any

from gaia.devices.live_power import (
    COUNTRY_ZONE,
    RESTRICTED_ZONES,
    SMARD_ZONES,
    SPLIT_COUNTRIES,
    PriceSeries,
    SmardDayAheadPrice,
    device_id_for,
    zone_for,
)

if TYPE_CHECKING:  # pragma: no cover
    from gaia.capabilities import GatewayRuntime

ZONE_TZ: dict[str, str] = {
    "DE-LU": "Europe/Berlin", "AT": "Europe/Vienna", "BE": "Europe/Brussels",
    "NO2": "Europe/Oslo", "DK1": "Europe/Copenhagen", "DK2": "Europe/Copenhagen",
    "FR": "Europe/Paris", "IT-North": "Europe/Rome", "NL": "Europe/Amsterdam",
    "PL": "Europe/Warsaw", "SE4": "Europe/Stockholm", "CH": "Europe/Zurich",
    "SI": "Europe/Ljubljana", "CZ": "Europe/Prague", "HU": "Europe/Budapest",
}
MAX_WINDOW_H = 24.0
MAX_LOOKAHEAD_H = 48.0

_ZONE_PROP = {
    "type": "string", "enum": list(SMARD_ZONES),
    "description": "bidding zone; or pass country / city / device_id instead",
    "default": "DE-LU",
}
_WHERE_PROPS: dict[str, Any] = {
    "zone": _ZONE_PROP,
    "country": {"type": "string", "maxLength": 60,
                "description": "a country that is one bidding zone: DE, AT, BE, FR, NL, PL, "
                               "CH, SI, CZ, HU (ISO code or name)"},
    "city": {"type": "string", "maxLength": 160,
             "description": "a city in such a country, e.g. \"Lyon\" (needs the gazetteer)"},
    "device_id": {"type": "string", "description": "smard-price-<zone>, e.g. smard-price-de-lu"},
}
_WHERE_ANY = [{"required": ["zone"]}, {"required": ["country"]}, {"required": ["city"]},
              {"required": ["device_id"]}]

PRICE_READ_IN: dict[str, Any] = {"type": "object", "properties": _WHERE_PROPS, "anyOf": _WHERE_ANY}
PRICE_READ_OUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reading": {"type": "object", "description": "attested values: price_eur_mwh, "
                    "price_min/mean/max_24h_eur_mwh, negative_intervals_24h, …"},
        "attestation": {"type": "object"},
        "zone": {"type": "object"},
    },
}
WINDOW_IN: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_WHERE_PROPS,
        "duration_hours": {"type": "number", "minimum": 0.25, "maximum": MAX_WINDOW_H,
                           "default": 3, "description": "how long the job runs"},
        "within_hours": {"type": "number", "minimum": 0.25, "maximum": MAX_LOOKAHEAD_H,
                         "description": "only windows that end within this many hours "
                                        "(default: the whole published horizon)"},
        "load_kw": {"type": "number", "minimum": 0, "maximum": 1_000_000,
                    "description": "optional constant load, to price the energy at wholesale"},
    },
    "anyOf": _WHERE_ANY,
}
WINDOW_OUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "best": {"type": "object", "description": "start/end, mean price, energy cost"},
        "run_now": {"type": ["object", "null"]},
        "saving_eur_mwh": {"type": ["number", "null"]},
        "series": {"type": "array", "description": "[[interval start unix s, EUR/MWh], …] used"},
        "series_sha256": {"type": "string"},
        "source": {"type": "object"},
    },
}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _local(ts: float, zone: str) -> str | None:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(ts, ZoneInfo(ZONE_TZ[zone])).isoformat()
    except Exception:  # noqa: BLE001 — no tzdata in a slim image: UTC still answers
        return None


def _zone_from_country(text: str, runtime: "GatewayRuntime") -> str:
    code = str(text).strip().upper()
    gaz = getattr(runtime, "gazetteer", None)
    if len(code) != 2 and gaz is not None:
        targets = gaz.countries_named(text)
        code = next(iter(targets)) if len(targets) == 1 else code
    if code in COUNTRY_ZONE:
        return COUNTRY_ZONE[code]
    if code in SPLIT_COUNTRIES:
        raise ValueError(f"{code} has several bidding zones; pass zone= one of "
                         f"{', '.join(SPLIT_COUNTRIES[code])} (the ones GAIA relays)")
    if code in ("ES", "PT", "FI", "EE", "LT", "LV"):
        raise ValueError(f"{code} is not offered: its public price series is licensed for "
                         "private and internal use only; GAIA sells only CC BY 4.0 series")
    raise ValueError(f"no relayed bidding zone for country {str(text)[:40]!r}; GAIA relays "
                     f"{', '.join(SMARD_ZONES)} (CC BY 4.0 series only)")


def resolve_zone(raw: dict[str, Any], runtime: "GatewayRuntime") -> str:
    device_id = str(raw.get("device_id") or "").strip()
    if device_id:
        for zone in SMARD_ZONES:
            if device_id_for(zone) == device_id:
                return zone
        raise ValueError(f"{device_id[:60]!r} is not a power price relay; use smard-price-<zone>")
    if raw.get("zone"):
        zone = zone_for(str(raw["zone"]))
        if zone is None and str(raw["zone"]).strip().upper() in RESTRICTED_ZONES:
            raise ValueError(
                f"zone {str(raw['zone']).strip()[:30]} is not offered: its public price series "
                "is licensed for private and internal use only, and GAIA sells only CC BY 4.0 "
                f"series ({', '.join(SMARD_ZONES)})")
        if zone is None:
            raise ValueError(f"unknown bidding zone {str(raw['zone'])[:30]!r}; GAIA relays "
                             f"{', '.join(SMARD_ZONES)}")
        return zone
    if raw.get("country"):
        return _zone_from_country(str(raw["country"]), runtime)
    city = raw.get("city", raw.get("place"))
    if city:
        gaz = getattr(runtime, "gazetteer", None)
        if gaz is None:
            raise ValueError("this GAIA has no gazetteer to place a city; pass zone or country")
        place = gaz.require(str(city))
        return _zone_from_country(place.country_code, runtime)
    raise ValueError("name the bidding zone: zone (\"DE-LU\", \"FR\", …), country "
                     "(\"DE\"), city (\"Lyon\") or device_id")


def _device(runtime: "GatewayRuntime", zone: str) -> SmardDayAheadPrice:
    try:
        device = runtime.fleet.get(device_id_for(zone))
    except ValueError:
        raise ValueError(f"zone {zone} is not enabled on this GAIA (GAIA_SMARD_ZONES)") from None
    if not isinstance(device, SmardDayAheadPrice):
        raise ValueError(f"{device_id_for(zone)} is not a SMARD price relay")
    return device


def _zone_info(zone: str) -> dict[str, Any]:
    fid, name, _lat, _lon = SMARD_ZONES[zone]
    return {"zone": zone, "name": name, "smard_series": fid, "timezone": ZONE_TZ[zone],
            "device_id": device_id_for(zone)}


def price_read_handler(runtime: "GatewayRuntime"):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        zone = resolve_zone(raw, runtime)
        _device(runtime, zone)
        out = runtime.read(device_id_for(zone))
        return {**out, "zone": _zone_info(zone)}
    return handler


def cheapest_window(series: PriceSeries, *, now: float, duration_s: float,
                    within_s: float | None = None) -> dict[str, Any]:
    """The contiguous run of ceil(duration/step) intervals with the lowest mean price.

    Windows start at the current interval or later and end by the horizon (or by
    now + within). A gap in the published series breaks a window. Ties go to the earlier
    start. Pure: the same series and inputs give the same answer.
    """
    step = series.step_s
    k = max(1, math.ceil(duration_s / step - 1e-9))
    current = series.at(now)
    first = current[0] if current else None
    if first is None:
        raise ValueError("no published price for the current interval")
    limit = series.horizon_end if within_s is None else min(series.horizon_end, now + within_s)
    pts = [(t, p) for t, p in series.points if t >= first and t + step <= limit]
    if len(pts) < k:
        raise ValueError(
            f"the published horizon holds {len(pts) * step / 3600:.2f} h from now "
            f"(until {_iso(series.horizon_end)}), shorter than {k * step / 3600:.2f} h; "
            "tomorrow's prices appear around 13:00 CET")
    best: tuple[float, int] | None = None
    run_now: float | None = None
    for i in range(len(pts) - k + 1):
        window = pts[i:i + k]
        if window[-1][0] - window[0][0] != (k - 1) * step:
            continue  # a gap: not k contiguous intervals
        mean = sum(p for _t, p in window) / k
        if i == 0:
            run_now = mean
        if best is None or mean < best[0] - 1e-12:
            best = (mean, i)
    if best is None:
        raise ValueError("no contiguous window of that length in the published series")
    mean, i = best
    window = pts[i:i + k]
    return {"k": k, "best_mean": mean, "best_start": window[0][0],
            "best_end": window[-1][0] + step, "run_now_mean": run_now,
            "best_points": window, "used": pts}


def window_handler(runtime: "GatewayRuntime"):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        try:
            duration_h = float(raw.get("duration_hours", 3))
            within_h = raw.get("within_hours")
            within_h = None if within_h is None else float(within_h)
            load_kw = raw.get("load_kw")
            load_kw = None if load_kw is None else float(load_kw)
        except (TypeError, ValueError) as exc:
            raise ValueError("duration_hours, within_hours and load_kw must be numbers") from exc
        if not (math.isfinite(duration_h) and 0.25 <= duration_h <= MAX_WINDOW_H):
            raise ValueError(f"duration_hours must be from 0.25 to {MAX_WINDOW_H:g}")
        if within_h is not None and not (math.isfinite(within_h)
                                         and 0.25 <= within_h <= MAX_LOOKAHEAD_H):
            raise ValueError(f"within_hours must be from 0.25 to {MAX_LOOKAHEAD_H:g}")
        if load_kw is not None and not (math.isfinite(load_kw) and 0 <= load_kw <= 1_000_000):
            raise ValueError("load_kw must be from 0 to 1,000,000")
        zone = resolve_zone(raw, runtime)
        device = _device(runtime, zone)
        series = device.series()
        now = runtime.clock.now()
        w = cheapest_window(series, now=now, duration_s=duration_h * 3600,
                            within_s=None if within_h is None else within_h * 3600)
        step_h = series.step_s / 3600.0

        def cost(points: list[tuple[int, float]]) -> float | None:
            if load_kw is None:
                return None
            return round(sum(p for _t, p in points) * load_kw * step_h / 1000.0, 4)

        used = [[t, p] for t, p in w["used"]]
        digest = hashlib.sha256(json.dumps(used, separators=(",", ":")).encode()).hexdigest()
        best = {
            "start_unix": w["best_start"], "end_unix": w["best_end"],
            "start_iso": _iso(w["best_start"]), "end_iso": _iso(w["best_end"]),
            "start_local": _local(w["best_start"], zone),
            "mean_eur_mwh": round(w["best_mean"], 4),
            "intervals": w["k"], "hours": round(w["k"] * step_h, 4),
            "energy_cost_eur_wholesale": cost(w["best_points"]),
        }
        run_now = None
        saving = saving_pct = None
        if w["run_now_mean"] is not None:
            now_points = w["used"][:w["k"]]
            run_now = {
                "start_unix": now_points[0][0], "start_iso": _iso(now_points[0][0]),
                "mean_eur_mwh": round(w["run_now_mean"], 4),
                "energy_cost_eur_wholesale": cost(now_points),
            }
            saving = round(w["run_now_mean"] - w["best_mean"], 4)
            if w["run_now_mean"] > 0:
                saving_pct = round(100.0 * saving / w["run_now_mean"], 2)
        return {
            "zone": _zone_info(zone),
            "requested": {"duration_hours": duration_h, "within_hours": within_h,
                          "load_kw": load_kw},
            "best": best,
            "run_now": run_now,
            "saving_eur_mwh": saving,
            "saving_pct": saving_pct,
            "horizon_end_iso": _iso(series.horizon_end),
            "resolution_min": series.step_s / 60.0,
            "series": used,
            "series_sha256": digest,
            "computed_at": _iso(now),
            "source": {"urls": list(series.sources), "text": device.source,
                       "attribution": device.attribution},
            "note": "wholesale day-ahead price; a retail bill adds grid fees, levies and VAT",
        }
    return handler


__all__ = ["PRICE_READ_IN", "PRICE_READ_OUT", "WINDOW_IN", "WINDOW_OUT", "ZONE_TZ",
           "cheapest_window", "price_read_handler", "resolve_zone", "window_handler"]
