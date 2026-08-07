import { useEffect, useMemo, useRef, useState, useCallback } from "react";

// Same threshold as the rest of the project's confidence-calibration
// framing -- high confidence AND wrong is the specific thing worth
// visually calling out (the "hallucination" case).
const HALLUCINATION_CONFIDENCE_THRESHOLD = 0.8;

const DOT_RADIUS = 8; // px -- matches the ~16px dot diameter used below

// Monitor-relative viewport height (not a px guess) that the zoomable
// canvas renders inside.
const VIEWPORT_VH = 60;

// Deliberately tiny -- with zoom available, dots no longer need real
// screen-space separation at 1x; they only need a genuinely distinct
// coordinate so zooming in can turn a small real gap into a large,
// clickable one.
const MIN_STEP = 2;
const MAX_ZOOM = 24;

// Fixed LOGICAL canvas width, never the live viewport width. The
// previous version fed the ResizeObserver's viewport width directly
// into `x = confidence * plotWidth`, so any resize noise (scrollbar
// showing/hiding, a font/layout settle pass, the panel's own mount
// animation) silently RESHUFFLED every dot's x position -- confirmed:
// two screenshots of the same click, seconds apart, showed the exact
// same cluster shape sitting at a different x. Geometry must be
// computed once against a stable reference and never move again except
// via the user's own zoom/pan; only fitScale (how that fixed canvas
// maps onto whatever the real viewport happens to be) should react to
// viewport size.
const CANVAS_LOGICAL_WIDTH = 1000;

// Custom zoom/pan replaced react-zoom-pan-pinch (2026-08-xx) -- the
// library's wheel step semantics produced a huge jump on a single
// wheel tick with no usable in-between, and its own pan updates were
// hard to reason about. This is a small, fully-owned implementation:
// wheel = exponential zoom centered on the cursor (each notch is a
// fixed, predictable ~15% scale change, not a fixed absolute step),
// drag = pan. No animation/easing needed -- multiplicative zoom
// already feels continuous as the wheel fires repeatedly.
const WHEEL_SENSITIVITY = 0.0015;

// Beeswarm-style layout: x is the REAL confidence value (never adjusted).
// Dots sharing (near-)identical confidence are grouped and spread
// symmetrically around the band's center line. Step is COMPRESSED, never
// clamped, so an oversized group never collapses two dots onto the same
// coordinate (that was silently making the top one eat every click for
// the rest of the stack).
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
      const availableSpan = band.bottom - band.top - DOT_RADIUS * 2;
      const step = n > 1 ? Math.min(MIN_STEP, availableSpan / (n - 1)) : MIN_STEP;
      group.items.forEach((ep, i) => {
        const offset = (i - (n - 1) / 2) * step;
        const y = Math.min(Math.max(band.baseY + offset, band.top + DOT_RADIUS), band.bottom - DOT_RADIUS);
        positions[ep.episode_id] = { x: group.x, y };
      });
    }
  }

  return positions;
}

export default function ScatterPlot({ episodes, selectedId, onSelect }) {
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

  const maxBandCount = useMemo(() => {
    let correct = 0;
    let incorrect = 0;
    for (const ep of episodes) (ep.correct ? correct++ : incorrect++);
    return Math.max(correct, incorrect, 1);
  }, [episodes]);
  const canvasWidth = CANVAS_LOGICAL_WIDTH;
  const canvasHeight = maxBandCount * MIN_STEP * 2 + 32;

  const positions = useMemo(
    () => computeSwarmPositions(episodes, canvasWidth, canvasHeight),
    [episodes, canvasWidth, canvasHeight]
  );

  const fitScale =
    viewportSize.width > 0 && viewportSize.height > 0
      ? Math.min(1, viewportSize.width / canvasWidth, viewportSize.height / canvasHeight)
      : 1;

  // { scale, x, y } -- x/y are the CSS translate of the content div, in
  // viewport px, applied BEFORE scale (transform-origin is top-left).
  const [transform, setTransform] = useState({ scale: 1, x: 0, y: 0 });
  const dragState = useRef(null);

  // Reset to a centered fit only when the real DATA changes (a new
  // episode set -- different model/class selected) or canvasHeight
  // (which is itself purely data-derived, not viewport-derived).
  // Deliberately NOT keyed on viewportSize -- that was what let resize
  // noise silently recenter/reshuffle the view out from under the user.
  const episodeSignature = episodes.map((e) => e.episode_id).join(",");

  useEffect(() => {
    if (!viewportSize.width || !viewportSize.height) return;
    const scale = Math.min(1, viewportSize.width / canvasWidth, viewportSize.height / canvasHeight);
    const x = (viewportSize.width - canvasWidth * scale) / 2;
    const y = (viewportSize.height - canvasHeight * scale) / 2;
    setTransform({ scale, x, y });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeSignature, canvasHeight, viewportSize.width > 0 && viewportSize.height > 0]);

  // React attaches wheel listeners as PASSIVE by default (since v17) --
  // e.preventDefault() inside a React onWheel handler is silently a
  // no-op, which is exactly why the page itself was scrolling: the
  // browser's native scroll ran unprevented at the same time our state
  // update did, and the resulting layout shift under the cursor is what
  // made panning look uncontrolled and clicks miss their target. Fixed
  // with a real native, non-passive listener attached directly to the
  // DOM node.
  const fitScaleRef = useRef(fitScale);
  fitScaleRef.current = fitScale;

  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;

    const handleWheel = (e) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const cursorX = e.clientX - rect.left;
      const cursorY = e.clientY - rect.top;

      setTransform((prev) => {
        const factor = Math.exp(-e.deltaY * WHEEL_SENSITIVITY);
        const newScale = Math.min(Math.max(prev.scale * factor, fitScaleRef.current), MAX_ZOOM);
        // Keep the point under the cursor fixed on screen.
        const contentX = (cursorX - prev.x) / prev.scale;
        const contentY = (cursorY - prev.y) / prev.scale;
        return {
          scale: newScale,
          x: cursorX - contentX * newScale,
          y: cursorY - contentY * newScale,
        };
      });
    };

    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, []);

  const onPointerDown = useCallback((e) => {
    dragState.current = { startX: e.clientX, startY: e.clientY, origX: transform.x, origY: transform.y, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  }, [transform.x, transform.y]);

  const onPointerMove = useCallback((e) => {
    const drag = dragState.current;
    if (!drag) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true;
    if (drag.moved) {
      setTransform((prev) => ({ ...prev, x: drag.origX + dx, y: drag.origY + dy }));
    }
  }, []);

  const onPointerUp = useCallback((e) => {
    dragState.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
  }, []);

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

      <div
        ref={viewportRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        className="shrink-0 border-l border-b border-outline relative scatter-grid mb-10 ml-4 mt-8 overflow-hidden cursor-grab active:cursor-grabbing"
        style={{ height: `${VIEWPORT_VH}vh`, touchAction: "none" }}
      >
        {canvasWidth > 0 && (
          <div
            style={{
              position: "absolute",
              left: 0,
              top: 0,
              width: canvasWidth,
              height: canvasHeight,
              transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
              transformOrigin: "0 0",
            }}
          >
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
                  <div style={{ transform: `scale(${1 / transform.scale})` }}>
                    <button
                      onPointerDown={(e) => e.stopPropagation()}
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
