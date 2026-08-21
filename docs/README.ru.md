# GAIA — шлюз физического оракула

<!-- aicom-readme-badges -->
<p align="center">
  <a href="https://github.com/alexar76/gaia/actions/workflows/ci.yml"><img src="../docs/badges/ci.svg" alt="CI" /></a>
  <a href="https://iot.modelmarket.dev/"><img src="../docs/badges/demo.svg" alt="Live demo" /></a>
  <a href="https://alexar76.github.io/gaia/"><img src="../docs/badges/landing.svg" alt="Landing" /></a>
  <img src="../docs/badges/python.svg" alt="Python >=3.11" />
  <img src="../docs/badges/aimarket.svg" alt="AIMarket v2" />
  <img src="../docs/badges/signing.svg" alt="Ed25519 signing" />
  <a href="../LICENSE"><img src="../docs/badges/license.svg" alt="License: MIT" /></a>
</p>
<!-- /aicom-readme-badges -->

> 🌐 [English](../README.md) · **Русский** · [Español](README.es.md) · [Français](README.fr.md) · [中文](README.zh.md) · [Глоссарий](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md)

**GAIA** — шлюз **физического оракула**: продаёт Ed25519-**аттестованные** **показания** **датчиков**
с виртуальных IoT-устройств как платные capability AIMarket v2 и отдаёт Metis-совместимый
`/v1/verify`, чтобы хаб мог провести **расчёт** через **Pay-on-Verified** **эскроу** —
честное показание оплачивает **поставщика**, лживый датчик автоматически возвращает средства
покупателю.

Это **третий класс оракулов** экосистемы: математические оракулы доказывают вычисления, Metis
оценивает выход LLM, GAIA привязывает расчёт к физике. Демо-**флот** симулирован (две
со-расположенные метеостанции с одной site truth, узел воздуха, счётчик энергии — модели
имитируют BME280/SDS011/SCD30/Shelly-EM), но каждый wire-surface — манифест, **invoke**,
**квитанции**, подпись поставщика, verify envelope, W3C WoT Thing Descriptions — настоящий.

<p align="center">
  <strong><a href="https://iot.modelmarket.dev/">Живое демо</a></strong>
  ·
  <strong><a href="https://alexar76.github.io/gaia/">Лендинг</a></strong>
  ·
  <strong><a href="https://github.com/alexar76/gaia/pkgs/container/gaia">GHCR</a></strong>
</p>

> 📖 Глубокий разбор (монорепо): [`docs/iot-physical-oracles.md`](https://github.com/alexar76/aicom/blob/main/docs/iot-physical-oracles.md)
> 🌍 Живые устройства: [`gaia/devices/live.py`](../gaia/devices/live.py)
> 🎬 3D: [`frontend/`](../frontend/) (`cd frontend && npm i && npm run dev`)

## Быстрый старт

```bash
docker pull ghcr.io/alexar76/gaia:latest
docker run --rm -p 9320:9320 ghcr.io/alexar76/gaia:latest

pip install -e vendor/oracle-core -e ".[dev]"
python -m gaia.main                             # :9320
```

Проверка:

```bash
curl -s localhost:9320/.well-known/ai-market.json
curl -s localhost:9320/ai-market/v2/manifest

curl -s -X POST localhost:9320/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -d '{"capability_id": "gaia.weather.read@v1", "product_id": "gaia.gateway",
       "input": {"device_id": "ws-01"}}'
```

## Живые устройства — ретрансляторы публичных датчиков

`GAIA_ENABLE_LIVE=1` регистрирует реальные public-API **ретрансляторы** рядом с симуляторами.
Каждый ретранслятор идёт тем же путём Ed25519-аттестации и **правдоподобия**. Недоступный
upstream → `DeviceOffline` → 503 / `{ok:false}` → **без списания**. Хосты upstream в
**allowlist** (защита от SSRF); клиенты invoke **не** передают URL.

| Device id | Capability | Upstream | Ключ? |
|-----------|------------|----------|------|
| `nws-01` | `gaia.weather.read@v1` | NOAA/NWS `api.weather.gov` | нет |
| `om-wx-01` | `gaia.weather.read@v1` | [Open-Meteo](https://open-meteo.com) weather | нет |
| `osm-01` | `gaia.air.read@v1` | openSenseMap | нет |
| `om-aq-01` | `gaia.air.read@v1` | Open-Meteo air quality | нет |
| `sta-01` | `gaia.air.read@v1` | OGC SensorThings (Fraunhofer) | нет |
| `openaq-01` | `gaia.air.read@v1` | OpenAQ v3 | **да** (`GAIA_OPENAQ_API_KEY`) |
| `uk-grid-01` | `gaia.grid.read@v1` | UK Carbon Intensity | нет |
| `usgs-quake-01` | `gaia.quake.read@v1` | USGS GeoJSON feed | нет |
| `noaa-tide-01` | `gaia.tide.read@v1` | NOAA CO-OPS tides | нет |
| `firms-fire-01` | `gaia.fire.read@v1` | NASA FIRMS VIIRS (cite NASA) | нет |
| `safecast-01` | `gaia.radiation.read@v1` | Safecast (CC0) | нет |
| `cybernews-jam-01` | `gaia.jamming.read@v1` | CyberNews GNSS (CC BY 4.0) | нет |
| `feeder-adsb-01` | `gaia.adsb.read@v1` | Own dump1090 ingest | `GAIA_FEEDER_*` |
| `feeder-ais-01` | `gaia.ais.read@v1` | Own AIS ingest | `GAIA_FEEDER_*` |

Заметки оператора (5 языков): [`docs/LIVE-RELAYS.md`](LIVE-RELAYS.md).
Добавить датчик / пин (GAIA → ATLAS): [`add-gaia-atlas-sensor`](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md) (EN · RU · ES · FR · ZH).

```bash
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.grid.read@v1","product_id":"gaia.gateway",
       "input":{"device_id":"uk-grid-01"}}'
```

## Лицензия

MIT — см. [LICENSE](../LICENSE).
