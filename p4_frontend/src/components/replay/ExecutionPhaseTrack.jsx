import { useLayoutEffect, useRef, useState } from "react";

const STEP_STATUS = {
  done: { icon: "check", css: "bg-correct-green text-on-primary border-correct-green" },
  // Real fix: bg-primary/20 (20% opacity) let the green line show straight
  // through an in_progress node -- the icon box is supposed to be the ONE
  // place the line is deliberately hidden, so it needs a real opaque fill
  // like every other reached state, not a translucent one.
  in_progress: { icon: "sync", css: "bg-primary text-on-primary border-primary" },
  failed: { icon: "close", css: "bg-error text-on-error border-error" },
  timed_out: { icon: "warning", css: "bg-error text-on-error border-error" },
  // Real contrast fix: an outline-variant border + no fill + outline-variant
  // icon color all sit near-black against this panel's own near-black
  // background (surface-container-lowest) -- a not-yet-reached node was
  // reading as an invisible void, not a muted future step. A visible fill
  // one step lighter than the panel + a brighter icon color fixes it while
  // staying clearly less prominent than any reached/active node.
  pending: { icon: "radio_button_unchecked", css: "border-outline bg-surface-container-high text-on-surface-variant/60" },
};

// `active` = the node the green line is CURRENTLY passing through (gets the
// looping pulse). Re-keyed on `reached` so the one-shot light-up pop plays
// exactly once, the instant a node flips from pending to reached. No
// background on the outer wrapper (icon + label) -- only the icon box
// itself is opaque, so the green line stays visible everywhere along its
// path EXCEPT the small square it's literally passing behind.
// Real fix for a geometry bug found live: the label used to be a normal
// flex-col child, so a long label ("WAITING FOR TERMINATION") made THAT
// node's flex item as wide as its text, not its icon -- flex `items-center`
// then centered the 24px icon inside that much-wider box, pushing the icon
// itself inward from the item's own edge. The rail's endpoints are computed
// assuming icon-width-only spacing, so the first/last icons (whose labels
// are short/long respectively) ended up inset from where the rail actually
// terminates, poking the line out past the box on both ends. Fixed by
// taking the label out of layout entirely (absolute, centered under the
// icon) -- every node's real flex-item width is now exactly the 24px icon,
// regardless of label length, so rail <-> icon alignment is exact.
function Node({ icon, label, css, reached, active }) {
  return (
    <div data-phase-node className="relative flex items-center justify-center w-6 h-6 z-10">
      <div
        key={reached ? "on" : "off"}
        className={`phase-node-ring w-6 h-6 rounded flex items-center justify-center border ${css} ${
          reached ? "phase-tick-light-up" : ""
        } ${active ? "phase-node-active" : ""}`}
      >
        <span className={`phase-node-icon material-symbols-outlined text-[15px] ${icon === "sync" ? "phase-icon-sync" : ""}`}>{icon}</span>
      </div>
      <span
        className={`absolute top-full mt-1.5 left-1/2 -translate-x-1/2 font-label-caps text-[9px] text-center whitespace-nowrap ${
          reached ? "text-on-surface-variant" : "text-outline-variant"
        }`}
      >
        {label}
      </span>
    </div>
  );
}

// Real, always-present start marker -- every episode's tracker begins here,
// regardless of fault class or whether an action was ever taken.
const START_NODE = { label: "START", status: "done", icon: "flag" };

