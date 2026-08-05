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

// Visually emphasizes the real numbers already present in the reasoning
// sentence (e.g. "37.7 bytes/s", "300ms", "380MiB") -- highlights the exact
// evidence already there instead of leaving it flat body text, no new/
// invented content.
const NUMBER_RE = /(-?\d[\d,]*\.?\d*\s?(?:ms|MiB|Mi|bytes\/s|%)?)/g;
export function HighlightedSentence({ text }) {
  const parts = text.split(NUMBER_RE);
  return (
    <>
      {parts.map((part, i) =>
        /\d/.test(part) ? (
          <span key={i} className="text-primary font-semibold">{part}</span>
        ) : (
          <span key={i}>{part}</span>
        )
      )}
    </>
  );
}

// Multi-step fix progress -- real, timestamped data when present
// (action_result.progress_log, added 2026-07-23 for disk-full's real
// scale-down/wait/scale-up/wait sequence). Falls back to a single-line
// summary for single-step actions (restart_deployment, patch_memory_limit)
// or episodes recorded before progress_log existed -- never fabricates
// intermediate steps that weren't actually recorded.
// `revealCount`/`activeDetailOverride` are optional -- omitted (Snapshot's
// own unchanged call site), every step shows in full immediately. Replay
// passes a real revealCount (steps beyond it stay reserved-but-invisible,
// never removed -- no layout shift) and a partially-typed slice of the
// CURRENTLY revealing step's own real `detail` text, satisfying the
// "sequence items with their own nested prose typewriter" rule.
export function ActionProgress({ episode, revealCount, activeDetailOverride }) {
  const progressLog = episode.action_result?.progress_log;

  if (progressLog && progressLog.length > 0) {
    const count = revealCount ?? progressLog.length;
    return (
      <div className="mt-4 space-y-2">
        {progressLog.map((step, i) => {
          const style = STEP_STATUS_ICON[step.status] ?? STEP_STATUS_ICON.done;
          const isRevealed = i < count;
          const isActive = i === count - 1;
          const detail = isActive && activeDetailOverride !== undefined ? activeDetailOverride : step.detail;
          return (
            <div key={i} className={`flex items-center gap-3 ${isRevealed ? "" : "invisible"}`}>
              <span className={`material-symbols-outlined text-[16px] ${style.color}`}>{style.icon}</span>
              <span className="font-data-mono text-xs">
                {step.step}: {step.status}
                {detail && <span className="text-on-surface-variant"> ({detail})</span>}
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
    const isRevealed = revealCount == null || revealCount > 0;
    return (
      <p className={`font-data-mono text-xs mt-3 text-on-surface-variant ${isRevealed ? "" : "invisible"}`}>
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
            <div className="bg-surface-container-high border border-outline-variant p-5">
              <span className="font-label-caps text-[10px] text-primary uppercase">{STEP_LABELS[i]}</span>
              <p className="font-data-mono text-sm leading-relaxed mt-2.5">
                <HighlightedSentence text={s} />
              </p>
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
