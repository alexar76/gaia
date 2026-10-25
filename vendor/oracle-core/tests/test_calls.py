"""The call log: every invoke written down, the operator's stats, metrics and privacy.

Runs on SQLite always, and on Postgres too when ORACLE_TEST_POSTGRES_URL names a server
(the same assertions, so the two backends cannot drift).
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from oracle_core import Capability, OracleSpec, PaidTierPolicy, create_app
from oracle_core.calls import CallLog, from_env, outcome_for
from oracle_core.price_gate import PriceGate
from oracle_core.storage import Store, open_store

from test_price_gate import AUD, HUB, HUB_KEY, PUBLIC, _signed

TOKEN = "ops-test-token"
PG_URL = os.environ.get("ORACLE_TEST_POSTGRES_URL", "")


def _spec(tmp_path):
    def broken(_d):
        raise RuntimeError("upstream down")

    def picky(d):
        if "msg" not in d:
            raise ValueError("msg is required")
        return {"echo": d["msg"]}

    def quotes(d):
        raise ValueError(f"{d.get('vat')!r} is not a VAT number; {str(d.get('note'))[:12]!r} ignored")

    return OracleSpec(
        name="Log Oracle", product_id="prod-log", description="test", public_url=PUBLIC,
        categories=["test"], signing_key_path=str(tmp_path / "key"),
        capabilities=[
            Capability("log.priced@v1", "priced", handler=picky, price_per_call_usd=0.002),
            Capability("log.free@v1", "free", handler=lambda d: {"free": True}, price_per_call_usd=0.0),
            Capability("log.broken@v1", "broken", handler=broken, price_per_call_usd=0.0),
            Capability("log.quotes@v1", "quotes its input", handler=quotes, price_per_call_usd=0.0),
        ],
    )


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def store(request, tmp_path):
    if request.param == "postgres":
        s = Store(PG_URL)
        s.execute("DROP TABLE IF EXISTS oracle_calls")
    else:
        s = open_store(f"sqlite:///{tmp_path / 'calls.sqlite'}")
    yield s
    s.close()


def _app(tmp_path, store, *, enforce=True, rate=120, monkeypatch=None):
    env = {"ORACLE_TRUSTED_HUBS": f"{HUB}={HUB_KEY.public_b64}"} if enforce else {}
    log = CallLog(store, "prod-log", flush_s=0.05, salt=b"s" * 16)
    app = create_app(_spec(tmp_path), price_gate=PriceGate.from_env(PUBLIC, env),
                     paid_tier=PaidTierPolicy(), call_log=log, invoke_rate_limit=rate)
    # As our nginx reaches it: from loopback, the caller in X-Real-IP.
    return TestClient(app, client=("127.0.0.1", 50000)), log


def _call(c, cap, inp=None, headers=None, ip="203.0.113.7"):
    return c.post("/ai-market/v2/invoke", headers={"X-Real-IP": ip, **(headers or {})},
                  json={"capability_id": cap, "input": inp if inp is not None else {"msg": "hi"}})


def test_every_way_an_invoke_ends_is_one_row(tmp_path, store):
    c, log = _app(tmp_path, store)
    assert _call(c, "log.free@v1").status_code == 200                       # ok, free
    assert _call(c, "log.priced@v1").status_code == 402                     # no hub signature
    assert _call(c, "log.priced@v1", headers=_signed("log.priced@v1")).status_code == 200  # hub
    assert _call(c, "log.priced@v1", inp={}, headers=_signed("log.priced@v1", {})).json()["ok"] is False
    assert _call(c, "log.broken@v1").json()["ok"] is False                  # handler failed
    assert _call(c, "log.nope@v1").json()["ok"] is False                    # unknown capability
    log.flush()
    rows = log.calls(since=0, until=time.time() + 1, limit=50)
    seen = sorted((r["capability_id"], r["outcome"], r["http_status"], r["caller_kind"]) for r in rows)
    assert seen == sorted([
        ("log.free@v1", "ok", 200, "anonymous"),
        ("log.priced@v1", "payment_required", 402, "anonymous"),
        ("log.priced@v1", "ok", 200, "hub"),
        ("log.priced@v1", "failed", 200, "hub"),
        ("log.broken@v1", "failed", 200, "anonymous"),
        ("log.nope@v1", "failed", 200, "anonymous"),
    ])
    hubbed = [r for r in rows if r["caller_kind"] == "hub" and r["outcome"] == "ok"][0]
    assert hubbed["caller_hub"].rstrip("/") == HUB and hubbed["price_usd"] == pytest.approx(0.002)
    failed = [r for r in rows if r["capability_id"] == "log.broken@v1"][0]
    assert "upstream down" in failed["reason"]


def test_revenue_counts_only_served_calls_someone_vouched_for(tmp_path, store):
    c, log = _app(tmp_path, store, enforce=False)          # gate off: priced calls are served
    for _ in range(3):
        _call(c, "log.priced@v1")                          # anonymous: never revenue
    log.flush()
    stats = log.stats(since=0, until=time.time() + 1)
    assert stats["capabilities"]["log.priced@v1"]["outcomes"] == {"ok": 3}
    assert stats["revenue_usd"] == 0.0


def test_the_operators_own_paid_tier_reader_is_never_revenue(tmp_path, store):
    log = CallLog(store, "prod-log", flush_s=0.05, salt=b"s" * 16)
    app = create_app(_spec(tmp_path), price_gate=PriceGate.from_env(PUBLIC, {"ORACLE_TRUSTED_HUBS": f"{HUB}={HUB_KEY.public_b64}"}),
                     paid_tier=PaidTierPolicy(secret="operator-reader"), call_log=log)
    c = TestClient(app)
    r = _call(c, "log.priced@v1", headers={"X-AIMarket-Paid-Tier": "operator-reader"})
    assert r.status_code == 200 and r.json()["ok"] is True       # served: the operator's own reader
    log.flush()
    row = log.calls(since=0, until=time.time() + 1)[0]
    assert row["caller_kind"] == "paid_tier" and row["price_usd"] == 0.0
    assert log.stats(since=0, until=time.time() + 1)["revenue_usd"] == 0.0


def test_a_rate_limited_call_is_logged_too(tmp_path, store):
    c, log = _app(tmp_path, store, rate=1)
    _call(c, "log.free@v1")
    assert _call(c, "log.free@v1").status_code == 429
    log.flush()
    outcomes = [r["outcome"] for r in log.calls(since=0, until=time.time() + 1)]
    assert sorted(outcomes) == ["ok", "rate_limited"]


def test_no_client_address_is_stored(tmp_path, store):
    c, log = _app(tmp_path, store)
    _call(c, "log.free@v1", ip="198.51.100.23")
    _call(c, "log.free@v1", ip="198.51.100.23")
    _call(c, "log.free@v1", ip="198.51.100.99")
    log.flush()
    rows = store.query("SELECT * FROM oracle_calls")
    assert rows and not any("198.51.100" in str(v) for row in rows for v in row)
    assert log.stats(since=0, until=time.time() + 1)["distinct_clients"] == 2


def test_the_input_itself_is_never_stored(tmp_path, store):
    c, log = _app(tmp_path, store)
    _call(c, "log.free@v1", inp={"secret": "VAT DE123456789"})
    log.flush()
    rows = store.query("SELECT * FROM oracle_calls")
    assert not any("DE123456789" in str(v) for row in rows for v in row)
    assert len(log.calls(since=0, until=time.time() + 1)[0]["input_sha256"]) == 64


def test_an_error_that_quotes_the_input_is_stored_without_it(tmp_path, store):
    c, log = _app(tmp_path, store)
    body = _call(c, "log.quotes@v1", inp={"vat": "DE123456789",
                                          "note": "Hauptstrasse 5, Berlin"}).json()
    assert body["ok"] is False and "DE123456789" in body["error"]     # the caller still sees it
    log.flush()
    reason = log.calls(since=0, until=time.time() + 1)[0]["reason"]
    assert "DE123456789" not in reason and "Hauptstrasse" not in reason
    assert "is not a VAT number" in reason


def test_old_rows_are_purged(tmp_path, store):
    c, log = _app(tmp_path, store)
    _call(c, "log.free@v1")
    log.flush()
    store.execute("UPDATE oracle_calls SET ts = ?", (time.time() - 100 * 86_400,))
    _call(c, "log.free@v1")
    log.flush()
    log.purge()
    assert len(log.calls(since=0, until=time.time() + 1)) == 1


def test_ops_api_is_closed_without_a_token_and_open_with_it(tmp_path, store, monkeypatch):
    c, log = _app(tmp_path, store)
    _call(c, "log.free@v1")
    log.flush()
    monkeypatch.delenv("ORACLE_OPS_TOKEN", raising=False)
    assert c.get("/ops/v1/stats").status_code == 503
    monkeypatch.setenv("ORACLE_OPS_TOKEN", TOKEN)
    assert c.get("/ops/v1/stats").status_code == 401
    assert c.get("/ops/v1/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    stats = c.get("/ops/v1/stats", headers=auth).json()
    assert stats["calls"] == 1 and stats["service"] == "prod-log"
    calls = c.get("/ops/v1/calls?capability=log.free@v1", headers=auth).json()["calls"]
    assert calls[0]["outcome"] == "ok"
    metrics = c.get("/ops/v1/metrics", headers=auth)
    assert metrics.headers["content-type"].startswith("text/plain")
    assert 'oracle_calls_total{service="prod-log",capability="log.free@v1",outcome="ok"} 1' in metrics.text
    assert "oracle_call_duration_seconds_bucket" in metrics.text
    assert c.get("/ops/v1/stats?since=yesterday", headers=auth).status_code == 400


def test_a_failing_database_never_breaks_an_answer(tmp_path, store, monkeypatch):
    c, log = _app(tmp_path, store)

    def down(*_a, **_k):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(store, "executemany", down)     # every later write fails
    for _ in range(3):
        r = _call(c, "log.free@v1")
        assert r.status_code == 200 and r.json()["ok"] is True
    log.flush()
    assert log.dropped >= 3 and log.write_errors >= 1      # counted, not hidden


def test_outcomes_from_status():
    assert outcome_for(200, {"ok": True}) == "ok"
    assert outcome_for(200, {"ok": False}) == "failed"
    assert outcome_for(402) == "payment_required"
    assert outcome_for(401) == "unauthorized"
    assert outcome_for(429) == "rate_limited"
    assert outcome_for(500) == "error"


def test_off_unless_asked(monkeypatch, tmp_path):
    monkeypatch.delenv("ORACLE_CALLS", raising=False)
    assert from_env("svc") is None
    monkeypatch.setenv("ORACLE_CALLS", "1")
    monkeypatch.setenv("ORACLE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ORACLE_DATABASE_URL", raising=False)
    log = from_env("svc")
    assert log is not None and (tmp_path / "oracle.sqlite").is_file()
    assert (tmp_path / "calls.salt").stat().st_mode & 0o777 == 0o600
    log.close()


def test_call_log_true_keeps_one_without_the_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ORACLE_CALLS", raising=False)
    monkeypatch.delenv("ORACLE_DATABASE_URL", raising=False)
    monkeypatch.setenv("ORACLE_DATA_DIR", str(tmp_path))
    app = create_app(_spec(tmp_path), call_log=True)
    assert isinstance(app.state.call_log, CallLog)
    _call(TestClient(app), "log.free@v1")
    app.state.call_log.flush()
    assert len(app.state.call_log.calls(since=0, until=time.time() + 1)) == 1
    assert create_app(_spec(tmp_path), call_log=False).state.call_log is None
    app.state.call_log.close()


@pytest.mark.skipif(not PG_URL, reason="needs ORACLE_TEST_POSTGRES_URL")
def test_postgres_reconnects_after_a_lost_connection(tmp_path):
    s = Store(PG_URL)
    s.execute("DROP TABLE IF EXISTS oracle_calls")
    c, log = _app(tmp_path, s)
    s._conn.close()                     # the server dropped us (restart, idle timeout)
    _call(c, "log.free@v1")
    log.flush()
    assert len(log.calls(since=0, until=time.time() + 1)) == 1 and log.dropped == 0
    s.close()