// The two leading "gate" nodes (semantic validation, RBAC) are a real, fixed
// mechanism every dispatched action passes through (tool_call_validator.py
// in front of the blast-radius cage, same static-config framing as
// SecurityCage) -- not per-episode measured data, so shown as a constant
// pair of nodes rather than fabricated per-episode pass/fail. The real
// per-episode data is action_result.progress_log's ordered steps.
//
// Built for EVERY fault class, not just the 6 auto-fix ones: a report-only
// episode (no action_result at all) never reaches the gates/steps, so its
// track is just START -> VERDICT -- still real, still shows something,
// instead of the old behavior of rendering nothing at all for that episode.
//
// `times` (real seconds, only meaningful in Replay mode) are what actually
// fixes the tracker-vs-content desync: each progress_log step's tick now
// lights up at that SAME step's real `action-step-N` schedule unit's own
// `.end` (replaySchedule.js -- when its line actually finishes typing),
// not an even index/(N-1) fraction of the whole episode. Without a real
// schedule (Snapshot, or before Replay's units are ready), times fall back
// to an even 0..1 spread -- harmless, since Snapshot always renders fully
// reached regardless of node position.
export function buildTrack(episode, actionStepUnits, totalDuration, durabilityUnit) {
  const hasSchedule = Array.isArray(actionStepUnits) && actionStepUnits.length > 0 && totalDuration > 0;
  const progressLog = episode.action_result?.progress_log;

  if (episode.scores_action_taken) {
    const steps = progressLog && progressLog.length > 0
      ? progressLog.map((s) => ({ label: s.step.replace(/_/g, " ").toUpperCase(), status: s.status }))
      : [{ label: episode.scores_action_taken.replace(/_/g, " ").toUpperCase(), status: episode.action_applied ? "done" : "failed" }];

    // Real dedupe fix: some real progress_log arrays (disk-full's restore
    // action) already end with their own genuine "complete: done" entry --
    // appending a synthetic COMPLETE node unconditionally on top of that
    // produced two identical-looking COMPLETE ticks. Only add the
    // synthetic fallback when the real log DOESN'T already end with one.
    const lastStepIsComplete = steps.length > 0 && steps[steps.length - 1].label === "COMPLETE";
    // RBAC_CHECK dropped, 2026-08-XX: it has no corresponding visible
    // content anywhere in the episode column (unlike SEMANTIC_VAL, which
    // at least maps conceptually onto the tool-call-validator gate) --
    // with no real pacing to sync to, the line just jumped from
    // SEMANTIC_VAL to RBAC_CHECK instantly and reads as noise, not a real
    // milestone. SEMANTIC_VAL alone still stands for "the real pre-dispatch
    // validation gate" (tool_call_validator.py, in front of the RBAC cage).
    const nodes = [
      START_NODE,
      { label: "SEMANTIC_VAL", status: "done" },
      ...steps,
      ...(lastStepIsComplete ? [] : [{ label: "COMPLETE", status: episode.action_applied ? "done" : "failed" }]),
    ];

    let times;
    if (hasSchedule) {
      // SEMANTIC_VAL has no real duration or visible content of its own (a
      // constant, instant mechanism, nothing to type) -- it fires right
      // before dispatch, so it's placed just before the first real action
      // step's own start, not spread across the whole preceding reasoning
      // span (which was lighting it up mid-way through unrelated
      // OBSERVATION/HYPOTHESIS text).
      const firstStart = actionStepUnits[0].start;
      const gateLead = Math.min(0.3, firstStart / 4);
      const stepEnds = steps.map((_, i) => actionStepUnits[i]?.end ?? actionStepUnits[actionStepUnits.length - 1].end);
      const scheduleEnd = durabilityUnit ? durabilityUnit.end : (stepEnds[stepEnds.length - 1] ?? totalDuration);

      // Real bug found live: when the real progress_log already ends with
      // its own "complete" entry (disk-full), that node's time was still
      // just actionStepUnits[last].end -- the moment THAT log line finishes
      // typing, not the real episode end. trust-context/durability still
      // had real content left to reveal after it, so the tracker sat fully
      // "done" for a long silent tail while the seekbar kept moving. The
      // tracker's FINAL node always represents true episode completion, so
      // its own time is always overridden to the real schedule end --
      // regardless of whether the underlying node is the real log's own
      // "complete" entry or the synthetic fallback below. The text LINE
      // itself in the Action Taken panel still finishes typing at its own
      // real (earlier) time -- only the TRACKER tick for it is deferred,
      // which is correct: that log line means "the fix was applied," not
      // "the whole episode, including durability confirmation, is done."
      if (lastStepIsComplete) {
        stepEnds[stepEnds.length - 1] = scheduleEnd;
      }
      times = [0, Math.max(0, firstStart - gateLead), ...stepEnds];
      if (!lastStepIsComplete) {
        times.push(scheduleEnd);
      }
    } else {
      times = nodes.map((_, i) => (nodes.length > 1 ? i / (nodes.length - 1) : 0));
    }
    return { nodes, times };
  }

  // Real bug fix: report-only episodes never have actionStepUnits (no
  // action was taken), so the top-level `hasSchedule` check above was
  // always false for them, silently falling to a fake `[0, 1]` timeline
  // regardless of the episode's real (often much longer) totalDuration --
  // VERDICT would "reach" after 1 fake second instead of the real end.
  // Report-only's own real requirement is just a real totalDuration.
  const nodes = [START_NODE, { label: "VERDICT", status: episode.correct ? "done" : "failed" }];
  const times = totalDuration > 0 ? [0, totalDuration] : [0, 1];
  return { nodes, times };
}

// Real DOM measurement, not index-percentage math -- a node's true
// rendered pixel center (as a 0..1 fraction of the track row's own width)
// is what the green line's target position is driven by, so the tick
// lights up exactly when the line visually reaches the box's center, not
// an approximation that can drift from the real layout (flex gap
// distribution, border widths, sub-pixel rounding). Re-measures on resize
// and whenever the node COUNT changes (a different episode).
function useNodeFractions(nodeCount) {
  const containerRef = useRef(null);
  const [fracs, setFracs] = useState(null);

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    function measure() {
      const containerRect = container.getBoundingClientRect();
      const nodeEls = container.querySelectorAll("[data-phase-node]");
      if (nodeEls.length !== nodeCount || containerRect.width === 0) return;
      setFracs(
        Array.from(nodeEls).map((el) => {
          const r = el.getBoundingClientRect();
          const centerX = r.left + r.width / 2;
          return (centerX - containerRect.left) / containerRect.width;
        })
      );
    }
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(container);
    return () => ro.disconnect();
  }, [nodeCount]);

  return { containerRef, fracs };
}

