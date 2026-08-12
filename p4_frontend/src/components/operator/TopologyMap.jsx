import { useCallback, useRef, useState } from "react";
import LoadingDots from "../shared/LoadingDots";
import { ALL_CLASSES, AUTO_FIX_CLASSES, CLASS_LABELS, FAULT_TARGETS } from "../../constants/faultClasses";

// Real "necklace loop" -- 12 fault classes as oval nodes evenly spaced on
// one ring, connected by a real circular guide line, drag-to-rotate in
// either direction. One bead per FAULT CLASS (not grouped by service like
// the earlier grid attempt) -- this is what actually fixes the original
// uneven-node-height problem: every bead now holds exactly one fault, so
// there's nothing left to be uneven.
const PILL_W = 188;
const PILL_H = 96;

// Real radius, computed from the real pill size, not copied from Stitch's
// mockup (its spacing didn't account for real label lengths and produced
// visible overlap). Chord distance between two adjacent bead centers on a
// 12-point circle is 2*R*sin(15deg); this must exceed the pill's own
// width plus a real gap for the pills to never touch.
const GAP = 20;
const RADIUS = Math.ceil((PILL_W + GAP) / (2 * Math.sin(Math.PI / 12))) + 20; // +20 margin
const CONTAINER = RADIUS * 2 + PILL_W + 40;

function Bead({ fc, token, role, trustMap, episodeInFlight, isActive, live, onTrigger, onResolve, onStopHold }) {
  const isAutoFix = AUTO_FIX_CLASSES.includes(fc);
  const trustState = trustMap[fc];
  const canAct = Boolean(token) && (role === "admin" || role === "demo-trigger");
  const state = isActive ? live?.episode_state : null;
  const target = FAULT_TARGETS[fc]?.target;

  let statusDot = null;
  if (isAutoFix && trustState) {
    statusDot = (
      <span
        className={`w-1.5 h-1.5 rounded-full shrink-0 ${trustState === "can_act" ? "bg-[#238636] animate-pulse" : "bg-outline-variant"}`}
        title={trustState === "can_act" ? "CAN-ACT" : trustState.toUpperCase()}
      />
    );
  }
  if (isActive && state && state !== "resolved" && state !== "failed") {
    statusDot = <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse shrink-0" title={state} />;
  }

  let button;
  if (isActive && state === "holding") {
    button = live?.can_stop_hold_early ? (
      <button
        onClick={onStopHold}
        className="px-2.5 py-1 bg-primary border border-primary text-on-primary font-label-caps text-[9px] hover:brightness-110 font-bold"
      >
        STOP & FIX
      </button>
    ) : (
      <span className="font-label-caps text-[9px] text-on-surface-variant">LIVE — CHECK TAB</span>
    );
  } else if (isActive && state === "awaiting_fix" && isAutoFix) {
    button = (
      <button
        onClick={onResolve}
        className="px-2.5 py-1 bg-primary border border-primary text-on-primary font-label-caps text-[9px] hover:brightness-110 font-bold"
      >
        DIAGNOSE & FIX
      </button>
    );
  } else if (isActive && (state === "resolving" || state === "awaiting_fix" || state === "injecting")) {
    button = (
      <span className="flex items-center gap-1 font-label-caps text-[9px] text-primary">
        <LoadingDots /> {state.replace("_", " ").toUpperCase()}
      </span>
    );
  } else {
    const disabled = !canAct || episodeInFlight;
    button = (
      <button
        onClick={() => onTrigger(fc)}
        disabled={disabled}
        title={!token ? "Sign in to trigger faults" : episodeInFlight ? "An episode is already in flight" : undefined}
        className="tactical-btn px-2.5 py-1 font-label-caps text-[9px] text-primary disabled:opacity-40"
      >
        {isAutoFix ? "INJECT FAULT" : "TRIGGER"}
      </button>
    );
  }

  return (
    <div
      className={`glass-module flex flex-col items-center justify-center gap-1 text-center px-3 ${
        isActive && state && state !== "resolved" && state !== "failed"
          ? "active-fault-glow border border-primary"
          : "border border-outline-variant/30"
      }`}
      style={{ width: PILL_W, height: PILL_H, borderRadius: PILL_H / 2 }}
    >
      <div className="flex items-center gap-1">
        {statusDot}
        <span className="font-data-mono text-[11px] leading-tight text-on-surface line-clamp-2">{CLASS_LABELS[fc]}</span>
      </div>
      <span className="font-label-caps text-[9px] text-on-surface-variant">{target}</span>
      {button}
    </div>
  );
}

