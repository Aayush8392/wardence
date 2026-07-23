import { useEffect, useState } from "react";
import { fetchSystemStatus } from "../../api/r2";

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

  return (
    <div className="lg:col-span-4 bg-surface-container-low border border-outline-variant p-4 relative overflow-hidden h-24">
      <div className="relative z-10 flex flex-col justify-between h-full">
        <div className="flex justify-between items-center">
          <span className="font-label-caps text-[9px] text-on-surface-variant">TRUST_STATE_VECTOR</span>
          <span className="font-data-mono text-[10px] text-primary">VECTOR_MAG: {vectorMag.toFixed(2)}</span>
        </div>
        <div className="flex items-end gap-1 h-8">
          {sorted.map((r, i) => (
            <div
              key={r.fault_class}
              title={`${r.fault_class}: ${((r.diagnosis_accuracy ?? 0) * 100).toFixed(0)}%`}
              className={`w-1 ${i === 0 ? "bg-primary-fixed-dim vector-point" : "bg-primary"}`}
              style={{ height: `${Math.max((r.diagnosis_accuracy ?? 0) * 100, 8)}%` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function StatTile({ label, value, tone }) {
  return (
    <div className="flex-1 min-w-[150px] p-4 flex flex-col gap-1">
      <span className="font-label-caps text-[10px] text-on-surface-variant">{label}</span>
      <div className={`font-data-mono text-xl ${tone ?? "text-on-surface"}`}>{value}</div>
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
        <StatTile label="AVG_EARNED_TRUST" value={avgEarnedTrust != null ? `${(avgEarnedTrust * 100).toFixed(1)}%` : "—"} tone="text-primary" />
        <StatTile label="AUTONOMOUS_ACTS" value={autonomousActs.toLocaleString()} />
        <StatTile label="DEMOTIONS_LOGGED" value={demotionsLogged} tone={demotionsLogged > 0 ? "text-error" : "text-on-surface"} />
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
