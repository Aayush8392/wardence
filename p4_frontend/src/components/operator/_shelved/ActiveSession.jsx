import { CLASS_LABELS } from "../../constants/faultClasses";

const STEPS = ["injecting", "holding", "awaiting_fix", "resolving", "resolved"];

const STATE_COPY = {
  injecting: "Injecting…",
  holding: "Fault live — check storefront tab",
  awaiting_fix: "Confirmed — use topology node to fix",
  resolving: "Diagnosing & applying fix…",
  resolved: "Complete",
  failed: "Failed — see server log",
};

// Compact single-row status card -- lives in the top status strip now,
// not a tall sidebar. The real action buttons (inject, stop-hold,
// diagnose & fix) live on the topology node itself (matches Stitch's own
// disk-full/queue-master example: the action happens where the fault was
// injected). `live` is polled once, by the parent, shared with
// TopologyMap so the two never duplicate requests for the same episode.
export default function ActiveSession({ episodeId, faultClass, live, onViewReplay }) {
  // No placeholder card when nothing's active -- every bead on the
  // topology ring already shows its own live status inline, so an empty
  // "No active episode" card here was pure redundancy.
  if (!episodeId) return null;

  const state = live?.episode_state ?? "injecting";
  const stepIndex = STEPS.indexOf(state);

  return (
    <div className="glass-module hud-bracket border border-outline-variant/30 p-3 flex items-center gap-4 flex-wrap">
      <div className="flex items-center gap-2 shrink-0">
        <span className="material-symbols-outlined text-primary text-base">route</span>
        <span className="font-data-mono text-sm">{CLASS_LABELS[faultClass] ?? faultClass}</span>
      </div>

      <div className="flex items-center gap-1 shrink-0">
        {STEPS.map((s, i) => (
          <div
            key={s}
            className={`w-2.5 h-2.5 rounded-full ${
              i < stepIndex ? "bg-[#238636]" : i === stepIndex ? "bg-primary animate-pulse" : "bg-outline-variant/30"
            }`}
            title={s.replace("_", " ").toUpperCase()}
          />
        ))}
      </div>

      <p className="font-body-sm text-xs text-on-surface-variant flex-1 min-w-[140px]">
        {STATE_COPY[state] ?? state}
        {live?.elapsed_in_state_s != null && ` (${live.elapsed_in_state_s}s)`}
      </p>

      {state === "resolved" && live.republished_at && (
        <button
          onClick={() => onViewReplay?.(episodeId)}
          className="px-3 py-1.5 border border-primary text-primary font-label-caps text-[10px] hover:bg-primary/10 shrink-0"
        >
          VIEW FULL REPLAY →
        </button>
      )}
      {state === "resolved" && !live.republished_at && (
        <span className="font-label-caps text-[10px] text-on-surface-variant shrink-0">Publishing…</span>
      )}
    </div>
  );
}
