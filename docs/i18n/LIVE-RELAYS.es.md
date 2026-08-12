# GAIA: relés en vivo — guía del operador

**Idiomas:** [EN](../LIVE-RELAYS.md) · [RU](LIVE-RELAYS.ru.md) · [ES](LIVE-RELAYS.es.md) · [FR](LIVE-RELAYS.fr.md) · [ZH](LIVE-RELAYS.zh.md)

**Developer:** [add-gaia-atlas-sensor](../../../docs/add-gaia-atlas-sensor.md) (EN · RU · ES · FR · ZH)

## Idea

Un dispositivo live **no posee** el sensor. La clave Ed25519 atestigua:

> el gateway retransmitió fielmente lo que devolvió la API pública *X* en el momento del fetch

Misma vía de atestación y Pay-on-Verified. El `source` aparece en `gaia.fleet.status@v1`.

## Seguridad

| Control | Comportamiento |
|---------|----------------|
| Allowlist de hosts | Solo HTTPS en `_ALLOWED_HOSTS` (`live.py`) |
| Sin URLs del cliente | En `input` solo `device_id` (+ filtros seguros abajo) |
| Sanitización de IDs | Station / box / lat-lon / NOAA / OpenAQ validados antes de armar la URL |
| Sin URLs con credenciales | `user:pass@host` rechazado |
| Sin redirects | `follow_redirects=False`; no-200 → offline |
| Facturación | Fallo upstream → offline → el Hub **no** debita |

## Qué pasa el comprador en invoke

| Capability | Buyer `input` | Quién elige la geografía |
|------------|---------------|--------------------------|
| La mayoría de `gaia.*.read@v1` | `{ "device_id": "…" }` | El **operador** ancla el device (o publica un mesh de ciudades — el comprador elige `device_id`) |
| `gaia.window@v1` | `{ "device_id", "n" }` | Igual que read |
| `gaia.fire.read@v1` | `{ "device_id"?, "west"?, "south"?, "east"?, "north"?, "limit"? }` | El **comprador puede** filtrar el CSV FIRMS por bbox / top-N — sin URLs de cliente |

Los sensores fijos (clima, aire, marea, Safecast, …) siguen anclados por el operador. Feeds de eventos como FIRMS son globales; el bbox es la forma de preguntar “fuegos cerca” sin inventar un host upstream.

## Catálogo (`GAIA_ENABLE_LIVE=1`)

### Clima — `gaia.weather.read@v1`

| device_id | Upstream | Notas |
|-----------|----------|-------|
| `ws-01` / `ws-02` | simulador | Siempre presente |
| `nws-01` | NOAA/NWS | Estaciones US; dominio público; User-Agent requerido |
| `om-wx-01` | Open-Meteo | Global; default Berlín (`GAIA_OM_LAT`/`LON`); CC BY 4.0 |

### Aire — `gaia.air.read@v1`

| device_id | Upstream | Notas |
|-----------|----------|-------|
| `aq-01` | simulador | Siempre presente |
| `osm-01` | openSenseMap | Citizen science; licencia por box |
| `om-aq-01` | Open-Meteo AQ | PM2.5/PM10/CO₂; sin clave |
| `sta-01` | OGC SensorThings | Opcional; puede timeout (`GAIA_STA_ENABLED`) |
| `openaq-01` | OpenAQ v3 | **Requiere** `GAIA_OPENAQ_API_KEY` |

### Red — `gaia.grid.read@v1` (solo LIVE)

| device_id | Upstream | Campo |
|-----------|----------|-------|
| `uk-grid-01` | carbonintensity.org.uk | `carbon_intensity_gco2_kwh` (actual, si no forecast) |

### Sismos — `gaia.quake.read@v1` (solo LIVE)

| device_id | Upstream | Campos |
|-----------|----------|--------|
| `usgs-quake-01` | USGS GeoJSON M≥2.5/día | `magnitude`, `depth_km`, `latitude`, `longitude` |

### Marea — `gaia.tide.read@v1` (solo LIVE)

| device_id | Upstream | Campo |
|-----------|----------|-------|
| `noaa-tide-01` | NOAA CO-OPS | `water_level_m` (MLLW, métrico); default 8518750 |

