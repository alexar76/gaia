"""The two readers that called GAIA's priced reads directly, against a GAIA that enforces.

With oracle_core's price gate on, a priced capability called directly on GAIA answers 402
naming the hub. Two readers in this repository did exactly that:

* the GAIA/ATLAS sensors course (courses/gaia-atlas-sensors-course), whose lab01 reads
  ``gaia.weather.read@v1`` ($0.001); it now buys it on the hub's free sandbox trial;
* GAIA's own W3C WoT Thing Descriptions, whose forms pointed at GAIA's invoke; with the
  gate on they point at the hub.

Both run here in process against a real hub that sells GAIA (AIMARKET_SELLS_FOR) and
signs what it admits, and a real GAIA that pins that hub and enforces. Only the network
between them is replaced. Monorepo only: the standalone GAIA repository has neither the
hub nor the course, and a core older than 0.5 has no gate.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("aimarket_hub")
pytest.importorskip("oracle_core.price_gate")
pytest.importorskip("aimarket_hub.provider_request_auth")  # the hub that signs

_COURSE = Path(__file__).resolve().parents[2] / "courses" / "gaia-atlas-sensors-course"
if not (_COURSE / "courselib" / "sensors.py").is_file():
    pytest.skip("the sensors course is not in this checkout", allow_module_level=True)
sys.path.insert(0, str(_COURSE))

import aimarket_hub.api as hub_api  # noqa: E402
import aimarket_hub.channels as channels_mod  # noqa: E402
import aimarket_hub.outbound_http as outbound  # noqa: E402
import aimarket_hub.sandbox_trials as trials  # noqa: E402
from aimarket_hub.api import create_app  # noqa: E402
from aimarket_hub.channels import ChannelLedger  # noqa: E402
from aimarket_hub.config import HubConfig  # noqa: E402
from aimarket_hub.database import HubDatabase  # noqa: E402
from aimarket_hub.models import Capability, Peer  # noqa: E402
from aimarket_hub.signing import Signer  # noqa: E402
from courselib import sensors  # noqa: E402

from gaia.app import build_app  # noqa: E402
from gaia.capabilities import GatewayRuntime  # noqa: E402

HUB = "http://hub.test"
GAIA = "https://iot.example"
READ = {"capability_id": "gaia.weather.read@v1", "input": {"device_id": "ws-01"}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    # ── The hub: sells GAIA, gives 5 trial calls a visitor an hour, signs what it admits.
    monkeypatch.setenv("AIMARKET_HUB_URL", HUB)
    monkeypatch.setenv("AIMARKET_SELLS_FOR", GAIA)
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_SKIP_SEED", "1")
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    monkeypatch.setenv("AIMARKET_SANDBOX_MAX_PER_VISITOR", "5")
    monkeypatch.setenv("AIMARKET_SANDBOX_QUOTA_WINDOW", "hourly")
    monkeypatch.setattr(trials, "_MAX_PER_VISITOR", 5)  # both read once, at import
    monkeypatch.setattr(trials, "_QUOTA_WINDOW", "hourly")
    monkeypatch.setattr(trials, "_ledger", trials.SandboxTrialLedger(str(tmp_path / "trials.db")))
    monkeypatch.setattr(channels_mod, "_ledger", ChannelLedger(db_path=str(tmp_path / "ch.db")))
    monkeypatch.setattr(hub_api, "_peer_endpoint_cache", {})
    hub_signer = Signer(str(tmp_path / "hub.key"))

    # ── GAIA: pins that hub and enforces; nothing priced is served unsigned.
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gaia.key"))
    monkeypatch.setenv("ORACLE_TRUSTED_HUBS", f"{HUB}={hub_signer.public_key_b64}")
    monkeypatch.setenv("ORACLE_PRICE_GATE", "enforce")
    monkeypatch.delenv("ORACLE_PAID_TIER_SECRET", raising=False)
    monkeypatch.delenv("GAIA_ENABLE_LIVE", raising=False)
    runtime = GatewayRuntime(key_dir=str(tmp_path / "devkeys"), autotick=False)
    runtime.warm_up(3)
    gaia_app = build_app(runtime, public_url=GAIA)
    gaia_seen: list[dict[str, str]] = []

    async def to_gaia(method, url, **kw):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gaia_app),
                                     base_url=GAIA) as c:
            return await c.request(method, httpx.URL(url).path, **kw)

    async def safe_get(url, *, timeout=10.0):
        return await to_gaia("GET", url)

    async def safe_post(url, *, json=None, headers=None, timeout=30.0, invoke=False):
        gaia_seen.append(dict(headers or {}))
        return await to_gaia("POST", url, json=json, headers=headers or {})

    monkeypatch.setattr(outbound, "safe_get", safe_get)
    monkeypatch.setattr(outbound, "safe_post", safe_post)

    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub.key")
    db = HubDatabase(config.db_path)
    db.upsert_peer(Peer(url=GAIA, name="GAIA", capabilities_count=1,
                        well_known_url=f"{GAIA}/.well-known/ai-market.json",
                        categories=["oracle"], trusted=True))
    # What the crawler stores for GAIA's catalogue entry.
    db.upsert_capability(Capability(
        capability_id=READ["capability_id"], product_id="gaia.gateway", name="GAIA weather",
        price_per_call_usd=0.001, source_hub=GAIA, source_hub_name="GAIA", trust_score=0.9,
    ))
    hub_app = create_app(config=config, db=db, signer=hub_signer)

    with TestClient(hub_app) as hub, TestClient(gaia_app) as gaia:
        # The course's HTTP client, pointed at these two apps instead of the network.
        def urlopen(req, timeout=None, context=None):
            parts = urlsplit(req.full_url)
            client = hub if f"{parts.scheme}://{parts.netloc}" == HUB else gaia
            r = client.request(req.get_method(), parts.path, content=req.data,
                               headers=dict(req.header_items()))
            if r.status_code >= 400:
                raise urllib.error.HTTPError(req.full_url, r.status_code, r.reason_phrase,
                                             {}, io.BytesIO(r.content))
            return io.BytesIO(r.content)

        monkeypatch.setattr(sensors.urllib.request, "urlopen", urlopen)
        monkeypatch.setenv("COURSE_HUB_URL", HUB)
        monkeypatch.setenv("COURSE_GAIA_URL", GAIA)
        monkeypatch.setenv("COURSE_SANDBOX_VISITOR", "course-reader-e2e-01")
        yield hub, gaia, gaia_seen


def test_gaia_really_refuses_a_direct_priced_read(world):
    """The ground the two readers stand on: the old direct path is a 402 naming the hub."""
    _, gaia, _ = world
    r = gaia.post("/ai-market/v2/invoke", json=READ)
    assert r.status_code == 402, r.text
    assert r.json()["payment_ways"][0]["invoke_url"] == f"{HUB}/ai-market/v2/invoke"
    with pytest.raises(sensors.SensorsError) as refused:
        sensors.invoke(READ["capability_id"], READ["input"])
    assert refused.value.status == 402


def test_the_course_reads_weather_through_the_hub_trial(world):
    _, _, gaia_seen = world
    # The SIM pin: the LIVE one (om-wx-01) needs GAIA_ENABLE_LIVE and the network.
    payload = sensors.gaia_weather_read(sensors.SIM_WEATHER_DEVICE)
    assert sensors.reading_from_invoke(payload)["device_id"] == "ws-01"
    assert payload["sandbox"]["remaining"] == 4 and payload["sandbox"]["max_trials"] == 5
    # GAIA served it because the hub signed it, not because the gate was open.
    assert "X-AIMarket-Hub-Signature" in gaia_seen[-1]
    # The free registry is still read from GAIA directly, with no trial spent.
    devices = sensors.fleet_devices()
    assert any(d["device_id"] == "ws-01" for d in devices)


def test_the_course_says_when_the_hourly_trial_is_spent(world):
    for _ in range(5):
        sensors.gaia_weather_read(sensors.SIM_WEATHER_DEVICE)
    with pytest.raises(sensors.SensorsError, match="free trial is spent") as spent:
        sensors.gaia_weather_read(sensors.SIM_WEATHER_DEVICE)
    assert spent.value.status == 429 and "trial_quota_exhausted" in str(spent.value)


def test_a_wot_form_followed_as_written_returns_the_reading(world):
    hub, gaia, gaia_seen = world
    td = gaia.get("/wot/ws-01").json()
    (form,) = td["properties"]["temperature_c"]["forms"]
    assert form["href"] == f"{HUB}/ai-market/v2/invoke"
    body = {"product_id": form["aimarket:product_id"],
            "capability_id": form["aimarket:capability_id"],
            "input": form["aimarket:input"],
            "source_hub": form["aimarket:source_hub"]}
    r = hub.post(urlsplit(form["href"]).path, json=body,
                 headers={"X-AIMarket-Sandbox-Visitor": "wot-reader-e2e-01"})
    assert r.status_code == 200, r.text
    reading = r.json()["output"]["reading"]
    assert reading["device_id"] == "ws-01" and "temperature_c" in reading["values"]
    assert "X-AIMarket-Hub-Signature" in gaia_seen[-1]
