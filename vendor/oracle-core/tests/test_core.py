import pytest
from httpx import ASGITransport, AsyncClient

from oracle_core import Capability, OracleSpec, Signer, create_app
from oracle_core.protocol import Protocol


def _spec(tmp_path):
    return OracleSpec(
        name="Test Oracle",
        product_id="prod-test",
        description="test",
        public_url="http://localhost:9999",
        categories=["test"],
        signing_key_path=str(tmp_path / "key"),
        capabilities=[
            Capability("test.echo@v1", "echo", handler=lambda d: {"echo": d.get("msg", "")}, price_per_call_usd=0.002),
            Capability("test.async@v1", "async", handler=_async_handler),
        ],
    )


async def _async_handler(d):
    return {"doubled": d.get("n", 0) * 2}


class TestProtocol:
    def test_manifest_self_verifies(self, tmp_path):
        proto = Protocol(_spec(tmp_path))
        m = proto.manifest()
        assert m["capabilities_count"] == 2
        assert proto.signer.verify_manifest_signature(m) is True

    @pytest.mark.asyncio
    async def test_invoke_envelope_and_receipt(self, tmp_path):
        proto = Protocol(_spec(tmp_path))
        r = await proto.invoke("test.echo@v1", {"msg": "hi"})
        assert r["output"] == {"echo": "hi"}
        assert r["price_usd"] == 0.002
        assert len(r["provenance"]["input_hash"]) == 64
        assert proto.signer.verify_receipt(r["receipt"]) is True

    @pytest.mark.asyncio
    async def test_async_handler(self, tmp_path):
        proto = Protocol(_spec(tmp_path))
        r = await proto.invoke("test.async@v1", {"n": 21})
        assert r["output"] == {"doubled": 42}

    def test_unknown_capability_raises(self, tmp_path):
        with pytest.raises(ValueError):
            Protocol(_spec(tmp_path)).spec.capability("nope@v1")

    @pytest.mark.asyncio
    async def test_measured_metrics_after_invoke(self, tmp_path):
        proto = Protocol(_spec(tmp_path))
        await proto.invoke("test.echo@v1", {"msg": "x"})
        tool = next(t for t in proto.manifest()["tools"] if t["capability_id"] == "test.echo@v1")
        assert tool["metrics_source"] == "measured" and tool["calls_observed"] >= 1


class TestApp:
    @pytest.mark.asyncio
    async def test_endpoints(self, tmp_path):
        app = create_app(_spec(tmp_path))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/api/health")).json()["status"] == "ok"
            wk = (await c.get("/.well-known/ai-market.json")).json()
            assert wk["protocol_version"] == "v2" and wk["signer_public_key"]
            inv = (await c.post("/ai-market/v2/invoke", json={"capability_id": "test.echo@v1", "input": {"msg": "yo"}})).json()
            assert inv["ok"] is True and inv["output"] == {"echo": "yo"}
            bad = (await c.post("/ai-market/v2/invoke", json={"capability_id": "x@v1", "input": {}})).json()
            assert bad["ok"] is False


class TestSigningPQC:
    def test_hybrid_when_enabled(self, tmp_path):
        from oracle_core.signing import pqc_available

        if not pqc_available():
            pytest.skip("dilithium-py not installed")
        s = Signer(tmp_path / "k", pqc=True)
        sig = s.sign_payload("a|b")
        assert sig.get("pq_algorithm") == "ml-dsa-65"
        assert Signer.verify_signature_object("a|b", sig) is True
        bad = dict(sig)
        bad["pq_value"] = "AA" + sig["pq_value"][2:]
        assert Signer.verify_signature_object("a|b", bad) is False


# ── Invoke error translation ─────────────────────────────────────
# Only ValueError used to become {"ok": false, "error": …}; every other way a handler can
# say "no" escaped as a bare 500 Internal Server Error with an empty body. A caller that
# read the schema and mistyped one field got nothing to correct, and a federated refusal
# ("Unknown capability: platon.verify@v1") was replaced by silence.

