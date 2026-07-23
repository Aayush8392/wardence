import { useEffect, useMemo, useRef, useState } from "react";
import { TransformWrapper, TransformComponent } from "react-zoom-pan-pinch";

// Same threshold as the rest of the project's confidence-calibration
// framing -- high confidence AND wrong is the specific thing worth
// visually calling out (the "hallucination" case).
const HALLUCINATION_CONFIDENCE_THRESHOLD = 0.8;

const DOT_RADIUS = 8; // px -- matches the ~16px dot diameter used below

// Monitor-relative viewport height (not a px guess) that the zoomable
// canvas renders inside.
const VIEWPORT_VH = 60;

// Deliberately tiny -- with zoom available, dots no longer need real
// screen-space separation at 1x (that's what "cluttered by default" means);
// they only need a GENUINELY DISTINCT coordinate so that zooming in later
// can turn a small real gap into a large, clickable one. Max zoom (below)
// is chosen so this minimum gap becomes comfortably clickable once fully
// zoomed in: DOT_RADIUS*2 (16px, the click target) / MIN_STEP (2px) = 8x
// to just touch; MAX_ZOOM is set well above that for margin.
const MIN_STEP = 2;
const MAX_ZOOM = 24;

// Beeswarm-style layout: x is the REAL confidence value (never adjusted).
// Dots sharing (near-)identical confidence are grouped and spread
// symmetrically around the band's center line, MIN_STEP apart -- a
// deterministic placement (no collision-detection loop, no attempt cap,
// no silent clamping/stacking at high density like the previous version
// had). Every dot in a group of size N gets a genuinely unique y within
// N * MIN_STEP px, which for realistic episode counts fits comfortably
// inside a band even at moderate canvas heights -- there is no longer any
// need for the canvas itself to be physically huge; zoom does the
// decluttering, not the canvas's raw size.
function computeSwarmPositions(episodes, plotWidth, plotHeight) {
  const positions = {};
  if (!plotWidth || !plotHeight) return positions;

  const bands = {
    correct: { items: [], top: 0, bottom: plotHeight / 2, baseY: plotHeight * 0.25 },
    incorrect: { items: [], top: plotHeight / 2, bottom: plotHeight, baseY: plotHeight * 0.75 },
  };
  episodes.forEach((ep) => bands[ep.correct ? "correct" : "incorrect"].items.push(ep));

  for (const band of Object.values(bands)) {
    const sorted = [...band.items].sort((a, b) => a.score_confidence - b.score_confidence);

    // Group dots whose x lands within 1px of each other -- those are the
    // ones that actually need separating; anything further apart in x is
    // already visually distinct without any y offset.
    const groups = [];
    for (const ep of sorted) {
      const x = ep.score_confidence * plotWidth;
      const last = groups[groups.length - 1];
      if (last && Math.abs(last.x - x) < 1) {
        last.items.push(ep);
      } else {
        groups.push({ x, items: [ep] });
      }
    }

    for (const group of groups) {
      const n = group.items.length;
      group.items.forEach((ep, i) => {
        const offset = (i - (n - 1) / 2) * MIN_STEP;
        const y = Math.min(Math.max(band.baseY + offset, band.top + DOT_RADIUS), band.bottom - DOT_RADIUS);
        positions[ep.episode_id] = { x: group.x, y };
      });
    }
  }

  return positions;
}

