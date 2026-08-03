import { useEffect, useState } from "react";
import AnimatedNumber from "../shared/AnimatedNumber";
import { fetchSystemStatus } from "../../api/r2";
import BarChart from "../charts/bar-chart";
import Bar from "../charts/bar";
import { ChartTooltip } from "../charts/tooltip";

const PERCENT_FORMAT = { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 };

// Real computed stats only -- no fabricated numbers (locked rule). Every
// value is derived from trust_ladder.json/episodes.json/trust_history.json/
// system_status.json, nothing hardcoded like the export's placeholder
// "1,248" / "4.10%".

// Trust State Vector: bars represent the real per-class diagnosis accuracy
// (auto-fix classes first, matching the matrix's own ordering), not
// fabricated heights. VECTOR_MAG is the real weighted aggregate accuracy.
function TrustVector({ rows }) {
  const sorted = rows
    .slice()
    .sort((a, b) => (b.diagnosis_accuracy ?? 0) - (a.diagnosis_accuracy ?? 0))
    .slice(0, 9);

  const scored = rows.filter((r) => r.diagnosis_accuracy != null);
  const vectorMag =
    scored.length > 0
      ? scored.reduce((sum, r) => sum + r.diagnosis_accuracy * r.episodes_scored, 0) /
        scored.reduce((sum, r) => sum + r.episodes_scored, 0)
      : 0;

  const chartData = sorted.map((r) => ({
    name: r.fault_class.replaceAll("-", "_"),
    value: Math.round((r.diagnosis_accuracy ?? 0) * 1000) / 10, // 1 decimal, real %
  }));

  return (
    <div className="lg:col-span-4 bg-surface-container-low border border-outline-variant p-4 relative overflow-hidden h-32 flex flex-col">
      <div className="flex justify-between items-center mb-1">
        <span
          className="font-label-caps text-[9px] text-on-surface-variant"
          title="Top 9 fault classes by diagnosis accuracy. Bar height = accuracy (0-100%)."
        >
          TOP_9_CLASSES_BY_ACCURACY
        </span>
        <span
          className="font-data-mono text-[10px] text-primary"
          title="Overall diagnosis accuracy across every scored class, weighted by episode count"
        >
          OVERALL_ACCURACY: <AnimatedNumber value={vectorMag} format={PERCENT_FORMAT} />
        </span>
      </div>
      <BarChart
        data={chartData}
        xDataKey="name"
        className="flex-1"
        barGap={0.3}
        margin={{ top: 4, right: 2, bottom: 2, left: 2 }}
      >
        <Bar dataKey="value" fill="var(--chart-1)" lineCap={2} />
        <ChartTooltip showDatePill={false} showCrosshair={false} showDots={false} />
      </BarChart>
    </div>
  );
}

// value: a raw number to animate via NumberFlow, or a pre-formatted string
// (e.g. "—" for no data) to render as-is -- SECURITY_CAGE's Active_Armed/
// Tripped label has no numeric value at all, so it stays a plain StatTile
// child rendered outside this component.
function StatTile({ label, value, format, tone }) {
  return (
    <div className="flex-1 min-w-[150px] p-4 flex flex-col gap-1">
      <span className="font-label-caps text-[10px] text-on-surface-variant">{label}</span>
      <div className={`font-data-mono text-xl ${tone ?? "text-on-surface"}`}>
        {typeof value === "number" ? <AnimatedNumber value={value} format={format} /> : value}
      </div>
    </div>
  );
}

export default function Hero({ rows, episodes, trustHistory }) {
  const [systemStatus, setSystemStatus] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetchSystemStatus()
      .then((data) => { if (!cancelled) setSystemStatus(data); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const scored = rows.filter((r) => r.diagnosis_accuracy != null);
  const avgEarnedTrust =
    scored.length > 0
      ? scored.reduce((sum, r) => sum + r.diagnosis_accuracy * r.episodes_scored, 0) /
        scored.reduce((sum, r) => sum + r.episodes_scored, 0)
      : null;

  // Real count of episodes where the agent actually took a write action
  // (not just diagnosed) -- honest stand-in for the export's fabricated
  // "AUTONOMOUS_ACTS: 1,248".
  const autonomousActs = episodes.filter((e) => e.action_applied).length;

  // Real count of demotion events -- honest stand-in for the export's
  // fabricated "HUMAN_INT_RATE" (no such metric is tracked anywhere in the
  // backend; a demotion event is the real governance signal closest in
  // spirit -- trust being pulled back).
  const demotionsLogged = (trustHistory ?? []).filter((ev) => ev.state_after === "report_only").length;

  return (
    <div className="mb-8">
      <header className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start mb-8">
        <div className="lg:col-span-8">
          <div className="flex items-center gap-3 mb-2">
            <span className="font-label-caps text-[10px] text-primary px-2 border border-primary">LIVE_GOVERNANCE_STATE</span>
          </div>
          <h1 className="font-display-lg text-4xl lg:text-5xl text-on-surface font-bold tracking-tighter uppercase leading-none">
            EARNED_AUTONOMY <br />
            <span className="text-primary">AI GOVERNANCE FOR SRE</span>
          </h1>
          <p className="mt-3 text-sm text-on-surface-variant max-w-lg">
            Autonomy is measured, not assumed. Every fault class here earns the right to act
            through continuous, out-of-band verification — or is correctly kept report-only.
          </p>
        </div>

        <TrustVector rows={rows} />
      </header>

      <section className="status-ribbon border border-outline-variant flex flex-wrap lg:flex-nowrap divide-x divide-outline-variant">
        <StatTile
          label="AVG_EARNED_TRUST"
          value={avgEarnedTrust != null ? avgEarnedTrust : "—"}
          format={PERCENT_FORMAT}
          tone="text-primary"
        />
        <StatTile label="AUTONOMOUS_ACTS" value={autonomousActs} />
        <StatTile label="DEMOTIONS_LOGGED" value={demotionsLogged} tone={demotionsLogged > 0 ? "text-error" : "text-on-surface"} />
        <StatTile
          label="SYSTEM_INTEGRITY"
          value={systemStatus?.integrity_score_pct != null ? systemStatus.integrity_score_pct / 100 : "—"}
          format={PERCENT_FORMAT}
          tone="text-primary"
        />
        <div className="flex-1 min-w-[150px] p-4 flex flex-col gap-1">
          <span className="font-label-caps text-[10px] text-on-surface-variant">SECURITY_CAGE</span>
          <div className="flex items-center gap-2 mt-1">
            <span className={`w-2 h-2 ${systemStatus?.tripped ? "bg-red-500" : "bg-primary"}`} />
            <span className="font-data-mono text-[11px] text-on-surface uppercase">
              {!systemStatus ? "—" : systemStatus.tripped ? "Tripped" : "Active_Armed"}
            </span>
          </div>
        </div>
      </section>
    </div>
  );
}
