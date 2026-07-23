import { useEffect, useState } from "react";
import { fetchTrustLadder, fetchTrustHistory, fetchEpisodes } from "../../api/r2";
import { useNavHistory } from "../../context/NavHistoryContext";
import Hero from "../../components/landing/Hero";
import TrustMatrix from "../../components/landing/TrustMatrix";
import ReasoningPanel from "../../components/landing/ReasoningPanel";

export default function TrustLadder() {
  const [rows, setRows] = useState(null);
  const [trustHistory, setTrustHistory] = useState(null);
  const [episodes, setEpisodes] = useState(null);
  const [error, setError] = useState(null);
  const { currentContext } = useNavHistory();

  // Consume a cross-tab jump from Operator's "Review Decision" link (pair #3)
  const highlightClass = currentContext?.type === "faultClass" ? currentContext.faultClass : null;

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchTrustLadder(), fetchTrustHistory(), fetchEpisodes()])
      .then(([ladder, history, eps]) => {
        if (cancelled) return;
        setRows(ladder);
        setTrustHistory(history);
        setEpisodes(eps);
      })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  if (error) return <p className="text-error">Failed to load Trust Ladder: {error}</p>;
  if (!rows) return <p className="text-on-surface-variant">Loading…</p>;

  // episodes is already ordered t0 DESC (see publish_to_r2.py), so [0] is
  // the most recently scored episode across every class -- real, honest
  // criterion for the "live tether target" row highlight (not arbitrary).
  const latestEpisode = episodes[0] ?? null;

  return (
    <div className="max-w-[1600px] mx-auto">
      <Hero rows={rows} episodes={episodes} trustHistory={trustHistory} />

      {highlightClass && (
        <p className="text-xs text-on-surface-variant mb-4">
          Reviewing <strong className="text-on-surface">{highlightClass}</strong>. Note: this table is a periodic
          snapshot (published to R2), so a change made moments ago in Operator may not be reflected yet.
        </p>
      )}

      <TrustMatrix
        rows={rows}
        episodes={episodes}
        highlightClass={highlightClass}
        liveTargetClass={latestEpisode?.fault_class ?? null}
      />

      <ReasoningPanel latestEpisode={latestEpisode} />

      {/* Ceiling/ITBench callout -- locked to live here, not a standalone
          Methodology tab (see wardence_frontend.md). */}
      <p className="text-xs text-on-surface-variant opacity-70 max-w-2xl mt-8 mb-8">
        Frontier models solve ~11% of real SRE scenarios (ITBench). Most fault classes here are
        correctly kept report-only, not a failure — that's the thesis: autonomy is earned per
        class, not assumed.
      </p>
    </div>
  );
}
