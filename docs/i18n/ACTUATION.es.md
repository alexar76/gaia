# Control de dispositivos — se paga solo el efecto medido

> 🌐 [English](../ACTUATION.md) · [Русский](ACTUATION.ru.md) · **Español** · [Français](ACTUATION.fr.md) · [中文](ACTUATION.zh.md)

Leer el mundo físico es la mitad de lo que un agente necesita de él. La otra mitad es cambiarlo:
encender un calefactor, una bomba o un cargador de coche eléctrico durante un tiempo. GAIA lo
vende como `gaia.actuate@v1`, con una regla que hace seguro comprárselo a un desconocido:

**el precio de una orden solo se debe cuando su efecto se ha medido.**

Un actuador es un **enchufe inteligente con medición**: el relé que GAIA conmuta y el medidor que
solo ve lo que ese relé conmuta. «Relé encendido» y «la potencia subió» son entonces el mismo
hecho físico. Un medidor de toda la casa no serviría: un hervidor en otra habitación «probaría»
que el calefactor se encendió.

## Qué se puede controlar

GAIA conmuta un solo tipo de dispositivo: un **enchufe inteligente con medición**. En la
práctica controla lo que esté enchufado a él, y hay exactamente dos órdenes: `on` durante un
número limitado de segundos, y `off`. Sin regulación de intensidad, sin consignas de
temperatura, sin horarios: un horario es simplemente el comprador llamando en el momento que
elija, por ejemplo al inicio de la ventana que encontró `gaia.power.window@v1`.

### Qué aparatos sirven

Una orden solo se paga cuando el medidor ve su efecto, así que el aparato tiene que **consumir
una carga estable pocos segundos después de recibir corriente**, y casi nada una vez apagado.

| Detrás del enchufe | ¿Sirve? | Por qué |
|---|---|---|
| Calefactor eléctrico, radiador de aceite, toallero | sí | 0,5–2,5 kW estables en cuanto recibe corriente |
| Termo eléctrico enchufado | mientras calienta | cuando el agua ya está caliente su termostato corta; un `on` entonces no muestra efecto y se rechaza sin coste |
| Cable de carga de coche eléctrico para enchufe doméstico, cargador de bicicleta o patinete eléctrico | sí, dentro de la capacidad del enchufe y del cable | la batería tiene que cargar de verdad: una llena no consume nada y la orden se rechaza |
| Bomba, ventilador, deshumidificador | sí | si su consumo supera el `expect_on_w` del operador |
| Lámpara, luz de cultivo | sí | con un `expect_on_w` bajo, aún muy por encima de `off_max_w` |
| Frigorífico, congelador, cualquier aparato con termostato propio, modo de espera o arranque suave lento | mal | «encendido» puede no consumir nada durante minutos, así que la mayoría de las órdenes se rechazan |
| Cargas trifásicas o de conexión fija, cualquier cosa por encima de 16 A | no | un enchufe: como mucho 230 V × 16 A = 3680 W |
| Cerraduras, válvulas de gas, equipos médicos o de seguridad | **nunca** | nada cuyo encendido o apagado inesperado pueda dañar a alguien cabe en un interruptor de pago |

Un `on` cuyo efecto no se mide **se vuelve a apagar enseguida** y no cuesta nada: un aparato poco
adecuado le hace perder tiempo al comprador, pero nunca dinero.

```mermaid
flowchart TD
    A["Un aparato cuya conmutación quiere vender"] --> S{"¿Es seguro si se enciende o se apaga de forma inesperada?"}
    S -- no --> N1["Nunca: cerraduras, gas, equipos médicos o de seguridad"]
    S -- sí --> W{"¿Un enchufe normal, como mucho 3680 W?"}
    W -- no --> N2["No compatible: trifásico, conexión fija, más de 16 A"]
    W -- sí --> D{"¿Consumo estable segundos después de encenderlo?"}
    D -- no --> N3["Poco adecuado: termostato, espera o arranque suave hacen fallar la mayoría de las órdenes, sin coste"]
    D -- sí --> P{"¿Detrás de un enchufe Tasmota con chip de medición que solo mide esta toma?"}
    P -- no --> N4["Use un enchufe así: Shelly, Zigbee, Matter y los enchufes en la nube aún no son compatibles"]
    P -- sí --> OK["Sirve: inclúyalo en su archivo de actuadores"]
```

