export default function EpisodeList({ episodes, selectedId, onSelect, maxHeight }) {
  return (
    <section
      className="w-80 shrink-0 border-r border-outline-variant flex flex-col overflow-hidden"
      style={{ maxHeight: maxHeight ?? undefined }}
    >
      <div className="flex-1 min-h-0 overflow-y-auto">
        {episodes.length === 0 && (
          <p className="p-4 text-sm text-on-surface-variant">No episodes match this filter.</p>
        )}
        {episodes.map((ep) => (
          <div
            key={ep.episode_id}
            onClick={() => onSelect(ep.episode_id)}
            className={`p-4 border-b border-outline-variant hover:bg-surface-variant cursor-pointer transition-colors ${
              ep.episode_id === selectedId ? "bg-surface-container-high" : ""
            }`}
          >
            <div className="flex justify-between items-start mb-2">
              <span className={`font-data-mono text-sm ${ep.episode_id === selectedId ? "text-primary" : "text-on-surface-variant"}`}>
                {ep.episode_id.slice(0, 8)}
              </span>
              <span
                className={`px-2 py-0.5 border font-label-caps text-[9px] ${
                  ep.correct ? "border-[#238636] text-[#238636]" : "border-error text-error"
                }`}
              >
                {ep.correct ? "CORRECT" : "WRONG"}
              </span>
            </div>
            <div className="space-y-1">
              <div className="flex justify-between">
                <span className="font-label-caps text-[10px] text-on-surface-variant uppercase">Fault:</span>
                <span className="font-data-mono text-xs">{ep.fault_class}</span>
              </div>
              <div className="flex justify-between">
                <span className="font-label-caps text-[10px] text-on-surface-variant uppercase">Target:</span>
                <span className="font-data-mono text-xs">{ep.target}</span>
              </div>
              <div className="mt-2">
                <span className="font-data-mono text-[11px] text-on-surface-variant">{ep.t0}</span>
              </div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
