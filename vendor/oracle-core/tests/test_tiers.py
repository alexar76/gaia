"""Free/paid tier policy — the guard that bounds CPU an unpaid caller can command.

These run against a real ``create_app`` over TestClient, not against mocks: the
thing being tested is the wiring (which header lifts what, which bucket refuses
first, what status code comes back), and a mock of the wiring would pass while the
wiring was wrong.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from oracle_core import Capability, OracleSpec, create_app
from oracle_core.protocol import Protocol
from oracle_core.tiers import (
    FreeTierExceeded,
    PaidTierPolicy,
    enforce_free_tier,
)


# ── enforce_free_tier ────────────────────────────────────────────────────────────


def test_absent_field_is_never_refused():
    """The 'call it with no arguments' path must always work — every declared default
    in the family sits at or below its own ceiling, and a bare call is the first thing
    a discovering agent does."""
    enforce_free_tier("x.do@v1", {"difficulty": 100_000}, {})
    enforce_free_tier("x.do@v1", {"difficulty": 100_000}, {"seed": "abc"})


def test_at_the_ceiling_is_allowed():
    enforce_free_tier("x.do@v1", {"difficulty": 100_000}, {"difficulty": 100_000})


def test_one_over_the_ceiling_is_refused():
    with pytest.raises(FreeTierExceeded) as ei:
        enforce_free_tier("x.do@v1", {"difficulty": 100_000}, {"difficulty": 100_001})
    exc = ei.value
    assert exc.field == "difficulty"
    assert exc.requested == 100_001
    assert exc.ceiling == 100_000
    assert exc.capability_id == "x.do@v1"


def test_message_says_the_ceiling_and_the_way_out():
    """A 402 whose body does not say the number is a 402 the caller cannot act on."""
    with pytest.raises(FreeTierExceeded) as ei:
        enforce_free_tier("x.do@v1", {"T": 1_000}, {"T": 5_000})
    msg = str(ei.value)
    assert "1000" in msg
    assert "5000" in msg
    assert "payment channel" in msg.lower()


def test_body_is_machine_readable():
    with pytest.raises(FreeTierExceeded) as ei:
        enforce_free_tier("x.do@v1", {"T": 1_000}, {"T": 5_000})
    body = ei.value.as_body()
    assert body["ok"] is False
    assert body["error"] == "payment_required"
    assert body["free_tier"] == {"field": "T", "requested": 5_000, "max": 1_000}


def test_string_digits_are_coerced():
    """A caller sending "5000" must not slip past a check written for ints."""
    with pytest.raises(FreeTierExceeded):
        enforce_free_tier("x.do@v1", {"T": 1_000}, {"T": "5000"})


def test_uncoercible_value_is_left_to_the_handler():
    """Rejecting malformed input is the handler's job and its message is better."""
    enforce_free_tier("x.do@v1", {"T": 1_000}, {"T": "banana"})
    enforce_free_tier("x.do@v1", {"T": 1_000}, {"T": None})
    enforce_free_tier("x.do@v1", {"T": 1_000}, {"T": {"nested": 1}})


def test_float_over_ceiling_is_refused():
    with pytest.raises(FreeTierExceeded):
        enforce_free_tier("x.do@v1", {"T": 1_000}, {"T": 5000.7})


def test_negative_and_zero_pass():
    """Below the ceiling is below the ceiling; the handler owns the lower bound."""
    enforce_free_tier("x.do@v1", {"T": 1_000}, {"T": 0})
    enforce_free_tier("x.do@v1", {"T": 1_000}, {"T": -1})


def test_dotted_path_reaches_into_a_nested_object():
    """aestus.open@v1 takes T inside the puzzle — a top-level check would bound seal
    and leave the identical cost open one endpoint over."""
    enforce_free_tier("a.open@v1", {"puzzle.T": 1_000}, {"puzzle": {"T": 1_000}})
    with pytest.raises(FreeTierExceeded) as ei:
        enforce_free_tier("a.open@v1", {"puzzle.T": 1_000}, {"puzzle": {"T": 1_001}})
    assert ei.value.field == "puzzle.T"


