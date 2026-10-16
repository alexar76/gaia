"""gaia.place.resolve@v1 — a place name or a coordinate → one place, and what GAIA sells there.

Free, because it is the input layer for everything priced: a model that has a city name
and no coordinates asks here first, learns where the place is, whether the name was
ambiguous, which weather relay is nearest and how far, and that air quality reads at the
exact point. Backed by the GeoNames gazetteer (gaia/gazetteer.py, CC BY 4.0); advertised
only when GAIA_GAZETTEER_PATH points at a built gazetteer.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from gaia.gazetteer import Gazetteer, GazetteerError, Place, haversine_km

if TYPE_CHECKING:  # pragma: no cover
    from gaia.capabilities import GatewayRuntime

#: Same reach as gaia.weather.read's place matching (capabilities._NEAREST_RELAY_MAX_KM).
WEATHER_RELAY_MAX_KM = 75.0

PLACE_RESOLVE_IN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string", "maxLength": 160,
            "description": "place name in any common spelling, optionally with the state "
                           "or country after a comma: \"Kraków\", \"Paris, Texas\", "
                           "\"Toronto, ON\", \"Москва\", \"東京\"",
            "default": "Kraków",
        },
        "country": {"type": "string", "maxLength": 60,
                    "description": "optional country (ISO code or name) to narrow the query"},
        "latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5,
                  "description": "how many candidates (by name) or nearby places (by point)"},
    },
    "anyOf": [{"required": ["query"]}, {"required": ["latitude", "longitude"]}],
}

PLACE_RESOLVE_OUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["name", "point"]},
        "chosen": {"type": ["object", "null"],
                   "description": "the place, or null when the name is ambiguous/unknown"},
        "ambiguous": {"type": "boolean"},
        "candidates": {"type": "array"},
        "readable_with": {"type": "object",
                          "description": "the GAIA reads that cover the chosen place"},
        "provenance": {"type": "object", "description": "GeoNames build, licence, attribution"},
    },
}


def _weather_relays(runtime: "GatewayRuntime") -> list[Any]:
    from gaia.devices.live_om import OpenMeteoWeather

    return [d for d in runtime.fleet.devices() if isinstance(d, OpenMeteoWeather)]


def readable_with(runtime: "GatewayRuntime", lat: float, lon: float) -> dict[str, Any]:
    """Which GAIA reads answer for this point — so the next call is a purchase, not a guess."""
    out: dict[str, Any] = {}
    relays = _weather_relays(runtime)
    if relays:
        device, km = min(((d, haversine_km(lat, lon, d.latitude, d.longitude)) for d in relays),
                         key=lambda pair: pair[1])
        out["gaia.weather.read@v1"] = {
            "covered": km <= WEATHER_RELAY_MAX_KM,
            "nearest_device_id": device.device_id,
            "distance_km": round(km, 1),
            "call": ({"latitude": round(lat, 5), "longitude": round(lon, 5)}
                     if km <= WEATHER_RELAY_MAX_KM else {"device_id": device.device_id}),
            "note": ("the nearest live relay is within reach" if km <= WEATHER_RELAY_MAX_KM
                     else f"no relay within {WEATHER_RELAY_MAX_KM:.0f} km; pass the device_id "
                          "to read the nearest one anyway"),
        }
    try:
        runtime.fleet.get("om-aq-01")
        out["gaia.air.read@v1"] = {
            "covered": True,
            "call": {"latitude": round(lat, 5), "longitude": round(lon, 5)},
            "note": "Open-Meteo air quality at the exact point",
        }
    except ValueError:
        pass
    return out


def place_resolve_handler(runtime: "GatewayRuntime", gazetteer: Gazetteer):
    def handler(data: dict[str, Any]) -> dict[str, Any]:
        raw = data if isinstance(data, dict) else {}
        try:
            limit = int(raw.get("limit") or 5)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer from 1 to 10") from exc
        limit = max(1, min(10, limit))
        lat, lon = raw.get("latitude"), raw.get("longitude")
        query = raw.get("query", raw.get("city", raw.get("place")))
        if lat is not None or lon is not None:
            if lat is None or lon is None:
                raise ValueError("latitude and longitude must be supplied together")
            try:
                lat_f, lon_f = float(lat), float(lon)
            except (TypeError, ValueError) as exc:
                raise ValueError("latitude and longitude must be numbers") from exc
            if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
                raise ValueError("latitude and longitude must be finite")
            near = gazetteer.nearest(lat_f, lon_f, limit=limit)
            places = [{**p.as_dict(), "distance_km": round(km, 2)} for p, km in near]
            return {
                "mode": "point",
                "query": {"latitude": lat_f, "longitude": lon_f},
                "chosen": places[0] if places else None,
                "ambiguous": False,
                "reason": ("nearest populated place" if places else
                           "no populated place of 1,000+ people within 100 km (open sea, "
                           "polar or desert); readings still work at the coordinate"),
                "candidates": places,
                "readable_with": readable_with(runtime, lat_f, lon_f),
                "provenance": gazetteer.provenance(),
            }
        if not isinstance(query, str) or not query.strip():
            raise ValueError("pass query (a place name) or latitude and longitude")
        country = raw.get("country")
        try:
            res = gazetteer.resolve(query, str(country) if country else "")
        except GazetteerError as exc:
            raise ValueError(str(exc)) from None
        body = res.as_dict(limit=limit)
        chosen: Place | None = res.chosen
        return {
            "mode": "name",
            **body,
            "readable_with": (readable_with(runtime, chosen.latitude, chosen.longitude)
                              if chosen else {}),
            "provenance": gazetteer.provenance(),
        }

    return handler


__all__ = ["PLACE_RESOLVE_IN", "PLACE_RESOLVE_OUT", "place_resolve_handler", "readable_with"]
