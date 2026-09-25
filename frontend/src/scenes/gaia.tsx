import { useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { Billboard, Line, Text } from "@react-three/drei";
import * as THREE from "three";

/* ===========================================================================
 *  GAIA — THE LIVING SENSOR FLEET (signature 3D scene)
 *
 *  GAIA is the physical-world oracle gateway: it attests real device readings
 *  and sells them under PAY-ON-VERIFIED escrow — a buyer's payment is HELD, a
 *  plausibility verifier judges the reading, and only an honest reading gets
 *  the money CAPTURED; a lying sensor is REFUSED and the payment REFUNDED.
 *
 *  This scene is that gateway made cinematic — a fleet dashboard in deep space,
 *  every value evolved DETERMINISTICALLY on the CPU (no network), mirroring the
 *  real simulators the oracle ships:
 *
 *    · TWO co-located WEATHER STATIONS (ws-01, ws-02) — glowing nodes that
 *      PULSE size + colour by temperature (cool cyan → warm amber), a thin
 *      ring whose radius tracks barometric pressure, and streak particles for
 *      wind. The pair share one physical truth, so they visibly AGREE.
 *    · ONE AIR-QUALITY node (aq-01) — a volumetric PARTICLE CLOUD whose density
 *      + colour track PM2.5 (clean sparse cyan → hazy dense amber); CO₂ rides a
 *      slowly expanding translucent shell.
 *    · ONE ENERGY METER (em-01) — a scrolling instanced POWER GRAPH of the last
 *      N power_w samples (standby floor + fridge duty-cycle + evening curve +
 *      occasional appliance spike) with a bright white leading head.
 *
 *  THE THESIS — PAY-ON-VERIFIED: periodically a reading pulse travels from a
 *  device to the central PLAUSIBILITY VERIFIER core. The core emits a GREEN
 *  capture burst (settled) for an honest reading, or a RED refund burst when
 *  that device has been pushed into an injected SPIKE (a liar). A compact
 *  verdict ticker narrates it: "ws-01 · verified · captured $0.001" /
 *  "aq-01 · refused · refunded". Honest sensor paid; lying sensor refunded.
 *
 *  Rendered INSIDE the shared CosmicCanvas (camera / nebula / stars / bloom
 *  already provided). Everything that scales with a count is instanced; the
 *  per-frame hot path mutates preallocated buffers + scratch objects only —
 *  no allocations, steady 60fps for minutes. (Live-value label strings are
 *  rebuilt at ~6 Hz, far off the hot path — the only strings created at all.)
 * ========================================================================= */

const CYAN = new THREE.Color("#6ee7ff");
const INDIGO = new THREE.Color("#a5b4fc");
const PURPLE = new THREE.Color("#c084fc");
const WARM = new THREE.Color("#f59e0b"); // heat / haze
const GREEN = new THREE.Color("#22c55e"); // escrow captured
const RED = new THREE.Color("#ef4444"); // payment refunded
const WHITE = new THREE.Color("#ffffff");

// ---- world layout --------------------------------------------------------
const CORE = new THREE.Vector3(0, 0.6, 0); // the plausibility verifier
const WS0 = new THREE.Vector3(-8.2, 3.4, -1.0); // weather station ws-01
const WS1 = new THREE.Vector3(-6.7, 2.5, 0.7); //  co-located twin ws-02
const AQ = new THREE.Vector3(8.0, 2.7, -0.6); // air-quality aq-01
const EM = new THREE.Vector3(0.4, -5.0, 0.6); // energy meter em-01 (graph anchor)

// device table (drives pulses + connectors + labels)
const DEVICES = [
  { id: "ws-01", pos: WS0, accent: CYAN },
  { id: "ws-02", pos: WS1, accent: CYAN },
  { id: "aq-01", pos: AQ, accent: WARM },
  { id: "em-01", pos: EM, accent: PURPLE },
];
const N_DEV = DEVICES.length;

// ---- counts (instanced; comfortably 60fps) -------------------------------
const N_AQ = 260; // air-quality cloud particles
const N_BARS = 56; // energy power-graph bars
const N_WIND = 40; // weather wind streaks

// ---- energy graph geometry -----------------------------------------------
const BAR_S = 0.16; // bar spacing (world units)
const BAR_HALF = ((N_BARS - 1) * BAR_S) / 2;
const BAR_MAXH = 2.5; // world height at MAX_POWER
const MAX_POWER = 2400; // W — full-height appliance spike
const SAMPLE_DT = 0.11; // seconds between graph samples

// ---- pay-on-verified pulse cadence ---------------------------------------
const PULSE_PERIOD = 2.4; // one verdict every 2.4 s
const T_TRAVEL = 1.3; // reading travels device → core
const T_BURST = 0.75; // core capture / refund burst
// a pulse is a REFUND (device caught lying) when (count % 9) === 5 → ~1 in 9.
const REFUND_MOD = 9;
const REFUND_PHASE = 5;

// ---- diurnal clock: one simulated day every DAY_SECONDS ------------------
const DAY_SECONDS = 40;

const clamp = (v: number, lo: number, hi: number) => (v < lo ? lo : v > hi ? hi : v);
const smoothstep = (a: number, b: number, x: number) => {
  const t = clamp((x - a) / (b - a), 0, 1);
  return t * t * (3 - 2 * t);
};
const hump = (h: number, c: number, w: number) => Math.exp(-((h - c) * (h - c)) / (2 * w * w));

// ---- deterministic physics (mirrors gaia/gaia/devices/*) -----------------
// Shared weather truth for the co-located pair (they see the same day/front).
function weatherTruth(t: number) {
  const hour = ((t / DAY_SECONDS) * 24) % 24;
  const diurnal = 6.0 * Math.sin((Math.PI * (hour - 9.0)) / 12.0); // peak ~15:00
  const front = 1.4 * Math.sin(t * 0.05 + 1.0); // slow front excursion
  const temperature = 12.0 + diurnal + front;
  const pressure = 1013.25 + 6.0 * Math.sin(t * 0.03) + 3.0 * Math.sin(t * 0.017 + 2.0);
  let wind = 3.0 + 2.2 * Math.sin(t * 0.11) + 1.4 * Math.sin(t * 0.37 + 1.0);
  wind += Math.max(0, 4.0 * Math.sin(t * 0.9 + 0.5) - 2.6); // occasional gust
  return { temperature, pressure, wind: Math.max(0, wind), hour };
}

function airQuality(t: number, hour: number) {
  const weekday = 1; // the fleet dashboard shows a weekday
  const bg = 8.0 + 3.0 * Math.sin(t * 0.04);
  const traffic = weekday ? 9.0 * hump(hour, 8.0, 1.3) + 7.5 * hump(hour, 18.0, 1.6) : 1.5 * hump(hour, 14.0, 3.0);
  const pm25 = Math.max(3.0, bg + traffic);
  const occupancy = hump(hour, 13.0, 3.2);
  const co2 = 420.0 + 420.0 * occupancy;
  return { pm25, co2 };
}

function energyPower(t: number, hour: number) {
  const standby = 95.0;
  const fridge = t % 6.0 < 2.0 ? 55.0 : 0.0; // sped-up duty cycle
  const evening = 240.0 * Math.exp(-((hour - 20.0) * (hour - 20.0)) / (2 * 2.2 * 2.2));
  // deterministic appliance burst: ~1.5 s spike every 7 s
  const phase = t % 7.0;
  const appliance = phase < 1.5 ? 700.0 + 900.0 * Math.abs(Math.sin(t * 0.7)) : 0.0;
  const jitter = 6.0 * Math.sin(t * 5.0);
  return Math.max(0, standby + fridge + evening + appliance + jitter);
}

export default function Scene() {
  const group = useRef<THREE.Group>(null);

  // verifier core + verdict bursts
  const coreRef = useRef<THREE.Mesh>(null);
  const coreMat = useRef<THREE.MeshStandardMaterial>(null);
  const coreRingRef = useRef<THREE.Mesh>(null);
  const burstRef = useRef<THREE.Mesh>(null);
  const burstMat = useRef<THREE.MeshBasicMaterial>(null);
  const burstRingRef = useRef<THREE.Mesh>(null);
  const burstRingMat = useRef<THREE.MeshBasicMaterial>(null);

  // reading bead (single traveling pulse) + connector lines
  const beadRef = useRef<THREE.Mesh>(null);
  const beadMat = useRef<THREE.MeshBasicMaterial>(null);
  const lineRefs = useRef<any[]>([]);

  // weather nodes + rings
  const wsNodeRefs = useRef<(THREE.Mesh | null)[]>([]);
  const wsMatRefs = useRef<(THREE.MeshStandardMaterial | null)[]>([]);
  const wsRingRefs = useRef<(THREE.Mesh | null)[]>([]);
  const windRef = useRef<THREE.InstancedMesh>(null);

  // air-quality cloud + CO₂ shell
  const aqRef = useRef<THREE.InstancedMesh>(null);
  const aqShellRef = useRef<THREE.Mesh>(null);
  const aqShellMat = useRef<THREE.MeshBasicMaterial>(null);

  // energy graph
  const energyRef = useRef<THREE.InstancedMesh>(null);

  // drifting labels + verdict ticker (troika Text — mutated imperatively)
  const wsLabel = useRef<any>(null);
  const aqLabel = useRef<any>(null);
  const emLabel = useRef<any>(null);
  const verdictLabel = useRef<any>(null);
  const tallyLabel = useRef<any>(null);

  // ---- scratch (no per-frame allocation) ---------------------------------
  const dummy = useMemo(() => new THREE.Object3D(), []);
  const tmpColor = useMemo(() => new THREE.Color(), []);
  const tmpVec = useMemo(() => new THREE.Vector3(), []);

  // air-quality particle base positions (spherical shell around AQ), fixed once.
  const aqBase = useMemo(() => {
    const arr = new Float32Array(N_AQ * 3);
    let s = 0x9e3779b9 >>> 0;
    const rnd = () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
    for (let i = 0; i < N_AQ; i++) {
      // uniform-ish point in a ball, radius biased toward the surface for volume
      const u = rnd();
      const v = rnd();
      const theta = u * Math.PI * 2;
      const phi = Math.acos(2 * v - 1);
      const r = 0.5 + 1.5 * Math.cbrt(rnd());
      arr[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      arr[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
      arr[i * 3 + 2] = r * Math.cos(phi);
    }
    return arr;
  }, []);

  // wind streak lanes (offset + phase), fixed once.
  const windLanes = useMemo(() => {
    const arr = new Float32Array(N_WIND * 3); // y, z, phase
    let s = 0xc0ffee >>> 0;
    const rnd = () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
    for (let i = 0; i < N_WIND; i++) {
      arr[i * 3] = (rnd() - 0.5) * 2.6; // y offset around the pair
      arr[i * 3 + 1] = (rnd() - 0.5) * 2.6; // z offset
      arr[i * 3 + 2] = rnd(); // phase 0..1
    }
    return arr;
  }, []);
  const windCentroid = useMemo(
    () => new THREE.Vector3().addVectors(WS0, WS1).multiplyScalar(0.5),
    []
  );

  // energy power ring buffer (zero-alloc scrolling graph).
  const energy = useMemo(
    () => ({ powers: new Float32Array(N_BARS).fill(180), head: 0, accum: 0 }),
    []
  );

  // pay-on-verified pulse state.
  const pulse = useMemo(() => ({ lastCount: -1, captured: 0 }), []);

  // label throttle.
  const labelClock = useRef(0);

  // connector line endpoints (device → core), fixed geometry.
  const connectors = useMemo(
    () =>
      DEVICES.map((d) => [d.pos.clone(), CORE.clone()] as [THREE.Vector3, THREE.Vector3]),
    []
  );

  useFrame(({ clock }, rawDelta) => {
    const dt = Math.min(rawDelta, 1 / 30); // clamp big frames (tab refocus)
    const t = clock.elapsedTime;

    // gentle cinematic drift of the whole fleet
    if (group.current) {
      group.current.rotation.y = Math.sin(t * 0.08) * 0.14;
      group.current.position.y = Math.sin(t * 0.2) * 0.18;
    }

    // ---- evolve the physics (deterministic) ------------------------------
    const w = weatherTruth(t);
    const aq = airQuality(t, w.hour);
    const powerNow = energyPower(t, w.hour);

    // =====================================================================
    //  PAY-ON-VERIFIED PULSE — the thesis
    // =====================================================================
    const count = Math.floor(t / PULSE_PERIOD);
    const localP = t - count * PULSE_PERIOD;
    const source = count % N_DEV;
    const refund = count % REFUND_MOD === REFUND_PHASE;
    const verdictColor = refund ? RED : GREEN;
    const srcDev = DEVICES[source];

    // travel + burst envelopes
    const travelRaw = clamp(localP / T_TRAVEL, 0, 1);
    const travel = smoothstep(0, 1, travelRaw);
    const bursting = localP >= T_TRAVEL && localP < T_TRAVEL + T_BURST;
    const burstT = clamp((localP - T_TRAVEL) / T_BURST, 0, 1);
    // the source device is visibly "lying" (injected spike) during a refund pulse
    const lying = refund && localP < T_TRAVEL + T_BURST;
    const lyingWs0 = lying && source === 0;
    const lyingWs1 = lying && source === 1;
    const lyingAq = lying && source === 2;
    const lyingEm = lying && source === 3;

    // edge-triggered verdict ticker (once per pulse, off the hot path)
    if (count !== pulse.lastCount && count >= 0) {
      pulse.lastCount = count;
      if (!refund) pulse.captured += 0.001;
      if (verdictLabel.current) {
        verdictLabel.current.text = refund
          ? `${srcDev.id} · refused · refunded`
          : `${srcDev.id} · verified · captured $0.001`;
        verdictLabel.current.color = refund ? "#ef4444" : "#22c55e";
        verdictLabel.current.sync();
      }
      if (tallyLabel.current) {
        tallyLabel.current.text = `pay-on-verified · Metis-gated escrow · settled $${pulse.captured.toFixed(3)}`;
        tallyLabel.current.sync();
      }
    }

    // ---- WEATHER STATIONS: pulse by temperature, ring by pressure --------
    const heatU = clamp((w.temperature - 4) / 18, 0, 1); // 0 cool → 1 warm
    const pressU = clamp((w.pressure - 1000) / 25, 0, 1);
    const bias = [0.0, 0.25]; // tiny per-unit calibration bias → they agree, not identical
    for (let s = 0; s < 2; s++) {
      const node = wsNodeRefs.current[s];
      const mat = wsMatRefs.current[s];
      const lyingThis = s === 0 ? lyingWs0 : lyingWs1;
      if (node) {
        const pulseSize = 0.42 + 0.05 * Math.sin(t * 2.2 + s) + heatU * 0.12;
        const flare = lyingThis ? 1 + 0.6 * Math.abs(Math.sin(t * 30)) : 1; // erratic when lying
        node.scale.setScalar(pulseSize * flare);
      }
      if (mat) {
        // cool cyan → warm amber; red + hot when caught lying
        if (lyingThis) tmpColor.copy(RED);
        else tmpColor.copy(CYAN).lerp(WARM, clamp(heatU + bias[s] * 0.01, 0, 1));
        mat.color.copy(tmpColor);
        mat.emissive.copy(tmpColor);
        mat.emissiveIntensity = lyingThis ? 3.2 : 1.8 + heatU * 0.9;
      }
      const ring = wsRingRefs.current[s];
      if (ring) {
        const rad = 0.9 + pressU * 0.9;
        ring.scale.setScalar(rad);
        ring.rotation.z = t * (0.3 + s * 0.1);
        ring.rotation.x = Math.PI / 2.4;
        const rm = ring.material as THREE.MeshBasicMaterial;
        rm.opacity = 0.28 + pressU * 0.22;
      }
    }

    // wind streaks flowing past the pair; speed ∝ wind
    if (windRef.current) {
      const span = 5.0;
      const speed = 0.4 + w.wind * 0.22;
      const len = 0.28 + w.wind * 0.09;
      for (let i = 0; i < N_WIND; i++) {
        const oy = windLanes[i * 3];
        const oz = windLanes[i * 3 + 1];
        const ph = windLanes[i * 3 + 2];
        // travel along +x, wrap in [-span/2, span/2]
        let x = ((ph + t * speed * 0.16) % 1) * span - span / 2;
        dummy.position.set(windCentroid.x + x, windCentroid.y + oy, windCentroid.z + oz);
        dummy.scale.set(len, 0.02, 0.02);
        dummy.rotation.set(0, 0, 0);
        dummy.updateMatrix();
        windRef.current.setMatrixAt(i, dummy.matrix);
        const fade = 0.25 + 0.55 * (0.5 + 0.5 * Math.sin((x / span) * Math.PI));
        tmpColor.copy(CYAN).multiplyScalar(clamp(fade * (0.5 + w.wind * 0.08), 0, 1.4));
        windRef.current.setColorAt(i, tmpColor);
      }
      windRef.current.instanceMatrix.needsUpdate = true;
      if (windRef.current.instanceColor) windRef.current.instanceColor.needsUpdate = true;
    }

    // ---- AIR QUALITY: density + colour by PM2.5, CO₂ shell ---------------
    const density = clamp((aq.pm25 - 4) / 34, 0.24, 1); // sparse clean → dense hazy
    const hazU = clamp((aq.pm25 - 6) / 30, 0, 1);
    const nActive = Math.floor(density * N_AQ);
    if (aqRef.current) {
      const swell = 1 + hazU * 0.35;
      for (let i = 0; i < N_AQ; i++) {
        const bx = aqBase[i * 3];
        const by = aqBase[i * 3 + 1];
        const bz = aqBase[i * 3 + 2];
        // slow turbulent drift (deterministic)
        const dx = Math.sin(t * 0.6 + i * 0.7) * 0.12;
        const dy = Math.cos(t * 0.5 + i * 1.1) * 0.12;
        const dz = Math.sin(t * 0.7 + i * 0.3) * 0.12;
        dummy.position.set(AQ.x + bx * swell + dx, AQ.y + by * swell + dy, AQ.z + bz * swell + dz);
        const on = i < nActive;
        const flick = 0.6 + 0.4 * Math.sin(t * 2 + i);
        const sc = on ? (0.075 + 0.05 * flick + hazU * 0.06) * (lyingAq ? 1.6 : 1) : 0.0001;
        dummy.scale.setScalar(sc);
        dummy.updateMatrix();
        aqRef.current.setMatrixAt(i, dummy.matrix);
        if (lyingAq) tmpColor.copy(RED);
        else tmpColor.copy(CYAN).lerp(WARM, hazU);
        tmpColor.multiplyScalar(on ? 1.0 + 0.5 * flick : 0);
        aqRef.current.setColorAt(i, tmpColor);
      }
      aqRef.current.instanceMatrix.needsUpdate = true;
      if (aqRef.current.instanceColor) aqRef.current.instanceColor.needsUpdate = true;
    }
    if (aqShellRef.current && aqShellMat.current) {
      const co2U = clamp((aq.co2 - 420) / 420, 0, 1); // 0..1 over 420→840 ppm
      // subtle halo — must not dominate the particle cloud it wraps
      const shell = 1.7 + co2U * 1.2 + Math.sin(t * 0.8) * 0.05;
      aqShellRef.current.position.copy(AQ);
      aqShellRef.current.scale.setScalar(shell);
      aqShellMat.current.opacity = 0.035 + co2U * 0.08;
      aqShellMat.current.color.copy(INDIGO).lerp(WARM, co2U * 0.55);
    }

    // ---- ENERGY METER: scrolling instanced power graph -------------------
    energy.accum += dt;
    while (energy.accum >= SAMPLE_DT) {
      energy.accum -= SAMPLE_DT;
      // when em is the caught liar, push an absurd off-scale spike (the lie)
      const sample = lyingEm ? MAX_POWER * 1.06 : powerNow;
      energy.powers[energy.head] = sample;
      energy.head = (energy.head + 1) % N_BARS;
    }
    if (energyRef.current) {
      const frac = energy.accum / SAMPLE_DT; // 0..1 smooth scroll
      for (let d = 0; d < N_BARS; d++) {
        // d = 0 is the newest (leading head); older bars march left
        const bi = (energy.head - 1 - d + N_BARS * 2) % N_BARS;
        const p = energy.powers[bi];
        const h = clamp(p / MAX_POWER, 0, 1) * BAR_MAXH + 0.02;
        const x = BAR_HALF - (d + frac) * BAR_S;
        dummy.position.set(EM.x + x, EM.y + h / 2, EM.z);
        dummy.scale.set(0.11, h, 0.11);
        dummy.rotation.set(0, 0, 0);
        dummy.updateMatrix();
        energyRef.current.setMatrixAt(d, dummy.matrix);
        // colour: indigo (low) → purple → cyan (high); brightest at the head
        const hu = clamp(p / MAX_POWER, 0, 1);
        if (hu < 0.5) tmpColor.copy(INDIGO).lerp(PURPLE, hu * 2);
        else tmpColor.copy(PURPLE).lerp(CYAN, (hu - 0.5) * 2);
        const recency = 1 - d / N_BARS; // newer = brighter
        tmpColor.multiplyScalar(0.4 + 0.6 * recency);
        if (d === 0) tmpColor.lerp(WHITE, 0.6); // bright leading head
        if (lyingEm && p > MAX_POWER) tmpColor.copy(RED); // the off-scale lie
        energyRef.current.setColorAt(d, tmpColor);
      }
      energyRef.current.instanceMatrix.needsUpdate = true;
      if (energyRef.current.instanceColor) energyRef.current.instanceColor.needsUpdate = true;
    }

    // ---- connectors + traveling reading bead -----------------------------
    for (let i = 0; i < N_DEV; i++) {
      const ln = lineRefs.current[i];
      if (ln && ln.material) {
        const active = i === source && localP < T_TRAVEL + T_BURST;
        ln.material.opacity = active ? 0.16 + 0.34 * (1 - burstT * (bursting ? 1 : 0)) : 0.07;
      }
    }
    if (beadRef.current && beadMat.current) {
      const show = travelRaw < 1 && localP < T_TRAVEL;
      if (show) {
        tmpVec.copy(srcDev.pos).lerp(CORE, travel);
        beadRef.current.position.copy(tmpVec);
        const sc = 0.16 + 0.06 * Math.sin(t * 8);
        beadRef.current.scale.setScalar(sc);
        // reading bead is cyan, tinting toward its verdict colour as it nears core
        tmpColor.copy(CYAN).lerp(verdictColor, travel * 0.7);
        beadMat.current.color.copy(tmpColor);
        beadMat.current.opacity = 1;
      } else {
        beadRef.current.scale.setScalar(0.0001);
        beadMat.current.opacity = 0;
      }
    }

    // ---- the plausibility verifier core + verdict burst ------------------
    if (coreRef.current && coreMat.current) {
      const pulseC = 1 + Math.sin(t * 2.4) * 0.05;
      coreRef.current.position.copy(CORE);
      coreRef.current.scale.setScalar(0.85 * pulseC);
      coreRef.current.rotation.y = t * 0.4;
      coreRef.current.rotation.x = t * 0.16;
      // core flashes toward the verdict colour at the moment of judgement
      const flash = bursting ? 1 - burstT : 0;
      tmpColor.copy(INDIGO).lerp(verdictColor, flash * 0.85);
      coreMat.current.emissive.copy(tmpColor);
      coreMat.current.emissiveIntensity = 1.6 + flash * 2.6;
    }
    if (coreRingRef.current) {
      coreRingRef.current.position.copy(CORE);
      coreRingRef.current.rotation.x = Math.PI / 2;
      coreRingRef.current.rotation.z = -t * 0.5;
      coreRingRef.current.scale.setScalar(1.5 + Math.sin(t * 1.6) * 0.05);
    }
    // expanding capture/refund burst shell + ring
    if (burstRef.current && burstMat.current) {
      burstRef.current.position.copy(CORE);
      if (bursting) {
        const s = 0.8 + burstT * 3.4;
        burstRef.current.scale.setScalar(s);
        burstMat.current.color.copy(verdictColor);
        burstMat.current.opacity = (1 - burstT) * 0.5;
      } else {
        burstRef.current.scale.setScalar(0.0001);
        burstMat.current.opacity = 0;
      }
    }
    if (burstRingRef.current && burstRingMat.current) {
      burstRingRef.current.position.copy(CORE);
      burstRingRef.current.rotation.x = Math.PI / 2;
      if (bursting) {
        const s = 1.0 + burstT * 4.2;
        burstRingRef.current.scale.setScalar(s);
        burstRingMat.current.color.copy(verdictColor);
        burstRingMat.current.opacity = (1 - burstT) * 0.8;
      } else {
        burstRingRef.current.scale.setScalar(0.0001);
        burstRingMat.current.opacity = 0;
      }
    }

    // ---- drifting live-value labels (rebuilt at ~6 Hz, off the hot path) --
    labelClock.current += dt;
    if (labelClock.current >= 0.16) {
      labelClock.current = 0;
      if (wsLabel.current) {
        wsLabel.current.text = `ws-01·02   ${w.temperature.toFixed(1)} °C   ${w.pressure.toFixed(0)} hPa`;
        wsLabel.current.sync();
      }
      if (aqLabel.current) {
        // NB: the default troika font lacks the ₂ subscript glyph (renders tofu),
        // so CO2 is spelled with an ASCII 2; µ and ³ are present and fine.
        aqLabel.current.text = `aq-01   PM2.5 ${aq.pm25.toFixed(1)} µg/m³   CO2 ${aq.co2.toFixed(0)} ppm`;
        aqLabel.current.sync();
      }
      if (emLabel.current) {
        emLabel.current.text = `em-01   ${powerNow.toFixed(0)} W`;
        emLabel.current.sync();
      }
    }
  });

  return (
    <group ref={group}>
      {/* ===================== CONNECTORS ===================== */}
      {connectors.map((seg, i) => (
        <Line
          key={i}
          ref={(el) => (lineRefs.current[i] = el)}
          points={seg}
          color={i === 2 ? "#f59e0b" : i === 3 ? "#c084fc" : "#6ee7ff"}
          lineWidth={1}
          transparent
          opacity={0.07}
          toneMapped={false}
        />
      ))}

      {/* ===================== WEATHER STATIONS (ws-01, ws-02) ===================== */}
      {[WS0, WS1].map((p, s) => (
        <group key={s}>
          <mesh ref={(el) => (wsNodeRefs.current[s] = el)} position={p}>
            <icosahedronGeometry args={[1, 3]} />
            <meshStandardMaterial
              ref={(el) => (wsMatRefs.current[s] = el)}
              color={CYAN}
              emissive={CYAN}
              emissiveIntensity={1.9}
              roughness={0.3}
              metalness={0.2}
              toneMapped={false}
            />
          </mesh>
          {/* pressure ring */}
          <mesh ref={(el) => (wsRingRefs.current[s] = el)} position={p}>
            <torusGeometry args={[1, 0.02, 8, 80]} />
            <meshBasicMaterial
              color={INDIGO}
              transparent
              opacity={0.35}
              toneMapped={false}
              blending={THREE.AdditiveBlending}
              depthWrite={false}
            />
          </mesh>
        </group>
      ))}

      {/* wind streaks (instanced) flowing past the pair */}
      <instancedMesh
        ref={windRef as any}
        args={[undefined as any, undefined as any, N_WIND]}
        frustumCulled={false}
      >
        <boxGeometry args={[1, 1, 1]} />
        <meshBasicMaterial vertexColors toneMapped={false} transparent opacity={0.85} blending={THREE.AdditiveBlending} depthWrite={false} />
      </instancedMesh>

      <Billboard position={[windCentroid.x, windCentroid.y + 2.7, windCentroid.z]}>
        <Text
          ref={wsLabel}
          fontSize={0.4}
          color="#6ee7ff"
          anchorX="center"
          anchorY="middle"
          outlineWidth={0.008}
          outlineColor="#04030f"
          letterSpacing={0.04}
        >
          ws-01·02
        </Text>
      </Billboard>

      {/* ===================== AIR QUALITY (aq-01) ===================== */}
      <instancedMesh
        ref={aqRef as any}
        args={[undefined as any, undefined as any, N_AQ]}
        frustumCulled={false}
      >
        <sphereGeometry args={[1, 8, 8]} />
        <meshBasicMaterial vertexColors toneMapped={false} transparent opacity={0.85} blending={THREE.AdditiveBlending} depthWrite={false} />
      </instancedMesh>
      {/* CO₂ expanding shell */}
      <mesh ref={aqShellRef}>
        <sphereGeometry args={[1, 24, 24]} />
        <meshBasicMaterial
          ref={aqShellMat}
          color={INDIGO}
          transparent
          opacity={0.08}
          toneMapped={false}
          blending={THREE.AdditiveBlending}
          depthWrite={false}
          side={THREE.BackSide}
        />
      </mesh>

      <Billboard position={[AQ.x, AQ.y + 3.0, AQ.z]}>
        <Text
          ref={aqLabel}
          fontSize={0.4}
          color="#f59e0b"
          anchorX="center"
          anchorY="middle"
          outlineWidth={0.008}
          outlineColor="#04030f"
          letterSpacing={0.04}
        >
          aq-01
        </Text>
      </Billboard>

      {/* ===================== ENERGY METER (em-01) ===================== */}
      <instancedMesh
        ref={energyRef as any}
        args={[undefined as any, undefined as any, N_BARS]}
        frustumCulled={false}
      >
        <boxGeometry args={[1, 1, 1]} />
        <meshStandardMaterial
          vertexColors
          emissive={WHITE}
          emissiveIntensity={1.4}
          roughness={0.35}
          metalness={0.2}
          toneMapped={false}
        />
      </instancedMesh>
      {/* graph baseline */}
      <mesh position={[EM.x, EM.y - 0.01, EM.z]}>
        <boxGeometry args={[BAR_HALF * 2 + 0.4, 0.02, 0.02]} />
        <meshBasicMaterial color={INDIGO} transparent opacity={0.4} toneMapped={false} />
      </mesh>

      <Billboard position={[EM.x, EM.y - 0.75, EM.z]}>
        <Text
          ref={emLabel}
          fontSize={0.4}
          color="#c084fc"
          anchorX="center"
          anchorY="middle"
          outlineWidth={0.008}
          outlineColor="#04030f"
          letterSpacing={0.04}
        >
          em-01
        </Text>
      </Billboard>

      {/* ===================== PLAUSIBILITY VERIFIER CORE ===================== */}
      <mesh ref={coreRef}>
        <icosahedronGeometry args={[1, 4]} />
        <meshStandardMaterial
          ref={coreMat}
          color={"#0b1030"}
          emissive={INDIGO}
          emissiveIntensity={1.6}
          roughness={0.2}
          metalness={0.4}
          toneMapped={false}
        />
      </mesh>
      {/* slowly rotating guard ring around the verifier */}
      <mesh ref={coreRingRef}>
        <torusGeometry args={[1, 0.014, 8, 100]} />
        <meshBasicMaterial color={INDIGO} transparent opacity={0.5} toneMapped={false} blending={THREE.AdditiveBlending} depthWrite={false} />
      </mesh>
      {/* verdict burst — green capture / red refund */}
      <mesh ref={burstRef}>
        <sphereGeometry args={[1, 24, 24]} />
        <meshBasicMaterial
          ref={burstMat}
          color={GREEN}
          transparent
          opacity={0}
          toneMapped={false}
          blending={THREE.AdditiveBlending}
          depthWrite={false}
          side={THREE.BackSide}
        />
      </mesh>
      <mesh ref={burstRingRef}>
        <torusGeometry args={[1, 0.03, 8, 100]} />
        <meshBasicMaterial
          ref={burstRingMat}
          color={GREEN}
          transparent
          opacity={0}
          toneMapped={false}
          blending={THREE.AdditiveBlending}
          depthWrite={false}
        />
      </mesh>

      {/* traveling reading bead */}
      <mesh ref={beadRef}>
        <sphereGeometry args={[1, 16, 16]} />
        <meshBasicMaterial ref={beadMat} color={CYAN} transparent opacity={0} toneMapped={false} />
      </mesh>

      {/* verifier label + verdict ticker */}
      <Billboard position={[CORE.x, CORE.y + 1.9, CORE.z]}>
        <Text
          fontSize={0.34}
          color="#a5b4fc"
          anchorX="center"
          anchorY="middle"
          outlineWidth={0.008}
          outlineColor="#04030f"
          letterSpacing={0.14}
        >
          PLAUSIBILITY VERIFIER
        </Text>
      </Billboard>
      <Billboard position={[CORE.x, CORE.y - 1.7, CORE.z]}>
        <Text
          ref={verdictLabel}
          fontSize={0.46}
          color="#22c55e"
          anchorX="center"
          anchorY="middle"
          outlineWidth={0.01}
          outlineColor="#04030f"
          letterSpacing={0.05}
        >
          ws-01 · verified · captured $0.001
        </Text>
      </Billboard>
      <Billboard position={[CORE.x, CORE.y - 2.4, CORE.z]}>
        <Text
          ref={tallyLabel}
          fontSize={0.26}
          color="#8aa6c8"
          anchorX="center"
          anchorY="middle"
          outlineWidth={0.006}
          outlineColor="#04030f"
          letterSpacing={0.06}
        >
          pay-on-verified · Metis-gated escrow · settled $0.000
        </Text>
      </Billboard>
    </group>
  );
}