export default function TopologyMap({ token, role, trustMap, episodeInFlight, activeEpisode, live, onTrigger, onResolve, onStopHold }) {
  const center = CONTAINER / 2;

  // Drag-to-rotate, both directions -- no idle auto-spin (explicitly
  // dropped, see wardence_frontend.md's necklace-loop design notes) and
  // no click-to-top-and-lock yet (a separate, later follow-up). Each
  // bead counter-rotates its own content (see the position div's
  // transform below) so labels/buttons stay upright regardless of ring
  // angle -- this is the real technique Stitch executed correctly and
  // v0 did not (v0 rotated bead TEXT along with position, illegible at
  // extreme angles).
  const ringRef = useRef(null);
  const dragState = useRef(null); // { startPointerAngle, startRotation, pointerId }
  const [rotationDeg, setRotationDeg] = useState(0);
  const [dragging, setDragging] = useState(false);

  // Momentum/inertia after release -- real velocity tracked from the last
  // couple of pointermove samples (deg/ms), not the drag's average speed,
  // so a light flick vs. a forceful one produce genuinely different
  // amounts of leftover spin. Friction is applied per real elapsed ms via
  // rAF (frame-rate independent), not a fixed per-frame multiplier, so
  // the deceleration feels the same regardless of the display's refresh
  // rate.
  const lastSample = useRef(null); // { rotation, time }
  const velocityRef = useRef(0); // deg/ms
  const momentumFrame = useRef(null);

  const stopMomentum = () => {
    if (momentumFrame.current != null) {
      cancelAnimationFrame(momentumFrame.current);
      momentumFrame.current = null;
    }
  };

  const FRICTION_PER_16MS = 0.94; // per real 16ms tick, scaled by actual dt below
  const MIN_VELOCITY = 0.005; // deg/ms, below this we just stop

  const runMomentum = useCallback(() => {
    let lastT = performance.now();
    const step = (t) => {
      const dt = t - lastT;
      lastT = t;
      setRotationDeg((r) => r + velocityRef.current * dt);
      velocityRef.current *= Math.pow(FRICTION_PER_16MS, dt / 16);
      if (Math.abs(velocityRef.current) > MIN_VELOCITY) {
        momentumFrame.current = requestAnimationFrame(step);
      } else {
        momentumFrame.current = null;
      }
    };
    momentumFrame.current = requestAnimationFrame(step);
  }, []);

  const angleFromCenter = useCallback((clientX, clientY) => {
    const rect = ringRef.current.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    return (Math.atan2(clientY - cy, clientX - cx) * 180) / Math.PI;
  }, []);

  const handlePointerDown = (e) => {
    // Don't start a ring-drag when the press lands on a real button
    // (INJECT/TRIGGER/DIAGNOSE & FIX/etc.) -- those need a normal click,
    // not to be swallowed into a rotation gesture.
    if (e.target.closest("button")) return;
    // Real bug found via user testing: without this, a mouse-drag over
    // bead label text starts the browser's own native text-selection
    // drag instead of (or alongside) the rotation -- and that native
    // selection-drag is ALSO what was auto-scrolling the page when the
    // cursor neared the viewport edge (a browser behavior tied to
    // selection dragging, nothing this component built). preventDefault
    // here stops the native drag/selection gesture from ever starting.
    e.preventDefault();
    stopMomentum(); // grabbing mid-spin cancels any leftover momentum immediately
    dragState.current = {
      startPointerAngle: angleFromCenter(e.clientX, e.clientY),
      startRotation: rotationDeg,
      pointerId: e.pointerId,
    };
    lastSample.current = { rotation: rotationDeg, time: performance.now() };
    velocityRef.current = 0;
    setDragging(true);
    // Defensive only -- real mouse/touch input always has an active
    // pointer id a browser will accept here; this only guards against
    // rare edge cases (and synthetic/automated test dispatches) where it
    // doesn't, so a capture failure never crashes the actual drag.
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* non-fatal */
    }
  };

  const handlePointerMove = (e) => {
    if (!dragState.current || dragState.current.pointerId !== e.pointerId) return;
    const currentAngle = angleFromCenter(e.clientX, e.clientY);
    const delta = currentAngle - dragState.current.startPointerAngle;
    const newRotation = dragState.current.startRotation + delta;
    setRotationDeg(newRotation);

    const now = performance.now();
    const dt = now - lastSample.current.time;
    if (dt > 0) {
      // Only the MOST RECENT sample's velocity matters (how fast the
      // pointer was moving right before release), not an average over
      // the whole drag -- this is what makes a slow-then-fast flick
      // produce real leftover spin instead of being dragged down by an
      // earlier slow portion of the same gesture.
      velocityRef.current = (newRotation - lastSample.current.rotation) / dt;
      lastSample.current = { rotation: newRotation, time: now };
    }
  };

  const endDrag = (e) => {
    if (!dragState.current || dragState.current.pointerId !== e.pointerId) return;
    dragState.current = null;
    setDragging(false);
    if (Math.abs(velocityRef.current) > MIN_VELOCITY) {
      runMomentum();
    }
  };

  return (
    <div className="glass-module hud-bracket border border-outline-variant/30">
      <div className="p-4 border-b border-outline-variant/30 flex items-center justify-between">
        <h2 className="font-headline-md text-lg">Fault Topology</h2>
        <span className="font-body-sm text-xs text-on-surface-variant">
          Drag the ring to rotate. Every trigger opens the storefront in a new tab for real-time verification.
        </span>
      </div>

      <div className="p-4 flex justify-center overflow-x-auto">
        {/* Real bug found via user testing: the ring div below is a square
            sized exactly to CONTAINER -- rotating a square by anything
            other than a multiple of 90deg increases its axis-aligned
            bounding box (a rotated square's corners stick out past its
            own sides), and this wrapper's `overflow-x-auto` above was
            registering that inflated box as real scrollable content, even
            though the actual visible beads never move outside the
            already-correctly-sized circle. `overflow-hidden` here clips
            the ring's rotated element box to the true visible area --
            nothing real gets clipped, since CONTAINER was already sized
            from the real bead layout, not guessed. */}
        <div className="relative shrink-0 overflow-hidden" style={{ width: CONTAINER, height: CONTAINER }}>
          <svg className="absolute inset-0 pointer-events-none" width={CONTAINER} height={CONTAINER}>
            <circle cx={center} cy={center} r={RADIUS} fill="none" stroke="#6b7280" strokeOpacity="0.25" />
          </svg>
          <div
            ref={ringRef}
            className={`absolute inset-0 select-none ${dragging ? "cursor-grabbing" : "cursor-grab"}`}
            style={{ transform: `rotate(${rotationDeg}deg)`, touchAction: "none" }}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          >
            {ALL_CLASSES.map((fc, i) => {
              const angle = (i / ALL_CLASSES.length) * 2 * Math.PI - Math.PI / 2; // index 0 at top, unrotated reference
              const x = center + RADIUS * Math.cos(angle);
              const y = center + RADIUS * Math.sin(angle);
              return (
                <div
                  key={fc}
                  className="absolute"
                  style={{ left: x, top: y, transform: `translate(-50%, -50%) rotate(${-rotationDeg}deg)` }}
                >
                  <Bead
                    fc={fc}
                    token={token}
                    role={role}
                    trustMap={trustMap}
                    episodeInFlight={episodeInFlight}
                    isActive={activeEpisode?.faultClass === fc}
                    live={live}
                    onTrigger={onTrigger}
                    onResolve={onResolve}
                    onStopHold={onStopHold}
                  />
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
