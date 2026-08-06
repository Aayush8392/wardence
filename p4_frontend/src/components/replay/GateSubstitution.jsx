// Real, honest record of a safety-veto override -- p3_agent.py's
// _dispatch_with_gate only ever sets this when the LLM's proposed tool
// disagreed with the deterministic mapping for the real fault class, and a
// safer deterministic action was dispatched instead. None on every other
// episode -- shown only when it actually happened, never a fabricated
// "what if" scenario.
// `reasonText` optionally overrides the REAL per-episode reason string
// (Replay passes a partially-typed slice of it) -- the fixed instructional
// sentence above it is static UI copy, not per-episode data, so it isn't
// typewritten, same distinction already used for header/section labels.
export default function GateSubstitution({ substitution, reasonText, opacity = 1, active = false }) {
  if (!substitution) return null;

  const { proposed_tool, proposed_params, substituted_tool, substituted_params, reason } = substitution;

  return (
    <div className={`border border-warning-amber/50 bg-warning-amber/5 p-4 relative ${active ? "content-live-glow" : ""}`} style={{ opacity }}>
      <div className="absolute top-0 left-3 -translate-y-1/2 px-2 py-0.5 bg-surface-variant font-label-caps text-[9px] text-warning-amber">
        SAFETY OVERRIDE
      </div>
      <p className="font-data-md text-xs text-on-surface-variant leading-relaxed mb-3">
        The proposed action didn't match what the real diagnosis calls for, so a safer, deterministic action was
        dispatched instead.
      </p>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <div className="font-label-caps text-[9px] text-on-surface-variant mb-1">PROPOSED</div>
          <div className="font-data-mono text-xs text-on-surface">{proposed_tool}</div>
          {proposed_params && (
            <div className="font-data-mono text-[10px] text-on-surface-variant/70 mt-0.5">
              {JSON.stringify(proposed_params)}
            </div>
          )}
        </div>
        <div>
          <div className="font-label-caps text-[9px] text-correct-green mb-1">DISPATCHED</div>
          <div className="font-data-mono text-xs text-on-surface">{substituted_tool}</div>
          {substituted_params && (
            <div className="font-data-mono text-[10px] text-on-surface-variant/70 mt-0.5">
              {JSON.stringify(substituted_params)}
            </div>
          )}
        </div>
      </div>
      {reason && (
        <div className="mt-3 pt-3 border-t border-outline-variant/30">
          <div className="font-label-caps text-[9px] text-on-surface-variant mb-1">REASON</div>
          <div className="font-data-md text-xs text-on-surface-variant">{reasonText ?? reason}</div>
        </div>
      )}
    </div>
  );
}
