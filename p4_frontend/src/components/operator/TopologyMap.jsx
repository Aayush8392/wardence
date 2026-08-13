import { useCallback, useEffect, useRef, useState } from "react";
import LoadingDots from "../shared/LoadingDots";
import CentralThinkingHub from "./CentralThinkingHub";
import { useTickingElapsed } from "../../hooks/useTickingElapsed";
import { ALL_CLASSES, AUTO_FIX_CLASSES, CLASS_LABELS, FAULT_TARGETS } from "../../constants/faultClasses";

// Real "necklace loop" -- 12 fault classes as oval nodes evenly spaced on
// one ring, connected by a real circular guide line, drag-to-rotate in
// either direction. One bead per FAULT CLASS (not grouped by service like
// the earlier grid attempt) -- this is what actually fixes the original
// uneven-node-height problem: every bead now holds exactly one fault, so
// there's nothing left to be uneven.
const PILL_W = 188;
// Bumped from 96 -> 116, per real content need: an active episode now
// shows up to 5 real lines (label, target, statusLine, button, and the
// button's own icon+text row) -- 96px was sized before statusLine
// existed. Does not affect ring spacing (RADIUS's chord-distance formula
// only depends on PILL_W/GAP), only the pill's own visual height.
const PILL_H = 116;

// Real radius, computed from the real pill size, not copied from Stitch's
// mockup (its spacing didn't account for real label lengths and produced
// visible overlap). Chord distance between two adjacent bead centers on a
// 12-point circle is 2*R*sin(15deg); this must exceed the pill's own
// width plus a real gap for the pills to never touch.
const GAP = 20;
const RADIUS = Math.ceil((PILL_W + GAP) / (2 * Math.sin(Math.PI / 12))) + 20; // +20 margin
const CONTAINER = RADIUS * 2 + PILL_W + 40;

// Real clear-zone sizing for the Central Thinking Hub, sitting in the
// ring's empty center (wardence_frontend.md's own "reuses dead space"
// framing) -- derived from the real bead geometry above (RADIUS minus
// half a bead's width plus the real gap: the true unobstructed distance
// from ring center to the nearest bead edge), not a guessed constant.
// Real fix, per explicit "too small" feedback: the Hub is a RECTANGLE
// (wider than tall), not a circle -- the old version capped itself to a
// small circle diameter even though a rectangle inscribed in the same
// clear zone can be meaningfully larger. Since the ring can rotate to
// any angle, the safe rectangle must fit inside the clear zone from
// EVERY angle -- i.e. its own half-diagonal must stay within the clear
// radius (an 8% margin kept for real visual breathing room, not the
// mathematical maximum).
const HUB_CLEAR_RADIUS = RADIUS - PILL_W / 2 - GAP;
const HUB_ASPECT = 1.4; // wider than tall
const HUB_MAX_DIAG_HALF = HUB_CLEAR_RADIUS * 0.92;
const HUB_H = Math.round((HUB_MAX_DIAG_HALF * 2) / Math.sqrt(1 + HUB_ASPECT * HUB_ASPECT));
const HUB_W = Math.round(HUB_H * HUB_ASPECT);

