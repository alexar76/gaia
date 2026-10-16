"""The payer statement: the only identity GAIA trusts for who is commanding a device.

What must hold:
  * the wire format is pinned by a golden vector the hub's implementation must reproduce;
  * a statement is accepted only from a pinned hub, for this GAIA, this call, this input,
    within the clock window, once;
  * a statement that fails refuses the command; it is never quietly treated as anonymous;
  * it is checked only when the handler asks, so a call the price gate refused never is;
  * no statement is anonymous, and nothing a caller types on its own becomes an identity;
  * over HTTP, the identity reaches the handler for its own request and no other.
"""

from __future__ import annotations

import base64
import json
import secrets
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from gaia import payer_identity as pid
from gaia.app import build_app
from gaia.capabilities import GatewayRuntime
from gaia.payer_identity import ANONYMOUS, Caller, PayerVerifier, normalize_identity
from oracle_core.signing import Signer

HUB = "https://hub.test"
GAIA = "http://gaia.test"
AUD = f"{GAIA}/ai-market/v2/invoke"
CAP = "gaia.actuate@v1"
INPUT = {"actuator_id": "plug-sim-01", "command": "on", "duration_s": 60}
NOW = 1_792_145_220  # 2026-10-16T10:07:00Z
PAYER = "0x" + "Ab" * 20
NONCE = "ab" * 16

# The price gate's own vector (oracles/core/tests/test_price_gate.py): seed 07×32, the same hub,
# audience, capability, input, time and nonce. Only the domain, the payer and the rail differ,
# so a hub implementation can check both statements of one call against one fixture.
GOLDEN = dict(hub="https://Hub.Example:443/",
              audience="https://provider.example/oracle/ai-market/v2/invoke",
              capability_id="gate.priced@v1", input_payload={"msg": "hi", "n": 1.5},
              timestamp=1792000000, nonce="0123456789abcdef0123456789abcdef",
              payer="0x" + "AB" * 20, rail="channel")
GOLDEN_PUBKEY = "6kpsY+KcUgq+9VB7Ey7F+ZVHdq6+vnuSQh7qaRRG0iw="
GOLDEN_CANONICAL = (
    'aimarket-hub-payer/v1\n{"aud":"https://provider.example/oracle/ai-market/v2/invoke",'
    '"capability_id":"gate.priced@v1","hub":"https://hub.example",'
    '"input_sha256":"e119189c8dcefbf9474d5d89322c15b79c9d1424a8a31a923ec1b3f93d39f41a",'
    '"nonce":"0123456789abcdef0123456789abcdef",'
    '"payer":"0xabababababababababababababababababababab","rail":"channel","ts":1792000000}')
GOLDEN_SIGNATURE = ("bD78JNaEh3ZRhvL7aFMVPUHv5Ys0fTBSAszy9MlnteErc7ul6hUj5kFfFLIIAqpQSitGbPLV"
                    "ymyzYRDVkgBECQ==")


def _golden_canonical() -> str:
    g = GOLDEN
    return pid.payer_canonical(
        hub=g["hub"], audience=g["audience"], capability_id=g["capability_id"],
        input_digest=pid.input_sha256(g["input_payload"]), timestamp=g["timestamp"],
        nonce=g["nonce"], payer=g["payer"], rail=g["rail"])


@pytest.fixture
def hub(tmp_path):
    return Signer(tmp_path / "hub.key")


def _statement(hub, *, payer=PAYER, rail="channel", ts=NOW, nonce=NONCE, aud=AUD,
               hub_url=HUB, cap=CAP, inp=INPUT) -> dict[str, str]:
    headers = {pid.HUB_HEADER: hub_url, pid.AUDIENCE_HEADER: aud,
               pid.TIMESTAMP_HEADER: str(ts), pid.NONCE_HEADER: nonce}
    headers.update(pid.sign_payer(hub.sign_canonical, hub_url=hub_url, audience=aud,
                                  capability_id=cap, input_payload=inp, payer=payer,
                                  rail=rail, timestamp=ts, nonce=nonce))
    return headers


