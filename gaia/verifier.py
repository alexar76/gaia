"""Metis-envelope-compatible ``/v1/verify`` for physical readings.

The hub's Pay-on-Verified worker POSTs ``{"input": <str|obj>, "route": …,
"min_verify_score": …}`` to ``{AIMARKET_VERIFY_METIS_URL}/v1/verify`` and reads
back the Metis envelope. GAIA implements that exact contract with the
statistical :class:`~gaia.plausibility.PlausibilityVerifier` under the hood —
proving the verifier slot is an INTERFACE, not a Metis lock-in: any service
that answers this envelope can gate escrow settlement.

Accepted inputs:
  * the hub's composed audit string (``Task (buyer intent): … Delivered result
    (JSON): …``) — parsed for the delivered-result JSON block, whether or not it
    is wrapped in the hub's per-request untrusted-data fence;
  * a dict carrying ``reading`` (+ optional ``attestation``) directly;
  * a bare reading dict.

The success envelope carries ``verify_performed: true`` and an explicit
``delivery_verdict``: GAIA's statistical check IS a judgement about the delivered
goods, so it says so structurally instead of leaving the hub to infer a delivery
verdict from a free-text summary. The error envelope reports
``verify_performed: false`` — nothing was checked, which the hub must classify as
indeterminate (operator policy) rather than as a provider fault.

Every envelope also echoes the ``threshold`` the verdict was decided at and where
that number came from (``threshold_source``). Two thresholds have to agree for the
escrow to be meaningful — the hub's ``AIMARKET_VERIFY_SCORE_THRESHOLD`` and the bar
GAIA applies — and they are configured in two different places. The hub passes its
bar as ``min_verify_score``; GAIA honours it and says so, so a caller whose bar was
NOT the one used (an older hub that sends nothing, a misconfigured deployment) can
detect the disagreement instead of acting on a verdict decided at a bar it never set.

Engine-error semantics mirror Metis: unparseable input returns HTTP 200 with
``status: "error"`` (the hub then applies its bounded-retry + fail-open/closed
policy), never a 5xx.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections import OrderedDict
from typing import Any

from gaia.plausibility import PlausibilityVerifier

logger = logging.getLogger("gaia.verifier")

_RESULT_MARK = "Delivered result (JSON):"
_INTENT_MARK = "Task (buyer intent):"
_JUDGE_MARK = "\n\nJudge whether"

# The hub fences the (untrusted, paid-on-pass) provider output in a per-request
# nonce marker so an LLM judge is told the block is DATA. The nonce is minted per
# attempt, so the seller cannot pre-bake a matching marker into its output.
_FENCE_OPEN_RE = re.compile(r"<<<UNTRUSTED-DELIVERY-([0-9a-fA-F]{4,64})>>>")

# A fence marker is ~50 chars; probing this much of a candidate block covers it plus
# any leading newline without copying a 100k-char delivery to test one prefix.
_FENCE_PROBE_CHARS = 128
# Cap on candidate delimiters examined, so a marker-flooded delivery cannot make the
# scan quadratic.
_MAX_MARKER_SCAN = 16

_TRACE_CAP = 1000
_MAX_INPUT_CHARS = 200_000  # matches Metis's /v1/verify input cap
_MAX_REASONS = 6


class VerifierService:
    def __init__(self, verifier: PlausibilityVerifier):
        self.verifier = verifier
        self._traces: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Anti-replay high-water: the escrow gate settles each (device, seq) at
        # most once. A genuine attested reading replayed to double-settle is
        # rejected here, without burdening the read-only gaia.verify@v1 diagnostic
        # (which calls PlausibilityVerifier.check directly and stays stateless).
        self._high_water: dict[str, int] = {}
        # One-shot log keys: a threshold disagreement repeats on every invoke, and a
        # per-request warning would bury the signal it is meant to raise.
        self._warned: set[str] = set()

    # ── Envelope entry point ─────────────────────────────────────────────────

    def _threshold(self, min_verify_score: float | None) -> tuple[float, str]:
        """The bar this verdict is decided at, and where it came from.

        A caller-supplied ``min_verify_score`` always wins — the party moving money
        owns the bar. The local default is only a fallback, and saying WHICH applied
        is the whole point: a hub that re-checks the returned score against its own
        threshold and a GAIA silently judging at 0.7 would disagree without either
        side being able to notice."""
        local = float(self.verifier.threshold)
        if min_verify_score is None:
            return local, "verifier_default"
        applied = float(min_verify_score)
        if abs(applied - local) > 1e-9 and "threshold" not in self._warned:
            self._warned.add("threshold")
            logger.warning(
                "gaia verify: caller bar %.4f differs from the configured default %.4f — "
                "honouring the caller's and reporting it in the envelope", applied, local,
            )
        return applied, "request"

    def verify(self, raw_input: Any, min_verify_score: float | None = None) -> dict[str, Any]:
        threshold, threshold_source = self._threshold(min_verify_score)
        parsed = self._extract(raw_input)
        if parsed is None:
            return self._error_envelope("unparseable_input", threshold, threshold_source)
        reading, attestation, intent = parsed
        verdict = self.verifier.check(reading, attestation, min_score=threshold)
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
            # A real check ran on the delivered reading — say so explicitly, so the
            # hub's escrow can distinguish "judged and failed" (a provider fault)
            # from "nothing was judged" (operator policy, never a fault).
            "verify_performed": True,
            # The bar `verified`/`fulfils` were decided at, and whose bar it was.
            "threshold": round(threshold, 4),
            "threshold_source": threshold_source,
            "delivery_verdict": _delivery_verdict(verdict),
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
        split = _split_delivered(text)
        if split is None:
            return None
        head, blob = split
        if _INTENT_MARK in head:
            intent = head.split(_INTENT_MARK, 1)[1].strip()
        else:
            intent = head.strip()
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
    def _error_envelope(error: str, threshold: float, threshold_source: str) -> dict[str, Any]:
        return {
            "answer": "",
            "status": "error",
            "verified": False,
            "verify_score": 0.0,
            # Reported even here: the bar this attempt WOULD have used. A caller
            # diagnosing a run of indeterminate settlements needs to see whether its
            # own threshold ever reached the verifier.
            "threshold": round(threshold, 4),
            "threshold_source": threshold_source,
            # NOTHING was verified. Without this the hub's 0.0 score is
            # indistinguishable from "the reading was judged implausible", and a
            # provider would be blamed (and eventually slashed) for GAIA's failure
            # to parse the request.
            "verify_performed": False,
            "route": "fast",
            "depth": None,
            "iterations": 0,
            "clarifications": [],
            "usage": {},
            "trace_id": None,
            "error": error,
        }


# ── Prompt fence + structured delivery verdict ───────────────────────────────


def _cut_judge_tail(blob: str) -> str:
    return blob.rsplit(_JUDGE_MARK, 1)[0] if _JUDGE_MARK in blob else blob


def _split_delivered(text: str) -> tuple[str, str] | None:
    """Split an audit prompt into (everything before the delivered result, the result).

    Which ``Delivered result (JSON):`` is the hub's own is the whole ballgame. The
    seller's output sits INSIDE the delivered block, so a marker literal smuggled into
    it appears AFTER the hub's — and keying off the last occurrence therefore let the
    seller move this parse onto its own text. That is not merely noisy: a lying sensor
    could turn its own conviction into ``unparseable_input``, which the hub classifies
    as indeterminate (no verify_failed event, no fault escalation, no slash), and which
    a fail-open operator still pays out. So:

      * a FENCING hub (current) is unambiguous — take the FIRST block whose body opens
        with a per-attempt nonce fence. The hub's own block is always first, because
        anything the seller writes is nested inside it;
      * an older, unfenced hub keeps the last-marker rule, which is the right answer
        there: that hub rejects these markers in the buyer intent, so the last one is
        the genuine delimiter.

    Only the head of each candidate is probed, so a seller that floods its output with
    marker literals costs O(1) per candidate rather than a full rescan.
    """
    if _RESULT_MARK not in text:
        return None
    pos = text.find(_RESULT_MARK)
    for _ in range(_MAX_MARKER_SCAN):
        if pos < 0:
            break
        body = pos + len(_RESULT_MARK)
        if _FENCE_OPEN_RE.match(text[body:body + _FENCE_PROBE_CHARS].lstrip()):
            return text[:pos], _unfence(_cut_judge_tail(text[body:]))
        pos = text.find(_RESULT_MARK, body)
    pos = text.rfind(_RESULT_MARK)
    body = pos + len(_RESULT_MARK)
    return text[:pos], _unfence(_cut_judge_tail(text[body:]))


def _unfence(blob: str) -> str:
    """Strip the hub's per-request untrusted-data fence around the delivered JSON.

    The opening marker must be the FIRST thing in the delivered-result block (that
    is the hub's structural position, and the nonce is minted per attempt so a
    seller cannot pre-bake it into its output); the audited span then runs to the
    LAST close of that same nonce, so markers forged inside the delivery cannot
    shrink it. Anything else — no fence, a fence that does not start the block — is
    returned unchanged, so an older hub's bare prompt still parses and a stray
    marker never redirects the parse.
    """
    body = blob.lstrip()
    match = _FENCE_OPEN_RE.match(body)
    if match is None:
        return blob
    inner = body[match.end():]
    close = f"<<</UNTRUSTED-DELIVERY-{match.group(1)}>>>"
    cut = inner.rfind(close)
    return inner if cut < 0 else inner[:cut]


def _delivery_verdict(verdict) -> dict[str, Any]:
    """The judgement about the DELIVERED GOODS, in the hub's structured shape.

    `fulfils` is the plausibility outcome and `score` how completely the reading
    fulfils the buyer's ask (the same per-field minimum the escrow threshold is
    compared against); the reasons name the physics that convicted the reading, so a
    refunded buyer gets the "why" without pulling the trace.
    """
    reasons = [f"{c.name}: {c.detail}" for c in verdict.checks if not c.ok]
    return {
        "fulfils": bool(verdict.verified),
        "score": float(verdict.score),
        "reasons": reasons[:_MAX_REASONS] or [verdict.summary],
    }
