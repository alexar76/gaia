<!-- aicom-mirror-notice -->
> **🔄 Synced from a monorepo — but with a live history.** `gaia` mirrors the
> canonical AI-Factory monorepo. History here is append-only (no force-push).
> **Pull requests are welcome** — merged PRs are imported back into the monorepo
> and re-synced here, so your contribution becomes canonical.
> 💬 **[Issues](https://github.com/alexar76/gaia/issues)** · **[Pull requests](https://github.com/alexar76/gaia/pulls)** both welcome.

# GAIA — physical-world oracle gateway

<!-- sensor-operator-guide -->
> Own a weather station or air-quality sensor? [Sell your readings through your own Hub + GAIA](https://github.com/alexar76/aicom/blob/main/docs/sensor-operator.md).
<!-- /sensor-operator-guide -->

For an existing MQTT station, see the [single-station MQTT adapter](docs/mqtt-feeder.md).

<!-- aicom-readme-badges -->
<p align="center">
  <a href="https://github.com/alexar76/gaia/actions/workflows/ci.yml"><img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/ci.svg" alt="CI" /></a>
  <a href="https://github.com/alexar76/gaia/actions/workflows/pages.yml"><img src="https://github.com/alexar76/gaia/actions/workflows/pages.yml/badge.svg" alt="Pages deploy" /></a>
  <a href="https://iot.modelmarket.dev/"><img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/demo.svg" alt="Live demo status" /></a>
  <a href="https://alexar76.github.io/gaia/"><img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/landing.svg" alt="Landing" /></a>
  <a href="https://github.com/alexar76/gaia/pkgs/container/gaia"><img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/ghcr.svg" alt="GHCR package" /></a>
  <img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/python.svg" alt="Python >=3.11" />
  <img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/tests.svg" alt="tests" />
  <img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/aimarket.svg" alt="AIMarket v2" />
  <img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/signing.svg" alt="Ed25519 signing" />
  <a href="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/coverage.svg"><img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/coverage.svg" alt="Test coverage" /></a>
  <a href="https://github.com/alexar76/gaia/blob/main/LICENSE"><img src="https://raw.githubusercontent.com/alexar76/gaia/refs/heads/main/docs/badges/license.svg" alt="License: MIT" /></a>
</p>
<!-- /aicom-readme-badges -->

<p align="center">
  <a href="https://iot.modelmarket.dev/">
    <img src="docs/assets/hero.svg" alt="GAIA — a reading leaves a physical site, is signed at the source with Ed25519, sold as an AIMarket v2 capability, and settled by /v1/verify: plausible pays the provider, implausible refunds the buyer" width="100%" />
  </a>
</p>

> 🌐 **English** · [Русский](docs/README.ru.md) · [Español](docs/README.es.md) · [Français](docs/README.fr.md) · [中文](docs/README.zh.md) · [Glossary](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md)


**GAIA** is a physical-world oracle gateway that sells Ed25519-attested sensor readings from
virtual IoT devices as paid AIMarket v2 capabilities, and serves a Metis-envelope-compatible
`/v1/verify` so the hub's Pay-on-Verified escrow can settle them —
an honest reading gets the provider paid, a lying sensor refunds the buyer automatically.

It is the ecosystem's **third oracle class**: math oracles prove computations, Metis judges
LLM output, GAIA grounds settlement in physics. The demo fleet is simulated (two co-located
weather stations sharing one site truth, an air-quality node, an energy meter — models imitate
BME280/SDS011/SCD30/Shelly-EM-class hardware), but every wire surface — manifest, invoke,
receipts, provider signature, verify envelope, W3C WoT Thing Descriptions — is the real one.

<p align="center">
  <strong><a href="https://iot.modelmarket.dev/">Live demo</a></strong>
  ·
  <strong><a href="https://alexar76.github.io/gaia/">Landing</a></strong>
  ·
  <strong><a href="https://github.com/alexar76/gaia/pkgs/container/gaia">GHCR</a></strong>
</p>

> 📖 Deep-dive (monorepo): [`docs/iot-physical-oracles.md`](https://github.com/alexar76/aicom/blob/main/docs/iot-physical-oracles.md)
> 🌍 Live devices: [`gaia/devices/live.py`](gaia/devices/live.py)
> 🎬 3D visualization: [`frontend/`](frontend/) (`cd frontend && npm i && npm run dev`)

## Quickstart

```bash
# Container
docker pull ghcr.io/alexar76/gaia:latest
docker run --rm -p 9320:9320 ghcr.io/alexar76/gaia:latest

# From source (satellite checkout vendors oracle-core)
pip install -e vendor/oracle-core -e ".[dev]"   # or: pip install -e ../oracles/core -e ".[dev]"
python -m gaia.main                             # :9320
```

Poke it:

```bash
curl -s localhost:9320/.well-known/ai-market.json
curl -s localhost:9320/ai-market/v2/manifest

curl -s -X POST localhost:9320/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -d '{"capability_id": "gaia.weather.read@v1", "product_id": "gaia.gateway",
       "input": {"device_id": "ws-01"}}'
```

## Live devices — relay real public sensors

Set `GAIA_ENABLE_LIVE=1` to register real public-API relays alongside the simulators.
Every relay uses the same Ed25519 attestation + plausibility path. Unreachable upstream
→ `DeviceOffline` → 503 / `{ok:false}` → **no debit**. Upstream hosts are **allowlisted**
(SSRF defence); invoke clients never supply a URL.

| Device id | Capability | Upstream | Key? |
|-----------|------------|----------|------|
| `nws-01` | `gaia.weather.read@v1` | NOAA/NWS `api.weather.gov` | no |
| `om-wx-01` | `gaia.weather.read@v1` | [Open-Meteo](https://open-meteo.com) weather | no |
| `osm-01` | `gaia.air.read@v1` | openSenseMap | no |
| `om-aq-01` | `gaia.air.read@v1` | Open-Meteo air quality | no |
| `sta-01` | `gaia.air.read@v1` | OGC SensorThings (Fraunhofer) | no |
| `openaq-01` | `gaia.air.read@v1` | OpenAQ v3 | **yes — free** key from [explore.openaq.org](https://explore.openaq.org) → `GAIA_OPENAQ_API_KEY` |
| `uk-grid-01` | `gaia.grid.read@v1` | UK Carbon Intensity | no |
| `usgs-quake-01` | `gaia.quake.read@v1` | USGS GeoJSON feed | no |
| `noaa-tide-01` | `gaia.tide.read@v1` | NOAA CO-OPS tides | no |
| `firms-fire-01` | `gaia.fire.read@v1` | NASA FIRMS VIIRS (cite NASA) | no |
| `safecast-01` | `gaia.radiation.read@v1` | Safecast (CC0) | no |
| `cybernews-jam-01` | `gaia.jamming.read@v1` | CyberNews GNSS (CC BY 4.0) | no |
| `feeder-adsb-01` | `gaia.adsb.read@v1` | Own dump1090 ingest | `GAIA_FEEDER_*` |
| `feeder-ais-01` | `gaia.ais.read@v1` | Own AIS ingest | `GAIA_FEEDER_*` |

Full operator notes (5 languages): [`docs/LIVE-RELAYS.md`](docs/LIVE-RELAYS.md).
Add a sensor / pin (GAIA → ATLAS): [`docs/add-gaia-atlas-sensor.md`](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md) (EN · RU · ES · FR · ZH).

```bash
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.grid.read@v1","product_id":"gaia.gateway",
       "input":{"device_id":"uk-grid-01"}}'
```

## Beyond reading — places, power prices, imagery, device control

| Capability | Price | What it does | Guide |
|---|---|---|---|
| `gaia.place.resolve@v1` | free | Any populated place by name ("Kraków", "Paris, Texas", "東京") → one place, or the candidates when the name is ambiguous; says which GAIA reads cover it (GeoNames, CC BY 4.0) | [Places](docs/PLACES.md) |
| `gaia.power.price.read@v1` | $0.001 | Day-ahead electricity price of a European bidding zone, now and over the next 24 h (SMARD, CC BY 4.0; 15 zones) | [Power prices](docs/POWER-PRICES.md) |
| `gaia.power.window@v1` | $0.002 | When is power cheapest for N hours — with the prices used and their SHA-256 (an 11 kW EV charge was 38 % cheaper at 12:30 than at 08:40 on 2026-10-16) | [Power prices](docs/POWER-PRICES.md) |
| `gaia.imagery.read@v1` | $0.005 | Latest cloud-free Sentinel-2 view of any land point: NDVI, NDWI, NBR, NDMI | [Imagery](docs/IMAGERY.md) |
| `gaia.actuator.list@v1` | free | The operator's switchable devices and their limits | [Device control](docs/ACTUATION.md) |
| `gaia.actuate@v1` | $0.05 | Switch the public demo plug on for N seconds or off — **paid only for the effect its meter measures** (Pay-on-Verified). A real plug answers only its owner | [Device control](docs/ACTUATION.md) |
| `gaia.owner.command@v1` | free | The owner (or a delegate) switches their own plug, signed with their wallet (EIP-712) — no hub, no payment | [Device control](docs/ACTUATION.md#who-may-switch-a-plug) |
| `gaia.owner.consumption@v1` | free | The owner reads their plug's hourly or daily consumption, signed by the plug's key | [Device control](docs/ACTUATION.md#consumption-statistics) |
| `gaia.consumption.read@v1` | $0.01 | A plug's consumption statistics, **only where its owner put them on sale**, in whole finished hours or days | [Device control](docs/ACTUATION.md#consumption-statistics) |

## License

MIT — see [LICENSE](LICENSE).
