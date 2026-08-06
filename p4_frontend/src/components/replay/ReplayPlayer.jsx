import { useMemo } from "react";
import { buildReplaySchedule, fadeProgress, typedText } from "./replaySchedule";
import useReplayEngine from "../../hooks/useReplayEngine";
import ReplaySeekbar from "./ReplaySeekbar";
import { HeaderStrip, ServiceContext } from "./CaseFile";
import { HighlightedSentence, ActionProgress } from "./ReasoningStream";
import EvidenceGrid from "./EvidenceGrid";
import ExecutionPhaseTrack, { getDispatchedField, buildTrack } from "./ExecutionPhaseTrack";
import GateSubstitution from "./GateSubstitution";
import TrustContext from "./TrustContext";
import DurabilityVerdict from "./DurabilityVerdict";
import SecurityCage from "../shared/SecurityCage";

function findUnit(units, section) {
  return units.find((u) => u.section === section);
}
function findUnits(units, section) {
  return units.filter((u) => u.section === section);
}

// Real "is this card currently being built" check -- true from the moment
// a unit starts fading/typing in until it (or, for `endOverride`, some
// LATER real milestone it's genuinely still waiting on) finishes. Drives
// the pulsating blue ring so the user can see what's actively happening,
// distinct from opacity (which only governs the fade-in itself).
function isLive(unit, elapsed, endOverride) {
  if (!unit) return false;
  const end = endOverride ?? unit.end;
  return elapsed >= unit.start && elapsed < end;
}

