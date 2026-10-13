"""One owner-controlled MQTT station -> authenticated GAIA IoT feeder.

Run with ``python -m gaia.mqtt_feeder --config station.json``.
Timestamp mappings must refer to measurement time, never adapter receipt time.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("gaia.mqtt_feeder")
UNITS = {
    "temperature_c": {"C", "F"},
    "humidity_pct": {"%"},
    "pressure_hpa": {"hPa", "Pa", "inHg"},
    "pm2_5_ugm3": {"ug/m3"},
}


def number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label}: expected a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{label}: expected a finite number") from None
    if not math.isfinite(result):
        raise ValueError(f"{label}: expected a finite number")
    return result


def at_path(payload: dict, path: str):
    value = payload
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"missing mapped field: {path}")
        value = value[key]
    return value


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("configuration must be a JSON object")
    topic = config.get("topic")
    if not isinstance(topic, str) or not topic or any(c in topic for c in ("#", "+", "\0")):
        raise ValueError("topic must identify one station, without MQTT wildcards")
    for field, bound in (("latitude", 90), ("longitude", 180)):
        value = number(config.get(field), field)
        if not -bound <= value <= bound:
            raise ValueError(f"{field}: out of range")
    age = number(config.get("max_age_s", 600), "max_age_s")
    if not 0 < age <= 600:
        raise ValueError("max_age_s must be greater than 0 and at most 600")
    stamp = config.get("timestamp")
    if not isinstance(stamp, dict) or stamp.get("format") not in {"unix_s", "unix_ms", "iso8601"}:
        raise ValueError("timestamp requires path and format: unix_s, unix_ms or iso8601")
    mappings = config.get("fields")
    if not isinstance(mappings, dict) or not mappings:
        raise ValueError("configure at least one measured field")
    for target, mapping in mappings.items():
        if target not in UNITS or not isinstance(mapping, dict) or mapping.get("unit") not in UNITS[target]:
            raise ValueError(f"unsupported field or unit: {target}")
    for mapping in [stamp, *mappings.values()]:
        path = mapping.get("path")
        if not isinstance(path, str) or not all(path.split(".")):
            raise ValueError("each mapping needs a non-empty dot-separated path")
    return config


def make_reading(payload: bytes, config: dict, *, retained: bool = False,
                 now: float | None = None) -> dict:
    """Fail closed on retained, stale, future or incomplete measurements."""
    if retained:
        raise ValueError("retained MQTT delivery rejected; wait for a new observation")
    if len(payload) > 65536:
        raise ValueError("MQTT payload exceeds 64 KiB")
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("MQTT payload is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("MQTT payload must be an object")
    stamp = config["timestamp"]
    raw = at_path(data, stamp["path"])
    if stamp["format"] == "iso8601":
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            observed = parsed.timestamp()
        except (ValueError, OverflowError):
            raise ValueError("timestamp must be ISO 8601 with an explicit timezone") from None
    else:
        observed = number(raw, "timestamp") / (1000 if stamp["format"] == "unix_ms" else 1)
    age = (time.time() if now is None else now) - observed
    if observed <= 0 or age < -30 or age > float(config.get("max_age_s", 600)):
        raise ValueError("observation is stale or timestamp is in the future")
    fields = {k: number(config[k], k) for k in ("latitude", "longitude")}
    for target, mapping in config["fields"].items():
        value = number(at_path(data, mapping["path"]), target)
        unit = mapping["unit"]
        if unit == "F":
            value = (value - 32) * 5 / 9
        elif unit == "Pa":
            value /= 100
        elif unit == "inHg":
            value *= 33.8638866667
        if ((target == "humidity_pct" and not 0 <= value <= 100)
                or (target == "temperature_c" and value < -273.15)
                or (target == "pressure_hpa" and value <= 0)
                or (target == "pm2_5_ugm3" and value < 0)):
            raise ValueError(f"{target}: out of range")
        fields[target] = value
    return {"device_id": "feeder-iot-01", "observed_at": observed, "fields": fields}


class Forwarder:
    def __init__(self, config: dict, url: str, token: str, client):
        self.config = validate_config(config)
        parsed = urlsplit(url)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.hostname or parsed.path.rstrip("/")
                or parsed.scheme not in {"http", "https"}
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("GAIA URL must be an HTTPS origin or loopback HTTP origin")
        if not token.strip():
            raise ValueError("GAIA_FEEDER_TOKEN is required")
        self.url = url.rstrip("/") + "/feeder/v1/ingest"
        self.token = token
        self.client = client
        self.last_observed = 0.0

    def handle(self, topic: str, payload: bytes, *, retained=False, now=None) -> dict:
        if topic != self.config["topic"]:
            raise ValueError("unexpected MQTT topic")
        reading = make_reading(payload, self.config, retained=retained, now=now)
        if reading["observed_at"] <= self.last_observed:
            raise ValueError("duplicate or out-of-order observation")
        response = self.client.post(self.url, json=reading,
                                    headers={"Authorization": f"Bearer {self.token}"},
                                    timeout=10, follow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f"GAIA ingest returned HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError("GAIA did not acknowledge the reading")
        self.last_observed = reading["observed_at"]
        return reading


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check-payload", type=Path, help="validate a saved observation without connecting or uploading")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = validate_config(json.loads(args.config.read_text()))
        if args.check_payload:
            print(json.dumps(make_reading(args.check_payload.read_bytes(), config), allow_nan=False))
            return 0
        from paho.mqtt import client as mqtt

        host = os.environ.get("MQTT_HOST", "localhost")
        tls = os.environ.get("MQTT_TLS", "1") == "1"
        if not tls and host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote MQTT requires TLS; use a local broker or tunnel for plaintext")
        port = int(os.environ.get("MQTT_PORT", "8883" if tls else "1883"))
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if os.environ.get("MQTT_USERNAME"):
            client.username_pw_set(os.environ["MQTT_USERNAME"], os.environ.get("MQTT_PASSWORD"))
        if tls:
            client.tls_set(ca_certs=os.environ.get("MQTT_CA_FILE") or None)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        with httpx.Client(trust_env=False) as http:
            forwarder = Forwarder(config, os.environ.get("GAIA_URL", "http://127.0.0.1:9320"),
                                  os.environ.get("GAIA_FEEDER_TOKEN", ""), http)

            def on_connect(client, userdata, flags, reason_code, properties):
                if reason_code.is_failure:
                    log.error("MQTT connection refused")
                else:
                    client.subscribe(config["topic"], qos=1)
                    log.info("Subscribed to configured station topic")

            def on_message(client, userdata, message):
                try:
                    forwarder.handle(message.topic, message.payload, retained=message.retain)
                    log.info("Forwarded fresh observation")
                except (ValueError, httpx.HTTPError):
                    # Do not print payloads, credentials or upstream response bodies.
                    log.warning("Observation not forwarded: invalid data or ingest unavailable")

            client.on_connect = on_connect
            client.on_message = on_message
            client.connect_async(host, port, keepalive=60)
            try:
                client.loop_forever(retry_first_connection=True)
            except KeyboardInterrupt:
                pass
            finally:
                client.disconnect()
        return 0
    except ImportError:
        parser.exit(2, "Install the mqtt extra: pip install '.[mqtt]'\n")
    except (ValueError, OSError):
        parser.exit(2, "Invalid configuration or unavailable local file; check the setup guide.\n")


if __name__ == "__main__":
    raise SystemExit(main())
