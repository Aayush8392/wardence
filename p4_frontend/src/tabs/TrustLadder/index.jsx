import { useEffect, useState } from "react";
import { fetchTrustLadder } from "../../api/r2";
import { useNavHistory } from "../../context/NavHistoryContext";

const STATE_LABELS = {
  report_only: "Report-Only",
  can_act: "Can-Act",
  demoted: "Demoted",
};

export default function TrustLadder() {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const { navigateTo, currentContext } = useNavHistory();

  // Consume a cross-tab jump from Operator's "Review Decision" link (pair #3)
  const highlightClass = currentContext?.type === "faultClass" ? currentContext.faultClass : null;

  useEffect(() => {
    let cancelled = false;
    fetchTrustLadder()
      .then((data) => { if (!cancelled) setRows(data); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  if (error) return <p>Failed to load Trust Ladder: {error}</p>;
  if (!rows) return <p>Loading…</p>;

  return (
    <div>
      <h1>Trust Ladder</h1>

      {/* Ceiling/ITBench callout -- locked to live here, not a standalone
          Methodology tab (see wardence_frontend.md). */}
      <p style={{ fontSize: 13, opacity: 0.8, maxWidth: 560 }}>
        Frontier models solve ~11% of real SRE scenarios (ITBench). Most fault
        classes here are correctly kept report-only, not a failure — that's
        the thesis: autonomy is earned per class, not assumed.
      </p>

      {highlightClass && (
        <p style={{ fontSize: 12, opacity: 0.8 }}>
          Reviewing <strong>{highlightClass}</strong>. Note: this table is a periodic snapshot
          (published to R2), so a change made moments ago in Operator may not be reflected yet.
        </p>
      )}

      <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 16 }}>
        <thead>
          <tr style={{ textAlign: "left", borderBottom: "1px solid #444" }}>
            <th>Fault Class</th>
            <th>Tier</th>
            <th>State</th>
            <th>Streak</th>
            <th>Diagnosis Accuracy</th>
            <th>Episodes Scored</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.fault_class}
              style={{
                cursor: "pointer",
                borderBottom: "1px solid #2a2a2a",
                outline: r.fault_class === highlightClass ? "2px solid #4caf50" : "none",
              }}
              onClick={() =>
                navigateTo(
                  "/replay",
                  { type: "faultClass", faultClass: r.fault_class },
                  "Trust Ladder"
                )
              }
            >
              <td>{r.fault_class}</td>
              <td>{r.tier}</td>
              <td>{STATE_LABELS[r.state] ?? r.state}</td>
              <td>{r.streak ?? "—"}</td>
              <td>{r.diagnosis_accuracy != null ? `${(r.diagnosis_accuracy * 100).toFixed(1)}%` : "—"}</td>
              <td>{r.episodes_scored}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
