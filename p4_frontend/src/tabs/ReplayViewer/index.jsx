import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchEpisodes } from "../../api/r2";
import { fetchLiveTrust } from "../../api/operator";
import { useNavHistory } from "../../context/NavHistoryContext";
import { useAuth } from "../../context/AuthContext";
import EpisodeList from "../../components/replay/EpisodeList";
import ReasoningStream from "../../components/replay/ReasoningStream";
import ToolOutputSnapshot from "../../components/replay/ToolOutputSnapshot";
import DurabilityVerdict from "../../components/replay/DurabilityVerdict";
import SecurityCage from "../../components/shared/SecurityCage";

function EpisodeDetail({ episode, canPromote, onPromote }) {
  return (
    <section className="p-6">
      <div className="flex justify-between items-end mb-8 border-b border-outline-variant pb-4">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <h1 className="font-display-lg text-2xl">{episode.episode_id.slice(0, 8)}</h1>
            <span
              className={`px-3 py-1 font-label-caps text-xs font-bold ${
                episode.correct ? "bg-[#238636] text-white" : "bg-error-container text-white"
              }`}
            >
              {episode.correct ? "CORRECT RESOLUTION" : "INCORRECT DIAGNOSIS"}
            </span>
          </div>
          <div className="flex gap-4 mt-1">
            <span className="font-data-mono text-xs text-on-surface-variant">
              TARGET: <span className="text-on-surface">{episode.target}</span>
            </span>
            <span className="font-data-mono text-xs text-on-surface-variant">
              CLASS: <span className="text-on-surface">{episode.fault_class}</span>
            </span>
            <span className="font-data-mono text-xs text-on-surface-variant">
              PREDICTED: <span className="text-on-surface">{episode.predicted_class ?? "n/a"}</span>
            </span>
          </div>
        </div>
        <div className="flex flex-col items-end">
          <span className="font-label-caps text-[10px] text-on-surface-variant uppercase">Confidence</span>
          <span className="font-data-mono text-2xl text-primary font-bold">
            {episode.score_confidence?.toFixed(2) ?? "n/a"}
          </span>
        </div>
      </div>

      {canPromote && (
        <div className="bg-primary/10 border border-primary/30 px-4 py-2 mb-6">
          <p className="text-primary text-sm">
            Correctly diagnosed while report-only —{" "}
            <button onClick={onPromote} className="font-label-caps text-xs hover:underline">
              PROMOTE CLASS →
            </button>
          </p>
        </div>
      )}

      <div className="grid grid-cols-12 gap-6">
        <div className="col-span-12 lg:col-span-7">
          <ReasoningStream episode={episode} />
        </div>

        <div className="col-span-12 lg:col-span-5 space-y-6">
          <ToolOutputSnapshot toolOutput={episode.tool_output} />
          <DurabilityVerdict
            verdict={episode.snapshot_durability_verdict ?? episode.scores_durability_verdict}
            elapsedS={episode.durability_elapsed_s}
          />
          <SecurityCage />
        </div>
      </div>
    </section>
  );
}

