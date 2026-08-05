// Pure -- no React, no rendering. Extracted so replaySchedule.js can measure
// the real body text's length for typewriter timing without duplicating
// this logic or guessing a duration.
export function computeTrustContext(entry) {
  if (!entry) return null;
  const isAutoFix = entry.tier === "auto-fix";
  const canAct = entry.dimension_a?.state === "can_act";

  let headline;
  let body;
  if (!isAutoFix) {
    headline = "REPORT-ONLY CLASS";
    body = `${entry.fault_class} has no safe, bounded ops-level fix — the system is designed to correctly ` +
      `withhold autonomy here rather than attempt one. Diagnosis only, by design.`;
  } else if (canAct) {
    headline = "AUTONOMY EARNED";
    body = `This class has earned autonomous fix rights (${entry.dimension_a.streak} consecutive correct ` +
      `outcome${entry.dimension_a.streak === 1 ? "" : "s"}) — a wrong or unsafe result here would demote it ` +
      `back to report-only.`;
  } else {
    headline = "STILL EARNING TRUST";
    body = `This class is capable of an autonomous fix but hasn't yet earned the right to act on its own — ` +
      `every real outcome here is still being measured toward that.`;
  }
  return { headline, body, isAutoFix };
}

// Honest explanation of WHY this episode did or didn't get an autonomous
// fix -- real Dimension A/B state, pulled from the same trust_ladder.json
// the Trust Ladder tab itself reads. Turns "there's nothing here" into
// "here's why there's nothing here," using data that already exists but
// was never surfaced inside the Replay Viewer before.
//
// `bodyText` optionally overrides the body paragraph (Replay passes a
// partially-typed slice of the same real string); omitted, it shows the
// full real body immediately -- Snapshot's own unchanged behavior.
export default function TrustContext({ entry, bodyText }) {
  const computed = computeTrustContext(entry);
  if (!computed) return null;
  const { headline, body, isAutoFix } = computed;

  return (
    <div className="border border-outline-variant bg-surface-container-low p-4 relative">
      <div className="absolute top-0 left-3 -translate-y-1/2 px-2 py-0.5 bg-surface-variant font-label-caps text-[9px] text-on-surface-variant">
        WHY THIS OUTCOME
      </div>
      <div className="font-label-caps text-[10px] text-primary mb-1.5">{headline}</div>
      <p className="font-data-md text-xs text-on-surface-variant leading-relaxed">{bodyText ?? body}</p>
      <div className="flex gap-4 mt-3 pt-3 border-t border-outline-variant/30 font-data-mono text-[10px] text-on-surface-variant">
        <span>DIM_A: <span className="text-on-surface">{isAutoFix ? entry.dimension_a.state : "n/a"}</span></span>
        <span>DIM_B: <span className="text-on-surface">{entry.dimension_b?.mode ?? "stub"}</span></span>
      </div>
    </div>
  );
}
