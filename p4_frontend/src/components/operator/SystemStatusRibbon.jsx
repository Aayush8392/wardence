// Heuristic range for the ONE known, already-documented cause of errors in
// this system (front-end's null address/card lookup on /orders and /cart,
// see wardence_buildlog.md's network-latency validation notes) -- not a
// hardcoded label. Outside this range, the annotation is honestly omitted
// rather than misattributing a different, unexplained problem to a bug
// that isn't necessarily the cause that time.
const KNOWN_BUG_MIN = 0.1;
const KNOWN_BUG_MAX = 0.5;

export default function SystemStatusRibbon({ status }) {
  if (!status) return null;

  const errorPct = status.error_rate * 100;
  const matchesKnownBug = status.error_rate >= KNOWN_BUG_MIN && status.error_rate <= KNOWN_BUG_MAX;

  const running = status.pods_by_phase?.Running ?? 0;
  const failed = status.pods_by_phase?.Failed ?? 0;

  // No own grid wrapper -- rendered as siblings inside Operator's shared
  // top status grid (alongside TriggerBudget), per explicit ask to move
  // trigger budget up next to pod health/etc. rather than a separate row.
  return (
    <>
      <div className="bg-surface-container border border-outline p-4 flex flex-col justify-between">
        <p className="font-label-caps text-[11px] text-on-surface-variant mb-2">REQUEST RATE</p>
        <div className="flex items-baseline gap-2">
          <span className="font-data-mono text-3xl text-primary">{status.request_rate_per_s}</span>
          <span className="font-label-caps text-[11px] text-on-surface-variant">REQ/S</span>
        </div>
      </div>

      <div className="bg-surface-container border border-error/30 p-4 flex flex-col justify-between">
        <p className="font-label-caps text-[11px] text-error mb-2">ERROR RATE</p>
        <div className="flex items-baseline gap-2">
          <span className="font-data-mono text-3xl text-error">{errorPct.toFixed(1)}%</span>
          {matchesKnownBug && (
            <span className="font-body-sm text-[11px] text-on-surface-variant">
              consistent with known front-end null-lookup bug
            </span>
          )}
        </div>
      </div>

      <div className="bg-surface-container border border-outline p-4 flex flex-col justify-between">
        <p className="font-label-caps text-[11px] text-on-surface-variant mb-2">POD HEALTH</p>
        <div className="flex items-center justify-between">
          <div className="flex items-baseline gap-1">
            <span className="font-data-mono text-3xl">{running}</span>
            <span className="font-label-caps text-[11px] text-[#238636]">RUNNING</span>
          </div>
          <div className="h-8 w-px bg-outline-variant" />
          <div className="flex items-baseline gap-1">
            <span className="font-data-mono text-3xl text-error">{failed}</span>
            <span className="font-label-caps text-[11px] text-error">FAILED</span>
          </div>
        </div>
      </div>
    </>
  );
}