def test_dotted_path_on_missing_or_wrong_shape_does_not_raise():
    enforce_free_tier("a.open@v1", {"puzzle.T": 1_000}, {})
    enforce_free_tier("a.open@v1", {"puzzle.T": 1_000}, {"puzzle": None})
    enforce_free_tier("a.open@v1", {"puzzle.T": 1_000}, {"puzzle": "not-an-object"})
    enforce_free_tier("a.open@v1", {"puzzle.T": 1_000}, {"puzzle": {}})


def test_several_ceilings_all_enforced():
    """aestus.seal has two independent cost knobs; bounding one is bounding none."""
    limits = {"T": 1_000_000, "modulus_bits": 2048}
    enforce_free_tier("a.seal@v1", limits, {"T": 1_000_000, "modulus_bits": 2048})
    with pytest.raises(FreeTierExceeded) as ei:
        enforce_free_tier("a.seal@v1", limits, {"T": 10, "modulus_bits": 3072})
    assert ei.value.field == "modulus_bits"


# ── PaidTierPolicy ───────────────────────────────────────────────────────────────


def test_default_policy_grants_nothing():
    """The shipped default. Fails closed: nobody is lifted, including the hub, until
    the operator says how a paid call is recognised."""
    p = PaidTierPolicy.from_env({})
    assert p.enabled is False
    assert p.is_paid({"X-Payment-Channel": "chan_whatever"}, "1.2.3.4") is False
    assert p.is_paid({"X-AIMarket-Paid-Tier": "guess"}, "1.2.3.4") is False


def test_secret_lifts_only_on_exact_match():
    p = PaidTierPolicy.from_env({"ORACLE_PAID_TIER_SECRET": "s3cret"})
    assert p.enabled is True
    assert p.is_paid({"X-AIMarket-Paid-Tier": "s3cret"}, "9.9.9.9") is True
    assert p.is_paid({"X-AIMarket-Paid-Tier": "s3cre"}, "9.9.9.9") is False
    assert p.is_paid({"X-AIMarket-Paid-Tier": "s3cretX"}, "9.9.9.9") is False
    assert p.is_paid({"X-AIMarket-Paid-Tier": ""}, "9.9.9.9") is False
    assert p.is_paid({}, "9.9.9.9") is False


def test_secret_header_is_case_insensitive():
    """HTTP header names are case-insensitive and Starlette normalises them; a policy
    that only matched one casing would work in tests and fail behind a proxy."""
    p = PaidTierPolicy.from_env({"ORACLE_PAID_TIER_SECRET": "s3cret"})
    assert p.is_paid({"x-aimarket-paid-tier": "s3cret"}, "9.9.9.9") is True


def test_secret_does_not_need_a_channel_header():
    """The secret is the operator vouching directly; requiring a channel id as well
    would only add a value the oracle cannot check anyway."""
    p = PaidTierPolicy.from_env({"ORACLE_PAID_TIER_SECRET": "s3cret"})
    assert p.is_paid({"X-AIMarket-Paid-Tier": "s3cret"}, "*") is True


def test_trusted_proxy_needs_both_the_address_and_a_channel():
    p = PaidTierPolicy.from_env({"ORACLE_TRUSTED_PAYMENT_PROXIES": "10.0.0.5"})
    assert p.is_paid({"X-Payment-Channel": "chan_1"}, "10.0.0.5") is True
    # Right address, no channel — the hub forwards a channel only when it took a hold.
    assert p.is_paid({}, "10.0.0.5") is False
    assert p.is_paid({"X-Payment-Channel": ""}, "10.0.0.5") is False
    # Channel, wrong address — a stranger echoing a channel id off a receipt.
    assert p.is_paid({"X-Payment-Channel": "chan_1"}, "10.0.0.6") is False


