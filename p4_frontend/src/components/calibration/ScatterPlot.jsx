import { useMemo, useRef, useState, useCallback, useEffect } from "react";

// Same threshold as the rest of the project's confidence-calibration
// framing -- high confidence AND wrong is the specific thing worth
// visually calling out (the "hallucination" case).
const HALLUCINATION_CONFIDENCE_THRESHOLD = 0.8;

// Fixed SVG WIDTH -- same pattern as EfficiencyFrontier.jsx, which
// never hit any of the width/overlap bugs this component went through
// several broken iterations on. `preserveAspectRatio="none"` (below)
// lets the SVG's own CSS width/height (w-full h-full on the wrapper)
// stretch this fixed logical WIDTH to fill whatever the real panel
// gives it -- no ResizeObserver, no JS-measured fitScale, no min-w-0
// CSS trap, and critically: no way for a large/small dataset to ever
// feed back into how WIDE the plot renders (the actual root cause
// every prior version of this file kept re-discovering in a different
// form -- a real ~254-episode cohort collapsing fitScale's height
// term and crushing the whole canvas including its width down to a
// thin center sliver, confirmed via the user's own browser console).
//
// HEIGHT, unlike WIDTH, is deliberately NOT a fixed constant --
// it's derived per-render from real point density (see
// `bandHeights`/`HEIGHT` inside the component below). A fixed height
// re-created the same class of bug in a new form: once collision
// resolution became Y-only (the X-axis-accuracy fix), a dense band
// had nowhere left to spread and dots were silently clipped past the
// visible viewBox edge even at max zoom-out. Deriving HEIGHT from
// real content guarantees every dot always has real room to exist
// inside the viewBox, the same guarantee WIDTH already had.
const WIDTH = 1000;
const MARGIN = { top: 16, right: 16, bottom: 8, left: 16 };

const DOT_R = 5; // px, in SVG user-space units (== screen px at 1:1, since viewBox matches typical rendered width)
const HALLUCINATION_DOT_R = 7;

const MAX_ZOOM = 24;
const WHEEL_SENSITIVITY = 0.0015;

// Small extra breathing room (SVG units) kept between the pan clamp's
// true content edge and the visible viewport edge -- without this, a
// dot sitting exactly at confidence 0.0 or 1.0 renders with its
// CENTER right on the panel border once panned all the way to that
// edge, so half the dot (its radius) visually clips past the panel's
// own overflow-hidden boundary. This nudges the clamp to stop
// slightly short of the true edge, just enough to keep a full dot
// radius comfortably inside the visible window.
const EDGE_PAD = DOT_R * 3;

// Real greedy spatial separation -- replaces the earlier bucket-based
// nudge, which had a real, measured flaw: two points whose raw x
// differed by just over half a bucket width could land in ADJACENT
// buckets and get nudged independently with no knowledge of each
// other, sometimes landing almost exactly on top of each other by
// coincidence (confirmed via direct measurement on a real 454-point
// dataset: 1698 overlapping pairs, including near-zero distances).
// This version processes points in x-order and, for each one, checks
// real distance against every already-placed point within a local
// window (not just points that happened to round into the same
// bucket) -- if too close, pushes it directly away along the real
// vector between them. A few passes converge quickly since only local
// neighbors ever interact.
const MIN_SEPARATION = DOT_R * 2 + 1; // real minimum center-to-center distance

// Real, larger invisible hit-target radius (SVG user-space units,
// same counter-scaled-to-constant-screen-size treatment as DOT_R) --
// lets a click land within this radius of a dot's real center and
// still count, instead of requiring pixel-perfect placement on the
// ~5px visible dot itself.
//
// This can only be safely enabled once dots are far enough apart on
// SCREEN that neighboring hit-circles don't overlap and start eating
// each other's clicks -- exactly the failure mode collision
// resolution (MIN_SEPARATION) already exists to prevent for the
// visible dots themselves, but MIN_SEPARATION is a fixed LOGICAL
// distance; two dots that are MIN_SEPARATION apart in plot-space can
// still be arbitrarily close together in screen-space at low zoom
// (view.scale multiplies logical distance to get screen distance).
// So the threshold below is the real zoom level past which even two
// dots sitting at the closest allowed logical distance (MIN_SEPARATION)
// still have non-overlapping hit-circles on screen:
//   view.scale * MIN_SEPARATION >= 2 * HIT_R
//   view.scale >= (2 * HIT_R) / MIN_SEPARATION
// Derived, not guessed -- if HIT_R or MIN_SEPARATION change, this
// recomputes correctly on its own.
const HIT_R = 12;
const HIT_TARGET_MIN_ZOOM = (2 * HIT_R) / MIN_SEPARATION;

