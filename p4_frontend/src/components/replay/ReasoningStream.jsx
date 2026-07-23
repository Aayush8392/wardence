const STEP_LABELS = ["OBSERVATION", "HYPOTHESIS", "ACTION TRIGGERED"];

const STEP_STATUS_ICON = {
  done: { icon: "check_circle", color: "text-[#238636]" },
  in_progress: { icon: "sync", color: "text-primary" },
  failed: { icon: "cancel", color: "text-error" },
  timed_out: { icon: "warning", color: "text-error" },
};

function formatStepTime(iso) {
  if (!iso) return null;
  return new Date(iso).toLocaleTimeString("en-GB");
}

// Multi-step fix progress -- real, timestamped data when present
// (action_result.progress_log, added 2026-07-23 for disk-full's real
// scale-down/wait/scale-up/wait sequence). Falls back to a single-line
// summary for single-step actions (restart_deployment, patch_memory_limit)
// or episodes recorded before progress_log existed -- never fabricates
// intermediate steps that weren't actually recorded.
function ActionProgress({ episode }) {
  const progressLog = episode.action_result?.progress_log;

  if (progressLog && progressLog.length > 0) {
    return (
      <div className="mt-4 space-y-2">
        {progressLog.map((step, i) => {
          const style = STEP_STATUS_ICON[step.status] ?? STEP_STATUS_ICON.done;
          return (
            <div key={i} className="flex items-center gap-3">
              <span className={`material-symbols-outlined text-[16px] ${style.color}`}>{style.icon}</span>
              <span className="font-data-mono text-xs">
                {step.step}: {step.status}
                {step.detail && <span className="text-on-surface-variant"> ({step.detail})</span>}
              </span>
              <span className="font-data-mono text-[10px] text-on-surface-variant ml-auto">
                {formatStepTime(step.at)}
              </span>
            </div>
          );
        })}
      </div>
    );
  }

  if (episode.scores_action_taken) {
    return (
      <p className="font-data-mono text-xs mt-3 text-on-surface-variant">
        Action: {episode.scores_action_taken} —{" "}
        <span className={episode.action_applied ? "text-[#238636]" : "text-error"}>
          {episode.action_applied ? "applied" : "not applied"}
        </span>
      </p>
    );
  }

  return null;
}

export default function ReasoningStream({ episode }) {
  const sentences = (episode.reasoning ?? "")
    .split(/(?<=[.!])\s+|\n+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 2);

  const hasAction = Boolean(episode.scores_action_taken);

  return (
    <div className="space-y-6">
      <h2 className="font-label-caps text-xs text-on-surface-variant flex items-center gap-2">
        <span className="material-symbols-outlined text-sm">psychology</span> REASONING
      </h2>

      {sentences.length === 0 && (
        <p className="font-data-mono text-xs text-on-surface-variant opacity-60">
          No reasoning snapshot captured for this episode.
        </p>
      )}

      <div className="relative pl-6 space-y-4">
        <div className="absolute left-[7px] top-0 bottom-0 w-px bg-outline-variant" />

        {sentences.map((s, i) => (
          <div key={i} className="relative">
            <div className="absolute -left-[23px] top-1.5 w-2 h-2 rounded-full bg-primary border-2 border-surface-container" />
            <div className="bg-surface-container-high border border-outline-variant p-4">
              <span className="font-label-caps text-[10px] text-primary uppercase">{STEP_LABELS[i]}</span>
              <p className="font-data-mono text-xs leading-relaxed mt-2">{s}</p>
            </div>
          </div>
        ))}

        {hasAction && (
          <div className="relative">
            <div className="absolute -left-[23px] top-1.5 w-2 h-2 rounded-full bg-[#238636] border-2 border-surface-container" />
            <div className="bg-surface-container-high border border-outline-variant p-4">
              <span className="font-label-caps text-[10px] text-[#238636] uppercase">Action taken</span>
              <ActionProgress episode={episode} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
