import { useEffect, useState } from "react";
import { fetchModelScorecard, fetchEpisodes, fetchComparisonEpisodes } from "../../api/r2";
import ModelCardTable from "../../components/scorecard/ModelCardTable";
import ConfusionMatrixHeatmap from "../../components/scorecard/ConfusionMatrixHeatmap";
import FallbackFunnel from "../../components/scorecard/FallbackFunnel";
import ActionDurabilityChart from "../../components/scorecard/ActionDurabilityChart";
import EfficiencyFrontier from "../../components/scorecard/EfficiencyFrontier";
import CalibrationSection from "../../components/scorecard/CalibrationSection";

// Real rebuild of the old per-episode-scatter Calibration page into the
// Model Scorecard (Kimi review 27) -- per-MODEL comparison, not just
// per-episode confidence. Route/component name kept as "Calibration"
// (App.jsx's /calibration route, this folder's own name) to avoid an
// unrelated routing churn -- only the nav label and page content
// actually changed. Synthesized from 3 real Stitch mockups
// (stitch_wardence_casefile_incident_detail_5/6/7.zip, all really
// titled "Model Scorecard" internally) with every fabricated element
// found during review corrected or dropped before building -- see the
// session notes for the full list (fake fault-class names, fake dollar
// costs, a fake live "Calibration Drift Detected" banner, an undefined
// "Action Bias" scalar, a redundant per-class panel already covered by
// the confusion matrix). Explicitly NOT built: a radar/spider chart
// (Kimi review 27 argues against it directly, normalization crimes
// across incompatible axes).
export default function Calibration() {
  const [scorecard, setScorecard] = useState(null);
  const [episodes, setEpisodes] = useState(null);
  const [comparisonEpisodes, setComparisonEpisodes] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchModelScorecard(), fetchEpisodes(), fetchComparisonEpisodes()])
      .then(([sc, eps, cmpEps]) => {
        if (cancelled) return;
        setScorecard(sc);
        setEpisodes(eps);
        setComparisonEpisodes(cmpEps);
      })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  if (error) return <p className="text-error-red">Failed to load model scorecard: {error}</p>;
  if (!scorecard || !episodes || !comparisonEpisodes) return <p className="text-on-surface-variant">Loading…</p>;

  return (
    <div className="max-w-[1600px] mx-auto">
      <div className="mb-8">
        <h1 className="font-display-lg text-3xl">Model Scorecard</h1>
        <p className="text-on-surface-variant text-sm mt-1 max-w-2xl">
          Real per-model comparison across the full provider fallback chain — accuracy, efficiency, calibration,
          and action durability. Every number here is measured, not modeled.
        </p>
      </div>

      <div className="flex flex-col gap-6">
        <ModelCardTable scorecard={scorecard} />

        <ConfusionMatrixHeatmap confusionMatrix={scorecard.confusion_matrix} />

        {/* items-stretch (grid default) -- the right column's
            EfficiencyFrontier fills 100% height via its own h-full, so
            it naturally matches the left column's real stacked height
            (Funnel + Action/Durability) without any measurement. */}
        <div className="grid grid-cols-12 gap-6">
          <div className="col-span-12 lg:col-span-5 flex flex-col gap-6">
            <FallbackFunnel tierAccuracy={scorecard.tier_accuracy} />
            <ActionDurabilityChart scorecard={scorecard} />
          </div>
          <div className="col-span-12 lg:col-span-7">
            <EfficiencyFrontier scorecard={scorecard} />
          </div>
        </div>

        <CalibrationSection scorecard={scorecard} episodes={episodes} comparisonEpisodes={comparisonEpisodes} />
      </div>
    </div>
  );
}
