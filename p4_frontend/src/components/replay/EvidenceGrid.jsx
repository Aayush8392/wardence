// Real per-field thresholds, copied from p2_readonly_loop/agent.py's own
// stubbed diagnosis constants -- not guessed. Used only to size a gauge
// bar's fill against something meaningful; never invents a threshold for
// a field that doesn't have one in the real diagnoser.
const THRESHOLDS = {
  p95_latency_ms: 300, // HIGH_LATENCY_THRESHOLD_MS
  peak_memory_mib: 380, // memory-leak numeric threshold
  peak_threads_connected: 100, // connection-pool-exhaustion numeric threshold
};

function GaugeTile({ label, value, unit, threshold }) {
  const pct = threshold ? Math.min(100, (value / threshold) * 100) : null;
  const breached = threshold != null && value >= threshold;
  return (
    <div className="border border-outline-variant/40 bg-surface-container-lowest p-3 relative group">
      <div className="absolute top-0 left-0 w-2 h-2 border-t border-l border-outline-variant/60" />
      <div className="font-label-caps text-[9px] text-outline mb-1">{label}</div>
      <div className={`font-data-mono text-xl ${breached ? "text-error" : "text-on-surface"}`}>
        {value}
        <span className="text-xs ml-0.5">{unit}</span>
      </div>
      {threshold != null && (
        <div className="mt-2">
          <div className="w-full h-1.5 bg-surface-container-highest border border-outline-variant/30 overflow-hidden">
            <div className={`h-full ${breached ? "bg-error" : "bg-primary"}`} style={{ width: `${pct}%` }} />
          </div>
          <div className="font-data-mono text-[9px] text-outline text-right mt-0.5">
            {breached ? "THRESH_EXCEEDED" : `LIMIT ${threshold}${unit}`}
          </div>
        </div>
      )}
    </div>
  );
}

function ListTile({ label, items }) {
  return (
    <div className="border border-outline-variant/40 bg-surface-container-lowest p-3 relative">
      <div className="absolute top-0 left-0 w-2 h-2 border-t border-l border-outline-variant/60" />
      <div className="font-label-caps text-[9px] text-outline mb-1.5">{label}</div>
      {items.length === 0 ? (
        <div className="font-data-mono text-sm text-on-surface-variant/50">none</div>
      ) : (
        <div className="flex flex-wrap gap-1">
          {items.map((it) => (
            <span key={it} className="font-data-mono text-[10px] text-error bg-error/10 border border-error/30 px-1.5 py-0.5 rounded-sm">
              {it}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// Only renders real fields actually present on this episode's tool_output
// (p2_readonly_loop/agent.py's query_prometheus return shape) -- gauge/bar
// treatment for single numeric values with a known real threshold, a
// plain pill list for the pod-name arrays. Never fabricates a per-bucket
// histogram or a field that wasn't actually captured.
export default function EvidenceGrid({ toolOutput }) {
  if (!toolOutput) return null;

  const numericFields = [
    { key: "p95_latency_ms", label: "P95_LATENCY", unit: "ms" },
    { key: "peak_memory_mib", label: "PEAK_MEMORY", unit: "Mi" },
    { key: "peak_threads_connected", label: "DB_CONNECTIONS", unit: "" },
  ].filter((f) => typeof toolOutput[f.key] === "number");

  const listFields = [
    { key: "oom_pods", label: "OOM_PODS" },
    { key: "evicted_pods", label: "EVICTED_PODS" },
    { key: "crashlooping_pods", label: "CRASHLOOPING_PODS" },
  ].filter((f) => Array.isArray(toolOutput[f.key]));

  if (numericFields.length === 0 && listFields.length === 0) return null;

  return (
    <div className="grid grid-cols-2 gap-2">
      {numericFields.map((f) => (
        <GaugeTile
          key={f.key}
          label={f.label}
          value={Number(toolOutput[f.key].toFixed(1))}
          unit={f.unit}
          threshold={THRESHOLDS[f.key]}
        />
      ))}
      {listFields.map((f) => (
        <ListTile key={f.key} label={f.label} items={toolOutput[f.key]} />
      ))}
    </div>
  );
}