def _verifier(hub, now=NOW) -> PayerVerifier:
    return PayerVerifier({HUB: [hub.public_key_b64]}, [GAIA], clock=lambda: now)


# ── the wire format ───────────────────────────────────────────────────────────


def test_the_wire_format_is_pinned_by_a_golden_vector():
    key = Ed25519PrivateKey.from_private_bytes(b"\x07" * 32)
    pub = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    canonical = _golden_canonical()
    assert pub == GOLDEN_PUBKEY
    assert canonical == GOLDEN_CANONICAL
    assert base64.b64encode(key.sign(canonical.encode())).decode() == GOLDEN_SIGNATURE
    assert Signer.verify(GOLDEN_CANONICAL, GOLDEN_SIGNATURE, GOLDEN_PUBKEY)


def test_the_shared_rules_match_the_price_gate():
    """URL spelling, the input digest, the pinned-hub list and the four shared headers must
    be the price gate's own, or one pin would mean two things."""
    gate = pytest.importorskip("oracle_core.price_gate")
    for url in ("https://Hub.example/", "https://hub.example:443", "http://h:8080/x/",
                "ftp://nope", "", "https://[::1]:9/a"):
        assert pid.normalize_url(url) == gate.normalize_url(url)
    assert pid.input_sha256(INPUT) == gate.input_sha256(INPUT)
    raw = f"https://a.test={GOLDEN_PUBKEY},broken,https://b.test/={GOLDEN_PUBKEY}"
    assert pid.parse_trusted_hubs(raw) == gate.parse_trusted_hubs(raw)
    for name in ("HUB_HEADER", "AUDIENCE_HEADER", "TIMESTAMP_HEADER", "NONCE_HEADER"):
        assert getattr(pid, name) == getattr(gate, name)
    assert pid.DOMAIN != gate.DOMAIN
    # On one fixture the two statements agree on every field they share.
    g = GOLDEN
    admission = gate.request_canonical(
        hub=g["hub"], audience=g["audience"], capability_id=g["capability_id"],
        input_digest=gate.input_sha256(g["input_payload"]), timestamp=g["timestamp"],
        nonce=g["nonce"])
    ours = json.loads(_golden_canonical().split("\n", 1)[1])
    theirs = json.loads(admission.split("\n", 1)[1])
    assert {k: ours[k] for k in theirs} == theirs
    assert set(ours) - set(theirs) == {"payer", "rail"}


@pytest.mark.parametrize("raw, spelled", [
    ("0x" + "Ab" * 20, "0x" + "ab" * 20),               # EIP-55 case is a checksum
    ("  0x" + "12" * 20 + " ", "0x" + "12" * 20),
    ("7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV", "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV"),
])
def test_an_identity_is_a_wallet_address_in_one_spelling(raw, spelled):
    assert normalize_identity(raw) == spelled


@pytest.mark.parametrize("raw", ["", "buyer-1", "0x" + "ab" * 19, "0x" + "zz" * 20,
                                 "0O" + "1" * 40, None, "anonymous"])
def test_anything_else_is_not_an_identity(raw):
    with pytest.raises(ValueError, match="not a wallet address"):
        normalize_identity(raw)


def test_a_malformed_trusted_hub_entry_is_dropped_not_widened():
    hubs = pid.parse_trusted_hubs(f"{HUB}={GOLDEN_PUBKEY}, junk , https://x.test=notakey")
    assert hubs == {HUB: (GOLDEN_PUBKEY,)}


# ── verification ──────────────────────────────────────────────────────────────


def test_a_statement_from_a_pinned_hub_names_the_payer(hub):
    out = _verifier(hub).resolve(_statement(hub), CAP, INPUT)
    assert out == pid.Resolution(Caller("0x" + "ab" * 20, vouched_by=HUB, rail="channel"))


def test_no_statement_is_anonymous_whatever_else_the_caller_sends(hub):
    typed = {"X-Payment-Channel": "ch_123", "X-AIMarket-Buyer": "buyer-7",
             pid.PAYER_HEADER: PAYER, pid.RAIL_HEADER: "channel"}
    assert _verifier(hub).resolve(typed, CAP, INPUT) is ANONYMOUS