def test_trusted_proxy_accepts_cidr():
    p = PaidTierPolicy.from_env({"ORACLE_TRUSTED_PAYMENT_PROXIES": "10.0.0.0/24"})
    assert p.is_paid({"X-Payment-Channel": "c"}, "10.0.0.77") is True
    assert p.is_paid({"X-Payment-Channel": "c"}, "10.0.1.77") is False


def test_trusted_proxy_accepts_ipv6():
    p = PaidTierPolicy.from_env({"ORACLE_TRUSTED_PAYMENT_PROXIES": "2001:db8::/32"})
    assert p.is_paid({"X-Payment-Channel": "c"}, "2001:db8::1") is True
    assert p.is_paid({"X-Payment-Channel": "c"}, "2001:dba::1") is False


def test_malformed_proxy_entry_neither_widens_nor_breaks():
    """A typo in the env must not silently trust everyone, and must not stop the good
    entries beside it from working."""
    p = PaidTierPolicy.from_env(
        {"ORACLE_TRUSTED_PAYMENT_PROXIES": "not-an-ip, 10.0.0.5 ,300.1.1.1"}
    )
    assert p.is_paid({"X-Payment-Channel": "c"}, "10.0.0.5") is True
    assert p.is_paid({"X-Payment-Channel": "c"}, "8.8.8.8") is False


def test_unresolvable_client_key_is_not_trusted():
    """`client_key` falls back to "*" when there is no address at all."""
    p = PaidTierPolicy.from_env({"ORACLE_TRUSTED_PAYMENT_PROXIES": "10.0.0.5"})
    assert p.is_paid({"X-Payment-Channel": "c"}, "*") is False
    assert p.is_paid({"X-Payment-Channel": "c"}, "") is False


def test_blank_env_values_do_not_enable_the_policy():
    p = PaidTierPolicy.from_env(
        {"ORACLE_PAID_TIER_SECRET": "   ", "ORACLE_TRUSTED_PAYMENT_PROXIES": " , "}
    )
    assert p.enabled is False


# ── Protocol.invoke ──────────────────────────────────────────────────────────────


def _spec(tmp_path, **cap_kwargs) -> OracleSpec:
    return OracleSpec(
        name="Costly Oracle",
        product_id="prod-costly",
        description="d",
        public_url="http://localhost:9999",
        categories=["oracle"],
        signing_key_path=str(tmp_path / "key"),
        capabilities=[
            Capability(
                capability_id="costly.work@v1",
                product_id="prod-costly",
                description="does work proportional to T",
                handler=lambda d: {"did": int(d.get("T", 1))},
                input_schema={"type": "object", "properties": {"T": {"type": "integer"}}},
                **cap_kwargs,
            )
        ],
    )


@pytest.mark.asyncio
async def test_invoke_defaults_to_unpaid(tmp_path):
    """`paid` defaults to False so a call site added later is bounded until someone
    thinks about payment, rather than unbounded until someone notices."""
    proto = Protocol(_spec(tmp_path, free_tier_max={"T": 100}))
    with pytest.raises(FreeTierExceeded):
        await proto.invoke("costly.work@v1", {"T": 101})


@pytest.mark.asyncio
async def test_invoke_paid_lifts_the_ceiling(tmp_path):
    proto = Protocol(_spec(tmp_path, free_tier_max={"T": 100}))
    out = await proto.invoke("costly.work@v1", {"T": 10_000}, paid=True)
    assert out["output"]["did"] == 10_000


@pytest.mark.asyncio
async def test_a_refused_call_is_not_recorded_as_a_fast_success(tmp_path):
    """Refusals must not flatter the p50 the manifest advertises."""
    proto = Protocol(_spec(tmp_path, free_tier_max={"T": 100}))
    with pytest.raises(FreeTierExceeded):
        await proto.invoke("costly.work@v1", {"T": 101})
    assert proto.metrics.count("costly.work@v1") == 0
    tool = proto._tool_with_metrics(proto.spec.capability("costly.work@v1"))
    assert tool["metrics_source"] == "declared"