export default function ScatterPlot({ episodes, selectedId, onSelect }) {
  // Measures the fixed VIEWPORT (not the canvas -- the canvas's own size
  // is data-derived below, independent of anything measured here).
  const viewportRef = useRef(null);
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    if (!viewportRef.current) return;
    const el = viewportRef.current;
    const observer = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      setViewportSize({ width, height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Real, data-derived canvas height -- but based on MIN_STEP (2px), not
  // the old collision-avoidance step (12.8px). At realistic episode counts
  // this stays close to the viewport's own size (a band with ~150 same-
  // confidence dots needs only ~150*MIN_STEP+16 =~316px), so fitScale
  // below stays near 1 instead of collapsing toward ~0.03 like the earlier
  // (much larger step) version did -- that's what let it crush the width.
  // Without this, a band with more dots than (bandHeight/MIN_STEP) forces
  // the excess onto genuinely IDENTICAL coordinates (not just close), which
  // no zoom level can ever separate -- confirmed as the actual cause of
  // the solid-blob-at-max-zoom result.
  const maxBandCount = useMemo(() => {
    let correct = 0;
    let incorrect = 0;
    for (const ep of episodes) (ep.correct ? correct++ : incorrect++);
    return Math.max(correct, incorrect, 1);
  }, [episodes]);
  const canvasWidth = viewportSize.width;
  const canvasHeight = Math.max(viewportSize.height, maxBandCount * MIN_STEP * 2 + 32);

  const positions = useMemo(
    () => computeSwarmPositions(episodes, canvasWidth, canvasHeight),
    [episodes, canvasWidth, canvasHeight]
  );

  // Fit the (now modest) canvas into the viewport by default -- close to
  // 1x at realistic data volumes, not the near-zero crush from before.
  const fitScale =
    viewportSize.height > 0 && canvasHeight > 0 ? Math.min(1, viewportSize.height / canvasHeight) : 1;

  // Live zoom level, synced from the library's own transform state. A
  // uniform CSS scale() on the canvas scales a dot's SIZE by the exact
  // same factor as the GAP between dots -- overlap is a ratio, and uniform
  // scaling can't change a ratio, so zoom alone can never resolve overlap
  // (confirmed: dots got MORE overlapped when zoomed in, not less). The
  // fix is to counter-scale each dot by 1/currentScale so its ON-SCREEN
  // size stays constant while the real gap between dots keeps growing with
  // zoom -- only then does zooming in actually separate them.
  const [currentScale, setCurrentScale] = useState(fitScale);

  return (
    <div className="col-span-12 lg:col-span-8 bg-surface-container-low border border-outline-variant relative flex flex-col p-8 overflow-hidden">
      <div className="absolute left-8 top-8 flex items-center gap-2 z-10">
        <span className="w-2 h-2 rounded-full bg-correct-green" />
        <span className="font-label-caps text-[11px] text-correct-green">CORRECT</span>
      </div>
      <div className="absolute left-8 bottom-16 flex items-center gap-2 z-10">
        <span className="w-2 h-2 rounded-full bg-error-red" />
        <span className="font-label-caps text-[11px] text-error-red">INCORRECT</span>
      </div>

      {/* No flex-1 here on purpose -- Tailwind's flex-1 resolves to
          flex-basis: 0%, which overrides an explicit height in a flex
          column and lets the item grow to its own content size instead
          (exactly the "still tall" bug: the fixed vh height was being
          silently ignored). This div needs a real fixed height, not
          "fill available space." */}
      <div
        ref={viewportRef}
        className="shrink-0 border-l border-b border-outline relative scatter-grid mb-10 ml-4 mt-8 overflow-hidden"
        style={{ height: `${VIEWPORT_VH}vh` }}
      >
        {canvasWidth > 0 && (
          // key forces a clean remount (zoom/pan reset to fit) whenever the
          // real canvas dimensions change -- e.g. switching the fault-class
          // filter changes the episode count, so an old zoom/pan state
          // computed against the previous canvas size would be meaningless.
          <TransformWrapper
            key={`${canvasWidth}x${canvasHeight}`}
            initialScale={fitScale}
            minScale={fitScale}
            maxScale={MAX_ZOOM}
            centerOnInit
            limitToBounds
            doubleClick={{ disabled: true }}
            onTransform={(_ref, state) => setCurrentScale(state.scale)}
          >
            <TransformComponent
              wrapperStyle={{ width: "100%", height: "100%" }}
              contentStyle={{ width: canvasWidth, height: canvasHeight }}
            >
              <div style={{ width: canvasWidth, height: canvasHeight, position: "relative" }}>
                {/* Hallucination zone: confidence >= threshold AND the
                    incorrect (bottom) half -- matches the real
                    HALLUCINATION_CONFIDENCE_THRESHOLD, not an arbitrary
                    decorative box. */}
                <div
                  className="absolute right-0 bottom-0 h-1/2 bg-error-red/5 border-l border-t border-error-red/20 pointer-events-none"
                  style={{ width: `${(1 - HALLUCINATION_CONFIDENCE_THRESHOLD) * 100}%` }}
                >
                  <div className="absolute top-1 right-1 opacity-40">
                    <span className="font-label-caps text-[9px] text-error-red">HALLUCINATION ZONE</span>
                  </div>
                </div>

                {episodes.map((ep) => {
                  const pos = positions[ep.episode_id];
                  if (!pos) return null;
                  const isHallucination = !ep.correct && ep.score_confidence >= HALLUCINATION_CONFIDENCE_THRESHOLD;
                  const isSelected = ep.episode_id === selectedId;

                  return (
                    <div
                      key={ep.episode_id}
                      className="absolute group/point"
                      style={{ left: pos.x, top: pos.y, transform: "translate(-50%, -50%)" }}
                    >
                      {/* Counter-scale: cancels the ancestor canvas's zoom
                          so this dot's rendered size stays constant while
                          its real (x, y) position still moves with zoom/pan
                          like everything else -- this is what makes zoom
                          actually separate dots instead of just enlarging
                          the same overlap ratio. */}
                      <div style={{ transform: `scale(${1 / currentScale})` }}>
                        <button
                          onClick={() => onSelect(ep)}
                          aria-label={`Episode ${ep.episode_id.slice(0, 8)}`}
                          className={`rounded-full cursor-pointer hover:scale-125 transition-transform ${
                            isHallucination
                              ? "w-4 h-4 bg-error-red ring-2 ring-white/20 border-2 border-error-red point-glow"
                              : `w-3 h-3 ${ep.correct ? "bg-correct-green" : "bg-error-red"}`
                          } ${isSelected ? "ring-2 ring-primary" : ""}`}
                        />
                        {isHallucination && (
                          <div className="absolute -top-9 left-1/2 -translate-x-1/2 bg-surface-container-highest px-2 py-1 border border-outline hidden group-hover/point:block whitespace-nowrap z-20">
                            <span className="font-data-mono text-[10px] text-error-red font-bold">
                              CRITICAL: HIGH CONFIDENCE ERROR
                            </span>
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </TransformComponent>
          </TransformWrapper>
        )}
      </div>

      <div className="ml-4 flex justify-between relative">
        {["0.0", "0.2", "0.4", "0.6", "0.8", "1.0"].map((t) => (
          <span key={t} className="font-label-caps text-[11px] text-on-surface-variant">{t}</span>
        ))}
        <div className="absolute -bottom-6 left-1/2 -translate-x-1/2">
          <span className="font-label-caps text-[11px] text-on-surface-variant tracking-widest">
            SELF-REPORTED CONFIDENCE
          </span>
        </div>
      </div>
    </div>
  );
}