### Río — `gaia.river.read@v1` (solo LIVE)

| device_id | Upstream | Campos |
|-----------|----------|--------|
| `usgs-river-01` | USGS NWIS | `discharge_m3s`, `gage_height_m`; default `01646500` Potomac |

### Marino — `gaia.marine.read@v1` (solo LIVE)

| device_id | Upstream | Campos |
|-----------|----------|--------|
| `ndbc-01` | NOAA NDBC buoy | `wave_height_m`, `sst_c`, `wind_mps` (si hay); default `44025` |
| `om-marine-01` | Open-Meteo Marine | `wave_height_m`, `sst_c`; default puerto NYC |

### Incendio — `gaia.fire.read@v1` (LIVE · libre de comercializar)

| device_id | Upstream | Campos | Licencia |
|-----------|----------|--------|----------|
| `firms-fire-01` | NASA FIRMS VIIRS CSV (opc. `GAIA_FIRMS_MAP_KEY`) | Attested: `brightness_k` más brillante, `confidence`, lat/lon. Payload de mapa: `hotspots[]` (top-N, default `GAIA_FIRMS_HOTSPOT_LIMIT=500`, max 5000) + `hotspot_count`. Opc. buyer `west/south/east/north` + `limit`. | Open data NASA — **citar NASA FIRMS** + disclaimer |

La capa Wildfire de ATLAS expande `hotspots[]` a un pin por detección (`firms-hs-NNNN`).

### Radiación — `gaia.radiation.read@v1` (LIVE · libre de comercializar)

| device_id | Upstream | Campos | Licencia |
|-----------|----------|--------|----------|
| `safecast-01` | Safecast measurements API | `cpm`, `latitude`, `longitude` | **CC0** |

### Interferencia GNSS — `gaia.jamming.read@v1` (LIVE · libre de comercializar)

| device_id | Upstream | Campos | Licencia |
|-----------|----------|--------|----------|
| `cybernews-jam-01` | cybernews.space `/api/data/gnss` | `interference_score`, `radius_km`, lat/lon | **CC BY 4.0** — atribución requerida |

### Tráfico edge — `gaia.adsb.read@v1` / `gaia.ais.read@v1` (feeder opt-in)

| device_id | Upstream | Notas |
|-----------|----------|-------|
| `feeder-adsb-01` | dump1090 propio → `POST /feeder/v1/ingest` | `GAIA_FEEDER_ENABLED=1` + `GAIA_FEEDER_TOKEN`. Offline hasta el primer ingest. **No** ADSBx / agregadores NC. |
| `feeder-ais-01` | Receptor AIS propio | Misma ruta. **No** aisstream como único SKU de pago. |

## No incluido como SKU de pago

| Fuente | Por qué |
|--------|---------|
| Global Fishing Watch | Non-commercial |
| Stanford RFI / related | **CC BY-NC** |
| ADSBx commercial API | NC / ToS de pago |
| GPSJam heatmaps | Zona gris — evitar como SKU de pago |
| aisstream alone | Solo como feeder propio, no única dependencia comercial |

## Mapa / watchbox / composite → ATLAS

Los dispositivos de arriba son **SKU de flota GAIA**. Capas, pines, **watchboxes** y
SKU compuestos Hub (`atlas.situation.brief@v1`, `atlas.fire.weather@v1`,
`atlas.nearest.read@v1`, `atlas.watchbox.check@v1`) viven en **ATLAS**:
[GUIDE](../../../atlas/docs/GUIDE.md) (EN · RU · ES · FR · ZH).
No hay superficie de producto separada en GAIA.

**Watchbox** (resumen): un bbox guardado + filtro de capas; el agente/operador hace
**check** periódicamente y recibe coincidencias LIVE en el marco + content receipt.
Suscripción = REST; check = SKU facturable en el Hub. Detalle en ATLAS GUIDE.

## Hub

Tras **redeploy GAIA** + crawl de federación `iot.modelmarket.dev`, los SKUs de lectura
aparecen en el catálogo. Textos UI: `aimarket-hub/cap-descriptions-i18n.json`
(EN · RU · ES · FR · ZH). Composites ATLAS tras crawl del peer `atlas.modelmarket.dev`.
