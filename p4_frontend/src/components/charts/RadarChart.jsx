import { useEffect, useState } from "react";

// Real, hand-rolled SVG radar/spider chart -- same "no new chart
// dependency" convention as ScatterPlot.jsx/EfficiencyFrontier.jsx (this
// project has never added a chart library). Renders exactly one shape:
// the CURRENT fault class's real 6-axis Trust Dossier data
// (publish_to_r2.py's build_radar_dossier). A null axis value (action
// accuracy / calibration deviation / confidence stdev on a
// too-sparse or report-only class) is plotted at the chart's own
// center (0) and its label rendered dimmed with "N/A" -- never
// fabricated as a guessed midpoint.
// Real bug found and fixed 2026-08-1x: the SVG is CSS-scaled to fill
// whatever real pixel width its container gives it (EpisodePanel's
// slide-out is a fixed sm:w-[420px], ~400px usable after padding) --
// SIZE below is just the viewBox's internal coordinate count, NOT a
// screen-pixel size. Every earlier attempt to make this chart "bigger"
// by RAISING SIZE (280 -> 320 -> 420 -> 460) was backwards: raising
// SIZE while the real container width stays fixed shrinks the render
// scale factor (realContainerPx / SIZE), so fonts/radius defined in
// viewBox units actually got SMALLER on screen with each "increase",
// which is exactly the cramped result reported. Fixed properly this
// time: SIZE is set close to the real container width so the scale
// factor is ~1:1 (viewBox units ~= real screen px), and every
// font-size/margin/offset constant below is chosen as a genuine target
// pixel size at that scale, not a number that happened to work at some
// other SIZE.
const SIZE = 400;
const CENTER = SIZE / 2;
// Margin widened to 62 -- labels reverted back to their full 2-line
// names (see ALL_AXES below), so the label+value stack is back to 3
// lines for most axes. Label start point stays RADIUS+28 (kept for the
// real gap from the outer-ring tick, found necessary two rounds ago);
// 62 gives ~34px of real room below that start point, enough for the
// 2nd label line + value line + descent without clipping.
const RADIUS = SIZE / 2 - 62;
const RINGS = [0.25, 0.5, 0.75, 1];

// Real axis count differs by tier, not just a cosmetic shape choice:
// action_accuracy is structurally N/A for every report-only class (no
// Dimension-C history exists for a class that never dispatches an
// action -- see build_action_accuracy_by_class's docstring), so a
// report-only class's chart drops that axis entirely rather than
// showing a permanent dead vertex. Auto-fix classes keep all 6
// (hexagon); report-only classes get the real remaining 5 (pentagon).
const ALL_AXES = [
  { key: "diagnosis_accuracy", label: "DIAGNOSTIC\nACCURACY" },
  { key: "action_accuracy", label: "ACTION\nACCURACY" },
  { key: "calibration_deviation", label: "CALIBRATION" },
  { key: "dimension_b_streak", label: "TRUST\nSTREAK" },
  { key: "avg_response_time_ms", label: "RESPONSE\nSPEED*" },
  { key: "confidence_stdev", label: "CONFIDENCE\nSPREAD*" },
];

function pointOn(angle, r) {
  return [CENTER + r * Math.sin(angle), CENTER - r * Math.cos(angle)];
}

// Normalizes each axis to a real 0-1 "higher is better on the page"
// scale. Accuracy/action-accuracy are already 0-1. Calibration
// deviation is inverted (lower deviation = better, so 1-deviation,
// clamped -- real deviations observed so far are well under 1).
// Trust streak is scaled against the Dimension-B promotion threshold
// (5) since that's the real, meaningful target, clamped at 1 for any
// class that has streaked past it. Response time and confidence
// spread have no natural 0-1 bound, so they're min-max scaled against
// the REAL range observed across the whole roster (passed in via
// `allValues`) -- honest relative comparison, not a guessed fixed
// ceiling; response time is inverted (faster = higher on the chart).
function normalize(key, value, allValues) {
  if (value === null || value === undefined) return null;
  switch (key) {
    case "diagnosis_accuracy":
    case "action_accuracy":
      return Math.max(0, Math.min(1, value));
    case "calibration_deviation":
      return Math.max(0, Math.min(1, 1 - value));
    case "dimension_b_streak":
      return Math.max(0, Math.min(1, value / 5));
    case "avg_response_time_ms": {
      const vals = allValues.filter((v) => v !== null && v !== undefined);
      if (vals.length < 2) return 0.5; // not enough spread to place relatively -- neutral, not guessed
      const lo = Math.min(...vals);
      const hi = Math.max(...vals);
      if (hi === lo) return 0.5;
      return 1 - (value - lo) / (hi - lo); // faster (lower ms) -> higher on chart
    }
    case "confidence_stdev": {
      const vals = allValues.filter((v) => v !== null && v !== undefined);
      if (vals.length < 2) return 0.5;
      const lo = Math.min(...vals);
      const hi = Math.max(...vals);
      if (hi === lo) return 0.5;
      return (value - lo) / (hi - lo);
    }
    default:
      return null;
  }
}

