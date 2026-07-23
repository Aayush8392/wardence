// Staggered jump/fade dots for inline busy-state indicators -- replaces a
// static "…" with a genuinely animated cue (see index.css's .loading-dot /
// dot-jump keyframe). Color inherits from the parent (currentColor), so it
// reads correctly on both the promote (white-on-green) and demote
// (white-on-red) buttons without a separate prop.
export default function LoadingDots() {
  return (
    <span className="inline-flex items-center gap-0.5" aria-label="Loading">
      <span className="loading-dot" />
      <span className="loading-dot" />
      <span className="loading-dot" />
    </span>
  );
}
