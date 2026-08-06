import { useRef } from "react";

// Real, continuous scrub -- not staged/beat-based. `milestones` are the
// schedule's own unit start times (replaySchedule.js), used only for the
// skip forward/back buttons; the drag itself is free continuous seeking.
export default function ReplaySeekbar({ engine, milestones }) {
  const { elapsed, totalDuration, playing, speed, canSpeedUp, canSpeedDown } = engine;
  const pct = totalDuration > 0 ? (elapsed / totalDuration) * 100 : 0;

  // Slightly larger epsilon than a raw float-precision guard (0.01) --
  // real milestones from ExecutionPhaseTrack's own buildTrack can legally
  // land very close together (e.g. SEMANTIC_VAL right before the first
  // real step), and Framer Motion's own `elapsed` after a seek isn't
  // always bit-exact with the target passed to it.
  const SKIP_EPSILON = 0.05;

  // Real bug found live: seek() alone never pauses playback -- jumping
  // backward while still playing snapped the position, then playback just
  // kept rolling forward and blew straight past that same milestone again
  // within a fraction of a second, LOOKING like skip-back couldn't go past
  // an already-completed milestone when it actually could, it just
  // immediately un-did it. Only skip-BACK pauses to fix this -- skip-
  // forward has no such problem (continuing to play forward from a
  // forward jump is the natural direction, nothing to undo) and should
  // keep playing uninterrupted, per explicit ask.
  function skipForward() {
    const next = milestones.find((m) => m > elapsed + SKIP_EPSILON);
    engine.seek(next ?? totalDuration);
  }
  function skipBack() {
    const prev = [...milestones].reverse().find((m) => m < elapsed - SKIP_EPSILON);
    engine.seek(prev ?? 0);
    engine.pause();
  }

  // Real click-vs-drag distinction on the slider, per explicit ask: a
  // plain click should jump and keep playing (unchanged); an actual
  // click-and-HOLD drag should pause immediately and stay paused until
  // Play is pressed again. The native <input type=range> fires the same
  // onChange for both, so intent is tracked separately via pointer events
  // -- `draggedRef` only flips true once real pointer MOVEMENT is
  // detected while the button is held, not on pointerdown alone (a plain
  // click is a zero-movement press+release).
  const wasPlayingRef = useRef(false);
  const draggedRef = useRef(false);

  function handlePointerDown() {
    wasPlayingRef.current = playing;
    draggedRef.current = false;
  }
  function handlePointerMove() {
    if (!draggedRef.current) {
      draggedRef.current = true;
      engine.pause();
    }
  }
  function handlePointerUp() {
    // Only a genuine drag pauses permanently (stays paused, matching the
    // ask). A plain click never paused in the first place, so nothing to
    // resume -- but if it happened to interrupt active playback via a
    // fast double-input, restore it.
    if (!draggedRef.current && wasPlayingRef.current) engine.play();
    draggedRef.current = false;
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
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
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
