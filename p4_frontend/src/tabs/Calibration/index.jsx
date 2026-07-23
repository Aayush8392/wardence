import { useEffect, useMemo, useState } from "react";
import { fetchEpisodes } from "../../api/r2";
import { useNavHistory } from "../../context/NavHistoryContext";
import { computeCalibrationStats } from "../../utils/calibration";
import ScatterPlot from "../../components/calibration/ScatterPlot";
import EpisodeDetail from "../../components/calibration/EpisodeDetail";
import CalibrationMetrics from "../../components/calibration/CalibrationMetrics";

export default function Calibration() {
  const [episodes, setEpisodes] = useState(null);
  const [error, setError] = useState(null);
  const [filterClass, setFilterClass] = useState("all");
  const [selected, setSelected] = useState(null);
  const { navigateTo } = useNavHistory();

  useEffect(() => {
    let cancelled = false;
    fetchEpisodes()
      .then((data) => { if (!cancelled) setEpisodes(data); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  const classes = useMemo(() => {
    if (!episodes) return [];
    return [...new Set(episodes.map((e) => e.fault_class))].sort();
  }, [episodes]);

  const visible = useMemo(() => {
    if (!episodes) return [];
    return episodes.filter(
      (e) => e.score_confidence != null && (filterClass === "all" || e.fault_class === filterClass)
    );
  }, [episodes, filterClass]);

  const stats = useMemo(() => computeCalibrationStats(visible), [visible]);

  if (error) return <p className="text-error">Failed to load episodes: {error}</p>;
  if (!episodes) return <p className="text-on-surface-variant">Loading…</p>;

  return (
    <div className="max-w-[1600px] mx-auto">
      <div className="flex justify-between items-end mb-8 flex-wrap gap-4">
        <div>
          <h1 className="font-display-lg text-3xl">Confidence Calibration</h1>
          <p className="text-on-surface-variant text-sm mt-1 max-w-lg">
            Self-reported confidence vs. whether the diagnosis was actually correct. Miscalibration — high
            confidence paired with a wrong answer — is the thing worth catching.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <span className="font-label-caps text-[11px] text-on-surface-variant uppercase">Fault Class Filter:</span>
          <select
            value={filterClass}
            onChange={(e) => { setFilterClass(e.target.value); setSelected(null); }}
            className="bg-surface-container-low border border-outline-variant px-3 py-2 text-sm font-data-mono"
          >
            <option value="all">All Classes</option>
            {classes.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="grid grid-cols-12 gap-6">
        <ScatterPlot episodes={visible} selectedId={selected?.episode_id} onSelect={setSelected} />

        <div className="col-span-12 lg:col-span-4 flex flex-col gap-6">
          <EpisodeDetail
            episode={selected}
            onViewReplay={() => selected && navigateTo(`/replay/${selected.episode_id}`, null, "Calibration")}
          />
          <CalibrationMetrics stats={stats} />
        </div>
      </div>
    </div>
  );
}
