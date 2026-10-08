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
