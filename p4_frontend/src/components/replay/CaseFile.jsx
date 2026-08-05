import { useState } from "react";
import ReasoningSequence from "./ReasoningSequence";
import ExecutionPhaseTrack from "./ExecutionPhaseTrack";
import EvidenceGrid from "./EvidenceGrid";
import DurabilityVerdict from "./DurabilityVerdict";
import ReplayPlayer from "./ReplayPlayer";
import SecurityCage from "../shared/SecurityCage";

function HeaderStrip({ episode, canPromote, onPromote }) {
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

export default function CaseFile({ episode, canPromote, onPromote }) {
  const [mode, setMode] = useState("snapshot");

  return (
    <div className="border border-outline-variant bg-surface-container p-4 space-y-4">
      <div className="flex items-center justify-between">
        <HeaderStrip episode={episode} canPromote={canPromote} onPromote={onPromote} />
      </div>

      <div className="flex justify-end">
        <div className="inline-flex border border-outline-variant">
          <button
            onClick={() => setMode("snapshot")}
            className={`px-3 py-1.5 font-label-caps text-[10px] ${mode === "snapshot" ? "bg-primary text-on-primary" : "text-on-surface-variant hover:text-primary"}`}
          >
            SNAPSHOT
          </button>
          <button
            onClick={() => setMode("replay")}
            className={`px-3 py-1.5 font-label-caps text-[10px] border-l border-outline-variant ${mode === "replay" ? "bg-primary text-on-primary" : "text-on-surface-variant hover:text-primary"}`}
          >
            REPLAY
          </button>
        </div>
      </div>

      {mode === "replay" ? (
        <ReplayPlayer key={episode.episode_id} episode={episode} />
      ) : (
        <div className="grid grid-cols-12 gap-4">
          <div className="col-span-12 lg:col-span-7 space-y-4">
            <ReasoningSequence episode={episode} />
          </div>
          <div className="col-span-12 lg:col-span-5 space-y-4">
            <EvidenceGrid toolOutput={episode.tool_output} />
            <ExecutionPhaseTrack episode={episode} />
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
