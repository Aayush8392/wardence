import { useLayoutEffect, useRef, useState, memo } from "react";

// Memoized so a selectedId change (fires on EVERY click) only re-renders the
// 1-2 rows whose own `isSelected` actually flipped, not the whole list --
// without this, React re-evaluated the className/ref ternary for every
// rendered row on every single click.
// `animationDelay` (ms, optional) -- purely visual stagger on mount/re-
// filter, no interaction tied to it. `matrix-row-hover` reuses the exact
// same tactile hover technique (scale + shift + colored left inset border)
// already used on the Trust Ladder table, for visual consistency across
// the app rather than inventing a second hover style.
// Real "click selected row again to deselect" behavior -- passes `null`
// up to the caller (same convention DotSphere's own click handler already
// uses) instead of always navigating to itself, so `onSelect`'s identity
// stays stable (still just `navigate`) rather than needing to know the
// current selection to decide -- that would've broken this memoization.
const EpisodeRow = memo(function EpisodeRow({ episode: ep, isSelected, onSelect, measureRef, animationDelay }) {
  return (
    <div
      ref={measureRef}
      onClick={() => onSelect(isSelected ? null : ep.episode_id)}
      style={animationDelay != null ? { animationDelay: `${animationDelay}ms` } : undefined}
      className={`episode-row-enter matrix-row-hover rounded-lg mb-2 p-4 cursor-pointer transition-colors border ${
        isSelected
          ? "bg-surface-container-high border-primary ring-1 ring-primary/40"
          : "bg-surface-container-low border-transparent hover:bg-surface-variant"
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
  // Scroll-position-linked fade gradients (same real technique React Bits'
  // AnimatedList uses) -- purely visual, hints there's more content
  // above/below without a hard-edged cutoff.
  const [topFade, setTopFade] = useState(0);
  const [bottomFade, setBottomFade] = useState(1);

  const measureRow = (node) => {
    if (!node || measuredRef.current === node) return;
    measuredRef.current = node;
    // Real fix: rows now have a real mb-2 gap between them (the rounded-
    // card look) -- getBoundingClientRect() only covers the border box,
    // NOT margin, so the per-row footprint used for virtualization's
    // spacer/scroll math needs the row's own real computed marginBottom
    // added in, or it silently drifts out of sync by 8px per row (~2800
    // rows = ~22,400px of accumulated error).
    const rect = node.getBoundingClientRect();
    const marginBottom = parseFloat(getComputedStyle(node).marginBottom) || 0;
    const h = rect.height + marginBottom;
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

  const updateFade = (el) => {
    if (!el) return;
    const { scrollTop: st, scrollHeight, clientHeight } = el;
    setTopFade(Math.min(st / 40, 1));
    const bottomDistance = scrollHeight - (st + clientHeight);
    setBottomFade(scrollHeight <= clientHeight ? 0 : Math.min(bottomDistance / 40, 1));
  };
  const handleScroll = (e) => {
    setScrollTop(e.currentTarget.scrollTop);
    updateFade(e.currentTarget);
  };
  // Real fix: bottomFade defaulted to 1 (assuming there's always more
  // content below), but a short filtered list that fits entirely in the
  // viewport never fires a scroll event to correct that -- it would show a
  // bottom fade hinting at content that doesn't exist. Recompute whenever
  // the real episode count (a filter/search/date-range change) changes.
  useLayoutEffect(() => {
    updateFade(scrollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodes]);

  // Real fix: applying a new filter (search/class/date/sort) left the
  // scroll position wherever it happened to be, which could land on a
  // blank/mismatched spot in the newly-filtered list. Only resets to top
  // when nothing is selected -- the scroll-to-selected effect below
  // already owns positioning correctly whenever a real selection exists,
  // and shouldn't fight with this one.
  useLayoutEffect(() => {
    if (selectedId || !scrollRef.current) return;
    scrollRef.current.scrollTop = 0;
    setScrollTop(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodes]);

  // Selection can arrive from a cross-tab jump (Trust Ladder / Calibration)
  // to an episode nowhere near the current scroll position -- and with
  // virtualization, that row may not even be rendered yet, so a plain
  // `ref.scrollIntoView()` (the old approach) can't find it. Compute its
  // target scroll offset directly from its index instead, and re-run once
  // maxHeight arrives for the same reason as before (it starts null on
  // first mount, before the panel's real height is known). `behavior:
  // "smooth"` per explicit ask -- the old instant jump felt snappy/jarring.
  useLayoutEffect(() => {
    if (!selectedId || !scrollRef.current) return;
    const idx = episodes.findIndex((e) => e.episode_id === selectedId);
    if (idx < 0) return;
    const rowTop = idx * effectiveRowHeight;
    const el = scrollRef.current;
    if (rowTop < el.scrollTop || rowTop + effectiveRowHeight > el.scrollTop + el.clientHeight) {
      el.scrollTo({ top: rowTop - viewportHeight / 2 + effectiveRowHeight / 2, behavior: "smooth" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, maxHeight, episodes, effectiveRowHeight]);

  return (
    // Real fix (2026-07-24 hardcoded-value audit, parked -- flagged as
    // HIGHEST PRIORITY there, picked back up now): w-80 was a flat 320px
    // regardless of screen size -- cramped on 4K/ultrawide, disproportionate
    // on smaller monitors. w-1/4 makes this a real percentage of the row it
    // shares with the sphere (flex-1 absorbs the rest), so it scales
    // consistently with the actual available width instead of a fixed number.
    <section
      className="relative w-1/4 shrink-0 border-r border-outline-variant flex flex-col overflow-hidden"
      style={{ maxHeight: maxHeight ?? undefined }}
    >
      <div
        className="pointer-events-none absolute top-0 left-0 right-0 h-8 z-10 bg-gradient-to-b from-surface-container to-transparent transition-opacity"
        style={{ opacity: topFade }}
      />
      {/* overflow-x-hidden: the hover state's translateX(3px) + selected
          row's ring can extend a couple px past the container's own edge
          -- without this the browser adds a horizontal scrollbar to
          accommodate that, which was the real cause of the small
          horizontal-scroll glitch on selection. */}
      <div ref={scrollRef} onScroll={handleScroll} className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden">
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
            animationDelay={Math.min(i * 30, 300)}
          />
        ))}
        {bottomSpacer > 0 && <div style={{ height: bottomSpacer }} />}
      </div>
      <div
        className="pointer-events-none absolute bottom-0 left-0 right-0 h-8 z-10 bg-gradient-to-t from-surface-container to-transparent transition-opacity"
        style={{ opacity: bottomFade }}
      />
    </section>
  );
}
