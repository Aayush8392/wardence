import {
  modelLabel,
  isPrimaryModel,
  primaryConfidenceSource,
  isLowVarianceFlag,
  visibleModelKeys,
} from "../../utils/modelScorecard";

// Kimi review 27's own recommended replacement for a radar/spider chart
// (explicitly rejected -- see build_model_scorecard's docstring): a
// sortable table first, small multiples/heatmaps below it. This is
// that table. "Action Bias" is deliberately NOT a column here -- Kimi's
// own fix was two raw columns (diagnostic accuracy, action durability)
// instead of one fabricated composite delta; durability is real but
// only ever populated for the two real dispatching providers
// (Gemma/Nemotron), so it renders "—" for comparison-only models
// rather than a fake 0%.
export default function ModelCardTable({ scorecard }) {
  const modelKeys = visibleModelKeys(scorecard.overall_accuracy).sort(
    (a, b) => (scorecard.overall_accuracy[b].total_episodes ?? 0) - (scorecard.overall_accuracy[a].total_episodes ?? 0)
  );

  return (
    <div className="bg-surface-container-low border border-outline-variant p-5 overflow-x-auto">
      <span className="font-label-caps text-[11px] text-on-surface-variant block mb-4">
        MODEL_CARD_TABLE
      </span>
      <table className="w-full text-sm min-w-[820px]">
        <thead>
          <tr className="text-left border-b border-outline-variant">
            <th className="font-label-caps text-[10px] text-on-surface-variant pb-2 pr-4">MODEL</th>
            <th className="font-label-caps text-[10px] text-on-surface-variant pb-2 pr-4">TIER</th>
            <th className="font-label-caps text-[10px] text-on-surface-variant pb-2 pr-4">OVERALL ACC</th>
            <th className="font-label-caps text-[10px] text-on-surface-variant pb-2 pr-4">EFFICIENCY</th>
            <th className="font-label-caps text-[10px] text-on-surface-variant pb-2 pr-4">COST / CORRECT</th>
            <th className="font-label-caps text-[10px] text-on-surface-variant pb-2 pr-4">DURABILITY</th>
            <th className="font-label-caps text-[10px] text-on-surface-variant pb-2 pr-4">CALIB. DEVIATION</th>
            <th className="font-label-caps text-[10px] text-on-surface-variant pb-2">FLAGS</th>
          </tr>
        </thead>
        <tbody>
          {modelKeys.map((key) => {
            const acc = scorecard.overall_accuracy[key];
            const eff = scorecard.efficiency?.[key];
            const cost = scorecard.cost_per_correct?.[key];
            const durability = scorecard.durability_rate?.[key];
            const source = primaryConfidenceSource(key);
            const variance = scorecard.confidence_variance?.[key]?.[source];
            const calib = scorecard.calibration_error?.[key]?.[source];
            const overreach = scorecard.overreach?.[key];
            const lowVariance = isLowVarianceFlag(variance);
            const hasOverreach = overreach && overreach.overreach_events > 0;

            return (
              <tr key={key} className="border-b border-outline-variant/50 hover:bg-surface-container-high/50">
                <td className="py-3 pr-4 font-data-mono text-on-surface">{modelLabel(key)}</td>
                <td className="py-3 pr-4">
                  <span
                    className={`font-label-caps text-[10px] px-2 py-0.5 border ${
                      isPrimaryModel(key)
                        ? "border-primary/40 text-primary"
                        : "border-outline-variant text-on-surface-variant"
                    }`}
                  >
                    {isPrimaryModel(key) ? "PRIMARY" : "FALLBACK"}
                  </span>
                </td>
                <td className="py-3 pr-4 font-data-mono text-on-surface">
                  {acc.accuracy != null ? `${(acc.accuracy * 100).toFixed(1)}%` : "—"}
                  <span className="text-on-surface-variant text-xs ml-1">
                    ({acc.correct}/{acc.total_episodes})
                  </span>
                </td>
                <td className="py-3 pr-4 font-data-mono text-on-surface-variant">
                  {eff?.avg_neurons != null
                    ? `${eff.avg_neurons.toFixed(1)} Nrn`
                    : eff?.avg_tokens != null
                    ? `${Math.round(eff.avg_tokens)} tok`
                    : "—"}
                </td>
                <td className="py-3 pr-4 font-data-mono text-on-surface-variant">
                  {cost?.neurons_per_correct != null
                    ? `${cost.neurons_per_correct.toFixed(1)} Nrn`
                    : cost?.tokens_per_correct != null
                    ? `${Math.round(cost.tokens_per_correct)} tok`
                    : "—"}
                </td>
                <td className="py-3 pr-4 font-data-mono text-on-surface-variant">
                  {durability?.durability_rate != null
                    ? `${(durability.durability_rate * 100).toFixed(1)}%`
                    : <span className="text-on-surface-variant/50">— never dispatches</span>}
                </td>
                <td className="py-3 pr-4 font-data-mono text-on-surface-variant">
                  {calib?.deviation != null ? (
                    <>
                      {(calib.deviation * 100).toFixed(1)}pp
                      <span className="text-[10px] text-on-surface-variant/70 ml-1">({source})</span>
                    </>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="py-3">
                  <div className="flex flex-wrap gap-1">
                    {hasOverreach && (
                      <span className="font-label-caps text-[10px] px-2 py-0.5 bg-error-red/20 text-error-red border border-error-red/40">
                        OVERREACH
                      </span>
                    )}
                    {lowVariance && (
                      <span className="font-label-caps text-[10px] px-2 py-0.5 bg-warning-amber/20 text-warning-amber border border-warning-amber/40">
                        CONF. VAR (σ&lt;{(0.05).toFixed(2)})
                      </span>
                    )}
                    {!hasOverreach && !lowVariance && (
                      <span className="font-label-caps text-[10px] px-2 py-0.5 border border-outline-variant text-on-surface-variant">
                        CLEAN
                      </span>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="text-[10px] text-on-surface-variant mt-3">
        Cost/Correct is derived (avg cost ÷ accuracy), not an exact per-episode join — llm_token_usage.json
        aggregates per class/provider, not per episode. "Durability" only applies to Gemma/Nemotron, the two
        providers that actually dispatch real actions — Groq/OpenRouter run comparison-only and never touch the
        cluster.
      </p>
    </div>
  );
}
