# Connect one MQTT station to GAIA

The adapter subscribes to one owner-controlled JSON topic and uploads observations to `/feeder/v1/ingest`. It uses the existing `feeder-iot-01` device and `gaia.iot.read@v1` capability. It does not configure a hub, pay for calls or announce federation membership.

## Install and configure

From the AICOM repository root, using Python 3.11 or later:

```bash
python3 -m venv .venv-mqtt
. .venv-mqtt/bin/activate
python -m pip install -e ./oracles/core -e './gaia[mqtt]'
cp gaia/config/mqtt-station.example.json local-station.json
```

Edit `local-station.json`: set your actual latitude/longitude, exact MQTT topic and payload paths. The example deliberately contains null coordinates and refuses to run until configured. Do not commit local configuration or credentials. The JSON contains no passwords.

Map `timestamp.path` to **the observation time provided by your station or its trusted acquisition software**. Supported formats are `unix_s`, `unix_ms` and `iso8601` with an explicit timezone. The adapter never invents an observation time. A Home Assistant state refresh, MQTT receipt time, or re-published retained value is not necessarily a measurement time. If the source does not supply trustworthy observation timing, fix the source export first.

The supplied mapping expects this shape, where each placeholder must be replaced with an actual JSON number from the device:

```json
{
  "observed_at": "<measurement Unix seconds>",
  "temperature": "<measured degrees Celsius>",
  "humidity": "<measured relative humidity percent>"
}
```

Dot-separated object paths such as `observation.time` are supported; arrays and literal dots in keys are not. Every configured measurement must be present and numeric in each message. Remove mappings for fields your station does not measure. Partial per-field MQTT topics need an acquisition-side join that preserves actual measurement times; this adapter expects one coherent observation.

| Output field | Accepted source units |
| --- | --- |
| `temperature_c` | `C`, `F` |
| `humidity_pct` | `%` |
| `pressure_hpa` | `hPa`, `Pa`, `inHg` |
| `pm2_5_ugm3` | `ug/m3` |

Pressure conversion changes units only. It does not convert sea-level pressure into station pressure. Document which pressure you publish. Wind, rain, UV, CO2 and historical queries are outside this adapter's scope.

## Check a real payload without uploading

Save one fresh observation exported from your own station as `reading.json`, then run:

```bash
python -m gaia.mqtt_feeder --config local-station.json --check-payload reading.json
```

This only prints normalized JSON. It does not connect to MQTT, upload to GAIA or prove physical sensor accuracy. Old saved samples are rejected instead of being restamped as fresh.

## Start the adapter

First start GAIA with `GAIA_ENABLE_LIVE=1`, `GAIA_FEEDER_ENABLED=1` and a private `GAIA_FEEDER_TOKEN`, following the [sensor operator guide](https://github.com/alexar76/aicom/blob/main/docs/sensor-operator.md). In the monorepo the guide is also available locally at `docs/sensor-operator.md`; verify public availability before sharing the GitHub link.

Load the same feeder token and MQTT credentials from your private service environment. Do not paste them into commands saved in shell history. Required and optional settings:

| Variable | Meaning |
| --- | --- |
| `GAIA_FEEDER_TOKEN` | Required; must match the private GAIA provider |
| `GAIA_URL` | Provider origin; default `http://127.0.0.1:9320` |
| `MQTT_HOST` | Broker hostname; default `localhost` |
| `MQTT_TLS` | Default `1`; `0` allowed only for a loopback broker or tunnel |
| `MQTT_PORT` | Default `8883` with TLS, `1883` without |
| `MQTT_CA_FILE` | Optional CA bundle; otherwise system trust roots |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | Broker credentials when required |

```bash
python -m gaia.mqtt_feeder --config local-station.json
```

Use broker permissions that allow the station to publish and the adapter to subscribe only to its station topic. Configure remote brokers with valid TLS certificates. GAIA uploads require HTTPS except on loopback, and redirects are not followed. No global proxy settings are inherited for the authenticated upload.

The adapter reconnects and re-subscribes after MQTT disconnects. It rejects retained deliveries, oversized payloads, missing fields, nonfinite values, impossible humidity, observations over `max_age_s` old (300 seconds in the example, maximum 600), timestamps more than 30 seconds ahead and duplicate/out-of-order observations within the current adapter process. Restarting loses the local duplicate high-water mark; freshness checks still apply.

There is no persistent queue or automatic HTTP retry. An upload failure is logged; the next fresh observation is the recovery path. MQTT QoS 1 is not an end-to-end delivery guarantee. Use a frequent source update interval and service supervision. This small single-station adapter performs a bounded HTTP upload in its MQTT callback; high-rate or multi-station collection needs a separate queued design.

GAIA currently retains only the latest reading in process memory. After GAIA restarts, the next fresh message is required. Its sold reading's `ts` is the gateway reading time, **not** the original observation time. The feeder enforces observation freshness internally, but this adapter does not add a buyer-visible signed observation-time field. Applications requiring that field or an archive need further work. The signature comes from the operator's GAIA key, not a hardware secure element, and does not independently establish measurement accuracy.

## Check through your hub

Follow the operator guide to list `gaia.iot.read@v1` with the pinned GAIA public key, your price and receiving address. Check an unpaid request returns `402`. Test a real payment only with the payer's explicit authorization and agreed budget. Check the delivered values, signature, receipt and receipt of funds; simulator balances are not revenue.

Stop the source and wait beyond the feeder's 600-second limit. Confirm the hub fails the request instead of returning an old reading as a paid success. Preserve the observation time when restarting the source. No automatic announcement or real purchase is performed by the adapter.

## Reproduce the software-only verification

From the monorepo root, in a disposable environment with GAIA dev dependencies and Hub dependencies installed:

```bash
python -m pip install -e './oracles/core' -e './gaia[dev,mqtt]' -e './aimarket-hub[pqc]' 'amqtt>=0.11,<0.12'
python -m pytest gaia/tests/test_mqtt_feeder.py -q
```

The socket test uses a real local MQTT broker and synthetic observations. GAIA and Hub HTTP calls use in-process ASGI transports. It checks authenticated ingest, signatures, an unpaid `402`, a purchase using local demo credits and a stale reading rejected without another debit. It does not test physical hardware, public HTTPS deployment, federation admission or on-chain settlement. The broker test skips if the optional broker or sibling Hub is unavailable; mapping and ingest tests still run.

Client API reference: [Eclipse Paho Python documentation](https://eclipse.dev/paho/clients/python/docs/).
