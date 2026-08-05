import { useEffect, useMemo, useState } from "react";
import ReasoningSequence from "./ReasoningSequence";
import ExecutionPhaseTrack from "./ExecutionPhaseTrack";
import EvidenceGrid from "./EvidenceGrid";
import DurabilityVerdict from "./DurabilityVerdict";

const BEAT_MS = 2600;

// The locked 5-beat "lite story" skeleton (wardence_frontend.md, Replay
// Viewer episode-breakdown design), applied identically to lite AND
// enriched episodes -- same fixed pacing for both, per explicit call to
// keep this simple rather than giving enriched episodes a richer replay
// pace. The "Action Taken" beat is skipped entirely (not shown empty)
// when no action was taken, matching the locked spec.
function buildBeats(episode) {
  const beats = [
    { key: "incident", label: "INCIDENT DECLARED" },
    { key: "evidence", label: "EVIDENCE GATHERED" },
    { key: "diagnosis", label: "DIAGNOSIS REACHED" },
  ];
  if (episode.scores_action_taken) beats.push({ key: "action", label: "ACTION TAKEN" });
  beats.push({ key: "verdict", label: "SYSTEM VERDICT" });
  return beats;
}

function IncidentBeat({ episode }) {
  return (
    <div className="font-data-mono text-sm space-y-1">
      <div>FAULT_CLASS: <span className="text-primary">{episode.fault_class}</span></div>
      <div>TARGET: <span className="text-on-surface">{episode.target}</span></div>
      <div className="text-on-surface-variant text-xs">{episode.t0}</div>
    </div>
  );
}

function DiagnosisBeat({ episode }) {
  return (
    <div className="flex items-center gap-6">
      <div className="flex flex-col items-center">
        <span className="font-data-mono text-3xl text-primary">{episode.score_confidence?.toFixed(2) ?? "n/a"}</span>
        <span className="font-label-caps text-[9px] text-on-surface-variant">CONFIDENCE</span>
      </div>
      <div className="font-data-mono text-sm space-y-1">
        <div>PREDICTED: <span className="text-on-surface">{episode.predicted_class ?? "n/a"}</span></div>
        <div>ACTUAL: <span className="text-on-surface">{episode.fault_class}</span></div>
        <span className={`inline-block mt-1 px-2 py-0.5 font-label-caps text-[9px] ${episode.correct ? "bg-correct-green/20 text-correct-green" : "bg-error-red/20 text-error-red"}`}>
          {episode.correct ? "CORRECT" : "INCORRECT"}
        </span>
      </div>
    </div>
  );
}

function VerdictBeat({ episode }) {
  return (
    <div className="flex items-center gap-3">
      <span className={`material-symbols-outlined text-3xl ${episode.correct ? "text-correct-green" : "text-error-red"}`}>
        {episode.correct ? "check_circle" : "cancel"}
      </span>
      <span className={`font-headline-md text-xl tracking-widest ${episode.correct ? "text-correct-green" : "text-error-red"}`}>
        {episode.correct ? "CORRECT RESOLUTION" : "INCORRECT DIAGNOSIS"}
      </span>
    </div>
  );
}

// Keyed by episode_id from CaseFile (see below) so a fresh episode
// selection remounts this component entirely -- a clean reset of
// beatIndex/playing without needing an effect that syncs state to a
// changing prop (which React's own effect-linting flags as the sign of
// state that should just be derived/remounted instead).
export default function ReplayPlayer({ episode }) {
  const beats = useMemo(() => buildBeats(episode), [episode]);
  const [beatIndex, setBeatIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const isLastBeat = beatIndex >= beats.length - 1;

  useEffect(() => {
    if (!playing || isLastBeat) return undefined;
    const t = setTimeout(() => setBeatIndex((i) => Math.min(i + 1, beats.length - 1)), BEAT_MS);
    return () => clearTimeout(t);
  }, [playing, isLastBeat, beats.length]);

  const activeKey = beats[beatIndex]?.key;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <button
          onClick={() => (isLastBeat ? (setBeatIndex(0), setPlaying(true)) : setPlaying((p) => !p))}
          className="w-8 h-8 flex items-center justify-center border border-primary text-primary hover:bg-primary/10"
        >
          <span className="material-symbols-outlined text-lg">
            {isLastBeat ? "replay" : playing ? "pause" : "play_arrow"}
          </span>
        </button>
        <button
          onClick={() => setBeatIndex((i) => Math.max(0, i - 1))}
          disabled={beatIndex === 0}
          className="w-8 h-8 flex items-center justify-center border border-outline-variant text-on-surface-variant hover:border-primary hover:text-primary disabled:opacity-30"
        >
          <span className="material-symbols-outlined text-lg">skip_previous</span>
        </button>
        <button
          onClick={() => setBeatIndex((i) => Math.min(beats.length - 1, i + 1))}
          disabled={beatIndex === beats.length - 1}
          className="w-8 h-8 flex items-center justify-center border border-outline-variant text-on-surface-variant hover:border-primary hover:text-primary disabled:opacity-30"
        >
          <span className="material-symbols-outlined text-lg">skip_next</span>
        </button>

        <div className="flex-1 flex items-center gap-1">
          {beats.map((b, i) => (
            <div key={b.key} className="flex-1 flex flex-col items-center gap-1">
              <div className={`w-full h-1 ${i <= beatIndex ? "bg-primary" : "bg-outline-variant/30"}`} />
              <span className={`font-label-caps text-[8px] ${i <= beatIndex ? "text-primary" : "text-outline"}`}>{b.label}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="min-h-[220px] border border-outline-variant bg-surface-container-low p-4">
        {activeKey === "incident" && <IncidentBeat episode={episode} />}
        {activeKey === "evidence" && (
          <div className="space-y-4">
            <ReasoningSequence episode={episode} revealCount={999} />
            <EvidenceGrid toolOutput={episode.tool_output} />
          </div>
        )}
        {activeKey === "diagnosis" && <DiagnosisBeat episode={episode} />}
        {activeKey === "action" && (
          <div className="space-y-3">
            <ExecutionPhaseTrack episode={episode} />
            <DurabilityVerdict
              verdict={episode.snapshot_durability_verdict ?? episode.scores_durability_verdict}
              elapsedS={episode.durability_elapsed_s}
            />
          </div>
        )}
        {activeKey === "verdict" && <VerdictBeat episode={episode} />}
      </div>
    </div>
  );
}