function Bead({ fc, token, role, trustMap, episodeInFlight, isActive, live, fixRequested, onTrigger, onDiagnoseAndFix }) {
  const isAutoFix = AUTO_FIX_CLASSES.includes(fc);
  const trustState = trustMap[fc];
  const canAct = Boolean(token) && (role === "admin" || role === "demo-trigger");
  // `live?.episode_state` is `undefined` (not `null`) whenever `live`
  // itself is null -- normalized so "no real state yet" is always the
  // same value, regardless of exactly why it's missing.
  const state = isActive ? (live?.episode_state ?? null) : null;
  const target = FAULT_TARGETS[fc]?.target;

  // Real, immediate optimistic "Inject Fault was clicked" flag -- only
  // covers the brief window between the click and `isActive` actually
  // becoming true (the parent confirming this bead's episode exists).
  // Once isActive is true, real polled `state` takes over entirely --
  // this does NOT try to track the fix-in-progress lifecycle at all
  // (that's `fixRequested`, lifted to the parent -- see its own
  // declaration comment in Operator/index.jsx for why a per-bead flag
  // cleared by state-diffing alone was a real, confirmed bug).
  const [injectPending, setInjectPending] = useState(false);
  useEffect(() => {
    if (isActive) setInjectPending(false);
  }, [isActive]);

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

  // Real, smoothly-ticking elapsed time in the CURRENT state -- see
  // useTickingElapsed's own docstring. Real base value comes straight
  // from live-status's `elapsed_in_state_s`; only interpolates the
  // seconds between polls, never invents a number the server hasn't
  // confirmed.
  const elapsedS = useTickingElapsed(isActive ? live?.elapsed_in_state_s : null, isActive && Boolean(state));

  // Real, SEPARATE status line -- per explicit correction, this must
  // never occupy the same slot as the action button (the two used to
  // swap for each other, losing the "fault is live" signal the instant
  // the button became clickable). Every number here is real: the
  // fault's own real hold duration (live.hold_duration_s, a locked
  // per-class constant the backend now includes) and the real 5-minute
  // abandonment ceiling (live.abandon_ceiling_s), both minus the real
  // ticking elapsed time above -- never guessed.
  let statusLine = null;
  if (isActive && state === "holding" && live?.hold_duration_s != null && elapsedS != null) {
    const remaining = Math.max(0, live.hold_duration_s - elapsedS);
    statusLine = `LIVE — ${remaining}s LEFT`;
  } else if (
    isActive && state === "awaiting_fix" && !fixRequested &&
    live?.abandon_ceiling_s != null && elapsedS != null
  ) {
    const remaining = Math.max(0, live.abandon_ceiling_s - elapsedS);
    statusLine = `AUTO-FIX IN ${remaining}s`;
  } else if (isActive && fixRequested && state !== "resolved" && state !== "failed" && elapsedS != null) {
    // Covers both the real ~10s wait for a holding class's stop-flag to
    // land AND the real diagnosis/action/durability call once resolving
    // genuinely starts -- one continuous count-up, since there's no
    // real fixed total to count down to (a real fallback-chain cascade
    // can genuinely take longer than the common case).
    statusLine = `${elapsedS}s ELAPSED`;
  }

  let button;
  if (!isAutoFix) {
    // Report-only classes have no fix action -- unchanged, just the same
    // instant-disable treatment on the trigger click itself.
    if (isActive && state && state !== "resolved" && state !== "failed") {
      button = (
        <span className="flex items-center gap-1 font-label-caps text-[9px] text-primary">
          <LoadingDots /> {state.replace("_", " ").toUpperCase()}
        </span>
      );
    } else {
      const disabled = !canAct || episodeInFlight || injectPending;
      button = (
        <button
          onClick={() => { setInjectPending(true); onTrigger(fc); }}
          disabled={disabled}
          title={!token ? "Sign in to trigger faults" : episodeInFlight ? "An episode is already in flight" : undefined}
          className="tactical-btn px-2.5 py-1 font-label-caps text-[9px] text-primary disabled:opacity-40"
        >
          {injectPending && <LoadingDots />} TRIGGER
        </button>
      );
    }
  } else if (isActive && state === "resolved" && !live?.republished_at) {
    // Real, honest intermediate label for the real ~12s R2-republish
    // gap -- the episode really is resolved (the Hub's own Resolution
    // frame already says so), but this button's own job (letting the
    // user act again) genuinely isn't safe until the published data
    // catches up. Named for what's actually happening, not hidden
    // behind a generic spinner.
    button = (
      <span className="flex items-center gap-1 font-label-caps text-[9px] text-primary">
        <LoadingDots /> DOCUMENTING EPISODE
      </span>
    );
  } else if (injectPending || !isActive || !state || state === "resolved" || state === "failed") {
    // Genuinely idle, the click just fired (injectPending), or fully
    // terminal (resolved+republished, or failed) -- back to the real
    // starting point.
    const disabled = !canAct || episodeInFlight || injectPending;
    button = (
      <button
        onClick={() => { setInjectPending(true); onTrigger(fc); }}
        disabled={disabled}
        title={!token ? "Sign in to trigger faults" : episodeInFlight ? "An episode is already in flight" : undefined}
        className="tactical-btn px-2.5 py-1 font-label-caps text-[9px] text-primary disabled:opacity-40 flex items-center justify-center gap-1"
      >
        {injectPending && <LoadingDots />} {injectPending ? "INJECTING" : "INJECT FAULT"}
      </button>
    );
  } else if (state === "injecting") {
    button = (
      <span className="flex items-center gap-1 font-label-caps text-[9px] text-primary">
        <LoadingDots /> INJECTING
      </span>
    );
  } else if (fixRequested) {
    // Real, single "committed" state -- covers the whole real gap from
    // the click through to resolution: the ~10s wait for a holding
    // class's stop-flag to actually land, AND the real diagnosis/action
    // call once resolving genuinely starts. Driven by the durable
    // `fixRequested` flag, not by `state` alone -- this is what fixes
    // the real "re-enabled for a couple seconds" bug (state flips
    // holding->awaiting_fix before the chained resolve call has actually
    // landed; fixRequested stays true straight through that gap).
    button = (
      <span className="flex items-center gap-1 font-label-caps text-[9px] text-primary">
        <LoadingDots /> FIXING
      </span>
    );
  } else if (state === "holding" && !live?.can_stop_hold_early) {
    // Real, deliberate DISABLED placeholder -- the fault is genuinely
    // live (statusLine above already says so, persistently), but
    // there's no real fix decision to make yet (evidence hasn't
    // confirmed). Kept as the same button shape/slot rather than
    // disappearing, per explicit correction: the action button should
    // always be present, never swapped out for a status line. Real
    // correction: labeled "INJECTING", not a disabled "Diagnose & Fix"
    // placeholder -- per explicit ask, the button should keep saying
    // INJECTING for the WHOLE not-yet-ready window, not just the
    // literal `injecting` DB state.
    button = (
      <button disabled className="px-2.5 py-1 bg-primary border border-primary text-on-primary font-label-caps text-[9px] font-bold opacity-40 cursor-not-allowed flex items-center justify-center gap-1">
        <LoadingDots /> INJECTING
      </button>
    );
  } else {
    // Real, genuine decision point: holding+evidence-confirmed, or
    // awaiting_fix -- the only two moments this button is ever enabled.
    button = (
      <button
        onClick={onDiagnoseAndFix}
        className="px-2.5 py-1 bg-primary border border-primary text-on-primary font-label-caps text-[9px] hover:brightness-110 font-bold"
      >
        DIAGNOSE & FIX
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
      {statusLine && (
        <span className="font-label-caps text-[8px] text-warning-amber">{statusLine}</span>
      )}
      {button}
    </div>
  );
}

export default function TopologyMap({ token, role, trustMap, episodeInFlight, activeEpisode, live, fixRequested, onTrigger, onDiagnoseAndFix }) {
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
                    fixRequested={fixRequested}
                    onTrigger={onTrigger}
                    onDiagnoseAndFix={onDiagnoseAndFix}
                  />
                </div>
              );
            })}
          </div>
          {/* Real bug found + fixed: this used to render BEFORE ringRef
              below, which is `absolute inset-0` (covers the WHOLE
              container, not just the visible ring path) -- a later
              sibling paints over an earlier one at the same stacking
              level regardless of visual transparency, so ringRef was
              silently swallowing every pointer event over this exact
              region even though nothing was drawn there. Rendering the
              hub AFTER ringRef in the DOM (not just relying on
              pointer-events tricks) is what actually lets clicks/scroll
              reach it -- confirmed the simplest real fix, no CSS
              stacking-context juggling needed. Still not a child of
              ringRef -- must stay upright regardless of ring rotation,
              same reasoning as each bead's own counter-rotation. */}
          <div
            className="absolute pointer-events-none"
            style={{ left: center, top: center, transform: "translate(-50%, -50%)" }}
          >
            <div className="pointer-events-auto">
              <CentralThinkingHub
                episodeId={activeEpisode?.episodeId}
                episodeState={live?.episode_state}
                live={live}
                token={token}
                width={HUB_W}
                height={HUB_H}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
