"""Synthetic MQTT observations, actual GAIA ingest and signed Hub responses.

No physical sensor or real money is used. The socket test additionally requires
``amqtt`` and ``paho-mqtt``; mapping and HTTP tests run without a broker.
"""
import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from gaia.app import build_app
from gaia.attestation import verify_reading
from gaia.capabilities import GatewayRuntime
from gaia.devices.feeder import FeederDevice, FeederStore
from gaia.mqtt_feeder import Forwarder, make_reading, validate_config


@pytest.fixture
def config():
    return validate_config({
        "topic": "test/station", "latitude": 51.0, "longitude": 0.0,
        "timestamp": {"path": "observation.time", "format": "unix_s"},
        "fields": {"temperature_c": {"path": "temperature", "unit": "F"},
                   "humidity_pct": {"path": "humidity", "unit": "%"}},
    })


def payload(now, **overrides):
    return json.dumps({"observation": {"time": now}, "temperature": 50,
                       "humidity": 80, **overrides}).encode()


def test_measurement_time_and_units_are_preserved(config):
    result = make_reading(payload(1000), config, now=1030)
    assert result["observed_at"] == 1000
    assert result["fields"] == {"latitude": 51, "longitude": 0,
                                "temperature_c": 10, "humidity_pct": 80}


@pytest.mark.parametrize("changes,retained,now", [
    ({}, True, 1000), ({}, False, 1601), ({}, False, 969),
    ({"observation": {}}, False, 1000),
    ({"temperature": None}, False, 1000),
    ({"temperature": True}, False, 1000),
    ({"temperature": "NaN"}, False, 1000),
    ({"temperature": "Infinity"}, False, 1000),
    ({"humidity": 101}, False, 1000),
])
def test_bad_observations_cannot_become_fresh_readings(config, changes, retained, now):
    with pytest.raises(ValueError):
        make_reading(payload(1000, **changes), config, retained=retained, now=now)


def test_timestamp_formats_and_pressure_conversion(config):
    config["timestamp"]["format"] = "iso8601"
    config["fields"] = {"pressure_hpa": {"path": "pressure", "unit": "Pa"}}
    stamp = "2026-10-13T12:00:00+00:00"
    from datetime import datetime
    now = datetime.fromisoformat(stamp).timestamp()
    reading = make_reading(payload(stamp, pressure=101325), config, now=now)
    assert reading["fields"]["pressure_hpa"] == pytest.approx(1013.25)
    with pytest.raises(ValueError):
        make_reading(payload("2026-10-13T12:00:00", pressure=101325), config, now=now)
    config["timestamp"]["format"] = "unix_ms"
    assert make_reading(payload(now * 1000, pressure=101325), config, now=now)["observed_at"] == now


@pytest.mark.parametrize("change", [{"topic": "station/#"}, {"latitude": None},
                                  {"max_age_s": 601}, {"fields": {}}])
def test_invalid_configuration(config, change):
    with pytest.raises(ValueError):
        validate_config({**config, **change})


def test_duplicate_rejection_and_failed_upload_retry(config):
    status = [503, 200]
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(status.pop(0), json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        adapter = Forwarder(config, "http://localhost:9320", "test-only", client)
        with pytest.raises(ValueError, match="503"):
            adapter.handle(config["topic"], payload(1000), now=1000)
        adapter.handle(config["topic"], payload(1000), now=1001)
        with pytest.raises(ValueError, match="duplicate"):
            adapter.handle(config["topic"], payload(1000), now=1002)
    assert len(requests) == 2
    assert requests[0].headers["Authorization"] == "Bearer test-only"


@pytest.mark.parametrize("status,body", [(200, {"ok": False}), (302, {"ok": True})])
def test_no_acknowledgement_or_redirect_is_not_success(config, status, body):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))) as client:
        adapter = Forwarder(config, "https://gaia.example", "test-only", client)
        with pytest.raises(ValueError):
            adapter.handle(config["topic"], payload(1000), now=1000)
        assert adapter.last_observed == 0


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "0")
    monkeypatch.setenv("GAIA_FEEDER_TOKEN", "test-only")
    monkeypatch.setenv("GAIA_SIGNING_KEY_PATH", str(tmp_path / "gateway.key"))
    monkeypatch.setenv("GAIA_SIM_CONTROL", "0")
    runtime = GatewayRuntime(key_dir=str(tmp_path / "devices"))
    device = FeederDevice("feeder-iot-01", runtime.clock, kind="iot",
                          store=FeederStore(), key_dir=tmp_path / "devices")
    runtime.fleet.add(device)
    app = build_app(runtime, public_url="http://127.0.0.1:9320")
    with TestClient(app) as client:
        yield app, client, device


def test_adapter_ingest_auth_and_real_signed_reading(config, gateway):
    app, client, device = gateway
    stamp = time.time()
    with pytest.raises(ValueError, match="401"):
        Forwarder(config, "http://127.0.0.1:9320", "wrong", client).handle(
            config["topic"], payload(stamp))
    Forwarder(config, "http://127.0.0.1:9320", "test-only", client).handle(
        config["topic"], payload(stamp))
    assert device._store.get(device.device_id)["observed_at"] == stamp
    output = client.post("/ai-market/v2/invoke", json={
        "product_id": "gaia.gateway", "capability_id": "gaia.iot.read@v1",
        "input": {"device_id": device.device_id},
    }).json()["output"]
    assert output["reading"]["values"]["temperature_c"] == 10
    assert verify_reading(output["reading"], output["attestation"], device.signer.public_key_b64)


