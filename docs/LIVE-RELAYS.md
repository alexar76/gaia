# GAIA live relays — operator guide

**Languages:** [EN](LIVE-RELAYS.md) · [RU](i18n/LIVE-RELAYS.ru.md) · [ES](i18n/LIVE-RELAYS.es.md) · [FR](i18n/LIVE-RELAYS.fr.md) · [ZH](i18n/LIVE-RELAYS.zh.md)

**Add a sensor / pin (developers):** [`docs/add-gaia-atlas-sensor.md`](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md) (EN · RU · ES · FR · ZH) — mesh city YAML + LIVE relay checklist, glossary-aligned.

**Operator use cases** (ATLAS map + ATLAS Analyst — live vs proposed vs hold): [`atlas/docs/OPERATOR-USE-CASES.md`](https://github.com/alexar76/atlas/blob/main/docs/OPERATOR-USE-CASES.md) (EN · RU · ES · FR · ZH).

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
| `gaia.gnss.integrity.read@v1` | `{ "device_id"?, "station_id"? }` | Omit `station_id` for a network inventory; set it for the exact station exposed by ATLAS. Virtual ids `gnss-station:euref:*` / `gnss-station:ga:*` are accepted. |

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
| `geonet-01` | GeoNet NZ (CC BY 3.0 NZ) | same fields; local catalogue |
| `emsc-01` | EMSC FDSN (CC BY 4.0 — cite EMSC) | Euro-Med density; preliminary; **not** a USGS replacement |

### Tide — `gaia.tide.read@v1` (live-only SKU)

| device_id | Upstream | Field |
|-----------|----------|-------|
| `noaa-tide-01` | NOAA CO-OPS | `water_level_m` (MLLW, metric); default station 8518750 |

### River — `gaia.river.read@v1` (live-only SKU)

| device_id | Upstream | Fields |
|-----------|----------|--------|
| `usgs-river-01` | USGS NWIS | `discharge_m3s`, `gage_height_m` (metric); default site `01646500` Potomac |
| `eccc-hydro-01` | ECCC hydrometric | Same fields when published; default `02HC003` (End-use Licence + attribution) |
| `smhi-hydro-01` | SMHI hydroobs | `discharge_m3s` (15 min); default station `2357` (CC BY 4.0) |

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

Hub `safecast-01` keeps a 30-day recency window (recent CPM). Extra map anchors (`safecast-melbourne`, `safecast-adelaide`) use archive mode (`max_age_days: 0`, up to 40 pages × 150 rows, cluster cap 5000) so southern-AU 2014 bGeigie drive-grids stay on the map. Pins carry `captured_at` — historical surveys are not “now”.

### GNSS jamming — `gaia.jamming.read@v1` (live-only SKU · free to commercialize)

| device_id | Upstream | Fields | License |
|-----------|----------|--------|---------|
| `cybernews-jam-01` | cybernews.space `/api/data/gnss` | `severity_score`, `radius_km`, `latitude`, `longitude` | **CC BY 4.0** — attribution required |

### GNSS station integrity — `gaia.gnss.integrity.read@v1` (live-only SKU · free to commercialize)

| device_id | Upstream | Claim | License |
|-----------|----------|-------|---------|
| `gnss-euref-01` | EUREF EPN official station list | Station inventory; source-published availability/latency become `derived_degradation` with `cause=unestablished` | **CC BY 4.0** — attribution required |
| `gnss-ga-01` | Geoscience Australia public site-log API | Station inventory only; integrity is `unknown` until a measurement product is attached | **CC BY 3.0 Australia** — attribution required |

These are not synonyms for jamming. A data-file delay may come from a receiver,
uplink, data centre, or maintenance. ATLAS renders the station field and may fuse
it with separately cited interference reports, but every reading and receipt keeps
`claim_class`, `cause`, `source_url`, licence and the evidence boundary.

### Edge traffic — `gaia.adsb.read@v1` / `gaia.ais.read@v1` / `gaia.iot.read@v1` (opt-in feeders)

| device_id | Upstream | Notes |
|-----------|----------|-------|
| `feeder-adsb-01` | Own dump1090 push → `POST /feeder/v1/ingest` | Requires `GAIA_FEEDER_ENABLED=1` + `GAIA_FEEDER_TOKEN`. Offline until first ingest. **Not** ADSBx / third-party NC aggregators. |
| `feeder-ais-01` | Own AIS receiver push | Same ingest path. **Not** aisstream-as-sole paid SKU. |
| `feeder-iot-01` | Own IoT / Tasmota / TTN / SenML push (`temperature_c`, `humidity_pct`, `pressure_hpa`, `pm2_5_ugm3`) | Same ingest path. |

## P0 — ATLAS event layers + GAIA in-situ (commercially clear)

### Natural events — `gaia.events.read@v1`

| device_id | Upstream | License |
|-----------|----------|---------|
| `eonet-01` | NASA EONET v3 open events | NASA open data — **cite NASA EONET**; no NASA endorsement |

### Space weather — `gaia.spacewx.read@v1`

| device_id | Upstream | License |
|-----------|----------|---------|
| `swpc-01` | NOAA SWPC planetary Kp + OVATION aurora | U.S. public domain |

### Lightning — `gaia.lightning.read@v1`

| device_id | Upstream | License |
|-----------|----------|---------|
| `glm-01` | GOES-19/18 GLM-L2-LCFA via NOAA NODD S3 | U.S. public domain. Requires `h5py`. **Not** Blitzortung (NC). GOES-16 East stopped GLM LCFA in 2025. |

### Weather alerts — `gaia.alerts.read@v1`

| device_id | Upstream | License |
|-----------|----------|---------|
| `nws-alerts-01` | NWS CAP GeoJSON `api.weather.gov/alerts/active` | Free for any purpose (U.S. PD) |

### Additional in-situ weather / air / ocean / geomag

| device_id | SKU | Upstream | License |
|-----------|-----|----------|---------|
| `sc-01` | `gaia.air.read@v1` | Sensor.Community area query (default Berlin SDS011) | **ODbL + DbCL** — cite Sensor.Community; share-alike on a derived DB dump, not this live query |
| `cwop-01` | `gaia.weather.read@v1` | MADIS **CWOP only** via IEM | NOAA: CWOP has **no redistribution restrictions**. Other MADIS mesonets are **not** included. |
| `argo-01` | `gaia.argo.read@v1` | Official GDAC active-float directory; pass `wmo` for that float's latest QC-gated profile | Unrestricted — **cite DOI 10.17882/42182** |
| `metno-01` | `gaia.weather.read@v1` | MET Norway METAR (in-situ airport instruments) | **CC BY 4.0 + NLOD** — attribution: MET Norway. Not locationforecast (model). |
| `usgs-geomag-01`, `usgs-geomag-*` | `gaia.geomag.read@v1` | All 14 official USGS observatories; total field F (default BOU) | U.S. PD. Each observatory is a separate selectable device and ATLAS pin. **Not INTERMAGNET** (CC BY-NC) and not Kyoto Dst. |

## P1 — verified licences only

Copernicus **GloFAS operational WMS requires registration**. We do **not** scrape it. Flood layer is NWS CAP flood/flash-flood alerts (U.S. PD) — USGS WaterWatch `/webservices/realtime` JSON was retired (HTTP 301, 2026-08).

| device_id | SKU | Upstream | License |
|-----------|-----|----------|---------|
| `nws-flood-01` | `gaia.flood.read@v1` | NWS CAP flood / flash-flood alerts | U.S. PD |
| `effis-01` | `gaia.effis.read@v1` | Copernicus EFFIS current fires | **CC BY 4.0** — cite Copernicus EMS / JRC |
| `usgs-volcano-01` | `gaia.volcano.read@v1` | USGS elevated volcanoes | U.S. PD (PAGER/ShakeMap already ride on `usgs-quake-01` GeoJSON) |
| `dwd-01` | `gaia.weather.read@v1` | DWD SYNOP via Bright Sky | **CC BY 4.0** — attribution: Deutscher Wetterdienst |
| `eccc-01` | `gaia.weather.read@v1` | ECCC MSC GeoMet climate-hourly | End-use Licence — commercial + attribution |
| `aurn-01` | `gaia.air.read@v1` | Defra AURN via London Air JSON | **OGL** — cite Defra UK-AIR |
| `geonet-01` | `gaia.quake.read@v1` | GeoNet NZ earthquakes | **CC BY 3.0 NZ** — cite GeoNet / GNS Science |
| `uhslc-01` | `gaia.tide.read@v1` | UHSLC fast-delivery gauge | May be used and redistributed for free — cite UHSLC |
| `eia-01` | `gaia.grid.read@v1` | EIA US48 hourly demand | Free key (`GAIA_EIA_API_KEY`); cite EIA; no endorsement |
| `knmi-01` | `gaia.weather.read@v1` | KNMI 10-min observations | **CC BY 4.0** — requires `GAIA_KNMI_API_KEY` |

## P2 — licence-pinned public relays

Own-edge `gaia.ais.read@v1` stays the operator receiver. Public AIS is a **different** SKU.

| device_id | SKU | Upstream | License |
|-----------|-----|----------|---------|
| `fintraffic-ais-01` | `gaia.ais.public.read@v1` | Fintraffic Digitraffic AIS REST snapshot (`meri.digitraffic.fi`) | **CC BY 4.0** — credit Fintraffic. Finnish waters only. **Not** GFW, AISStream, or own-edge AIS |
| `eccc-hydro-01` | `gaia.river.read@v1` | ECCC MSC GeoMet `hydrometric-realtime` (default `02HC003` Humber at Weston) | End-use Licence — commercial + attribution to ECCC. Stage may be geodetic |
| `fmi-01` | `gaia.weather.read@v1` | FMI open WFS simple observations (default Helsinki) | **CC BY 4.0** — attribution: Finnish Meteorological Institute |
| `nws-tsunami-01` | `gaia.tsunami.read@v1` | NWS CAP tsunami warning/watch/advisory | U.S. PD. Warning product, not a tide gauge. Empty feed → offline / no debit |
| `smhi-hydro-01` | `gaia.river.read@v1` | SMHI hydroobs parameter 2 (15-min discharge, default station 2357 Abisko) | **CC BY 4.0** — attribution: SMHI. Not a flood forecast |

Kystverket AIS, EMSC, NHC cyclones, EA England floods, PTWC Atom, and ADSB.lol live as **P3**. Current USGS continuous water quality and the additional environmental grids live as **P4**.

## P3 — licence-pinned public relays

Same bar as P2: reviewed `source_policy`, HTTPS allowlist, empty feed → offline / no debit.

| device_id | SKU | Upstream | License |
|-----------|-----|----------|---------|
| `nhc-cyclone-01` | `gaia.cyclone.read@v1` | NOAA NHC `CurrentStorms.json` | U.S. PD. Atlantic + East Pacific + Central Pacific only — **not** JTWC, **not** a NW-Pacific typhoon, **not** EONET. Empty season → offline |
| `emsc-01` | `gaia.quake.read@v1` | EMSC FDSN `seismicportal.eu` | **CC BY 4.0** — cite EMSC. Preliminary parameters. Distinct pin from `usgs-quake-01` |
| `ea-flood-01` | `gaia.flood.read@v1` | Environment Agency `/id/floods` | **OGL**. England only — not SEPA / NRW, not an in-situ gauge, not GloFAS. Attribution: EA flood and river level data (Beta) |
| `ptwc-01` | `gaia.tsunami.read@v1` | PTWC Atom `tsunami.gov/events/xml/PHEBAtom.xml` | U.S. PD. Warning product, not a tide gauge. Information-only earthquake statements are **not** sold. Empty → offline |
| `kystverket-ais-01` | `gaia.ais.public.read@v1` | BarentsWatch `bwapi/v1/geodata/ais/openpositions` | **NLOD 2.0**. Requires `GAIA_BARENTSWATCH_TOKEN` or client id/secret (same class as KNMI). Norwegian waters only — **not** Fintraffic, not global AIS |
| `adsb-lol-01` | `gaia.adsb.public.read@v1` | `api.adsb.lol` area query | **ODbL 1.0**. Operator-anchored (default LHR). Commercial **reading** OK; a public derived DB is share-alike. **Not** `gaia.adsb.read@v1`, not OpenSky / ADSBx |

## P4 — additional environmental point and grid relays

ATLAS contract: every visible point is located at the coordinate that produced
its reading. Station networks expose every official station as its own device or
bbox hotspot. Grids accept arbitrary buyer coordinates and resolve them to the
source/query cell; dense products fan out into cell-centre hotspots and hide
their zero-coordinate parent.

| Atlas layer / device | GAIA capability | Source and reading | Commercial basis / attribution |
|---|---|---|---|
| Smoke `hms-smoke-01` | `gaia.smoke.read@v1` | Every NOAA HMS polygon with its **full ring and holes**, stable `polygon_id`, geometry digest and bbox; the centroid is only the ATLAS map anchor. Qualitative light/medium/heavy, not PM2.5 | U.S. PD; cite NOAA/NESDIS HMS |
| Water quality `usgs-wq-01` | `gaia.water_quality.read@v1` | Fully paginates fresh latest-continuous observations (48h default, configurable `max_age_hours`) and batch-joins active sites to the official USGS monitoring-locations registry; optional parameter filter/`require_all`; one signed row per station coordinate; per-parameter timestamps, Approved/Provisional and qualifiers retained; stale/unresolved/truncated data fails closed | U.S. PD; cite USGS Water Data for the Nation |
| DART `noaa-dart-01`, `dart-*` | `gaia.dart.read@v1` | All active stations from the official NDBC directory (43 at the checked-in refresh); not a tsunami warning | U.S. PD; cite NOAA/NDBC |
| Precipitation `imerg-01` | `gaia.precipitation.read@v1` | NASA GPM IMERG Early Run cell at any requested coordinate; exact source cell centre; preliminary; Earthdata token | NASA open data; cite NASA GPM IMERG, no endorsement |
| Radar `nexrad-status-01` | `gaia.radar.status.read@v1` | One NEXRAD health/status reading per WSR-88D coordinate; not reflectivity | U.S. PD; cite NOAA/NWS ROC |
| Atmosphere `cams-*` | `gaia.atmosphere.read@v1` | CAMS aerosol/dust/pollen at any requested coordinate through paid or self-hosted Open-Meteo | Data CC BY 4.0; hosted free endpoint is non-commercial |
| Radiation `radnet-*` | `gaia.radnet.read@v1` | All 140 official EPA monitor coordinates; approved dose and derived R02–R09 total | U.S. Government data; cite EPA RadNet |
| Soil `soil-*` | `gaia.soil_moisture.read@v1` | Copernicus CLMS SWI020 at any requested coordinate; CDSE OAuth | Free for any purpose; Copernicus modification attribution |
| Solar `solar-*` | `gaia.solar.read@v1` | NASA POWER daily all/clear-sky irradiation at any requested coordinate and source date | NASA open data/CC0 unless marked; cite NASA POWER, no endorsement |
| Snow `snow-*` | `gaia.snow.read@v1` | NOAA NOHRSC/SNODAS depth and SWE at any requested CONUS coordinate, returned at the exact grid cell | U.S. PD; cite NOAA/NWS NOHRSC; model/provisional |
| Sea ice `nsidc-ice-01` | `gaia.sea_ice.read@v1` | Current Sea Ice Index v4 at any requested Arctic coordinate, returned at the exact EPSG:3411 cell centre | U.S. Government data; required Fetterer et al. citation; not for navigation |
| Land temperature `lst-*` | `gaia.land_temperature.read@v1` | Sentinel-3 SLSTR L2 1-km LST + uncertainty at any requested coordinate; CDSE OAuth | Copernicus free/full/open; modification attribution |

The checked-in station registries are generated from the official EPA ArcGIS
monitor directory and the official NDBC active-station XML. Refresh both GAIA
and ATLAS copies together from the monorepo root:

```bash
python3 scripts/update_p4_networks.py
```

The generator requires exactly 140 unique RadNet locations at refresh time and
rejects unmatched EPA CSV endpoints; DART membership follows every station
currently marked `dart="y"` by NDBC. Existing public device IDs for Birmingham,
Washington, Los Angeles, and DART 46407 are preserved for compatibility.

NISE v5 was evaluated but deliberately not exposed: its official coverage ended
16 January 2026. The current Sea Ice Index v4 is used instead so a paid LIVE SKU
does not silently resell an archive as current data.

## License / commercialize (what is intentionally NOT included)

These Azimuth-style sources are **not** wired as GAIA Hub SKUs because they are NC or commercially restricted without a separate deal:

| Source | Why excluded |
|--------|----------------|
| Global Fishing Watch | Non-commercial terms |
| Stanford RFI / related | **CC BY-NC** |
| ADSBx commercial API | Non-commercial / paid ToS |
| GPSJam heatmaps | Gray — avoid as paid SKU |
| aisstream alone | Gray — use only as own feeder, not sole commercial dependency |
| Blitzortung | Non-commercial |
| INTERMAGNET / Kyoto Dst | **CC BY-NC** — USGS geomag SKU is direct USGS only |
| Copernicus GloFAS WMS | Registered / authorised access — not scraped; flood layer is USGS WaterWatch |
| Open-Meteo **hosted free API** extra SKUs | Hosted ToS is non-commercial — see *Open-Meteo: data vs endpoint* below. Self-host, or buy a plan, before charging for `om-*` |

## Open-Meteo: data vs endpoint

Two different licences, and only one of them is a problem:

- **The data** is CC BY 4.0 — resell freely, attribution required.
- **The hosted free endpoint** is not: *"You may only use the free API services for
  non-commercial purposes"*, with "integration into commercial products" named as
  commercial use ([terms](https://open-meteo.com/en/terms)).

This matters more than one pin. Open-Meteo is the **20-city mesh** (`om-wx-*`,
`om-aq-*`) plus the marine anchors — 21 of the 34 weather pins ATLAS serves. The
non-OM remainder is essentially US + DE/NO/CA only, so dropping it collapses global
coverage. And `atlas.nearest.read@v1` defaults to `layers=["weather"]`, so the
default path of a **paid** SKU lands on these pins.

GAIA therefore **fails closed**: with payments on (`AIFACTORY_CRYPTO_ENABLED=1`) and
the origin still the hosted free API, `om-*` relays refuse to construct and name the
three ways out.

| Option | How | Trade-off |
|--------|-----|-----------|
| **Self-host, same host** (deploy default) | `scripts/deploy_gaia.sh` adds `om-node.yml` + `om-selfhost.yml`; sets `GAIA_OM_BASE_URL=http://open-meteo:8080` | Keeps all pins + global coverage. Costs disk: ~32–48 GB narrow variable set, 150 GB+ comprehensive. The deploy script refuses rather than half-syncing |
| **Self-host, separate big-disk host** | `sudo ./scripts/deploy_om_node.sh` there, then `GAIA_OM_BASE_URL=https://om.…` + `GAIA_OM_AUTH_TOKEN=…` here | Keeps the oracle host small. `deploy_gaia.sh` detects a remote origin and skips both the local instance and the local disk gate |
| **Paid plan** | `GAIA_OM_BASE_URL=<customer endpoint>` + `GAIA_OM_API_KEY`, or `GAIA_OM_ALLOW_HOSTED_COMMERCIAL=1` to assert you hold one | No disk cost; a subscription grants a commercial-use licence |
| **Stay non-commercial** | `deploy_gaia.sh --om-hosted`, payments off | Free tier is then within terms. Cannot charge |

### Running it on a separate host

`scripts/deploy_om_node.sh` deploys the API + sync on a big-disk host and puts nginx
in front with **TLS and a mandatory bearer token** (plus an optional source-IP
allowlist). The auth gate is not optional decoration:

- unauthenticated, this is our synced data served at our bandwidth to anyone;
- it keeps third parties from ever interacting with the AGPL program remotely.

GAIA sends the bearer via `GAIA_OM_AUTH_TOKEN`, and only to the operator-configured
origin — never to `open-meteo.com`, so an unset `GAIA_OM_BASE_URL` cannot leak the
secret to a third party. Air quality and marine need their own models synced on that
node (`OM_NODE_MODELS`) or those relays 404 and their pins stay offline.

### AGPL and our MIT licence

The server is **AGPLv3**. `gaia/docker-compose.om-node.yml` and
`gaia/docker-compose.om-selfhost.yml` are the only files in this MIT repo that touch
it, and only by image reference. Three properties keep GAIA a separate work:

1. **no vendored source** — the image is pulled from upstream at deploy time;
2. **unmodified** — AGPLv3 §13 obliges offering Corresponding Source of a *modified*
   version to users interacting with it remotely. Patch it → publish the patch;
3. **not publicly reachable** — no `ports:` in the compose, and the cross-host edge
   requires a bearer, so no third party is a remote user of it.

Break any one of those and get a lawyer before shipping.

A self-hosted relay says so in its own `source` string (`…; relayed via operator-run
Open-Meteo instance <origin>`) — provenance is never implied in this fleet.

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `GAIA_ENABLE_LIVE` | `0` (compose: `1`) | Register relays |
| `GAIA_OM_BASE_URL` | hosted free API | Open-Meteo forecast origin — self-host or paid customer endpoint |
| `GAIA_OM_AQ_BASE_URL` | hosted free API | Same for air quality |
| `GAIA_OM_MARINE_BASE_URL` | hosted free API | Same for marine |
| `GAIA_OM_API_KEY` | empty | `apikey=` for a paid Open-Meteo plan |
| `GAIA_OM_ALLOW_HOSTED_COMMERCIAL` | `0` | Assert a commercial plan covers the hosted origin |
| `GAIA_OM_SYNC_MODELS` / `_VARIABLES` / `_PAST_DAYS` | `ecmwf_ifs025` / 4 vars / `2` | What the self-hosted instance downloads (drives disk) |
| `GAIA_OM_MIN_FREE_GB` | `48` | Disk the deploy script demands before self-hosting |
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
| `GAIA_SAFECAST_MAX_AGE_DAYS` | `30` | Recency window (`captured_after`). **0** = archive (no date filter). Hub default stays 30; extra AU anchors use 0 so 2014 drive-grids remain on the map. Clamp 0–7300 |
| `GAIA_SAFECAST_MAX_PAGES` | `5` (recent) / `40` (archive) | Safecast pages × 150 rows. Override 1–40 |
| `GAIA_CYBERNEWS_ENABLED` | `1` | CyberNews GNSS CC BY |
| `GAIA_GNSS_EUREF_ENABLED` | `1` | EUREF EPN station/integrity inventory |
| `GAIA_GNSS_DIRECTORY_TTL_S` | `900` | EPN refresh TTL; stale disk cache survives outages |
| `GAIA_GNSS_GA_ENABLED` | `1` | Geoscience Australia GNSS station metadata |
| `GAIA_FEEDER_ENABLED` | `0` | Own edge ADS-B/AIS/IoT devices |
| `GAIA_FEEDER_TOKEN` | empty | Bearer token for `/feeder/v1/ingest` |
| `GAIA_EONET_ENABLED` | `1` | NASA EONET events |
| `GAIA_SWPC_ENABLED` | `1` | NOAA SWPC Kp + OVATION |
| `GAIA_GLM_ENABLED` | `1` | GOES GLM (needs h5py) |
| `GAIA_CAP_ENABLED` | `1` | NWS CAP alerts |
| `GAIA_EARTHDATA_TOKEN` | empty | Enables NASA IMERG downloads (free Earthdata Login token) |
| `GAIA_CDSE_CLIENT_ID` / `_CLIENT_SECRET` | empty | Enables Copernicus CLMS soil and Sentinel-3 LST |
| `GAIA_HMS_SMOKE_ENABLED` / `GAIA_USGS_WQ_ENABLED` / `GAIA_DART_ENABLED` | `1` | NOAA smoke, USGS water quality, NOAA DART |
| `GAIA_IMERG_ENABLED` / `GAIA_NEXRAD_STATUS_ENABLED` / `GAIA_CAMS_ENABLED` | `1` | IMERG (only registers with token), NEXRAD status, CAMS |
| `GAIA_RADNET_ENABLED` / `GAIA_COPERNICUS_SOIL_ENABLED` | `1` | EPA RadNet and credential-gated CLMS SWI |
| `GAIA_POWER_SOLAR_ENABLED` / `GAIA_NOHRSC_SNOW_ENABLED` | `1` | NASA POWER solar and NOAA NOHRSC snow cells |
| `GAIA_NSIDC_ICE_ENABLED` / `GAIA_SENTINEL3_LST_ENABLED` | `1` | Current Sea Ice Index and credential-gated Sentinel-3 LST |
| `GAIA_SC_ENABLED` | `1` | Sensor.Community |
| `GAIA_CWOP_STATION` | `EW1156` | MADIS CWOP id |
| `GAIA_ARGO_WMO` | `4902911` | Argo WMO |
| `GAIA_METNO_ICAO` | `ENGM` | MET Norway METAR |
| `GAIA_GEOMAG_IMO` | `BOU` | USGS geomag observatory |
| `GAIA_EIA_API_KEY` | empty | Enables EIA US48 demand |
| `GAIA_KNMI_API_KEY` | empty | Enables KNMI 10-min obs |
| `GAIA_BARENTSWATCH_TOKEN` | empty | Enables `kystverket-ais-01` (BarentsWatch AIS Bearer) |
| `GAIA_BARENTSWATCH_CLIENT_ID` / `_CLIENT_SECRET` | empty | Alternative: OpenID client-credentials (`scope=ais`) |
| `GAIA_ADSB_LOL_LAT` / `_LON` / `_DIST_NM` | `51.47` / `-0.4543` / `80` | ADSB.lol operator anchor (LHR) and radius nmi |
| `GAIA_*_ENABLED` | `1` | Per-relay toggles (`OM_WEATHER`, `OM_AQ`, `OM_MARINE`, `UK_CARBON`, `USGS_QUAKE`, `USGS_RIVER`, `NOAA_TIDE`, `NDBC`, `FLOOD`, `EFFIS`, `VOLCANO`, `DWD`, `ECCC`, `AURN`, `GEONET`, `UHSLC`, `NHC`, `EMSC`, `EA_FLOOD`, `PTWC`, `KYSTVERKET_AIS`, `ADSB_LOL`) |

## Honesty

- Factory storefront ≠ hub catalog. GAIA sells **physical / relay** readings on the hub.
- Live keys prove **relay custody**, not sensor ownership.
- OpenAQ is optional until an operator supplies a free API key.
- Attribution: Open-Meteo requires CC BY 4.0 credit; NWS/USGS/NOAA are US Government public domain; UK Carbon Intensity is National Grid ESO open data; FIRMS requires NASA citation; Safecast is CC0; CyberNews GNSS requires CC BY 4.0 attribution.
- Edge feeders attest **your** receiver push — never launder third-party NC aggregators as LIVE provenance.
- Add more instances of existing kinds with one command: [`docs/add-gaia-atlas-sensor.md`](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md).

## Map / watchboxes / composite briefs → ATLAS

Open-data devices above are **GAIA fleet SKUs**. Map layers, pins,
**watchboxes**, and composite Hub SKUs
(`atlas.situation.brief@v1`, `atlas.fire.weather@v1`, `atlas.nearest.read@v1`,
`atlas.watchbox.check@v1`)
live on **ATLAS** — see [`atlas/docs/GUIDE.md`](https://github.com/alexar76/atlas/blob/main/docs/GUIDE.md)
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
| 2026-08-05 | weather + quake | $0.02 | [`onchain-journal` §3l](https://github.com/alexar76/aicom/blob/main/docs/onchain-journal.md) |
| 2026-08-05 | Open-Meteo weather only | $0.01 | [`onchain-journal` §3m](https://github.com/alexar76/aicom/blob/main/docs/onchain-journal.md) |
