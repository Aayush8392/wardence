const VERDICT_STYLE = {
  confirmed: { border: "#238636", color: "text-[#238636]", icon: "verified" },
  flapped: { border: "#93000a", color: "text-error", icon: "warning" },
};

// opacity optional (default 1, Snapshot's own call site is unaffected).
// `active` (Replay only) -- pulsating blue ring while this block is the one
// currently fading in.
export default function DurabilityVerdict({ verdict, elapsedS, opacity = 1, active = false }) {
  if (!verdict) return null;
  const style = VERDICT_STYLE[verdict] ?? { border: "#8b919d", color: "text-on-surface-variant", icon: "help" };

  return (
    <div
      className={`bg-surface-container-high border-l-4 p-4 flex justify-between items-center ${active ? "content-live-glow" : ""}`}
      style={{ borderLeftColor: style.border, opacity }}
    >
      <div>
        <div className={`font-label-caps text-[10px] font-bold ${style.color}`}>DURABILITY VERDICT</div>
        <div className="font-display-lg text-xl tracking-tight uppercase">{verdict}</div>
        {elapsedS != null && (
          <div className="font-data-mono text-[11px] text-on-surface-variant">
            {elapsedS}s stabilization window passed.
          </div>
        )}
      </div>
      <span className={`material-symbols-outlined text-3xl ${style.color}`}>{style.icon}</span>
    </div>
  );
}