@pytest.mark.asyncio
async def test_mqtt_packet_to_hub_buyer(config, gateway, tmp_path, monkeypatch):
    """Real loopback MQTT; in-process HTTP; local demo credits, never real funds."""
    broker_class = pytest.importorskip("amqtt.broker").Broker
    mqtt = pytest.importorskip("paho.mqtt.client")
    pytest.importorskip("aimarket_hub")
    import aimarket_hub.channels as channels
    import aimarket_hub.outbound_http as outbound
    from aimarket_hub.api import create_app
    from aimarket_hub.channels import ChannelLedger
    from aimarket_hub.config import HubConfig
    from aimarket_hub.database import HubDatabase
    from aimarket_hub.models import Capability
    from aimarket_hub.signing import Signer

    app, gaia_client, device = gateway
    broker = broker_class({"listeners": {"default": {"type": "tcp", "bind": "127.0.0.1:0"}},
                     "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await broker.start()
    port = broker._servers["default"].instance.sockets[0].getsockname()[1]
    subscriber = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    ready, delivered = threading.Event(), threading.Event()
    errors = []
    adapter = Forwarder(config, "http://127.0.0.1:9320", "test-only", gaia_client)

    def message(client, userdata, msg):
        try:
            adapter.handle(msg.topic, msg.payload, retained=msg.retain)
        except Exception as exc:
            errors.append(exc)
        finally:
            delivered.set()

    subscriber.on_connect = lambda c, u, f, r, p: c.subscribe(config["topic"], qos=1)
    subscriber.on_subscribe = lambda *args: ready.set()
    subscriber.on_message = message
    try:
        subscriber.connect_async("127.0.0.1", port)
        subscriber.loop_start()
        assert await asyncio.to_thread(ready.wait, 5), "MQTT subscription timed out"
        # A publish after CONNACK avoids relying on Paho's pre-connect buffering.
        connected = threading.Event()
        publisher.on_connect = lambda *args: connected.set()
        publisher.connect_async("127.0.0.1", port)
        publisher.loop_start()
        assert await asyncio.to_thread(connected.wait, 5)
        publisher.publish(config["topic"], payload(time.time()), qos=1, retain=False)
        assert await asyncio.to_thread(delivered.wait, 5), "MQTT reading did not reach GAIA"
        assert not errors

        for key, value in {"AIFACTORY_CRYPTO_ENABLED": "1", "AIMARKET_ALLOW_DEMO_CREDIT": "1",
                           "AIMARKET_SKIP_SEED": "1", "AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG": "1"}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(channels, "_ledger", ChannelLedger(db_path=str(tmp_path / "ledger.db")))

        async def post_to_gaia(url, *, json=None, headers=None, **kwargs):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gaia.test") as client:
                return await client.post(httpx.URL(url).path, json=json, headers=headers or {})

        monkeypatch.setattr(outbound, "safe_post", post_to_gaia)
        settings = HubConfig()
        settings.db_path = str(tmp_path / "hub.db")
        settings.signing_key_path = str(tmp_path / "hub.key")
        db = HubDatabase(settings.db_path)
        db.upsert_capability(Capability(
            capability_id="gaia.iot.read@v1", product_id="gaia.gateway", name="Test station",
            source_hub="local", invoke_url="http://gaia.test/ai-market/v2/invoke",
            price_per_call_usd=0.002, publisher_id="test-owner", trust_score=0.9,
            provider_pubkey=app.state.protocol.signer.public_key_b64,
        ))
        with TestClient(create_app(config=settings, db=db, signer=Signer(settings.signing_key_path))) as buyer:
            request = {"product_id": "gaia.gateway", "capability_id": "gaia.iot.read@v1",
                       "source_hub": "local", "input": {"device_id": "feeder-iot-01"}}
            assert buyer.post("/ai-market/v2/invoke", json=request).status_code == 402
            channel = buyer.post("/ai-market/v2/channel/open", json={"deposit_usd": 1.0}).json()["channel"]
            headers = {"X-Payment-Channel": channel["channel_id"], "X-Payment-Channel-Secret": channel["channel_secret"]}
            response = buyer.post("/ai-market/v2/invoke", json=request, headers=headers)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["result"]["reading"]["values"]["temperature_c"] == 10
            assert verify_reading(result["result"]["reading"], result["result"]["attestation"], device.signer.public_key_b64)
            assert result.get("receipt")
            verified = buyer.post("/ai-market/v2/receipts/verify", json={"receipt": result["receipt"]})
            assert verified.status_code == 200
            assert verified.json()["valid"] is True
            before = channels._ledger.get(channel["channel_id"])["balance_usd"]
            assert before < 1.0
            device._store.put(device.device_id, {"latitude": 51, "longitude": 0, "temperature_c": 10}, observed_at=time.time()-601)
            stale = buyer.post("/ai-market/v2/invoke", json=request, headers=headers)
            assert stale.status_code == 502
            assert "receipt" not in stale.json()
            assert channels._ledger.get(channel["channel_id"])["balance_usd"] == before
    finally:
        subscriber.disconnect()
        publisher.disconnect()
        await asyncio.to_thread(subscriber.loop_stop)
        await asyncio.to_thread(publisher.loop_stop)
        await broker.shutdown()
