import { useEffect, useState } from "react";
import NumberFlow from "@number-flow/react";

// NumberFlow only animates CHANGES to its `value` prop -- the first
// render just displays whatever value it's given, with nothing to
// transition from. To get the "counts up from 0 on load" effect
// everywhere, start at 0 and flip to the real value right after mount
// so NumberFlow sees a real transition, not just an initial paint.
// Slower than NumberFlow's own default (~750ms) -- this project's own
// feedback was "too many numbers animating at once feels laggy," so
// this component is now reserved for a small handful of headline
// numbers, each given a slightly more deliberate, calmer count-up.
const SLOW_TIMING = { duration: 1400, easing: "ease-out" };

export default function AnimatedNumber({ value, format, delay = 0 }) {
  const [displayValue, setDisplayValue] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => setDisplayValue(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return (
    <NumberFlow
      value={displayValue}
      format={format}
      transformTiming={SLOW_TIMING}
      spinTiming={SLOW_TIMING}
    />
  );
}
