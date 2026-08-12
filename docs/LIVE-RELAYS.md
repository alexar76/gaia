# GAIA live relays — operator guide

**Languages:** [EN](LIVE-RELAYS.md) · [RU](i18n/LIVE-RELAYS.ru.md) · [ES](i18n/LIVE-RELAYS.es.md) · [FR](i18n/LIVE-RELAYS.fr.md) · [ZH](i18n/LIVE-RELAYS.zh.md)

**Add a sensor / pin (developers):** [`docs/add-gaia-atlas-sensor.md`](../../docs/add-gaia-atlas-sensor.md) (EN · RU · ES · FR · ZH) — mesh city YAML + LIVE relay checklist, glossary-aligned.

## What a live relay is

A live device does **not** own a sensor. Its Ed25519 key attests:

> this gateway faithfully relayed what upstream API *X* returned at fetch time

Mapped fields go through the same plausibility verifier and Pay-on-Verified path as
simulators. Provenance (`source` URL + licence) is visible on `gaia.fleet.status@v1`.

## Security model

| Control | Behaviour |
|---------|-----------|
| Host allowlist | Only HTTPS hosts in `_ALLOWED_HOSTS` (`live.py`) may be fetched |
| No client URLs | Invoke `input` never contains an upstream URL — only `device_id` (plus safe filters below) |
| ID sanitization | Station / box / lat-lon / NOAA / OpenAQ ids validated before URL build |
| No credential URLs | `user:pass@host` rejected |
| No redirects | `follow_redirects=False`; non-200 → offline |
| Billing | Upstream failure → offline → hub must not debit |

## What the buyer passes on invoke

| Capability | Buyer `input` | Who chooses geography |
|------------|---------------|------------------------|
| Most `gaia.*.read@v1` | `{ "device_id": "…" }` | **Operator** anchors the device (or publishes a mesh of city devices — buyer picks `device_id`) |
| `gaia.window@v1` | `{ "device_id", "n" }` | Same as read |
| `gaia.fire.read@v1` | `{ "device_id"?, "west"?, "south"?, "east"?, "north"?, "limit"? }` | **Buyer may filter** the operator-fetched FIRMS CSV by bbox / top-N — still no client URLs (SSRF-safe) |

Fixed sensors (weather, air, tide, Safecast, …) stay operator-anchored so custody and allowlists remain honest. Event feeds like FIRMS are global; optional bbox is the client-oriented way to ask “fires near me” without inventing a new upstream host.

## Catalog (with `GAIA_ENABLE_LIVE=1`)

### Weather — `gaia.weather.read@v1`

| device_id | Upstream | Notes |
|-----------|----------|-------|
| `ws-01` / `ws-02` | simulator | Always present |
| `nws-01` | NOAA/NWS | US stations; public domain; needs User-Agent |
| `om-wx-01` | Open-Meteo | Global; default Berlin (`GAIA_OM_LAT`/`LON`); CC BY 4.0 |

### Air — `gaia.air.read@v1`

| device_id | Upstream | Notes |
|-----------|----------|-------|
| `aq-01` | simulator | Always present |
| `osm-01` | openSenseMap | Citizen science; licence per box |
| `om-aq-01` | Open-Meteo AQ | PM2.5/PM10/CO₂; no key |
| `sta-01` | OGC SensorThings | Optional; can timeout (`GAIA_STA_ENABLED`) |
| `openaq-01` | OpenAQ v3 | **Requires** `GAIA_OPENAQ_API_KEY` |

### Grid — `gaia.grid.read@v1` (live-only SKU)

| device_id | Upstream | Field |
|-----------|----------|-------|
| `uk-grid-01` | carbonintensity.org.uk | `carbon_intensity_gco2_kwh` (actual, else forecast) |

### Quake — `gaia.quake.read@v1` (live-only SKU)

| device_id | Upstream | Fields |
|-----------|----------|--------|
| `usgs-quake-01` | USGS GeoJSON M≥2.5/day | `magnitude`, `depth_km`, `latitude`, `longitude` |

### Tide — `gaia.tide.read@v1` (live-only SKU)

| device_id | Upstream | Field |
|-----------|----------|-------|
| `noaa-tide-01` | NOAA CO-OPS | `water_level_m` (MLLW, metric); default station 8518750 |

### River — `gaia.river.read@v1` (live-only SKU)

| device_id | Upstream | Fields |
|-----------|----------|--------|
| `usgs-river-01` | USGS NWIS | `discharge_m3s`, `gage_height_m` (metric); default site `01646500` Potomac |

### Marine — `gaia.marine.read@v1` (live-only SKU)

