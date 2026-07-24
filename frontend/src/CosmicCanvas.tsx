import { ReactNode, useMemo, useRef } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { OrbitControls, Sparkles, Stars } from "@react-three/drei";
import { Bloom, EffectComposer, Vignette } from "@react-three/postprocessing";
import * as THREE from "three";

/* ===========================================================================
 *  CosmicCanvas — the shared deep-space stage for the GAIA gateway scene.
 *
 *  Adapted verbatim from the oracle-family CosmicCanvas so GAIA renders in the
 *  exact same visual language as the 17 math oracles: a procedural fbm nebula
 *  sphere, layered starfields, drifting sparkles, three coloured key lights,
 *  and a Bloom + Vignette post stack. The signature scene mounts as `children`.
 * ========================================================================= */

// Procedural fbm nebula backdrop (the cosmic signature of the family).
const NEBULA_VERT = `
varying vec3 vDir;
void main(){ vDir = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }
`;
const NEBULA_FRAG = `
precision highp float;
varying vec3 vDir; uniform float uTime;
float hash(vec3 p){ p=fract(p*0.3183099+0.1); p*=17.0; return fract(p.x*p.y*p.z*(p.x+p.y+p.z)); }
float noise(vec3 x){ vec3 i=floor(x),f=fract(x); f=f*f*(3.0-2.0*f);
  return mix(mix(mix(hash(i+vec3(0,0,0)),hash(i+vec3(1,0,0)),f.x),mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
             mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z); }
float fbm(vec3 p){ float v=0.0,a=0.5; for(int i=0;i<5;i++){ v+=a*noise(p); p*=2.02; a*=0.5; } return v; }
void main(){
  vec3 d=normalize(vDir); float t=uTime*0.015; vec3 p=d*2.2+vec3(t,t*0.5,-t*0.3);
  float n=fbm(p); float clouds=smoothstep(0.42,0.95,n);
  vec3 deep=vec3(0.012,0.012,0.045), purple=vec3(0.18,0.07,0.34), cyan=vec3(0.10,0.45,0.62), magenta=vec3(0.46,0.12,0.42);
  vec3 col=deep; col=mix(col,purple,clouds*0.85);
  float hi=smoothstep(0.6,1.0,fbm(p*1.7+5.0)); col=mix(col,cyan,hi*0.5*clouds);
  col=mix(col,magenta,smoothstep(0.72,1.0,n)*0.4);
  float band=exp(-pow(d.y*2.4,2.0))*0.14; col+=vec3(0.20,0.26,0.42)*band;
  gl_FragColor=vec4(col,1.0);
}
`;

function Nebula() {
  const matRef = useRef<THREE.ShaderMaterial>(null);
  const uniforms = useMemo(() => ({ uTime: { value: 0 } }), []);
  useFrame(({ clock }) => { if (matRef.current) matRef.current.uniforms.uTime.value = clock.elapsedTime; });
  return (
    <mesh renderOrder={-10}>
      <sphereGeometry args={[92, 48, 48]} />
      <shaderMaterial ref={matRef} vertexShader={NEBULA_VERT} fragmentShader={NEBULA_FRAG}
        uniforms={uniforms} side={THREE.BackSide} depthWrite={false} fog={false} />
    </mesh>
  );
}

export interface CosmicCanvasProps {
  children: ReactNode;
  camera?: [number, number, number];
  fov?: number;
  autoRotate?: boolean;
  autoRotateSpeed?: number;
  bloom?: number;
  controls?: boolean;
}

/** Shared cosmic environment (nebula + starfields + sparkles + bloom + lights).
 *  GAIA renders its sensor-fleet scene as `children` inside this Canvas. */
export function CosmicCanvas({
  children,
  camera = [0, 2.4, 21],
  fov = 46,
  autoRotate = true,
  autoRotateSpeed = 0.26,
  bloom = 1.02,
  controls = true,
}: CosmicCanvasProps) {
  return (
    <Canvas camera={{ position: camera, fov }} dpr={[1, 2]}
      gl={{ antialias: true, alpha: false, powerPreference: "high-performance" }}>
      <color attach="background" args={["#04030f"]} />
      <fog attach="fog" args={["#04030f", 26, 72]} />
      <ambientLight intensity={0.15} />
      <pointLight position={[8, 10, 6]} intensity={2.4} color="#6ee7ff" />
      <pointLight position={[-6, 5, -4]} intensity={1.8} color="#c084fc" />
      <pointLight position={[0, -3, 8]} intensity={0.7} color="#f59e0b" />
      <Nebula />
      <Stars radius={120} depth={60} count={6000} factor={4} fade speed={0.6} />
      <Stars radius={60} depth={28} count={2400} factor={3} fade speed={1.3} />
      <Sparkles count={160} scale={[22, 12, 22]} size={2} speed={0.3} opacity={0.3} color="#6ee7ff" />
      <Sparkles count={110} scale={[28, 16, 28]} size={3.4} speed={0.16} opacity={0.2} color="#c084fc" />
      {children}
      <EffectComposer multisampling={0}>
        <Bloom intensity={bloom} luminanceThreshold={0.3} luminanceSmoothing={0.82} mipmapBlur />
        <Vignette eskil offset={0.14} darkness={1.05} />
      </EffectComposer>
      {controls && (
        <OrbitControls enablePan={false} maxDistance={34} minDistance={7}
          autoRotate={autoRotate} autoRotateSpeed={autoRotateSpeed} />
      )}
    </Canvas>
  );
}