@pytest.mark.asyncio
async def test_capability_without_ceilings_is_untouched(tmp_path):
    """40 of 42 declare nothing; they must behave exactly as before."""
    proto = Protocol(_spec(tmp_path))
    out = await proto.invoke("costly.work@v1", {"T": 10 ** 9})
    assert out["output"]["did"] == 10 ** 9


# ── Manifest publication ─────────────────────────────────────────────────────────


def test_ceilings_are_published_so_a_buyer_reads_them_before_paying(tmp_path):
    proto = Protocol(
        _spec(tmp_path, free_tier_max={"T": 100}, cpu_budget_ms_per_min=20_000,
              global_cpu_budget_ms_per_min=60_000)
    )
    tool = proto.manifest()["tools"][0]
    assert tool["free_tier_max"] == {"T": 100}
    assert tool["cpu_budget_ms_per_min"] == 20_000
    assert tool["global_cpu_budget_ms_per_min"] == 60_000


def test_a_capability_with_no_controls_adds_no_keys(tmp_path):
    """Byte-identical tool entries for the 40 untouched capabilities."""
    tool = Protocol(_spec(tmp_path)).manifest()["tools"][0]
    assert "free_tier_max" not in tool
    assert "cpu_budget_ms_per_min" not in tool
    assert "global_cpu_budget_ms_per_min" not in tool


def test_published_ceilings_do_not_break_the_manifest_signature(tmp_path):
    proto = Protocol(_spec(tmp_path, free_tier_max={"T": 100}, cpu_budget_ms_per_min=20_000))
    assert proto.signer.verify_manifest_signature(proto.manifest()) is True


# ── Cost estimation ──────────────────────────────────────────────────────────────


def test_no_cost_formula_means_one_unit(tmp_path):
    cap = _spec(tmp_path).capabilities[0]
    assert cap.estimate_cost_ms({"T": 10 ** 9}) == 1.0


def test_cost_formula_is_used(tmp_path):
    cap = _spec(tmp_path, cost_ms=lambda d: d["T"] / 10.0).capabilities[0]
    assert cap.estimate_cost_ms({"T": 5_000}) == 500.0


def test_cost_is_floored_at_one(tmp_path):
    """A zero-cost estimate would make a budget admit unlimited calls."""
    cap = _spec(tmp_path, cost_ms=lambda d: 0).capabilities[0]
    assert cap.estimate_cost_ms({}) == 1.0
    cap = _spec(tmp_path, cost_ms=lambda d: -50).capabilities[0]
    assert cap.estimate_cost_ms({}) == 1.0


def test_a_broken_cost_formula_degrades_instead_of_500ing(tmp_path):
    """A cost estimator is a convenience; it must not be able to take the oracle down."""
    cap = _spec(tmp_path, cost_ms=lambda d: d["absent"]).capabilities[0]
    assert cap.estimate_cost_ms({}) == 1.0
    cap = _spec(tmp_path, cost_ms=lambda d: "not a number").capabilities[0]
    assert cap.estimate_cost_ms({}) == 1.0


# ── HTTP surface ─────────────────────────────────────────────────────────────────


def _client(tmp_path, policy=None, **cap_kwargs) -> TestClient:
    app = create_app(
        _spec(tmp_path, **cap_kwargs),
        paid_tier=policy or PaidTierPolicy.from_env({}),
    )
    return TestClient(app)


