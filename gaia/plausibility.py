"""Statistical plausibility verifier for physical readings.

Deterministic, sub-millisecond, no LLM: physics bounds, robust z-scores
against the device's own recent history, rate-of-change limits, co-located
sibling agreement, stuck-sensor detection, and (for energy) register
monotonicity. Produces a score in [0, 1] plus named checks, which the HTTP
layer wraps into a Metis-compatible ``/v1/verify`` envelope — so the hub's
Pay-on-Verified escrow can point at GAIA exactly as it points at Metis.

Honesty note (also in the docs): thresholds below are calibrated to the GAIA
simulators; a real deployment calibrates per sensor datasheet, and the
verifier should run on an operator SEPARATE from the data seller — a provider
verifying its own goods is a conflict of interest. This module demonstrates
the interface and the math, not a trust topology.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from gaia.attestation import verify_reading
from gaia.fleet import Fleet

# Field physics: hard bounds, jitter floor (allowed step regardless of dt),
# max slope per minute, minimum std used for z-scores (avoids div-by-noise-free),
# and whether the field is continuous enough for stuck detection.
@dataclass(frozen=True)
class FieldPhysics:
    lo: float
    hi: float
    jitter_floor: float
    slope_per_min: float | None
    z_floor: float = 0.1
    stuck_check: bool = False
    monotonic: bool = False
    # z-scores are wrong for legitimately bursty processes (appliance loads,
    # wind gusts) — an honest kettle would be convicted by statistics.
    z_check: bool = True


PHYSICS: dict[str, FieldPhysics] = {
    "temperature_c": FieldPhysics(-60.0, 60.0, 1.0, 0.5, 0.15, stuck_check=True),
    "humidity_pct": FieldPhysics(0.0, 100.0, 6.0, 3.0, 1.0, stuck_check=True),
    "pressure_hpa": FieldPhysics(870.0, 1085.0, 1.5, 0.5, 0.2, stuck_check=True),
    "wind_mps": FieldPhysics(0.0, 75.0, 8.0, None, 0.5, z_check=False),
    "pm2_5_ugm3": FieldPhysics(0.0, 1000.0, 8.0, 6.0, 1.0, stuck_check=True),
    "pm10_ugm3": FieldPhysics(0.0, 2000.0, 14.0, 10.0, 1.5),
    "co2_ppm": FieldPhysics(350.0, 10_000.0, 60.0, 40.0, 10.0, stuck_check=True),
    "voc_index": FieldPhysics(0.0, 500.0, 40.0, None, 5.0),
    "voltage_v": FieldPhysics(180.0, 260.0, 4.0, 3.0, 0.5, stuck_check=True),
    "current_a": FieldPhysics(0.0, 64.0, 6.0, None, 0.05, z_check=False),
    "power_w": FieldPhysics(0.0, 15_000.0, 1500.0, None, 10.0, z_check=False),
    "energy_wh": FieldPhysics(0.0, 1e12, 0.0, None, monotonic=True),
}

# Hard checks disqualify their field outright: physics violations (bounds,
# register rollback) and the dead-sensor signature (a continuous field frozen
# to 4 decimals) are not statistical judgement calls.
_HARD_FAMILIES = ("bounds", "monotonic", "stuck", "schema")

# Sibling agreement tolerances — only fields where two co-located same-model
# sensors genuinely measure one shared truth.
SIBLING_TOLERANCE: dict[str, float] = {
    "temperature_c": 3.0,
    "humidity_pct": 12.0,
    "pressure_hpa": 2.5,
    "wind_mps": 6.0,
}

_Z_LIMIT = 5.0
_Z_MIN_HISTORY = 24
_Z_WINDOW = 64
_STUCK_RUN = 6
_SIBLING_MAX_AGE_S = 600.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class Verdict:
    verified: bool
    score: float
    checks: list[Check] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "score": self.score,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
        }


def _parse_ts(iso: str) -> float:
    import calendar
    import time as _time
    try:
        return calendar.timegm(_time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


class PlausibilityVerifier:
    def __init__(self, fleet: Fleet, threshold: float = 0.7):
        self.fleet = fleet
        self.threshold = threshold

    # ── Entry point ─────────────────────────────────────────────────────────

    def check(self, reading: dict[str, Any], attestation: dict[str, Any] | None = None,
              min_score: float | None = None, *, require_attestation: bool = True) -> Verdict:
        threshold = self.threshold if min_score is None else min_score
        checks: list[Check] = []

        device_id = str(reading.get("device_id", ""))
        try:
            device = self.fleet.get(device_id)
        except ValueError:
            return Verdict(False, 0.0, [Check("known_device", False, f"unknown device {device_id!r}")],
                           f"unknown device {device_id!r}")
        checks.append(Check("known_device", True, device.model))

        # Identity is decisive and FAIL-CLOSED. spec §4.3: every reading MUST carry a
        # device attestation. A missing attestation is not "unverified, score the
        # numbers" — it is a hard failure, exactly like a bad one: plausible numbers
        # from an unproven sensor are worth nothing, and omitting the field must never
        # be cheaper than forging it. require_attestation=False is reserved for callers
        # that verify identity out-of-band (there are none on the escrow path).
        if require_attestation and not isinstance(attestation, dict):
            checks.append(Check("device_attestation", False,
                                "no device attestation supplied (spec §4.3: every reading MUST carry one)"))
            return Verdict(False, 0.0, checks, "missing device attestation")
        if isinstance(attestation, dict):
            ok = verify_reading(reading, attestation, expected_pubkey=device.signer.public_key_b64)
            checks.append(Check("device_attestation", ok,
                                "signature verifies against the fleet-pinned device key" if ok
                                else "attestation does not verify against the pinned device key"))
            if not ok:
                return Verdict(False, 0.0, checks, "attestation failure")

        values = reading.get("values") or {}
        history = self._prior_history(device_id, reading)
        prev = history[-1] if history else None

        for fname, value in values.items():
            phys = PHYSICS.get(fname)
            if phys is None:
                checks.append(Check(f"schema:{fname}", False, "field not in physics table"))
                continue
            if not isinstance(value, (int, float)):
                checks.append(Check(f"schema:{fname}", False, "non-numeric value"))
                continue
            checks.append(self._bounds(fname, float(value), phys))
            if phys.monotonic and prev is not None:
                checks.append(self._monotonic(fname, float(value), prev))
                continue  # a register has no meaningful z/rate/jitter stats
            z = self._zscore(fname, float(value), phys, history)
            if z is not None:
                checks.append(z)
            r = self._rate(fname, float(value), phys, prev, reading)
            if r is not None:
                checks.append(r)
            if phys.stuck_check:
                s = self._stuck(fname, float(value), history)
                if s is not None:
                    checks.append(s)

        sib = self._siblings(device_id, values, reading)
        checks.extend(sib)

        checks.extend(self._electrical_consistency(values, prev, reading))

        score = self._score(checks)
        failed = [c.name for c in checks if not c.ok and ":" in c.name]
        summary = ("all plausibility checks passed" if not failed
                   else f"failed: {', '.join(failed[:6])}")
        return Verdict(score >= threshold, round(score, 4), checks, summary)

    @staticmethod
    def _score(checks: list[Check]) -> float:
        """Per-field scoring, overall = MIN over fields.

        A sensor lies one field at a time; averaging across fields would let
        three honest channels launder one fabricated one. Within a field: any
        HARD check failure zeroes it; soft checks (zscore/rate/sibling)
        average; a field with only passing hard checks scores 1.0.
        """
        by_field: dict[str, list[Check]] = {}
        for c in checks:
            if ":" not in c.name:
                continue
            by_field.setdefault(c.name.split(":", 1)[1], []).append(c)
        if not by_field:
            return 0.0
        field_scores: list[float] = []
        for fchecks in by_field.values():
            if any(not c.ok and c.name.split(":", 1)[0] in _HARD_FAMILIES for c in fchecks):
                field_scores.append(0.0)
                continue
            soft = [c for c in fchecks if c.name.split(":", 1)[0] not in _HARD_FAMILIES]
            field_scores.append(sum(1 for c in soft if c.ok) / len(soft) if soft else 1.0)
        return min(field_scores)

    # ── Individual checks ────────────────────────────────────────────────────

    def _prior_history(self, device_id: str, reading: dict[str, Any]) -> list[dict[str, Any]]:
        """History strictly BEFORE the reading under audit (drop it and anything
        newer by seq, so a reading is never judged against itself)."""
        seq = reading.get("seq", 0)
        return [h for h in self.fleet.history(device_id) if h.get("seq", 0) < seq]

    @staticmethod
    def _bounds(fname: str, value: float, phys: FieldPhysics) -> Check:
        ok = phys.lo <= value <= phys.hi
        return Check(f"bounds:{fname}", ok,
                     f"{value} within [{phys.lo}, {phys.hi}]" if ok
                     else f"{value} outside physical bounds [{phys.lo}, {phys.hi}]")

    @staticmethod
    def _monotonic(fname: str, value: float, prev: dict[str, Any]) -> Check:
        prev_v = prev.get("values", {}).get(fname)
        if not isinstance(prev_v, (int, float)):
            return Check(f"monotonic:{fname}", True, "no prior register value")
        ok = value >= float(prev_v) - 1e-9
        return Check(f"monotonic:{fname}", ok,
                     "register is non-decreasing" if ok
                     else f"register went backwards: {prev_v} -> {value}")

    @staticmethod
    def _zscore(fname: str, value: float, phys: FieldPhysics,
                history: list[dict[str, Any]]) -> Check | None:
        if not phys.z_check:
            return None  # bursty-by-nature field — statistics would convict honesty
        series = [h["values"][fname] for h in history[-_Z_WINDOW:]
                  if isinstance(h.get("values", {}).get(fname), (int, float))]
        if len(series) < _Z_MIN_HISTORY:
            return None  # not enough evidence — abstain rather than guess
        mean = statistics.fmean(series)
        std = max(statistics.pstdev(series), phys.z_floor)
        z = abs(value - mean) / std
        ok = z <= _Z_LIMIT
        return Check(f"zscore:{fname}", ok, f"|z|={z:.2f} vs window mean {mean:.2f}")

    @staticmethod
    def _rate(fname: str, value: float, phys: FieldPhysics,
              prev: dict[str, Any] | None, reading: dict[str, Any]) -> Check | None:
        if prev is None or phys.slope_per_min is None:
            return None
        prev_v = prev.get("values", {}).get(fname)
        if not isinstance(prev_v, (int, float)):
            return None
        dt_min = max(0.0, (_parse_ts(reading.get("ts", "")) - _parse_ts(prev.get("ts", ""))) / 60.0)
        allowed = phys.jitter_floor + phys.slope_per_min * dt_min
        delta = abs(value - float(prev_v))
        ok = delta <= allowed
        return Check(f"rate:{fname}", ok,
                     f"Δ={delta:.2f} over {dt_min:.1f} min (allowed {allowed:.2f})")

    @staticmethod
    def _stuck(fname: str, value: float, history: list[dict[str, Any]]) -> Check | None:
        tail = [h["values"][fname] for h in history[-(_STUCK_RUN - 1):]
                if isinstance(h.get("values", {}).get(fname), (int, float))]
        if len(tail) < _STUCK_RUN - 1:
            return None
        ok = not all(v == value for v in tail)
        return Check(f"stuck:{fname}", ok,
                     "value varies" if ok
                     else f"identical value {value} for {_STUCK_RUN} consecutive readings")

    @staticmethod
    def _electrical_consistency(values: dict[str, Any], prev: dict[str, Any] | None,
                                reading: dict[str, Any]) -> list[Check]:
        """Cross-field physics for the energy meter: the electrical channels must
        agree with each other, so a meter can't fabricate one field within its
        (gross) bounds while the others stay honest. Two invariants:
          power_w ≈ voltage_v · current_a  (within a plausible power-factor band)
          Δenergy_wh ≈ power_w · Δt_hours   (the register integrates real power)
        Only emitted when the relevant fields are present, so non-meter devices
        are unaffected."""
        out: list[Check] = []
        v, i, p = values.get("voltage_v"), values.get("current_a"), values.get("power_w")
        if all(isinstance(x, (int, float)) for x in (v, i, p)):
            apparent = float(v) * float(i)  # VA
            # Real power ≤ apparent power; power factor realistically ≥ 0.4 for
            # household loads. Allow a small additive slack for rounding/noise.
            hi = apparent + 25.0
            lo = 0.4 * apparent - 25.0
            ok = lo <= float(p) <= hi
            out.append(Check("consistency:power", ok,
                             f"power {float(p):.1f}W vs V·I={apparent:.1f}VA (pf-band [{max(0.0,lo):.0f},{hi:.0f}])"))
        e = values.get("energy_wh")
        if prev is not None and isinstance(e, (int, float)) and isinstance(p, (int, float)):
            prev_e = prev.get("values", {}).get("energy_wh")
            if isinstance(prev_e, (int, float)):
                dt_h = max(0.0, (_parse_ts(reading.get("ts", "")) - _parse_ts(prev.get("ts", ""))) / 3600.0)
                if dt_h > 0:
                    expected = float(p) * dt_h
                    delta = float(e) - float(prev_e)
                    # Power can move between the two samples; allow the register
                    # delta to sit within a wide band around p·Δt (+ absolute slack).
                    tol = max(50.0 * dt_h, 0.75 * abs(expected)) + 5.0
                    ok = abs(delta - expected) <= tol
                    out.append(Check("consistency:energy", ok,
                                     f"Δenergy {delta:.2f}Wh vs power·Δt {expected:.2f}Wh (tol {tol:.1f})"))
        return out

    def _siblings(self, device_id: str, values: dict[str, Any],
                  reading: dict[str, Any]) -> list[Check]:
        out: list[Check] = []
        ts = _parse_ts(reading.get("ts", ""))
        for sib in self.fleet.siblings(device_id):
            last = self.fleet.last_reading(sib.device_id)
            if not last or abs(ts - _parse_ts(last.get("ts", ""))) > _SIBLING_MAX_AGE_S:
                continue
            for fname, tol in SIBLING_TOLERANCE.items():
                mine = values.get(fname)
                theirs = last.get("values", {}).get(fname)
                if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
                    delta = abs(float(mine) - float(theirs))
                    ok = delta <= tol
                    out.append(Check(f"sibling:{fname}", ok,
                                     f"Δ={delta:.2f} vs {sib.device_id} (tol {tol})"))
        return out
