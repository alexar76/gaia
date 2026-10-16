# 地点 — 按名称查找任何居民点

> 🌐 [English](../PLACES.md) · [Русский](PLACES.ru.md) · [Español](PLACES.es.md) · [Français](PLACES.fr.md) · **中文**

买方的模型会问「克拉科夫的空气」或「Paris, Texas」——它从未见过坐标或设备 ID。GAIA 用 **GeoNames**
地名库解析名称——所有人口 1000 以上的地点（共 171,306 个），国家名称来自 Unicode CLDR 的 16 种
语言——遇到有歧义的名称时如实说明，而不是猜测。

## `gaia.place.resolve@v1` — 免费

```bash
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.place.resolve@v1","input":{"query":"Paris, Texas"}}'
```

返回地点（名称、地区、国家、坐标、人口、时区、GeoNames ID）、其他候选、作答的地名库版本（含其
SHA-256），以及**哪些 GAIA 读数覆盖该点**——最近的天气中继及其距离、该精确坐标处的空气质量——
让下一次调用是一次购买，而不是猜测。用 `latitude` + `longitude` 代替 `query` 时，返回最近的
居民点（会跳过「Mitte」这类城区）。

## 规则

| 输入 | 结果 |
|---|---|
| `Kraków`、`Krakow`、`Краков` | Kraków, Lesser Poland, PL |
| `Paris` | Paris, Île-de-France, FR——只有当最大的候选至少是第二名的 5 倍时才选它 |
| `Paris, Texas` · `Paris, TX` | Paris, Texas, US |
| `Toronto, ON` · `Sydney, NSW` | 支持州和省的缩写 |
| `Москва, Россия` · `Kraków, Polonia` | 逗号后的国家可以是 16 种语言中的任意一种 |
| `東京` · `Sài Gòn` · `Kiev` · `Cologne` | 别名和历史名称都能找到地点 |
| `Springfield` · `Cambridge` · `Victoria` | **有歧义**——拒绝并给出候选；请加上州或国家 |
| `Paris, Narnia` | 拒绝：限定词既不是国家也不是地区 |

别名的权重是正式名称的二十分之一，因此香港的历史名称「Victoria」不再压过真正名为 Victoria 的城市。

## 在天气和空气读数中

`gaia.weather.read@v1` 和 `gaia.air.read@v1` 的 `city` 字段接受同样的名称。GAIA 中继的城市仍然
优先匹配，并读取它们自己的中继；其他任何地点交给地名库。随后空气质量在该精确坐标处读取。天气则
读取 75 公里内最近的在线中继，否则拒绝并给出最近的中继及其距离：「Paris, Texas」不再是「GAIA
未中继的城市」，而是距最近中继（芝加哥）1,148 公里的地点。答案会说明地点来自何处（`place_source`：
`gaia-relay-list` 或 `geonames`），并附带 GeoNames 署名。

## 运营方

地名库是一个 SQLite 文件（36 MB），在构建镜像时由 `python -m gaia.gazetteer build --out PATH`
在单独的 Docker 阶段生成，因此普通的代码修改不会重新下载 GeoNames；提高 `GAZETTEER_EPOCH` 即可
刷新数据。GAIA 从 `GAIA_GAZETTEER_PATH` 以只读方式打开它。没有它时，GAIA 与以前一样只能解析其
中继的城市。把它做成镜像中的文件是有意为之：只读参考数据与代码同版本，无需运行数据库，开发与
生产完全一致。

许可：GeoNames (geonames.org) · CC BY 4.0；国家名称：Unicode CLDR（Unicode License）。