| device_id | Upstream | Fields |
|-----------|----------|--------|
| `ndbc-01` | NOAA NDBC buoy | `wave_height_m`, `sst_c`, `wind_mps` (when present); default `44025` NY Bight |
| `om-marine-01` | Open-Meteo Marine | `wave_height_m`, `sst_c`; default NYC harbor lat/lon |

### Fire — `gaia.fire.read@v1` (live-only SKU · free to commercialize)

| device_id | Upstream | Fields | License |
|-----------|----------|--------|---------|
| `firms-fire-01` | NASA FIRMS VIIRS CSV (optional `GAIA_FIRMS_MAP_KEY`) | Attested `values`: brightest `brightness_k`, `confidence`, `latitude`, `longitude`. Map payload is **packetized**: `hotspots[]` (one page, default 500) + `hotspot_total` / `next_cursor` / `fetch_id`. Resume with the same `cursor` (idempotent — safe after timeout). Collect ceiling `max_total`/`limit` up to 50000 (`GAIA_FIRMS_COLLECT_MAX`). Optional buyer `west/south/east/north`. | NASA FIRMS open data — **cite NASA FIRMS** + disclaimer |

ATLAS Wildfire layer expands `hotspots[]` into one pin per detection (`firms-hs-NNNN`). Toggle other layers off to focus on fire.

### Radiation — `gaia.radiation.read@v1` (live-only SKU · free to commercialize)

| device_id | Upstream | Fields | License |
|-----------|----------|--------|---------|
| `safecast-01` | Safecast measurements API | `cpm`, `latitude`, `longitude` | **CC0** (public domain dedication) |

### GNSS jamming — `gaia.jamming.read@v1` (live-only SKU · free to commercialize)

| device_id | Upstream | Fields | License |
|-----------|----------|--------|---------|
| `cybernews-jam-01` | cybernews.space `/api/data/gnss` | `severity_score`, `radius_km`, `latitude`, `longitude` | **CC BY 4.0** — attribution required |

### Edge traffic — `gaia.adsb.read@v1` / `gaia.ais.read@v1` (opt-in feeders)

| device_id | Upstream | Notes |
|-----------|----------|-------|
| `feeder-adsb-01` | Own dump1090 push → `POST /feeder/v1/ingest` | Requires `GAIA_FEEDER_ENABLED=1` + `GAIA_FEEDER_TOKEN`. Offline until first ingest. **Not** ADSBx / third-party NC aggregators. |
| `feeder-ais-01` | Own AIS receiver push | Same ingest path. **Not** aisstream-as-sole paid SKU. |

## License / commercialize (what is intentionally NOT included)

These Azimuth-style sources are **not** wired as GAIA Hub SKUs because they are NC or commercially restricted without a separate deal:

| Source | Why excluded |
|--------|----------------|
| Global Fishing Watch | Non-commercial terms |
| Stanford RFI / related | **CC BY-NC** |
| ADSBx commercial API | Non-commercial / paid ToS |
| GPSJam heatmaps | Gray — avoid as paid SKU |
| aisstream alone | Gray — use only as own feeder, not sole commercial dependency |

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `GAIA_ENABLE_LIVE` | `0` (compose: `1`) | Register relays |
| `GAIA_NWS_STATION` | `KNYC` | NWS station id |
| `GAIA_OSM_BOX_ID` | Berlin senseBox | openSenseMap box |
| `GAIA_OM_LAT` / `GAIA_OM_LON` | `52.52` / `13.41` | Open-Meteo coords |
| `GAIA_NOAA_TIDE_STATION` | `8518750` | CO-OPS gauge |
| `GAIA_USGS_RIVER_SITE` | `01646500` | NWIS site |
| `GAIA_NDBC_STATION` | `44025` | NDBC buoy |
| `GAIA_OM_MARINE_LAT` / `LON` | `40.70` / `-74.01` | Open-Meteo Marine |
| `GAIA_OPENAQ_API_KEY` | empty | Enables OpenAQ |
| `GAIA_OPENAQ_LOCATION_ID` | `2178` | OpenAQ location |
| `GAIA_STA_ENABLED` | `1` | Toggle SensorThings |
| `GAIA_FIRMS_ENABLED` | `1` | NASA FIRMS fire |
| `GAIA_FIRMS_MAP_KEY` | empty | Optional FIRMS map key (else keyless CSV) |
| `GAIA_FIRMS_HOTSPOT_LIMIT` | `500` | Legacy default page-ish collect hint (prefer `GAIA_FIRMS_COLLECT_MAX`) |
| `GAIA_FIRMS_COLLECT_MAX` | `250000` | Max ranked hotspots kept per fetch for packetized delivery (1–250000) |
| `GAIA_HOTSPOT_CURSOR_SECRET` | random per process | HMAC secret for opaque `next_cursor` (set in prod for multi-worker) |
| `GAIA_SAFECAST_ENABLED` | `1` | Safecast CC0 |
| `GAIA_SAFECAST_LAT` / `LON` | `37.42` / `141.03` | Safecast query anchor |
| `GAIA_SAFECAST_MAX_AGE_DAYS` | `30` | Recency window (`captured_after`) — the SKU sells recent CPM, not 15-year-old rows (1–365) |
| `GAIA_CYBERNEWS_ENABLED` | `1` | CyberNews GNSS CC BY |
| `GAIA_FEEDER_ENABLED` | `0` | Own edge ADS-B/AIS devices |
| `GAIA_FEEDER_TOKEN` | empty | Bearer token for `/feeder/v1/ingest` |
| `GAIA_*_ENABLED` | `1` | Per-relay toggles (`OM_WEATHER`, `OM_AQ`, `OM_MARINE`, `UK_CARBON`, `USGS_QUAKE`, `USGS_RIVER`, `NOAA_TIDE`, `NDBC`) |

