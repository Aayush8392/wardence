// Real per-episode transition labeling -- trust_history/diagnoser_mode_
// history/action_trust_history each record a `state_before`/`state_after`
// pair FOR THIS EXACT episode_id (publish_to_r2.py's build_episodes(),
// 2026-08-XX addition), so "did this specific episode's own outcome
// promote or demote the class" is a real, derivable fact, not a guess.
// The three dimensions' "elevated" state names differ but the shape is
// identical (a lesser default vs. an earned state), so one shared set
// covers all three.
const ELEVATED_STATES = new Set(["can_act", "llm", "llm_can_act"]);
function transitionLabel(before, after) {
  if (before == null || after == null || before === after) return "";
  const wasElevated = ELEVATED_STATES.has(before);
  const isElevated = ELEVATED_STATES.has(after);
  if (!wasElevated && isElevated) return " (promoted)";
  if (wasElevated && !isElevated) return " (demoted)";
  return "";
}

// Pure -- no React, no rendering. Extracted so replaySchedule.js can measure
// the real body text's length for typewriter timing without duplicating
// this logic or guessing a duration.
//
// `episode` (optional) -- when present, DIM_A/B/C reflect what that
// dimension's state genuinely WAS at the moment this specific episode ran
// (real per-episode before/after data, see transitionLabel's comment
// above). A null per-episode value means "no real record for THIS episode"
// (an old episode from before that dimension's history table even existed,
// or a report-only class with no real Dimension A/C at all) -- real bug
// found live: this used to silently substitute the class's LIVE state
// instead, which reintroduces the exact mixed-signal confusion this whole
// feature exists to fix (confirmed on a real 2026-07-24 episode -- predates
// Phase H's LLM wiring entirely, `provider: null` correctly meaning "no
// LLM ever ran," yet the old code showed "DIM_B: llm" by masking the real
// "not tracked yet" answer with today's live class state). The DISPLAYED
// badges below are never silently backfilled with live data now -- only
// the headline's overall narrative falls back to live state when a
// per-episode Dimension A value is missing, since SOME narrative answer is
// better than none and the badge right next to it is what stays honest
// about what's actually known for this specific episode.
export function computeTrustContext(entry, episode) {
  if (!entry) return null;
  const isAutoFix = entry.tier === "auto-fix";
  const hasEpisode = episode != null;

  const dimAState = hasEpisode ? episode.dimension_a_before : entry.dimension_a?.state;
  const dimBState = hasEpisode ? episode.dimension_b_before : (entry.dimension_b?.mode ?? "stub");
  const dimCState = hasEpisode ? episode.dimension_c_before : (entry.dimension_c?.state ?? null);
  // Headline narrative: falls back to live state only when the per-episode
  // value itself is missing (rare -- Dimension A predates Phase H, so this
  // is basically only a truly ancient episode) -- SOME answer beats none.
  const canAct = (dimAState ?? entry.dimension_a?.state) === "can_act";

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
  return {
    headline, body, isAutoFix,
    dimA: { state: dimAState ?? "not tracked", transition: transitionLabel(episode?.dimension_a_before, episode?.dimension_a_after) },
    dimB: { state: dimBState ?? "not tracked", transition: transitionLabel(episode?.dimension_b_before, episode?.dimension_b_after) },
    dimC: isAutoFix ? { state: dimCState ?? "not tracked", transition: transitionLabel(episode?.dimension_c_before, episode?.dimension_c_after) } : null,
  };
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
// `opacity`/`active` (Replay only) -- real bug fixed alongside this: Replay
// was already passing a real `opacity` value here, but this component
// never actually destructured or applied it, so the card was always fully
// visible immediately regardless of the schedule (the body TEXT still
// typed in correctly via bodyText, opacity just silently did nothing).
export default function TrustContext({ entry, episode, bodyText, opacity = 1, active = false }) {
  const computed = computeTrustContext(entry, episode);
  if (!computed) return null;
  const { headline, body, isAutoFix, dimA, dimB, dimC } = computed;

  return (
    <div
      className={`border border-outline-variant bg-surface-container-low p-4 relative ${active ? "content-live-glow" : ""}`}
      style={{ opacity }}
    >
      <div className="absolute top-0 left-3 -translate-y-1/2 px-2 py-0.5 bg-surface-variant font-label-caps text-[9px] text-on-surface-variant">
        WHY THIS OUTCOME
      </div>
      <div className="font-label-caps text-[10px] text-primary mb-1.5">{headline}</div>
      <p className="font-data-md text-xs text-on-surface-variant leading-relaxed">{bodyText ?? body}</p>
      {/* Real per-episode state (dimA/B/C's `state` -- what this dimension
          genuinely WAS at the moment THIS episode ran), plus a real
          "(promoted)"/"(demoted)" bracket whenever this episode's own
          outcome is the one that actually caused that dimension's
          before -> after transition (trust_history/diagnoser_mode_history/
          action_trust_history's own recorded episode_id, not a guess). */}
      <div className="flex gap-4 mt-3 pt-3 border-t border-outline-variant/30 font-data-mono text-[10px] text-on-surface-variant">
        <span>DIM_A: <span className="text-on-surface">{isAutoFix ? dimA.state : "n/a"}</span>{isAutoFix && <span className="text-warning-amber">{dimA.transition}</span>}</span>
        <span>DIM_B: <span className="text-on-surface">{dimB.state}</span><span className="text-warning-amber">{dimB.transition}</span></span>
        {isAutoFix && dimC && (
          <span>DIM_C: <span className="text-on-surface">{dimC.state}</span><span className="text-warning-amber">{dimC.transition}</span></span>
        )}
      </div>
    </div>
  );
}
