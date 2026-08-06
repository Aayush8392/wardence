import { useMemo, useRef, useState } from "react";
import {
  modelLabel,
  visibleModelKeys,
  realClassesFromConfusionMatrix,
  classAccuracy,
  classColor,
  modelShape,
} from "../../utils/modelScorecard";

// Real Kimi review 27 recommendation (item on "Efficiency"): "four
// points is too thin for a standalone section... plot all 48 (class x
// model) points instead of 4 model aggregates." Real per-(model,class)
// latency comes from the backend's avg_response_time_by_class (added
// specifically for this); real per-(model,class) accuracy is derived
// client-side from confusion_matrix (no new backend field needed --
// same diagonal-over-row-total arithmetic as build_overall_accuracy,
// just per class instead of summed across all of them).
//
// Encoding: COLOR = fault class (12 real colors), SHAPE = model (4
// shapes) -- flipped from an earlier color-per-model version per
// explicit request, since with 12 classes and only 4 models, color is
// the scarcer/more legible channel and deserves the higher-cardinality
// role. Class-color legend was dropped (12 swatches was noise) -- the
// hover tooltip already names the class directly, so the legend only
// needs to explain the 4 shapes.
const MARGIN = { top: 20, right: 20, bottom: 44, left: 48 };

// Fixed internal viewBox dims, not a real measured pixel height --
// `preserveAspectRatio="none"` on the <svg> below stretches this to
// fill whatever height CSS grid gives the component (matching the
// Fallback Funnel + Action/Durability column's real stacked height via
// plain grid item-stretch, the default), so the same recurring
// ResizeObserver-measurement bug class already hit elsewhere in this
// project (Replay Viewer, Calibration's old ScatterPlot) never comes
// up here -- no JS measurement needed at all.
const WIDTH = 640;
const HEIGHT = 500;

// Real, verified before hardcoding (not guessed): across all 48
// (model, class) cohorts in the current live data, the lowest real
// accuracy is Gemma/network-latency at ~77.4% -- nothing sits below
// 70%. Starting the axis there (instead of 50%) uses the real observed
// range instead of wasting half the chart on values that never occur.
// If a future cohort's real accuracy ever drops below 70%, this will
// clip it visually at the axis floor -- revisit this constant if that
// happens, don't silently let a real low score get clipped off-chart.
const ACC_AXIS_MIN = 0.7;
const ACC_AXIS_MAX = 1.0;

// Collision-avoidance: points whose raw (x, y) round to the same small
// bucket get nudged apart in a small deterministic ring -- same real
// need as ScatterPlot.jsx's beeswarm grouping elsewhere in this
// project (dots sharing near-identical values need SOME separation to
// stay individually clickable), just a simpler ring-offset instead of
// a full swarm layout since this chart has far fewer points (48 vs.
// hundreds of episodes).
const COLLISION_BUCKET_PX = 10;
const NUDGE_RADIUS_PX = 7;