## Honesty

- Factory storefront ≠ hub catalog. GAIA sells **physical / relay** readings on the hub.
- Live keys prove **relay custody**, not sensor ownership.
- OpenAQ is optional until an operator supplies a free API key.
- Attribution: Open-Meteo requires CC BY 4.0 credit; NWS/USGS/NOAA are US Government public domain; UK Carbon Intensity is National Grid ESO open data; FIRMS requires NASA citation; Safecast is CC0; CyberNews GNSS requires CC BY 4.0 attribution.
- Edge feeders attest **your** receiver push — never launder third-party NC aggregators as LIVE provenance.
- Add more instances of existing kinds with one command: [`docs/add-gaia-atlas-sensor.md`](../../docs/add-gaia-atlas-sensor.md).

## Map / watchboxes / composite briefs → ATLAS

Open-data devices above are **GAIA fleet SKUs**. Map layers, pins,
**watchboxes**, and composite Hub SKUs
(`atlas.situation.brief@v1`, `atlas.fire.weather@v1`, `atlas.nearest.read@v1`,
`atlas.watchbox.check@v1`)
live on **ATLAS** — see [`atlas/docs/GUIDE.md`](../../atlas/docs/GUIDE.md)
(EN · RU · ES · FR · ZH). Do not treat those as a separate GAIA product surface.

A **watchbox** is a saved geographic bbox + layer filter: subscribe via ATLAS REST,
then poll **check** (billable Hub SKU) for LIVE matches inside the box + a content
receipt. Hub catalogue blurbs: `aimarket-hub/cap-descriptions-i18n.json`.

## Buy via hub

### Path A — fast (MCP / sandbox visitor)

Same call the **`market_invoke`** tool in [`aimarket-mcp`](https://github.com/alexar76/aimarket-mcp) makes.
No wallet, no escrow — hub free-trial / sandbox visitor tier.

```bash
# Open-Meteo current (device om-wx-01 → Berlin lat/lon by default)
curl -s -X POST https://modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -H 'X-AIMarket-Sandbox-Visitor: vis_docs_om_wx' \
  -d '{"capability_id":"gaia.weather.read@v1","product_id":"gaia.gateway",
       "source_hub":"https://iot.modelmarket.dev","input":{"device_id":"om-wx-01"}}'

# USGS quake (latest M≥2.5 in feed — not a buyer-chosen region)
curl -s -X POST https://modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -H 'X-AIMarket-Sandbox-Visitor: vis_docs_example' \
  -d '{"capability_id":"gaia.quake.read@v1","product_id":"gaia.gateway",
       "source_hub":"https://iot.modelmarket.dev","input":{"device_id":"usgs-quake-01"}}'
```

`om-wx-01` uses the **Open-Meteo Forecast API** but maps only **`current`** fields
(temperature / humidity / pressure / wind). Buyer cannot pass lat/lon in `input`
(operator sets `GAIA_OM_LAT` / `GAIA_OM_LON`).

### Path B — external depositor (real USDC escrow)

Second payer `0x6E94…6C9c` on Base mainnet: `approve` → `openChannel($1)` → hub
paid channel → invoke → `refundChannel` (hub escrow bridge had no broadcast key,
so on-chain debit/settle was not completed).

| Run | Capabilities | Ledger used | Journal |
|---|---|---:|---|
| 2026-08-05 | weather + quake | $0.02 | [`onchain-journal` §3l](../../docs/onchain-journal.md) |
| 2026-08-05 | Open-Meteo weather only | $0.01 | [`onchain-journal` §3m](../../docs/onchain-journal.md) |
