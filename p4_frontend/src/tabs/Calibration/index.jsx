import { useEffect, useMemo, useState } from "react";
import { fetchEpisodes } from "../../api/r2";
import { useNavHistory } from "../../context/NavHistoryContext";

// Confidence at/above this AND wrong = worth calling out specifically --
// this is the project's own miscalibration thesis made visible, not an
// arbitrary UI threshold.
const HALLUCINATION_CONFIDENCE_THRESHOLD = 0.8;

const PLOT_WIDTH = 640;
const PLOT_HEIGHT = 240;
const PADDING = 30;

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

  if (error) return <p>Failed to load episodes: {error}</p>;
  if (!episodes) return <p>Loading…</p>;

  return (
    <div>
      <h1>Calibration</h1>
      <p style={{ fontSize: 13, opacity: 0.8, maxWidth: 560 }}>
        Self-reported confidence vs. whether the diagnosis was actually correct, per episode.
        Miscalibration — high confidence paired with a wrong answer — is the thing worth catching.
      </p>

      <p style={{ fontSize: 12, opacity: 0.6 }}>
        Early dataset: {visible.length} scored episode{visible.length === 1 ? "" : "s"} with logged confidence.
      </p>

      <label>
        Fault class:{" "}
        <select value={filterClass} onChange={(e) => { setFilterClass(e.target.value); setSelected(null); }}>
          <option value="all">All</option>
          {classes.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </label>

      <ScatterPlot episodes={visible} onSelect={setSelected} />

      <div style={{ display: "flex", gap: 16, fontSize: 12, marginTop: 4 }}>
        <span>● correct</span>
        <span style={{ color: "#e05" }}>● incorrect</span>
      </div>

      {selected && (
        <EpisodeCallout
          episode={selected}
          onViewReplay={() =>
            navigateTo(`/replay/${selected.episode_id}`, null, "Calibration")
          }
        />
      )}
    </div>
  );
}

function ScatterPlot({ episodes, onSelect }) {
  const plotW = PLOT_WIDTH - PADDING * 2;
  const plotH = PLOT_HEIGHT - PADDING * 2;

  return (
    <svg width={PLOT_WIDTH} height={PLOT_HEIGHT} style={{ marginTop: 12, background: "#141414" }}>
      {/* axes */}
      <line x1={PADDING} y1={PADDING} x2={PADDING} y2={PLOT_HEIGHT - PADDING} stroke="#555" />
      <line x1={PADDING} y1={PLOT_HEIGHT - PADDING} x2={PLOT_WIDTH - PADDING} y2={PLOT_HEIGHT - PADDING} stroke="#555" />
      <text x={PADDING} y={PLOT_HEIGHT - 6} fontSize={10} fill="#888">0.0</text>
      <text x={PLOT_WIDTH - PADDING - 20} y={PLOT_HEIGHT - 6} fontSize={10} fill="#888">1.0 confidence</text>
      <text x={4} y={PADDING + 10} fontSize={10} fill="#888">correct</text>
      <text x={4} y={PLOT_HEIGHT - PADDING - 4} fontSize={10} fill="#888">wrong</text>

      {episodes.map((ep) => {
        const x = PADDING + ep.score_confidence * plotW;
        const y = ep.correct ? PADDING + plotH * 0.25 : PADDING + plotH * 0.75;
        // small jitter so identically-scored episodes don't fully overlap
        const jitterY = ((ep.episode_id.charCodeAt(0) % 20) - 10);
        const isHallucination = !ep.correct && ep.score_confidence >= HALLUCINATION_CONFIDENCE_THRESHOLD;

        return (
          <circle
            key={ep.episode_id}
            cx={x}
            cy={y + jitterY}
            r={5}
            fill={ep.correct ? "#4caf50" : "#e05"}
            stroke={isHallucination ? "#fff" : "none"}
            strokeWidth={isHallucination ? 2 : 0}
            style={{ cursor: "pointer" }}
            onClick={() => onSelect(ep)}
          />
        );
      })}
    </svg>
  );
}

function EpisodeCallout({ episode, onViewReplay }) {
  const isHallucination =
    !episode.correct && episode.score_confidence >= HALLUCINATION_CONFIDENCE_THRESHOLD;

  return (
    <div style={{ border: "1px solid #444", padding: 12, marginTop: 12, maxWidth: 480 }}>
      {isHallucination && (
        <p style={{ color: "#e05", fontWeight: 600 }}>
          ⚠ Hallucination warning: high confidence ({episode.score_confidence.toFixed(2)}), wrong diagnosis.
        </p>
      )}
      <p>
        <strong>{episode.fault_class}</strong> on <code>{episode.target}</code> — predicted{" "}
        <strong>{episode.predicted_class}</strong>, confidence {episode.score_confidence.toFixed(2)} —{" "}
        {episode.correct ? "correct" : "incorrect"}
      </p>
      <button onClick={onViewReplay}>View full replay →</button>
    </div>
  );
}
