import { useEffect, useMemo, useRef, useState } from "react";

// Same threshold as the rest of the project's confidence-calibration
// framing -- high confidence AND wrong is the specific thing worth
// visually calling out (the "hallucination" case).
const HALLUCINATION_CONFIDENCE_THRESHOLD = 0.8;

const DOT_RADIUS = 8; // px -- matches the ~16px dot diameter used below

// Beeswarm-style layout: x is the REAL confidence value (never adjusted),
// y is purely for visual separation within the correct/incorrect band --
// points that would overlap in x get nudged apart in y instead of relying
// on random jitter, so every point stays individually clickable rather
// than merging into a blob at small-to-medium point counts.
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
    const placed = [];
    for (const ep of sorted) {
      const x = ep.score_confidence * plotWidth;
      let y = band.baseY;
      let attempt = 0;
      const step = DOT_RADIUS * 1.6;
      while (placed.some((p) => Math.hypot(p.x - x, p.y - y) < DOT_RADIUS * 2) && attempt < 60) {
        attempt++;
        const dir = attempt % 2 === 0 ? 1 : -1;
        const mag = Math.ceil(attempt / 2) * step;
        y = Math.min(Math.max(band.baseY + dir * mag, band.top + DOT_RADIUS), band.bottom - DOT_RADIUS);
      }
      placed.push({ x, y });
      positions[ep.episode_id] = { x, y };
    }
  }

  return positions;
}

export default function ScatterPlot({ episodes, selectedId, onSelect }) {
  const plotRef = useRef(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    if (!plotRef.current) return;
    const el = plotRef.current;
    const observer = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      setSize({ width, height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const positions = useMemo(
    () => computeSwarmPositions(episodes, size.width, size.height),
    [episodes, size.width, size.height]
  );

  return (
    <div className="col-span-12 lg:col-span-8 bg-surface-container-low border border-outline-variant relative flex flex-col p-8 overflow-hidden min-h-[420px]">
      <div className="absolute left-8 top-8 flex items-center gap-2 z-10">
        <span className="w-2 h-2 rounded-full bg-correct-green" />
        <span className="font-label-caps text-[11px] text-correct-green">CORRECT</span>
      </div>
      <div className="absolute left-8 bottom-16 flex items-center gap-2 z-10">
        <span className="w-2 h-2 rounded-full bg-error-red" />
        <span className="font-label-caps text-[11px] text-error-red">INCORRECT</span>
      </div>

      <div ref={plotRef} className="flex-1 border-l border-b border-outline relative scatter-grid mb-10 ml-4 mt-8">
        {/* Hallucination zone: confidence >= threshold AND the incorrect
            (bottom) half -- matches the real HALLUCINATION_CONFIDENCE_THRESHOLD,
            not an arbitrary decorative box. */}
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
          );
        })}
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
