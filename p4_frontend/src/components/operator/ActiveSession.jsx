import { useEffect, useState } from "react";

// Real target mapping (p2_readonly_loop/injector.py's FAULT_CONFIG), not
// guessed -- only classes whose target maps to something a visitor
// actually browses on the storefront get a specific hint. disk-full's
// target (queue-master) is a backend order-processing consumer with no
// direct customer-facing page, so it deliberately falls through to the
// generic message rather than pointing at a page that won't show anything.
const LANDED_HINTS = {
  "crash-loop": "Try opening the Cart page on the storefront -- it may briefly fail to load while the pod restarts.",
  oom: "Try browsing the product catalogue/homepage -- it may briefly fail to load while the pod restarts.",
};

// M:SS, no cap -- neither the injecting nor resolving phase has a known
// duration bound worth showing a progress bar against (resolving
// especially: it may be genuinely diagnosing, or it may still be padding
// out the hidden settle wait -- see wardence_frontend.md's "Two-Phase
// Trigger Flow" section for why those two cases are deliberately never
// distinguished to the user). This just counts up plainly so it's
// visibly alive, not frozen.
function formatMinSec(ms) {
  const totalSeconds = Math.floor(ms / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function useElapsed(startedAt) {
  const [elapsedMs, setElapsedMs] = useState(0);

  // Reset during render when startedAt changes (a new phase began), rather
  // than synchronously in the effect body -- see TriggerBudget.jsx for the
  // same pattern and reasoning.
  const [syncedStartedAt, setSyncedStartedAt] = useState(startedAt);
  if (startedAt !== syncedStartedAt) {
    setSyncedStartedAt(startedAt);
    setElapsedMs(0);
  }

  useEffect(() => {
    if (!startedAt) return;
    const id = setInterval(() => setElapsedMs(Date.now() - startedAt), 250);
    return () => clearInterval(id);
  }, [startedAt]);

  return elapsedMs;
}

// Two-phase trigger flow (2026-07-24, see wardence_frontend.md): replaces
// the old single-call model's fake, elapsed-time-inferred phase timeline
// with real, distinct states driven directly by which real backend call
// is in flight. "resolving" deliberately covers BOTH the hidden
// settle-wait pad and the real diagnosis call with one indistinguishable
// label -- that's the whole point of the design, not an oversight.
export default function ActiveSession({
  injectingClass,
  injectStartedAt, // timestamp, set once by the parent when the inject call fires
  injectedEpisode, // { faultClass, episodeId, t0 } once landed, awaiting user's Diagnose & Fix click
  resolvingClass,
  resolveStartedAt, // timestamp, set once by the parent when the resolve call fires
  lastResult,
  onViewReplay,
}) {
  // Both startedAt values must come from the PARENT as stable state (set
  // once when the call fires, not recomputed here) -- computing Date.now()
  // inline on every render would reset useElapsed's internal clock every
  // render instead of once per phase. Same pattern the old single-phase
  // version already used for this exact reason.
  const injectElapsedMs = useElapsed(injectingClass ? injectStartedAt : null);
  const resolveElapsedMs = useElapsed(resolvingClass ? resolveStartedAt : null);

  const phase = injectingClass
    ? "injecting"
    : resolvingClass
    ? "resolving"
    : injectedEpisode
    ? "awaiting-fix"
    : lastResult
    ? "done"
    : "idle";

  return (
    <div className="bg-surface-container-low border border-outline min-h-[320px] flex flex-col">
      <div className="p-4 border-b border-outline bg-surface-container-high flex items-center justify-between">
        <h2 className="font-label-caps text-[11px]">ACTIVE SESSION</h2>
        {(injectedEpisode?.episodeId || lastResult?.episode_id) && (
          <span className="font-data-mono text-[10px] text-primary">
            ID: {(injectedEpisode?.episodeId ?? lastResult?.episode_id).slice(0, 8)}
          </span>
        )}
      </div>

      <div className="p-4 flex-1 font-data-mono text-xs space-y-4">
        {phase === "idle" && (
          <p className="text-on-surface-variant opacity-60">No active session. Trigger a fault to see it here.</p>
        )}

        {phase === "injecting" && (
          <div className="flex items-center gap-2 text-primary">
            <span className="material-symbols-outlined text-xs animate-spin">refresh</span>
            <span>[RUNNING] Injecting fault: {injectingClass}</span>
            <span className="ml-auto">{formatMinSec(injectElapsedMs)}</span>
          </div>
        )}

        {phase === "awaiting-fix" && (
          <>
            <div className="flex items-center gap-2 text-[#238636]">
              <span className="material-symbols-outlined text-xs">check_circle</span>
              <span>[LANDED] Fault injected: {injectedEpisode.faultClass}</span>
            </div>
            {LANDED_HINTS[injectedEpisode.faultClass] && (
              <p className="text-on-surface-variant opacity-80">{LANDED_HINTS[injectedEpisode.faultClass]}</p>
            )}
            <p className="text-on-surface-variant opacity-80">
              Confirm the fault is live, then click DIAGNOSE &amp; FIX on the matching card to start the
              real fix.
            </p>
          </>
        )}

        {phase === "resolving" && (
          <div className="flex items-center gap-2 text-primary">
            <span className="material-symbols-outlined text-xs animate-spin">refresh</span>
            <span>[RUNNING] Diagnosing &amp; fixing: {resolvingClass}</span>
            <span className="ml-auto">{formatMinSec(resolveElapsedMs)}</span>
          </div>
        )}

        {phase === "done" && (
          <div className="space-y-2">
            <div className={lastResult.correct ? "text-[#238636]" : "text-error"}>
              [DONE] Predicted: {lastResult.predicted_class ?? "n/a"} — {lastResult.correct ? "correct" : "incorrect"}
            </div>
            {lastResult.target && (
              <div className="text-on-surface-variant">Target: {lastResult.target}</div>
            )}
            {lastResult.confidence != null && (
              <div className="text-on-surface-variant">Confidence: {lastResult.confidence.toFixed(2)}</div>
            )}
            {lastResult.action_taken && (
              <div className="text-on-surface-variant">
                Action: {lastResult.action_taken} ({lastResult.durability_verdict ?? "pending"})
              </div>
            )}
            {lastResult.totalElapsedMs != null && (
              <div className="text-on-surface-variant">Total elapsed: {formatMinSec(lastResult.totalElapsedMs)}</div>
            )}
            {lastResult.scorer_error && (
              <div className="text-error">Scoring failed: {lastResult.scorer_error}</div>
            )}
            {lastResult.episode_id && (
              <button
                onClick={() => onViewReplay(lastResult.episode_id)}
                className="mt-3 text-primary hover:underline font-label-caps text-[11px]"
              >
                VIEW FULL REPLAY →
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
