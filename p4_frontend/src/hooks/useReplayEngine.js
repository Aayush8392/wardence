import { useEffect, useRef, useState } from "react";
import { useMotionValue, animate } from "motion/react";

// Real playback speeds, locked -- default 1x, +/- steps through this list
// and disables at either end (never wraps).
export const SPEED_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];

// Drives one motion value (elapsed seconds) via Framer Motion's real
// AnimationPlaybackControls (confirmed against the installed motion-dom
// types: .time is a directly settable seek, .speed is a real 1=normal
// playback-rate multiplier) instead of hand-rolling a requestAnimationFrame
// loop + delta*speed math ourselves -- same library already used elsewhere
// in this project (Bklit UI's chart components).
export default function useReplayEngine(totalDuration) {
  const elapsedMV = useMotionValue(0);
  const controlsRef = useRef(null);
  const [playing, setPlaying] = useState(false);
  const [speedIndex, setSpeedIndex] = useState(SPEED_STEPS.indexOf(1));
  const [elapsed, setElapsed] = useState(0);

  // Mirror the motion value into real React state so consumers (the
  // seekbar, the schedule renderer) re-render on change -- useMotionValue
  // intentionally does NOT trigger React renders on its own.
  useEffect(() => {
    const unsub = elapsedMV.on("change", (v) => setElapsed(v));
    return unsub;
  }, [elapsedMV]);

  useEffect(() => {
    // Fresh episode (new totalDuration) -- reset to a clean start rather
    // than carrying over a stale controls instance from the last episode.
    controlsRef.current?.stop();
    elapsedMV.set(0);
    setPlaying(false);
    return () => controlsRef.current?.stop();
  }, [totalDuration, elapsedMV]);

  function ensureControls() {
    if (controlsRef.current) return controlsRef.current;
    const controls = animate(elapsedMV, totalDuration, {
      duration: totalDuration,
      ease: "linear",
      onComplete: () => setPlaying(false),
    });
    controls.speed = SPEED_STEPS[speedIndex];
    controls.pause(); // start paused -- caller decides when to actually play
    controlsRef.current = controls;
    return controls;
  }

  function play() {
    const controls = ensureControls();
    if (elapsedMV.get() >= totalDuration) {
      controls.time = 0; // real replay of a fully-watched episode restarts from 0
    }
    controls.play();
    setPlaying(true);
  }

  function pause() {
    controlsRef.current?.pause();
    setPlaying(false);
  }

  function toggle() {
    if (playing) pause();
    else play();
  }

  // Direct seek -- render state elsewhere is a pure function of elapsed
  // (replaySchedule.js's fadeProgress/textProgress), so no special un-type logic is
  // needed here for a backward drag; it just falls out of the same math.
  function seek(t) {
    const clamped = Math.max(0, Math.min(totalDuration, t));
    const controls = ensureControls();
    controls.time = clamped;
    if (!playing) controls.pause(); // setting .time on a paused-then-created controls can auto-resume; force it back off
  }

  function setSpeed(nextIndex) {
    const clamped = Math.max(0, Math.min(SPEED_STEPS.length - 1, nextIndex));
    setSpeedIndex(clamped);
    if (controlsRef.current) controlsRef.current.speed = SPEED_STEPS[clamped];
  }

  return {
    elapsed,
    totalDuration,
    playing,
    speed: SPEED_STEPS[speedIndex],
    speedIndex,
    canSpeedUp: speedIndex < SPEED_STEPS.length - 1,
    canSpeedDown: speedIndex > 0,
    play,
    pause,
    toggle,
    seek,
    speedUp: () => setSpeed(speedIndex + 1),
    speedDown: () => setSpeed(speedIndex - 1),
  };
}
