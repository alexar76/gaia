# Device control — pay only for the measured effect

> 🌐 **English** · [Русский](i18n/ACTUATION.ru.md) · [Español](i18n/ACTUATION.es.md) · [Français](i18n/ACTUATION.fr.md) · [中文](i18n/ACTUATION.zh.md)

Reading the physical world is half of what an agent needs from it. The other half is
changing it: turn on a heater, a pump or an EV charger for a while. GAIA sells that as
`gaia.actuate@v1`, with one rule that makes it safe to buy from a stranger:

**the price of a command is due only when the command's effect is measured.**

An actuator is a **metering smart plug**: the relay GAIA switches and the meter that sees
only what that relay switches. "Relay on" and "power rose" are then the same physical fact.
A whole-house meter would not do — a kettle somewhere else would "prove" that a heater came on.

## What can be controlled

GAIA switches one kind of device: a **metering smart plug**. What it controls in practice is
whatever is plugged into that plug, and there are exactly two commands — `on` for a bounded
number of seconds, and `off`. No dimming, no temperature setpoints, no schedules: a schedule is
simply the buyer calling at the moment it chooses, for example at the start of the window
`gaia.power.window@v1` found.

### Which appliances fit

A command is paid only when the meter sees its effect, so the appliance has to **draw a steady
load within seconds of being powered**, and draw almost nothing once it is off.

| Behind the plug | Fits? | Why |
|---|---|---|
| Electric heater, oil radiator, towel rail | yes | a steady 0.5–2.5 kW as soon as it is powered |
| Water heater on a plug | while it heats | once the water is hot its thermostat cuts out; an `on` then shows no effect and is refused, free of charge |
| EV charging cable for a household socket, e-bike or scooter charger | yes, within the plug's and the cable's rating | the battery must actually charge: a full one draws nothing, and the command is refused |
| Pump, fan, dehumidifier | yes | if its draw clears the operator's `expect_on_w` |
| Lamp, grow light | yes | with a low `expect_on_w`, still well above `off_max_w` |
| Fridge, freezer, anything with its own thermostat, standby or slow soft start | poorly | "on" may draw nothing for minutes, so most commands are refused |
| Three-phase or hard-wired loads, anything above 16 A | no | one socket: 230 V × 16 A = 3680 W at most |
| Locks, gas valves, medical or safety equipment | **never** | nothing whose unexpected on or off could hurt someone belongs on a paid switch |

An `on` whose effect is not measured is **switched straight back off** and costs nothing, so a
poor fit wastes a buyer's time but never their money.

```mermaid
flowchart TD
    A["An appliance you want to sell switching of"] --> S{"Safe if it turns on or off unexpectedly?"}
    S -- no --> N1["Never: locks, gas, medical, safety equipment"]
    S -- yes --> W{"One ordinary socket, at most 3680 W?"}
    W -- no --> N2["Not supported: three-phase, hard-wired, above 16 A"]
    W -- yes --> D{"Steady draw within seconds of power-on?"}
    D -- no --> N3["Poor fit: thermostat, standby or soft start make most commands fail, free of charge"]
    D -- yes --> P{"Behind a Tasmota plug with an energy chip that meters only this socket?"}
    P -- no --> N4["Use such a plug: Shelly, Zigbee, Matter and cloud plugs are not supported yet"]
    P -- yes --> OK["Fits: list it in your actuator file"]
```

### Which plugs GAIA drives

