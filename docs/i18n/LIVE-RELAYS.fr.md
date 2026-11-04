# GAIA : relais live — guide opérateur

**Langues :** [EN](../LIVE-RELAYS.md) · [RU](LIVE-RELAYS.ru.md) · [ES](LIVE-RELAYS.es.md) · [FR](LIVE-RELAYS.fr.md) · [ZH](LIVE-RELAYS.zh.md)

**Developer :** [add-gaia-atlas-sensor](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md) (EN · RU · ES · FR · ZH)

**Cas d’usage opérateur ATLAS :** [`OPERATOR-USE-CASES.fr.md`](https://github.com/alexar76/atlas/blob/main/docs/i18n/OPERATOR-USE-CASES.fr.md)

## Principe

Un appareil live **ne possède pas** le capteur. La clé Ed25519 atteste :

> la passerelle a fidèlement relayé la réponse de l’API publique *X* au moment du fetch

Même chemin d’attestation et Pay-on-Verified. Le `source` est visible via `gaia.fleet.status@v1`.

## Sécurité

| Contrôle | Comportement |
|----------|--------------|
| Allowlist d’hôtes | Uniquement HTTPS dans `_ALLOWED_HOSTS` (`live.py`) |
| Pas d’URL client | Dans `input` : seulement `device_id` (+ filtres sûrs ci-dessous) |
| Sanitisation des IDs | Station / box / lat-lon / NOAA / OpenAQ validés avant construction d’URL |
| Pas d’URL à credentials | `user:pass@host` rejeté |
| Pas de redirects | `follow_redirects=False` ; non-200 → offline |
| Facturation | Échec upstream → offline → le Hub **ne** débite pas |

## Ce que l’acheteur envoie à l’invoke

| Capability | Buyer `input` | Qui choisit la géographie |
|------------|---------------|---------------------------|
| La plupart des `gaia.*.read@v1` | `{ "device_id": "…" }` | L’**opérateur** ancre le device (ou publie un mesh de villes — l’acheteur choisit `device_id`) |
| `gaia.air.read@v1` | `{ "device_id", "latitude"?, "longitude"? }` | Pin **opérateur** **ou** lat/lon acheteur sur `om-aq-*` / `sc-01` / `sc-{slug}` (Open-Meteo ; Sensor.Community area — vide → offline) |
| `gaia.window@v1` | `{ "device_id", "n" }` | Comme read |
| `gaia.fire.read@v1` | `{ "device_id"?, "west"?, "south"?, "east"?, "north"?, "limit"? }` | L’**acheteur peut** filtrer le CSV FIRMS par bbox / top-N — sans URL client |

La plupart des capteurs fixes restent ancrés opérateur. Exceptions avec `latitude`/`longitude` acheteur sur le même SKU : Open-Meteo AQ (`om-aq-*`) et Sensor.Community (`sc-*`). Les feeds d’événements comme FIRMS sont globaux ; le bbox permet de demander « feux près de moi » sans inventer un hôte upstream.

## Catalogue (`GAIA_ENABLE_LIVE=1`)

### Météo — `gaia.weather.read@v1`

| device_id | Upstream | Notes |
|-----------|----------|-------|
| `ws-01` / `ws-02` | simulateur | Toujours présent |
| `nws-01` | NOAA/NWS | Stations US ; domaine public ; User-Agent requis |
| `om-wx-01` | Open-Meteo | Global ; défaut Berlin (`GAIA_OM_LAT`/`LON`) ; CC BY 4.0 |

### Air — `gaia.air.read@v1`

| device_id | Upstream | Notes |
|-----------|----------|-------|
| `aq-01` | simulateur | Toujours présent |
| `osm-01` | openSenseMap | Citizen science ; licence par box |
| `om-aq-01` | Open-Meteo AQ | PM2.5/PM10/CO₂ ; sans clé ; l’acheteur peut passer `latitude`/`longitude` |
| `sc-01`, `sc-{slug}` | Sensor.Community | Requête d’aire SDS011 crowd (ODbL — citer). Berlin + mesh villes (`GAIA_SC_MESH_ENABLED`). Lat/lon acheteur. Zone vide → offline. Crowd ≠ station de référence ; mesh ≠ chaque nœud sur Terre |
| `sta-01` | OGC SensorThings | Optionnel ; peut timeout (`GAIA_STA_ENABLED`) |
| `openaq-01` | OpenAQ v3 | **Requiert** une clé gratuite depuis [explore.openaq.org](https://explore.openaq.org) → `GAIA_OPENAQ_API_KEY` |

### Réseau — `gaia.grid.read@v1` (LIVE uniquement)

| device_id | Upstream | Champ |
|-----------|----------|-------|
| `uk-grid-01` | carbonintensity.org.uk | `carbon_intensity_gco2_kwh` (actual, sinon forecast) |

### Séismes — `gaia.quake.read@v1` (LIVE uniquement)

| device_id | Upstream | Champs |
|-----------|----------|--------|
| `usgs-quake-01` | USGS GeoJSON M≥2.5/jour | `magnitude`, `depth_km`, `latitude`, `longitude` |

### Marée — `gaia.tide.read@v1` (LIVE uniquement)

| device_id | Upstream | Champ |
|-----------|----------|-------|
| `noaa-tide-01` | NOAA CO-OPS | `water_level_m` (MLLW, métrique) ; défaut 8518750 |

### Rivière — `gaia.river.read@v1` (LIVE uniquement)

| device_id | Upstream | Champs |
|-----------|----------|--------|
| `usgs-river-01` | USGS NWIS | `discharge_m3s`, `gage_height_m` ; défaut `01646500` Potomac |

### Marin — `gaia.marine.read@v1` (LIVE uniquement)

| device_id | Upstream | Champs |
|-----------|----------|--------|
| `ndbc-01` | NOAA NDBC buoy | `wave_height_m`, `sst_c`, `wind_mps` (si présent) ; défaut `44025` |
| `om-marine-01` | Open-Meteo Marine | `wave_height_m`, `sst_c` ; défaut port NYC |

### Feu — `gaia.fire.read@v1` (LIVE · librement commercialisable)

| device_id | Upstream | Champs | Licence |
|-----------|----------|--------|---------|
| `firms-fire-01` | NASA FIRMS VIIRS CSV (opt. `GAIA_FIRMS_MAP_KEY`) | Attested : `brightness_k` le plus élevé, `confidence`, lat/lon. Payload carte : `hotspots[]` (top-N, défaut `GAIA_FIRMS_HOTSPOT_LIMIT=500`, max 5000) + `hotspot_count`. Opt. buyer `west/south/east/north` + `limit`. | Open data NASA — **citer NASA FIRMS** + disclaimer |

La couche Wildfire ATLAS déplie `hotspots[]` en un pin par détection (`firms-hs-NNNN`).

### Radiation — `gaia.radiation.read@v1` (LIVE · librement commercialisable)

| device_id | Upstream | Champs | Licence |
|-----------|----------|--------|---------|
| `safecast-01` | Safecast measurements API | `cpm`, `latitude`, `longitude` | **CC0** |

Hub `safecast-01` garde une fenêtre de 30 jours. Les ancres carte `safecast-melbourne` / `safecast-adelaide` sont en mode archive (`max_age_days: 0`) — sinon la grille 2014 du sud de l’Australie disparaît. Les pins portent `captured_at` — ce n’est pas « maintenant ».

### Brouillage GNSS — `gaia.jamming.read@v1` (LIVE · librement commercialisable)

| device_id | Upstream | Champs | Licence |
|-----------|----------|--------|---------|
| `cybernews-jam-01` | cybernews.space `/api/data/gnss` | `interference_score`, `radius_km`, lat/lon | **CC BY 4.0** — attribution requise |

### Trafic edge — `gaia.adsb.read@v1` / `gaia.ais.read@v1` (feeder opt-in)

| device_id | Upstream | Notes |
|-----------|----------|-------|
| `feeder-adsb-01` | dump1090 propre → `POST /feeder/v1/ingest` | `GAIA_FEEDER_ENABLED=1` + `GAIA_FEEDER_TOKEN`. Offline jusqu’au premier ingest. **Pas** ADSBx / agrégateurs NC. |
| `feeder-ais-01` | Récepteur AIS propre | Même chemin. **Pas** aisstream comme seul SKU payant. |

### P0 — air crowd (Sensor.Community)

| device_id | SKU | Notes |
|-----------|-----|-------|
| `sc-01`, `sc-{slug}` | `gaia.air.read@v1` | Sensor.Community **ODbL** — citer. Lat/lon acheteur sur le SKU ; mesh villes (`GAIA_SC_MESH_ENABLED`). Zone vide → offline. Crowd ≠ station de référence ; mesh ≠ chaque nœud terrestre |

### P2 — relais publics à licence épinglée

L'AIS edge opérateur (`gaia.ais.read@v1`) reste le récepteur. L'AIS public est un **autre** SKU.

| device_id | SKU | Upstream | Licence |
|-----------|-----|----------|---------|
| `fintraffic-ais-01` | `gaia.ais.public.read@v1` | Fintraffic Digitraffic AIS (`meri.digitraffic.fi`) | **CC BY 4.0** — crédit Fintraffic. Eaux finlandaises seulement. **Pas** GFW, AISStream ni AIS edge |
| `eccc-hydro-01` | `gaia.river.read@v1` | ECCC MSC GeoMet (défaut `02HC003` Humber) | End-use Licence — commercial + attribution ECCC. La cote peut être géodésique |
| `fmi-01` | `gaia.weather.read@v1` | FMI open WFS (Helsinki) | **CC BY 4.0** — Institut météorologique finlandais |
| `nws-tsunami-01` | `gaia.tsunami.read@v1` | NWS CAP tsunami warning/watch/advisory | PD USA. Produit d'alerte, pas un marégraphe. Flux vide → offline / pas de débit |
| `smhi-hydro-01` | `gaia.river.read@v1` | SMHI hydroobs (station 2357 Abisko) | **CC BY 4.0** — SMHI. Pas une prévision de crue |

Kystverket AIS, EMSC, cyclones NHC, crues EA (Angleterre), Atom PTWC et ADSB.lol vivent en **P3**.

| device_id | SKU | Notes |
|-----------|-----|-------|
| `nhc-cyclone-01` | `gaia.cyclone.read@v1` | NHC PD — AL/EP/CP seulement |
| `emsc-01` | `gaia.quake.read@v1` | EMSC CC BY 4.0 — citer EMSC |
| `ea-flood-01` | `gaia.flood.read@v1` | EA OGL, Angleterre seulement |
| `ptwc-01` | `gaia.tsunami.read@v1` | PTWC Atom — pas un marégraphe |
| `kystverket-ais-01` | `gaia.ais.public.read@v1` | BarentsWatch NLOD — jeton |
| `adsb-lol-01` | `gaia.adsb.public.read@v1` | ADSB.lol ODbL 1.0 |
| `frost-*-01` (6) | `gaia.weather.read@v1` | MET Norway Frost (CC BY 4.0 + NLOD) — in-situ, pas METAR; clé `GAIA_FROST_CLIENT_ID` |
| `dmi-*-01` (5) | `gaia.weather.read@v1` | DMI metObs CC BY 4.0 — Danemark, sans clé |
| `sepa-*-01` | `gaia.river.read@v1` | SEPA KiWIS OGL — Écosse uniquement |
| `nrw-*-01` | `gaia.river.read@v1` | NRW River Levels OGL — Pays de Galles; clé `GAIA_NRW_API_KEY` |
| `ingv-01` | `gaia.quake.read@v1` | INGV FDSN CC BY 4.0 — citer INGV; Italie/régional, pas un remplacement USGS/EMSC |
| `cdip-*-01` | `gaia.marine.read@v1` | CDIP ERDDAP — créditer CDIP/Scripps/USACE + [cdip.ucsd.edu](https://cdip.ucsd.edu/); vagues in-situ |
| `icos-*-01` | `gaia.ghg.read@v1` **nouveau** | ICOS ATC NRT CO₂ CC BY 4.0 — citer ICOS; clé `GAIA_ICOS_CPAUTH_TOKEN` |
| `hubeau-*-01` (20 FR) | `gaia.river.read@v1` | Hub'Eau hydrométrie (Etalab OL 2.0) — France seulement ; pas Vigicrues |
| `ehyd-*-01` (24 AT) | `gaia.river.read@v1` | eHYD / BMLUK `pegel_aktuell` CC BY 4.0 — « Datenquelle: ehyd.gv.at » ; Autriche seulement |
| `smhi-wx-*-01` (15 SE) | `gaia.weather.read@v1` | SMHI MetObs CC BY 4.0 — météo in-situ ; **pas** `smhi-hydro-01` |
| `sg-wx-*-01` (12 SG) | `gaia.weather.read@v1` | Singapore NEA / data.gov.sg — Singapore Open Data Licence ; vent (+ T/RH/pluie) |
| `sg-psi-*-01` (5 régions) | `gaia.air.read@v1` | Singapore NEA PSI / PM2.5 — Open Data Licence ; indice régional 24h |
| `hko-wx-*-01` (15 HK) | `gaia.weather.read@v1` | HKO `rhrread` — DATA.GOV.HK, attribution HKSAR / HKO |
| `be-aq-*-01` (12 BE) | `gaia.air.read@v1` | IRCELINE SOS PM2.5 CC BY 4.0 — citer IRCELINE ; Belgique seulement |
| `aemet-wx-*-01` (15 ES) | `gaia.weather.read@v1` | AEMET OpenData — réutilisation PSI, « Fuente: AEMET » ; clé `GAIA_AEMET_API_KEY` |
| `ch-wx-*-01` (15 CH) | `gaia.weather.read@v1` | MeteoSwiss OGD CC BY 4.0 — in situ ; GeoJSON LV95 → WGS84 |
| `bafu-*-01` (15 CH) | `gaia.river.read@v1` | BAFU/FOEN LINDAS OGD-CH Open-Use — Q + niveau géodésique ; pas une alerte |
| `cwa-wx-*-01` (12 TW) | `gaia.weather.read@v1` | CWA OpenData OGDL 1.0 ; clé `GAIA_CWA_API_KEY` |
| `mf-wx-*-01` (12 FR) | `gaia.weather.read@v1` | Météo-France DPObs Etalab OL 2.0 ; clé `GAIA_METEOFRANCE_APPLICATION_ID` |
| `ee-wx-*-01` (12 EE) | `gaia.weather.read@v1` | Estonian Weather Service XML CC BY 4.0 |
| `is-wx-*-01` (12 IS) | `gaia.weather.read@v1` | IMO AWS horaire ODC-By |
| `hk-aqhi-*-01` (18 HK) | `gaia.air.read@v1` | EPD AQHI DATA.GOV.HK — indice 1–10, **pas** PM2.5 |

Densify (mêmes SKU, plus de pins) : EA 7→20, PEGELONLINE 10→22, ECCC 1→8, FMI 1→5, Frost 2→6, DMI 2→5, AURN Londres 1→5, UHSLC FD 1→5.

## P4 — réseaux complets et couches par coordonnée

Un point ATLAS correspond à une coordonnée de lecture. Les réseaux publient toutes les stations officielles ; les sources en grille acceptent tout `latitude`/`longitude` acheteur et renvoient la coordonnée de cellule source/requête.

| Couche / device_id | Couverture | Base commerciale |
|---|---|---|
| Fumée `hms-smoke-01` | Chaque polygone HMS avec son anneau complet et ses trous, `polygon_id` stable, digest de géométrie et bbox ; le centroïde n’est que l’ancre carto | Domaine public USA ; citer NOAA/NESDIS HMS |
| Qualité de l’eau `usgs-wq-01` | Pagine les observations latest-continuous fraîches (48 h par défaut, `max_age_hours`) et joint les sites à USGS monitoring-locations ; filtre/`require_all` ; une ligne signée par coordonnée ; heure, Approved/Provisional et qualifiers ; données stale/incomplètes refusées | Domaine public USA ; citer USGS |
| DART `noaa-dart-01`, `dart-*` | Les 43 stations actives du répertoire NDBC figé | Domaine public USA ; citer NOAA/NDBC |
| Précipitations `imerg-01` | Toute coordonnée ; centre exact de cellule IMERG ; préliminaire | Données ouvertes NASA ; citer NASA GPM |
| État radar `nexrad-status-01` | Tous les WSR-88D à leurs coordonnées ; pas réflectivité | Domaine public USA ; citer NOAA/NWS |
| Atmosphère `cams-*` | Toute coordonnée CAMS | CC BY 4.0 ; Open-Meteo commercial/self-hosted |
| EPA RadNet `radnet-*` | Les 140 coordonnées officielles des moniteurs | Données du gouvernement USA ; citer EPA RadNet |
| Sol `soil-*` | Toute coordonnée CLMS SWI020 | Copernicus : tout usage avec attribution |
| Solaire `solar-*` | Toute coordonnée NASA POWER | Données ouvertes NASA ; citer NASA POWER |
| Neige `snow-*` | Toute coordonnée CONUS ; cellule SNODAS exacte | Domaine public USA ; citer NOAA/NOHRSC |
| Glace de mer `nsidc-ice-01` | Toute coordonnée arctique ; cellule exacte de 25 km | Données gouvernement USA ; citation requise ; pas navigation |
| Température terrestre `lst-*` | Toute coordonnée Sentinel-3 SLSTR | Copernicus free/full/open avec attribution des modifications |

Les registres RadNet et DART sont actualisés ensemble par `python3 scripts/update_p4_networks.py`, qui écrit les copies déployables GAIA et ATLAS.

## Points d’inondation possible et eaux souterraines de l’USGS

- **Points d’inondation possible** (`usgs-rtfi-01`, `gaia.flood.read@v1`) s’affichent dans la couche Inondations d’ATLAS. GAIA compare le seuil relevé sur le terrain à une mesure issue de la même série de la station hydrométrique et datant de moins de deux heures. La fiche indique l’heure de mesure, le niveau, le seuil et la hauteur de dépassement. Ce résultat provisoire signale une inondation possible. Toute fermeture de route exige une confirmation indépendante. Une réponse vide ne donne lieu à aucune observation facturable. [API USGS RT-FI](https://api.waterdata.usgs.gov/rtfi-api/docs)
- **Eaux souterraines** (`usgs-groundwater-01`, `gaia.groundwater.read@v1`) couvre trois puits pilotes de l’USGS dans le Maine, l’État de New York et le Kentucky. La profondeur de l’eau s’exprime en mètres par rapport à la surface locale du sol, avec des valeurs positives vers le bas. L’heure de mesure, le statut de validation et l’indicateur de qualité fourni par la source sont conservés. Seules les mesures des dernières 24 heures sont retenues. [USGS Water Data API](https://api.waterdata.usgs.gov/)

Les données de l’USGS relèvent du domaine public aux États-Unis. Mentionnez l’USGS comme source. Les deux relais sont activés par défaut avec `GAIA_ENABLE_LIVE=1`. Pour les désactiver, définissez respectivement `GAIA_USGS_RTFI_ENABLED=0` ou `GAIA_USGS_GROUNDWATER_ENABLED=0`. [Conditions de réutilisation de l’USGS](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits)

## P13 — prix de l'électricité du lendemain et imagerie Sentinel-2

| device_id | SKU | Source | Licence / périmètre |
|-----------|-----|--------|---------------------|
| `smard-price-<zone>` (15 zones : `de-lu`, `at`, `be`, `no2`, `dk1`, `dk2`, `fr`, `it-north`, `nl`, `pl`, `se4`, `ch`, `si`, `cz`, `hu`) | `gaia.power.price.read@v1`, `gaia.power.window@v1` | Séries du lendemain de SMARD.de (Bundesnetzagentur), blocs hebdomadaires au quart d'heure | **CC BY 4.0** — citer Bundesnetzagentur / SMARD.de. Prix de gros de bourse, pas un tarif. Séries associées aux zones par les valeurs. `GAIA_SMARD_ZONES` limite les zones, `GAIA_SMARD_ENABLED=0` les désactive. [Guide](POWER-PRICES.fr.md) |
| `s2-l2a-01` (requête par point) | `gaia.imagery.read@v1` | STAC Element84 Earth Search + AWS Open Data `sentinel-cogs` | **Données Copernicus Sentinel** — libres, complètes et ouvertes, usage commercial compris ; mention « Contains modified Copernicus Sentinel data <année> ». Un pixel de la scène dégagée la plus récente. `GAIA_S2_ENABLED=0` le désactive. [Guide](IMAGERY.fr.md) |

Ce ne sont pas des relais, mais ils sont sortis dans la même vague : `gaia.place.resolve@v1`, gratuite ([répertoire GeoNames](PLACES.fr.md)), et le pilotage d'appareils payé à l'effet vérifié `gaia.actuate@v1` ([guide](ACTUATION.fr.md)).

## Non inclus comme SKU payant

| Source | Pourquoi |
|--------|----------|
| Global Fishing Watch | Non-commercial |
| Stanford RFI / related | **CC BY-NC** |
| ADSBx commercial API | NC / ToS payant |
| GPSJam heatmaps | Zone grise — éviter comme SKU payant |
| aisstream alone | Seulement comme feeder propre, pas dépendance commerciale unique |
| ENTSO-E Transparency | Libre de consulter (EU 543/2013), mais ToS sans redistributabilité commerciale claire pour SKU payant |

## Carte / watchbox / composite → ATLAS

Les appareils ci-dessus sont des **SKU de flotte GAIA**. Couches, pins, **watchboxes** et
SKU composites Hub (`atlas.situation.brief@v1`, `atlas.fire.weather@v1`,
`atlas.nearest.read@v1`, `atlas.watchbox.check@v1`) sont sur **ATLAS** :
[GUIDE](https://github.com/alexar76/atlas/blob/main/docs/GUIDE.md) (EN · RU · ES · FR · ZH).
Pas de surface produit séparée côté GAIA.

**Watchbox** (résumé) : bbox enregistré + filtre de couches ; l’agent/opérateur fait un
**check** périodique et reçoit les matches LIVE dans le cadre + content receipt.
Abonnement = REST ; check = SKU facturable Hub. Détail dans ATLAS GUIDE.

## Hub

Après **redeploy GAIA** + crawl de fédération `iot.modelmarket.dev`, les SKUs de lecture
apparaissent au catalogue. Textes UI : `aimarket-hub/cap-descriptions-i18n.json`
(EN · RU · ES · FR · ZH). Composites ATLAS après crawl du peer `atlas.modelmarket.dev`.

## Sources de données supplémentaires

| Couche ou source | GAIA | Ce que montre la carte | Source et conditions |
|---|---|---|---|
| Nappes de Hub’Eau | `gaia.groundwater.read@v1` · `hubeau-groundwater-01` | Huit piézomètres pilotes français indiquent la profondeur sous le repère de mesure et l’altitude de l’eau en NGF. La fiche indique l’heure du relevé et le repère de chaque valeur. | [Etalab Licence Ouverte](https://assistance.brgm.fr/fr/aide/HubEau) |
| Prévisions fluviales NOAA NWPS | `gaia.river.forecast.read@v1` · `nwps-pttp1`, `nwps-rmdv2`, `nwps-vckm6`, `nwps-dlrt2` | La fiche indique le niveau maximal parmi les points disponibles de la prévision des prochaines 72 heures, l’heure d’émission et celle du pic. Les hauteurs se rapportent au repère local de la station. | [NOAA/NWS · U.S. public domain](https://water.noaa.gov/about/api) |
| Marégraphes de l’Environment Agency | `gaia.tide.read@v1` · `ea-tide-e70039`, `ea-tide-e72639`, `ea-tide-e70539`, `ea-tide-e70739`, `ea-tide-e73839` | Les stations côtières du Royaume-Uni mesurent le niveau moyen de la mer en mètres au-dessus du repère Ordnance Datum Newlyn (mAOD). | [Environment Agency · OGL v3](https://environment.data.gov.uk/flood-monitoring/doc/tidegauge) |
| Événements routiers Fintraffic | `gaia.road.events.read@v1` · `fintraffic-road-announcements-01`, `fintraffic-roadworks-01` | Les avis et travaux en Finlande indiquent les tronçons concernés et la période de validité publiée. | [Fintraffic Digitraffic · CC BY 4.0](https://www.digitraffic.fi/en/terms-of-service/) |
| NOAA G-AIRMET | `gaia.aviation.hazards.read@v1` · `awc-gairmet-01` | Les polygones et lignes des prévisions aéronautiques américaines indiquent le danger et les heures d’émission et de validité. | [NOAA/NWS Aviation Weather Center · U.S. public domain](https://aviationweather.gov/data/api/) |
| Alertes et fermetures routières NDW | `gaia.road.events.read@v1` · `ndw-road-srti-01`, `ndw-road-closures-01` | La couche `road_events` affiche les alertes de sécurité routière et les fermetures temporaires aux Pays-Bas avec la géométrie des tronçons concernés. Chaque publication signée expire cinq minutes après sa diffusion. Chaque mesure de circulation a sa propre période de validité. | [NDW · CC0 1.0](https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/) |
| Bouées de houle du FMI en mer Baltique | `gaia.marine.read@v1` · `fmi-marine-103976`, `fmi-marine-108126`, `fmi-marine-108499`, `fmi-marine-134220`, `fmi-marine-134221`, `fmi-marine-134246` | Six bouées de la couche `marine` fournissent la hauteur significative des vagues. La fiche indique aussi la période de pic, la direction d’où viennent les vagues et la température de l’eau lorsque ces valeurs ont été mesurées au même moment. L’horodatage marque le début de la mesure des vagues. Les observations expirent au bout de deux heures. | [FMI · CC BY 4.0](https://en.ilmatieteenlaitos.fi/download-observations-questions) |
| Enneigement USDA NRCS SNOTEL | `gaia.snow.read@v1` · `snotel-335`, `snotel-415` | La couche `snow` affiche les mesures horaires de l’épaisseur de neige et de son équivalent en eau en centimètres à Berthoud Summit et Copper Mountain, dans le Colorado. GAIA associe les mesures de la même heure qui ont passé le contrôle automatique de qualité et datent de six heures au plus. | [USDA NRCS · domaine public américain](https://catalog.data.gov/dataset/snowpack-telemetry-network-snotel) |
| Observations marines de SMHI | `gaia.ocean.observations.read@v1` · `smhi-ocean-flinten16-8m`, `smhi-ocean-flinten16-2m`, `smhi-ocean-flinten7-8m`, `smhi-ocean-kalmar2-0m` | `ocean_obs` · Quatre séries à des profondeurs définies dans trois stations suédoises. La fiche affiche la salinité en PSU, l’oxygène dissous en mL/L ou la vitesse et la direction du courant selon la station et la profondeur. Les mesures datent de la même heure et restent valables trois heures. | [SMHI · CC BY 4.0](https://www.smhi.se/data/om-smhis-data/villkor-for-anvandning) |
| Prévisions de fumée d’ECCC | `gaia.smoke.forecast.read@v1` · `eccc-smoke-seattle`, `eccc-smoke-toronto`, `eccc-smoke-calgary` | `smoke_forecast` · Trois mailles du modèle près de Seattle, Toronto et Calgary. La fiche affiche la prévision des PM₂.₅ totales près du sol dans le panache de fumée modélisé, en µg/m³, pour la prochaine heure pleine UTC. Elle indique aussi l’heure du cycle du modèle et l’heure de validité de la prévision. Le cycle peut dater de 18 heures au maximum. | [ECCC · MSC End-use Licence](https://eccc-msc.github.io/open-data/licence/readme_en/) |
| NOAA SWPC D-RAP | `gaia.hf.absorption.read@v1` · `swpc-drap-01` | `hf_absorption` · La grille mondiale du modèle indique la fréquence maximale affectée par une absorption de 1 dB dans la région D. La vue globale affiche les maxima en MHz dans 900 mailles de 6° × 12°. En zoomant, on accède aux échantillons d’origine des 8 100 mailles de 2° × 4°. La grille expire au bout de 15 minutes. | [NOAA SWPC](https://www.swpc.noaa.gov/products/d-region-absorption-predictions-d-rap) |
| Débit de dose gamma ambiant du BfS | `gaia.radiation.ambient.read@v1` · `bfs-odl-01` | `radiation` · Les stations allemandes affichent les débits horaires validés d’équivalent de dose gamma ambiant en nSv/h. La fiche indique la fin de l’heure d’observation. Les mesures expirent au bout de deux heures. | [BfS · DL-DE-BY 2.0](https://www.imis.bfs.de/geoportal/resources/sitepolicy.html) |
| Hauteur d’eau côtière de GeoNet | `gaia.tide.read@v1` · `geonet-coastal-auct`, `geonet-coastal-wlgt`, `geonet-coastal-taut`, `geonet-coastal-gist`, `geonet-coastal-jbtt` | `tide` · Cinq stations néo-zélandaises affichent la hauteur d’eau en mètres au-dessus du capteur de pression local. Les mesures expirent au bout de cinq minutes. | [GeoNet · CC BY 3.0 NZ](https://www.geonet.org.nz/policy) |
| Observations fluviales de ČHMÚ | `gaia.river.read@v1` · `chmi-river-042000`, `chmi-river-101800`, `chmi-river-200100` | `river` · Trois stations hydrométriques tchèques affichent la hauteur et le débit mesurés à titre provisoire, ainsi que la température de l’eau lorsqu’elle est relevée au même instant. Les mesures expirent au bout de deux heures. | [ČHMÚ · CC BY 4.0](https://www.chmi.cz/-/kde-najdu-otev%C5%99en%C3%A1-data-%C4%8Dhm%C3%BA-) |
| Positions ferroviaires d’Entur | `gaia.rail.read@v1` · `entur-rail-vyg-01` | `rail` · La carte affiche les dernières positions observées des trains Vy en Norvège et leur ligne, si disponible. Les positions expirent à la fin de la validité fixée par le fournisseur ou au bout de cinq minutes, selon la première échéance. | [Entur · NLOD](https://developer.entur.no/terms-of-service) |
| Coupures de courant UK Power Networks | `gaia.power.outages.read@v1` · `ukpn-live-faults-01` | `power_outages` · Pannes en cours dans la zone desservie par l’opérateur. La fiche indique la zone touchée approximative, le nombre de clients affectés et l’heure estimée de rétablissement lorsque l’opérateur la publie. L’instantané expire au bout de 30 minutes. | [UK Power Networks · CC BY 4.0](https://ukpowernetworks.opendatasoft.com/api/explore/v2.1/catalog/datasets/ukpn-live-faults) |
| Tirant d’air sous les ponts de NOAA PORTS | `gaia.bridge.clearance.read@v1` · `noaa-airgap-8726371`, `noaa-airgap-8545556`, `noaa-airgap-8575432` | `bridge_clearance` · Trois capteurs aux États-Unis indiquent la distance verticale préliminaire, en mètres, entre la surface de l’eau et un point de référence fixe du pont. La fiche affiche l’écart-type de l’échantillon et les indicateurs de la source. Les observations expirent au bout de 18 minutes. Pour décider d’un passage, consultez NOAA PORTS et les cartes marines officielles. | [NOAA NOS CO-OPS](https://opendap.co-ops.nos.noaa.gov/erddap/info/IOOS_Air_Gap/index.html) |
| Bouées dérivantes de NOAA AOML | `gaia.ocean.drifters.read@v1` · `aoml-drifters-01` | `ocean_drifters` · Les relevés mondiaux indiquent la dernière position observée et la température de surface de la mer de chaque bouée, ainsi que la pression au niveau de la mer lorsqu’elle est transmise. Les observations expirent au bout de six heures. | [NOAA AOML / OSMC · CC0 1.0](https://osmc.noaa.gov/erddap/info/OSMC_flattened/index.json) |
| Bike Share Toronto | `gaia.bikeshare.read@v1` · `toronto-bikeshare-01` | `bikeshare` · Les fiches indiquent les vélos disponibles, les vélos électriques lorsqu’ils sont renseignés, les bornes libres et le statut de la location et du retour. Les relevés expirent au bout de cinq minutes. | [City of Toronto · OGL](https://open.toronto.ca/open-data-licence/) |
| Restrictions routières de Toronto | `gaia.road.events.read@v1` · `toronto-road-restrictions-01` | `road_events` · Les avis municipaux indiquent le point ou le tronçon concerné pendant une restriction active, y compris les restrictions récurrentes planifiées. L’instantané de la source expire au bout de 15 minutes. Chaque avis a sa propre période d’activité. | [City of Toronto · OGL](https://open.toronto.ca/open-data-licence/) |
| Observations fluviales d’ARSO | `gaia.river.read@v1` · `arso-river-1060`, `arso-river-3660`, `arso-river-5078`, `arso-river-8080` | `river` · Quatre stations slovènes indiquent la hauteur provisoire en mètres au-dessus du zéro local et le débit en m³/s. La fiche inclut la température de l’eau lorsqu’elle est relevée au même moment. Les observations expirent au bout de deux heures. | [ARSO](https://kazalci.arso.gov.si/en/content/legal-note) |
| Niveaux côtiers de Kartverket | `gaia.tide.read@v1` · `kartverket-tide-bgo`, `kartverket-tide-osl`, `kartverket-tide-svg`, `kartverket-tide-tos`, `kartverket-tide-trd` | `tide` · Cinq marégraphes norvégiens indiquent le niveau côtier observé en mètres par rapport au référentiel vertical national NN2000. Les observations expirent au bout de deux heures. | [Kartverket · CC BY 4.0](https://vannstand.kartverket.no/tideapi_en.html) |

Les données sur les restrictions routières et les vélos en libre-service de Toronto portent cette mention. « Contains information licensed under the Open Government Licence – Toronto. » [Licence](https://open.toronto.ca/open-data-licence/)

### Recharge, phénomènes dangereux et observations de terrain

| Source | GAIA | ATLAS | Données et limites | Source et conditions |
| --- | --- | --- | --- | --- |
| Recharge NDW DOT-NL | `gaia.ev.charging.read@v1` · `ndw-ev-charging-01` | `ev_charging` | Les sites publics de recharge aux Pays-Bas indiquent le nombre de connecteurs disponibles et leur nombre total, par type et puissance. La requête par défaut couvre Utrecht. Les requêtes régionales couvrent jusqu’à 4 degrés carrés. Les pages du Hub comptent jusqu’à 500 sites et utilisent le jeton de l’instantané pour la suite. Un instantané expire au bout de quatre minutes. | [NDW · CC0 1.0](https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/) · [API GeoJSON](https://docs.ndw.nu/en/data-uitwisseling/interface-beschrijvingen/dafne-api/dafne_api_consumer_pull/) |
| Incendies et périmètres de BC Wildfire | `gaia.fire.perimeters.read@v1` · `bc-wildfire-01` | `wildfire_perimeters` | Les incendies actifs en Colombie-Britannique présentent leur état et le dernier périmètre publié lorsqu’il est disponible. L’heure de mise à jour du jeu et la date de chaque périmètre sont distinctes. L’instantané expire au bout d’une heure. | [BC Wildfire Service · OGL–BC 2.0](https://www2.gov.bc.ca/gov/content/data/policy-standards/data-policies/open-data/open-government-licence-bc) · [Périmètres](https://open.canada.ca/data/dataset/cdfc2d7b-c046-4bf0-90ac-4897232619e1) |
| Pollen mesuré de MétéoSuisse A7 | `gaia.pollen.read@v1` · `ch-pollen-*` | `pollen` | Quinze stations automatiques enregistrées mesurent les concentrations horaires de sept types de pollen en grains par mètre cube. Une valeur mesurée de zéro est conservée. Les observations expirent au bout de deux heures. | [Source: MeteoSwiss · CC BY 4.0](https://opendatadocs.meteoswiss.ch/general/terms-of-use) · [A7](https://opendatadocs.meteoswiss.ch/a-data-groundbased/a7-pollen-stations) |
| Réseau irlandais de bouées météorologiques | `gaia.marine.read@v1` · `ie-buoy-m2`, `ie-buoy-m3`, `ie-buoy-m5`, `ie-buoy-m6` | `marine` | Quatre bouées pilotes mesurent la hauteur, la période et la direction des vagues, avec la température de l’eau et le vent lorsqu’ils sont disponibles. Les observations expirent au bout de deux heures. Le code qualité 0 indique une qualité inconnue et le code 1 une bonne qualité. | [Marine Institute and Met Éireann · CC BY 4.0](https://erddap.marine.ie/erddap/info/IWBNetwork/index.html) |
| Bouées houlographiques australiennes AODN/IMOS | `gaia.marine.read@v1` · `au-wave-apollo-bay`, `au-wave-wilsons-prom`, `au-wave-wooli` | `marine` | Trois bouées pilotes indiquent la hauteur significative des vagues, leur période de pic et leur direction de provenance lorsque ces données sont disponibles. GAIA accepte le code qualité 1. Les observations expirent au bout de deux heures. | [AODN / IMOS · CC BY 4.0](https://registry.opendata.aws/aodn_wave_buoy_realtime_nonqc/) |
| Volcans et caméras GeoNet | `gaia.volcano.read@v1` · `geonet-volcano-01` | `volcano` | Douze volcans néo-zélandais affichent leur niveau d’alerte actuel et leur code couleur pour l’aviation. Le statut expire 30 minutes après sa récupération. Les miniatures disponibles portent leur propre heure de prise de vue et expirent au bout de 30 minutes. | [GeoNet / Earth Sciences New Zealand and sponsors · CC BY 3.0 NZ](https://www.geonet.org.nz/policy) · [Caméras](https://doi.org/10.21420/vq6s-w222) |
| Signalements locaux d’orages NOAA SPC | `gaia.storm.reports.read@v1` · `noaa-spc-storm-reports-01` | `storm_reports` | Les signalements préliminaires de tornades, de grêle et de vents violents aux États-Unis sont affichés sur une fenêtre glissante de 24 heures. Chaque point indique l’heure du phénomène signalée par la source. | [NOAA/NWS SPC · U.S. public domain](https://www.weather.gov/disclaimer) · [CSV](https://www.spc.noaa.gov/climo/reports/today.csv) |
| Avis de cendres volcaniques du VAAC de Washington | `gaia.volcanic.ash.read@v1` · `noaa-vaac-ash-01` | `volcanic_ash` | Les polygones publiés indiquent leurs limites d’altitude, l’heure d’émission et l’heure d’observation ou de validité de la prévision. L’avis expire à l’heure du prochain avis ou sept heures après son émission si cette échéance arrive plus tôt. | [NOAA Washington VAAC](https://ospo.noaa.gov/products/atmosphere/vaac/messages.html) · [Conditions de NOAA](https://www.noaa.gov/office-education/outreach-communication/faq) |
| Points chauds de Digital Earth Australia | `gaia.fire.hotspots.read@v1` · `dea-hotspots-01` | `fire_hotspots` | Les détections thermiques par satellite en Australie, dont celles de Himawari-9, sont affichées dans la zone demandée sur les trois dernières heures. La carte charge jusqu’à 800 détections et signale une couverture incomplète. | [Geoscience Australia · CC BY 4.0](https://knowledge.dea.ga.gov.au/data/product/dea-hotspots/) · [Jeu de données DEA Hotspots](https://pid.geoscience.gov.au/dataset/ga/111881) |

Les données de Colombie-Britannique portent la mention « Contains information licensed under the Open Government Licence – British Columbia. ». Les données des bouées australiennes portent la mention « Data was sourced from Australia’s Integrated Marine Observing System (IMOS) - IMOS is enabled by the National Collaborative Research Infrastructure Strategy (NCRIS) ».
