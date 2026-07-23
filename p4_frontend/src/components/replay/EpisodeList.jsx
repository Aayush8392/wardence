import { useEffect, useRef } from "react";

export default function EpisodeList({ episodes, selectedId, onSelect, maxHeight }) {
  // Selection highlight alone doesn't bring the row into view -- the list
  // is sorted t0 DESC and capped to the detail panel's height (internal
  // scroll), so a selection arriving via cross-tab jump (Trust Ladder /
  // Calibration) can land anywhere in that scroll range. Scroll it into
  // view explicitly whenever the selection changes.
  const selectedRef = useRef(null);

  // Also re-run once maxHeight arrives (not just on selectedId change) --
  // maxHeight starts null on first mount (the detail panel's ResizeObserver
  // hasn't measured yet), so the list has no internal scroll container to
  // scroll WITHIN when this first fires. Without maxHeight in the deps, the
  // scroll silently no-ops (or scrolls the outer page instead, which then
  // gets reset once the container becomes height-capped a frame later) and
  // nothing re-triggers it once the real cap lands.
  useEffect(() => {
    selectedRef.current?.scrollIntoView({ block: "center" });
  }, [selectedId, maxHeight]);

  return (
    <section
      className="w-80 shrink-0 border-r border-outline-variant flex flex-col overflow-hidden"
      style={{ maxHeight: maxHeight ?? undefined }}
    >
      <div className="flex-1 min-h-0 overflow-y-auto">
        {episodes.length === 0 && (
          <p className="p-4 text-sm text-on-surface-variant">No episodes match this filter.</p>
        )}
        {episodes.map((ep) => (
          <div
            key={ep.episode_id}
            ref={ep.episode_id === selectedId ? selectedRef : null}
            onClick={() => onSelect(ep.episode_id)}
            className={`p-4 border-b border-outline-variant hover:bg-surface-variant cursor-pointer transition-colors ${
              ep.episode_id === selectedId
                ? "bg-surface-container-high border border-primary ring-1 ring-primary/40 -m-px"
                : ""
            }`}
          >
            <div className="flex justify-between items-start mb-2">
              <span className={`font-data-mono text-sm ${ep.episode_id === selectedId ? "text-primary" : "text-on-surface-variant"}`}>
                {ep.episode_id.slice(0, 8)}
              </span>
              <span
                className={`px-2 py-0.5 border font-label-caps text-[9px] ${
                  ep.correct ? "border-[#238636] text-[#238636]" : "border-error text-error"
                }`}
              >
                {ep.correct ? "CORRECT" : "WRONG"}
              </span>
            </div>
            <div className="space-y-1">
              <div className="flex justify-between">
                <span className="font-label-caps text-[10px] text-on-surface-variant uppercase">Fault:</span>
                <span className="font-data-mono text-xs">{ep.fault_class}</span>
              </div>
              <div className="flex justify-between">
                <span className="font-label-caps text-[10px] text-on-surface-variant uppercase">Target:</span>
                <span className="font-data-mono text-xs">{ep.target}</span>
              </div>
              <div className="mt-2">
                <span className="font-data-mono text-[11px] text-on-surface-variant">{ep.t0}</span>
              </div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