def _err_spec(tmp_path):
    def missing_field(d):
        return {"v": d["required_field"]}          # KeyError

    def bad_input(d):
        raise ValueError("points must be a list of [x, y] pairs")

    def upstream_refused(d):
        raise RuntimeError("Unknown capability: platon.verify@v1")

    def genuine_fault(d):
        return 1 / 0                                # ZeroDivisionError

    return OracleSpec(
        name="Err Oracle", product_id="prod-err", description="err",
        public_url="http://localhost:9999", categories=["test"],
        signing_key_path=str(tmp_path / "errkey"),
        capabilities=[
            Capability("err.missing@v1", "missing", handler=missing_field),
            Capability("err.bad@v1", "bad", handler=bad_input),
            Capability("err.upstream@v1", "upstream", handler=upstream_refused),
            Capability("err.fault@v1", "fault", handler=genuine_fault),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cap,needle",
    [
        ("err.missing@v1", "missing required input field: required_field"),
        ("err.bad@v1", "points must be a list of [x, y] pairs"),
        ("err.upstream@v1", "Unknown capability: platon.verify@v1"),
    ],
)
async def test_refusals_reach_the_caller_with_their_reason(tmp_path, cap, needle):
    app = create_app(_err_spec(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/ai-market/v2/invoke", json={"capability_id": cap, "input": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert needle in body["error"], body


@pytest.mark.asyncio
async def test_a_real_fault_stays_a_5xx_but_says_what_broke(tmp_path):
    """A crash must not be laundered into a 200 — but an empty body is indistinguishable
    from a dead process, so the type and message travel with the 500."""
    app = create_app(_err_spec(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/ai-market/v2/invoke", json={"capability_id": "err.fault@v1", "input": {}})
    assert r.status_code == 500
    assert "ZeroDivisionError" in r.text


@pytest.mark.asyncio
async def test_manifest_validates_against_the_published_protocol_schema(tmp_path):
    """The whole point of a manifest is that a hub will accept it.

    `p50_latency_ms` is declared `integer` in aimarket-protocol/schemas/manifest.json.
    oracle_core emitted `round(p50, 2)`, so once a capability had been called and its
    measured latency was fractional (2261.84), every hub rejected the entire manifest and
    federated none of the oracle's capabilities.
    """
    import json
    from pathlib import Path

    # Both guards must come BEFORE the work, and the import is one of them. `jsonschema` is not
    # in the `dev` extra (pytest, pytest-asyncio, httpx), so a top-of-function import turned
    # "this check needs the monorepo" into a hard ImportError for anyone who ran the shipped
    # suite after `pip install aimarket-oracle-core[dev]` — the first thing a new contributor
    # does. The schema guard below was already written as a skip; the import was not.
    jsonschema = pytest.importorskip(
        "jsonschema", reason="jsonschema is not in the [dev] extra; monorepo-only check"
    )

    schema_path = (
        Path(__file__).resolve().parents[3] / "aimarket-protocol" / "schemas" / "manifest.json"
    )
    if not schema_path.is_file():
        pytest.skip(f"protocol schema not present at {schema_path}")

    app = create_app(_spec(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # Call one capability first so the metrics path (not just the declared default)
        # is what gets serialised — that is the shape that actually broke.
        await c.post("/ai-market/v2/invoke", json={"capability_id": "test.echo@v1", "input": {"msg": "x"}})
        manifest = (await c.get("/ai-market/v2/manifest")).json()

    for tool in manifest.get("tools") or []:
        assert isinstance(tool["p50_latency_ms"], int), tool
    jsonschema.validate(manifest, json.loads(schema_path.read_text()))


@pytest.mark.asyncio
async def test_the_hub_can_verify_an_oracle_manifest_signature(tmp_path):
    """Cross-implementation check: the hub's verifier against an oracle's signature.

    These are two separate codebases signing the same canonical string, and they had
    drifted — the hub added a fifth field (`by_hub_hash`) to stop a relay tampering with
    peer trust, oracle_core kept signing four, and so EVERY oracle manifest was rejected
    with "Invalid manifest signature". Federation was silently dead; the only federated
    rows in the live catalogue predated the hub's change. A unit test inside either
    package would have passed — only checking one against the other catches this.
    """
    import sys
    from pathlib import Path

    hub_pkg = Path(__file__).resolve().parents[3] / "aimarket-hub"
    if not (hub_pkg / "aimarket_hub" / "signing.py").is_file():
        pytest.skip("aimarket-hub not available in this checkout")
    sys.path.insert(0, str(hub_pkg))
    try:
        from aimarket_hub.signing import Signer as HubSigner
    except Exception as exc:  # noqa: BLE001 — hub deps may be absent in the oracle venv
        pytest.skip(f"aimarket_hub not importable: {exc}")

    app = create_app(_spec(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        manifest = (await c.get("/ai-market/v2/manifest")).json()

    oracle_key = manifest["signature"]["public_key"]
    hub_signer = HubSigner(str(tmp_path / "hubkey"))
    assert hub_signer.verify_manifest_signature(manifest, oracle_key), (
        "the hub rejects this oracle's manifest — the canonical forms have diverged again"
    )