def test_over_the_ceiling_answers_402_not_400(tmp_path):
    """Nothing is malformed — the caller asked for work this tier does not include, and
    402 is the status the rest of the ecosystem already uses for exactly that."""
    c = _client(tmp_path, free_tier_max={"T": 100})
    r = c.post("/ai-market/v2/invoke", json={"capability_id": "costly.work@v1", "input": {"T": 101}})
    assert r.status_code == 402
    body = r.json()
    assert body["error"] == "payment_required"
    assert body["free_tier"]["max"] == 100


def test_under_the_ceiling_still_works_unpaid(tmp_path):
    c = _client(tmp_path, free_tier_max={"T": 100})
    r = c.post("/ai-market/v2/invoke", json={"capability_id": "costly.work@v1", "input": {"T": 100}})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_secret_header_lifts_the_ceiling_over_http(tmp_path):
    policy = PaidTierPolicy.from_env({"ORACLE_PAID_TIER_SECRET": "s3cret"})
    c = _client(tmp_path, policy=policy, free_tier_max={"T": 100})
    body = {"capability_id": "costly.work@v1", "input": {"T": 5_000}}
    assert c.post("/ai-market/v2/invoke", json=body).status_code == 402
    r = c.post("/ai-market/v2/invoke", json=body, headers={"X-AIMarket-Paid-Tier": "s3cret"})
    assert r.status_code == 200
    assert r.json()["output"]["did"] == 5_000


def test_wrong_secret_does_not_lift(tmp_path):
    policy = PaidTierPolicy.from_env({"ORACLE_PAID_TIER_SECRET": "s3cret"})
    c = _client(tmp_path, policy=policy, free_tier_max={"T": 100})
    r = c.post(
        "/ai-market/v2/invoke",
        json={"capability_id": "costly.work@v1", "input": {"T": 5_000}},
        headers={"X-AIMarket-Paid-Tier": "wrong"},
    )
    assert r.status_code == 402


def test_trusted_proxy_lift_uses_the_forwarded_address(tmp_path):
    """The hub is the TCP client, so nginx sets X-Real-IP to the hub's address — the
    same header the rate limiter keys on."""
    policy = PaidTierPolicy.from_env({"ORACLE_TRUSTED_PAYMENT_PROXIES": "10.0.0.5"})
    c = _client(tmp_path, policy=policy, free_tier_max={"T": 100})
    body = {"capability_id": "costly.work@v1", "input": {"T": 5_000}}
    r = c.post(
        "/ai-market/v2/invoke", json=body,
        headers={"X-Real-IP": "10.0.0.5", "X-Payment-Channel": "chan_1"},
    )
    assert r.status_code == 200
    r = c.post(
        "/ai-market/v2/invoke", json=body,
        headers={"X-Real-IP": "10.0.0.6", "X-Payment-Channel": "chan_1"},
    )
    assert r.status_code == 402


_COST = {"cost_ms": lambda d: float(d.get("T", 1))}


def test_the_budget_rations_work_not_calls(tmp_path):
    """The property that made this replace a flat call limit: a cheap input stays
    freely repeatable while an expensive one spends the whole minute in one call."""
    c = _client(tmp_path, cpu_budget_ms_per_min=1_000, **_COST)
    cheap = {"capability_id": "costly.work@v1", "input": {"T": 1}}
    codes = [c.post("/ai-market/v2/invoke", json=cheap).status_code for _ in range(50)]
    assert codes == [200] * 50  # 50 ms of a 1000 ms budget

    c2 = _client(tmp_path, cpu_budget_ms_per_min=1_000, **_COST)
    dear = {"capability_id": "costly.work@v1", "input": {"T": 900}}
    assert c2.post("/ai-market/v2/invoke", json=dear).status_code == 200
    assert c2.post("/ai-market/v2/invoke", json=dear).status_code == 429


def test_a_single_call_over_the_whole_budget_is_refused(tmp_path):
    """Not admitted-then-blocking: otherwise one oversized call gets through every
    window no matter how large it is, which is the case the budget exists to stop."""
    c = _client(tmp_path, cpu_budget_ms_per_min=1_000, **_COST)
    r = c.post("/ai-market/v2/invoke", json={"capability_id": "costly.work@v1", "input": {"T": 5_000}})
    assert r.status_code == 429


