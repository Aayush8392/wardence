import BarChart from "../charts/bar-chart";
import Bar from "../charts/bar";
import { ChartTooltip } from "../charts/tooltip";
import { modelLabel } from "../../utils/modelScorecard";

// Kimi's own recommendation (review 27, item 3/item 25): replace the
// mockups' single fabricated "Action Bias" number with the real raw
// pair -- diagnostic accuracy vs. action durability rate -- and let the
// viewer read the gap themselves. Real, but model-level aggregate only
// (not per-class, which would need a new per-class durability backend
// field not yet built) -- only the two providers that actually dispatch
// real actions (Gemma/Nemotron) have any durability data at all.
export default function ActionDurabilityChart({ scorecard }) {
  const durabilityKeys = Object.keys(scorecard.durability_rate ?? {});

  if (durabilityKeys.length === 0) {
    return (
      <div className="bg-surface-container-low border border-outline-variant p-5">
        <span className="font-label-caps text-[11px] text-on-surface-variant block mb-2">
          ACTION_&amp;_DURABILITY
        </span>
        <p className="text-sm text-on-surface-variant">No real dispatched-action data yet.</p>
      </div>
    );
  }

  const chartData = durabilityKeys.map((key) => ({
    name: modelLabel(key),
    diagnostic_accuracy: Math.round((scorecard.overall_accuracy?.[key]?.accuracy ?? 0) * 1000) / 10,
    durability_rate: Math.round((scorecard.durability_rate?.[key]?.durability_rate ?? 0) * 1000) / 10,
  }));

  // Matches BarChart's own margin prop below -- keeps this plain label
  // row's left/right insets in sync with the chart's real plot area so
  // each name lands roughly under its own bar group without needing to
  // reach into the chart library's internal band scale.
  const chartMargin = { top: 10, right: 20, bottom: 4, left: 40 };

  return (
    <div className="bg-surface-container-low border border-outline-variant p-5">
      <div className="flex items-center justify-between mb-4">
        <span className="font-label-caps text-[11px] text-on-surface-variant">ACTION_&amp;_DURABILITY</span>
        <div className="flex items-center gap-4 text-[10px] font-label-caps">
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2" style={{ background: "var(--chart-1)" }} />
            <span className="text-on-surface-variant">DIAGNOSTIC ACCURACY</span>
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2" style={{ background: "var(--color-correct-green)" }} />
            <span className="text-on-surface-variant">DURABILITY RATE</span>
          </span>
        </div>
      </div>

      <div className="flex gap-2">
        {/* Y-axis title -- the chart library's real BarYAxis/BarXAxis
            components exist but aren't wired up anywhere else in this
            codebase yet; a plain rotated label is a much lower-risk
            way to answer "what's the axis" without reaching into
            untested chart-context internals for a single title. */}
        <div className="flex items-center justify-center shrink-0" style={{ width: 14 }}>
          <span
            className="font-label-caps text-[9px] text-on-surface-variant whitespace-nowrap"
            style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
          >
            PERCENT (%)
          </span>
        </div>

        <div className="flex-1 min-w-0">
          <BarChart data={chartData} xDataKey="name" aspectRatio="3 / 1" margin={chartMargin}>
            <Bar dataKey="diagnostic_accuracy" fill="var(--chart-1)" lineCap={2} />
            <Bar dataKey="durability_rate" fill="var(--color-correct-green)" lineCap={2} />
            <ChartTooltip showDatePill={false} />
          </BarChart>

          {/* Plain model-name row under the bars -- "at a glance"
              labeling per explicit request, on top of (not instead of)
              the existing hover tooltip. */}
          <div className="flex mt-1" style={{ paddingLeft: chartMargin.left, paddingRight: chartMargin.right }}>
            {chartData.map((d) => (
              <div key={d.name} className="flex-1 text-center">
                <span className="font-data-mono text-[10px] text-on-surface-variant">{d.name}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <p className="text-[10px] text-on-surface-variant mt-3">
        Real, model-level aggregate across all auto-fix classes — a per-class breakdown isn't published yet. Gemma
        and Nemotron are the only providers whose diagnoses ever get real dispatch, so they're the only ones shown.
      </p>
    </div>
  );
}