// Real, deliberate correctness rule (this is the fix for the
// "dot rendered at 0.85 shows up between 0.74 and 0.76" bug): a dot's
// X position on this plot IS its confidence value -- that's the axis
// being read. Collision resolution is only ever allowed to move a
// point along Y (which band a dot settles into vertically has no
// semantic meaning, it's purely a beeswarm spacing device). Moving X
// to resolve a collision silently corrupts the one value the chart
// exists to show. Every push/nudge below is now Y-only.
function resolveCollisions(rawPoints, bandBounds) {
  const placed = rawPoints.map((p) => ({ ...p, x: p.rawX, y: p.rawY }));
  // Cell width can stay wide (points never move in X, so an X-bucket
  // only ever needs to be wide enough to catch real neighbors); cell
  // height matches MIN_SEPARATION as before.
  const cellW = MIN_SEPARATION;
  const cellH = MIN_SEPARATION;

  const clampToBand = (p) => {
    const b = bandBounds[p.band];
    if (!b) return;
    if (p.y < b.min) p.y = b.min;
    if (p.y > b.max) p.y = b.max;
  };

  const PASSES = 150;
  for (let pass = 0; pass < PASSES; pass++) {
    // Rebuild the spatial hash EVERY pass from current (possibly
    // already-moved) positions -- this is what a static one-time sort
    // window got wrong: after points move, their real neighbors change,
    // and a grid must be recomputed to reflect that or collisions
    // between points that only became close in a LATER pass go
    // undetected.
    const grid = new Map();
    const cellKey = (x, y) => `${Math.floor(x / cellW)}:${Math.floor(y / cellH)}`;
    for (const p of placed) {
      const key = cellKey(p.x, p.y);
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push(p);
    }

    let anyMoved = false;
    for (const p of placed) {
      const cx = Math.floor(p.x / cellW);
      const cy = Math.floor(p.y / cellH);
      // Check this point's own cell plus all 8 neighbors -- any real
      // collision within MIN_SEPARATION must be in one of these 9
      // cells, since cellW/cellH == MIN_SEPARATION.
      for (let gx = cx - 1; gx <= cx + 1; gx++) {
        for (let gy = cy - 1; gy <= cy + 1; gy++) {
          const bucket = grid.get(`${gx}:${gy}`);
          if (!bucket) continue;
          for (const q of bucket) {
            if (q === p) continue;
            const dx = p.x - q.x;
            const dy = p.y - q.y;
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist < MIN_SEPARATION && dist > 1e-6) {
              const push = (MIN_SEPARATION - dist) / 2;
              // Y-only: if the two points happen to sit at the exact
              // same Y (dy===0, e.g. fresh off bandBase before any
              // pass has run), give each a deterministic opposite
              // nudge so they don't get stuck pushing purely along a
              // now-forbidden X.
              const dir = dy === 0 ? (p === bucket[0] ? 1 : -1) : dy / Math.abs(dy);
              p.y += dir * push;
              q.y -= dir * push;
              clampToBand(p);
              clampToBand(q);
              anyMoved = true;
            } else if (dist <= 1e-6) {
              // Exact coincidence (rare, but real -- possible when two
              // episodes share an identical confidence value). Y-only
              // nudge in a random direction (up or down), never X.
              const dir = Math.random() < 0.5 ? 1 : -1;
              const nudge = MIN_SEPARATION / 2;
              p.y += dir * nudge;
              q.y -= dir * nudge;
              clampToBand(p);
              clampToBand(q);
              anyMoved = true;
            }
          }
        }
      }
    }
    if (!anyMoved) break;
  }

  return placed;
}

// Real axis ticks, derived from the CURRENT view (pan/zoom), same
// "nice interval" logic as before -- picks 0.5/0.2/0.1/0.05/0.02/0.01...
// so zooming in reveals genuinely finer confidence values instead of
// just enlarging the same fixed labels.
const NICE_STEPS = [0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001];

function pickTickStep(visibleSpanConfidence) {
  const targetTickCount = 7;
  for (const step of NICE_STEPS) {
    if (visibleSpanConfidence / step >= targetTickCount) return step;
  }
  return NICE_STEPS[NICE_STEPS.length - 1];
}

function decimalsForStep(step) {
  if (step >= 0.1) return 1;
  if (step >= 0.01) return 2;
  return 3;
}

const plotW = WIDTH - MARGIN.left - MARGIN.right;

// confidence (0..1) -> SVG x, BEFORE pan/zoom
const confToPlotX = (c) => MARGIN.left + c * plotW;
// inverse, for reading back real confidence from a plot-space x
const plotXToConf = (x) => (x - MARGIN.left) / plotW;