def test_budget_429_names_the_budget_the_cost_and_the_remainder(tmp_path):
    """A caller well under 120/min who is refused anyway needs all three numbers to
    know whether to wait or to ask for less work."""
    c = _client(tmp_path, cpu_budget_ms_per_min=1_000, **_COST)
    body = {"capability_id": "costly.work@v1", "input": {"T": 900}}
    c.post("/ai-market/v2/invoke", json=body)
    r = c.post("/ai-market/v2/invoke", json=body)
    assert r.status_code == 429
    detail = r.json()["detail"]
    assert "costly.work@v1" in detail
    assert "1000 ms" in detail          # the budget
    assert "~900 ms" in detail          # what this call would cost
    assert "100 ms remain" in detail    # what is left


def test_budget_is_per_client(tmp_path):
    c = _client(tmp_path, cpu_budget_ms_per_min=1_000, **_COST)
    body = {"capability_id": "costly.work@v1", "input": {"T": 900}}
    for ip, expected in (("1.1.1.1", 200), ("1.1.1.1", 429), ("2.2.2.2", 200)):
        r = c.post("/ai-market/v2/invoke", json=body, headers={"X-Real-IP": ip})
        assert r.status_code == expected, ip


def test_global_budget_binds_a_distributed_caller(tmp_path):
    """Per-IP budgets are the wrong tool against a proxy fleet; this is the one that
    protects the machine."""
    c = _client(tmp_path, cpu_budget_ms_per_min=10_000, global_cpu_budget_ms_per_min=3_000, **_COST)
    body = {"capability_id": "costly.work@v1", "input": {"T": 1_000}}
    codes = [
        c.post("/ai-market/v2/invoke", json=body, headers={"X-Real-IP": f"5.5.5.{i}"}).status_code
        for i in range(5)
    ]
    assert codes == [200, 200, 200, 429, 429]


def test_global_429_says_it_is_capacity_not_the_caller(tmp_path):
    c = _client(tmp_path, global_cpu_budget_ms_per_min=1_000, **_COST)
    body = {"capability_id": "costly.work@v1", "input": {"T": 900}}
    c.post("/ai-market/v2/invoke", json=body, headers={"X-Real-IP": "5.5.5.1"})
    r = c.post("/ai-market/v2/invoke", json=body, headers={"X-Real-IP": "5.5.5.2"})
    assert r.status_code == 429
    assert "across all clients" in r.json()["detail"]


def test_a_capacity_refusal_does_not_debit_the_client_budget(tmp_path):
    """Both budgets are tested before either is charged. Otherwise a caller refused for
    server capacity silently loses their own allowance for work never performed."""
    c = _client(
        tmp_path, cpu_budget_ms_per_min=10_000, global_cpu_budget_ms_per_min=1_000, **_COST
    )
    body = {"capability_id": "costly.work@v1", "input": {"T": 900}}
    # Another client exhausts the shared budget first.
    assert c.post("/ai-market/v2/invoke", json=body, headers={"X-Real-IP": "1.1.1.1"}).status_code == 200
    # We are refused on capacity...
    assert c.post("/ai-market/v2/invoke", json=body, headers={"X-Real-IP": "2.2.2.2"}).status_code == 429
    # ...and our own budget is untouched, so a smaller request that fits still works.
    small = {"capability_id": "costly.work@v1", "input": {"T": 50}}
    assert c.post("/ai-market/v2/invoke", json=small, headers={"X-Real-IP": "2.2.2.2"}).status_code == 200


