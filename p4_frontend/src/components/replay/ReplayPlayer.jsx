import { useMemo } from "react";
import { buildReplaySchedule, fadeProgress, typedText } from "./replaySchedule";
import useReplayEngine from "../../hooks/useReplayEngine";
import ReplaySeekbar from "./ReplaySeekbar";
import { HeaderStrip, ServiceContext } from "./CaseFile";
import { HighlightedSentence, ActionProgress } from "./ReasoningStream";
import EvidenceGrid from "./EvidenceGrid";
import ExecutionPhaseTrack from "./ExecutionPhaseTrack";
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

// Fixed content-shape rules (locked, not decided per class/episode):
//   instant          -- header only, always full opacity.
//   fade              -- a fact/tile-batch: fades in as a whole.
//   fade-typewriter   -- real prose: container fades in, text types beneath.
// Every section below is ALWAYS mounted in its real Snapshot position --
// only opacity/text change over time, so the layout never shifts, and
// scrubbing backward un-fades/un-types for free (pure functions of elapsed).
export default function ReplayPlayer({ episode, trustEntry }) {
  const { units, totalDuration } = useMemo(() => buildReplaySchedule(episode, trustEntry), [episode, trustEntry]);
  const engine = useReplayEngine(totalDuration);
  const { elapsed } = engine;
  const milestones = useMemo(() => units.map((u) => u.start), [units]);

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

  const reachedActionSteps = actionStepUnits.filter((u) => elapsed >= u.start);
  const actionRevealCount = reachedActionSteps.length;
  const activeActionStep = reachedActionSteps[reachedActionSteps.length - 1];
  const activeDetailOverride = activeActionStep?.text != null ? typedText(activeActionStep, elapsed) : undefined;

  const evidenceLeadUnit = evidencePrimaryUnit ?? evidenceSecondaryUnit;
  const evidenceOpacity = fadeProgress(evidenceLeadUnit, elapsed);

  const hasReasoning = sentenceUnits.length > 0 || turnUnits.length > 0 || decisionUnit || actionStepUnits.length > 0;

  return (
    <div className="space-y-4">
      <ReplaySeekbar engine={engine} milestones={milestones} />

      {/* Header: the one fixed exception -- always full, instant opacity. */}
      <HeaderStrip episode={episode} canPromote={false} onPromote={() => {}} />

      {/* Same 12-col left/right placement as CaseFile's static Snapshot
          layout, locked -- every section below is always mounted here,
          nothing conditionally added/removed as the replay progresses. */}
      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 lg:col-span-7 space-y-4">
          <ServiceContext episode={episode} opacity={fadeProgress(contextUnit, elapsed)} />

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
                    <div className="bg-surface-container-high border border-outline-variant p-5">
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
                    <div className="bg-surface-container-high border border-outline-variant p-4">
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
                    <div className="bg-surface-container-high border border-outline-variant p-5">
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
                    <div className="bg-surface-container-high border border-outline-variant p-4">
                      <span className="font-label-caps text-[10px] text-[#238636] uppercase">Action taken</span>
                      <ActionProgress episode={episode} revealCount={actionRevealCount} activeDetailOverride={activeDetailOverride} />
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          <TrustContext
            entry={trustEntry}
            bodyText={trustUnit?.text != null ? typedText(trustUnit, elapsed) : undefined}
            opacity={fadeProgress(trustUnit, elapsed)}
          />
        </div>

        <div className="col-span-12 lg:col-span-5 space-y-4">
          <EvidenceGrid
            toolOutput={episode.tool_output}
            predictedClass={episode.predicted_class}
            opacity={evidenceOpacity}
            primaryOpacity={fadeProgress(evidencePrimaryUnit, elapsed)}
            secondaryOpacity={fadeProgress(evidenceSecondaryUnit, elapsed)}
          />

          <ExecutionPhaseTrack
            episode={episode}
            revealCount={actionRevealCount}
            opacity={fadeProgress(actionStepUnits[0], elapsed)}
          />

          <GateSubstitution
            substitution={episode.gate_substitution}
            reasonText={gateUnit?.text != null ? typedText(gateUnit, elapsed) : undefined}
            opacity={fadeProgress(gateUnit, elapsed)}
          />

          <DurabilityVerdict
            verdict={episode.snapshot_durability_verdict ?? episode.scores_durability_verdict}
            elapsedS={episode.durability_elapsed_s}
            opacity={fadeProgress(durabilityUnit, elapsed)}
          />

          <SecurityCage />
        </div>
      </div>
    </div>
  );
}
