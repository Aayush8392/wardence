import { useEffect, useState } from "react";

// Real known constant (matches operator_api.py's SETTLE_SECONDS, which
// itself matches run_episodes.py's own settle wait) -- used to render an
// honest, real-time-elapsed phase timeline. The backend call is a single
// blocking request with no live push channel, so exact phase transitions
// aren't pushed to the client -- this infers "roughly which real phase
// we're in" from real elapsed time against a real known constant, and is
// labeled as elapsed/approximate rather than claiming certainty.
const SETTLE_SECONDS = 35;
const INJECT_PHASE_MS = 2000; // rough real-world time for the inject call itself to land

function useElapsed(startedAt) {
  const [elapsedMs, setElapsedMs] = useState(0);

  // Reset during render when startedAt changes (a new trigger began),
  // rather than synchronously in the effect body -- see TriggerBudget.jsx
  // for the same pattern and reasoning.
  const [syncedStartedAt, setSyncedStartedAt] = useState(startedAt);
  if (startedAt !== syncedStartedAt) {
    setSyncedStartedAt(startedAt);
    // 0, not Date.now()-startedAt -- Date.now() is impure and can't be
    // called during render. startedAt is set the instant the trigger
    // fires, so elapsed is genuinely ~0 here; the effect's interval below
    // takes over within 250ms anyway.
    setElapsedMs(0);
  }

  useEffect(() => {
    if (!startedAt) return;
    const id = setInterval(() => setElapsedMs(Date.now() - startedAt), 250);
    return () => clearInterval(id);
  }, [startedAt]);

  return elapsedMs;
}

export default function ActiveSession({ triggeringClass, triggerStartedAt, lastResult, onViewReplay }) {
  const elapsedMs = useElapsed(triggerStartedAt);

  const inflight = triggeringClass != null;
  const settleElapsed = Math.max(elapsedMs - INJECT_PHASE_MS, 0);
  const settlePct = Math.min((settleElapsed / (SETTLE_SECONDS * 1000)) * 100, 100);
  const phase = !inflight ? null : elapsedMs < INJECT_PHASE_MS ? "inject" : settlePct < 100 ? "settle" : "diagnose";

  return (
    <div className="bg-surface-container-low border border-outline min-h-[320px] flex flex-col">
      <div className="p-4 border-b border-outline bg-surface-container-high flex items-center justify-between">
        <h2 className="font-label-caps text-[11px]">ACTIVE SESSION</h2>
        {lastResult?.episode_id && (
          <span className="font-data-mono text-[10px] text-primary">ID: {lastResult.episode_id.slice(0, 8)}</span>
        )}
      </div>

      <div className="p-4 flex-1 font-data-mono text-xs space-y-4">
        {!inflight && !lastResult && (
          <p className="text-on-surface-variant opacity-60">No active session. Trigger a fault to see it here.</p>
        )}

        {inflight && (
          <>
            <div className="flex items-center gap-2 text-[#238636]">
              <span className="material-symbols-outlined text-xs">check_circle</span>
              <span>[{phase === "inject" ? "RUNNING" : "DONE"}] Injecting fault: {triggeringClass}</span>
            </div>

            <div className="flex flex-col gap-1">
              <div className={`flex items-center gap-2 ${phase === "settle" ? "text-primary" : "text-on-surface-variant opacity-50"}`}>
                <span className="material-symbols-outlined text-xs animate-spin">refresh</span>
                <span>[{phase === "settle" ? "PENDING" : phase === "diagnose" ? "DONE" : "WAITING"}] Settle period (~{SETTLE_SECONDS}s)</span>
              </div>
              {phase === "settle" && (
                <div className="w-full h-1 bg-outline-variant/30">
                  <div className="h-full bg-primary" style={{ width: `${settlePct}%` }} />
                </div>
              )}
            </div>

            <div className={`flex items-center gap-2 ${phase === "diagnose" ? "text-primary" : "text-on-surface-variant opacity-40"}`}>
              <span className={`material-symbols-outlined text-xs ${phase === "diagnose" ? "animate-spin" : ""}`}>
                {phase === "diagnose" ? "refresh" : "hourglass_empty"}
              </span>
              <span>[{phase === "diagnose" ? "PENDING" : "WAITING"}] AI diagnosis + scoring</span>
            </div>
          </>
        )}

        {!inflight && lastResult && (
          <div className="space-y-2">
            <div className={lastResult.correct ? "text-[#238636]" : "text-error"}>
              [DONE] Predicted: {lastResult.predicted_class ?? "n/a"} — {lastResult.correct ? "correct" : "incorrect"}
            </div>
            {lastResult.action_taken && (
              <div className="text-on-surface-variant">
                Action: {lastResult.action_taken} ({lastResult.durability_verdict ?? "pending"})
              </div>
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
