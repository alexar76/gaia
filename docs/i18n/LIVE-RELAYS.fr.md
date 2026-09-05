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
| `gaia.window@v1` | `{ "device_id", "n" }` | Comme read |
| `gaia.fire.read@v1` | `{ "device_id"?, "west"?, "south"?, "east"?, "north"?, "limit"? }` | L’**acheteur peut** filtrer le CSV FIRMS par bbox / top-N — sans URL client |

Les capteurs fixes (météo, air, marée, Safecast, …) restent ancrés opérateur. Les feeds d’événements comme FIRMS sont globaux ; le bbox permet de demander « feux près de moi » sans inventer un hôte upstream.

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
| `om-aq-01` | Open-Meteo AQ | PM2.5/PM10/CO₂ ; sans clé |
| `sta-01` | OGC SensorThings | Optionnel ; peut timeout (`GAIA_STA_ENABLED`) |
| `openaq-01` | OpenAQ v3 | **Requiert** `GAIA_OPENAQ_API_KEY` |

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

## Non inclus comme SKU payant

| Source | Pourquoi |
|--------|----------|
| Global Fishing Watch | Non-commercial |
| Stanford RFI / related | **CC BY-NC** |
| ADSBx commercial API | NC / ToS payant |
| GPSJam heatmaps | Zone grise — éviter comme SKU payant |
| aisstream alone | Seulement comme feeder propre, pas dépendance commerciale unique |

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
