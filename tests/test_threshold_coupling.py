"""The threshold GAIA judges at, and saying so out loud.

Two numbers have to agree for the Pay-on-Verified escrow to mean anything:

  * the hub's ``AIMARKET_VERIFY_SCORE_THRESHOLD`` — the bar it re-applies to the
    returned score before it moves money;
  * the bar GAIA compares its plausibility score against to decide ``verified`` /
    ``delivery_verdict.fulfils``.

They live in two different services with two different defaults, and nothing used to
check they matched: GAIA silently fell back to :class:`PlausibilityVerifier`'s own 0.7
whenever a caller sent no ``min_verify_score``, and the envelope gave no way to tell
which bar had been used. So this module pins both halves of the coupling — GAIA
honours the caller's bar, and every envelope says which bar it was and where it came
from — plus the subtler way the two could disagree: reporting one number and deciding
on another.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gaia.app import build_app
from gaia.capabilities import GatewayRuntime
from gaia.plausibility import Check, PlausibilityVerifier


@pytest.fixture
def rt(tmp_path):
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys"), autotick=True, tick_s=60.0)
    runtime.warm_up(40)
    return runtime


@pytest.fixture
def client(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    with TestClient(build_app(rt, public_url="http://gaia.test")) as c:
        yield c


def _drifted(rt) -> dict:
    """A sensor whose twin has walked away from it: the sibling check convicts, the
    single-step rate and the z-score do not. That leaves temperature at 2 of 3 soft
    checks — an overall score of 0.6667, the only kind of reading where the choice of
    bar actually decides the outcome. (An honest reading scores 1.0 and a spiking one
    0.0; neither can tell you whose threshold was applied.)"""
    rt.fleet.get("ws-01").inject_fault("drift", fields=["temperature_c"], magnitude=2.0)
    out = None
    for _ in range(240):  # 4 simulated hours of 2 °C/h drift
        out = rt.read("ws-01")
        rt.fleet.read("ws-02")  # the honest twin keeps reporting
    return out


# ── 1. Whose bar was it? ─────────────────────────────────────────────────────


def test_envelope_reports_the_caller_supplied_bar(client, rt):
    env = client.post("/v1/verify", json={"input": rt.read("ws-01"),
                                          "min_verify_score": 0.55}).json()
    assert env["status"] == "success"
    assert env["threshold"] == pytest.approx(0.55)
    assert env["threshold_source"] == "request"


def test_envelope_admits_when_it_fell_back_to_its_own_default(client, rt):
    """A caller that sends no bar gets GAIA's. That is a legitimate default, but it
    must be *visible*: an operator whose hub judges at 0.9 while GAIA quietly judged at
    0.7 is settling on verdicts nobody asked for."""
    env = client.post("/v1/verify", json={"input": rt.read("ws-01")}).json()
    assert env["threshold"] == pytest.approx(0.7)   # PlausibilityVerifier's default
    assert env["threshold_source"] == "verifier_default"


def test_error_envelope_still_names_the_bar(client):
    """The error envelope is what an operator stares at when settlements go
    indeterminate — it must not be the one shape that hides the threshold."""
    env = client.post("/v1/verify", json={"input": "no delivered result here",
                                          "min_verify_score": 0.9}).json()
    assert env["status"] == "error" and env["verify_performed"] is False
    assert env["threshold"] == pytest.approx(0.9)
    assert env["threshold_source"] == "request"


# ── 2. The caller's bar actually decides the verdict ─────────────────────────


def test_the_same_reading_passes_or_fails_depending_on_whose_bar_applied(client, rt):
    """The mismatch case, made concrete: one reading, one score, two bars, opposite
    money outcomes. GAIA must apply the caller's — and the envelope must say so, or the
    hub cannot tell a genuine conviction from a verdict rendered at the wrong bar."""
    out = _drifted(rt)

    # Strict bar first: a failing verdict never advances the anti-replay high-water,
    # so the same reading can be re-judged at the other bar below.
    strict = client.post("/v1/verify", json={"input": out, "min_verify_score": 0.7}).json()
    assert strict["threshold"] == pytest.approx(0.7)
    assert strict["verify_score"] == pytest.approx(0.6667)
    assert strict["verified"] is False
    assert strict["delivery_verdict"]["fulfils"] is False

    lenient = client.post("/v1/verify", json={"input": out, "min_verify_score": 0.6}).json()
    assert lenient["threshold"] == pytest.approx(0.6)
    assert lenient["verify_score"] == pytest.approx(strict["verify_score"])  # same evidence
    assert lenient["verified"] is True                                       # opposite verdict
    assert lenient["delivery_verdict"]["fulfils"] is True


def test_a_caller_bar_differing_from_the_local_default_is_logged_once(client, rt, caplog):
    """Honoured, not silently swallowed: the disagreement is a deployment fact an
    operator should be able to see in the logs, and exactly once — it repeats on every
    single invoke."""
    with caplog.at_level("WARNING", logger="gaia.verifier"):
        for _ in range(3):
            client.post("/v1/verify", json={"input": rt.read("ws-01"),
                                            "min_verify_score": 0.42})
    hits = [r for r in caplog.records if "differs from the configured default" in r.message]
    assert len(hits) == 1


# ── 3. Report the number you decided on ──────────────────────────────────────


def test_the_verdict_is_decided_on_the_score_it_reports(client, rt):
    """`verified` used to be computed from the RAW score while the ROUNDED one was
    published, so the two could disagree by up to 5e-5. That is not cosmetic: the hub
    re-applies its threshold to the published number, so an envelope saying
    `fulfils: false` next to a score that clears the bar is read as a genuine
    conviction — a refund, a `verify_failed` reputation event and a step up the slash
    ladder for a reading that actually met the operator's bar.

    2/3 rounds to 0.6667, so a bar of exactly 0.6667 is where the two diverge.
    """
    out = _drifted(rt)
    env = client.post("/v1/verify", json={"input": out, "min_verify_score": 0.6667}).json()
    assert env["verify_score"] == pytest.approx(0.6667)
    dv = env["delivery_verdict"]
    # The published score clears the published bar, so the published verdict must agree.
    assert dv["score"] >= env["threshold"]
    assert dv["fulfils"] is True and env["verified"] is True


def test_plausibility_decides_and_publishes_the_same_number(rt):
    """The same invariant one layer down, stated as a property rather than a case:
    whatever bar it is given, `verified` is exactly `score >= threshold` on the score
    the Verdict carries."""
    out = _drifted(rt)
    verifier = PlausibilityVerifier(rt.fleet)
    for bar in (0.0, 0.5, 0.6666, 0.6667, 0.6668, 0.7, 1.0):
        v = verifier.check(out["reading"], out["attestation"], min_score=bar)
        assert v.verified is (v.score >= bar), (bar, v.score)


def test_a_bar_finer_than_the_envelope_grid_is_applied_exactly_and_echoed_honestly(client, rt):
    """The envelope publishes every number on a 4-decimal grid, but the bar handed to
    GAIA is an arbitrary float. Two things must hold at once, and they pull in opposite
    directions:

      * the DECISION uses the exact bar — 2/3 publishes as 0.6667, so a bar of 0.66675
        is genuinely above this reading and must convict it;
      * the ECHO may therefore be off, but only by the grid quantum. The hub compares
        its own bar against that echo and refuses to settle on a verdict it believes
        was rendered at a different bar, so an echo that drifted further than the grid
        would turn every honest settlement into `threshold_mismatch`.
    """
    out = _drifted(rt)
    bar = 0.66675
    env = client.post("/v1/verify", json={"input": out, "min_verify_score": bar}).json()
    assert env["verify_score"] == pytest.approx(0.6667)
    assert env["verified"] is False                      # the exact bar decided, not the echo
    assert env["delivery_verdict"]["fulfils"] is False
    assert env["threshold"] == pytest.approx(bar, abs=5e-5)


def test_delivery_verdict_score_is_the_envelope_score(client, rt):
    """One number, two places it is published. The hub compares `delivery_verdict.score`
    against the bar and reports `verify_score` in the receipt; if they could drift apart
    the receipt would document a different verdict than the one that moved the money."""
    for payload in (rt.read("ws-01"), _drifted(rt)):
        env = client.post("/v1/verify", json={"input": payload,
                                              "min_verify_score": 0.6}).json()
        assert env["delivery_verdict"]["score"] == env["verify_score"]
        assert env["delivery_verdict"]["fulfils"] is env["verified"]


def test_score_rounding_cannot_invert_a_hard_failure(rt):
    """Guard the guard: rounding must not turn a zeroed field into a pass. A hard-check
    failure is a 0.0 field score, and min-over-fields keeps it at 0.0 for any bar above
    zero."""
    verifier = PlausibilityVerifier(rt.fleet)
    unattested = {"device_id": "ws-01", "model": "x", "seq": 999_999,
                  "ts": "2026-06-01T12:00:00Z", "values": {"temperature_c": 21.5}}
    v = verifier.check(unattested, min_score=0.0001)
    assert v.score == 0.0 and v.verified is False
    assert any(isinstance(c, Check) and not c.ok for c in v.checks)
