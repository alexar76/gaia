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
