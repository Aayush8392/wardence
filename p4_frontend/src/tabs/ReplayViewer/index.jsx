import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { fetchEpisodes, fetchTrustLadder } from "../../api/r2";
import { fetchLiveTrust } from "../../api/operator";
import { useNavHistory } from "../../context/NavHistoryContext";
import { useAuth } from "../../context/AuthContext";
import EpisodeList from "../../components/replay/EpisodeList";
import DotSphere from "../../components/replay/DotSphere";
import CaseFile from "../../components/replay/CaseFile";

// The sphere (2026-08-07 prototype) is now a PERMANENT fixture, not a
// variable-height detail panel -- so both side-by-side columns just use
// this one fixed constant instead of the old ResizeObserver-driven
// height-sync (removed; there's nothing dynamic left to measure once
// both sides have a known, fixed height to begin with).
const SPHERE_HEIGHT = 480;

export default function ReplayViewer() {
  const { episodeId } = useParams();
  const { currentContext, navigateTo } = useNavHistory();
  const navigate = useNavigate();
  const { token, role } = useAuth();
  const [episodes, setEpisodes] = useState(null);
  const [error, setError] = useState(null);
  const [trustMap, setTrustMap] = useState({});
  const [trustLadder, setTrustLadder] = useState(null);
  const [search, setSearch] = useState("");
  const [manualClass, setManualClass] = useState("all");
  const [contextCleared, setContextCleared] = useState(false);
  const [sortOrder, setSortOrder] = useState("desc"); // "desc" (newest first, real current default) | "asc"
  const [dateFrom, setDateFrom] = useState(""); // "YYYY-MM-DD", empty = unbounded
  const [dateTo, setDateTo] = useState("");

  // Stable reference -- EpisodeList's rows are memoized (React.memo) so a
  // click only re-renders the 1-2 rows whose selection state actually
  // changed, not all ~2000+ rows; that memoization is defeated if the
  // onSelect callback passed down is a new function every render.
  // Real deselect support: EpisodeRow now passes `null` when clicking the
  // already-selected row (same convention DotSphere's own click handler
  // already uses) -- this stays a stable reference (still only depends on
  // `navigate`) rather than needing the current selection to decide,
  // which would defeat EpisodeList's row memoization.
  const selectEpisode = useCallback((id) => navigate(id ? `/replay/${id}` : "/replay"), [navigate]);

  useEffect(() => {
    let cancelled = false;
    fetchEpisodes()
      .then((data) => { if (!cancelled) setEpisodes(data); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  // Public R2 snapshot, no auth needed -- same data the Trust Ladder tab
  // reads, used here for CaseFile's "why this outcome" real trust-state
  // context. Best-effort: if it fails, CaseFile just skips that card.
  useEffect(() => {
    let cancelled = false;
    fetchTrustLadder()
      .then((rows) => { if (!cancelled) setTrustLadder(rows); })
      .catch(() => {});
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

  // Real date bounds for the range filter -- episodes.json is published
  // `ORDER BY t0 DESC` (build_episodes(), publish_to_r2.py), so episodes[0]
  // is the real newest and the last entry is the real oldest. Anything
  // outside this range genuinely has no data, so the date inputs' own
  // min/max block it at the OS picker level rather than accepting a
  // selection that could only ever return zero results.
  const { minDate, maxDate } = useMemo(() => {
    if (!episodes || episodes.length === 0) return { minDate: undefined, maxDate: undefined };
    return {
      minDate: episodes[episodes.length - 1].t0.slice(0, 10),
      maxDate: episodes[0].t0.slice(0, 10),
    };
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
    if (dateFrom) list = list.filter((e) => e.t0.slice(0, 10) >= dateFrom);
    if (dateTo) list = list.filter((e) => e.t0.slice(0, 10) <= dateTo);
    // Base data already arrives newest-first (real DB order) -- only pay
    // the cost of a reverse when the user actually asks for ascending,
    // never re-sort for the default/common case.
    if (sortOrder === "asc") list = [...list].reverse();
    return list;
  }, [episodes, filterClass, search, dateFrom, dateTo, sortOrder]);

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
          {/* Real bounded date-range filter -- min/max are the actual
              oldest/newest episode dates in the data, so the browser's own
              date picker blocks any selection that could only ever return
              zero results. */}
          <div className="flex items-center gap-1.5">
            <input
              type="date"
              value={dateFrom}
              min={minDate}
              max={dateTo || maxDate}
              onChange={(e) => setDateFrom(e.target.value)}
              className="bg-surface-container-low border border-outline-variant px-2 py-2 text-sm font-data-mono focus:outline-none focus:border-primary"
              title="From date"
            />
            <span className="text-on-surface-variant text-xs">–</span>
            <input
              type="date"
              value={dateTo}
              min={dateFrom || minDate}
              max={maxDate}
              onChange={(e) => setDateTo(e.target.value)}
              className="bg-surface-container-low border border-outline-variant px-2 py-2 text-sm font-data-mono focus:outline-none focus:border-primary"
              title="To date"
            />
            {(dateFrom || dateTo) && (
              <button
                onClick={() => { setDateFrom(""); setDateTo(""); }}
                className="text-primary text-xs hover:underline"
                title="Clear date range"
              >
                clear
              </button>
            )}
          </div>
          {/* Sort order toggle -- base data is real DB order (newest
              first); flipping to ascending only reverses the already-
              filtered list, never re-sorts. */}
          <button
            onClick={() => setSortOrder((o) => (o === "desc" ? "asc" : "desc"))}
            className="flex items-center gap-1.5 bg-surface-container-low border border-outline-variant px-3 py-2 text-sm font-data-mono hover:border-primary"
            title={sortOrder === "desc" ? "Newest first — click for oldest first" : "Oldest first — click for newest first"}
          >
            <span className="material-symbols-outlined text-base">
              {sortOrder === "desc" ? "arrow_downward" : "arrow_upward"}
            </span>
            {sortOrder === "desc" ? "NEWEST" : "OLDEST"}
          </button>
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
          onSelect={selectEpisode}
          maxHeight={SPHERE_HEIGHT}
        />

        {/* The sphere always plots the FULL episode set (real global topology),
            never the search-narrowed list -- only the fault-class filter (shared
            with the dropdown above) dims/pins it, since "search for episode
            abc123" narrowing the sphere to one dot would defeat its point as
            an overview. */}
        {/* Real fix: min-w-0 -- a flex child defaults to min-width:auto
            (its content's natural width), so the canvas's own intrinsic
            size was preventing this column from actually shrinking when
            EpisodeList's w-1/4 change left it less room on some screens --
            the row overflowed horizontally instead of the sphere resizing
            to fit. min-w-0 lets the flex layout actually shrink it, which
            is what DotSphere's own ResizeObserver then correctly redraws
            the canvas to match. */}
        <div className="flex-1 min-w-0">
          <DotSphere
            episodes={episodes}
            selectedId={selected?.episode_id}
            onSelect={(id) => navigate(id ? `/replay/${id}` : "/replay")}
            filterClass={filterClass}
            onFilterClass={(cls) => { setManualClass(cls ?? "all"); setContextCleared(true); }}
            height={SPHERE_HEIGHT}
          />
        </div>
      </div>

      <div className="mt-4">
        {selected ? (
          <CaseFile
            episode={selected}
            trustEntry={trustLadder?.find((r) => r.fault_class === selected.fault_class) ?? null}
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
          <div className="border border-outline-variant bg-surface-container-low flex items-center justify-center text-on-surface-variant text-sm p-10">
            Select an episode — from the list or the sphere — to view its full replay.
          </div>
        )}
      </div>
    </div>
  );
}
