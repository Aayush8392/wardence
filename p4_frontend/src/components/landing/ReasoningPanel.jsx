import SecurityCage from "../shared/SecurityCage";

const KEYWORD_CLASS = {
  OBSERVATION: "text-primary-fixed-dim",
  HYPOTHESIS: "text-primary",
  VERIFY: "text-primary",
  PLAN: "text-primary",
  EXECUTE: "text-primary-container",
  SUCCESS: "text-green-400",
  ERROR: "text-error",
  FAIL: "text-error",
};

function colorForLine(line) {
  const upper = line.toUpperCase();
  for (const [kw, cls] of Object.entries(KEYWORD_CLASS)) {
    if (upper.includes(kw)) return cls;
  }
  return null;
}

function ReasoningStream({ latestEpisode }) {
  const lines = (latestEpisode?.reasoning ?? "").split(/\n+/).filter(Boolean);

  return (
    <div className="lg:col-span-8 border border-outline-variant bg-surface-container-lowest p-6 font-data-mono text-xs overflow-hidden h-72 flex flex-col">
      <div className="flex justify-between mb-4 border-b border-outline-variant pb-3">
        <div className="flex items-center gap-3">
          <span className="text-primary font-label-caps text-[11px] tracking-widest">LIVE_REASONING_STREAM</span>
          {latestEpisode && (
            <span className="text-[9px] text-on-surface-variant border border-outline-variant px-1 font-data-mono uppercase">
              TARGET:{latestEpisode.target}
            </span>
          )}
        </div>
        <div className="flex gap-4">
          <span className="text-[9px] uppercase text-on-surface-variant">TLS_VERIFIED</span>
          <span className="text-[9px] uppercase text-primary">NODE:01_STABLE</span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto space-y-1 text-on-surface-variant">
        {!latestEpisode && <p className="text-on-surface-variant">No scored episodes yet.</p>}

        {latestEpisode && lines.length === 0 && (
          <span className="text-on-surface-variant opacity-60">(no reasoning string recorded for this episode)</span>
        )}

        {lines.map((line, i) => {
          const cls = colorForLine(line);
          return cls ? (
            <div key={i}>
              <span className={cls}>{line}</span>
            </div>
          ) : (
            <div key={i}>{line}</div>
          );
        })}

        {latestEpisode?.action_result && (
          <div className={latestEpisode.trust_correct ? "text-green-400" : "text-error"}>
            VERDICT :: {latestEpisode.snapshot_durability_verdict ?? latestEpisode.scores_durability_verdict ?? "n/a"}
            <span className="terminal-cursor" />
          </div>
        )}
      </div>
    </div>
  );
}

// Derived presentation of the real reasoning text, not synthetic data --
// splits the agent's own recorded reasoning into up to 3 labeled steps so
// it reads as a logic cycle rather than a raw paragraph.
function LogicCycle({ latestEpisode }) {
  const STEP_LABELS = ["OBSERVATION", "HYPOTHESIS", "EXECUTION"];
  const sentences = (latestEpisode?.reasoning ?? "")
    .split(/(?<=[.!])\s+|\n+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 3);

  return (
    <div className="lg:col-span-4 border border-outline-variant bg-surface-container-low p-6 flex flex-col gap-6">
      <h3 className="font-label-caps text-[10px] text-on-surface-variant tracking-widest">CURRENT_LOGIC_CYCLE</h3>

      {sentences.length === 0 ? (
        <p className="font-data-mono text-[11px] text-on-surface-variant opacity-60">—</p>
      ) : (
        <div className="space-y-6 relative before:absolute before:left-1 before:top-2 before:bottom-2 before:w-px before:bg-outline-variant">
          {sentences.map((s, i) => {
            const isLast = i === sentences.length - 1;
            return (
              <div key={i} className="relative pl-6">
                <div
                  className={`absolute left-0 top-1.5 w-2.5 h-2.5 border-2 border-surface-container-low ${
                    isLast ? "bg-surface-variant border-outline-variant" : "bg-primary"
                  }`}
                />
                <div className={`font-label-caps text-[9px] mb-1 ${isLast ? "text-on-surface-variant" : "text-primary"}`}>
                  {String(i + 1).padStart(2, "0")}_{STEP_LABELS[i] ?? "STEP"}
                </div>
                <p className={`font-data-mono text-[11px] leading-tight ${isLast ? "text-on-surface-variant opacity-70" : "text-on-surface"}`}>
                  {s}
                </p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function ReasoningPanel({ latestEpisode }) {
  return (
    <section className="grid grid-cols-1 lg:grid-cols-12 gap-6 mt-8">
      <ReasoningStream latestEpisode={latestEpisode} />
      <div className="lg:col-span-4 flex flex-col gap-6">
        <LogicCycle latestEpisode={latestEpisode} />
        <SecurityCage />
      </div>
    </section>
  );
}