@pytest.mark.parametrize("mutate, reason", [
    (lambda h, hub: h.update({pid.HUB_HEADER: "https://other.hub"}), "not one this GAIA trusts"),
    (lambda h, hub: h.update({pid.NONCE_HEADER: "xyz"}), "32 hex"),
    (lambda h, hub: h.update({pid.TIMESTAMP_HEADER: "soon"}), "unix seconds"),
    (lambda h, hub: h.update({pid.AUDIENCE_HEADER: "https://another.gaia/ai-market/v2/invoke"}),
     "not this GAIA"),
    (lambda h, hub: h.update({pid.RAIL_HEADER: "credit-card"}), "rail must be"),
    (lambda h, hub: h.update({pid.PAYER_HEADER: "buyer-1"}), "not a wallet address"),
    (lambda h, hub: h.update({pid.PAYER_HEADER: "0x" + "cd" * 20}), "does not verify"),
    (lambda h, hub: h.update({pid.RAIL_HEADER: "x402"}), "does not verify"),
    (lambda h, hub: h.update({pid.TIMESTAMP_HEADER: str(NOW + 1)}), "does not verify"),
])
def test_a_statement_that_fails_refuses_with_its_reason(hub, mutate, reason):
    headers = _statement(hub)
    mutate(headers, hub)
    out = _verifier(hub).resolve(headers, CAP, INPUT)
    assert out.caller is None and reason in out.problem and "nothing charged" in out.problem


def test_a_statement_is_for_one_call_only(hub):
    v = _verifier(hub)
    other_input = {**INPUT, "duration_s": 900}
    assert "does not verify" in v.resolve(_statement(hub), CAP, other_input).problem
    assert "does not verify" in v.resolve(_statement(hub), "gaia.verify@v1", INPUT).problem


def test_a_statement_signed_by_another_key_fails(hub, tmp_path):
    forger = Signer(tmp_path / "forger.key")
    out = _verifier(hub).resolve(_statement(forger), CAP, INPUT)
    assert "does not verify" in out.problem


def test_a_stale_statement_fails(hub):
    out = _verifier(hub, now=NOW + 121).resolve(_statement(hub), CAP, INPUT)
    assert "from this GAIA's clock" in out.problem


def test_a_statement_is_accepted_once(hub):
    v = _verifier(hub)
    assert v.resolve(_statement(hub), CAP, INPUT).caller is not None
    assert "replayed" in v.resolve(_statement(hub), CAP, INPUT).problem


class _CountingVerifier(PayerVerifier):
    def __init__(self, hub):
        super().__init__({HUB: [hub.public_key_b64]}, [GAIA], clock=lambda: NOW)
        self.calls = 0

    def resolve(self, *args):
        self.calls += 1
        return super().resolve(*args)