| `kind` in the actuator file | What it is |
|---|---|
| `tasmota-mqtt` | A real plug running [Tasmota](https://tasmota.github.io/) firmware, reached through the operator's own MQTT broker |
| `sim-plug` | A plug and an appliance in software (`load_w` from 1 to 3680 W). The public demo `plug-sim-01` on iot.modelmarket.dev is one; every answer from it carries `"simulated": true` |

A Tasmota plug qualifies when:

1. **It has an energy-monitoring chip.** Tasmota's main page shows Voltage, Current and Power,
   and `Status 8` answers with an `ENERGY` block. A plug without one cannot prove anything,
   so every command to it would be refused.
2. **Its meter sees only the switched socket.** GAIA switches `Power1`. A power strip that
   meters several outlets together does not qualify — another outlet would "prove" the command.
3. **It is on the operator's MQTT broker** (`mqtt://` or `mqtts://`) with its own `%topic%`.
   GAIA speaks to it only there, and a caller names an actuator id, never a topic or a broker.
4. **It is rated for the load.** Long loads near 16 A heat household sockets; follow the plug's
   rating and, for EV charging, the cable's.

Every Tasmota plug has `PulseTime`, the plug's own off-timer: GAIA sends it with the `on`, so
the plug switches itself off after the paid duration even when GAIA, the broker or the network
is gone. That timer holds at most 64 800 s, so one `on` lasts at most 18 hours.

Not supported yet: Shelly, Zigbee, Matter, Home Assistant and cloud-only plugs, plugs with
several relays per meter, dimmers, thermostats and valves. Each needs its own driver and its
own way to prove the effect.

### What the operator decides per actuator

| Field | Meaning | Rule (default) |
|---|---|---|
| `expect_on_w` | metered power that proves "on" | below the appliance's steady draw, above anything the plug idles at |
| `off_max_w` | metered power that proves "off" | `0 < off_max_w < expect_on_w` (10 W) |
| `min_on_s`, `max_on_s` | how long one `on` may last | `0 < min ≤ max ≤ 64800` (10 s, 900 s) |
| `cooldown_s` | pause between two commands to one actuator | `≥ 0` (30 s) |
| `effect_timeout_s` | how long GAIA waits for the effect to show | 1–120 s (20 s) |
| `latitude`, `longitude` | optional: where ATLAS shows the actuator | — |

With actuation enabled, an actuator file that breaks these rules stops GAIA at start-up rather
than run a switch whose "on" and "off" could look alike. A valid plug that cannot be reached is
skipped and logged, and the rest of GAIA runs.

## Capabilities

| Capability | Price | What it does |
|---|---|---|
| `gaia.actuator.list@v1` | free | The operator's actuators: commands, duration limits, cooldown, relay state, pinned device keys, and whether the kill switch is engaged |
| `gaia.actuate@v1` | $0.05 | Switch one actuator `on` for `duration_s` seconds (it switches itself off afterwards) or `off`. The result is the evidence bundle |

$0.05 is the hub's Pay-on-Verified price floor, so a buyer can hold the payment in escrow until
GAIA's `/v1/verify` confirms the effect.

## How a paid command settles

```mermaid
sequenceDiagram
    autonumber
    participant B as Buyer agent
    participant H as AIMarket hub
    participant G as GAIA
    participant P as Metering plug
    B->>H: invoke gaia.actuate@v1 {actuator_id, command: on, duration_s} + verify block
    H->>G: invoke (request-bound X-Provider-Signature required)
    G->>G: safety gates (operator switch, kill switch, registry, limits, cooldown, one at a time)
    G->>P: read meter → attested reading BEFORE
    G->>G: sign command record (actuator, command, duration, expected effect, seq, nonce, time)
    G->>P: switch (Tasmota: PulseTime + Power ON — the plug's own off-timer)
    loop until the effect shows or the window closes
        G->>P: read meter → attested reading AFTER
    end
    alt effect measured
        G-->>H: evidence bundle (command + before + after + effect)
        H->>H: escrow HOLD $0.05
        H->>G: POST /v1/verify (the bundle)
        G->>G: evaluate(): pinned key, same device, order in time, window, effect, physics, not a replay
        G-->>H: verified
        H->>H: capture — provider paid, verify_passed reputation event
        H-->>B: result + settled receipt
    else no effect
        G->>P: switch back off (an unproven ON never stays on)
        G-->>H: ok:false — evidence id in the error
        H-->>B: refusal — nothing debited
    end
```

Without a `verify` block the effect still decides the charge: GAIA answers `ok:false` for an
unproven effect, and the hub debits nothing for `ok:false`. The verify block adds a
second check before the money moves: the hub sends the evidence to GAIA's own verifier, which
re-checks it exactly ([below](#what-the-verdict-checks)) — a hub routes `gaia.actuate@v1` there with
`AIMARKET_VERIFY_ROUTES` rather than to its LLM jury.

## Safety gates

```mermaid
flowchart TD
    A["gaia.actuate@v1"] --> B{GAIA_ACTUATION_ENABLED=1?}
    B -- no --> X1[capability not advertised]
    B -- yes --> C{kill switch engaged?<br/>env, file or operator API}
    C -- yes --> X2[ok:false — nothing charged]
    C -- no --> D{actuator in the operator's YAML?}
    D -- no --> X3[ok:false — lists the actuators]
    D -- yes --> E{command on/off,<br/>duration within min..max?}
    E -- no --> X4[ok:false — says the limits]
    E -- yes --> F{cooldown passed,<br/>not busy?}
    F -- no --> X5[ok:false — retry in N s]
    F -- yes --> G[before → command → after → evaluate]
    G --> H{effect measured?}
    H -- yes --> OK[evidence bundle — billable]
    H -- no --> OFF[switch back off · ok:false · nothing charged]
```

A caller names an **actuator id** — never an MQTT topic, a broker or a device the operator did
not list. "On" is always bounded: a duration is required and capped by the operator.

## Call it

```bash
# what can be switched, and the limits
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.actuator.list@v1","input":{}}'

# switch the demo heater on for one minute
curl -s -X POST https://iot.modelmarket.dev/ai-market/v2/invoke -H 'Content-Type: application/json' \
  -d '{"capability_id":"gaia.actuate@v1",
       "input":{"actuator_id":"plug-sim-01","command":"on","duration_s":60}}'
```

The answer (abridged):

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

Anyone can check it again: `POST /v1/verify` with the bundle (the escrow slot), or invoke
`gaia.verify@v1` with it. Evidence of recent commands, including refused ones, is public at
`GET /actuation/v1/evidence/<command_id>`; `GET /actuation/v1/status` shows every actuator.

## What the verdict checks

All checks are hard — one false fact and nothing is owed:

| Check | Meaning |
|---|---|
| `command_signature` | the command record is signed by the plug's **pinned** key (from the fleet registry, never the key the bundle carries) |
| `before_device`, `after_device` | both readings come from that same actuator |
| `before_signature`, `after_signature` | both readings verify against the pinned key |
| `order` | before → command → after, in sequence number and in time |
| `window` | the effect was read within the promised window |
| `effect` | the expected predicate holds on the after reading (`power_w >= expect_on_w` for on, `<= off_max_w` for off) |
| `relay_state` | the plug reports the relay state the command asked for |
| `after_plausible` | the after reading passes GAIA's physics checks |

Each command settles once: its `seq` (wall-clock milliseconds, only ever growing) is the
escrow verifier's high-water mark, so a replayed bundle is rejected.

Plausibility knows plugs: `relay_on` is a field, an open relay that meters more than 25 W is a
contradiction, and the energy register is checked against what one 16 A socket could pass in
the time since the last read — not against present power times the gap, since a plug switched
between two reads is honest.

## Operators

The public demo runs one **simulated** plug (`plug-sim-01`, a 1500 W heater) from
[`config/actuators.yaml`](../config/actuators.yaml). For real hardware:

1. A Tasmota plug **with an energy chip**, on your MQTT broker, answering `Status 8`.
2. Your actuator file, from [`config/actuators.example.yaml`](../config/actuators.example.yaml):
   ```yaml
   actuators:
     - id: lab-heater-01
       kind: tasmota-mqtt
       topic: tasmota_lab_heater   # the plug's %topic%
       expect_on_w: 800            # below the heater's draw, above anything the plug idles at
       off_max_w: 5
       min_on_s: 30
       max_on_s: 1800
       cooldown_s: 60
       effect_timeout_s: 20
   ```
3. Environment:
   ```bash
   GAIA_ACTUATION_ENABLED=1
   GAIA_ACTUATORS_FILE=/path/to/actuators.yaml
   GAIA_MQTT_BROKER=mqtts://broker.example:8883
   GAIA_MQTT_USERNAME=gaia            # password from your secret store, never a file in git
   GAIA_ACTUATION_TOKEN=…             # Bearer token for the kill switch API
   ```
4. **The hub that sells it with Pay-on-Verified** routes the capability to this GAIA's verifier, so
   an exact check, not an LLM jury, decides the money:
   ```
   # GAIA: in production /v1/verify refuses every caller until this is set
   GAIA_VERIFY_TOKEN=…                # a random secret, from a 0600 file
   # the hub's env file (one line, no quotes: --env-file takes values literally)
   AIMARKET_VERIFY_ROUTES={"gaia.actuate@v1": {"url": "https://<gaia>", "verifier_id": "gaia.verify@v1", "key_env": "AIMARKET_VERIFY_GAIA_TOKEN"}}
   AIMARKET_VERIFY_GAIA_TOKEN=…       # the same value as GAIA_VERIFY_TOKEN
   ```
   Without the token on the hub, the hub refuses the verify block before anything is switched;
   with a wrong one, GAIA answers 401 and the buyer is refunded. See
   [the hub's verifier routes](https://github.com/alexar76/aimarket-hub/blob/main/docs/pay-on-verified.md#verifier-routes).

ON is sent as `Backlog PulseTime1 <duration>; Power1 ON`, so **the plug switches itself off**
after the paid duration even if GAIA, the broker or the network goes away. GAIA also sends its
own OFF at the deadline as a second stop.

### Kill switch

Three ways, any one is enough; while engaged every command is refused and nothing is charged:

```bash
curl -X POST https://<gaia>/actuation/v1/kill   -H "Authorization: Bearer $GAIA_ACTUATION_TOKEN"   # also switches everything off
curl -X POST https://<gaia>/actuation/v1/resume -H "Authorization: Bearer $GAIA_ACTUATION_TOKEN"
touch <data dir>/ACTUATION_KILL        # a file, for when the API is unreachable
GAIA_ACTUATION_KILL=1                  # an environment switch, for a restart
```

Without `GAIA_ACTUATION_TOKEN` the kill switch API answers 503 — it never opens unauthenticated.
Every command, outcome, kill and resume is appended to `actuation.jsonl` in the data directory.

## Honest limits

- A Tasmota plug has no secure element. GAIA's key signs **what the plug reported** — custody
  of the report, as with every GAIA relay, not proof that the hardware cannot lie. The meter and
  the relay being one device is what keeps the claim narrow.
- The effect proves power flowed through the socket, not what the appliance did with it.
- The public demo plug is simulated (`SIM`); its heater exists only in software, and every answer says so (`"simulated": true`).
- With the route, GAIA is both the seller and the judge of `gaia.actuate@v1`. That is acceptable
  only because the verdict is a deterministic check of signed evidence that anyone can run again
  (`POST /v1/verify`, or `gaia.verify@v1` with the bundle), and because a buyer can appeal a pass
  to the hub's appeal court, a different verifier, where the hub runs one. No verifier, GAIA or an
  LLM, can catch evidence forged by whoever holds the plug's key.

W3C WoT: each plug's Thing Description (`GET /wot/<id>`) carries `switchOn` / `switchOff`
**actions** whose forms are paid `gaia.actuate@v1` invokes.
