import { useLayoutEffect, useRef, useState, memo } from "react";

// Memoized so a selectedId change (fires on EVERY click) only re-renders the
// 1-2 rows whose own `isSelected` actually flipped, not the whole list --
// without this, React re-evaluated the className/ref ternary for every
// rendered row on every single click.
const EpisodeRow = memo(function EpisodeRow({ episode: ep, isSelected, onSelect, measureRef }) {
  return (
    <div
      ref={measureRef}
      onClick={() => onSelect(ep.episode_id)}
      className={`p-4 border-b border-outline-variant hover:bg-surface-variant cursor-pointer transition-colors ${
        isSelected ? "bg-surface-container-high border border-primary ring-1 ring-primary/40 -m-px" : ""
      }`}
    >
      <div className="flex justify-between items-start mb-2">
        <span className={`font-data-mono text-sm ${isSelected ? "text-primary" : "text-on-surface-variant"}`}>
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
  );
});

// Real fix, not another patch on top of memoization: with up to ~2000+
// episodes, the list previously kept EVERY row as a permanent DOM node and
// recreated all of their element descriptors on every re-render (visible in
// profiling as real jsxDEV/reconciliation cost even with rows memoized --
// memoization only stops re-rendering a row's INSIDES, it doesn't stop React
// having to walk the full list every time). This renders only the rows
// actually inside (plus a small overscan around) the scrollable viewport,
// same technique as react-window, hand-rolled since rows are close enough to
// uniform height to make that safe and it avoids a new dependency for one
// list. Row height is measured from the first real rendered row, not
// guessed, and refined if the actual DOM disagrees with the estimate.
const OVERSCAN = 8;
const FALLBACK_ROW_HEIGHT = 152;

export default function EpisodeList({ episodes, selectedId, onSelect, maxHeight }) {
  const scrollRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [rowHeight, setRowHeight] = useState(null);
  const measuredRef = useRef(null);

  const measureRow = (node) => {
    if (!node || measuredRef.current === node) return;
    measuredRef.current = node;
    const h = node.getBoundingClientRect().height;
    if (h > 0) setRowHeight(h);
  };

  const effectiveRowHeight = rowHeight ?? FALLBACK_ROW_HEIGHT;
  const viewportHeight = maxHeight ?? 600;

  const startIndex = Math.max(0, Math.floor(scrollTop / effectiveRowHeight) - OVERSCAN);
  const endIndex = Math.min(
    episodes.length,
    Math.ceil((scrollTop + viewportHeight) / effectiveRowHeight) + OVERSCAN
  );
  const visible = episodes.slice(startIndex, endIndex);
  const topSpacer = startIndex * effectiveRowHeight;
  const bottomSpacer = (episodes.length - endIndex) * effectiveRowHeight;

  const handleScroll = (e) => setScrollTop(e.currentTarget.scrollTop);

  // Selection can arrive from a cross-tab jump (Trust Ladder / Calibration)
  // to an episode nowhere near the current scroll position -- and with
  // virtualization, that row may not even be rendered yet, so a plain
  // `ref.scrollIntoView()` (the old approach) can't find it. Compute its
  // target scroll offset directly from its index instead, and re-run once
  // maxHeight arrives for the same reason as before (it starts null on
  // first mount, before the panel's real height is known).
  useLayoutEffect(() => {
    if (!selectedId || !scrollRef.current) return;
    const idx = episodes.findIndex((e) => e.episode_id === selectedId);
    if (idx < 0) return;
    const rowTop = idx * effectiveRowHeight;
    const el = scrollRef.current;
    if (rowTop < el.scrollTop || rowTop + effectiveRowHeight > el.scrollTop + el.clientHeight) {
      el.scrollTop = rowTop - viewportHeight / 2 + effectiveRowHeight / 2;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, maxHeight, episodes, effectiveRowHeight]);

  return (
    <section
      className="w-80 shrink-0 border-r border-outline-variant flex flex-col overflow-hidden"
      style={{ maxHeight: maxHeight ?? undefined }}
    >
      <div ref={scrollRef} onScroll={handleScroll} className="flex-1 min-h-0 overflow-y-auto">
        {episodes.length === 0 && (
          <p className="p-4 text-sm text-on-surface-variant">No episodes match this filter.</p>
        )}
        {topSpacer > 0 && <div style={{ height: topSpacer }} />}
        {visible.map((ep, i) => (
          <EpisodeRow
            key={ep.episode_id}
            episode={ep}
            isSelected={ep.episode_id === selectedId}
            onSelect={onSelect}
            measureRef={startIndex + i === 0 ? measureRow : undefined}
          />
        ))}
        {bottomSpacer > 0 && <div style={{ height: bottomSpacer }} />}
      </div>
    </section>
  );
}
