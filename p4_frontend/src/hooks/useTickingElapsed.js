import { useEffect, useRef, useState } from "react";

// Shared by TopologyMap's Bead and CentralThinkingHub -- both need to
// show a real elapsed/remaining-time count that ticks smoothly between
// the real, server-computed `elapsed_in_state_s` values that only arrive
// once per live-status poll (every 4s). Real base value, interpolated
// client-side for the seconds in between -- never a purely local guess
// (every resync snaps back to whatever the server most recently said,
// so drift never accumulates beyond one poll interval).
export function useTickingElapsed(baseSeconds, active) {
  const baseRef = useRef({ value: null, receivedAt: null });
  const [, forceTick] = useState(0);

  useEffect(() => {
    if (baseSeconds != null) {
      baseRef.current = { value: baseSeconds, receivedAt: performance.now() };
    }
  }, [baseSeconds]);

  useEffect(() => {
    if (!active) return undefined;
    const id = setInterval(() => forceTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [active]);

  if (baseRef.current.value == null) return null;
  return Math.round(baseRef.current.value + (performance.now() - baseRef.current.receivedAt) / 1000);
}