export default function ReplayViewer() {
  const { episodeId } = useParams();
  const { currentContext, navigateTo } = useNavHistory();
  const { token, role } = useAuth();
  const [episodes, setEpisodes] = useState(null);
  const [error, setError] = useState(null);
  const [trustMap, setTrustMap] = useState({});
  const [search, setSearch] = useState("");
  const [manualClass, setManualClass] = useState("all");
  const [contextCleared, setContextCleared] = useState(false);
  const [detailHeight, setDetailHeight] = useState(null);
  const resizeObserverRef = useRef(null);

  // Measure the detail panel's REAL rendered height and cap the episode
  // list to it (with internal scroll) -- plain CSS stretch/h-full doesn't
  // work here since both panels are content-sized (no fixed height
  // anywhere), so the browser just let both grow to fit their own content
  // instead of syncing. This keeps the list always matching the detail
  // panel's actual height, live, as its content changes.
  //
  // A callback ref, not useRef+useLayoutEffect([]) -- this component has
  // early returns for loading/error state ABOVE where this div renders.
  // On first mount `episodes` is still null, so the div with the ref
  // doesn't exist yet; a plain effect with empty deps fires once against
  // that first render, finds nothing to observe, and never runs again
  // once the real content mounts later. A callback ref instead fires
  // correctly every time the node actually attaches, whenever that is.
  const detailCallbackRef = useCallback((node) => {
    if (resizeObserverRef.current) {
      resizeObserverRef.current.disconnect();
      resizeObserverRef.current = null;
    }
    if (node) {
      const observer = new ResizeObserver((entries) => {
        setDetailHeight(entries[0].contentRect.height);
      });
      observer.observe(node);
      resizeObserverRef.current = observer;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchEpisodes()
      .then((data) => { if (!cancelled) setEpisodes(data); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  // Live (not R2-snapshot) trust state -- only needed for the admin-only
  // "Promote Class" link, so only fetched when there's an admin token.
  useEffect(() => {
    if (role !== "admin" || !token) return;
    let cancelled = false;
    fetchLiveTrust(token)
      .then((states) => {
        if (cancelled) return;
        setTrustMap(Object.fromEntries(states.map((s) => [s.fault_class, s.state])));
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [role, token]);

  // Consume a cross-tab jump from Trust Ladder ({type:"faultClass", faultClass}) --
  // the manual dropdown below takes precedence once the user actually picks
  // something, so the two filters don't fight each other.
  const contextClass = !contextCleared && currentContext?.type === "faultClass" ? currentContext.faultClass : null;
  const filterClass = manualClass !== "all" ? manualClass : contextClass;

  const classes = useMemo(() => {
    if (!episodes) return [];
    return [...new Set(episodes.map((e) => e.fault_class))].sort();
  }, [episodes]);

  const visible = useMemo(() => {
    if (!episodes) return [];
    let list = filterClass ? episodes.filter((e) => e.fault_class === filterClass) : episodes;
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      list = list.filter(
        (e) =>
          e.episode_id.toLowerCase().includes(q) ||
          e.fault_class.toLowerCase().includes(q) ||
          e.target.toLowerCase().includes(q)
      );
    }
    return list;
  }, [episodes, filterClass, search]);

  if (error) return <p className="text-error">Failed to load episodes: {error}</p>;
  if (!episodes) return <p className="text-on-surface-variant">Loading…</p>;

  const selected = episodeId ? episodes.find((e) => e.episode_id === episodeId) : null;
  if (episodeId && !selected) return <p className="text-on-surface-variant">Episode {episodeId} not found.</p>;

  return (
    <div className="max-w-[1600px] mx-auto">
      <div className="flex justify-between items-center mb-4 flex-wrap gap-3">
        <h1 className="font-display-lg text-2xl">Replay Viewer</h1>
        <div className="flex items-center gap-3">
          <span className="font-label-caps text-[11px] text-on-surface-variant uppercase">Fault Class:</span>
          <select
            value={filterClass ?? "all"}
            onChange={(e) => setManualClass(e.target.value)}
            className="bg-surface-container-low border border-outline-variant px-3 py-2 text-sm font-data-mono"
          >
            <option value="all">All Classes</option>
            {classes.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="FILTER EPISODES…"
            className="bg-surface-container-low border border-outline-variant px-3 py-2 text-sm font-data-mono w-64 focus:outline-none focus:border-primary"
          />
        </div>
      </div>

      {filterClass && (
        <p className="text-xs text-on-surface-variant mb-4">
          Filtered to fault class: <strong className="text-on-surface">{filterClass}</strong>{" "}
          <button
            onClick={() => { setManualClass("all"); setContextCleared(true); }}
            className="text-primary hover:underline"
          >
            clear
          </button>
        </p>
      )}

      <div className="flex items-start border border-outline-variant bg-surface-container">
        <EpisodeList
          episodes={visible}
          selectedId={selected?.episode_id}
          onSelect={(id) => navigateTo(`/replay/${id}`, null, "Replay Viewer")}
          maxHeight={detailHeight}
        />

        <div ref={detailCallbackRef} className="flex-1">
          {selected ? (
            <EpisodeDetail
              episode={selected}
              canPromote={
                role === "admin" &&
                trustMap[selected.fault_class] === "report_only" &&
                selected.correct &&
                !selected.scores_action_taken
              }
              onPromote={() =>
                navigateTo("/operator", { type: "promoteClass", faultClass: selected.fault_class }, "Replay Viewer")
              }
            />
          ) : (
            <div className="min-h-[420px] flex items-center justify-center text-on-surface-variant text-sm p-6">
              Select an episode to view its full replay.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
