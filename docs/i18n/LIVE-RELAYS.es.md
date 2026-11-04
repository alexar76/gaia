# GAIA: relés en vivo — guía del operador

**Idiomas:** [EN](../LIVE-RELAYS.md) · [RU](LIVE-RELAYS.ru.md) · [ES](LIVE-RELAYS.es.md) · [FR](LIVE-RELAYS.fr.md) · [ZH](LIVE-RELAYS.zh.md)

**Developer:** [add-gaia-atlas-sensor](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md) (EN · RU · ES · FR · ZH)

**Casos de uso del operador ATLAS:** [`OPERATOR-USE-CASES.es.md`](https://github.com/alexar76/atlas/blob/main/docs/i18n/OPERATOR-USE-CASES.es.md)

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
| `gaia.air.read@v1` | `{ "device_id", "latitude"?, "longitude"? }` | Pin del **operador** **o** lat/lon del comprador en `om-aq-*` / `sc-01` / `sc-{slug}` (Open-Meteo; Sensor.Community area — vacío → offline) |
| `gaia.window@v1` | `{ "device_id", "n" }` | Igual que read |
| `gaia.fire.read@v1` | `{ "device_id"?, "west"?, "south"?, "east"?, "north"?, "limit"? }` | El **comprador puede** filtrar el CSV FIRMS por bbox / top-N — sin URLs de cliente |

La mayoría de sensores fijos siguen anclados por el operador. Excepciones con `latitude`/`longitude` del comprador en el mismo SKU: Open-Meteo AQ (`om-aq-*`) y Sensor.Community (`sc-*`). Feeds de eventos como FIRMS son globales; el bbox es la forma de preguntar “fuegos cerca” sin inventar un host upstream.

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
| `om-aq-01` | Open-Meteo AQ | PM2.5/PM10/CO₂; sin clave; el comprador puede pasar `latitude`/`longitude` |
| `sc-01`, `sc-{slug}` | Sensor.Community | Consulta de área SDS011 crowd (ODbL — citar). Berlín + mesh de ciudades (`GAIA_SC_MESH_ENABLED`). Lat/lon del comprador. Área vacía → offline. Crowd ≠ estación de referencia; mesh ≠ cada nodo en la Tierra |
| `sta-01` | OGC SensorThings | Opcional; puede timeout (`GAIA_STA_ENABLED`) |
| `openaq-01` | OpenAQ v3 | **Requiere** clave gratuita de [explore.openaq.org](https://explore.openaq.org) → `GAIA_OPENAQ_API_KEY` |

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

Hub `safecast-01` conserva una ventana de 30 días. Los anclajes de mapa `safecast-melbourne` / `safecast-adelaide` usan archivo (`max_age_days: 0`); si no, desaparece la malla de 2014 del sur de Australia. Los pines llevan `captured_at` — no es «ahora».

### Interferencia GNSS — `gaia.jamming.read@v1` (LIVE · libre de comercializar)

| device_id | Upstream | Campos | Licencia |
|-----------|----------|--------|----------|
| `cybernews-jam-01` | cybernews.space `/api/data/gnss` | `interference_score`, `radius_km`, lat/lon | **CC BY 4.0** — atribución requerida |

### Tráfico edge — `gaia.adsb.read@v1` / `gaia.ais.read@v1` (feeder opt-in)

| device_id | Upstream | Notas |
|-----------|----------|-------|
| `feeder-adsb-01` | dump1090 propio → `POST /feeder/v1/ingest` | `GAIA_FEEDER_ENABLED=1` + `GAIA_FEEDER_TOKEN`. Offline hasta el primer ingest. **No** ADSBx / agregadores NC. |
| `feeder-ais-01` | Receptor AIS propio | Misma ruta. **No** aisstream como único SKU de pago. |

### P0 — aire crowd (Sensor.Community)

| device_id | SKU | Notas |
|-----------|-----|-------|
| `sc-01`, `sc-{slug}` | `gaia.air.read@v1` | Sensor.Community **ODbL** — citar. Lat/lon del comprador en el SKU; mesh de ciudades (`GAIA_SC_MESH_ENABLED`). Área vacía → offline. Crowd ≠ estación de referencia; mesh ≠ cada nodo en la Tierra |

### P2 — relés públicos con licencia fijada

El AIS de borde propio (`gaia.ais.read@v1`) sigue siendo el receptor del operador. El AIS público es **otro** SKU.

| device_id | SKU | Upstream | Licencia |
|-----------|-----|----------|----------|
| `fintraffic-ais-01` | `gaia.ais.public.read@v1` | Fintraffic Digitraffic AIS (`meri.digitraffic.fi`) | **CC BY 4.0** — acreditar Fintraffic. Solo aguas finlandesas. **No** GFW, AISStream ni AIS propio |
| `eccc-hydro-01` | `gaia.river.read@v1` | ECCC MSC GeoMet (default `02HC003` Humber) | End-use Licence — comercial + atribución ECCC. La cota puede ser geodésica |
| `fmi-01` | `gaia.weather.read@v1` | FMI open WFS (Helsinki) | **CC BY 4.0** — Instituto Meteorológico Finlandés |
| `nws-tsunami-01` | `gaia.tsunami.read@v1` | NWS CAP tsunami warning/watch/advisory | PD EE.UU. Producto de alerta, no un mareógrafo. Feed vacío → offline / no débito |
| `smhi-hydro-01` | `gaia.river.read@v1` | SMHI hydroobs (estación 2357 Abisko) | **CC BY 4.0** — SMHI. No es un pronóstico de crecida |

Kystverket AIS, EMSC, ciclones NHC, inundaciones EA (Inglaterra), Atom PTWC y ADSB.lol viven como **P3**.

| device_id | SKU | Notas |
|-----------|-----|-------|
| `nhc-cyclone-01` | `gaia.cyclone.read@v1` | NHC PD — solo AL/EP/CP |
| `emsc-01` | `gaia.quake.read@v1` | EMSC CC BY 4.0 — citar EMSC |
| `ea-flood-01` | `gaia.flood.read@v1` | EA OGL, solo Inglaterra |
| `ptwc-01` | `gaia.tsunami.read@v1` | PTWC Atom — no un mareógrafo |
| `kystverket-ais-01` | `gaia.ais.public.read@v1` | BarentsWatch NLOD — token |
| `adsb-lol-01` | `gaia.adsb.public.read@v1` | ADSB.lol ODbL 1.0 |
| `frost-*-01` (6) | `gaia.weather.read@v1` | MET Norway Frost (CC BY 4.0 + NLOD) — in-situ, not METAR; needs `GAIA_FROST_CLIENT_ID` |
| `dmi-*-01` (5) | `gaia.weather.read@v1` | DMI metObs CC BY 4.0 — Denmark, no API key |
| `sepa-*-01` | `gaia.river.read@v1` | SEPA KiWIS OGL — Scotland only |
| `nrw-*-01` | `gaia.river.read@v1` | NRW River Levels OGL — Wales only; needs `GAIA_NRW_API_KEY` |
| `ingv-01` | `gaia.quake.read@v1` | INGV FDSN CC BY 4.0 — cite INGV; Italy/regional, not USGS/EMSC replacement |
| `cdip-*-01` | `gaia.marine.read@v1` | CDIP ERDDAP — acknowledge CDIP/Scripps/USACE + [cdip.ucsd.edu](https://cdip.ucsd.edu/); in-situ waves |
| `icos-*-01` | `gaia.ghg.read@v1` **new** | ICOS ATC NRT CO₂ CC BY 4.0 — cite ICOS; needs `GAIA_ICOS_CPAUTH_TOKEN` |
| `hubeau-*-01` (20 FR) | `gaia.river.read@v1` | Hub'Eau hydrométrie (Etalab OL 2.0) — solo Francia; no Vigicrues |
| `ehyd-*-01` (24 AT) | `gaia.river.read@v1` | eHYD / BMLUK `pegel_aktuell` CC BY 4.0 — «Datenquelle: ehyd.gv.at»; solo Austria |
| `smhi-wx-*-01` (15 SE) | `gaia.weather.read@v1` | SMHI MetObs CC BY 4.0 — clima in-situ; **no** `smhi-hydro-01` |
| `sg-wx-*-01` (12 SG) | `gaia.weather.read@v1` | Singapore NEA / data.gov.sg — Singapore Open Data Licence; viento (+ T/RH/lluvia) |
| `sg-psi-*-01` (5 regiones) | `gaia.air.read@v1` | Singapore NEA PSI / PM2.5 — Open Data Licence; índice regional 24h |
| `hko-wx-*-01` (15 HK) | `gaia.weather.read@v1` | HKO `rhrread` — DATA.GOV.HK, atribución HKSAR / HKO |
| `be-aq-*-01` (12 BE) | `gaia.air.read@v1` | IRCELINE SOS PM2.5 CC BY 4.0 — citar IRCELINE; solo Bélgica |
| `aemet-wx-*-01` (15 ES) | `gaia.weather.read@v1` | AEMET OpenData — reutilización PSI, «Fuente: AEMET»; clave `GAIA_AEMET_API_KEY` |
| `ch-wx-*-01` (15 CH) | `gaia.weather.read@v1` | MeteoSwiss OGD CC BY 4.0 — in situ; GeoJSON LV95 → WGS84 |
| `bafu-*-01` (15 CH) | `gaia.river.read@v1` | BAFU/FOEN LINDAS OGD-CH Open-Use — Q + nivel geodésico; no es aviso de crecida |
| `cwa-wx-*-01` (12 TW) | `gaia.weather.read@v1` | CWA OpenData OGDL 1.0; clave `GAIA_CWA_API_KEY` |
| `mf-wx-*-01` (12 FR) | `gaia.weather.read@v1` | Météo-France DPObs Etalab OL 2.0; clave `GAIA_METEOFRANCE_APPLICATION_ID` |
| `ee-wx-*-01` (12 EE) | `gaia.weather.read@v1` | Estonian Weather Service XML CC BY 4.0 |
| `is-wx-*-01` (12 IS) | `gaia.weather.read@v1` | IMO AWS horario ODC-By |
| `hk-aqhi-*-01` (18 HK) | `gaia.air.read@v1` | EPD AQHI DATA.GOV.HK — índice 1–10, **no** PM2.5 |

Densify (mismos SKU, más pines): EA 7→20, PEGELONLINE 10→22, ECCC 1→8, FMI 1→5, Frost 2→6, DMI 2→5, AURN Londres 1→5, UHSLC FD 1→5.

## P4 — redes completas y capas por coordenada

Un punto ATLAS corresponde a una coordenada de lectura. Las redes publican todas las estaciones oficiales; las fuentes de cuadrícula aceptan `latitude`/`longitude` arbitrarios del comprador y devuelven la coordenada de la celda fuente/consulta.

| Capa / device_id | Cobertura | Base comercial |
|---|---|---|
| Humo `hms-smoke-01` | Cada polígono HMS con su anillo completo y huecos, `polygon_id` estable, digest de geometría y bbox; el centroide es solo el ancla del mapa | Dominio público EE. UU.; citar NOAA/NESDIS HMS |
| Calidad del agua `usgs-wq-01` | Pagina observaciones latest-continuous frescas (48 h por defecto, `max_age_hours`) y une sitios a USGS monitoring-locations; filtro/`require_all`; una fila firmada por coordenada; hora, Approved/Provisional y qualifiers; datos stale/incompletos se rechazan | Dominio público EE. UU.; citar USGS |
| DART `noaa-dart-01`, `dart-*` | Las 43 estaciones activas del directorio NDBC fijado | Dominio público EE. UU.; citar NOAA/NDBC |
| Precipitación `imerg-01` | Cualquier coordenada; centro exacto de celda IMERG; preliminar | Datos abiertos NASA; citar NASA GPM |
| Estado radar `nexrad-status-01` | Todos los WSR-88D en sus coordenadas; no reflectividad | Dominio público EE. UU.; citar NOAA/NWS |
| Atmósfera `cams-*` | Cualquier coordenada CAMS | CC BY 4.0; Open-Meteo comercial/self-hosted |
| EPA RadNet `radnet-*` | Las 140 coordenadas oficiales de monitores | Datos del Gobierno de EE. UU.; citar EPA RadNet |
| Suelo `soil-*` | Cualquier coordenada CLMS SWI020 | Copernicus: cualquier finalidad con atribución |
| Solar `solar-*` | Cualquier coordenada NASA POWER | Datos abiertos NASA; citar NASA POWER |
| Nieve `snow-*` | Cualquier coordenada CONUS; celda SNODAS exacta | Dominio público EE. UU.; citar NOAA/NOHRSC |
| Hielo marino `nsidc-ice-01` | Cualquier coordenada ártica; celda exacta de 25 km | Datos Gobierno EE. UU.; cita obligatoria; no navegación |
| Temperatura terrestre `lst-*` | Cualquier coordenada Sentinel-3 SLSTR | Copernicus free/full/open con atribución de modificaciones |

Los registros RadNet y DART se actualizan juntos con `python3 scripts/update_p4_networks.py`, que escribe las copias de despliegue de GAIA y ATLAS.

## Puntos de posible inundación y aguas subterráneas del USGS

- **Puntos de posible inundación** (`usgs-rtfi-01`, `gaia.flood.read@v1`) aparecen en la capa de inundaciones de ATLAS. GAIA compara el umbral medido en campo con una lectura de la misma serie de la estación hidrométrica tomada en las últimas dos horas. La ficha muestra la hora de observación, el nivel, el umbral y cuánto se supera. El resultado es un indicio provisional de posible inundación. Los cierres de carreteras requieren confirmación independiente. Una respuesta vacía no genera una observación facturable. [API USGS RT-FI](https://api.waterdata.usgs.gov/rtfi-api/docs)
- **Aguas subterráneas** (`usgs-groundwater-01`, `gaia.groundwater.read@v1`) cubre tres pozos piloto del USGS en Maine, Nueva York y Kentucky. La profundidad del agua se expresa en metros respecto a la superficie local del terreno, con valores positivos hacia abajo. Se conservan la hora de observación, el estado de aprobación y el indicador de calidad de origen. Se admiten lecturas de las últimas 24 horas. [USGS Water Data API](https://api.waterdata.usgs.gov/)

Los datos del USGS son de dominio público en EE. UU. Atribuya los datos al USGS. Ambos relés están habilitados por defecto cuando `GAIA_ENABLE_LIVE=1`. Para deshabilitarlos, use `GAIA_USGS_RTFI_ENABLED=0` o `GAIA_USGS_GROUNDWATER_ENABLED=0`, respectivamente. [Condiciones del USGS](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits)

## P13 — precios eléctricos del día siguiente e imágenes Sentinel-2

| device_id | SKU | Fuente | Licencia / alcance |
|-----------|-----|--------|--------------------|
| `smard-price-<zona>` (15 zonas: `de-lu`, `at`, `be`, `no2`, `dk1`, `dk2`, `fr`, `it-north`, `nl`, `pl`, `se4`, `ch`, `si`, `cz`, `hu`) | `gaia.power.price.read@v1`, `gaia.power.window@v1` | Series del día siguiente de SMARD.de (Bundesnetzagentur), bloques semanales por cuarto de hora | **CC BY 4.0** — citar Bundesnetzagentur / SMARD.de. Precio mayorista de bolsa, no una tarifa. Series emparejadas con zonas por valores. `GAIA_SMARD_ZONES` limita las zonas, `GAIA_SMARD_ENABLED=0` las desactiva. [Guía](POWER-PRICES.es.md) |
| `s2-l2a-01` (consulta por punto) | `gaia.imagery.read@v1` | STAC de Element84 Earth Search + AWS Open Data `sentinel-cogs` | **Datos Copernicus Sentinel** — libres, completos y abiertos, incluido el uso comercial; crédito «Contains modified Copernicus Sentinel data <año>». Un píxel de la escena despejada más reciente. `GAIA_S2_ENABLED=0` lo desactiva. [Guía](IMAGERY.es.md) |

No son relés, pero salieron en la misma tanda: `gaia.place.resolve@v1`, gratuita ([nomenclátor GeoNames](PLACES.es.md)), y el control de dispositivos con pago verificado `gaia.actuate@v1` ([guía](ACTUATION.es.md)).

## No incluido como SKU de pago

| Fuente | Por qué |
|--------|---------|
| Global Fishing Watch | Non-commercial |
| Stanford RFI / related | **CC BY-NC** |
| ADSBx commercial API | NC / ToS de pago |
| GPSJam heatmaps | Zona gris — evitar como SKU de pago |
| aisstream alone | Solo como feeder propio, no única dependencia comercial |
| ENTSO-E Transparency | Libre de ver (EU 543/2013), pero ToS sin redistribución comercial clara para SKU de pago |

## Mapa / watchbox / composite → ATLAS

Los dispositivos de arriba son **SKU de flota GAIA**. Capas, pines, **watchboxes** y
SKU compuestos Hub (`atlas.situation.brief@v1`, `atlas.fire.weather@v1`,
`atlas.nearest.read@v1`, `atlas.watchbox.check@v1`) viven en **ATLAS**:
[GUIDE](https://github.com/alexar76/atlas/blob/main/docs/GUIDE.md) (EN · RU · ES · FR · ZH).
No hay superficie de producto separada en GAIA.

**Watchbox** (resumen): un bbox guardado + filtro de capas; el agente/operador hace
**check** periódicamente y recibe coincidencias LIVE en el marco + content receipt.
Suscripción = REST; check = SKU facturable en el Hub. Detalle en ATLAS GUIDE.

## Hub

Tras **redeploy GAIA** + crawl de federación `iot.modelmarket.dev`, los SKUs de lectura
aparecen en el catálogo. Textos UI: `aimarket-hub/cap-descriptions-i18n.json`
(EN · RU · ES · FR · ZH). Composites ATLAS tras crawl del peer `atlas.modelmarket.dev`.

## Fuentes de datos adicionales

| Capa o fuente | GAIA | Qué muestra el mapa | Fuente y condiciones |
|---|---|---|---|
| Aguas subterráneas de Hub’Eau | `gaia.groundwater.read@v1` · `hubeau-groundwater-01` | Ocho pozos piloto franceses informan de la profundidad desde la referencia de medición y de la cota del agua en NGF. La ficha muestra la hora y la referencia de cada valor. | [Etalab Licence Ouverte](https://assistance.brgm.fr/fr/aide/HubEau) |
| Pronóstico fluvial NOAA NWPS | `gaia.river.forecast.read@v1` · `nwps-pttp1`, `nwps-rmdv2`, `nwps-vckm6`, `nwps-dlrt2` | La ficha muestra el nivel más alto entre los puntos disponibles del pronóstico de las próximas 72 horas, la hora de emisión y la hora del máximo. Las alturas se refieren al datum local de la estación. | [NOAA/NWS · U.S. public domain](https://water.noaa.gov/about/api) |
| Mareógrafos de Environment Agency | `gaia.tide.read@v1` · `ea-tide-e70039`, `ea-tide-e72639`, `ea-tide-e70539`, `ea-tide-e70739`, `ea-tide-e73839` | Las estaciones costeras del Reino Unido miden el nivel medio del mar en metros sobre Ordnance Datum Newlyn (mAOD). | [Environment Agency · OGL v3](https://environment.data.gov.uk/flood-monitoring/doc/tidegauge) |
| Incidencias viales de Fintraffic | `gaia.road.events.read@v1` · `fintraffic-road-announcements-01`, `fintraffic-roadworks-01` | Los avisos y las obras en Finlandia muestran el tramo afectado y el periodo de validez publicado. | [Fintraffic Digitraffic · CC BY 4.0](https://www.digitraffic.fi/en/terms-of-service/) |
| NOAA G-AIRMET | `gaia.aviation.hazards.read@v1` · `awc-gairmet-01` | Los polígonos y líneas de previsión aeronáutica para Estados Unidos muestran el peligro y las horas de emisión y validez. | [NOAA/NWS Aviation Weather Center · U.S. public domain](https://aviationweather.gov/data/api/) |
| Avisos y cortes de carretera de NDW | `gaia.road.events.read@v1` · `ndw-road-srti-01`, `ndw-road-closures-01` | La capa `road_events` muestra avisos de seguridad vial y cortes temporales en los Países Bajos con la geometría de los tramos afectados. Cada publicación firmada caduca cinco minutos después de su emisión. Cada medida tiene su propio periodo de validez. | [NDW · CC0 1.0](https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/) |
| Boyas de oleaje del FMI en el Báltico | `gaia.marine.read@v1` · `fmi-marine-103976`, `fmi-marine-108126`, `fmi-marine-108499`, `fmi-marine-134220`, `fmi-marine-134221`, `fmi-marine-134246` | Seis boyas en `marine` informan de la altura significativa de las olas. La ficha incluye el periodo de pico, la dirección de procedencia del oleaje y la temperatura del agua cuando se midieron a la misma hora. La marca de tiempo indica el inicio del muestreo del oleaje. Las observaciones caducan a las dos horas. | [FMI · CC BY 4.0](https://en.ilmatieteenlaitos.fi/download-observations-questions) |
| Manto nival de USDA NRCS SNOTEL | `gaia.snow.read@v1` · `snotel-335`, `snotel-415` | La capa `snow` muestra el espesor de la nieve y su equivalente en agua en centímetros a partir de mediciones horarias en Berthoud Summit y Copper Mountain, Colorado. GAIA empareja las mediciones de la misma hora que han superado el control automático de calidad y tienen como máximo seis horas de antigüedad. | [USDA NRCS · dominio público de EE. UU.](https://catalog.data.gov/dataset/snowpack-telemetry-network-snotel) |
| Observaciones marinas de SMHI | `gaia.ocean.observations.read@v1` · `smhi-ocean-flinten16-8m`, `smhi-ocean-flinten16-2m`, `smhi-ocean-flinten7-8m`, `smhi-ocean-kalmar2-0m` | `ocean_obs` · Cuatro series a profundidades definidas en tres estaciones suecas. La ficha muestra la salinidad en PSU, el oxígeno disuelto en mL/L o la velocidad y dirección de la corriente, según la estación y la profundidad. Las mediciones corresponden a la misma hora y tienen una antigüedad máxima de tres horas. | [SMHI · CC BY 4.0](https://www.smhi.se/data/om-smhis-data/villkor-for-anvandning) |
| Pronóstico de humo de ECCC | `gaia.smoke.forecast.read@v1` · `eccc-smoke-seattle`, `eccc-smoke-toronto`, `eccc-smoke-calgary` | `smoke_forecast` · Tres celdas del modelo cerca de Seattle, Toronto y Calgary. La ficha muestra el pronóstico de PM₂.₅ total cerca de la superficie dentro de la pluma de humo modelada, en µg/m³, para la próxima hora en punto UTC. También indica la hora de ejecución del modelo y la hora de validez del pronóstico. La ejecución puede tener hasta 18 horas de antigüedad. | [ECCC · MSC End-use Licence](https://eccc-msc.github.io/open-data/licence/readme_en/) |
| NOAA SWPC D-RAP | `gaia.hf.absorption.read@v1` · `swpc-drap-01` | `hf_absorption` · La cuadrícula global del modelo muestra la frecuencia más alta afectada por una absorción de 1 dB en la región D. La vista global muestra los máximos en MHz en 900 celdas de 6° × 12°. Al acercar el mapa se ven las muestras originales de las 8100 celdas de 2° × 4°. La cuadrícula caduca a los 15 minutos. | [NOAA SWPC](https://www.swpc.noaa.gov/products/d-region-absorption-predictions-d-rap) |
| Tasa de dosis gamma ambiental de BfS | `gaia.radiation.ambient.read@v1` · `bfs-odl-01` | `radiation` · Las estaciones alemanas muestran tasas horarias validadas de equivalente de dosis gamma ambiental en nSv/h. La ficha indica el final de la hora de observación. Los datos caducan a las dos horas. | [BfS · DL-DE-BY 2.0](https://www.imis.bfs.de/geoportal/resources/sitepolicy.html) |
| Altura del agua costera de GeoNet | `gaia.tide.read@v1` · `geonet-coastal-auct`, `geonet-coastal-wlgt`, `geonet-coastal-taut`, `geonet-coastal-gist`, `geonet-coastal-jbtt` | `tide` · Cinco estaciones de Nueva Zelanda muestran la altura del agua en metros sobre el sensor de presión local. Los datos caducan a los cinco minutos. | [GeoNet · CC BY 3.0 NZ](https://www.geonet.org.nz/policy) |
| Observaciones fluviales de ČHMÚ | `gaia.river.read@v1` · `chmi-river-042000`, `chmi-river-101800`, `chmi-river-200100` | `river` · Tres estaciones checas muestran mediciones preliminares del nivel y el caudal, además de la temperatura del agua si se midió al mismo tiempo. Los datos caducan a las dos horas. | [ČHMÚ · CC BY 4.0](https://www.chmi.cz/-/kde-najdu-otev%C5%99en%C3%A1-data-%C4%8Dhm%C3%BA-) |
| Posiciones de trenes de Entur | `gaia.rail.read@v1` · `entur-rail-vyg-01` | `rail` · El mapa muestra posiciones observadas recientes de trenes de Vy en Noruega y su línea cuando está disponible. Cada posición caduca al vencer el plazo del proveedor o al cabo de cinco minutos, lo que ocurra primero. | [Entur · NLOD](https://developer.entur.no/terms-of-service) |
