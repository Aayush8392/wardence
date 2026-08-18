import { useEffect, useState } from "react";
import LoadingDots from "../shared/LoadingDots";
import { useTickingElapsed } from "../../hooks/useTickingElapsed";
import { AUTO_FIX_CLASSES, REPORT_ONLY_CLASSES, CLASS_LABELS, FAULT_TARGETS } from "../../constants/faultClasses";

// Real "Functional Matrix" grid -- replaces the necklace ring (shelved in
// ./_shelved/TopologyMap.jsx, not deleted). Same real state logic as the
// old Bead component (idle/injecting/holding/awaiting_fix/fixing/resolved,
// the real countdown/elapsed math, the real fixRequested-driven "committed"
// state) -- only the container/visual skin changed, per explicit ask to
// match Stitch's rectangular-card layout (stitch_mockups/
// stitch_wardence_casefile_incident_detail_12.zip) while keeping our own
// mechanics underneath, not Stitch's.
//
// Two distinct click targets per card, per the locked panel design: the
// card BODY (name/label) opens the slide-out panel in dossier mode without
// triggering anything; the TRIGGER/action button injects a real fault (and
// implicitly opens/retargets the panel to this class, handled by the
// parent via onOpenPanel + onTrigger both firing).

function FaultCard({ fc, bucket, token, role, trustMap, episodeInFlight, crashLoopReady, isActive, isSelected, live, fixRequested, onTrigger, onDiagnoseAndFix, onOpenPanel }) {
  const isAutoFix = bucket === "auto-fix";
  const trustState = trustMap[fc];
  const canAct = Boolean(token) && (role === "admin" || role === "demo-trigger");
  const state = isActive ? (live?.episode_state ?? null) : null;
  const target = FAULT_TARGETS[fc]?.target;
  const isLive = isActive && state && state !== "resolved" && state !== "failed";

  const [injectPending, setInjectPending] = useState(false);
  useEffect(() => {
    if (isActive) setInjectPending(false);
  }, [isActive]);

  const elapsedS = useTickingElapsed(isActive ? live?.elapsed_in_state_s : null, isActive && Boolean(state));

  // Real fix, priority order corrected: fixRequested (the user already
  // committed to fixing) must win over the holding-countdown check --
  // otherwise, for the real ~10s gap between clicking "Diagnose & Fix"
  // and the hold loop's own tick actually flipping episode_state to
  // awaiting_fix, this used to keep showing "LIVE -- Xs LEFT" underneath
  // a button/label that already said FIXING, a real visible contradiction
  // found live 2026-08-15.
  let statusLine = null;
  if (isActive && fixRequested && state !== "resolved" && state !== "failed" && elapsedS != null) {
    statusLine = `${elapsedS}s ELAPSED`;
  } else if (isActive && state === "holding" && live?.hold_duration_s != null && elapsedS != null) {
    const remaining = Math.max(0, live.hold_duration_s - elapsedS);
    statusLine = `LIVE — ${remaining}s LEFT`;
  } else if (
    isActive && state === "awaiting_fix" && !fixRequested &&
    live?.abandon_ceiling_s != null && elapsedS != null
  ) {
    const remaining = Math.max(0, live.abandon_ceiling_s - elapsedS);
    statusLine = isAutoFix ? `AUTO-FIX IN ${remaining}s` : `RESOLVING IN ${remaining}s`;
  }

  const doTrigger = (e) => {
    e.stopPropagation();
    setInjectPending(true);
    onTrigger(fc);
  };

  const idleButton = (label) => {
    const disabled = !canAct || episodeInFlight || injectPending;
    return (
      <button onClick={doTrigger} disabled={disabled} className="tactical-btn px-2.5 py-1 font-label-caps text-[9px] text-primary disabled:opacity-40 flex items-center justify-center gap-1">
        {injectPending && <LoadingDots />} {label}
      </button>
    );
  };

  // Real, single "stage" derivation shared by both buckets -- every real
  // episode_state value gets its own distinct label (INJECTING/LIVE
  // FAULT/AWAITING FIX/FIXING/PUBLISHING), per explicit ask: "the fault
  // is live should come up separately" from the generic INJECTING label
  // it used to share the whole not-yet-ready window with, and the real
  // ~12s R2-republish tail gets its own honest PUBLISHING label rather
  // than implying the episode is fully done. Real fix, same session: the
  // button's own text now switches TRIGGER -> INJECTING while pending,
  // matching the state label underneath instead of staying static.
  let stateLabel;
  let actionButton;

  // Real, disabled-but-visible button -- matches the original Bead's own
  // treatment (a real <button disabled> with its own text/spinner), never
  // just a bare LoadingDots span with no button shape at all. Used for
  // every "waiting, nothing to click yet" stage.
  const waitingButton = (label) => (
    <button disabled className="px-2.5 py-1 bg-primary border border-primary text-on-primary font-label-caps text-[9px] font-bold opacity-40 cursor-not-allowed flex items-center justify-center gap-1">
      <LoadingDots /> {label}
    </button>
  );
  const diagnoseButton = (
    <button
      onClick={(e) => { e.stopPropagation(); onDiagnoseAndFix(); }}
      className="px-2.5 py-1 bg-primary border border-primary text-on-primary font-label-caps text-[9px] hover:brightness-110 font-bold"
    >
      DIAGNOSE & FIX
    </button>
  );
  // Report-only equivalent of diagnoseButton -- same handler underneath
  // (onDiagnoseAndFix -> handleStopHold for the "holding" state, works
  // identically for report-only classes since the backend's evidence-file
  // early-exit mechanism already covers all 6 of them), just a label that
  // doesn't imply a fix ever gets dispatched.
  const revertButton = (
    <button
      onClick={(e) => { e.stopPropagation(); onDiagnoseAndFix(); }}
      className="px-2.5 py-1 bg-primary border border-primary text-on-primary font-label-caps text-[9px] hover:brightness-110 font-bold"
    >
      REVERT
    </button>
  );

  // Real crash-loop warm-standby cooldown (Model A, locked -- see
  // wardence_crash_loop_warm_standby_LOCKED_SPEC.md). Derived live from
  // the cluster on the backend (operator_api.py's /trigger/status
  // crash_loop_ready field, itself derived from the real Service
  // selector + a real readiness check, never a stored flag) -- true
  // whenever carts hasn't been confirmed restored to steady state yet
  // from a prior episode. Checked only in the branch below that would
  // otherwise show a plain enabled TRIGGER -- deliberately does NOT
  // touch the "resolved && !republished_at" PUBLISHING branch just
  // below, so the real ~12s R2-publish tail still plays out naturally
  // before this cooldown state ever appears, per explicit ask: no
  // artificial wait, just an honest state once publishing is actually
  // done (or was never in flight to begin with, e.g. a fresh page
  // load).
  const crashLoopCoolingDown = fc === "crash-loop" && crashLoopReady === false;

  if (injectPending || !isActive || !state || (state === "resolved" && live?.republished_at) || state === "failed") {
    if (!injectPending && crashLoopCoolingDown) {
      stateLabel = "COOLDOWN";
      actionButton = waitingButton("TRIGGER");
    } else {
      stateLabel = injectPending ? "INJECTING" : "IDLE";
      actionButton = idleButton(injectPending ? "INJECTING" : "TRIGGER");
    }
  } else if (state === "resolved" && !live?.republished_at) {
    // Real, honest intermediate stage -- diagnosis/action/durability are
    // all genuinely done, but the ~12s R2 publish hasn't landed, so the
    // card isn't safe to re-trigger yet. Applies to both buckets.
    stateLabel = "PUBLISHING";
    actionButton = waitingButton("PUBLISHING");
  } else if (state === "injecting") {
    stateLabel = "INJECTING";
    actionButton = waitingButton("INJECTING");
  } else if (fixRequested) {
    // Real for both buckets now: report-only classes can also set
    // fixRequested (clicking REVERT during "holding"), and need their own
    // honest label for the ~10s gap before the backend's hold loop
    // actually notices the stop request and flips episode_state.
    stateLabel = isAutoFix ? "FIXING" : "REVERTING";
    actionButton = waitingButton(isAutoFix ? "FIXING" : "REVERTING");
  } else if (state === "holding") {
    // Real, deliberate correction: the fault is genuinely LIVE (and the
    // statusLine's own countdown above already says so) for the WHOLE
    // holding window, not just once evidence confirms -- the label must
    // never contradict the countdown by still saying INJECTING here.
    // Only the BUTTON's enabled/disabled state depends on
    // live.can_stop_hold_early (evidence confirmed enough to act), never
    // the label text. Report-only classes get the same early-exit option
    // as auto-fix (REVERT instead of DIAGNOSE & FIX) -- the backend's
    // evidence-file mechanism already covers all 6 report-only classes,
    // this was previously frontend-only-disabled with no real reason.
    stateLabel = "LIVE FAULT";
    if (live?.can_stop_hold_early) {
      actionButton = isAutoFix ? diagnoseButton : revertButton;
    } else {
      actionButton = waitingButton("CONFIRMING");
    }
  } else if (state === "awaiting_fix") {
    stateLabel = "AWAITING FIX";
    actionButton = isAutoFix ? diagnoseButton : waitingButton("AWAITING FIX");
  } else {
    // state === "resolving" (report-only auto-chain, or auto-fix without
    // fixRequested somehow set -- shouldn't happen but falls back safely).
    stateLabel = "FIXING";
    actionButton = waitingButton("FIXING");
  }

  return (
    <div
      role="button"
      tabIndex={0}
      // Real toggle: clicking the card that's already the open panel's
      // target closes it instead of re-opening itself -- delegated to the
      // parent (Operator/index.jsx owns openPanelClass), this component
      // only ever calls onOpenPanel(fc), never null, itself.
      onClick={() => onOpenPanel(fc)}
      onKeyDown={(e) => { if (e.key === "Enter") onOpenPanel(fc); }}
      className={`glass-module flex flex-col gap-2 p-3 cursor-pointer transition-colors ${
        isLive || isSelected ? "border-2 border-primary" : "border border-outline-variant/30 hover:border-outline-variant"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-data-mono text-xs text-on-surface leading-tight">{CLASS_LABELS[fc]}</span>
        <span
          className={`shrink-0 font-label-caps text-[9px] px-1.5 py-0.5 border flex items-center gap-1 ${
            isAutoFix ? "border-primary text-primary" : "border-outline-variant text-on-surface-variant"
          }`}
          title={isAutoFix && trustState ? (trustState === "can_act" ? "CAN-ACT" : trustState.toUpperCase()) : undefined}
        >
          {isAutoFix && trustState && (
            <span className={`w-1.5 h-1.5 rounded-full ${trustState === "can_act" ? "bg-[#238636]" : "bg-outline-variant"}`} />
          )}
          {isAutoFix ? "AF" : "RP"}
        </span>
      </div>
      <span className="font-label-caps text-[9px] text-on-surface-variant">{target}</span>

      {statusLine && <span className="font-label-caps text-[8px] text-warning-amber">{statusLine}</span>}

      <div className="mt-auto flex items-center justify-between gap-2 pt-1">
        <span className={`font-label-caps text-[9px] ${isLive ? "text-primary" : "text-on-surface-variant"}`}>{stateLabel}</span>
        {actionButton}
      </div>
    </div>
  );
}

function Section({ title, bucket, classes, activeFaultClass, selectedFaultClass, ...cardProps }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 ${bucket === "auto-fix" ? "bg-primary" : "bg-outline-variant"}`} />
          <h3 className="font-label-caps text-xs text-on-surface-variant tracking-wide">{title}</h3>
        </div>
        <span className="font-label-caps text-[10px] text-on-surface-variant">{classes.length} CLASSES</span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {classes.map((fc) => (
          <FaultCard
            key={fc}
            fc={fc}
            bucket={bucket}
            isActive={activeFaultClass === fc}
            isSelected={selectedFaultClass === fc}
            {...cardProps}
          />
        ))}
      </div>
    </div>
  );
}

export default function FaultGrid({ token, role, trustMap, episodeInFlight, crashLoopReady, activeEpisode, selectedFaultClass, live, fixRequested, onTrigger, onDiagnoseAndFix, onOpenPanel }) {
  const cardProps = { token, role, trustMap, episodeInFlight, crashLoopReady, live, fixRequested, onTrigger, onDiagnoseAndFix, onOpenPanel };

  return (
    <div className="glass-module hud-bracket border border-outline-variant/30 p-4 space-y-6">
      <Section title="AUTO-FIX CAPABLE" bucket="auto-fix" classes={AUTO_FIX_CLASSES} activeFaultClass={activeEpisode?.faultClass} selectedFaultClass={selectedFaultClass} {...cardProps} />
      <Section title="DIAGNOSTIC ONLY" bucket="report-only" classes={REPORT_ONLY_CLASSES} activeFaultClass={activeEpisode?.faultClass} selectedFaultClass={selectedFaultClass} {...cardProps} />
    </div>
  );
}
