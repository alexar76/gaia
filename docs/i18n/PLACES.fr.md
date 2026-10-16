# Lieux — toute localité par son nom

> 🌐 [English](../PLACES.md) · [Русский](PLACES.ru.md) · [Español](PLACES.es.md) · **Français** · [中文](PLACES.zh.md)

Le modèle d'un acheteur demande « l'air à Cracovie » ou « Paris, Texas » ; il n'a jamais vu de
coordonnée ni d'identifiant d'appareil. GAIA résout le nom avec le répertoire **GeoNames** — tous
les lieux de 1 000 habitants ou plus (171 306), avec les noms de pays en 16 langues issus d'Unicode
CLDR — et le dit honnêtement quand un nom est ambigu au lieu de deviner.

## `gaia.place.resolve@v1` — gratuit

```bash
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.place.resolve@v1","input":{"query":"Paris, Texas"}}'
```

Renvoie le lieu (nom, région, pays, coordonnées, population, fuseau horaire, identifiant GeoNames),
les autres candidats, la version du répertoire qui a répondu (avec son SHA-256), et **quelles
lectures GAIA couvrent le point** — le relais météo le plus proche et sa distance, la qualité de
l'air à la coordonnée exacte — pour que l'appel suivant soit un achat et non une supposition. Avec
`latitude` + `longitude` au lieu de `query`, il répond par la localité la plus proche (les
quartiers comme « Mitte » sont ignorés).

## Les règles

| Entrée | Résultat |
|---|---|
| `Kraków`, `Krakow`, `Краков` | Kraków, Lesser Poland, PL |
| `Paris` | Paris, Île-de-France, FR — le plus grand n'est choisi que s'il fait au moins 5 fois le suivant |
| `Paris, Texas` · `Paris, TX` | Paris, Texas, US |
| `Toronto, ON` · `Sydney, NSW` | les abréviations d'États et de provinces fonctionnent |
| `Москва, Россия` · `Kraków, Polonia` | le pays après la virgule dans l'une des 16 langues |
| `東京` · `Sài Gòn` · `Kiev` · `Cologne` | les noms alternatifs et historiques trouvent le lieu |
| `Springfield` · `Cambridge` · `Victoria` | **ambigu** — refusé avec les candidats ; ajoutez l'État ou le pays |
| `Paris, Narnia` | refusé : le qualificatif ne nomme aucun pays ni région |

Un nom alternatif compte vingt fois moins qu'un nom officiel, si bien que l'ancien nom de Hong
Kong, « Victoria », ne l'emporte plus sur les villes qui s'appellent vraiment Victoria.

## Dans les lectures météo et air

`gaia.weather.read@v1` et `gaia.air.read@v1` acceptent les mêmes noms dans leur champ `city`. Les
villes relayées par GAIA restent prioritaires et lisent leurs propres relais ; tout autre lieu
passe par le répertoire. La qualité de l'air est alors lue à cette coordonnée exacte. La météo est
lue au relais actif le plus proche à moins de 75 km, sinon refusée avec le relais le plus proche et
sa distance : « Paris, Texas » n'est plus « une ville que GAIA ne relaie pas », c'est un lieu à
1 148 km du relais le plus proche (Chicago). La réponse indique d'où vient le lieu
(`place_source` : `gaia-relay-list` ou `geonames`) et porte l'attribution GeoNames.

## Opérateurs

Le répertoire est un seul fichier SQLite (36 Mo) construit lors de la construction de l'image par
`python -m gaia.gazetteer build --out PATH`, dans une étape Docker à part : les modifications de
code ordinaires ne retéléchargent pas GeoNames ; augmentez `GAZETTEER_EPOCH` pour rafraîchir les
données. GAIA l'ouvre en lecture seule depuis `GAIA_GAZETTEER_PATH`. Sans lui, GAIA ne résout que
les villes qu'il relaie, comme avant. C'est volontairement un fichier dans l'image : des données de
référence en lecture seule, versionnées avec le code, sans base de données à exploiter,
identiques en développement et en production.

Licence : GeoNames (geonames.org) · CC BY 4.0 ; noms de pays : Unicode CLDR (Unicode License).
