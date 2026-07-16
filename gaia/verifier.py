"""Metis-envelope-compatible ``/v1/verify`` for physical readings.

The hub's Pay-on-Verified worker POSTs ``{"input": <str|obj>, "route": …,
"min_verify_score": …}`` to ``{AIMARKET_VERIFY_METIS_URL}/v1/verify`` and reads
back the Metis envelope. GAIA implements that exact contract with the
statistical :class:`~gaia.plausibility.PlausibilityVerifier` under the hood —
proving the verifier slot is an INTERFACE, not a Metis lock-in: any service
that answers this envelope can gate escrow settlement.

Accepted inputs:
  * the hub's composed audit string (``Task (buyer intent): … Delivered result
    (JSON): …``) — parsed for the delivered-result JSON block;
  * a dict carrying ``reading`` (+ optional ``attestation``) directly;
  * a bare reading dict.

Engine-error semantics mirror Metis: unparseable input returns HTTP 200 with
``status: "error"`` (the hub then applies its bounded-retry + fail-open/closed
policy), never a 5xx.
"""

from __future__ import annotations

import json
import secrets
from collections import OrderedDict
from typing import Any

from gaia.plausibility import PlausibilityVerifier

_RESULT_MARK = "Delivered result (JSON):"
_INTENT_MARK = "Task (buyer intent):"
_JUDGE_MARK = "\n\nJudge whether"

_TRACE_CAP = 1000
_MAX_INPUT_CHARS = 200_000  # matches Metis's /v1/verify input cap


class VerifierService:
    def __init__(self, verifier: PlausibilityVerifier):
        self.verifier = verifier
        self._traces: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Anti-replay high-water: the escrow gate settles each (device, seq) at
        # most once. A genuine attested reading replayed to double-settle is
        # rejected here, without burdening the read-only gaia.verify@v1 diagnostic
        # (which calls PlausibilityVerifier.check directly and stays stateless).
        self._high_water: dict[str, int] = {}

    # ── Envelope entry point ─────────────────────────────────────────────────

    def verify(self, raw_input: Any, min_verify_score: float | None = None) -> dict[str, Any]:
        parsed = self._extract(raw_input)
        if parsed is None:
            return self._error_envelope("unparseable_input")
        reading, attestation, intent = parsed
        verdict = self.verifier.check(reading, attestation, min_score=min_verify_score)
        # Freshness is enforced only for a reading that otherwise passed — a failed
        # verdict never advances the high-water, so a legitimately-retried invoke of
        # a fresh reading is unaffected.
        if verdict.verified:
            fresh = self._check_and_advance_freshness(reading)
            if fresh is not None:
                verdict = fresh
        trace_id = f"gaia_{secrets.token_hex(8)}"
        self._remember(trace_id, intent, reading, verdict)
        return {
            "answer": verdict.summary,
            "status": "success",
            "verified": verdict.verified,
            "verify_score": verdict.score,
            "route": "fast",
            "depth": None,
            "iterations": 1,
            "clarifications": [],
            "usage": {},
            "trace_id": trace_id,
        }

    def trace(self, trace_id: str) -> dict[str, Any] | None:
        return self._traces.get(trace_id)

    def _check_and_advance_freshness(self, reading: dict[str, Any]):
        """Reject a replay of an already-settled (device, seq); else advance the
        high-water. Returns a failing Verdict on replay, or None when fresh."""
        from gaia.plausibility import Check, Verdict
        device_id = str(reading.get("device_id", ""))
        try:
            seq = int(reading.get("seq", 0))
        except (TypeError, ValueError):
            seq = 0
        last = self._high_water.get(device_id, -1)
        if seq <= last:
            return Verdict(
                False, 0.0,
                [Check("freshness", False,
                       f"seq {seq} already settled for {device_id} (high-water {last}) — replay rejected")],
                "replay rejected",
            )
        self._high_water[device_id] = seq
        return None

    # ── Parsing ──────────────────────────────────────────────────────────────

    def _extract(self, raw: Any) -> tuple[dict[str, Any], dict[str, Any] | None, str] | None:
        """Return (reading, attestation, intent) or None if nothing readable."""
        if isinstance(raw, str):
            return self._extract_from_text(raw)
        if isinstance(raw, dict):
            intent = str(raw.get("intent", ""))
            body = raw
            # {"input": {...}} nesting (a caller forwarding the whole verify body)
            if "reading" not in body and isinstance(body.get("input"), (dict, str)):
                return self._extract(body["input"])
            if isinstance(body.get("reading"), dict):
                att = body.get("attestation") if isinstance(body.get("attestation"), dict) else None
                return body["reading"], att, intent
            if "device_id" in body and "values" in body:  # bare reading
                return body, None, intent
        return None

    def _extract_from_text(self, text: str) -> tuple[dict[str, Any], dict[str, Any] | None, str] | None:
        if len(text) > _MAX_INPUT_CHARS:
            return None
        if _RESULT_MARK not in text:
            return None
        # Key off the LAST delimiter, not the first: the hub always appends the
        # genuine delivered output AFTER the (untrusted) buyer intent, so a fake
        # "Delivered result (JSON):" smuggled into intent cannot redirect the parse.
        intent = text.rsplit(_RESULT_MARK, 1)[0]
        if _INTENT_MARK in intent:
            intent = intent.split(_INTENT_MARK, 1)[1].strip()
        else:
            intent = intent.strip()
        blob = text.rsplit(_RESULT_MARK, 1)[1]
        if _JUDGE_MARK in blob:
            blob = blob.rsplit(_JUDGE_MARK, 1)[0]
        try:
            delivered = json.loads(blob.strip())
        except (ValueError, RecursionError):
            # RecursionError: adversarially deep JSON. Both stay HTTP-200 error
            # envelopes (never a 5xx), honouring the module's fail-safe contract.
            return None
        if not isinstance(delivered, dict):
            return None
        if isinstance(delivered.get("reading"), dict):
            att = delivered.get("attestation") if isinstance(delivered.get("attestation"), dict) else None
            return delivered["reading"], att, intent
        if "device_id" in delivered and "values" in delivered:
            return delivered, None, intent
        return None

    # ── Trace store ──────────────────────────────────────────────────────────

    def _remember(self, trace_id: str, intent: str, reading: dict[str, Any], verdict) -> None:
        self._traces[trace_id] = {
            "trace_id": trace_id,
            "intent": intent,
            "device_id": reading.get("device_id"),
            "seq": reading.get("seq"),
            "ts": reading.get("ts"),
            **verdict.to_dict(),
        }
        while len(self._traces) > _TRACE_CAP:
            self._traces.popitem(last=False)

    @staticmethod
    def _error_envelope(error: str) -> dict[str, Any]:
        return {
            "answer": "",
            "status": "error",
            "verified": False,
            "verify_score": 0.0,
            "route": "fast",
            "depth": None,
            "iterations": 0,
            "clarifications": [],
            "usage": {},
            "trace_id": None,
            "error": error,
        }
