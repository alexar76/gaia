# Places — any populated place by name

> 🌐 **English** · [Русский](i18n/PLACES.ru.md) · [Español](i18n/PLACES.es.md) · [Français](i18n/PLACES.fr.md) · [中文](i18n/PLACES.zh.md)

A buyer's model asks for "air in Kraków" or "Paris, Texas"; it has never seen a coordinate or a
device id. GAIA resolves the name from the **GeoNames** gazetteer — every place of 1,000+ people
(171,306 of them), with country names in 16 languages from the Unicode CLDR — and is honest when
a name is ambiguous instead of guessing.

## `gaia.place.resolve@v1` — free

```bash
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.place.resolve@v1","input":{"query":"Paris, Texas"}}'
```

Returns the place (name, region, country, coordinates, population, time zone, GeoNames id), the
other candidates, the gazetteer build that answered (with its SHA-256), and **which GAIA reads
cover the point** — the nearest weather relay and its distance, and air quality at the exact
coordinate — so the next call is a purchase, not a guess. With `latitude` + `longitude` instead
of `query` it answers the nearest populated place (city sections such as "Mitte" are skipped).

## The rules

| Input | Result |
|---|---|
| `Kraków`, `Krakow`, `Краков` | Kraków, Lesser Poland, PL |
| `Paris` | Paris, Île-de-France, FR — the largest is chosen only when it is at least 5× the next one |
| `Paris, Texas` · `Paris, TX` | Paris, Texas, US |
| `Toronto, ON` · `Sydney, NSW` | state and province abbreviations work |
| `Москва, Россия` · `Kraków, Polonia` | the country after the comma in any of 16 languages |
| `東京` · `Sài Gòn` · `Kiev` · `Cologne` | alternate and historic names find the place |
| `Springfield` · `Cambridge` · `Victoria` | **ambiguous** — refused with the candidates; add the state or country |
| `Paris, Narnia` | refused: the qualifier names no country or state |

An alternate name counts twenty times less than an official one, so Hong Kong's historic name
"Victoria" no longer outranks the cities actually called Victoria.

## In weather and air reads

`gaia.weather.read@v1` and `gaia.air.read@v1` accept the same names in their `city` field. The
cities GAIA relays still match first and read their own relays; any other place goes to the
gazetteer. Air quality is then read at that exact coordinate. Weather is read from the nearest
live relay within 75 km, or refused with the nearest relay and its distance — "Paris, Texas" is
no longer "not a city GAIA relays", it is a place 1,148 km from the nearest relay (Chicago). The answer
says where the place came from (`place_source`: `gaia-relay-list` or `geonames`) and carries the
GeoNames attribution.

## Operators

The gazetteer is one SQLite file (36 MB) built at image build time by
`python -m gaia.gazetteer build --out PATH`, in a Docker stage of its own, so ordinary code
changes do not fetch GeoNames again; raise `GAZETTEER_EPOCH` to refresh the data. GAIA opens it
read-only from `GAIA_GAZETTEER_PATH`. Without it GAIA resolves only the cities it relays, exactly
as before. It is a file in the image on purpose: read-only reference data versioned with the
code, no database to run, identical in development and production.

Licence: GeoNames (geonames.org) · CC BY 4.0; country names: Unicode CLDR (Unicode License).