// Real dispatched magnitude value -- action_result.limit (patch_memory_limit/
// patch_cpu_limit) or .replicas (scale_deployment). Moved out to its own
// highlighted tile in the Telemetry Snapshot (EvidenceGrid) -- see that
// component's `dispatchedField` prop -- this module no longer renders it.
export function getDispatchedField(episode) {
  if (episode.action_result?.limit != null) {
    return { label: "DISPATCHED LIMIT", value: episode.action_result.limit, unit: "" };
  }
  if (episode.action_result?.replicas != null) {
    return { label: "DISPATCHED REPLICAS", value: episode.action_result.replicas, unit: "" };
  }
  return null;
}

// Persistent, always-visible, whole-episode progress strip -- no longer
// gated on scores_action_taken (every fault class gets a track now) and no
// longer driven by a local `revealCount` scoped to the action phase alone.
// `actionStepUnits` (Replay mode -- the real `action-step-N` schedule units
// from replaySchedule.js) drives real content-synced tick timing; without
// it (Snapshot's own call site), the track renders fully complete, matching
// the old always-full static behavior.
export default function ExecutionPhaseTrack({ episode, elapsed, totalDuration, actionStepUnits, durabilityUnit, playing = false }) {
  const { nodes: allNodes, times } = buildTrack(episode, actionStepUnits, totalDuration, durabilityUnit);
  const { containerRef, fracs } = useNodeFractions(allNodes.length);

  const isSnapshot = elapsed == null || totalDuration == null || totalDuration <= 0;

  // Real fix: the line used to be drawn from the container's literal left
  // edge (x=0), but START's own real measured center (fracs[0]) is never
  // exactly 0 -- so a thin sliver of green was always poking out before
  // the START icon itself. The line now starts exactly at START's real
  // measured position and only ever grows rightward from there.
  const startFrac = fracs ? fracs[0] : 0;

  let reachedFlags, activeIdx, currentFrac;
  if (isSnapshot) {
    reachedFlags = allNodes.map(() => true);
    activeIdx = -1;
    // Real measured fraction of the LAST node's own center -- not a flat
    // 100% -- so the line's end still lands exactly on the final tick's
    // real position instead of overshooting past it (the same overflow
    // bug fixed earlier, now driven by real measurement instead of a
    // fixed inset guess).
    currentFrac = fracs ? fracs[fracs.length - 1] : 1;
  } else {
    reachedFlags = times.map((t) => elapsed >= t);
    activeIdx = reachedFlags.lastIndexOf(true);
    if (!fracs) {
      currentFrac = startFrac;
    } else {
      // Piecewise-linear interpolation between consecutive nodes' REAL
      // measured centers, keyed to the SAME real content-sync times used
      // for reachedFlags above -- the fill reaches each node's true pixel
      // center at exactly the moment that node's real content actually
      // finishes, both in time AND in position.
      let i = 0;
      while (i < times.length - 1 && elapsed >= times[i + 1]) i++;
      const segStart = times[i];
      const segEnd = times[i + 1] ?? segStart;
      const fracStart = fracs[i] ?? 0;
      const fracEnd = fracs[i + 1] ?? fracStart;
      const segProgress = segEnd > segStart ? Math.min(1, Math.max(0, (elapsed - segStart) / (segEnd - segStart))) : 1;
      currentFrac = fracStart + (fracEnd - fracStart) * segProgress;
    }
  }
  const lineLeftPct = startFrac * 100;
  const lineWidthPct = Math.max(0, (currentFrac - startFrac) * 100);

  return (
    <div className="border border-outline-variant bg-surface-container-lowest p-4 pb-8 relative">
      <div className="absolute top-0 left-3 -translate-y-1/2 px-2 py-0.5 bg-surface-variant font-label-caps text-[9px] text-on-surface-variant">
        EXECUTION_PHASE_TRACK
      </div>
      {/* Real fix, paired with Node's own layout change above: with the
          label now taken out of flex flow (absolute, below the icon), the
          row no longer reserves vertical space for it -- pb-8 on the panel
          (up from p-4 on every side) reserves that room so labels aren't
          clipped by the panel's own bottom border. */}
      <div ref={containerRef} className="flex items-center justify-between relative mt-2 px-4">
        <div className="absolute top-1/2 left-0 right-0 h-px bg-outline-variant/30 -z-0 -translate-y-1/2" />
        <div
          className="absolute top-1/2 h-[2px] bg-correct-green -z-0 -translate-y-1/2"
          style={{ left: `${lineLeftPct}%`, width: `${Math.min(100 - lineLeftPct, lineWidthPct)}%` }}
        />
        {allNodes.map((n, i) => {
          const reached = reachedFlags[i];
          const style = reached ? (STEP_STATUS[n.status] ?? STEP_STATUS.done) : STEP_STATUS.pending;
          return (
            <Node
              key={i}
              icon={n.icon ?? style.icon}
              css={style.css}
              label={n.label}
              reached={reached}
              active={!isSnapshot && playing && i === activeIdx && elapsed < times[times.length - 1]}
            />
          );
        })}
      </div>
    </div>
  );
}
