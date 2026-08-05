import { useState } from "react";
import ReasoningSequence from "./ReasoningSequence";
import ExecutionPhaseTrack from "./ExecutionPhaseTrack";
import EvidenceGrid from "./EvidenceGrid";
import DurabilityVerdict from "./DurabilityVerdict";
import GateSubstitution from "./GateSubstitution";
import TrustContext from "./TrustContext";
import ReplayPlayer from "./ReplayPlayer";
import SecurityCage from "../shared/SecurityCage";

// Real, free context (namespace/target/t0) that already exists on every
// episode but was never surfaced anywhere in the case file before.
// opacity optional (default 1, Snapshot's own call site is unaffected) --
// Replay passes a real scrub-derived value on an always-mounted element.
export function ServiceContext({ episode, opacity = 1 }) {
  return (
    <div className="border border-outline-variant bg-surface-container-low p-4 relative" style={{ opacity }}>
      <div className="absolute top-0 left-3 -translate-y-1/2 px-2 py-0.5 bg-surface-variant font-label-caps text-[9px] text-on-surface-variant">
        SERVICE_CONTEXT
      </div>
      <div className="grid grid-cols-2 gap-3 font-data-mono text-xs">
        <div>
          <div className="font-label-caps text-[9px] text-on-surface-variant mb-0.5">NAMESPACE</div>
          <div className="text-on-surface">{episode.namespace}</div>
        </div>
        <div>
          <div className="font-label-caps text-[9px] text-on-surface-variant mb-0.5">TARGET</div>
          <div className="text-on-surface">{episode.target}</div>
        </div>
        <div className="col-span-2">
          <div className="font-label-caps text-[9px] text-on-surface-variant mb-0.5">T0_TIMESTAMP</div>
          <div className="text-on-surface">{episode.t0}</div>
        </div>
      </div>
    </div>
  );
}

export function HeaderStrip({ episode, canPromote, onPromote }) {
  return (
    <div className="border border-outline-variant bg-surface-container-low/80 p-4 flex flex-wrap gap-4 items-center justify-between">
      <div>
        <div className="flex items-center gap-3">
          <h1 className="font-headline-md text-xl">{episode.episode_id.slice(0, 8)}</h1>
          <span className={`px-2 py-0.5 font-label-caps text-[9px] ${episode.correct ? "bg-correct-green/20 text-correct-green" : "bg-error-red/20 text-error-red"}`}>
            {episode.correct ? "CORRECT" : "WRONG"}
          </span>
        </div>
        <div className="flex gap-4 mt-1 font-data-mono text-xs text-on-surface-variant">
          <span>FAULT: <span className="text-primary">{episode.fault_class}</span></span>
          <span>TARGET: <span className="text-on-surface">{episode.target}</span></span>
        </div>
      </div>
      <div className="flex gap-6">
        <div className="flex flex-col items-end">
          <span className="font-label-caps text-[10px] text-outline">CONFIDENCE</span>
          <span className="font-data-mono text-sm text-on-surface">{episode.score_confidence?.toFixed(2) ?? "n/a"}</span>
        </div>
        <div className="w-px h-8 bg-outline-variant/30" />
        <div className="flex flex-col items-end">
          <span className="font-label-caps text-[10px] text-outline">PROVIDER</span>
          <span className="font-data-mono text-sm text-on-surface">
            {episode.provider ? `${episode.model ?? episode.provider} / ${episode.provider}` : "stub (rule-based)"}
          </span>
        </div>
      </div>
      {canPromote && (
        <div className="w-full bg-primary/10 border border-primary/30 px-4 py-2">
          <p className="text-primary text-sm">
            Correctly diagnosed while report-only —{" "}
            <button onClick={onPromote} className="font-label-caps text-xs hover:underline">
              PROMOTE CLASS →
            </button>
          </p>
        </div>
      )}
    </div>
  );
}

// Real, honest note on WHY this button always looks the same regardless of
// how many times an episode has already been watched: replaying just re-runs
// the same real, deterministic presentation timeline (replaySchedule.js) --
// there's nothing new to discover, it's the same episode data walked through
// again, on demand, any time.
export default function CaseFile({ episode, trustEntry, canPromote, onPromote }) {
  const [replaying, setReplaying] = useState(false);

  return (
    <div className="border border-outline-variant bg-surface-container p-4 space-y-4">
      <div className="flex items-center justify-between">
        <HeaderStrip episode={episode} canPromote={canPromote} onPromote={onPromote} />
      </div>

      <div className="flex justify-end">
        {replaying ? (
          <button
            onClick={() => setReplaying(false)}
            className="px-3 py-1.5 font-label-caps text-[10px] border border-outline-variant text-on-surface-variant hover:text-primary hover:border-primary"
          >
            EXIT REPLAY
          </button>
        ) : (
          <button
            onClick={() => setReplaying(true)}
            className="px-3 py-1.5 font-label-caps text-[10px] border border-primary text-primary hover:bg-primary/10 flex items-center gap-1.5"
          >
            <span className="material-symbols-outlined text-sm">play_circle</span>
            VIEW REPLAY
          </button>
        )}
      </div>

      {replaying ? (
        <ReplayPlayer key={episode.episode_id} episode={episode} trustEntry={trustEntry} />
      ) : (
        <div className="grid grid-cols-12 gap-4">
          <div className="col-span-12 lg:col-span-7 space-y-4">
            <ServiceContext episode={episode} />
            <ReasoningSequence episode={episode} />
            <TrustContext entry={trustEntry} />
          </div>
          <div className="col-span-12 lg:col-span-5 space-y-4">
            <EvidenceGrid toolOutput={episode.tool_output} predictedClass={episode.predicted_class} />
            <ExecutionPhaseTrack episode={episode} />
            <GateSubstitution substitution={episode.gate_substitution} />
            <DurabilityVerdict
              verdict={episode.snapshot_durability_verdict ?? episode.scores_durability_verdict}
              elapsedS={episode.durability_elapsed_s}
            />
            <SecurityCage />
          </div>
        </div>
      )}
    </div>
  );
}