function computeVisibleTicks(view) {
  // view = { scale, tx, ty } -- an SVG-space transform: screenX = (plotX + tx) * scale
  const visMinX = -view.tx;
  const visMaxX = WIDTH / view.scale - view.tx;
  const cMin = Math.max(0, plotXToConf(visMinX));
  const cMax = Math.min(1, plotXToConf(visMaxX));
  if (cMax <= cMin) return [];

  // Tick STEP is derived from scale alone (a full-width, unclamped
  // confidence span at the current zoom level), never from the real
  // clamped [cMin, cMax] visible span. This is the fix for a real bug:
  // near either content edge, EDGE_PAD (see clampView) lets the pan
  // clamp go slightly past the true 0.0/1.0 confidence bounds, but
  // cMin/cMax are hard-clamped to [0, 1] -- so at the same zoom level,
  // being panned exactly to an edge (cMax clamps to a clean 1.0) vs.
  // being one drag short of it (cMax lands at some unclamped value
  // like 0.998) produced two DIFFERENT visible span WIDTHS purely
  // from pan position, which crossed pickTickStep's threshold and
  // silently changed the tick density mid-drag with no actual zoom
  // change. Using the true unclamped full-viewport span for the step
  // decision (only the individual tick VALUES get clamped to [0,1]
  // below) ties tick density to scale only, exactly as intended.
  const fullSpanConfidence = (WIDTH / view.scale) / plotW;
  const step = pickTickStep(fullSpanConfidence);
  const decimals = decimalsForStep(step);
  const ticks = [];
  const start = Math.ceil(cMin / step) * step;
  for (let c = start; c <= cMax + 1e-9; c += step) {
    const rounded = Math.round(c * 1000) / 1000;
    if (rounded < -1e-9 || rounded > 1 + 1e-9) continue;
    ticks.push({ value: rounded, plotX: confToPlotX(rounded), decimals });
  }
  return ticks;
}

function clampView(next, contentHeight) {
  const scale = Math.min(Math.max(next.scale, 1), MAX_ZOOM);
  // Real, exact content-edge clamp -- bounds the visible window to
  // where the DOTS actually live (confidence 0.0..1.0, mapped via
  // confToPlotX to [MARGIN.left, WIDTH - MARGIN.right]), not the
  // full [0, WIDTH] viewBox. The earlier version clamped to the full
  // viewBox, which left MARGIN.left/right of real slack: you could
  // pan until the margin's empty space was fully in view past the
  // true edge, which is exactly why 1.0 could still be dragged short
  // of the right edge (and kept receding further as you zoomed in --
  // margin space is a fixed SVG-unit amount, so at higher scale it
  // maps to a proportionally larger, more noticeable screen gap).
  //
  // The screen shows a window of WIDTH/scale content-units at any
  // given scale (screenX = (plotX + tx) * scale, so the visible
  // content-space window is [-tx, WIDTH/scale - tx]). For that window
  // to never show anything outside the real content bounds
  // [MARGIN.left, WIDTH - MARGIN.right]:
  //   -tx <= MARGIN.left           =>  tx >= -MARGIN.left
  //   WIDTH/scale - tx >= WIDTH - MARGIN.right
  //                                =>  tx <= WIDTH/scale - WIDTH + MARGIN.right
  // At scale===1 the WIDTH/scale window exactly equals [0, WIDTH],
  // wider than the real content bounds on both sides -- both
  // constraints are simultaneously violated no matter what tx is;
  // this is intentional and handled by the midpoint-collapse below,
  // which happens to work out to tx===0 exactly at scale===1 (WIDTH
  // and MARGIN.left/right are symmetric), matching the already-
  // confirmed no-pan-at-max-zoom-out behavior. As scale grows past
  // the point where the window fits inside the content span, the two
  // bounds separate and behave as a normal clamp tied to the real
  // content edges rather than the viewBox edges.
  const maxTx = -(MARGIN.left - EDGE_PAD);
  const minTx = WIDTH / scale - WIDTH + MARGIN.right - EDGE_PAD;
  const maxTy = 0;
  const minTy = contentHeight / scale - contentHeight;

  // Collapse to the midpoint whenever the window is too wide for both
  // edge constraints to hold at once (min > max) -- centers content
  // with its margins evenly rather than letting Math.min/Math.max
  // silently pick whichever bound happens to win.
  const clampAxis = (value, min, max) => {
    if (min > max) return (min + max) / 2;
    return Math.min(max, Math.max(min, value));
  };
  return {
    scale,
    tx: clampAxis(next.tx, minTx, maxTx),
    ty: clampAxis(next.ty, minTy, maxTy),
  };
}