def test_over_the_ceiling_answers_402_even_when_it_also_blows_the_budget(tmp_path):
    """Order matters. A request over the free ceiling is over it permanently, so a 429
    "retry shortly" would send the caller into a loop that can never succeed — while
    402 names the ceiling and says payment lifts it. The budget check, whose refusal
    really does clear on its own, is the one that comes second."""
    c = _client(tmp_path, free_tier_max={"T": 100}, cpu_budget_ms_per_min=1_000, **_COST)
    r = c.post("/ai-market/v2/invoke", json={"capability_id": "costly.work@v1", "input": {"T": 50_000}})
    assert r.status_code == 402
    assert r.json()["free_tier"]["max"] == 100


def test_a_paid_caller_over_the_budget_gets_429_not_402(tmp_path):
    """Paid lifts the ceiling, not the machine's capacity."""
    policy = PaidTierPolicy.from_env({"ORACLE_PAID_TIER_SECRET": "k"})
    c = _client(tmp_path, policy=policy, free_tier_max={"T": 100},
                cpu_budget_ms_per_min=1_000, **_COST)
    r = c.post(
        "/ai-market/v2/invoke",
        json={"capability_id": "costly.work@v1", "input": {"T": 50_000}},
        headers={"X-AIMarket-Paid-Tier": "k"},
    )
    assert r.status_code == 429


def test_an_over_ceiling_request_does_not_spend_the_budget(tmp_path):
    """A 402'd request performs no work, so it must cost no budget — otherwise probing
    the ceiling would lock a caller out of the calls they are entitled to."""
    c = _client(tmp_path, free_tier_max={"T": 100}, cpu_budget_ms_per_min=1_000, **_COST)
    for _ in range(5):
        assert c.post(
            "/ai-market/v2/invoke",
            json={"capability_id": "costly.work@v1", "input": {"T": 50_000}},
        ).status_code == 402
    # Budget untouched: a legal call of nearly the whole budget still fits.
    r = c.post("/ai-market/v2/invoke", json={"capability_id": "costly.work@v1", "input": {"T": 100}})
    assert r.status_code == 200


def test_unknown_capability_still_says_so_rather_than_402(tmp_path):
    c = _client(tmp_path, free_tier_max={"T": 100}, cpu_budget_ms_per_min=1_000, **_COST)
    r = c.post("/ai-market/v2/invoke", json={"capability_id": "nope.nope@v1", "input": {}})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "Unknown capability" in r.json()["error"]


def test_a_capability_with_no_rate_limit_keeps_the_app_wide_one(tmp_path):
    """The two new buckets can only tighten; a capability that declares neither must
    behave exactly as it did before."""
    app = create_app(_spec(tmp_path), invoke_rate_limit=2, paid_tier=PaidTierPolicy.from_env({}))
    c = TestClient(app)
    body = {"capability_id": "costly.work@v1", "input": {"T": 1}}
    codes = [c.post("/ai-market/v2/invoke", json=body).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_budgeted_out_calls_do_not_reach_the_handler(tmp_path):
    """The whole point is that refused work is not performed."""
    calls: list[int] = []
    spec = _spec(tmp_path, cpu_budget_ms_per_min=1_000, **_COST)
    spec.capabilities[0].handler = lambda d: (calls.append(1), {"did": 1})[1]
    c = TestClient(create_app(spec, paid_tier=PaidTierPolicy.from_env({})))
    body = {"capability_id": "costly.work@v1", "input": {"T": 900}}
    for _ in range(4):
        c.post("/ai-market/v2/invoke", json=body)
    assert len(calls) == 1


def test_refused_free_tier_call_does_not_reach_the_handler(tmp_path):
    calls: list[int] = []
    spec = _spec(tmp_path, free_tier_max={"T": 100})
    spec.capabilities[0].handler = lambda d: (calls.append(1), {"did": 1})[1]
    c = TestClient(create_app(spec, paid_tier=PaidTierPolicy.from_env({})))
    c.post("/ai-market/v2/invoke", json={"capability_id": "costly.work@v1", "input": {"T": 10 ** 9}})
    assert calls == []
