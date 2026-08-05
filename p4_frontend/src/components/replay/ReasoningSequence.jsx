import ReasoningStream from "./ReasoningStream";

// Real transcript_lines shape (p2_readonly_loop/react_agent.py's
// _run_episode_with_provider): a flat array of strings, alternating
// "[Turn N] Action: call_tool <tool>" / "Result: <json>" / occasional
// "System: <nudge>" lines. The model's final diagnosis is NEVER appended
// to this array (the loop returns immediately on `action: "diagnose"`) --
// it lives separately in episode.reasoning, same field the lite tier
// already uses. Grouping by the "[Turn" marker turns the flat log back
// into real per-turn cards without inventing any structure that isn't there.
function parseTranscript(lines) {
  const turns = [];
  let current = null;
  for (const line of lines) {
    const m = line.match(/^\[Turn (\d+)\] Action: (.*)$/);
    if (m) {
      if (current) turns.push(current);
      current = { turn: Number(m[1]), action: m[2], extra: [] };
    } else if (current) {
      current.extra.push(line);
    }
  }
  if (current) turns.push(current);
  return turns;
}

function formatAction(action) {
  const m = action.match(/^call_tool (\S+)$/);
  if (m) return { kind: "tool", tool: m[1] };
  return { kind: "raw", text: action };
}

function TurnCard({ turn, isLast }) {
  const parsed = formatAction(turn.action);
  const resultLine = turn.extra.find((l) => l.startsWith("Result:"));
  const systemLine = turn.extra.find((l) => l.startsWith("System:"));

  return (
    <div className="relative">
      <div
        className={`absolute -left-[19px] w-6 h-6 rounded-full flex items-center justify-center z-10 border ${
          isLast ? "bg-primary border-primary shadow-[0_0_8px_rgba(88,166,255,0.6)]" : "bg-surface border-primary/60"
        }`}
      >
        <span className={`font-data-mono text-[10px] ${isLast ? "text-on-primary" : "text-primary"}`}>
          {String(turn.turn).padStart(2, "0")}
        </span>
      </div>
      <div className={`p-3 rounded-sm border ${isLast ? "bg-primary/5 border-primary/40" : "bg-surface-container-low border-outline-variant/30"}`}>
        <div className="flex gap-2 items-start mb-1">
          <span className="font-label-caps text-[10px] text-primary shrink-0 w-16">ACTION:</span>
          {parsed.kind === "tool" ? (
            <code className="font-data-mono text-xs text-correct-green bg-correct-green/10 px-1.5 py-0.5 rounded-sm">
              {parsed.tool}
            </code>
          ) : (
            <span className="font-data-mono text-xs text-on-surface-variant">{parsed.text}</span>
          )}
        </div>
        {resultLine && (
          <div className="flex gap-2 items-start">
            <span className="font-label-caps text-[10px] text-outline shrink-0 w-16">OBSERVE:</span>
            <span className="font-data-mono text-xs text-on-surface-variant break-all">
              {resultLine.replace(/^Result:\s*/, "")}
            </span>
          </div>
        )}
        {systemLine && (
          <div className="flex gap-2 items-start mt-1">
            <span className="font-label-caps text-[10px] text-warning-amber shrink-0 w-16">SIGNAL:</span>
            <span className="font-data-mono text-xs text-warning-amber/90">
              {systemLine.replace(/^System:\s*/, "")}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

function DecisionCard({ episode }) {
  const sentence = (episode.reasoning ?? "").trim();
  return (
    <div className="relative">
      <div className="absolute -left-[19px] w-6 h-6 rounded-full bg-primary border border-primary flex items-center justify-center z-10 shadow-[0_0_12px_rgba(88,166,255,0.6)]">
        <span className="material-symbols-outlined text-[14px] text-on-primary">flag</span>
      </div>
      <div className="bg-primary/10 border border-primary/50 p-3 rounded-sm">
        <div className="font-label-caps text-[10px] text-primary mb-1">DECISION</div>
        <div className="font-data-mono text-xs text-on-surface">
          {sentence || "No reasoning sentence captured for this episode."}
        </div>
        <div className="flex gap-4 mt-2 font-data-mono text-[11px] text-on-surface-variant">
          <span>PREDICTED: <span className="text-on-surface">{episode.predicted_class ?? "n/a"}</span></span>
          <span>CONFIDENCE: <span className="text-on-surface">{episode.score_confidence?.toFixed(2) ?? "n/a"}</span></span>
        </div>
      </div>
    </div>
  );
}

// Real, honest tier split (locked 2026-08-05): enriched (real transcript_json,
// new episodes only) vs. lite (flat tool_output + one reasoning sentence, every
// older episode). Never assume every episode has a transcript.
export default function ReasoningSequence({ episode, revealCount }) {
  const transcript = episode.transcript;

  if (!transcript || transcript.length === 0) {
    return <ReasoningStream episode={episode} />;
  }

  const turns = parseTranscript(transcript);
  const visibleTurns = revealCount != null ? turns.slice(0, Math.max(0, revealCount)) : turns;
  const showDecision = revealCount == null || revealCount > turns.length;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="font-label-caps text-xs text-on-surface-variant flex items-center gap-2">
          <span className="material-symbols-outlined text-sm">psychology</span> REASONING_SEQUENCE
        </h2>
        {episode.provider && (
          <span className="font-data-mono text-[10px] text-on-surface-variant">
            {episode.model ?? episode.provider} / {episode.provider}
            {episode.tier ? ` · ${episode.tier}` : ""}
          </span>
        )}
      </div>
      <div className="relative pl-6 space-y-4">
        <div className="absolute left-[7px] top-2 bottom-2 w-px bg-primary/30 border-l border-dashed border-primary/50" />
        {visibleTurns.map((t, i) => (
          <TurnCard key={t.turn} turn={t} isLast={i === visibleTurns.length - 1 && !showDecision} />
        ))}
        {showDecision && <DecisionCard episode={episode} />}
      </div>
    </div>
  );
}
