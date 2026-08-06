import { useMemo, useState } from "react";
import {
  modelLabel,
  realClassesFromConfusionMatrix,
  allPredictedClasses,
  visibleModelKeys,
} from "../../utils/modelScorecard";

// Kimi review 27's real, binned-color-scale instruction (item 1, chart
// pushback #1): "make it a text-in-cell heatmap with a truncated/binned
// color scale... Do not rely on a continuous viridis scale -- it will
// lie to the viewer by making 97% and 100% look identical." Real bins,
// matching his own example thresholds.
function cellColorClass(pct) {
  if (pct == null) return "bg-surface-container-high/30 text-on-surface-variant/30";
  if (pct >= 1) return "bg-surface-container-highest text-on-surface";
  if (pct >= 0.95) return "bg-warning-amber/25 text-warning-amber";
  if (pct >= 0.9) return "bg-warning-amber/40 text-warning-amber";
  return "bg-error-red/35 text-error-red";
}

// Real predicted-class labels, but every actual real fault class it's
// diagnosed as is included even if never seen for THIS model -- the
// mockups' fabricated class names (DNS_E, RBAC_, TLS_E, ...) are gone;
// this pulls the real roster from the real confusion matrix itself.
export default function ConfusionMatrixHeatmap({ confusionMatrix }) {
  const modelKeys = useMemo(() => visibleModelKeys(confusionMatrix), [confusionMatrix]);
  const [selectedModel, setSelectedModel] = useState(modelKeys[0]);

  const realClasses = useMemo(() => realClassesFromConfusionMatrix(confusionMatrix), [confusionMatrix]);
  const predictedUniverse = useMemo(() => {
    const set = allPredictedClasses(confusionMatrix);
    // Real predicted classes only -- realClasses first (diagonal-aligned),
    // then any extra predicted label that isn't itself a real fault class
    // (e.g. "no anomaly detected", the DL-detector's generic fallback).
    const extra = [...set].filter((c) => !realClasses.includes(c)).sort();
    return [...realClasses, ...extra];
  }, [confusionMatrix, realClasses]);

  const byActual = confusionMatrix?.[selectedModel] ?? {};

  return (
    <div className="bg-surface-container-low border border-outline-variant p-5 overflow-x-auto">
      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <span className="font-label-caps text-[11px] text-on-surface-variant">CONFUSION_MATRIX</span>
        <select
          value={selectedModel}
          onChange={(e) => setSelectedModel(e.target.value)}
          className="bg-surface-container-low border border-outline-variant px-3 py-1.5 text-xs font-data-mono"
        >
          {modelKeys.map((k) => (
            <option key={k} value={k}>{modelLabel(k)}</option>
          ))}
        </select>
      </div>

      <table className="text-xs min-w-[900px]">
        <thead>
          <tr>
            <th className="text-left font-label-caps text-[9px] text-on-surface-variant pb-2 pr-3">ACTUAL \ PREDICTED</th>
            {predictedUniverse.map((p) => (
              <th key={p} className="font-label-caps text-[9px] text-on-surface-variant pb-2 px-1 whitespace-nowrap">
                {p === "no anomaly detected" ? "UNCLASS." : p.slice(0, 6).toUpperCase()}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {realClasses.map((actualClass) => {
            const row = byActual[actualClass] ?? {};
            const rowTotal = Object.values(row).reduce((s, n) => s + n, 0);
            return (
              <tr key={actualClass}>
                <td className="font-data-mono text-on-surface pr-3 py-1 whitespace-nowrap">{actualClass}</td>
                {predictedUniverse.map((predictedClass) => {
                  const count = row[predictedClass];
                  const pct = count != null && rowTotal ? count / rowTotal : null;
                  return (
                    <td key={predictedClass} className="px-1 py-1">
                      <div
                        className={`w-12 h-8 flex items-center justify-center font-data-mono text-[10px] ${cellColorClass(
                          count != null ? pct : null
                        )}`}
                        title={count != null ? `${actualClass} → ${predictedClass}: ${count} (${(pct * 100).toFixed(0)}%)` : ""}
                      >
                        {count ?? ""}
                      </div>
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="text-[10px] text-on-surface-variant mt-3">
        Rows are real ground-truth fault classes, columns are what {modelLabel(selectedModel)} actually predicted.
        Diagonal = correct. Binned color scale (white=100%, amber=90-99%, red&lt;90%) — a continuous gradient would
        make 97% and 100% look identical, which this data can't afford (most cells sit in a tight 90-100% band).
      </p>
    </div>
  );
}
