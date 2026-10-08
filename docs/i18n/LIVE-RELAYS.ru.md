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
| `gaia.air.read@v1` | `{ "device_id", "latitude"?, "longitude"? }` | **Операторский** пин **или** lat/lon покупателя на `om-aq-*` / `sc-01` / `sc-{slug}` (Open-Meteo; Sensor.Community area — пусто → offline) |
| `gaia.window@v1` | `{ "device_id", "n" }` | Как у read |
| `gaia.fire.read@v1` | `{ "device_id"?, "west"?, "south"?, "east"?, "north"?, "limit"? }` | **Покупатель может** отфильтровать FIRMS CSV по bbox / top-N — без клиентских URL |

Большинство фиксированных сенсоров — оператор-якоря. Исключения с buyer `latitude`/`longitude` на том же SKU: Open-Meteo AQ (`om-aq-*`) и Sensor.Community (`sc-*`). Event-фиды вроде FIRMS глобальны; bbox — способ спросить «пожары рядом», не открывая новый upstream.

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
| `om-aq-01` | Open-Meteo AQ | PM2.5/PM10/CO₂; без ключа; покупатель может передать `latitude`/`longitude` |
| `sc-01`, `sc-{slug}` | Sensor.Community | Crowd SDS011 area (ODbL — цитировать). Берлин + mesh городов (`GAIA_SC_MESH_ENABLED`). Buyer lat/lon. Пустая зона → offline. Crowd ≠ станция; mesh ≠ все узлы Земли |
| `sta-01` | OGC SensorThings | Опционально; может таймаутить (`GAIA_STA_ENABLED`) |
| `openaq-01` | OpenAQ v3 | **Нужен бесплатный** `GAIA_OPENAQ_API_KEY` — взять на [explore.openaq.org](https://explore.openaq.org) (регистрация → API key). Прописать в `.env` на хосте GAIA и пересоздать `gaia-backend`. Локация по умолчанию `GAIA_OPENAQ_LOCATION_ID=2178` (одна точка на карте). |

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
| `sc-01`, `sc-{slug}` | `gaia.air.read@v1` | Sensor.Community ODbL — цитировать; любой lat/lon на SKU; mesh `sc-{slug}` (`GAIA_SC_MESH_ENABLED`); пусто → offline; crowd ≠ станция |
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
| `frost-*-01` (6) | `gaia.weather.read@v1` | MET Norway Frost (CC BY 4.0 + NLOD) — in-situ, не METAR; нужен `GAIA_FROST_CLIENT_ID` |
| `dmi-*-01` (5) | `gaia.weather.read@v1` | DMI metObs CC BY 4.0 — Дания, без ключа |
| `sepa-*-01` | `gaia.river.read@v1` | SEPA KiWIS OGL — только Шотландия |
| `nrw-*-01` | `gaia.river.read@v1` | NRW River Levels OGL — только Уэльс; нужен `GAIA_NRW_API_KEY` |
| `ingv-01` | `gaia.quake.read@v1` | INGV FDSN CC BY 4.0 — цитировать INGV; Италия/регион, не замена USGS/EMSC |
| `cdip-*-01` | `gaia.marine.read@v1` | CDIP ERDDAP — атрибуция CDIP/Scripps/USACE + [cdip.ucsd.edu](https://cdip.ucsd.edu/); in-situ волны |
| `icos-*-01` | `gaia.ghg.read@v1` **новый** | ICOS ATC NRT CO₂ CC BY 4.0 — цитировать ICOS; нужен `GAIA_ICOS_CPAUTH_TOKEN` |
| `hubeau-*-01` (20 FR) | `gaia.river.read@v1` | Hub'Eau hydrométrie (Etalab OL 2.0) — только Франция; не Vigicrues |
| `ehyd-*-01` (24 AT) | `gaia.river.read@v1` | eHYD / BMLUK `pegel_aktuell` CC BY 4.0 — «Datenquelle: ehyd.gv.at»; только Австрия |
| `smhi-wx-*-01` (15 SE) | `gaia.weather.read@v1` | SMHI MetObs CC BY 4.0 — in-situ погода; **не** `smhi-hydro-01` |
| `sg-wx-*-01` (12 SG) | `gaia.weather.read@v1` | Singapore NEA / data.gov.sg — Singapore Open Data Licence; ветер (+ T/RH/дождь) |
| `sg-psi-*-01` (5 регионов) | `gaia.air.read@v1` | Singapore NEA PSI / PM2.5 — Open Data Licence; региональный 24h индекс |
| `hko-wx-*-01` (15 HK) | `gaia.weather.read@v1` | HKO `rhrread` — DATA.GOV.HK, атрибуция HKSAR / HKO |
| `be-aq-*-01` (12 BE) | `gaia.air.read@v1` | IRCELINE SOS PM2.5 CC BY 4.0 — цитировать IRCELINE; только Бельгия |
| `aemet-wx-*-01` (15 ES) | `gaia.weather.read@v1` | AEMET OpenData — PSI, «Fuente: AEMET»; нужен `GAIA_AEMET_API_KEY` |
| `ch-wx-*-01` (15 CH) | `gaia.weather.read@v1` | MeteoSwiss OGD CC BY 4.0 — in-situ; GeoJSON LV95 → WGS84 |
| `bafu-*-01` (15 CH) | `gaia.river.read@v1` | BAFU/FOEN LINDAS OGD-CH Open-Use — Q + геодезический уровень; не предупреждение |
| `cwa-wx-*-01` (12 TW) | `gaia.weather.read@v1` | CWA OpenData OGDL 1.0; нужен `GAIA_CWA_API_KEY` |
| `mf-wx-*-01` (12 FR) | `gaia.weather.read@v1` | Météo-France DPObs Etalab OL 2.0; нужен `GAIA_METEOFRANCE_APPLICATION_ID` |
| `ee-wx-*-01` (12 EE) | `gaia.weather.read@v1` | Estonian Weather Service XML CC BY 4.0 |
| `is-wx-*-01` (12 IS) | `gaia.weather.read@v1` | IMO AWS hourly ODC-By |
| `hk-aqhi-*-01` (18 HK) | `gaia.air.read@v1` | EPD AQHI DATA.GOV.HK — индекс 1–10, **не** PM2.5 |

Densify (те же SKU, больше пинов): EA 7→20, PEGELONLINE 10→22, ECCC 1→8, FMI 1→5, Frost 2→6, DMI 2→5, AURN London 1→5, UHSLC FD 1→5.

## P4 — полные сети и координатные слои

Одна точка ATLAS соответствует одной координате показания. Станционные сети публикуют все официальные станции; сеточные источники принимают произвольные `latitude`/`longitude` покупателя и возвращают координату исходной/расчётной ячейки.

| Слой / device_id | Покрытие | Коммерческая основа |
|---|---|---|
| Дым `hms-smoke-01` | Каждый полигон HMS с полным контуром и отверстиями, стабильным `polygon_id`, digest геометрии и bbox; centroid — только якорь карты | PD США; цитировать NOAA/NESDIS HMS |
| Качество воды `usgs-wq-01` | Полностью обходит свежие latest-continuous наблюдения (48 ч по умолчанию, `max_age_hours`) и пакетно соединяет станции с USGS monitoring-locations; фильтр/`require_all`; одна подписанная строка на координату; время, Approved/Provisional и qualifiers; stale/неполные данные отклоняются | PD США; цитировать USGS |
| DART `noaa-dart-01`, `dart-*` | Все 43 активные станции зафиксированного каталога NDBC | PD США; цитировать NOAA/NDBC |
| Осадки `imerg-01` | Любая координата; точный центр ячейки IMERG; preliminary | NASA open data; цитировать NASA GPM |
| Статус радара `nexrad-status-01` | Все WSR-88D в собственных координатах; не reflectivity | PD США; цитировать NOAA/NWS |
| Атмосфера `cams-*` | Любая координата CAMS | CC BY 4.0; коммерческий/self-hosted Open-Meteo |
| EPA RadNet `radnet-*` | Все 140 официальных координат мониторов | Данные правительства США; цитировать EPA RadNet |
| Почва `soil-*` | Любая координата CLMS SWI020 | Copernicus: свободно для любых целей с атрибуцией |
| Солнце `solar-*` | Любая координата NASA POWER | NASA open data; цитировать NASA POWER |
| Снег `snow-*` | Любая координата CONUS; точная ячейка SNODAS | PD США; цитировать NOAA/NOHRSC |
| Морской лёд `nsidc-ice-01` | Любая арктическая координата; точная ячейка 25 км | Данные правительства США; обязательная цитата; не для навигации |
| Температура суши `lst-*` | Любая координата Sentinel-3 SLSTR | Copernicus free/full/open с атрибуцией изменений |

Реестры RadNet и DART обновляются одной командой `python3 scripts/update_p4_networks.py`; она одновременно пишет deploy-safe копии GAIA и ATLAS.

## Не включаем как платные Hub SKU

| Источник | Почему |
|----------|--------|
| Global Fishing Watch | Non-commercial |
| Stanford RFI / related | **CC BY-NC** |
| ADSBx commercial API | NC / paid ToS |
| GPSJam heatmaps | Серая зона — не как paid SKU |
| aisstream alone | Только как свой feeder, не единственная коммерческая зависимость |
| ENTSO-E Transparency | Свободно смотреть (EU 543/2013), но ToS без явной коммерческой перепродажи для paid Hub SKU |

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