### Qué enchufes maneja GAIA

| `kind` en el archivo de actuadores | Qué es |
|---|---|
| `tasmota-mqtt` | Un enchufe real con el firmware [Tasmota](https://tasmota.github.io/), accesible a través del broker MQTT propio del operador |
| `sim-plug` | Un enchufe y un aparato en software (`load_w` de 1 a 3680 W). La demostración pública `plug-sim-01` en iot.modelmarket.dev es de este tipo; cada respuesta suya lleva `"simulated": true` |

Un enchufe Tasmota sirve cuando:

1. **Tiene un chip de medición de energía.** La página principal de Tasmota muestra Voltage,
   Current y Power, y `Status 8` responde con un bloque `ENERGY`. Un enchufe sin él no puede
   probar nada, así que toda orden se rechazaría.
2. **Su medidor solo ve la toma conmutada.** GAIA conmuta `Power1`. Una regleta que mide varias
   tomas juntas no sirve: otra toma «probaría» la orden.
3. **Está en el broker MQTT del operador** (`mqtt://` o `mqtts://`) con su propio `%topic%`.
   GAIA solo le habla allí, y quien llama nombra un id de actuador, nunca un topic ni un broker.
4. **Está dimensionado para la carga.** Las cargas largas cerca de 16 A calientan los enchufes
   domésticos; respete la capacidad del enchufe y, para cargar un coche eléctrico, la del cable.

Todo enchufe Tasmota tiene `PulseTime`, su propio temporizador de apagado. GAIA lo envía con el
`on`, de modo que el enchufe se apaga solo al terminar el tiempo pagado aunque GAIA, el broker o
la red hayan desaparecido. Ese temporizador admite como mucho 64 800 s, así que un `on` dura como
máximo 18 horas.

Aún no son compatibles: Shelly, Zigbee, Matter, Home Assistant y los enchufes que solo funcionan
en la nube, los enchufes con varios relés por medidor, los reguladores de intensidad, los
termostatos y las válvulas. Cada uno necesita su propio controlador y su propia forma de probar
el efecto.

### Qué decide el operador para cada actuador

| Campo | Significado | Regla (por defecto) |
|---|---|---|
| `expect_on_w` | potencia medida que prueba «encendido» | por debajo del consumo estable del aparato, por encima de lo que el enchufe consume en reposo |
| `off_max_w` | potencia medida que prueba «apagado» | `0 < off_max_w < expect_on_w` (10 W) |
| `min_on_s`, `max_on_s` | cuánto puede durar un `on` | `0 < min ≤ max ≤ 64800` (10 s, 900 s) |
| `cooldown_s` | pausa entre dos órdenes al mismo actuador | `≥ 0` (30 s) |
| `effect_timeout_s` | cuánto espera GAIA a que aparezca el efecto | 1–120 s (20 s) |
| `latitude`, `longitude` | opcional: dónde lo muestra ATLAS | — |

Con el control de dispositivos activado, un archivo de actuadores que incumpla estas reglas
detiene GAIA al arrancar, en lugar de poner en marcha un interruptor cuyo «encendido» y
«apagado» podrían parecerse. Un enchufe bien descrito al que no se puede llegar se omite y queda
registrado, y el resto de GAIA sigue funcionando.

## Capacidades

| Capacidad | Precio | Qué hace |
|---|---|---|
| `gaia.actuator.list@v1` | gratis | Los actuadores del operador: órdenes, límites de duración, pausa entre órdenes, estado del relé, claves fijadas de los dispositivos y si el interruptor de emergencia está activado |
| `gaia.actuate@v1` | $0.05 | Encender un actuador (`on`) durante `duration_s` segundos —después se apaga solo— o apagarlo (`off`). El resultado es el paquete de pruebas |

$0.05 es el precio mínimo de Pay-on-Verified en el hub: el comprador puede retener el pago en
custodia (escrow) hasta que `/v1/verify` de GAIA confirme el efecto.

## Cómo se liquida una orden pagada

```mermaid
sequenceDiagram
    autonumber
    participant B as Agente comprador
    participant H as Hub AIMarket
    participant G as GAIA
    participant P as Enchufe con medición
    B->>H: invoke gaia.actuate@v1 {actuator_id, command: on, duration_s} + bloque verify
    H->>G: invoke (firma de respuesta X-Provider-Signature obligatoria)
    G->>G: controles de seguridad (activado por el operador, emergencia, registro, límites, pausa, una a la vez)
    G->>P: lectura del medidor → lectura firmada ANTES
    G->>G: firma del registro de la orden (actuador, orden, duración, efecto esperado, seq, nonce, hora)
    G->>P: conmutar (Tasmota: PulseTime + Power ON — el temporizador de apagado del propio enchufe)
    loop hasta que se vea el efecto o se cierre la ventana
        G->>P: lectura del medidor → lectura firmada DESPUÉS
    end
    alt efecto medido
        G-->>H: paquete de pruebas (orden + antes + después + efecto)
        H->>H: el escrow retiene $0.05
        H->>G: POST /v1/verify (el paquete)
        G->>G: evaluate(): clave fijada, mismo dispositivo, orden en el tiempo, ventana, efecto, física, no es una repetición
        G-->>H: verificado
        H->>H: cobro — el proveedor recibe el pago, evento de reputación verify_passed
        H-->>B: resultado + recibo liquidado
    else sin efecto
        G->>P: volver a apagar (un encendido no probado nunca queda encendido)
        G-->>H: ok:false — id de las pruebas en el error
        H-->>B: rechazo — no se cobra nada
    end
```

Sin bloque `verify` el efecto sigue decidiendo el cobro: ante un efecto no probado GAIA responde
`ok:false`, y el hub no cobra nada por `ok:false`. El bloque `verify` añade una segunda
comprobación antes de que se mueva el dinero: el hub envía las pruebas al verificador propio de
GAIA, que las vuelve a comprobar con exactitud (abajo); un hub enruta `gaia.actuate@v1` hacia él con
`AIMARKET_VERIFY_ROUTES` en lugar de a su jurado de LLM.

## Controles de seguridad

```mermaid
flowchart TD
    A["gaia.actuate@v1"] --> B{GAIA_ACTUATION_ENABLED=1?}
    B -- no --> X1[capacidad no anunciada]
    B -- sí --> C{¿interruptor de emergencia activado?<br/>variable, archivo o API del operador}
    C -- sí --> X2[ok:false — no se cobra nada]
    C -- no --> D{¿actuador en el YAML del operador?}
    D -- no --> X3[ok:false — lista los actuadores]
    D -- sí --> E{¿orden on/off,<br/>duración dentro de los límites?}
    E -- no --> X4[ok:false — indica los límites]
    E -- sí --> F{¿pausa cumplida,<br/>libre?}
    F -- no --> X5[ok:false — reintentar en N s]
    F -- sí --> G[antes → orden → después → evaluate]
    G --> H{¿efecto medido?}
    H -- sí --> OK[paquete de pruebas — facturable]
    H -- no --> OFF[volver a apagar · ok:false · no se cobra nada]
```

Quien llama nombra un **id de actuador** — nunca un tema MQTT, un broker ni un dispositivo que el
operador no haya listado. «Encender» siempre está acotado: la duración es obligatoria y el
operador fija su máximo.

## Cómo llamarlo

```bash
# qué se puede conmutar y con qué límites
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.actuator.list@v1","input":{}}'

# encender el calefactor de demostración un minuto
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.actuate@v1",
       "input":{"actuator_id":"plug-sim-01","command":"on","duration_s":60}}'
```

La respuesta (resumida):

```json
{
  "actuation": {
    "record": {"command_id": "act_1f…", "actuator_id": "plug-sim-01", "command": "on",
               "duration_s": 60, "seq": 1792145220000, "issued_at": "2026-10-16T10:07:00Z",
               "nonce": "…", "expect": {"field": "power_w", "op": ">=", "value": 1000, "within_s": 20}},
    "attestation": {"algorithm": "ed25519", "public_key": "…", "value": "…"}
  },
  "before": {"reading": {"values": {"power_w": 0.4, "relay_on": 0.0, "…": "…"}}, "attestation": {"…": "…"}},
  "after":  {"reading": {"values": {"power_w": 1497.2, "relay_on": 1.0, "…": "…"}}, "attestation": {"…": "…"}},
  "effect": {"verified": true, "expected": {"…": "…"}, "observed": 1497.2},
  "simulated": true,
  "auto_off_at": "2026-10-16T10:08:00Z"
}
```

Cualquiera puede comprobarlo de nuevo: `POST /v1/verify` con el paquete (la ranura del escrow),
o invocar `gaia.verify@v1` con él. Las pruebas de las órdenes recientes, también de las
rechazadas, son públicas en `GET /actuation/v1/evidence/<command_id>`; `GET /actuation/v1/status`
muestra todos los actuadores.

## Qué comprueba el veredicto

Todas las comprobaciones son duras: un solo hecho falso y no se debe nada.

| Comprobación | Significado |
|---|---|
| `command_signature` | el registro de la orden está firmado con la clave **fijada** del enchufe (del registro de la flota, nunca la que trae el paquete) |
| `before_device`, `after_device` | ambas lecturas vienen del mismo actuador |
| `before_signature`, `after_signature` | ambas lecturas se verifican con la clave fijada |
| `order` | antes → orden → después, por número de secuencia y por tiempo |
| `window` | el efecto se leyó dentro de la ventana prometida |
| `effect` | la condición esperada se cumple en la lectura posterior (`power_w >= expect_on_w` para encender, `<= off_max_w` para apagar) |
| `relay_state` | el enchufe informa el estado del relé que pidió la orden |
| `after_plausible` | la lectura posterior pasa los controles físicos de GAIA |

Cada orden se liquida una sola vez: su `seq` (milisegundos de reloj, siempre creciente) es la
marca máxima del verificador del escrow, así que un paquete repetido se rechaza.

La plausibilidad conoce los enchufes: `relay_on` es un campo; un relé abierto que mide más de
25 W es una contradicción; y el contador de energía se compara con lo que un enchufe de 16 A
pudo dejar pasar desde la última lectura, no con potencia actual por tiempo: un enchufe
conmutado entre dos lecturas es honesto.

## Operadores

La demostración pública funciona con un enchufe **simulado** (`plug-sim-01`, un calefactor de
1500 W) desde [`config/actuators.yaml`](../../config/actuators.yaml). Para hardware real:

1. Un enchufe Tasmota **con chip de energía**, en su broker MQTT, que responda a `Status 8`.
2. Su archivo de actuadores, a partir de [`config/actuators.example.yaml`](../../config/actuators.example.yaml):
   ```yaml
   actuators:
     - id: lab-heater-01
       kind: tasmota-mqtt
       topic: tasmota_lab_heater   # el %topic% del enchufe
       expect_on_w: 800            # por debajo del consumo del calefactor, por encima del reposo del enchufe
       off_max_w: 5
       min_on_s: 30
       max_on_s: 1800
       cooldown_s: 60
       effect_timeout_s: 20
   ```
3. Entorno:
   ```bash
   GAIA_ACTUATION_ENABLED=1
   GAIA_ACTUATORS_FILE=/path/to/actuators.yaml
   GAIA_MQTT_BROKER=mqtts://broker.example:8883
   GAIA_MQTT_USERNAME=gaia            # la contraseña, de su almacén de secretos, nunca en un archivo de git
   GAIA_ACTUATION_TOKEN=…             # token Bearer para la API del interruptor de emergencia
   ```
4. **El hub que la vende con Pay-on-Verified** enruta la capacidad al verificador de esta GAIA, para
   que el dinero lo decida una comprobación exacta y no un jurado de LLM:
   ```
   # GAIA: en producción /v1/verify rechaza a todos hasta que esto esté definido
   GAIA_VERIFY_TOKEN=…                # un secreto aleatorio, desde un archivo 0600
   # archivo de entorno del hub (una línea, sin comillas: --env-file toma los valores literalmente)
   AIMARKET_VERIFY_ROUTES={"gaia.actuate@v1": {"url": "https://<gaia>", "verifier_id": "gaia.verify@v1", "key_env": "AIMARKET_VERIFY_GAIA_TOKEN"}}
   AIMARKET_VERIFY_GAIA_TOKEN=…       # el mismo valor que GAIA_VERIFY_TOKEN
   ```
   Sin el token en el hub, el hub rechaza el bloque `verify` antes de conmutar nada; con uno
   incorrecto, GAIA responde 401 y se reembolsa al comprador. Véanse
   [las rutas de verificador del hub](https://github.com/alexar76/aimarket-hub/blob/main/docs/pay-on-verified.md#verifier-routes).

El encendido se envía como `Backlog PulseTime1 <duración>; Power1 ON`, así que **el enchufe se
apaga solo** al terminar la duración pagada aunque GAIA, el broker o la red desaparezcan. GAIA
además envía su propio OFF al vencer el plazo, como segundo freno.

### Interruptor de emergencia

Tres vías, basta cualquiera; mientras está activado toda orden se rechaza y no se cobra nada:

```bash
curl -X POST https://<gaia>/actuation/v1/kill   -H "Authorization: Bearer $GAIA_ACTUATION_TOKEN"   # además apaga todo
curl -X POST https://<gaia>/actuation/v1/resume -H "Authorization: Bearer $GAIA_ACTUATION_TOKEN"
touch <directorio de datos>/ACTUATION_KILL   # un archivo, para cuando la API no responde
GAIA_ACTUATION_KILL=1                        # una variable de entorno, para un reinicio
```

Sin `GAIA_ACTUATION_TOKEN` la API del interruptor responde 503: nunca se abre sin autenticación.
Cada orden, resultado, parada y reanudación se añade a `actuation.jsonl` en el directorio de datos.

## Límites honestos

- Un enchufe Tasmota no tiene elemento seguro. La clave de GAIA firma **lo que informó el
  enchufe** — custodia del informe, como en todo relé de GAIA, no prueba de que el hardware no
  pueda mentir. Que medidor y relé sean un solo dispositivo es lo que mantiene la afirmación acotada.
- El efecto prueba que pasó potencia por el enchufe, no qué hizo el aparato con ella.
- El enchufe de la demostración pública es simulado (`SIM`): su calefactor solo existe en software, y cada respuesta lo indica (`"simulated": true`).
- Con la ruta, GAIA es a la vez vendedor y juez de `gaia.actuate@v1`. Es aceptable solo porque el
  veredicto es una comprobación determinista de pruebas firmadas que cualquiera puede repetir
  (`POST /v1/verify`, o `gaia.verify@v1` con el paquete), y porque un comprador puede apelar un
  aprobado ante el tribunal de apelación del hub —otro verificador— cuando el hub tiene uno. Ningún
  verificador, ni GAIA ni un LLM, detecta pruebas falsificadas por quien tenga la clave del enchufe.

W3C WoT: la Thing Description de cada enchufe (`GET /wot/<id>`) incluye las **acciones**
`switchOn` / `switchOff`, cuyos formularios son invocaciones pagadas de `gaia.actuate@v1`.
