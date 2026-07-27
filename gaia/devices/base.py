"""Virtual device base — identity, sampling contract, fault injection.

A device is a tiny physical simulator plus an Ed25519 identity. ``read()``
produces one attested reading; ``inject_fault()`` turns the device into a liar
in one of four realistic ways so the plausibility verifier (and the hub's
Pay-on-Verified escrow behind it) has something worth catching:

    stuck    — the field freezes at its last value (dead sensor / ADC latch-up)
    spike    — a one-off absurd excursion (loose wire, EMI burst)
    drift    — a slowly growing bias (miscalibration, ageing)
    dropout  — the device stops answering entirely (power/radio loss)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oracle_core.signing import Signer

from gaia.attestation import sign_reading
from gaia.clock import SimClock


class DeviceOffline(RuntimeError):
    """Raised by read() while a dropout fault is active."""


@dataclass
class FaultSpec:
    kind: str = "none"          # none | stuck | spike | drift | dropout
    fields: list[str] = field(default_factory=list)  # empty = all numeric fields
    magnitude: float = 0.0      # spike: absolute offset; drift: units per hour
    since_epoch: float = 0.0    # sim time the fault began (drift grows from here)


class VirtualDevice:
    """Base class: subclasses implement ``sample()`` returning field values."""

    model = "GAIA-GENERIC"
    fields: dict[str, str] = {}  # field -> unit (informational; schema comes from here)

    def __init__(
        self,
        device_id: str,
        clock: SimClock,
        *,
        site: str = "site-1",
        seed: int = 0,
        key_dir: str | Path = "data/devices",
        firmware: str = "1.0.0",
    ):
        self.device_id = device_id
        self.site = site
        self.clock = clock
        self.firmware = firmware
        self.rng = random.Random(f"{device_id}:{seed}")
        self.signer = Signer(Path(key_dir) / f"{device_id}.key")
        self.fault = FaultSpec()
        self._seq = 0
        self._last_values: dict[str, float] = {}

    # ── Physics contract ───────────────────────────────────────────────────

    def sample(self) -> dict[str, float]:  # pragma: no cover - abstract
        raise NotImplementedError

    # ── Fault injection (simulation control surface) ───────────────────────

    def inject_fault(self, kind: str, *, fields: list[str] | None = None, magnitude: float = 0.0) -> None:
        if kind not in ("none", "stuck", "spike", "drift", "dropout"):
            raise ValueError(f"unknown fault kind: {kind}")
        self.fault = FaultSpec(
            kind=kind, fields=list(fields or []),
            magnitude=magnitude, since_epoch=self.clock.now(),
        )

    def clear_fault(self) -> None:
        self.fault = FaultSpec()

    def _faulted(self, values: dict[str, float]) -> dict[str, float]:
        f = self.fault
        if f.kind == "none":
            return values
        if f.kind == "dropout":
            raise DeviceOffline(f"{self.device_id} is offline (dropout fault)")
        targets = f.fields or list(values.keys())
        out = dict(values)
        if f.kind == "stuck" and self._last_values:
            for k in targets:
                if k in self._last_values:
                    out[k] = self._last_values[k]
        elif f.kind == "spike":
            for k in targets:
                out[k] = out[k] + f.magnitude
        elif f.kind == "drift":
            hours = max(0.0, (self.clock.now() - f.since_epoch) / 3600.0)
            for k in targets:
                out[k] = out[k] + f.magnitude * hours
        return out

    # ── Reading production ──────────────────────────────────────────────────

    def read(self) -> dict[str, Any]:
        """One attested reading: values + device metadata + device signature."""
        honest = self.sample()
        values = {k: round(v, 4) for k, v in self._faulted(honest).items()}
        self._seq += 1
        reading = {
            "device_id": self.device_id,
            "model": self.model,
            "site": self.site,
            "firmware": self.firmware,
            "seq": self._seq,
            "ts": self.clock.iso(),
            "values": values,
            "units": dict(self.fields),
        }
        attestation = sign_reading(reading, self.signer)
        # Remember post-fault values so `stuck` freezes what the world last saw.
        self._last_values = dict(values)
        return {"reading": reading, "attestation": attestation}

    # ── Shared physics helpers ───────────────────────────────────────────────

    def noise(self, sigma: float) -> float:
        return self.rng.gauss(0.0, sigma)


class OrnsteinUhlenbeck:
    """Mean-reverting random walk — the workhorse of believable slow processes
    (pressure fronts, mains voltage wander, background PM levels)."""

    def __init__(self, rng: random.Random, mean: float, theta: float, sigma: float, x0: float | None = None):
        self.rng = rng
        self.mean = mean
        self.theta = theta   # pull-back strength per hour
        self.sigma = sigma   # diffusion per sqrt(hour)
        self.x = mean if x0 is None else x0
        self._last_t: float | None = None

    def value(self, t_epoch: float) -> float:
        if self._last_t is None:
            self._last_t = t_epoch
            return self.x
        dt_h = max(0.0, (t_epoch - self._last_t) / 3600.0)
        self._last_t = t_epoch
        if dt_h == 0.0:
            return self.x
        self.x += self.theta * (self.mean - self.x) * dt_h + self.sigma * (dt_h ** 0.5) * self.rng.gauss(0, 1)
        return self.x
