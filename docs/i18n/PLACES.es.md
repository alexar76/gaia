# Lugares — cualquier localidad por su nombre

> 🌐 [English](../PLACES.md) · [Русский](PLACES.ru.md) · **Español** · [Français](PLACES.fr.md) · [中文](PLACES.zh.md)

El modelo de un comprador pide «el aire en Cracovia» o «Paris, Texas»; nunca ha visto una
coordenada ni un id de dispositivo. GAIA resuelve el nombre con el nomenclátor **GeoNames** —todos
los lugares de 1000 habitantes o más (171 306), con nombres de países en 16 idiomas del Unicode
CLDR— y es honesto cuando un nombre es ambiguo en vez de adivinar.

## `gaia.place.resolve@v1` — gratis

```bash
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.place.resolve@v1","input":{"query":"Paris, Texas"}}'
```

Devuelve el lugar (nombre, región, país, coordenadas, población, zona horaria, id de GeoNames), los
demás candidatos, la compilación del nomenclátor que respondió (con su SHA-256) y **qué lecturas
de GAIA cubren el punto** —el relé meteorológico más cercano y su distancia, y la calidad del aire
en la coordenada exacta—, para que la siguiente llamada sea una compra y no una suposición. Con
`latitude` + `longitude` en lugar de `query` responde con la localidad más cercana (se omiten
barrios como «Mitte»).

## Las reglas

| Entrada | Resultado |
|---|---|
| `Kraków`, `Krakow`, `Краков` | Kraków, Lesser Poland, PL |
| `Paris` | Paris, Île-de-France, FR — el mayor solo se elige si es al menos 5 veces el siguiente |
| `Paris, Texas` · `Paris, TX` | Paris, Texas, US |
| `Toronto, ON` · `Sydney, NSW` | funcionan las abreviaturas de estados y provincias |
| `Москва, Россия` · `Kraków, Polonia` | el país tras la coma en cualquiera de 16 idiomas |
| `東京` · `Sài Gòn` · `Kiev` · `Cologne` | los nombres alternativos e históricos encuentran el lugar |
| `Springfield` · `Cambridge` · `Victoria` | **ambiguo** — se rechaza con los candidatos; añada el estado o el país |
| `Paris, Narnia` | rechazado: el calificativo no nombra ningún país ni región |

Un nombre alternativo cuenta veinte veces menos que uno oficial, así que el nombre histórico de
Hong Kong, «Victoria», ya no se impone a las ciudades que realmente se llaman Victoria.

## En las lecturas de tiempo y aire

`gaia.weather.read@v1` y `gaia.air.read@v1` aceptan los mismos nombres en su campo `city`. Las
ciudades que GAIA retransmite siguen coincidiendo primero y leen sus propios relés; cualquier otro
lugar va al nomenclátor. La calidad del aire se lee entonces en esa coordenada exacta. El tiempo se
lee del relé activo más cercano a menos de 75 km, o se rechaza indicando el relé más cercano y su
distancia: «Paris, Texas» ya no es «una ciudad que GAIA no retransmite», es un lugar a 1148 km del
relé más cercano (Chicago). La respuesta indica de dónde vino el lugar (`place_source`:
`gaia-relay-list` o `geonames`) y lleva la atribución de GeoNames.

## Operadores

El nomenclátor es un único archivo SQLite (36 MB) que se compila al construir la imagen con
`python -m gaia.gazetteer build --out PATH`, en una etapa de Docker propia, de modo que los cambios
normales de código no vuelven a descargar GeoNames; suba `GAZETTEER_EPOCH` para actualizar los
datos. GAIA lo abre en solo lectura desde `GAIA_GAZETTEER_PATH`. Sin él, GAIA resuelve solo las
ciudades que retransmite, como antes. Es un archivo en la imagen a propósito: datos de referencia
de solo lectura versionados con el código, sin base de datos que operar, idénticos en desarrollo
y producción.

Licencia: GeoNames (geonames.org) · CC BY 4.0; nombres de países: Unicode CLDR (Unicode License).