def _drive(middleware, headers) -> None:
    import asyncio

    body = json.dumps({"capability_id": CAP, "input": INPUT}).encode()
    scope = {"type": "http", "method": "POST", "path": pid.INVOKE_PATH,
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    sent = iter([{"type": "http.request", "body": body, "more_body": False}])

    async def receive():
        return next(sent)

    async def send(message):
        pass

    asyncio.run(middleware(scope, receive, send))


def test_the_statement_is_checked_only_when_the_handler_asks(hub):
    """oracle-core's price gate runs between the middleware and the handler: a call it
    refuses never asks, so it never spends a nonce here."""
    verifier, seen = _CountingVerifier(hub), []

    async def refused(scope, receive, send):  # what a 402 from the gate looks like from here
        await receive()

    async def handled(scope, receive, send):
        await receive()
        seen.extend([pid.current(), pid.current()])

    _drive(pid.PayerIdentityMiddleware(refused, verifier, [CAP]), _statement(hub))
    assert verifier.calls == 0 and not verifier._seen
    _drive(pid.PayerIdentityMiddleware(handled, verifier, [CAP]), _statement(hub))
    assert verifier.calls == 1 and seen[0] == seen[1] and seen[0].caller.id == "0x" + "ab" * 20
    assert pid.current() is ANONYMOUS  # outside a request


def test_the_env_is_the_price_gates(hub):
    v = PayerVerifier.from_env(GAIA, env={
        "ORACLE_TRUSTED_HUBS": f"{HUB}={hub.public_key_b64}",
        "ORACLE_HUB_AUDIENCES": "https://gaia.alias.test", "ORACLE_HUB_MAX_SKEW_S": "30"})
    assert v.trusted == {HUB: (hub.public_key_b64,)} and v.max_skew_s == 30
    assert v.audiences == {AUD, "https://gaia.alias.test/ai-market/v2/invoke"}


# ── over HTTP: the middleware hands the handler its own request's payer ───────


@pytest.fixture
def gaia(tmp_path, monkeypatch, hub):
    cfg = tmp_path / "act.yaml"
    cfg.write_text(json.dumps({"actuators": [{
        "id": "plug-sim-01", "kind": "sim-plug", "label": "demo", "load_w": 1500,
        "expect_on_w": 1000, "cooldown_s": 0, "effect_timeout_s": 2}]}))
    monkeypatch.setenv("GAIA_ACTUATION_ENABLED", "1")
    monkeypatch.setenv("GAIA_ACTUATORS_FILE", str(cfg))
    monkeypatch.setenv("GAIA_ACTUATION_DIR", str(tmp_path / "act"))
    monkeypatch.delenv("GAIA_ACTUATION_KILL", raising=False)
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gw.key"))
    monkeypatch.setenv("ORACLE_TRUSTED_HUBS", f"{HUB}={hub.public_key_b64}")
    # These tests are about the payer statement alone. Pinning a hub also switches the
    # price gate on where oracle-core has one; with the gate is test_..._behind_the_price_gate.
    monkeypatch.setenv("ORACLE_PRICE_GATE", "off")
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys"), autotick=False)
    runtime.actuation.POLL_S = 0.01
    return runtime, TestClient(build_app(runtime, public_url=GAIA))


def _invoke(client, hub, command, payer=None, *, extra=None, cap=CAP):
    inp = {"actuator_id": "plug-sim-01", "command": command}
    if command == "on":
        inp["duration_s"] = 600
    headers: dict[str, str] = {}
    if payer:
        ts, nonce = int(time.time()), secrets.token_hex(16)
        headers = {pid.HUB_HEADER: HUB, pid.AUDIENCE_HEADER: AUD,
                   pid.TIMESTAMP_HEADER: str(ts), pid.NONCE_HEADER: nonce}
        headers.update(pid.sign_payer(hub.sign_canonical, hub_url=HUB, audience=AUD,
                                      capability_id=cap, input_payload=inp, payer=payer,
                                      rail="x402", timestamp=ts, nonce=nonce))
    headers.update(extra or {})
    return client.post("/ai-market/v2/invoke", json={"capability_id": cap, "input": inp},
                       headers=headers).json()


A = "0x" + "a1" * 20
B = "0x" + "b2" * 20


def test_the_payer_a_hub_vouched_for_holds_the_lease(gaia, hub):
    runtime, client = gaia
    plug = runtime.fleet.get("plug-sim-01")
    on = _invoke(client, hub, "on", A)
    assert on["ok"] and on["output"]["lease"]["holder"] == A
    anonymous = _invoke(client, hub, "off")
    assert anonymous["ok"] is False and "busy until" in anonymous["error"]
    assert "no payer a trusted hub vouched for" in anonymous["error"]
    other = _invoke(client, hub, "off", B)
    assert other["ok"] is False and "busy until" in other["error"] and plug.relay_is_on
    mine = _invoke(client, hub, "off", A)
    assert mine["ok"] and not plug.relay_is_on
    journal = [json.loads(x) for x in runtime.actuation.journal_path.read_text().splitlines()]
    assert journal[0]["caller"] == {"id": A, "vouched_by": HUB, "rail": "x402"}


def test_a_broken_statement_refuses_and_switches_nothing(gaia, hub):
    runtime, client = gaia
    out = _invoke(client, hub, "on", A, extra={pid.PAYER_HEADER: B})
    assert out["ok"] is False and "payer statement refused" in out["error"]
    assert "nothing charged" in out["error"]
    assert not runtime.fleet.get("plug-sim-01").relay_is_on


def test_one_requests_payer_does_not_leak_into_the_next(gaia, hub):
    runtime, client = gaia
    assert _invoke(client, hub, "on", A)["ok"]
    # same process, same thread pool: the next request without a statement is anonymous
    out = _invoke(client, hub, "on")
    assert out["ok"] is False and "no payer a trusted hub vouched for" in out["error"]


def test_other_capabilities_never_read_a_statement(gaia, hub):
    runtime, client = gaia
    listed = client.post("/ai-market/v2/invoke", headers={pid.PAYER_SIGNATURE_HEADER: "junk"},
                         json={"capability_id": "gaia.actuator.list@v1", "input": {}}).json()
    assert listed["ok"] and listed["output"]["count"] == 1


def test_a_body_that_is_not_json_still_gets_the_normal_answer(gaia):
    runtime, client = gaia
    r = client.post("/ai-market/v2/invoke", content=b"{not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_the_statement_rides_next_to_the_hub_admission_behind_the_price_gate(
        gaia, hub, monkeypatch, tmp_path):
    """Where oracle-core enforces its price gate, the hub sends both signatures on one
    call, with one timestamp and nonce: the admission (paid) and the statement (who)."""
    gate = pytest.importorskip("oracle_core.price_gate")
    monkeypatch.setenv("ORACLE_PRICE_GATE", "enforce")
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys2"), autotick=False)
    runtime.actuation.POLL_S = 0.01
    client = TestClient(build_app(runtime, public_url=GAIA))
    inp = {"actuator_id": "plug-sim-01", "command": "on", "duration_s": 600}
    ts, nonce = int(time.time()), secrets.token_hex(16)
    headers = gate.sign_request(hub.sign_canonical, hub_url=HUB, audience=AUD,
                                capability_id=CAP, input_payload=inp, now=ts, nonce=nonce)
    headers.update(pid.sign_payer(hub.sign_canonical, hub_url=HUB, audience=AUD,
                                  capability_id=CAP, input_payload=inp, payer=A,
                                  rail="channel", timestamp=ts, nonce=nonce))
    out = client.post("/ai-market/v2/invoke", json={"capability_id": CAP, "input": inp},
                      headers=headers).json()
    assert out["ok"] and out["output"]["lease"]["holder"] == A
    unpaid = client.post("/ai-market/v2/invoke", json={"capability_id": CAP, "input": inp})
    assert unpaid.status_code == 402


def test_a_call_the_price_gate_refuses_never_has_its_statement_checked(gaia, hub, monkeypatch,
                                                                         tmp_path):
    """A valid payer statement without the admission: 402, and GAIA's replay cache keeps
    the nonce for the real call."""
    pytest.importorskip("oracle_core.price_gate")
    monkeypatch.setenv("ORACLE_PRICE_GATE", "enforce")
    runtime = GatewayRuntime(key_dir=str(tmp_path / "keys3"), autotick=False)
    app = build_app(runtime, public_url=GAIA)
    client = TestClient(app)
    inp = {"actuator_id": "plug-sim-01", "command": "on", "duration_s": 600}
    ts, nonce = int(time.time()), secrets.token_hex(16)
    headers = {pid.HUB_HEADER: HUB, pid.AUDIENCE_HEADER: AUD,
               pid.TIMESTAMP_HEADER: str(ts), pid.NONCE_HEADER: nonce}
    headers.update(pid.sign_payer(hub.sign_canonical, hub_url=HUB, audience=AUD,
                                  capability_id=CAP, input_payload=inp, payer=A,
                                  rail="channel", timestamp=ts, nonce=nonce))
    r = client.post("/ai-market/v2/invoke", json={"capability_id": CAP, "input": inp},
                    headers=headers)
    assert r.status_code == 402 and not runtime.fleet.get("plug-sim-01").relay_is_on
    assert not app.state.payer_verifier._seen
