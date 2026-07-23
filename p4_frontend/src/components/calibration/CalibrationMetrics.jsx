export default function CalibrationMetrics({ stats }) {
  return (
    <div className="bg-surface-container-low border border-outline-variant p-5">
      <span className="font-label-caps text-[11px] text-on-surface-variant block mb-4">CALIBRATION METRICS</span>

      {!stats ? (
        <p className="text-on-surface-variant text-sm">No scored episodes with logged confidence yet.</p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-surface-container-high p-3 border border-outline-variant">
              <span className="font-label-caps text-[10px] text-on-surface-variant block">ECE</span>
              <span className="font-data-mono text-lg text-primary">{stats.ece.toFixed(3)}</span>
            </div>
            <div className="bg-surface-container-high p-3 border border-outline-variant">
              <span className="font-label-caps text-[10px] text-on-surface-variant block">BRIER SCORE</span>
              <span className="font-data-mono text-lg text-on-surface">{stats.brier.toFixed(3)}</span>
            </div>
          </div>

          <div className="mt-4 pt-4 border-t border-outline-variant">
            <div className="flex justify-between text-xs mb-2">
              <span className="text-on-surface-variant">Reliability Score (1 − ECE)</span>
              <span className="text-correct-green">{stats.reliabilityPct.toFixed(0)}%</span>
            </div>
            <div className="w-full h-1 bg-surface-container-highest">
              <div className="h-full bg-correct-green" style={{ width: `${stats.reliabilityPct}%` }} />
            </div>
          </div>

          <p className="text-[10px] text-on-surface-variant mt-3">
            Computed from {stats.n} scored episode{stats.n === 1 ? "" : "s"} with logged confidence — a small,
            early-stage dataset, not a large-sample statistical claim.
          </p>
        </>
      )}

      {/* Honest roadmap line, not a fabricated live drift signal -- see
          wardence_context.md's "Trust decay + re-certification" item.
          Calibration drift detection needs a real wired-in LLM (still a
          stub today) and a much larger episode volume than this project's
          ~150-episode budget provides. */}
      <div className="mt-4 pt-4 border-t border-outline-variant flex items-start gap-2">
        <span className="material-symbols-outlined text-on-surface-variant text-sm">schedule</span>
        <p className="text-[11px] text-on-surface-variant">
          <span className="text-primary font-label-caps">Coming in v2:</span> Calibration Drift Detection —
          tracking whether calibration shifts over time as the underlying model changes.
        </p>
      </div>
    </div>
  );
}
