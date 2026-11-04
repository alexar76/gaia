# GAIA 实时中继 — 运维指南

**语言：** [EN](../LIVE-RELAYS.md) · [RU](LIVE-RELAYS.ru.md) · [ES](LIVE-RELAYS.es.md) · [FR](LIVE-RELAYS.fr.md) · [ZH](LIVE-RELAYS.zh.md)

**开发者：** [add-gaia-atlas-sensor](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md)（EN · RU · ES · FR · ZH）

**ATLAS 运营方用例：** [`OPERATOR-USE-CASES.zh.md`](https://github.com/alexar76/atlas/blob/main/docs/i18n/OPERATOR-USE-CASES.zh.md)

## 要点

实时设备**并不拥有**传感器。Ed25519 密钥证明：

> 网关如实中继了公共 API *X* 在请求时刻返回的内容

走同一套认证与 Pay-on-Verified。`source` 见 `gaia.fleet.status@v1`。

## 安全

| 控制 | 行为 |
|------|------|
| 主机白名单 | 仅 `_ALLOWED_HOSTS`（`live.py`）中的 HTTPS |
| 无客户端 URL | `input` 仅含 `device_id`（+ 下方安全过滤器） |
| ID 校验 | Station / box / lat-lon / NOAA / OpenAQ 在拼 URL 前校验 |
| 无凭证 URL | 拒绝 `user:pass@host` |
| 无重定向 | `follow_redirects=False`；非 200 → offline |
| 计费 | 上游失败 → offline → Hub **不**扣费 |

## 买家 invoke 传什么

| Capability | Buyer `input` | 谁决定地理 |
|------------|---------------|------------|
| 多数 `gaia.*.read@v1` | `{ "device_id": "…" }` | **运营方**锚定设备（或发布城市 mesh——买家选 `device_id`） |
| `gaia.air.read@v1` | `{ "device_id", "latitude"?, "longitude"? }` | **运营方**钉点 **或** 买家 lat/lon（`om-aq-*` / `sc-01` / `sc-{slug}`：Open-Meteo；Sensor.Community 区域查询——空区 → offline） |
| `gaia.window@v1` | `{ "device_id", "n" }` | 同 read |
| `gaia.fire.read@v1` | `{ "device_id"?, "west"?, "south"?, "east"?, "north"?, "limit"? }` | **买家可**按 bbox / top-N 过滤 FIRMS CSV——仍无客户端 URL |

多数固定传感器仍由运营方锚定。同一 SKU 接受买家 `latitude`/`longitude` 的例外：Open-Meteo AQ（`om-aq-*`）与 Sensor.Community（`sc-*`）。FIRMS 这类事件源是全球的；bbox 是问「附近火灾」的方式，不会引入新上游主机。

## 目录（`GAIA_ENABLE_LIVE=1`）

### 天气 — `gaia.weather.read@v1`

| device_id | 上游 | 说明 |
|-----------|------|------|
| `ws-01` / `ws-02` | 模拟器 | 始终存在 |
| `nws-01` | NOAA/NWS | 美国站点；公有领域；需 User-Agent |
| `om-wx-01` | Open-Meteo | 全球；默认柏林（`GAIA_OM_LAT`/`LON`）；CC BY 4.0 |

### 空气 — `gaia.air.read@v1`

| device_id | 上游 | 说明 |
|-----------|------|------|
| `aq-01` | 模拟器 | 始终存在 |
| `osm-01` | openSenseMap | 公民科学；许可按 box |
| `om-aq-01` | Open-Meteo AQ | PM2.5/PM10/CO₂；无需密钥；买家可传 `latitude`/`longitude` |
| `sc-01`, `sc-{slug}` | Sensor.Community | 众包 SDS011 区域查询（ODbL——须注明）。柏林默认 + 城市 mesh（`GAIA_SC_MESH_ENABLED`）。买家 lat/lon。空区 → offline。众包 ≠ 参考站；mesh ≠ 地球上每个节点 |
| `sta-01` | OGC SensorThings | 可选；可能超时（`GAIA_STA_ENABLED`） |
| `openaq-01` | OpenAQ v3 | **需要**免费密钥（[explore.openaq.org](https://explore.openaq.org)）→ `GAIA_OPENAQ_API_KEY` |

### 电网 — `gaia.grid.read@v1`（仅 LIVE）

| device_id | 上游 | 字段 |
|-----------|------|------|
| `uk-grid-01` | carbonintensity.org.uk | `carbon_intensity_gco2_kwh`（actual，否则 forecast） |

### 地震 — `gaia.quake.read@v1`（仅 LIVE）

| device_id | 上游 | 字段 |
|-----------|------|------|
| `usgs-quake-01` | USGS GeoJSON 日 M≥2.5 | `magnitude`、`depth_km`、`latitude`、`longitude` |

### 潮汐 — `gaia.tide.read@v1`（仅 LIVE）

| device_id | 上游 | 字段 |
|-----------|------|------|
| `noaa-tide-01` | NOAA CO-OPS | `water_level_m`（MLLW，公制）；默认 8518750 |

### 河流 — `gaia.river.read@v1`（仅 LIVE）

| device_id | 上游 | 字段 |
|-----------|------|------|
| `usgs-river-01` | USGS NWIS | `discharge_m3s`、`gage_height_m`；默认 `01646500` Potomac |

### 海洋 — `gaia.marine.read@v1`（仅 LIVE）

| device_id | 上游 | 字段 |
|-----------|------|------|
| `ndbc-01` | NOAA NDBC 浮标 | `wave_height_m`、`sst_c`、`wind_mps`（若有）；默认 `44025` |
| `om-marine-01` | Open-Meteo Marine | `wave_height_m`、`sst_c`；默认纽约港 |

### 野火 — `gaia.fire.read@v1`（LIVE · 可自由商用）

| device_id | 上游 | 字段 | 许可 |
|-----------|------|------|------|
| `firms-fire-01` | NASA FIRMS VIIRS CSV（可选 `GAIA_FIRMS_MAP_KEY`） | 认证值：最亮 `brightness_k`、`confidence`、lat/lon。地图载荷：`hotspots[]`（top-N，默认 `GAIA_FIRMS_HOTSPOT_LIMIT=500`，最大 5000）+ `hotspot_count`。可选买家 `west/south/east/north` + `limit`。 | NASA 开放数据——**须注明 NASA FIRMS** + disclaimer |

ATLAS Wildfire 图层将 `hotspots[]` 展开为每条检测一个针脚（`firms-hs-NNNN`）。

### 辐射 — `gaia.radiation.read@v1`（LIVE · 可自由商用）

| device_id | 上游 | 字段 | 许可 |
|-----------|------|------|------|
| `safecast-01` | Safecast measurements API | `cpm`、`latitude`、`longitude` | **CC0** |

Hub `safecast-01` 仍为 30 天窗口。地图锚点 `safecast-melbourne` / `safecast-adelaide` 为档案模式（`max_age_days: 0`），否则南澳 2014 年车载网格会消失。图钉带 `captured_at`——不是「此刻」。

### GNSS 干扰 — `gaia.jamming.read@v1`（LIVE · 可自由商用）

| device_id | 上游 | 字段 | 许可 |
|-----------|------|------|------|
| `cybernews-jam-01` | cybernews.space `/api/data/gnss` | `interference_score`、`radius_km`、lat/lon | **CC BY 4.0**——须署名 |

### 边缘交通 — `gaia.adsb.read@v1` / `gaia.ais.read@v1`（可选 feeder）

| device_id | 上游 | 说明 |
|-----------|------|------|
| `feeder-adsb-01` | 自有 dump1090 → `POST /feeder/v1/ingest` | `GAIA_FEEDER_ENABLED=1` + `GAIA_FEEDER_TOKEN`。首次 ingest 前为 offline。**非** ADSBx / 第三方 NC 聚合。 |
| `feeder-ais-01` | 自有 AIS 接收机 | 同一 ingest。**勿**把 aisstream 当作唯一付费 SKU。 |

### P0 — 众包空气（Sensor.Community）

| device_id | SKU | 说明 |
|-----------|-----|------|
| `sc-01`, `sc-{slug}` | `gaia.air.read@v1` | Sensor.Community **ODbL**——须注明。SKU 上买家 lat/lon；城市 mesh（`GAIA_SC_MESH_ENABLED`）。空区 → offline。众包 ≠ 参考站；mesh ≠ 地球上每个节点 |

### P2 — 许可证已钉死的公共中继

自有边缘 AIS（`gaia.ais.read@v1`）仍是运营方接收机。公共 AIS 是**另一个** SKU。

| device_id | SKU | 上游 | 许可 |
|-----------|-----|------|------|
| `fintraffic-ais-01` | `gaia.ais.public.read@v1` | Fintraffic Digitraffic AIS（`meri.digitraffic.fi`） | **CC BY 4.0** — 注明 Fintraffic。仅芬兰水域。**不是** GFW、AISStream 或自有 AIS |
| `eccc-hydro-01` | `gaia.river.read@v1` | ECCC MSC GeoMet（默认 `02HC003` Humber） | End-use Licence — 可商用 + 注明 ECCC。水位可能是大地高 |
| `fmi-01` | `gaia.weather.read@v1` | FMI open WFS（赫尔辛基） | **CC BY 4.0** — 芬兰气象研究所 |
| `nws-tsunami-01` | `gaia.tsunami.read@v1` | NWS CAP 海啸 warning/watch/advisory | 美国公有领域。警报产品，不是验潮仪。空源 → 离线 / 不扣费 |
| `smhi-hydro-01` | `gaia.river.read@v1` | SMHI hydroobs（站 2357 Abisko） | **CC BY 4.0** — SMHI。不是洪水预报 |

Kystverket AIS、EMSC、NHC 气旋、EA 英格兰洪水、PTWC Atom、ADSB.lol 已作为 **P3** 接入。

| device_id | SKU | 说明 |
|-----------|-----|------|
| `nhc-cyclone-01` | `gaia.cyclone.read@v1` | NHC 公有领域 — 仅 AL/EP/CP |
| `emsc-01` | `gaia.quake.read@v1` | EMSC CC BY 4.0 — 须注明 EMSC |
| `ea-flood-01` | `gaia.flood.read@v1` | EA OGL，仅英格兰 |
| `ptwc-01` | `gaia.tsunami.read@v1` | PTWC Atom — 不是验潮仪 |
| `kystverket-ais-01` | `gaia.ais.public.read@v1` | BarentsWatch NLOD — 需要 token |
| `adsb-lol-01` | `gaia.adsb.public.read@v1` | ADSB.lol ODbL 1.0 |
| `frost-*-01`（6） | `gaia.weather.read@v1` | MET Norway Frost（CC BY 4.0 + NLOD）现场站，非 METAR；需 `GAIA_FROST_CLIENT_ID` |
| `dmi-*-01`（5） | `gaia.weather.read@v1` | DMI metObs CC BY 4.0 — 丹麦，无需密钥 |
| `sepa-*-01` | `gaia.river.read@v1` | SEPA KiWIS OGL — 仅苏格兰 |
| `nrw-*-01` | `gaia.river.read@v1` | NRW River Levels OGL — 仅威尔士；需 `GAIA_NRW_API_KEY` |
| `ingv-01` | `gaia.quake.read@v1` | INGV FDSN CC BY 4.0 — 注明 INGV；意大利/区域，非 USGS/EMSC 替代 |
| `cdip-*-01` | `gaia.marine.read@v1` | CDIP ERDDAP — 注明 CDIP/Scripps/USACE + [cdip.ucsd.edu](https://cdip.ucsd.edu/)；现场浪高 |
| `icos-*-01` | `gaia.ghg.read@v1` **新** | ICOS ATC NRT CO₂ CC BY 4.0 — 注明 ICOS；需 `GAIA_ICOS_CPAUTH_TOKEN` |
| `hubeau-*-01`（20 FR） | `gaia.river.read@v1` | Hub'Eau hydrométrie（Etalab OL 2.0）— 仅法国；非 Vigicrues |
| `ehyd-*-01`（24 AT） | `gaia.river.read@v1` | eHYD / BMLUK `pegel_aktuell` CC BY 4.0 — «Datenquelle: ehyd.gv.at»；仅奥地利 |
| `smhi-wx-*-01`（15 SE） | `gaia.weather.read@v1` | SMHI MetObs CC BY 4.0 — 现场气象；**不是** `smhi-hydro-01` |
| `sg-wx-*-01`（12 SG） | `gaia.weather.read@v1` | Singapore NEA / data.gov.sg — Singapore Open Data Licence；风（+ 温/湿/雨） |
| `sg-psi-*-01`（5 区域） | `gaia.air.read@v1` | Singapore NEA PSI / PM2.5 — Open Data Licence；区域 24h 指数 |
| `hko-wx-*-01`（15 HK） | `gaia.weather.read@v1` | HKO `rhrread` — DATA.GOV.HK，注明 HKSAR / HKO |
| `be-aq-*-01`（12 BE） | `gaia.air.read@v1` | IRCELINE SOS PM2.5 CC BY 4.0 — 注明 IRCELINE；仅比利时 |
| `aemet-wx-*-01`（15 ES） | `gaia.weather.read@v1` | AEMET OpenData — PSI 复用，「Fuente: AEMET」；需 `GAIA_AEMET_API_KEY` |
| `ch-wx-*-01`（15 CH） | `gaia.weather.read@v1` | MeteoSwiss OGD CC BY 4.0 — 现场站；GeoJSON LV95 → WGS84 |
| `bafu-*-01`（15 CH） | `gaia.river.read@v1` | BAFU/FOEN LINDAS OGD-CH Open-Use — 流量 + 大地水准；非洪水预警 |
| `cwa-wx-*-01`（12 TW） | `gaia.weather.read@v1` | CWA OpenData OGDL 1.0；需 `GAIA_CWA_API_KEY` |
| `mf-wx-*-01`（12 FR） | `gaia.weather.read@v1` | Météo-France DPObs Etalab OL 2.0；需 `GAIA_METEOFRANCE_APPLICATION_ID` |
| `ee-wx-*-01`（12 EE） | `gaia.weather.read@v1` | 爱沙尼亚气象局 XML CC BY 4.0 |
| `is-wx-*-01`（12 IS） | `gaia.weather.read@v1` | 冰岛气象局 AWS 小时 ODC-By |
| `hk-aqhi-*-01`（18 HK） | `gaia.air.read@v1` | EPD AQHI DATA.GOV.HK — 1–10 健康指数，**不是** PM2.5 |

扩网（同 SKU，更多针脚）：EA 7→20、PEGELONLINE 10→22、ECCC 1→8、FMI 1→5、Frost 2→6、DMI 2→5、伦敦 AURN 1→5、UHSLC FD 1→5。

## P4 — 完整网络与坐标图层

一个 ATLAS 点对应一个读数坐标。站点网络发布全部官方站点；网格源接受买方任意 `latitude`/`longitude`，并返回源/查询网格坐标。

| 图层 / device_id | 覆盖 | 商业依据 |
|---|---|---|
| 烟雾 `hms-smoke-01` | 每个 HMS 多边形的完整外环与内环、稳定 `polygon_id`、几何摘要与 bbox；质心仅作地图锚点 | 美国公有领域；注明 NOAA/NESDIS HMS |
| 水质 `usgs-wq-01` | 完整分页新鲜 latest-continuous 观测（默认 48 小时，可设 `max_age_hours`）并联接 USGS monitoring-locations；筛选/`require_all`；每站一条签名坐标；逐参数时间、Approved/Provisional 与 qualifiers；陈旧/不完整数据拒绝 | 美国公有领域；注明 USGS |
| DART `noaa-dart-01`, `dart-*` | 固化 NDBC 目录中的全部 43 个活动站 | 美国公有领域；注明 NOAA/NDBC |
| 降水 `imerg-01` | 任意坐标；精确 IMERG 网格中心；初步数据 | NASA 开放数据；注明 NASA GPM |
| 雷达状态 `nexrad-status-01` | 全部 WSR-88D 位于各自坐标；不是反射率 | 美国公有领域；注明 NOAA/NWS |
| 大气 `cams-*` | 任意 CAMS 坐标 | CC BY 4.0；商业/self-hosted Open-Meteo |
| EPA RadNet `radnet-*` | 全部 140 个官方监测点坐标 | 美国政府数据；注明 EPA RadNet |
| 土壤 `soil-*` | 任意 CLMS SWI020 坐标 | Copernicus：注明来源后可用于任何目的 |
| 太阳能 `solar-*` | 任意 NASA POWER 坐标 | NASA 开放数据；注明 NASA POWER |
| 积雪 `snow-*` | CONUS 内任意坐标；精确 SNODAS 网格 | 美国公有领域；注明 NOAA/NOHRSC |
| 海冰 `nsidc-ice-01` | 任意北极坐标；精确 25 公里网格 | 美国政府数据；必须引用；不可用于导航 |
| 地表温度 `lst-*` | 任意 Sentinel-3 SLSTR 坐标 | Copernicus free/full/open，修改需注明 |

用 `python3 scripts/update_p4_networks.py` 同时刷新 RadNet 与 DART 注册表，并写入 GAIA/ATLAS 两份可部署副本。

## USGS 潜在淹水点与地下水

- **潜在淹水点**（`usgs-rtfi-01`、`gaia.flood.read@v1`）显示在 ATLAS 的洪水图层中。GAIA 将实地测定的阈值与同一水文测站序列过去两小时内的观测值进行比较。详情显示观测时间、水位、阈值和超出量。超出阈值初步表明当地可能发生淹水。道路封闭情况需要另行确认。空结果不会产生可计费的观测。 [USGS RT-FI API](https://api.waterdata.usgs.gov/rtfi-api/docs)
- **地下水**（`usgs-groundwater-01`、`gaia.groundwater.read@v1`）覆盖美国缅因州、纽约州和肯塔基州的三口 USGS 试点观测井。地下水埋深以当地地表为基准，单位为米，向下为正。结果保留观测时间、审核状态和来源质量标记，仅纳入过去 24 小时的观测。 [USGS 水文数据 API](https://api.waterdata.usgs.gov/)

USGS 数据在美国属于公有领域。使用时请注明 USGS 来源。设置 `GAIA_ENABLE_LIVE=1` 后，两个中继默认启用。可分别通过 `GAIA_USGS_RTFI_ENABLED=0` 或 `GAIA_USGS_GROUNDWATER_ENABLED=0` 关闭。 [USGS 使用条款](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits)

## P13 — 日前电价与 Sentinel-2 影像

| device_id | SKU | 数据源 | 许可 / 范围 |
|-----------|-----|--------|-------------|
| `smard-price-<竞价区>`（15 个区：`de-lu`、`at`、`be`、`no2`、`dk1`、`dk2`、`fr`、`it-north`、`nl`、`pl`、`se4`、`ch`、`si`、`cz`、`hu`） | `gaia.power.price.read@v1`、`gaia.power.window@v1` | SMARD.de（德国联邦网络局）日前序列，按周的刻钟数据块 | **CC BY 4.0**——注明 Bundesnetzagentur / SMARD.de。交易所批发价，不是零售电价。序列按数值对应到竞价区。`GAIA_SMARD_ZONES` 限定竞价区，`GAIA_SMARD_ENABLED=0` 关闭。[指南](POWER-PRICES.zh.md) |
| `s2-l2a-01`（按点查询） | `gaia.imagery.read@v1` | Element84 Earth Search STAC + AWS Open Data `sentinel-cogs` | **Copernicus Sentinel 数据**——免费、完整、开放，含商业用途；署名「Contains modified Copernicus Sentinel data <年份>」。最新无云影像的一个像元。`GAIA_S2_ENABLED=0` 关闭。[指南](IMAGERY.zh.md) |

不是中继，但在同一批发布：免费的 `gaia.place.resolve@v1`（[GeoNames 地名库](PLACES.zh.md)）以及按验证付费的设备控制 `gaia.actuate@v1`（[指南](ACTUATION.zh.md)）。

## 不作为付费 Hub SKU

| 来源 | 原因 |
|------|------|
| Global Fishing Watch | 非商业条款 |
| Stanford RFI / related | **CC BY-NC** |
| ADSBx commercial API | NC / 付费 ToS |
| GPSJam heatmaps | 灰色地带——勿作付费 SKU |
| aisstream alone | 仅作自有 feeder，不作唯一商业依赖 |
| ENTSO-E Transparency | 可免费查看（EU 543/2013），但 ToS 未明确允许付费 Hub SKU 的商业再分发 |

## 地图 / watchbox / 组合产品 → ATLAS

以上设备是 **GAIA 机队 SKU**。地图图层、针脚、**watchbox** 与 Hub 组合 SKU
（`atlas.situation.brief@v1`、`atlas.fire.weather@v1`、`atlas.nearest.read@v1`、
`atlas.watchbox.check@v1`）在 **ATLAS** 上：
[GUIDE](https://github.com/alexar76/atlas/blob/main/docs/GUIDE.md)（EN · RU · ES · FR · ZH）。
GAIA 侧没有单独产品界面。

**Watchbox**（简述）：保存的 bbox + 图层过滤；代理/运营方定期 **check**，
返回框内 LIVE 匹配 + content receipt。订阅 = REST；check = Hub 计费 SKU。
详情见 ATLAS GUIDE。

## Hub

**重新部署 GAIA** 并等待 Hub federation crawl `iot.modelmarket.dev` 后，读数类能力出现在目录。
UI 文案：`aimarket-hub/cap-descriptions-i18n.json`（EN · RU · ES · FR · ZH）。
ATLAS 组合 SKU 在 crawl peer `atlas.modelmarket.dev` 后出现。

## 新增数据源

| 图层或来源 | GAIA | 地图显示内容 | 来源及条款 |
|---|---|---|---|
| Hub’Eau 地下水 | `gaia.groundwater.read@v1` · `hubeau-groundwater-01` | 法国八口试点井提供从测量基准点到水面的深度和以 NGF 表示的水面高程。卡片会显示观测时间和每项数值的基准。 | [Etalab Licence Ouverte](https://assistance.brgm.fr/fr/aide/HubEau) |
| NOAA NWPS 河流水位预报 | `gaia.river.forecast.read@v1` · `nwps-pttp1`, `nwps-rmdv2`, `nwps-vckm6`, `nwps-dlrt2` | 卡片显示未来 72 小时内可用预报点的最高水位、发布时间和峰值时间。水位以该水文站的本地基准为准。 | [NOAA/NWS · U.S. public domain](https://water.noaa.gov/about/api) |
| 英国环境署验潮站 | `gaia.tide.read@v1` · `ea-tide-e70039`, `ea-tide-e72639`, `ea-tide-e70539`, `ea-tide-e70739`, `ea-tide-e73839` | 英国沿海站点以米为单位提供相对于 Ordnance Datum Newlyn 基准的平均海平面高度（mAOD）。 | [Environment Agency · OGL v3](https://environment.data.gov.uk/flood-monitoring/doc/tidegauge) |
| Fintraffic 道路事件 | `gaia.road.events.read@v1` · `fintraffic-road-announcements-01`, `fintraffic-roadworks-01` | 芬兰道路通告和施工记录显示受影响路段及源数据中的有效时段。 | [Fintraffic Digitraffic · CC BY 4.0](https://www.digitraffic.fi/en/terms-of-service/) |
| NOAA G-AIRMET | `gaia.aviation.hazards.read@v1` · `awc-gairmet-01` | 美国航空预报的多边形和线条标注危险类型、发布时间和有效时间。 | [NOAA/NWS Aviation Weather Center · U.S. public domain](https://aviationweather.gov/data/api/) |
| NDW 道路通告和临时封闭 | `gaia.road.events.read@v1` · `ndw-road-srti-01`, `ndw-road-closures-01` | `road_events` 图层显示荷兰道路安全通告、临时封闭及受影响路段的几何形状。签名快照在发布五分钟后过期。每项交通措施另有自己的有效时段。 | [NDW · CC0 1.0](https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/) |
| FMI 波罗的海测波浮标 | `gaia.marine.read@v1` · `fmi-marine-103976`, `fmi-marine-108126`, `fmi-marine-108499`, `fmi-marine-134220`, `fmi-marine-134221`, `fmi-marine-134246` | `marine` 图层中的六个浮标提供有效波高。卡片也会显示同一时刻测得的谱峰周期、来波方向和水温。时间戳表示波浪采样的开始时间。观测数据两小时后过期。 | [FMI · CC BY 4.0](https://en.ilmatieteenlaitos.fi/download-observations-questions) |
| USDA NRCS SNOTEL 积雪观测 | `gaia.snow.read@v1` · `snotel-335`, `snotel-415` | `snow` 图层显示科罗拉多州 Berthoud Summit 和 Copper Mountain 两站的每小时积雪深度及雪水当量，单位为厘米。GAIA 只配对同一小时内通过自动质量检查且距今不超过六小时的测量值。 | [USDA NRCS · 美国公有领域](https://catalog.data.gov/dataset/snowpack-telemetry-network-snotel) |
| SMHI 海洋观测 | `gaia.ocean.observations.read@v1` · `smhi-ocean-flinten16-8m`, `smhi-ocean-flinten16-2m`, `smhi-ocean-flinten7-8m`, `smhi-ocean-kalmar2-0m` | `ocean_obs` · 瑞典三个站点提供四组指定深度的观测序列。卡片按站点和深度显示盐度（PSU）、溶解氧（mL/L）或流速和流向。同一卡片中的测量值取自同一观测时刻，最长保留三小时。 | [SMHI · CC BY 4.0](https://www.smhi.se/data/om-smhis-data/villkor-for-anvandning) |
| ECCC 野火烟雾预报 | `gaia.smoke.forecast.read@v1` · `eccc-smoke-seattle`, `eccc-smoke-toronto`, `eccc-smoke-calgary` | `smoke_forecast` · 覆盖西雅图、多伦多和卡尔加里附近的三个模型网格。卡片显示下一 UTC 整点模拟烟羽内的近地面总 PM₂.₅ 浓度（µg/m³）、模型起报时间及预报有效时间。模型起报时间距今最长 18 小时。 | [ECCC · MSC End-use Licence](https://eccc-msc.github.io/open-data/licence/readme_en/) |
| NOAA SWPC D-RAP | `gaia.hf.absorption.read@v1` · `swpc-drap-01` | `hf_absorption` · 全球模型网格显示受电离层 D 区 1 dB 吸收影响的最高频率。全球概览显示 900 个 6° × 12° 网格内的最大值，单位为 MHz。放大后可以查看全部 8,100 个 2° × 4° 原始网格的采样值。网格数据在 15 分钟后失效。 | [NOAA SWPC](https://www.swpc.noaa.gov/products/d-region-absorption-predictions-d-rap) |
| BfS 环境伽马剂量率 | `gaia.radiation.ambient.read@v1` · `bfs-odl-01` | `radiation` · 德国监测站显示经过验证的每小时环境伽马剂量当量率（nSv/h）。卡片注明观测小时的结束时间。数据在两小时后失效。 | [BfS · DL-DE-BY 2.0](https://www.imis.bfs.de/geoportal/resources/sitepolicy.html) |
| GeoNet 沿岸水位 | `gaia.tide.read@v1` · `geonet-coastal-auct`, `geonet-coastal-wlgt`, `geonet-coastal-taut`, `geonet-coastal-gist`, `geonet-coastal-jbtt` | `tide` · 新西兰五个站点显示以当地压力传感器为基准的水柱高度，单位为米。数据在五分钟后失效。 | [GeoNet · CC BY 3.0 NZ](https://www.geonet.org.nz/policy) |
| ČHMÚ 河流观测 | `gaia.river.read@v1` · `chmi-river-042000`, `chmi-river-101800`, `chmi-river-200100` | `river` · 捷克三个水文站显示初步观测水位和流量。如果同一时刻测量了水温，卡片也会显示水温。数据在两小时后失效。 | [ČHMÚ · CC BY 4.0](https://www.chmi.cz/-/kde-najdu-otev%C5%99en%C3%A1-data-%C4%8Dhm%C3%BA-) |
| Entur 列车位置 | `gaia.rail.read@v1` · `entur-rail-vyg-01` | `rail` · 地图显示挪威 Vy 列车近期观测位置，并在有数据时显示线路。位置在供应方设定的有效期结束时失效，最长保留五分钟。 | [Entur · NLOD](https://developer.entur.no/terms-of-service) |
| UK Power Networks 实时停电 | `gaia.power.outages.read@v1` · `ukpn-live-faults-01` | `power_outages` · 显示运营商服务区内的当前故障。卡片显示大致受影响范围、受影响客户数以及运营商发布的预计恢复时间。快照在 30 分钟后失效。 | [UK Power Networks · CC BY 4.0](https://ukpowernetworks.opendatasoft.com/api/explore/v2.1/catalog/datasets/ukpn-live-faults) |
| NOAA PORTS 桥下净空 | `gaia.bridge.clearance.read@v1` · `noaa-airgap-8726371`, `noaa-airgap-8545556`, `noaa-airgap-8575432` | `bridge_clearance` · 美国三座桥梁的传感器提供水面到桥梁固定参考点的初步垂直距离，单位为米。卡片显示样本标准差和来源标志。观测在 18 分钟后失效。通航决策请查阅 NOAA PORTS 和官方航海图。 | [NOAA NOS CO-OPS](https://opendap.co-ops.nos.noaa.gov/erddap/info/IOOS_Air_Gap/index.html) |
| NOAA AOML 海洋漂流浮标 | `gaia.ocean.drifters.read@v1` · `aoml-drifters-01` | `ocean_drifters` · 全球数据展示各浮标最近观测位置和海表温度。浮标报告海平面气压时也会显示。观测在六小时后失效。 | [NOAA AOML / OSMC · CC0 1.0](https://osmc.noaa.gov/erddap/info/OSMC_flattened/index.json) |
| Bike Share Toronto 共享单车 | `gaia.bikeshare.read@v1` · `toronto-bikeshare-01` | `bikeshare` · 站点卡片显示可用单车数量、已报告的电动单车数量、空桩数量及租借和归还状态。报告在五分钟后失效。 | [City of Toronto · OGL](https://open.toronto.ca/open-data-licence/) |
| 多伦多道路限制 | `gaia.road.events.read@v1` · `toronto-road-restrictions-01` | `road_events` · 市政公告在限制有效期间显示受影响的点位或路段，包含按计划重复实施的限制。来源快照在 15 分钟后失效。每条公告都有自己的生效时段。 | [City of Toronto · OGL](https://open.toronto.ca/open-data-licence/) |
| ARSO 河流观测 | `gaia.river.read@v1` · `arso-river-1060`, `arso-river-3660`, `arso-river-5078`, `arso-river-8080` | `river` · 斯洛文尼亚四个水文站报告初步水位和流量。水位以测站当地零点为基准，单位为米，流量单位为 m³/s。卡片也会显示同一时刻测得的水温。观测在两小时后失效。 | [ARSO](https://kazalci.arso.gov.si/en/content/legal-note) |
| Kartverket 沿岸水位 | `gaia.tide.read@v1` · `kartverket-tide-bgo`, `kartverket-tide-osl`, `kartverket-tide-svg`, `kartverket-tide-tos`, `kartverket-tide-trd` | `tide` · 挪威五个验潮站显示相对于国家高程基准 NN2000 的沿岸观测水位，单位为米。观测在两小时后失效。 | [Kartverket · CC BY 4.0](https://vannstand.kartverket.no/tideapi_en.html) |

多伦多道路限制与共享单车数据使用以下署名。“Contains information licensed under the Open Government Licence – Toronto.” [许可证](https://open.toronto.ca/open-data-licence/)

### 充电站、灾害与现场观测

| 来源 | GAIA | ATLAS | 数据与限制 | 来源与条款 |
| --- | --- | --- | --- | --- |
| NDW DOT-NL 公共充电站 | `gaia.ev.charging.read@v1` · `ndw-ev-charging-01` | `ev_charging` | 荷兰公共充电站按接口类型显示空闲数量、总数量和功率。默认查询乌得勒支。区域查询最多覆盖 4 平方度。Hub 每页最多返回 500 个站点，续页须使用快照标识。快照在四分钟后失效。 | [NDW · CC0 1.0](https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/) · [GeoJSON API](https://docs.ndw.nu/en/data-uitwisseling/interface-beschrijvingen/dafne-api/dafne_api_consumer_pull/) |
| BC Wildfire 火灾位置和边界 | `gaia.fire.perimeters.read@v1` · `bc-wildfire-01` | `wildfire_perimeters` | 不列颠哥伦比亚省的活动火灾显示当前状态，并在有数据时显示最新发布的边界。数据集更新时间和各边界的日期分别显示。快照在一小时后失效。 | [BC Wildfire Service · OGL–BC 2.0](https://www2.gov.bc.ca/gov/content/data/policy-standards/data-policies/open-data/open-government-licence-bc) · [火灾边界](https://open.canada.ca/data/dataset/cdfc2d7b-c046-4bf0-90ac-4897232619e1) |
| MeteoSwiss A7 花粉实测 | `gaia.pollen.read@v1` · `ch-pollen-*` | `pollen` | 15 个已登记的自动站按小时报告七类花粉的浓度，单位为粒每立方米。实测零值会保留。观测在两小时后失效。 | [Source: MeteoSwiss · CC BY 4.0](https://opendatadocs.meteoswiss.ch/general/terms-of-use) · [A7](https://opendatadocs.meteoswiss.ch/a-data-groundbased/a7-pollen-stations) |
| 爱尔兰气象浮标网 | `gaia.marine.read@v1` · `ie-buoy-m2`, `ie-buoy-m3`, `ie-buoy-m5`, `ie-buoy-m6` | `marine` | 四个试点浮标报告波高、周期和方向，并在有数据时显示海水温度和风况。观测在两小时后失效。质量标志 0 表示质量未知，1 表示质量良好。 | [Marine Institute and Met Éireann · CC BY 4.0](https://erddap.marine.ie/erddap/info/IWBNetwork/index.html) |
| 澳大利亚 AODN/IMOS 测波浮标 | `gaia.marine.read@v1` · `au-wave-apollo-bay`, `au-wave-wilsons-prom`, `au-wave-wooli` | `marine` | 三个试点浮标在有数据时报告有效波高、峰值周期和来波方向。GAIA 采用来源质量标志 1 的数据。观测在两小时后失效。 | [AODN / IMOS · CC BY 4.0](https://registry.opendata.aws/aodn_wave_buoy_realtime_nonqc/) |
| GeoNet 新西兰火山和相机 | `gaia.volcano.read@v1` · `geonet-volcano-01` | `volcano` | 12 座监测火山显示当前警戒级别和航空颜色代码。状态在取得后 30 分钟失效。可用的相机缩略图显示各自的拍摄时间，并在 30 分钟后失效。 | [GeoNet / Earth Sciences New Zealand and sponsors · CC BY 3.0 NZ](https://www.geonet.org.nz/policy) · [相机](https://doi.org/10.21420/vq6s-w222) |
| NOAA SPC 当地强风暴报告 | `gaia.storm.reports.read@v1` · `noaa-spc-storm-reports-01` | `storm_reports` | 地图显示过去 24 小时内美国龙卷风、冰雹和强风的初步报告。每个点标注来源报告的事件时间。 | [NOAA/NWS SPC · U.S. public domain](https://www.weather.gov/disclaimer) · [CSV](https://www.spc.noaa.gov/climo/reports/today.csv) |
| 华盛顿 VAAC 火山灰通告 | `gaia.volcanic.ash.read@v1` · `noaa-vaac-ash-01` | `volcanic_ash` | 已发布的火山灰多边形显示高度范围、签发时间以及观测时间或预报有效时间。通告在下一次预定签发时间或签发七小时后失效，以较早者为准。 | [NOAA Washington VAAC](https://ospo.noaa.gov/products/atmosphere/vaac/messages.html) · [NOAA 使用条款](https://www.noaa.gov/office-education/outreach-communication/faq) |
| Digital Earth Australia 热点 | `gaia.fire.hotspots.read@v1` · `dea-hotspots-01` | `fire_hotspots` | 在查询范围内显示最近三小时的澳大利亚卫星热异常，包括 Himawari-9 的探测结果。地图最多加载 800 个探测点，并标明覆盖是否完整。 | [Geoscience Australia · CC BY 4.0](https://knowledge.dea.ga.gov.au/data/product/dea-hotspots/) · [DEA Hotspots 数据集](https://pid.geoscience.gov.au/dataset/ga/111881) |

不列颠哥伦比亚省数据附有署名“Contains information licensed under the Open Government Licence – British Columbia.”。澳大利亚浮标数据附有署名“Data was sourced from Australia’s Integrated Marine Observing System (IMOS) - IMOS is enabled by the National Collaborative Research Infrastructure Strategy (NCRIS)”。
