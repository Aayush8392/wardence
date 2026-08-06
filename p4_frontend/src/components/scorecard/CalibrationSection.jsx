import { useMemo, useState } from "react";
import ScatterPlot from "../calibration/ScatterPlot";
import EpisodeDetail from "../calibration/EpisodeDetail";
import { useNavHistory } from "../../context/NavHistoryContext";
import {
  modelLabel,
  primaryConfidenceSource,
  isLowVarianceFlag,
  visibleModelKeys,
} from "../../utils/modelScorecard";

// Kimi review 27, section 2 ("Demoting calibration: defensible, but
// with guardrails") -- the deviation bar stays compact, but is
// explicitly NOT allowed to throw away the real per-episode cohort
// scatter that already existed on the old Calibration page. Demoted to
// a click-to-expand drill-down instead of deleted, per Kimi's explicit
// instruction. Also implements the confidence-clustering guardrail
// (item 2, 3rd bullet): a model whose σ(confidence) < 0.05 gets a ⚠️
// badge here regardless of how small its deviation looks.
export default function CalibrationSection({ scorecard, episodes }) {
  const [expandedModel, setExpandedModel] = useState(null);
  const [selected, setSelected] = useState(null);
  const { navigateTo } = useNavHistory();

  const rows = useMemo(() => {
    const keys = visibleModelKeys(scorecard.calibration_error);
    return keys
      .map((key) => {
        const source = primaryConfidenceSource(key);
        const calib = scorecard.calibration_error?.[key]?.[source];
        const variance = scorecard.confidence_variance?.[key]?.[source];
        if (!calib || calib.n === 0) return null;
        return { key, source, calib, variance };
      })
      .filter(Boolean)
      .sort((a, b) => (b.calib.deviation ?? 0) - (a.calib.deviation ?? 0));
  }, [scorecard]);

  const maxDeviation = Math.max(...rows.map((r) => r.calib.deviation ?? 0), 0.001);

  const expandedEpisodes = useMemo(() => {
    if (!expandedModel || !episodes) return [];
    return episodes.filter(
      (e) => `${e.provider}:${e.model}` === expandedModel && e.score_confidence != null
    );
  }, [expandedModel, episodes]);

  return (
    <div className="bg-surface-container-low border border-outline-variant p-5">
      <span className="font-label-caps text-[11px] text-on-surface-variant block mb-1">CALIBRATION</span>
      <p className="text-[11px] text-on-surface-variant mb-4">
        |mean(confidence) − real accuracy|, split logprob vs. self-reported (never blended). Most gaps sit near
        zero — that's honest, not a bug: calibration is not this project's current problem.
      </p>

      <div className="space-y-2">
        {rows.map((r) => {
          const flagged = isLowVarianceFlag(r.variance);
          const isExpanded = expandedModel === r.key;
          return (
            <div key={r.key}>
              <button
                onClick={() => { setExpandedModel(isExpanded ? null : r.key); setSelected(null); }}
                className="w-full flex items-center gap-3 hover:bg-surface-container-high/40 px-1 py-1 -mx-1 text-left"
              >
                <span className="font-data-mono text-xs text-on-surface w-40 shrink-0">{modelLabel(r.key)}</span>
                <div className="flex-1 h-4 bg-surface-container-highest relative">
                  <div
                    className={`h-full ${r.calib.deviation > 0.1 ? "bg-error-red" : "bg-primary"}`}
                    style={{ width: `${Math.max((r.calib.deviation / maxDeviation) * 100, 2)}%` }}
                  />
                </div>
                <span className="font-data-mono text-xs text-on-surface-variant w-16 text-right shrink-0">
                  {(r.calib.deviation * 100).toFixed(1)}pp
                </span>
                {flagged && (
                  <span className="font-label-caps text-[9px] px-1.5 py-0.5 bg-warning-amber/20 text-warning-amber border border-warning-amber/40 shrink-0">
                    ⚠ σ={r.variance.stdev.toFixed(3)}
                  </span>
                )}
                <span className="material-symbols-outlined text-on-surface-variant text-sm shrink-0">
                  {isExpanded ? "expand_less" : "open_in_full"}
                </span>
              </button>

              {isExpanded && (
                <div className="mt-3 mb-1 grid grid-cols-12 gap-4 items-start">
                  <ScatterPlot episodes={expandedEpisodes} selectedId={selected?.episode_id} onSelect={setSelected} />
                  <div className="col-span-12 lg:col-span-4">
                    <EpisodeDetail
                      episode={selected}
                      onViewReplay={() => selected && navigateTo(`/replay/${selected.episode_id}`, null, "Model Scorecard")}
                    />
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