function nudgeOverlaps(rawPoints) {
  const buckets = new Map();
  for (const p of rawPoints) {
    const key = `${Math.round(p.rawX / COLLISION_BUCKET_PX)}:${Math.round(p.rawY / COLLISION_BUCKET_PX)}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(p);
  }
  const out = [];
  for (const group of buckets.values()) {
    if (group.length === 1) {
      out.push({ ...group[0], x: group[0].rawX, y: group[0].rawY });
      continue;
    }
    group.forEach((p, i) => {
      const angle = (2 * Math.PI * i) / group.length;
      out.push({
        ...p,
        x: p.rawX + Math.cos(angle) * NUDGE_RADIUS_PX,
        y: p.rawY + Math.sin(angle) * NUDGE_RADIUS_PX,
      });
    });
  }
  return out;
}

function ShapeMarker({ shape, cx, cy, size, fill, opacity, onMouseEnter, onMouseMove, onMouseLeave }) {
  const props = { fill, opacity, style: { cursor: "pointer" }, onMouseEnter, onMouseMove, onMouseLeave };
  if (shape === "square") {
    return <rect x={cx - size} y={cy - size} width={size * 2} height={size * 2} {...props} />;
  }
  if (shape === "triangle") {
    const h = size * 1.3;
    return (
      <polygon
        points={`${cx},${cy - h} ${cx - h},${cy + h * 0.75} ${cx + h},${cy + h * 0.75}`}
        {...props}
      />
    );
  }
  if (shape === "diamond") {
    const d = size * 1.3;
    return <polygon points={`${cx},${cy - d} ${cx + d},${cy} ${cx},${cy + d} ${cx - d},${cy}`} {...props} />;
  }
  return <circle cx={cx} cy={cy} r={size} {...props} />;
}

export default function EfficiencyFrontier({ scorecard }) {
  const [hovered, setHovered] = useState(null);
  const [tooltipPos, setTooltipPos] = useState({ x: 0, y: 0 });
  const containerRef = useRef(null);
  const width = WIDTH;
  const height = HEIGHT;

  const modelKeys = useMemo(
    () => visibleModelKeys(scorecard.avg_response_time_by_class),
    [scorecard.avg_response_time_by_class]
  );
  const realClasses = useMemo(
    () => realClassesFromConfusionMatrix(scorecard.confusion_matrix),
    [scorecard.confusion_matrix]
  );

  const rawPoints = useMemo(() => {
    const out = [];
    modelKeys.forEach((modelKey) => {
      const byClass = scorecard.avg_response_time_by_class[modelKey] ?? {};
      realClasses.forEach((cls) => {
        const rt = byClass[cls];
        if (!rt) return;
        const acc = classAccuracy(scorecard.confusion_matrix, modelKey, cls);
        if (acc.accuracy == null) return;
        out.push({
          key: `${modelKey}::${cls}`,
          modelKey,
          cls,
          rt: rt.avg_response_time_ms,
          n: rt.n,
          accuracy: acc.accuracy,
          correct: acc.correct,
          total: acc.total,
          color: classColor(realClasses, cls),
          shape: modelShape(modelKey),
        });
      });
    });
    return out;
  }, [modelKeys, realClasses, scorecard]);

  if (rawPoints.length === 0) {
    return (
      <div className="bg-surface-container-low border border-outline-variant p-5">
        <span className="font-label-caps text-[11px] text-on-surface-variant">EFFICIENCY_FRONTIER</span>
        <p className="text-sm text-on-surface-variant mt-2">No real per-class response-time data yet.</p>
      </div>
    );
  }

  const rtValues = rawPoints.map((p) => p.rt);
  const logMin = Math.log10(Math.max(Math.min(...rtValues) * 0.7, 1));
  const logMax = Math.log10(Math.max(...rtValues) * 1.3);

  const plotW = width - MARGIN.left - MARGIN.right;
  const plotH = height - MARGIN.top - MARGIN.bottom;

  const xPos = (rt) => MARGIN.left + ((Math.log10(rt) - logMin) / (logMax - logMin)) * plotW;
  const yPos = (acc) =>
    MARGIN.top +
    plotH -
    ((Math.min(Math.max(acc, ACC_AXIS_MIN), ACC_AXIS_MAX) - ACC_AXIS_MIN) / (ACC_AXIS_MAX - ACC_AXIS_MIN)) * plotH;

  // Raw (unnudged) positions computed first, THEN grouped/nudged -- the
  // nudge only ever moves a point a few px for display, the tooltip
  // still reports the real underlying values, never the nudged ones.
  const points = nudgeOverlaps(rawPoints.map((p) => ({ ...p, rawX: xPos(p.rt), rawY: yPos(p.accuracy) })));

  const xTicks = [200, 1000, 5000, 20000, 50000].filter((t) => t >= 10 ** logMin && t <= 10 ** logMax);
  const yTicks = [0.7, 0.8, 0.9, 1.0];
  const hoveredPoint = points.find((p) => p.key === hovered);

  // Tooltip follows the cursor (container-relative px), clamped so it
  // never runs off the panel edge -- replaces the old fixed top-left
  // corner, which sat right on top of the densest cluster of real
  // points (most cohorts score 90-100%, near the chart's top).
  const handlePointMove = (e) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = Math.min(Math.max(e.clientX - rect.left + 12, 0), rect.width - 150);
    const y = Math.min(Math.max(e.clientY - rect.top + 12, 0), rect.height - 60);
    setTooltipPos({ x, y });
  };

  return (
    <div className="bg-surface-container-low border border-outline-variant p-5 flex flex-col h-full">
      <div className="flex items-center justify-between mb-3 shrink-0 flex-wrap gap-2">
        <span className="font-label-caps text-[11px] text-on-surface-variant">EFFICIENCY_FRONTIER</span>
        <div className="flex items-center gap-3 flex-wrap justify-end">
          {modelKeys.map((k) => (
            <span key={k} className="flex items-center gap-1.5 text-[9px] font-label-caps text-on-surface-variant">
              <svg width="10" height="10" viewBox="-5 -5 10 10">
                <ShapeMarker shape={modelShape(k)} cx={0} cy={0} size={3.5} fill="var(--color-on-surface-variant)" opacity={1} />
              </svg>
              {modelLabel(k)}
            </span>
          ))}
        </div>
      </div>

      <div ref={containerRef} className="relative flex-1 min-h-0">
        <svg viewBox={`0 0 ${width} ${height - 56}`} className="w-full h-full" preserveAspectRatio="none" role="img">
          <line x1={MARGIN.left} y1={MARGIN.top} x2={MARGIN.left} y2={MARGIN.top + plotH} stroke="var(--color-outline-variant)" />
          <line x1={MARGIN.left} y1={MARGIN.top + plotH} x2={MARGIN.left + plotW} y2={MARGIN.top + plotH} stroke="var(--color-outline-variant)" />

          {yTicks.map((a) => (
            <g key={a}>
              <line x1={MARGIN.left} y1={yPos(a)} x2={MARGIN.left + plotW} y2={yPos(a)} stroke="var(--color-outline-variant)" strokeOpacity={0.15} />
              <text x={MARGIN.left - 6} y={yPos(a) + 3} textAnchor="end" fontSize={8} fill="var(--color-on-surface-variant)" fontFamily="var(--font-data-mono)">
                {(a * 100).toFixed(0)}%
              </text>
            </g>
          ))}

          {xTicks.map((t) => (
            <text key={t} x={xPos(t)} y={MARGIN.top + plotH + 14} textAnchor="middle" fontSize={8} fill="var(--color-on-surface-variant)" fontFamily="var(--font-data-mono)">
              {t >= 1000 ? `${t / 1000}s` : `${t}ms`}
            </text>
          ))}

          <text x={MARGIN.left + plotW / 2} y={height - 56 - 2} textAnchor="middle" fontSize={8} fill="var(--color-on-surface-variant)" fontFamily="var(--font-label-caps)" letterSpacing="0.08em">
            AVG RESPONSE TIME (log scale, per class)
          </text>
          <text x={10} y={MARGIN.top + plotH / 2} textAnchor="middle" fontSize={8} fill="var(--color-on-surface-variant)" fontFamily="var(--font-label-caps)" letterSpacing="0.08em" transform={`rotate(-90, 10, ${MARGIN.top + plotH / 2})`}>
            ACCURACY
          </text>

          {points.map((p) => (
            <ShapeMarker
              key={p.key}
              shape={p.shape}
              cx={p.x}
              cy={p.y}
              size={hovered === p.key ? 6 : 4}
              fill={p.color}
              opacity={hovered && hovered !== p.key ? 0.35 : 0.85}
              onMouseEnter={(e) => { setHovered(p.key); handlePointMove(e); }}
              onMouseMove={handlePointMove}
              onMouseLeave={() => setHovered(null)}
            />
          ))}
        </svg>

        {hoveredPoint && (
          <div
            className="absolute bg-surface-container-highest border border-outline px-2.5 py-1.5 pointer-events-none text-[10px] font-data-mono z-10"
            style={{ left: tooltipPos.x, top: tooltipPos.y }}
          >
            <div className="text-on-surface">{modelLabel(hoveredPoint.modelKey)}</div>
            <div className="text-on-surface-variant">{hoveredPoint.cls}</div>
            <div className="text-on-surface-variant">
              {(hoveredPoint.accuracy * 100).toFixed(1)}% ({hoveredPoint.correct}/{hoveredPoint.total}) ·{" "}
              {hoveredPoint.rt >= 1000 ? `${(hoveredPoint.rt / 1000).toFixed(1)}s` : `${Math.round(hoveredPoint.rt)}ms`}
            </div>
          </div>
        )}
      </div>

      <p className="text-[9px] text-on-surface-variant mt-1 shrink-0">
        {points.length} real (class × model) cohorts. Y-axis starts at {(ACC_AXIS_MIN * 100).toFixed(0)}% — no real
        cohort in this data falls below it. Hover a dot for exact values (color = class, shape = model).
      </p>
    </div>
  );
}
