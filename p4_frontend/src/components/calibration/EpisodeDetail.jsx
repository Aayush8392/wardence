export default function EpisodeDetail({ episode, onViewReplay }) {
  if (!episode) {
    return (
      <div className="bg-surface-container-low border border-outline-variant p-5">
        <span className="font-label-caps text-[11px] text-primary">Detail Analysis</span>
        <p className="text-on-surface-variant text-sm mt-4">Click a point on the plot to inspect that episode.</p>
      </div>
    );
  }

  return (
    <div className="bg-surface-container-low border border-outline-variant p-5">
      <div className="flex items-center justify-between mb-6 border-b border-outline-variant pb-3">
        <span className="font-label-caps text-[11px] text-primary">Detail Analysis</span>
        <span className="font-data-mono text-xs text-on-surface-variant">
          ID: {episode.episode_id.slice(0, 8)}
        </span>
      </div>

      <div className="space-y-4">
        <div className="flex justify-between items-center">
          <span className="text-sm text-on-surface-variant">Target Entity</span>
          <span className="font-data-mono text-sm bg-surface-container-high px-2 py-0.5 border border-outline-variant">
            {episode.target}
          </span>
        </div>
        <div className="flex justify-between items-center">
          <span className="text-sm text-on-surface-variant">Fault Type</span>
          <span className="font-data-mono text-sm text-warning-amber">{episode.fault_class}</span>
        </div>
        <div className="flex justify-between items-center">
          <span className="text-sm text-on-surface-variant">System Confidence</span>
          <span className={`font-data-mono text-sm font-bold ${episode.correct ? "text-correct-green" : "text-error-red"}`}>
            {episode.score_confidence?.toFixed(2) ?? "n/a"}
          </span>
        </div>
        <div className="flex justify-between items-center">
          <span className="text-sm text-on-surface-variant">Outcome</span>
          <span className={`font-data-mono text-sm ${episode.correct ? "text-correct-green" : "text-error-red"}`}>
            {episode.correct ? "CORRECT" : "WRONG"}
          </span>
        </div>
        {!episode.correct && (
          <div className="flex justify-between items-center">
            <span className="text-sm text-on-surface-variant">Predicted vs Actual</span>
            <span className="font-data-mono text-sm text-error-red">
              {episode.predicted_class ?? "n/a"} → {episode.fault_class}
            </span>
          </div>
        )}
      </div>

      {episode.reasoning && (
        <div className="mt-8 p-4 bg-error-red/5 border-l-2 border-error-red">
          <p className="font-label-caps text-[11px] text-error-red mb-2">Internal Reasoner Output</p>
          <p className="font-data-mono text-xs text-on-surface-variant leading-relaxed whitespace-pre-wrap">
            {episode.reasoning}
          </p>
        </div>
      )}

      {episode.isComparisonOnly && (
        <div className="mt-4 p-3 bg-warning-amber/5 border-l-2 border-warning-amber">
          <p className="text-[11px] text-on-surface-variant leading-relaxed">
            This model never dispatches — it only ran a comparison-only diagnosis against this episode's real
            evidence, never acted on the cluster. The linked replay below shows the primary chain's real dispatch
            outcome for this episode (a different model), not this diagnosis.
          </p>
        </div>
      )}

      <button
        onClick={onViewReplay}
        className="w-full mt-6 py-3 border border-primary text-primary font-label-caps text-[11px] hover:bg-primary hover:text-on-primary transition-all"
      >
        {episode.isComparisonOnly ? "VIEW REAL DISPATCH REPLAY →" : "VIEW FULL REPLAY →"}
      </button>
    </div>
  );
}
