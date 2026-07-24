"""Device attestation chain + the statistical plausibility verifier."""

from __future__ import annotations

from gaia.attestation import verify_reading
from gaia.capabilities import GatewayRuntime
from gaia.plausibility import PlausibilityVerifier


def _runtime(tmp_path, warm=40) -> GatewayRuntime:
    rt = GatewayRuntime(key_dir=str(tmp_path / "keys"), autotick=True, tick_s=60.0)
    if warm:
        rt.warm_up(warm)
    return rt


def test_attestation_roundtrip_and_wrong_key(tmp_path):
    rt = _runtime(tmp_path, warm=0)
    out = rt.read("ws-01")
    reading, att = out["reading"], out["attestation"]
    ws01 = rt.fleet.get("ws-01")
    ws02 = rt.fleet.get("ws-02")
    assert verify_reading(reading, att, expected_pubkey=ws01.signer.public_key_b64)
    # The same signature must NOT verify as coming from a different device key.
    assert not verify_reading(reading, att, expected_pubkey=ws02.signer.public_key_b64)
    # Tampered values break the signature.
    tampered = {**reading, "values": {**reading["values"], "temperature_c": 99.0}}
    assert not verify_reading(tampered, att, expected_pubkey=ws01.signer.public_key_b64)


def test_honest_readings_pass(tmp_path):
    rt = _runtime(tmp_path)
    for device_id in ("ws-01", "ws-02", "aq-01", "em-01"):
        out = rt.read(device_id)
        verdict = rt.verifier.check(out["reading"], out["attestation"])
        assert verdict.verified, f"{device_id}: {verdict.summary}"
        assert verdict.score >= 0.9


def test_spike_is_caught(tmp_path):
    rt = _runtime(tmp_path)
    rt.fleet.get("ws-01").inject_fault("spike", fields=["temperature_c"], magnitude=45.0)
    out = rt.read("ws-01")
    verdict = rt.verifier.check(out["reading"], out["attestation"])
    assert not verdict.verified
    failed = {c.name for c in verdict.checks if not c.ok}
    # A 45-degree jump trips several independent alarms at once.
    assert any(n.startswith(("zscore:", "rate:", "sibling:", "bounds:")) for n in failed)


def test_stuck_sensor_is_caught(tmp_path):
    rt = _runtime(tmp_path)
    rt.fleet.get("ws-01").inject_fault("stuck")
    verdict = None
    for _ in range(8):  # let the frozen value accumulate a run
        out = rt.read("ws-01")
        verdict = rt.verifier.check(out["reading"], out["attestation"])
    assert verdict is not None and not verdict.verified
    assert any(c.name.startswith("stuck:") and not c.ok for c in verdict.checks)


def test_drift_is_caught_by_sibling_divergence(tmp_path):
    rt = _runtime(tmp_path)
    rt.fleet.get("ws-01").inject_fault("drift", fields=["temperature_c"], magnitude=2.0)
    verdict = None
    for _ in range(240):  # 4 simulated hours of 2°C/h drift
        out = rt.read("ws-01")
        rt.fleet.read("ws-02")  # the honest twin keeps reporting
        verdict = rt.verifier.check(out["reading"], out["attestation"])
    assert verdict is not None and not verdict.verified
    assert any(c.name == "sibling:temperature_c" and not c.ok for c in verdict.checks)


def test_energy_register_rollback_is_caught(tmp_path):
    rt = _runtime(tmp_path)
    out = rt.read("em-01")
    reading = out["reading"]
    rolled = {**reading, "seq": reading["seq"] + 1,
              "values": {**reading["values"], "energy_wh": reading["values"]["energy_wh"] - 500.0}}
    # require_attestation=False: this test isolates the numeric monotonic check
    # (the escrow path always supplies an attestation — see the e2e suite).
    verdict = rt.verifier.check(rolled, require_attestation=False)
    assert any(c.name == "monotonic:energy_wh" and not c.ok for c in verdict.checks)


def test_missing_attestation_fails_closed(tmp_path):
    """Regression (audit CRITICAL): a bare, in-bounds reading with a valid fleet
    device_id but NO attestation must NOT verify — omitting the field must never
    be cheaper than forging it."""
    rt = _runtime(tmp_path, warm=0)
    forged = {"device_id": "ws-01", "model": "x", "seq": 1,
              "ts": "2026-01-01T00:00:00Z",
              "values": {"temperature_c": 21.5, "humidity_pct": 48.0, "pressure_hpa": 1013.0}}
    verdict = rt.verifier.check(forged)  # default require_attestation=True
    assert not verdict.verified and verdict.score == 0.0
    assert any(c.name == "device_attestation" and not c.ok for c in verdict.checks)


def test_energy_cross_field_consistency_catches_fabricated_power(tmp_path):
    """A meter reporting in-bounds but electrically-impossible power (P >> V·I)
    is caught by the consistency check even though every field is within bounds."""
    rt = _runtime(tmp_path)
    out = rt.read("em-01")
    reading, att = out["reading"], out["attestation"]
    honest = rt.verifier.check(reading, att)
    assert honest.verified
    # Fabricate power far above V·I (re-sign so attestation still passes — we are
    # testing the PHYSICS cross-check, not the signature).
    dev = rt.fleet.get("em-01")
    tampered = {**reading, "seq": reading["seq"] + 1,
                "values": {**reading["values"], "power_w": 9000.0}}
    from gaia.attestation import sign_reading
    att2 = sign_reading(tampered, dev.signer)
    verdict = rt.verifier.check(tampered, att2, require_attestation=True)
    assert any(c.name == "consistency:power" and not c.ok for c in verdict.checks)


def test_forged_attestation_zeroes_the_score(tmp_path):
    rt = _runtime(tmp_path)
    out = rt.read("ws-01")
    reading = out["reading"]
    forged = dict(out["attestation"])
    forged["value"] = "A" * 86 + "=="  # syntactically plausible, cryptographically garbage
    verdict = rt.verifier.check(reading, forged)
    assert not verdict.verified and verdict.score == 0.0


def test_unknown_device_rejected(tmp_path):
    rt = _runtime(tmp_path, warm=0)
    verdict = PlausibilityVerifier(rt.fleet).check(
        {"device_id": "ghost-9", "seq": 1, "ts": "2026-01-01T00:00:00Z", "values": {"temperature_c": 20}}
    )
    assert not verdict.verified and verdict.score == 0.0
