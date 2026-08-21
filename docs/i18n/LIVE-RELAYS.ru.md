# GAIA: живые ретрансляторы — руководство оператора

**Языки:** [EN](../LIVE-RELAYS.md) · [RU](LIVE-RELAYS.ru.md) · [ES](LIVE-RELAYS.es.md) · [FR](LIVE-RELAYS.fr.md) · [ZH](LIVE-RELAYS.zh.md)

**Developer:** [add-gaia-atlas-sensor](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md) (EN · RU · ES · FR · ZH)

**Сценарии оператора ATLAS:** [`OPERATOR-USE-CASES.ru.md`](https://github.com/alexar76/atlas/blob/main/docs/i18n/OPERATOR-USE-CASES.ru.md)

## Суть

Живое устройство **не владеет** датчиком. Ключ Ed25519 удостоверяет:

> шлюз честно ретранслировал ответ публичного API *X* в момент запроса

Поля проходят ту же аттестацию и Pay-on-Verified. Источник (`source`) виден в `gaia.fleet.status@v1`.

## Безопасность

| Контроль | Поведение |
|----------|-----------|
| Allowlist хостов | Только HTTPS из `_ALLOWED_HOSTS` (`live.py`) |
| Без клиентских URL | В `input` только `device_id` (+ безопасные фильтры ниже) |
| Санитизация ID | Station / box / lat-lon / NOAA / OpenAQ id проверяются до сборки URL |
| Без credential URL | `user:pass@host` отклоняется |
| Без редиректов | `follow_redirects=False`; не-200 → offline |
| Биллинг | Upstream fail → offline → Hub **не** списывает |

## Что передаёт покупатель на invoke

| Capability | Buyer `input` | Кто задаёт географию |
|------------|---------------|----------------------|
| Большинство `gaia.*.read@v1` | `{ "device_id": "…" }` | **Оператор** якорит устройство (или mesh городов — покупатель выбирает `device_id`) |
| `gaia.window@v1` | `{ "device_id", "n" }` | Как у read |
| `gaia.fire.read@v1` | `{ "device_id"?, "west"?, "south"?, "east"?, "north"?, "limit"? }` | **Покупатель может** отфильтровать FIRMS CSV по bbox / top-N — без клиентских URL |

Фиксированные сенсоры (погода, воздух, прилив, Safecast, …) остаются оператор-якорями. Event-фиды вроде FIRMS глобальны; bbox — способ спросить «пожары рядом», не открывая новый upstream.

## Каталог (`GAIA_ENABLE_LIVE=1`)

### Погода — `gaia.weather.read@v1`

| device_id | Upstream | Заметки |
|-----------|----------|---------|
| `ws-01` / `ws-02` | симулятор | Всегда есть |
| `nws-01` | NOAA/NWS | US станции; public domain; нужен User-Agent |
| `om-wx-01` | Open-Meteo | Глобально; дефолт Berlin (`GAIA_OM_LAT`/`LON`); CC BY 4.0 |

### Воздух — `gaia.air.read@v1`

| device_id | Upstream | Заметки |
|-----------|----------|---------|
| `aq-01` | симулятор | Всегда есть |
| `osm-01` | openSenseMap | Citizen science; лицензия per box |
| `om-aq-01` | Open-Meteo AQ | PM2.5/PM10/CO₂; без ключа |
| `sta-01` | OGC SensorThings | Опционально; может таймаутить (`GAIA_STA_ENABLED`) |
| `openaq-01` | OpenAQ v3 | **Нужен** `GAIA_OPENAQ_API_KEY` |

### Сеть — `gaia.grid.read@v1` (только LIVE)

| device_id | Upstream | Поле |
|-----------|----------|------|
| `uk-grid-01` | carbonintensity.org.uk | `carbon_intensity_gco2_kwh` (actual, иначе forecast) |

### Сейсмика — `gaia.quake.read@v1` (только LIVE)

| device_id | Upstream | Поля |
|-----------|----------|------|
| `usgs-quake-01` | USGS GeoJSON M≥2.5/день | `magnitude`, `depth_km`, `latitude`, `longitude` |

### Прилив — `gaia.tide.read@v1` (только LIVE)

| device_id | Upstream | Поле |
|-----------|----------|------|
| `noaa-tide-01` | NOAA CO-OPS | `water_level_m` (MLLW, metric); дефолт 8518750 |

### Река — `gaia.river.read@v1` (только LIVE)

| device_id | Upstream | Поля |
|-----------|----------|------|
| `usgs-river-01` | USGS NWIS | `discharge_m3s`, `gage_height_m`; дефолт `01646500` Potomac |

### Море — `gaia.marine.read@v1` (только LIVE)

| device_id | Upstream | Поля |
|-----------|----------|------|
| `ndbc-01` | NOAA NDBC buoy | `wave_height_m`, `sst_c`, `wind_mps` (если есть); дефолт `44025` |
| `om-marine-01` | Open-Meteo Marine | `wave_height_m`, `sst_c`; дефолт NYC harbor |

### Пожар — `gaia.fire.read@v1` (LIVE · свободно коммерциализируемо)

| device_id | Upstream | Поля | Лицензия |
|-----------|----------|------|----------|
| `firms-fire-01` | NASA FIRMS VIIRS CSV (опц. `GAIA_FIRMS_MAP_KEY`) | Attested: ярчайший `brightness_k`, `confidence`, lat/lon. Пакеты: `hotspots[]` (стр. ≤500) + `hotspot_total` / `next_cursor` (докачка идемпотентна). Collect до 50000 (`GAIA_FIRMS_COLLECT_MAX`). Опц. buyer bbox. | Open data NASA — **цитировать NASA FIRMS** + disclaimer |

Слой Wildfire на ATLAS разворачивает `hotspots[]` в пин на детекцию (`firms-hs-NNNN`).

### Радиация — `gaia.radiation.read@v1` (LIVE · свободно коммерциализируемо)

| device_id | Upstream | Поля | Лицензия |
|-----------|----------|------|----------|
| `safecast-01` | Safecast measurements API | `cpm`, `latitude`, `longitude` | **CC0** |

Hub `safecast-01` — окно 30 дней. Якоря карты `safecast-melbourne` / `safecast-adelaide` — архив (`max_age_days: 0`), иначе 2014 drive-grid юга Австралии пропадает. На пинах `captured_at` — это не «сейчас».

### GNSS-глушение — `gaia.jamming.read@v1` (LIVE · свободно коммерциализируемо)

| device_id | Upstream | Поля | Лицензия |
|-----------|----------|------|----------|
| `cybernews-jam-01` | cybernews.space `/api/data/gnss` | `interference_score`, `radius_km`, lat/lon | **CC BY 4.0** — нужна атрибуция |

### Edge-трафик — `gaia.adsb.read@v1` / `gaia.ais.read@v1` (opt-in feeder)

| device_id | Upstream | Заметки |
|-----------|----------|---------|
| `feeder-adsb-01` | Свой dump1090 → `POST /feeder/v1/ingest` | `GAIA_FEEDER_ENABLED=1` + `GAIA_FEEDER_TOKEN`. Offline до первого ingest. **Не** ADSBx / сторонние NC. |
| `feeder-ais-01` | Свой AIS-приёмник | Тот же ingest. **Не** aisstream как единственный платный SKU. |
| `feeder-iot-01` | Свой IoT / Tasmota / TTN / SenML | Тот же ingest (`T/RH/P/PM2.5`). |

## P0 / P1 (коммерчески чистые)

Полные таблицы лицензий — в [английской версии](../LIVE-RELAYS.md). Кратко:

| device_id | SKU | Лицензия |
|-----------|-----|----------|
| `eonet-01` | `gaia.events.read@v1` | NASA open data — цитировать EONET |
| `swpc-01` | `gaia.spacewx.read@v1` | NOAA SWPC, public domain США |
| `glm-01` | `gaia.lightning.read@v1` | GOES-19/18 GLM NODD, PD США (не Blitzortung; G16 East с 2025 не пишет LCFA) |
| `nws-alerts-01` | `gaia.alerts.read@v1` | NWS CAP, свободно для любого использования |
| `sc-01` | `gaia.air.read@v1` | Sensor.Community ODbL — цитировать |
| `cwop-01` | `gaia.weather.read@v1` | Только MADIS CWOP (без ограничений) |
| `argo-01` | `gaia.argo.read@v1` | Официальный GDAC-каталог активных поплавков; передайте `wmo` для последнего профиля с проверкой QC, цитировать DOI 10.17882/42182 |
| `metno-01` | `gaia.weather.read@v1` | MET Norway METAR, CC BY 4.0 + NLOD |
| `usgs-geomag-01`, `usgs-geomag-*` | `gaia.geomag.read@v1` | Все 14 официальных обсерваторий USGS: каждая — отдельное устройство и точка ATLAS; USGS PD, **не INTERMAGNET** |
| `nws-flood-01` | `gaia.flood.read@v1` | NWS CAP паводки (WaterWatch JSON ушёл в 301; GloFAS WMS не скрейпится) |
| `effis-01` | `gaia.effis.read@v1` | Copernicus EFFIS CC BY 4.0 |
| `usgs-volcano-01` | `gaia.volcano.read@v1` | USGS PD |
| `fintraffic-ais-01` | `gaia.ais.public.read@v1` | Fintraffic AIS, CC BY 4.0, воды Финляндии — **не** own-edge AIS |
| `eccc-hydro-01` | `gaia.river.read@v1` | ECCC hydrometric, End-use Licence + атрибуция |
| `fmi-01` | `gaia.weather.read@v1` | FMI open data, CC BY 4.0 |
| `nws-tsunami-01` | `gaia.tsunami.read@v1` | NWS CAP цунами (PD США) — предупреждение, не датчик |
| `smhi-hydro-01` | `gaia.river.read@v1` | SMHI hydroobs, CC BY 4.0 |
| `nhc-cyclone-01` | `gaia.cyclone.read@v1` | NOAA NHC CurrentStorms, PD США — только AL/EP/CP, не JTWC |
| `emsc-01` | `gaia.quake.read@v1` | EMSC FDSN, CC BY 4.0 — цитировать EMSC; не замена USGS |
| `ea-flood-01` | `gaia.flood.read@v1` | EA OGL, только Англия (не SEPA/NRW) |
| `ptwc-01` | `gaia.tsunami.read@v1` | PTWC Atom, PD США — предупреждение, не мареограф |
| `kystverket-ais-01` | `gaia.ais.public.read@v1` | BarentsWatch NLOD 2.0, воды Норвегии — нужен токен |
| `adsb-lol-01` | `gaia.adsb.public.read@v1` | ADSB.lol ODbL 1.0 — не own-edge, не OpenSky/ADSBx |

## Не включаем как платные Hub SKU

| Источник | Почему |
|----------|--------|
| Global Fishing Watch | Non-commercial |
| Stanford RFI / related | **CC BY-NC** |
| ADSBx commercial API | NC / paid ToS |
| GPSJam heatmaps | Серая зона — не как paid SKU |
| aisstream alone | Только как свой feeder, не единственная коммерческая зависимость |

## Карта / watchbox / composite → ATLAS

Устройства выше — **SKU флота GAIA**. Слои, пины, **watchboxes** и composite Hub SKU
(`atlas.situation.brief@v1`, `atlas.fire.weather@v1`, `atlas.nearest.read@v1`,
`atlas.watchbox.check@v1`) — на **ATLAS**: [GUIDE](https://github.com/alexar76/atlas/blob/main/docs/GUIDE.md)
(EN · RU · ES · FR · ZH). Отдельной поверхности на GAIA для них нет.

**Watchbox** (кратко): сохранённый bbox + фильтр слоёв; агент/оператор периодически
делает **check** и получает LIVE-совпадения в рамке + content receipt. Подписка —
REST; check — billable Hub SKU. Подробности в ATLAS GUIDE.

## Хаб

Капабилити появляются на Hub после **redeploy GAIA** (`deploy_gaia.sh`) и federation crawl
`iot.modelmarket.dev`. Описания UI каталога: `aimarket-hub/cap-descriptions-i18n.json`
(EN · RU · ES · FR · ZH). ATLAS composite — после crawl peer `atlas.modelmarket.dev`.
