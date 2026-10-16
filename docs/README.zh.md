# GAIA — 物理预言机网关

<!-- sensor-operator-guide -->
> 你有气象站或空气质量传感器吗？[通过自己的 Hub + GAIA 销售读数](https://github.com/alexar76/aicom/blob/main/docs/sensor-operator.zh.md)。
<!-- /sensor-operator-guide -->

<!-- aicom-readme-badges -->
<p align="center">
  <a href="https://github.com/alexar76/gaia/actions/workflows/ci.yml"><img src="../docs/badges/ci.svg" alt="CI" /></a>
  <a href="https://iot.modelmarket.dev/"><img src="../docs/badges/demo.svg" alt="Live demo" /></a>
  <a href="https://alexar76.github.io/gaia/"><img src="../docs/badges/landing.svg" alt="Landing" /></a>
  <img src="../docs/badges/python.svg" alt="Python >=3.11" />
  <img src="../docs/badges/aimarket.svg" alt="AIMarket v2" />
  <img src="../docs/badges/signing.svg" alt="Ed25519 signing" />
  <a href="../LICENSE"><img src="../docs/badges/license.svg" alt="License: MIT" /></a>
</p>
<!-- /aicom-readme-badges -->

<p align="center">
  <a href="https://iot.modelmarket.dev/">
    <img src="assets/hero.svg" alt="GAIA — 读数离开物理站点，在源端用 Ed25519 签名，作为 AIMarket v2 能力售出，并由 /v1/verify 结算：可信则支付提供方，不可信则退款给买方" width="100%" />
  </a>
</p>

> 🌐 [English](../README.md) · [Русский](README.ru.md) · [Español](README.es.md) · [Français](README.fr.md) · **中文** · [术语表](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md)

**GAIA** 是**物理预言机**网关：把虚拟 IoT 设备上经 Ed25519 **证明（attestation）** 的传感器
**读数**作为 AIMarket v2 付费 capability 出售，并提供与 Metis envelope 兼容的 `/v1/verify`，
以便 Hub 通过 **Pay-on-Verified（验证后付款）** **托管（escrow）** 完成**结算** —
诚实读数支付给**提供方**，说谎传感器自动退款给买方。

它是生态的**第三类预言机**：数学预言机证明计算，Metis 评判 LLM 输出，GAIA 把结算锚定在物理世界。
演示**机队**是仿真的（两座共址气象站共享同一 site truth、空气质量节点、电表 — 模型仿
BME280/SDS011/SCD30/Shelly-EM），但每条线面 — 清单、**调用（invoke）**、**收据**、提供方签名、
verify envelope、W3C WoT Thing Descriptions — 都是真实协议面。

<p align="center">
  <strong><a href="https://iot.modelmarket.dev/">在线演示</a></strong>
  ·
  <strong><a href="https://alexar76.github.io/gaia/">落地页</a></strong>
  ·
  <strong><a href="https://github.com/alexar76/gaia/pkgs/container/gaia">GHCR</a></strong>
</p>

> 📖 深读（单体仓）：[`docs/iot-physical-oracles.md`](https://github.com/alexar76/aicom/blob/main/docs/iot-physical-oracles.md)
> 🌍 在线设备：[`gaia/devices/live.py`](../gaia/devices/live.py)
> 🎬 3D：[`frontend/`](../frontend/) (`cd frontend && npm i && npm run dev`)

## 快速开始

```bash
docker pull ghcr.io/alexar76/gaia:latest
docker run --rm -p 9320:9320 ghcr.io/alexar76/gaia:latest

pip install -e vendor/oracle-core -e ".[dev]"
python -m gaia.main                             # :9320
```

探测：

```bash
curl -s localhost:9320/.well-known/ai-market.json
curl -s localhost:9320/ai-market/v2/manifest

curl -s -X POST localhost:9320/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -d '{"capability_id": "gaia.weather.read@v1", "product_id": "gaia.gateway",
       "input": {"device_id": "ws-01"}}'
```

## 在线设备 — 公共传感器中继

设置 `GAIA_ENABLE_LIVE=1` 可在仿真器旁注册真实公共 API **中继（relay）**。
每个中继走同一套 Ed25519 证明与**合理性（plausibility）**路径。上游不可达 →
`DeviceOffline` → 503 / `{ok:false}` → **不扣款**。上游主机在 **allowlist** 中（防 SSRF）；
invoke 客户端**永不**提供 URL。

| Device id | Capability | Upstream | 密钥？ |
|-----------|------------|----------|------|
| `nws-01` | `gaia.weather.read@v1` | NOAA/NWS `api.weather.gov` | 否 |
| `om-wx-01` | `gaia.weather.read@v1` | [Open-Meteo](https://open-meteo.com) weather | 否 |
| `osm-01` | `gaia.air.read@v1` | openSenseMap | 否 |
| `om-aq-01` | `gaia.air.read@v1` | Open-Meteo air quality | 否 |
| `sta-01` | `gaia.air.read@v1` | OGC SensorThings (Fraunhofer) | 否 |
| `openaq-01` | `gaia.air.read@v1` | OpenAQ v3 | **是 — 免费**（[explore.openaq.org](https://explore.openaq.org)）→ `GAIA_OPENAQ_API_KEY` |
| `uk-grid-01` | `gaia.grid.read@v1` | UK Carbon Intensity | 否 |
| `usgs-quake-01` | `gaia.quake.read@v1` | USGS GeoJSON feed | 否 |
| `noaa-tide-01` | `gaia.tide.read@v1` | NOAA CO-OPS tides | 否 |
| `firms-fire-01` | `gaia.fire.read@v1` | NASA FIRMS VIIRS (cite NASA) | 否 |
| `safecast-01` | `gaia.radiation.read@v1` | Safecast (CC0) | 否 |
| `cybernews-jam-01` | `gaia.jamming.read@v1` | CyberNews GNSS (CC BY 4.0) | 否 |
| `feeder-adsb-01` | `gaia.adsb.read@v1` | Own dump1090 ingest | `GAIA_FEEDER_*` |
| `feeder-ais-01` | `gaia.ais.read@v1` | Own AIS ingest | `GAIA_FEEDER_*` |

运维说明（5 种语言）：[`docs/LIVE-RELAYS.md`](LIVE-RELAYS.md)。
添加传感器 / 标记（GAIA → ATLAS）：[`add-gaia-atlas-sensor`](https://github.com/alexar76/aicom/blob/main/docs/add-gaia-atlas-sensor.md)（EN · RU · ES · FR · ZH）。

```bash
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.grid.read@v1","product_id":"gaia.gateway",
       "input":{"device_id":"uk-grid-01"}}'
```

## 不止读取 — 地点、电价、影像、设备控制

| 能力 | 价格 | 作用 | 指南 |
|---|---|---|---|
| `gaia.place.resolve@v1` | 免费 | 按名称查找任意居民点（「Kraków」「Paris, Texas」「東京」）→ 一个地点，名称有歧义时给出候选；说明哪些 GAIA 读数覆盖该点（GeoNames，CC BY 4.0） | [地点](i18n/PLACES.zh.md) |
| `gaia.power.price.read@v1` | $0.001 | 欧洲某竞价区的日前电价，当前及未来 24 小时（SMARD，CC BY 4.0；15 个区） | [电价](i18n/POWER-PRICES.zh.md) |
| `gaia.power.window@v1` | $0.002 | N 小时内何时用电最便宜——附所用电价及其 SHA-256（2026-10-16，一次 11 kW 电动车充电从 12:30 开始比从 08:40 开始便宜 38%） | [电价](i18n/POWER-PRICES.zh.md) |
| `gaia.imagery.read@v1` | $0.005 | 任意陆地点最新的无云 Sentinel-2 影像：NDVI、NDWI、NBR、NDMI | [影像](i18n/IMAGERY.zh.md) |
| `gaia.actuator.list@v1` | 免费 | 运营方可切换的设备及其限值 | [设备控制](i18n/ACTUATION.zh.md) |
| `gaia.actuate@v1` | $0.05 | 将公开演示插座打开 N 秒或关闭——**只为其电表测得的效果付费**（Pay-on-Verified）。真实插座只听所有者的 | [设备控制](i18n/ACTUATION.zh.md) |
| `gaia.owner.command@v1` | 免费 | 所有者（或受托方）用自己的钱包签名（EIP-712）切换自己的插座——无需 hub，无需付费 | [设备控制](i18n/ACTUATION.zh.md#谁可以切换插座) |
| `gaia.owner.consumption@v1` | 免费 | 所有者读取自己插座按小时或按天的用电，由插座密钥签名 | [设备控制](i18n/ACTUATION.zh.md#电量统计) |
| `gaia.consumption.read@v1` | $0.01 | 插座的电量统计，**仅限所有者设置出售的插座**，按完整、已结束的小时或日 | [设备控制](i18n/ACTUATION.zh.md#电量统计) |

## 许可

MIT — 见 [LICENSE](../LICENSE)。
