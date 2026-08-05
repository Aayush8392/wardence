// Real, continuous scrub -- not staged/beat-based. `milestones` are the
// schedule's own unit start times (replaySchedule.js), used only for the
// skip forward/back buttons; the drag itself is free continuous seeking.
export default function ReplaySeekbar({ engine, milestones }) {
  const { elapsed, totalDuration, playing, speed, canSpeedUp, canSpeedDown } = engine;
  const pct = totalDuration > 0 ? (elapsed / totalDuration) * 100 : 0;

  function skipForward() {
    const next = milestones.find((m) => m > elapsed + 0.01);
    engine.seek(next ?? totalDuration);
  }
  function skipBack() {
    const prev = [...milestones].reverse().find((m) => m < elapsed - 0.01);
    engine.seek(prev ?? 0);
  }

  return (
    <div className="flex items-center gap-3 border border-outline-variant bg-surface-container-lowest px-3 py-2">
      <button
        onClick={skipBack}
        className="w-7 h-7 flex items-center justify-center text-on-surface-variant hover:text-primary"
        title="Previous milestone"
      >
        <span className="material-symbols-outlined text-lg">skip_previous</span>
      </button>

      <button
        onClick={engine.toggle}
        className="w-8 h-8 flex items-center justify-center border border-primary text-primary hover:bg-primary/10"
      >
        <span className="material-symbols-outlined text-lg">{playing ? "pause" : "play_arrow"}</span>
      </button>

      <button
        onClick={skipForward}
        className="w-7 h-7 flex items-center justify-center text-on-surface-variant hover:text-primary"
        title="Next milestone"
      >
        <span className="material-symbols-outlined text-lg">skip_next</span>
      </button>

      <input
        type="range"
        min={0}
        max={totalDuration}
        step={0.01}
        value={elapsed}
        onChange={(e) => engine.seek(Number(e.target.value))}
        className="flex-1 accent-primary"
        style={{
          background: `linear-gradient(to right, var(--color-primary) ${pct}%, var(--color-outline-variant) ${pct}%)`,
        }}
      />

      <div className="flex items-center gap-1 shrink-0">
        <button
          onClick={engine.speedDown}
          disabled={!canSpeedDown}
          className="w-6 h-6 flex items-center justify-center text-on-surface-variant hover:text-primary disabled:opacity-30 disabled:hover:text-on-surface-variant"
        >
          <span className="material-symbols-outlined text-sm">remove</span>
        </button>
        <span className="font-data-mono text-xs text-on-surface w-9 text-center">{speed}x</span>
        <button
          onClick={engine.speedUp}
          disabled={!canSpeedUp}
          className="w-6 h-6 flex items-center justify-center text-on-surface-variant hover:text-primary disabled:opacity-30 disabled:hover:text-on-surface-variant"
        >
          <span className="material-symbols-outlined text-sm">add</span>
        </button>
      </div>
    </div>
  );
}
