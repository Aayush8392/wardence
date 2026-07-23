// Real fields, matching query_prometheus's actual return shape
// (p2_readonly_loop/agent.py) -- not guessed metric names. Empty
// arrays/null values are skipped rather than shown as noise.
const FIELD_LABELS = {
  oom_pods: "OOM pods",
  evicted_pods: "Evicted pods",
  crashlooping_pods: "Crash-looping pods",
  p95_latency_ms: "p95 latency (ms)",
  peak_memory_mib: "Peak memory (MiB)",
  peak_threads_connected: "Peak DB connections",
};

function formatValue(value) {
  if (Array.isArray(value)) return value.length > 0 ? value.join(", ") : null;
  if (typeof value === "number") return value.toFixed(1);
  return value;
}

export default function ToolOutputSnapshot({ toolOutput }) {
  const entries = Object.entries(FIELD_LABELS)
    .map(([key, label]) => [label, formatValue(toolOutput?.[key])])
    .filter(([, value]) => value != null);

  return (
    <div className="bg-surface-container-lowest border border-outline-variant overflow-hidden">
      <div className="bg-surface-variant px-4 py-2 flex justify-between items-center">
        <span className="font-label-caps text-[10px] font-bold">TOOL OUTPUT SNAPSHOT</span>
        <span className="font-data-mono text-[10px] text-on-surface-variant">source: prometheus</span>
      </div>
      <div className="p-4 font-data-mono text-xs">
        {entries.length === 0 ? (
          <span className="text-on-surface-variant opacity-60">No signal captured for this episode.</span>
        ) : (
          <div className="space-y-2">
            {entries.map(([label, value]) => (
              <div key={label} className="flex justify-between gap-4">
                <span className="text-on-surface-variant">{label}</span>
                <span className="text-primary">{value}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
