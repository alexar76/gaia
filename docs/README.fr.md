# GAIA — passerelle d’oracle physique

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

<p align="center">
  <a href="https://iot.modelmarket.dev/">
    <img src="assets/hero.svg" alt="GAIA — une mesure quitte un site physique, est signée à la source avec Ed25519, vendue comme capacité AIMarket v2 et réglée par /v1/verify : plausible paie le fournisseur, implausible rembourse l'acheteur" width="100%" />
  </a>
</p>

> 🌐 [English](../README.md) · [Русский](README.ru.md) · [Español](README.es.md) · **Français** · [中文](README.zh.md) · [Glossaire](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md)

**GAIA** est une passerelle d’**oracle physique** qui vend des **lectures** de **capteurs**
avec **attestation** Ed25519 depuis des appareils IoT virtuels comme capabilities payantes
AIMarket v2, et sert un `/v1/verify` compatible Metis pour que le hub règle le
**séquestre / dépôt fiduciaire (escrow)** **Pay-on-Verified** —
une lecture honnête paie le **fournisseur** ; un capteur menteur rembourse l’acheteur
automatiquement.

C’est la **troisième classe d’oracles** de l’écosystème : les oracles mathématiques prouvent
des calculs, Metis juge la sortie LLM, GAIA ancre le **règlement** dans la physique. La
**flotte** démo est simulée (deux stations météo co-localisées avec une site truth, un nœud
air, un compteur d’énergie — modèles type BME280/SDS011/SCD30/Shelly-EM), mais chaque
surface filaire — manifeste, **invocation**, **reçus**, signature fournisseur, verify
envelope, W3C WoT Thing Descriptions — est la vraie.

<p align="center">
  <strong><a href="https://iot.modelmarket.dev/">Démo live</a></strong>
  ·
  <strong><a href="https://alexar76.github.io/gaia/">Landing</a></strong>
  ·
  <strong><a href="https://github.com/alexar76/gaia/pkgs/container/gaia">GHCR</a></strong>
</p>

> 📖 Approfondissement (monorepo) : [`docs/iot-physical-oracles.md`](https://github.com/alexar76/aicom/blob/main/docs/iot-physical-oracles.md)
> 🌍 Appareils live : [`gaia/devices/live.py`](../gaia/devices/live.py)
> 🎬 3D : [`frontend/`](../frontend/) (`cd frontend && npm i && npm run dev`)

## Démarrage rapide

```bash
docker pull ghcr.io/alexar76/gaia:latest
docker run --rm -p 9320:9320 ghcr.io/alexar76/gaia:latest

pip install -e vendor/oracle-core -e ".[dev]"
python -m gaia.main                             # :9320
```

Tester :

```bash
curl -s localhost:9320/.well-known/ai-market.json
curl -s localhost:9320/ai-market/v2/manifest

curl -s -X POST localhost:9320/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -d '{"capability_id": "gaia.weather.read@v1", "product_id": "gaia.gateway",
       "input": {"device_id": "ws-01"}}'
```

## Appareils live — relais de capteurs publics

`GAIA_ENABLE_LIVE=1` enregistre de vrais **relais** d’API publiques à côté des simulateurs.
Chaque relais suit le même chemin d’attestation Ed25519 et de **plausibilité**. Upstream
injoignable → `DeviceOffline` → 503 / `{ok:false}` → **pas de débit**. Les hôtes upstream
sont en **allowlist** (défense SSRF) ; les clients d’invoke **ne** fournissent **jamais**
d’URL.

| Device id | Capability | Upstream | Clé ? |
|-----------|------------|----------|------|
| `nws-01` | `gaia.weather.read@v1` | NOAA/NWS `api.weather.gov` | non |
| `om-wx-01` | `gaia.weather.read@v1` | [Open-Meteo](https://open-meteo.com) weather | non |
| `osm-01` | `gaia.air.read@v1` | openSenseMap | non |
| `om-aq-01` | `gaia.air.read@v1` | Open-Meteo air quality | non |
| `sta-01` | `gaia.air.read@v1` | OGC SensorThings (Fraunhofer) | non |
| `openaq-01` | `gaia.air.read@v1` | OpenAQ v3 | **oui** (`GAIA_OPENAQ_API_KEY`) |
| `uk-grid-01` | `gaia.grid.read@v1` | UK Carbon Intensity | non |
| `usgs-quake-01` | `gaia.quake.read@v1` | USGS GeoJSON feed | non |
| `noaa-tide-01` | `gaia.tide.read@v1` | NOAA CO-OPS tides | non |
| `firms-fire-01` | `gaia.fire.read@v1` | NASA FIRMS VIIRS (cite NASA) | non |
| `safecast-01` | `gaia.radiation.read@v1` | Safecast (CC0) | non |
| `cybernews-jam-01` | `gaia.jamming.read@v1` | CyberNews GNSS (CC BY 4.0) | non |
| `feeder-adsb-01` | `gaia.adsb.read@v1` | Own dump1090 ingest | `GAIA_FEEDER_*` |
| `feeder-ais-01` | `gaia.ais.read@v1` | Own AIS ingest | `GAIA_FEEDER_*` |

Notes opérateur (5 langues) : [`docs/LIVE-RELAYS.md`](LIVE-RELAYS.md).
Ajouter un capteur / pin (GAIA → ATLAS) : [`add-gaia-atlas-sensor`](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md) (EN · RU · ES · FR · ZH).

```bash
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.grid.read@v1","product_id":"gaia.gateway",
       "input":{"device_id":"uk-grid-01"}}'
```

## Licence

MIT — voir [LICENSE](../LICENSE).