// Real raw-value display, shown in brackets under each axis label --
// each axis gets its own real unit/precision, never a shared generic
// format. Deliberately shows the RAW value (e.g. calibration deviation
// as an actual deviation), not the inverted/normalized number used to
// place the point on the chart -- the placement and the label answer
// different questions ("how good, relative to the ring" vs "what is
// the real measured number").
function formatRaw(key, raw) {
  if (raw === null || raw === undefined) return null;
  switch (key) {
    case "diagnosis_accuracy":
    case "action_accuracy":
      return `${(raw * 100).toFixed(1)}%`;
    case "calibration_deviation":
      return `±${raw.toFixed(3)}`;
    case "dimension_b_streak":
      return `${Math.round(raw)}`;
    case "avg_response_time_ms":
      return `${Math.round(raw)}ms`;
    case "confidence_stdev":
      return raw.toFixed(3);
    default:
      return `${raw}`;
  }
}

// Real inverse of normalize() -- given a ring fraction (0-1), returns
// what RAW value that ring actually represents for this specific axis,
// in that axis's own real units. This is the fix for "no sense of
// scale": normalize() answers "where does this real value land on the
// chart," this answers the opposite question, "what real value would
// land HERE" -- both are needed, one for the data polygon, one for the
// axis ticks. Accuracy/streak/calibration have a fixed, absolute real
// meaning (100% is 100% regardless of the roster); response time and
// confidence spread don't (they're relative to whatever range the
// roster currently has), so their ticks are real min-max-derived
// numbers too, not fabricated round figures -- explicitly labeled
// "roster" in the caller so a reader doesn't mistake them for a fixed
// target the way the other axes' ticks are.
function tickValue(key, ringFraction, allValues) {
  switch (key) {
    case "diagnosis_accuracy":
    case "action_accuracy":
      return `${Math.round(ringFraction * 100)}%`;
    case "calibration_deviation":
      return `±${(1 - ringFraction).toFixed(2)}`;
    case "dimension_b_streak":
      return `${Math.round(ringFraction * 5)}`;
    case "avg_response_time_ms": {
      const vals = allValues.filter((v) => v !== null && v !== undefined);
      if (vals.length < 2) return null;
      const lo = Math.min(...vals);
      const hi = Math.max(...vals);
      if (hi === lo) return null;
      return `${Math.round(hi - ringFraction * (hi - lo))}ms`;
    }
    case "confidence_stdev": {
      const vals = allValues.filter((v) => v !== null && v !== undefined);
      if (vals.length < 2) return null;
      const lo = Math.min(...vals);
      const hi = Math.max(...vals);
      if (hi === lo) return null;
      return (lo + ringFraction * (hi - lo)).toFixed(3);
    }
    default:
      return null;
  }
}

// All 4 grid rings get a real tick label per axis -- user's explicit
// call after seeing the 2-ring version: 25%/75% are worth seeing too,
// not just the midpoint and the edge.
const TICK_RINGS = RINGS;

