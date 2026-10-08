"""Fleet — the gateway's device registry and rolling reading history.

The history ring buffers double as the plausibility verifier's evidence base:
z-scores against a device's own recent past, rate-of-change against its last
reading, and cross-checks against co-located siblings all read from here.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from gaia.devices.base import DeviceOffline, VirtualDevice

_HISTORY = 512  # per device — plenty for rolling stats, bounded for memory


class Fleet:
    def __init__(self) -> None:
        self._devices: dict[str, VirtualDevice] = {}
        self._history: dict[str, deque[dict[str, Any]]] = {}

    # ── Registry ────────────────────────────────────────────────────────────

    def add(self, device: VirtualDevice) -> None:
        if device.device_id in self._devices:
            raise ValueError(f"duplicate device_id: {device.device_id}")
        self._devices[device.device_id] = device
        self._history[device.device_id] = deque(maxlen=_HISTORY)

    def get(self, device_id: str) -> VirtualDevice:
        try:
            return self._devices[device_id]
        except KeyError:
            raise ValueError(f"unknown device: {device_id}") from None

    def devices(self) -> list[VirtualDevice]:
        return list(self._devices.values())

    def siblings(self, device_id: str) -> list[VirtualDevice]:
        """Co-located devices of the SAME model family (share physical truth)."""
        me = self.get(device_id)
        return [
            d for d in self._devices.values()
            if d.device_id != device_id and d.site == me.site and type(d) is type(me)
        ]

    # ── Reading + history ───────────────────────────────────────────────────

    def read(self, device_id: str) -> dict[str, Any]:
        """Read one device and record the reading in its history."""
        device = self.get(device_id)
        result = device.read()  # may raise DeviceOffline
        self._history[device_id].append(result["reading"])
        return result

    def history(self, device_id: str) -> list[dict[str, Any]]:
        return list(self._history.get(device_id, ()))

    def last_reading(self, device_id: str) -> dict[str, Any] | None:
        h = self._history.get(device_id)
        return h[-1] if h else None

    def status(self) -> dict[str, Any]:
        out = []
        for d in self._devices.values():
            offline = d.fault.kind == "dropout"
            out.append({
                "device_id": d.device_id,
                "model": d.model,
                "site": d.site,
                "firmware": d.firmware,
                "fields": dict(d.fields),
                "device_pubkey": d.signer.public_key_b64,
                # Live relay devices carry an upstream provenance string (source
                # URL + licence); simulators have none. Surfaced so a buyer sees
                # whose data a reading relays. See gaia.devices.live.
                "source": getattr(d, "source", None) or None,
                "fault": d.fault.kind,
                "online": not offline,
                "readings_recorded": len(self._history.get(d.device_id, ())),
            })
        return {"devices": out, "count": len(out)}


__all__ = ["Fleet", "DeviceOffline"]
