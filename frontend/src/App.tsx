import { Component, ReactNode, Suspense } from "react";
import { CosmicCanvas } from "./CosmicCanvas";
import Scene from "./scenes/gaia";

/* GAIA — physical-world oracle gateway.
 *
 * A lean standalone R3F app: one cosmic canvas, one signature scene, and a
 * minimal title overlay. GAIA is a SEPARATE satellite from the 17-strong math
 * oracle portal — it does not appear in oracles.ts / sceneLoaders.ts. */

class ErrorBoundary extends Component<{ children: ReactNode }, { err: string | null }> {
  state = { err: null as string | null };
  static getDerivedStateFromError(e: any) { return { err: String(e?.message || e) }; }
  render() {
    if (this.state.err)
      return <div style={{ position: "fixed", left: 30, bottom: 60, color: "#ef4444", fontSize: 12, zIndex: 10 }}>scene error: {this.state.err}</div>;
    return this.props.children;
  }
}

export function App() {
  return (
    <>
      <div className="overlay">
        <h1>GAIA</h1>
        <p>physical-world oracle gateway · pay-on-verified sensor fleet</p>
      </div>
      <div className="legend">
        <div className="row capture"><span className="dot" /> verified → escrow captured</div>
        <div className="row refund"><span className="dot" /> refused → payment refunded</div>
      </div>
      <ErrorBoundary>
        <Suspense fallback={null}>
          <CosmicCanvas>
            <Scene />
          </CosmicCanvas>
        </Suspense>
      </ErrorBoundary>
    </>
  );
}