export default function RadarChart({ dossierEntry, allDossierValuesByAxis }) {
  const isReportOnly = dossierEntry?.tier === "report-only";
  const axes = isReportOnly ? ALL_AXES.filter((a) => a.key !== "action_accuracy") : ALL_AXES;
  const n = axes.length;
  const angleStep = (2 * Math.PI) / n;

  // Real entrance animation, locked in the Screen-2 design session
  // (wardence_frontend.md, 2026-08-14): the coverage polygon grows from
  // a zero-area point at center out to its real values the first time
  // the dossier is opened -- not a decorative loop, plays once per
  // mount (EpisodePanel only mounts this component when dossierOpen
  // flips true, so "on open" and "on mount" are the same event here).
  const [grown, setGrown] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setGrown(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  const points = axes.map((axis, i) => {
    const angle = i * angleStep;
    const raw = dossierEntry ? dossierEntry[axis.key] : null;
    const norm = normalize(axis.key, raw, allDossierValuesByAxis?.[axis.key] ?? []);
    return { ...axis, angle, raw, norm, display: formatRaw(axis.key, raw) };
  });

  const polygonPoints = points
    .map((p) => {
      const r = (p.norm ?? 0) * RADIUS;
      const [x, y] = pointOn(p.angle, r);
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className="w-full h-auto">
      {/* Grid rings */}
      {RINGS.map((r) => (
        <polygon
          key={r}
          points={axes.map((_, i) => pointOn(i * angleStep, r * RADIUS).join(",")).join(" ")}
          fill="none"
          stroke="#e8e8ec"
          strokeOpacity={0.65}
          strokeWidth={1}
        />
      ))}
      {/* Spokes */}
      {axes.map((_, i) => {
        const [x, y] = pointOn(i * angleStep, RADIUS);
        return (
          <line
            key={i}
            x1={CENTER}
            y1={CENTER}
            x2={x}
            y2={y}
            stroke="#e8e8ec"
            strokeOpacity={0.65}
            strokeWidth={1}
          />
        );
      })}
      {/* Real data polygon + vertex dots, animated together as one group
          from a zero-area point at center out to their real positions.
          transform-box defaults to view-box for an SVG's own coordinate
          system, so transformOrigin in raw user-space px lands exactly
          on CENTER,CENTER regardless of the group's own bounding box. */}
      <g
        style={{
          transform: grown ? "scale(1)" : "scale(0)",
          transformOrigin: `${CENTER}px ${CENTER}px`,
          transition: "transform 1.2s cubic-bezier(0.16, 1, 0.3, 1)",
        }}
      >
        <polygon
          points={polygonPoints}
          fill="var(--color-primary)"
          fillOpacity={0.18}
          stroke="var(--color-primary)"
          strokeWidth={2}
        />
        {/* Vertex dots -- N/A axes get a hollow dot at center */}
        {points.map((p) => {
          const r = (p.norm ?? 0) * RADIUS;
          const [x, y] = pointOn(p.angle, r);
          return (
            <circle
              key={p.key}
              cx={x}
              cy={y}
              r={p.norm === null ? 3 : 3.5}
              fill={p.norm === null ? "none" : "var(--color-primary)"}
              stroke="var(--color-primary)"
              strokeWidth={p.norm === null ? 1 : 0}
              strokeDasharray={p.norm === null ? "2,2" : undefined}
            />
          );
        })}
      </g>
      {/* Scale ticks -- real per-axis value at the midpoint and outer
          rings, nudged a few px off the spoke line (perpendicular to
          it) so the number doesn't sit directly on top of the grid
          line/spoke it's labeling. Deliberately rendered AFTER the data
          <g> group above (not before, like the original version) --
          real bug found: when a real value lands close to a ring (e.g.
          diagnosis_accuracy at 96.6%, right next to the 100% ring), its
          opaque vertex dot painted over that ring's tick text since it
          rendered later in DOM order. Ticks now always paint on top,
          so a real value near a ring can never hide that ring's own
          label again. */}
      {axes.map((axis, i) => {
        const angle = i * angleStep;
        const perpAngle = angle + Math.PI / 2;
        return TICK_RINGS.map((ring) => {
          const label = tickValue(axis.key, ring, allDossierValuesByAxis?.[axis.key] ?? []);
          if (label === null) return null;
          // Real bug found: at ring=1 (the outer ring), this tick sat
          // almost exactly where the axis-name label starts (RADIUS+14)
          // once labels were shortened to single-line -- pulling the
          // tick radius 8px IN from the ring line itself (not sitting
          // on it) creates genuine radial separation from that zone,
          // on every ring, not just the outer one (keeps all 4 ticks
          // visually consistent along the spoke).
          const [bx, by] = pointOn(angle, ring * RADIUS - 8);
          const x = bx + 9 * Math.sin(perpAngle);
          const y = by - 9 * Math.cos(perpAngle);
          return (
            <text
              key={`${axis.key}-${ring}`}
              x={x}
              y={y}
              textAnchor="middle"
              dominantBaseline="middle"
              fontSize={8.5}
              fill="var(--on-surface-variant, #999)"
              opacity={0.8}
              stroke="var(--surface-container-lowest, #0d0d12)"
              strokeWidth={3}
              paintOrder="stroke"
            >
              {label}
            </text>
          );
        });
      })}
      {/* Axis labels -- full name (1 or 2 lines, per ALL_AXES) + a value
          line underneath. */}
      {points.map((p) => {
        const [x, y] = pointOn(p.angle, RADIUS + 28);
        const lines = p.label.split("\n");
        return (
          <text
            key={p.key}
            x={x}
            y={y}
            textAnchor="middle"
            dominantBaseline="middle"
            className="font-label-caps"
            fontSize={11}
            fill={p.norm === null ? "var(--on-surface-variant, #999)" : "var(--on-surface, #eee)"}
            opacity={p.norm === null ? 0.55 : 1}
          >
            {lines.map((line, li) => (
              <tspan key={li} x={x} dy={li === 0 ? 0 : 13}>
                {line}
              </tspan>
            ))}
            <tspan x={x} dy={13} fontSize={9.5} fillOpacity={0.75}>
              {p.display !== null ? `(${p.display})` : "(N/A)"}
            </tspan>
          </text>
        );
      })}
    </svg>
  );
}