export default function ScatterPlot({ episodes, selectedId, onSelect }) {
  const svgRef = useRef(null);
  const [view, setView] = useState({ scale: 1, tx: 0, ty: 0 });
  const dragState = useRef(null);
  const wasDragRef = useRef(false);

  // Real fix for dots rendering as ovals instead of circles.
  //
  // Root cause: the <svg> uses preserveAspectRatio="none" (needed so
  // gridlines/content fill the real container box edge-to-edge
  // regardless of aspect ratio) -- this means the viewBox's intrinsic
  // WIDTH:HEIGHT ratio gets independently stretched in X and Y to
  // match whatever the CONTAINER's real rendered CSS box ratio
  // happens to be. A circle drawn with equal cx/cy radius in viewBox
  // units only stays visually circular if that stretch is uniform
  // (WIDTH:HEIGHT == container's real renderedWidth:renderedHeight).
  // This was invisible before the Y-axis class-row redesign, when
  // HEIGHT was small/roughly-fixed -- now HEIGHT is a real, much
  // larger, data-dependent sum (many stacked class-rows), so the
  // viewBox's real aspect ratio has drifted far from the container's
  // actual rendered aspect ratio, and the stretch is now visibly
  // non-uniform (dots squashed into ovals).
  //
  // Fix: measure the container's REAL rendered box (ResizeObserver,
  // since it can change on window resize / layout changes, not just
  // once at mount) and compute the real per-axis stretch ratio
  // directly from it, then apply a corrective Y-scale to every dot's
  // own local transform so its radius reads as circular on screen
  // regardless of how the surrounding viewBox has been stretched.
  // Gridlines/separators are deliberately NOT corrected -- they
  // SHOULD stretch to fill the box, only dots need to counteract it.
  const containerRef = useRef(null);
  const [containerAspect, setContainerAspect] = useState(null); // renderedHeight / renderedWidth, or null until measured

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        setContainerAspect(rect.height / rect.width);
      }
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Reset pan/zoom only when the actual DATA changes (a different
  // model/class cohort selected) -- never on resize, since the fixed
  // viewBox means resize can't change anything about the underlying
  // coordinate space anyway.
  const episodeSignature = episodes.map((e) => e.episode_id).join(",");
  useEffect(() => {
    setView({ scale: 1, tx: 0, ty: 0 });
  }, [episodeSignature]);

  // Real, deliberate field-name note: this component reads two fields
  // per episode that aren't used anywhere else in this file --
  // `fault_class` (confirmed real per wardence_context.md's episode
  // schema) and a timestamp used purely for recency ordering. The
  // timestamp field's exact name isn't confirmed from any file
  // actually available while building this -- `t0` is the name used
  // in the architecture doc's own episode-schema example, so it's
  // tried first, with a couple of plausible fallbacks. If NONE of
  // these exist on the real episode object, every episode silently
  // falls into a single "unknown time" quartile bucket (oldest) rather
  // than crashing -- but the recency ordering described below won't
  // be real until this is confirmed. Grep the real episodes.json for
  // the actual field name and fix this list if it's not one of these.
  const getEpisodeTime = (ep) => {
    const raw = ep.t0 ?? ep.timestamp ?? ep.episode_t0 ?? ep.created_at ?? null;
    if (raw == null) return null;
    const t = typeof raw === "number" ? raw : Date.parse(raw);
    return Number.isFinite(t) ? t : null;
  };

  const FAULT_CLASS_FALLBACK = "unknown-class";
  const getEpisodeClass = (ep) => ep.fault_class ?? FAULT_CLASS_FALLBACK;

  // 4 discrete recency buckets per class-row (see the chat discussion
  // this was designed in) -- deliberately NOT a continuous
  // rank-to-pixel mapping. Quartiles are recomputed fresh from
  // whatever `episodes` is currently passed in on every render (no
  // persisted rank anywhere), so a cohort growing from e.g. 90 to 190
  // episodes just re-derives quartile membership against the new
  // total automatically -- two episodes that used to sit in different
  // quartiles can legitimately end up in the same one once enough
  // newer episodes exist above them, and that's correct behavior, not
  // a bug: "newest 25%" is inherently a moving target.
  //
  // At this project's real per-(class×model) cohort sizes (~15-25
  // episodes today, per the chat discussion, not hundreds) each
  // quartile only ever holds a handful of dots -- collision resolution
  // (already built for arbitrary density) comfortably handles that
  // without needing finer/diminishing-window bucketing, which would
  // solve a scale problem this project doesn't have.
  const QUARTILE_COUNT = 4;
  // quartile 0 = most recent, QUARTILE_COUNT-1 = oldest.
  function quartileOf(rankFromNewest, total) {
    if (total <= 1) return 0;
    return Math.min(QUARTILE_COUNT - 1, Math.floor((rankFromNewest / total) * QUARTILE_COUNT));
  }

  // Group episodes by fault_class, then rank each class's own episodes
  // by recency (newest first) to assign a quartile within that class
  // only -- recency is relative to the class's own cohort, not the
  // whole dataset, matching "within this class, which of its episodes
  // are newest" rather than a global time axis.
  const classGroups = useMemo(() => {
    const byClass = new Map();
    for (const ep of episodes) {
      const cls = getEpisodeClass(ep);
      if (!byClass.has(cls)) byClass.set(cls, []);
      byClass.get(cls).push(ep);
    }
    // Stable, deterministic class ordering (alphabetical) so rows
    // don't visually reshuffle between renders/cohort switches for
    // reasons unrelated to the actual data changing.
    const classNames = Array.from(byClass.keys()).sort();
    return classNames.map((cls) => {
      const list = byClass.get(cls);
      // Newest first. Episodes with no resolvable timestamp sort to
      // the end (treated as oldest) rather than crashing or sorting
      // arbitrarily.
      const sorted = [...list].sort((a, b) => {
        const ta = getEpisodeTime(a);
        const tb = getEpisodeTime(b);
        if (ta == null && tb == null) return 0;
        if (ta == null) return 1;
        if (tb == null) return -1;
        return tb - ta; // descending: newest first
      });
      const total = sorted.length;
      const withQuartile = sorted.map((ep, i) => ({
        ep,
        quartile: quartileOf(i, total),
      }));
      return { cls, episodes: withQuartile, total };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodes]);

  // Real, content-derived plot height -- unchanged principle from the
  // original fixed-band version (content must always determine its
  // own required space, never get silently clipped to fit a
  // constant), just generalized from 2 bands to N class-rows x 4
  // quartile sub-bands each.
  const plotWInner = WIDTH - MARGIN.left - MARGIN.right;
  const bandGap = 24; // fixed visual gap between class-rows, in SVG units
  const quartileGap = 4; // small fixed gap between quartile sub-bands within one row

  // Per-row height: same densest-x-bucket packing estimate the
  // original 2-band version used, just applied within each class's
  // own subset of episodes instead of the whole correct/incorrect
  // half.
  const estimateRowHeight = (list) => {
    if (list.length === 0) return MIN_SEPARATION * 4;
    const buckets = new Map();
    for (const { ep } of list) {
      const x = confToPlotX(ep.score_confidence);
      const key = Math.floor(x / MIN_SEPARATION);
      buckets.set(key, (buckets.get(key) || 0) + 1);
    }
    const maxBucket = Math.max(...buckets.values());
    const rowsForDensestBucket = Math.ceil(maxBucket / 3);
    const needed = rowsForDensestBucket * MIN_SEPARATION * 2.2 + MIN_SEPARATION * 3;
    return Math.max(MIN_SEPARATION * 4, needed);
  };

  const rowHeights = useMemo(
    () => classGroups.map((g) => estimateRowHeight(g.episodes)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [classGroups]
  );

  const plotH =
    rowHeights.reduce((sum, h) => sum + h, 0) + Math.max(0, classGroups.length - 1) * bandGap;
  const HEIGHT = plotH + MARGIN.top + MARGIN.bottom;

  // Real top offset per class-row, stacked top-to-bottom in the same
  // alphabetical class order as classGroups.
  const rowTop = useMemo(() => {
    const tops = [];
    let y = MARGIN.top;
    for (const h of rowHeights) {
      tops.push(y);
      y += h + bandGap;
    }
    return tops;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rowHeights]);

  // Each row is further split into 4 stacked quartile sub-bands
  // (quartile 0 = newest = TOP of the row, quartile 3 = oldest =
  // BOTTOM), each getting an equal share of the row's own height minus
  // the small fixed gaps between sub-bands. Collision resolution then
  // runs per (class, quartile) band exactly like it used to run per
  // (correct, incorrect) band -- same generic bandBounds/bandBase
  // mechanism, just with more, smaller bands.
  const bandTop = {}; // keyed by `${cls}::${quartile}`
  const bandHeightsMap = {}; // keyed by `${cls}::${quartile}`
  const bandBase = {};
  const bandBounds = {};

  classGroups.forEach((g, rowIdx) => {
    const rowH = rowHeights[rowIdx];
    const subH = (rowH - quartileGap * (QUARTILE_COUNT - 1)) / QUARTILE_COUNT;
    for (let q = 0; q < QUARTILE_COUNT; q++) {
      const key = `${g.cls}::${q}`;
      const top = rowTop[rowIdx] + q * (subH + quartileGap);
      bandTop[key] = top;
      bandHeightsMap[key] = subH;
      bandBase[key] = top + subH / 2;
      bandBounds[key] = { min: top + DOT_R, max: top + subH - DOT_R };
    }
  });

  const points = useMemo(() => {
    const raw = [];
    classGroups.forEach((g) => {
      for (const { ep, quartile } of g.episodes) {
        const band = `${g.cls}::${quartile}`;
        raw.push({
          ep,
          rawX: confToPlotX(ep.score_confidence),
          rawY: bandBase[band],
          band,
        });
      }
    });
    return resolveCollisions(raw, bandBounds);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [classGroups, rowHeights]);

  const visibleTicks = useMemo(() => computeVisibleTicks(view), [view]);

  // Wheel = zoom centered on cursor, in SVG user-space (converted from
  // client px via getScreenCTM, so it stays correct regardless of how
  // much the SVG has been CSS-stretched by preserveAspectRatio).
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;

    const handleWheel = (e) => {
      e.preventDefault();
      const pt = el.createSVGPoint();
      pt.x = e.clientX;
      pt.y = e.clientY;
      const ctm = el.getScreenCTM();
      if (!ctm) return;
      const svgPt = pt.matrixTransform(ctm.inverse());

      setView((prev) => {
        const factor = Math.exp(-e.deltaY * WHEEL_SENSITIVITY);
        const rawNext = { scale: prev.scale * factor, tx: prev.tx, ty: prev.ty };
        const clampedScale = Math.min(Math.max(rawNext.scale, 1), MAX_ZOOM);
        // Keep the point under the cursor fixed: solve for tx/ty such
        // that (plotPointUnderCursor + t) * scale stays constant.
        const plotUnderCursorX = svgPt.x / prev.scale - prev.tx;
        const plotUnderCursorY = svgPt.y / prev.scale - prev.ty;
        const newTx = svgPt.x / clampedScale - plotUnderCursorX;
        const newTy = svgPt.y / clampedScale - plotUnderCursorY;
        return clampView({ scale: clampedScale, tx: newTx, ty: newTy }, HEIGHT);
      });
    };

    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, [HEIGHT]);

  const onPointerDown = useCallback(
    (e) => {
      if (e.target.closest("button")) return;
      dragState.current = { startX: e.clientX, startY: e.clientY, origTx: view.tx, origTy: view.ty, moved: false };
      e.currentTarget.setPointerCapture(e.pointerId);
    },
    [view.tx, view.ty]
  );

  const onPointerMove = useCallback((e) => {
    const drag = dragState.current;
    if (!drag) return;
    const el = svgRef.current;
    if (!el) return;
    const ctm = el.getScreenCTM();
    if (!ctm) return;
    // Convert client-px deltas to SVG user-space deltas (divide by the
    // real CTM scale, since the SVG may be CSS-stretched wider/narrower
    // than its viewBox). Horizontal and vertical need their OWN scale
    // factors (ctm.a vs ctm.d), not a shared one -- this component's
    // viewBox is stretched non-uniformly (preserveAspectRatio="none",
    // fixed WIDTH=1000 vs a data-dependent HEIGHT that can run into the
    // thousands once many class-rows stack up), the same asymmetric
    // stretch that caused the earlier oval-dot bug. ctm.d (vertical) is
    // typically much smaller than ctm.a (horizontal) as a result -- using
    // ctm.a for dy too under-converts vertical screen-px movement into a
    // too-small user-space delta, which is exactly why vertical dragging
    // felt slow/undersized relative to horizontal dragging.
    const scaleX = ctm.a;
    const scaleY = ctm.d;
    const dx = (e.clientX - drag.startX) / scaleX;
    const dy = (e.clientY - drag.startY) / scaleY;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true;
    if (drag.moved) {
      setView((prev) => clampView({ scale: prev.scale, tx: drag.origTx + dx / prev.scale, ty: drag.origTy + dy / prev.scale }, HEIGHT));
    }
  }, []);

  const onPointerUp = useCallback((e) => {
    // Record whether this pointer session was a real drag (moved past
    // the 2px threshold in onPointerMove) BEFORE clearing dragState --
    // the browser fires a trailing click on the same target right
    // after pointerup, and onBackgroundClick below needs to know not
    // to treat that trailing click as "clicked empty space."
    wasDragRef.current = !!dragState.current?.moved;
    dragState.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      // no-op -- capture may already have been released
    }
  }, []);

  // Deselect on any click that reaches the SVG background itself --
  // covers both "click empty space inside the graph" (no dot under
  // the cursor, so nothing stopped propagation) and "click outside
  // the graph area" (handled separately below, since that's outside
  // this SVG entirely). A real dot's own onClick calls
  // e.stopPropagation() so a click that actually lands on/near a dot
  // never reaches here at all -- this only fires for genuine misses.
  //
  // wasDragRef guards against the click event the browser synthesizes
  // right after a real drag-release (pointerup ends the drag, but a
  // trailing click still fires on the same target) -- without this,
  // panning the view and releasing over empty space would immediately
  // deselect whatever was selected before the drag even started,
  // which isn't what "click empty space to deselect" means.
  const onBackgroundClick = useCallback(() => {
    if (wasDragRef.current) {
      wasDragRef.current = false;
      return;
    }
    onSelect(null);
  }, [onSelect]);

  const transformStr = `scale(${view.scale}) translate(${view.tx}, ${view.ty})`;

  // Real, deliberate CHANGE from constant-screen-size counter-scaling
  // (small at max zoom-out was too small per the real screenshot this
  // was reported from; the old behavior also had the relationship
  // backwards from what's wanted -- it made dots SHRINK as you zoomed
  // IN, since dotCounterScale = 1/view.scale). New rule: dots render
  // small at max zoom-out (view.scale=1) and GROW toward a capped
  // maximum screen size as you zoom in (toward MAX_ZOOM), instead of
  // staying a fixed size the whole time.
  //
  // Screen size (real on-screen px, not SVG user-space units) is
  // interpolated linearly from MIN_SCREEN_DOT_R at scale=1 to
  // MAX_SCREEN_DOT_R at scale=MAX_ZOOM, clamped at both ends. Since
  // SVG user-space radius r relates to on-screen size via
  // screenSize = r * view.scale (that's what the <g scale(...)>
  // transform does), the actual r passed to each <circle> has to be
  // BACK-SOLVED from the desired screen size: r = screenSize / scale.
  const MIN_SCREEN_DOT_R = 5; // on-screen px at view.scale = 1 (max zoom-out)
  const MAX_SCREEN_DOT_R = 11; // on-screen px at view.scale = MAX_ZOOM (max zoom-in) -- a real cap, not unbounded growth
  const zoomFrac = Math.min(1, Math.max(0, (view.scale - 1) / (MAX_ZOOM - 1)));
  const targetScreenR = MIN_SCREEN_DOT_R + (MAX_SCREEN_DOT_R - MIN_SCREEN_DOT_R) * zoomFrac;
  const dotCounterScale = targetScreenR / DOT_R / view.scale;

  // Real per-axis correction for the preserveAspectRatio="none" oval
  // bug (see the containerAspect measurement above) -- 1 until the
  // container's been measured at least once, so dots render as their
  // normal (possibly still slightly off) shape for one frame at mount
  // rather than not rendering at all while waiting on the
  // ResizeObserver's first callback.
  const viewBoxAspect = HEIGHT / WIDTH;
  const dotYCorrection = containerAspect ? viewBoxAspect / containerAspect : 1;

  return (
    <div className="col-span-12 lg:col-span-8 bg-surface-container-low border border-outline-variant relative flex flex-col p-8 overflow-hidden">
      {/* Color key only -- these no longer indicate vertical position.
          Y is now class-row (which class this episode belongs to) with
          a recency quartile within each row (newest at the top of a
          row, oldest at the bottom); correct/incorrect is shown purely
          via dot color, same green/red as before. */}
      <div className="absolute left-8 top-8 flex items-center gap-4 z-10">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-correct-green" />
          <span className="font-label-caps text-[11px] text-correct-green">CORRECT</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-error-red" />
          <span className="font-label-caps text-[11px] text-error-red">INCORRECT</span>
        </div>
      </div>
      <div className="absolute right-8 top-8 z-10 text-right">
        <span className="font-label-caps text-[10px] text-on-surface-variant tracking-widest">
          ↑ NEWEST&nbsp;&nbsp;↓ OLDEST (per class row)
        </span>
      </div>

      <div className="flex mb-10 ml-4 mt-8" style={{ height: "60vh" }}>
        {/* Y-axis class labels -- real HTML, positioned by screen-space
            fraction of the plot's own height, mirroring exactly how
            the X-axis tick labels below are positioned by screen-space
            fraction of width. This is the fix for panning/zooming
            shifting or hiding the row labels: SVG text drawn inside
            the pan/zoom <g> (the earlier, broken approach) moves and
            re-clips with the transform; real HTML positioned from
            view.ty/view.scale/HEIGHT the same way the X labels are
            positioned from view.tx/view.scale/WIDTH does not have that
            problem, since it's laid out independently of the SVG's own
            internal coordinate system. Row labels that pan outside the
            visible [0, 100]% vertical range are simply not rendered
            (same principle as computeVisibleTicks already applies on
            the X axis -- only show what's actually in view). */}
        <div className="relative shrink-0 overflow-hidden" style={{ width: "168px", height: "100%" }}>
          {classGroups.map((g, rowIdx) => {
            // Screen-space fraction of the plot's height that this
            // row's label falls at -- same formula shape as
            // screenXPct below, just Y instead of X and HEIGHT
            // instead of WIDTH (transform is
            // scale(view.scale) translate(view.tx, view.ty), so
            // screenY_plotUnits = (plotY + ty) * scale).
            const labelPlotY = rowTop[rowIdx] + 9; // small offset, matches the old in-SVG text's own y+9 baseline nudge
            const screenYPct = (((labelPlotY + view.ty) * view.scale) / HEIGHT) * 100;
            if (screenYPct < -5 || screenYPct > 105) return null; // off-screen, don't render
            return (
              <span
                key={`ylabel-${g.cls}`}
                className="absolute left-0 right-2 font-label-caps text-[10px] leading-tight text-on-surface-variant"
                style={{ top: `${screenYPct}%`, transform: "translateY(-50%)" }}
              >
                {g.cls.toUpperCase()} ({g.total})
              </span>
            );
          })}
        </div>

        <div
          ref={containerRef}
          className="flex-1 min-w-0 border-l border-b border-outline relative overflow-hidden cursor-grab active:cursor-grabbing"
          style={{ touchAction: "none" }}
        >
        <svg
          ref={svgRef}
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          preserveAspectRatio="none"
          className="w-full h-full"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onClick={onBackgroundClick}
        >
          <g transform={transformStr}>
            {/* Gridlines -- fixed SVG units, transform handles pan/zoom
                automatically since they're inside the same <g>. */}
            {visibleTicks.map((tick) => (
              <line
                key={`grid-${tick.value}`}
                x1={tick.plotX}
                y1={0}
                x2={tick.plotX}
                y2={HEIGHT}
                stroke="var(--color-on-surface-variant)"
                strokeOpacity={0.35}
                strokeWidth={1 / view.scale}
              />
            ))}

            {/* Horizontal separators between class-rows -- real chart
                gridlines, same treatment as the X-axis gridlines above
                (live inside the pan/zoom transform, so they move with
                the dots exactly like every other real gridline). Class
                NAME labels are NOT drawn here -- SVG text inside this
                transformed <g> is exactly what caused the earlier bug
                where panning/zooming shifted/hid the row labels; those
                are now rendered as real HTML outside the SVG entirely,
                same fix already applied to the X-axis tick labels. */}
            {classGroups.map((g, rowIdx) =>
              rowIdx > 0 ? (
                <line
                  key={`rowsep-${g.cls}`}
                  x1={0}
                  y1={rowTop[rowIdx] - bandGap / 2}
                  x2={WIDTH}
                  y2={rowTop[rowIdx] - bandGap / 2}
                  stroke="var(--color-on-surface-variant)"
                  strokeOpacity={0.45}
                  strokeWidth={1 / view.scale}
                />
              ) : null
            )}

            {points.map((p) => {
              const ep = p.ep;
              const isHallucination = !ep.correct && ep.score_confidence >= HALLUCINATION_CONFIDENCE_THRESHOLD;
              const isSelected = ep.episode_id === selectedId;
              const r = (isHallucination ? HALLUCINATION_DOT_R : DOT_R) * dotCounterScale;
              // ry is deliberately different from rx (r) -- this is the
              // actual fix for the oval-dot bug, not a workaround.
              // dotYCorrection counteracts the real, measured mismatch
              // between the viewBox's intrinsic aspect ratio and the
              // container's real rendered aspect ratio (both change
              // independently: viewBox's HEIGHT is data-dependent,
              // container's rendered size depends on layout/window
              // size), which preserveAspectRatio="none" otherwise
              // stretches non-uniformly into dots. rx stays exactly r
              // (SVG x-units == screen px 1:1 modulo the shared
              // view.scale zoom, no separate x-correction needed) --
              // only y needs the extra factor.
              const ry = r * dotYCorrection;

              // Re-clicking the currently-selected dot deselects it
              // (toggle), rather than just re-selecting the same
              // episode -- this is the "click a selected dot again"
              // deselect path.
              const handleDotClick = (e) => {
                e.stopPropagation();
                onSelect(isSelected ? null : ep);
              };

              return (
                <g key={ep.episode_id}>
                  {/* Larger invisible hit-target, only rendered once
                      dots are far enough apart on screen that adjacent
                      hit-circles can't overlap (see HIT_TARGET_MIN_ZOOM
                      above) -- below that zoom, dots are close/dense
                      enough that a bigger click radius would just make
                      mis-clicks worse, so it's left off entirely and
                      the real visible dot (below) is the only click
                      target. */}
                  {view.scale >= HIT_TARGET_MIN_ZOOM && (
                    <ellipse
                      cx={p.x}
                      cy={p.y}
                      rx={HIT_R * dotCounterScale}
                      ry={HIT_R * dotCounterScale * dotYCorrection}
                      fill="transparent"
                      className="cursor-pointer"
                      onPointerDown={(e) => e.stopPropagation()}
                      onClick={handleDotClick}
                      role="button"
                      aria-label={`Episode ${ep.episode_id.slice(0, 8)} (hit area)`}
                    />
                  )}
                  <ellipse
                    cx={p.x}
                    cy={p.y}
                    rx={r}
                    ry={ry}
                    className="cursor-pointer transition-[rx,ry] pointer-events-none"
                    fill={ep.correct ? "var(--color-correct-green)" : "var(--color-error-red)"}
                    stroke={isSelected ? "var(--color-primary)" : isHallucination ? "white" : "none"}
                    strokeOpacity={isSelected ? 1 : isHallucination ? 0.2 : 0}
                    strokeWidth={(isSelected ? 2 : 2) / view.scale}
                    aria-label={`Episode ${ep.episode_id.slice(0, 8)}`}
                  >
                    {isHallucination && <title>CRITICAL: HIGH CONFIDENCE ERROR</title>}
                  </ellipse>
                  {/* Below HIT_TARGET_MIN_ZOOM, the visible dot itself
                      is still the real click target (hit-circle above
                      is absent, so this needs its own handlers and
                      real pointer-events, not pointer-events-none). */}
                  {view.scale < HIT_TARGET_MIN_ZOOM && (
                    <ellipse
                      cx={p.x}
                      cy={p.y}
                      rx={r}
                      ry={ry}
                      fill="transparent"
                      className="cursor-pointer"
                      onPointerDown={(e) => e.stopPropagation()}
                      onClick={handleDotClick}
                      role="button"
                      aria-label={`Episode ${ep.episode_id.slice(0, 8)} (click target)`}
                    />
                  )}
                </g>
              );
            })}
          </g>
        </svg>
        </div>
      </div>

      {/* Tick labels -- screen-space positioned as a % of the viewport
          width (matching the SVG's own preserveAspectRatio stretch),
          so they line up with the gridlines above without needing any
          JS-measured pixel width. Offset by the same 168px Y-label
          gutter width as the plot box itself above (ml-4 matches the
          plot box's own ml-4, then the 168px gutter + its own ml-4
          are added via the wrapper below) so the tick marks land
          under the real gridlines, not under the old flat ml-4 alone. */}
      <div className="relative h-4" style={{ marginLeft: "calc(1rem + 168px)" }}>
        {visibleTicks.map((tick) => {
          // Screen-space fraction of the viewport width that this tick
          // falls at, accounting for the current pan/zoom transform:
          // screenX_plotUnits = (plotX + tx) * scale, then normalize by
          // WIDTH (since viewBox width == WIDTH, and the SVG element's
          // own CSS width maps 0..WIDTH to 0..100%).
          const screenXPct = (((tick.plotX + view.tx) * view.scale) / WIDTH) * 100;
          return (
            <span
              key={`label-${tick.value}`}
              className="absolute font-label-caps text-[11px] text-on-surface-variant -translate-x-1/2"
              style={{ left: `${screenXPct}%` }}
            >
              {tick.value.toFixed(tick.decimals)}
            </span>
          );
        })}
        <div className="absolute -bottom-6 left-1/2 -translate-x-1/2">
          <span className="font-label-caps text-[11px] text-on-surface-variant tracking-widest">
            SELF-REPORTED CONFIDENCE
          </span>
        </div>
      </div>
    </div>
  );
}
