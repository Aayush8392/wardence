const CLASS_LABELS = {
  "crash-loop": "Crash-loop Injection",
  oom: "OOM (Out of Memory)",
  "disk-full": "Disk-full Overflow",
};

export default function AdminOverrides({ autoFixClasses, trustMap, highlightClass, busyClass, onPromote, onDemote }) {
  return (
    <div className="bg-surface-container border border-outline overflow-hidden">
      <div className="bg-error-container/10 border-b border-error-container/30 p-4 flex items-center gap-3">
        <span className="material-symbols-outlined text-error">admin_panel_settings</span>
        <h2 className="font-headline-md text-lg">Admin Trust Overrides</h2>
      </div>

      <div className="p-6 space-y-4">
        {autoFixClasses.map((fc) => {
          const state = trustMap[fc] ?? "unknown";
          return (
            <div
              key={fc}
              className={`flex items-center justify-between p-3 bg-surface border ${
                fc === highlightClass ? "border-primary" : "border-outline"
              }`}
            >
              <div className="flex flex-col">
                <span className="font-bold">{CLASS_LABELS[fc] ?? fc}</span>
                <span className="font-data-mono text-[10px] text-on-surface-variant">
                  Current: {state.toUpperCase()}
                </span>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => onPromote(fc)}
                  disabled={busyClass === fc}
                  className="px-4 py-1.5 bg-[#238636] text-white font-label-caps text-[11px] hover:brightness-110 transition-all disabled:opacity-60"
                >
                  {busyClass === fc ? "…" : "FORCE PROMOTE"}
                </button>
                <button
                  onClick={() => onDemote(fc)}
                  disabled={busyClass === fc}
                  className="px-4 py-1.5 bg-error-container text-white font-label-caps text-[11px] hover:brightness-110 transition-all disabled:opacity-60"
                >
                  {busyClass === fc ? "…" : "FORCE DEMOTE"}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
