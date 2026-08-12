"""Own-edge feeder ingest — operator dump1090 / AIS push → LIVE devices.

Only **own** feeds are commercializeable here. Do not wire third-party NC
aggregators (ADSBx commercial, aisstream-as-sole-SKU) as the provenance source.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live import LiveDevice, _env, _num

log = logging.getLogger("gaia.feeder")

_ADS_B_FIELDS = {
    "latitude": "deg",
    "longitude": "deg",
    "altitude_m": "m",
    "speed_mps": "m/s",
}
_AIS_FIELDS = {
    "latitude": "deg",
    "longitude": "deg",
    "sog_knots": "kn",
    "cog_deg": "deg",
}

_ALLOWED_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    "adsb": frozenset(_ADS_B_FIELDS),
    "ais": frozenset(_AIS_FIELDS),
}

_MAX_AGE_S = 600.0  # stale ingest → DeviceOffline


class FeederStore:
    """Thread-safe latest-reading store keyed by device_id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, dict[str, Any]] = {}

    def put(self, device_id: str, fields: dict[str, float], *, observed_at: float | None = None) -> dict[str, Any]:
        now = time.time()
        rec = {
            "device_id": device_id,
            "fields": {k: float(v) for k, v in fields.items()},
            "observed_at": float(observed_at) if observed_at is not None else now,
            "ingested_at": now,
        }
        with self._lock:
            self._latest[device_id] = rec
        return rec

    def get(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._latest.get(device_id)
            return dict(rec) if rec else None

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()


# Process-wide store (one GAIA worker).
STORE = FeederStore()


class FeederDevice(LiveDevice):
    """LIVE device whose sample comes from the last authenticated ingest push.

    Subclasses LiveDevice so warm-up skips it (no synthetic hammering) and
    ``source`` provenance flows through ``Fleet.status`` like other relays.
    """

    model = "GAIA-FEEDER"
    url = ""  # no upstream HTTP — ingest only

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        kind: str = "adsb",
        provenance: str = "",
        store: FeederStore | None = None,
        max_age_s: float = _MAX_AGE_S,
        **kw,
    ):
        super().__init__(device_id, clock, **kw)
        kind = kind.lower().strip()
        if kind not in _ALLOWED_FIELDS_BY_KIND:
            raise ValueError(f"unsupported feeder kind: {kind!r}")
        self.kind = kind
        self.fields = dict(_ADS_B_FIELDS if kind == "adsb" else _AIS_FIELDS)
        self._allowed = _ALLOWED_FIELDS_BY_KIND[kind]
        self._store = store or STORE
        self.max_age_s = float(max_age_s)
        if provenance:
            self.source = provenance
        elif kind == "adsb":
            self.source = (
                "operator edge feeder (own dump1090 / ADS-B receiver; "
                "not a third-party aggregator)"
            )
        else:
            self.source = (
                "operator edge feeder (own AIS receiver; "
                "not a third-party aggregator)"
            )

    def map(self, payload: Any) -> dict[str, float | None]:  # pragma: no cover
        raise NotImplementedError("FeederDevice uses ingest store, not HTTP map()")

    def sample(self) -> dict[str, float]:
        rec = self._store.get(self.device_id)
        if not rec:
            raise DeviceOffline(f"{self.device_id}: no feeder ingest yet")
        age = time.time() - float(rec.get("observed_at") or 0.0)
        if age > self.max_age_s:
            raise DeviceOffline(
                f"{self.device_id}: feeder ingest stale ({age:.0f}s > {self.max_age_s:.0f}s)"
            )
        fields = rec.get("fields") or {}
        out: dict[str, float] = {}
        for k, v in fields.items():
            if k not in self._allowed:
                continue
            n = _num(v)
            if n is not None:
                out[k] = n
        if "latitude" not in out or "longitude" not in out:
            raise DeviceOffline(f"{self.device_id}: feeder payload missing lat/lon")
        return out


def ingest(
    device_id: str,
    fields: dict[str, Any],
    *,
    observed_at: float | None = None,
    allowed_devices: dict[str, FeederDevice] | None = None,
) -> dict[str, Any]:
    """Validate and store a push. ``allowed_devices`` maps id → FeederDevice."""
    if allowed_devices is not None and device_id not in allowed_devices:
        raise KeyError(f"unknown feeder device: {device_id}")
    device = (allowed_devices or {}).get(device_id)
    allowed = device._allowed if device else frozenset(fields)
    cleaned: dict[str, float] = {}
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
        n = _num(v)
        if n is not None:
            cleaned[k] = n
    if "latitude" not in cleaned or "longitude" not in cleaned:
        raise ValueError("fields must include numeric latitude and longitude")
    if not (-90.0 <= cleaned["latitude"] <= 90.0 and -180.0 <= cleaned["longitude"] <= 180.0):
        raise ValueError("latitude/longitude out of range")
    store = device._store if device is not None else STORE
    return store.put(device_id, cleaned, observed_at=observed_at)


def register_feeders(fleet: Any, clock: SimClock, *, key_dir: str) -> dict[str, FeederDevice]:
    """Register feeder devices when GAIA_FEEDER_ENABLED=1. Returns id→device map."""
    if _env("GAIA_FEEDER_ENABLED", "0").lower() not in ("1", "true", "yes", "on"):
        return {}
    out: dict[str, FeederDevice] = {}
    adsb = FeederDevice(
        "feeder-adsb-01",
        clock,
        kind="adsb",
        site="live-feeder-adsb",
        key_dir=key_dir,
    )
    ais = FeederDevice(
        "feeder-ais-01",
        clock,
        kind="ais",
        site="live-feeder-ais",
        key_dir=key_dir,
    )
    fleet.add(adsb)
    fleet.add(ais)
    out[adsb.device_id] = adsb
    out[ais.device_id] = ais
    log.info("Registered edge feeder devices: %s", ", ".join(out))
    return out


def feeder_token() -> str:
    return os.environ.get("GAIA_FEEDER_TOKEN", "").strip()


__all__ = [
    "FeederDevice",
    "FeederStore",
    "STORE",
    "ingest",
    "register_feeders",
    "feeder_token",
]