// Fixed content-shape rules (locked, not decided per class/episode):
//   instant          -- header only, always full opacity.
//   fade              -- a fact/tile-batch: fades in as a whole.
//   fade-typewriter   -- real prose: container fades in, text types beneath.
// Every section below is ALWAYS mounted in its real Snapshot position --
// only opacity/text change over time, so the layout never shifts, and
// scrubbing backward un-fades/un-types for free (pure functions of elapsed).
export default function ReplayPlayer({ episode, trustEntry, actions }) {
  const { units, totalDuration } = useMemo(() => buildReplaySchedule(episode, trustEntry), [episode, trustEntry]);
  const engine = useReplayEngine(totalDuration);
  const { elapsed } = engine;

  const contextUnit = findUnit(units, "context");
  const sentenceUnits = findUnits(units, "reasoning-sentence");
  const turnUnits = findUnits(units, "reasoning-turn");
  const decisionUnit = findUnit(units, "reasoning-decision");
  const evidencePrimaryUnit = findUnit(units, "evidence-primary");
  const evidenceSecondaryUnit = findUnit(units, "evidence-secondary");
  const actionStepUnits = findUnits(units, "action-step");
  const gateUnit = findUnit(units, "gate-substitution");
  const trustUnit = findUnit(units, "trust-context");
  const durabilityUnit = findUnit(units, "durability");

  // Real fix: skip forward/back used to jump between every raw schedule
  // unit (each reasoning sentence, each evidence tile-batch, etc.) --
  // 3+ clicks just to cross from START to SEMANTIC_VAL. Reusing the exact
  // same real `times` ExecutionPhaseTrack computes for its own tick
  // milestones means skip now jumps between the same NAMED milestones the
  // tracker shows, and guarantees they can never drift out of sync with
  // each other (single source of truth).
  const milestones = useMemo(
    () => buildTrack(episode, actionStepUnits, totalDuration, durabilityUnit).times,
    [episode, actionStepUnits, totalDuration, durabilityUnit]
  );

  const reachedActionSteps = actionStepUnits.filter((u) => elapsed >= u.start);
  const actionRevealCount = reachedActionSteps.length;
  const activeActionStep = reachedActionSteps[reachedActionSteps.length - 1];
  const activeLineOverride = activeActionStep?.text != null ? typedText(activeActionStep, elapsed) : undefined;

  const evidenceLeadUnit = evidencePrimaryUnit ?? evidenceSecondaryUnit;
  const evidenceOpacity = fadeProgress(evidenceLeadUnit, elapsed);

  const hasReasoning = sentenceUnits.length > 0 || turnUnits.length > 0 || decisionUnit || actionStepUnits.length > 0;

  const dispatchedField = getDispatchedField(episode);

  // Action Taken spans several real units (one per progress_log step) --
  // "live" for the whole card means from the first step's start until the
  // LAST real step's own text finishes typing, same "own content, own
  // pace" rule every other card already follows.
  const actionCardLive = actionStepUnits.length > 0
    && elapsed >= actionStepUnits[0].start
    && elapsed < actionStepUnits[actionStepUnits.length - 1].end;

  return (
    <div className="space-y-4">
      {/* Header: the one fixed exception -- always full, instant opacity.
          NOT part of the sticky block below -- only the seekbar + track
          need to stay pinned while scrolling through a long episode, the
          header can scroll away normally. */}
      <HeaderStrip episode={episode} canPromote={false} onPromote={() => {}} actions={actions} />

      {/* Seekbar + execution-phase track, frozen together at the top of the
          viewport while scrolling a long episode -- both are real
          progress indicators (pure functions of the same `elapsed`), so
          losing sight of either one while reading the content below
          defeats the point of an always-visible progress readout. Grouped
          into one sticky block (order matters: they must be adjacent, no
          header in between, for `sticky` to pin them as a single unit). */}
      <div className="sticky top-0 z-30 bg-surface-container pt-2 pb-1 -mx-4 px-4 space-y-4 shadow-[0_8px_12px_-8px_rgba(0,0,0,0.5)]">
        <ReplaySeekbar engine={engine} milestones={milestones} />
        <ExecutionPhaseTrack
          episode={episode}
          elapsed={elapsed}
          totalDuration={totalDuration}
          actionStepUnits={actionStepUnits}
          durabilityUnit={durabilityUnit}
          playing={engine.playing}
        />
      </div>

      {/* Same 12-col left/right placement as CaseFile's static Snapshot
          layout, locked -- every section below is always mounted here,
          nothing conditionally added/removed as the replay progresses. */}
      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 lg:col-span-7 space-y-4">
          <ServiceContext episode={episode} opacity={fadeProgress(contextUnit, elapsed)} active={isLive(contextUnit, elapsed)} />

          {hasReasoning && (
            <div className="space-y-6">
              <h2 className="font-label-caps text-xs text-on-surface-variant flex items-center gap-2">
                <span className="material-symbols-outlined text-sm">psychology</span> REASONING
              </h2>
              <div className="relative pl-6 space-y-4">
                <div className="absolute left-[7px] top-0 bottom-0 w-px bg-outline-variant" />

                {sentenceUnits.map((u) => (
                  <div key={u.id} className="relative" style={{ opacity: fadeProgress(u, elapsed) }}>
                    <div className="absolute -left-[23px] top-1.5 w-2 h-2 rounded-full bg-primary border-2 border-surface-container" />
                    <div className={`bg-surface-container-high border border-outline-variant p-5 ${isLive(u, elapsed) ? "content-live-glow" : ""}`}>
                      <span className="font-label-caps text-[10px] text-primary uppercase">{u.label}</span>
                      <p className="font-data-mono text-sm leading-relaxed mt-2.5">
                        <HighlightedSentence text={typedText(u, elapsed)} />
                      </p>
                    </div>
                  </div>
                ))}

                {turnUnits.map((u) => (
                  <div key={u.id} className="relative" style={{ opacity: fadeProgress(u, elapsed) }}>
                    <div className="absolute -left-[23px] top-1.5 w-2 h-2 rounded-full bg-primary border-2 border-surface-container" />
                    <div className={`bg-surface-container-high border border-outline-variant p-4 ${isLive(u, elapsed) ? "content-live-glow" : ""}`}>
                      <div className="font-data-mono text-xs text-on-surface-variant">
                        <span className="text-primary">[Turn {u.data.turn}]</span> {u.data.action}
                      </div>
                      {u.text != null && (
                        <p className="font-data-mono text-xs text-on-surface-variant mt-2 break-all">
                          {typedText(u, elapsed)}
                        </p>
                      )}
                    </div>
                  </div>
                ))}

                {decisionUnit && (
                  <div className="relative" style={{ opacity: fadeProgress(decisionUnit, elapsed) }}>
                    <div className="absolute -left-[23px] top-1.5 w-2 h-2 rounded-full bg-primary border-2 border-surface-container" />
                    <div className={`bg-surface-container-high border border-outline-variant p-5 ${isLive(decisionUnit, elapsed) ? "content-live-glow" : ""}`}>
                      <span className="font-label-caps text-[10px] text-primary uppercase">DECISION</span>
                      <p className="font-data-mono text-sm leading-relaxed mt-2.5">
                        <HighlightedSentence text={typedText(decisionUnit, elapsed)} />
                      </p>
                    </div>
                  </div>
                )}

                {actionStepUnits.length > 0 && (
                  <div className="relative" style={{ opacity: fadeProgress(actionStepUnits[0], elapsed) }}>
                    <div className="absolute -left-[23px] top-1.5 w-2 h-2 rounded-full bg-[#238636] border-2 border-surface-container" />
                    <div className={`bg-surface-container-high border border-outline-variant p-4 ${actionCardLive ? "content-live-glow" : ""}`}>
                      <span className="font-label-caps text-[10px] text-[#238636] uppercase">Action taken</span>
                      <ActionProgress episode={episode} revealCount={actionRevealCount} activeLineOverride={activeLineOverride} />
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          <TrustContext
            entry={trustEntry}
            episode={episode}
            bodyText={trustUnit?.text != null ? typedText(trustUnit, elapsed) : undefined}
            opacity={fadeProgress(trustUnit, elapsed)}
            active={isLive(trustUnit, elapsed)}
          />
        </div>

        <div className="col-span-12 lg:col-span-5 space-y-4">
          <EvidenceGrid
            toolOutput={episode.tool_output}
            predictedClass={episode.predicted_class}
            dispatchedField={dispatchedField}
            dispatchedOpacity={fadeProgress(actionStepUnits[0], elapsed)}
            opacity={evidenceOpacity}
            primaryOpacity={fadeProgress(evidencePrimaryUnit, elapsed)}
            secondaryOpacity={fadeProgress(evidenceSecondaryUnit, elapsed)}
            active={isLive(evidencePrimaryUnit, elapsed) || isLive(evidenceSecondaryUnit, elapsed)}
          />

          <GateSubstitution
            substitution={episode.gate_substitution}
            reasonText={gateUnit?.text != null ? typedText(gateUnit, elapsed) : undefined}
            opacity={fadeProgress(gateUnit, elapsed)}
            active={isLive(gateUnit, elapsed)}
          />

          <DurabilityVerdict
            verdict={episode.snapshot_durability_verdict ?? episode.scores_durability_verdict}
            elapsedS={episode.durability_elapsed_s}
            opacity={fadeProgress(durabilityUnit, elapsed)}
            active={isLive(durabilityUnit, elapsed)}
          />

          <SecurityCage />
        </div>
      </div>
    </div>
  );
}
