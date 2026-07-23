import { useEffect, useState } from "react";

// Real countdown -- not the mockup's fake hardcoded 48s timer. Ticks down
// client-side from the real your_cooldown_remaining_s (operator_api.py's
// /trigger/status), resyncing whenever a fresh status object arrives
// (new fetch, or right after triggering something) rather than drifting
// forever from one snapshot.
export default function TriggerBudget({ status }) {
  const [cooldown, setCooldown] = useState(status?.your_cooldown_remaining_s ?? 0);

  // Resync when a fresh status object arrives -- adjusting state during
  // render (comparing against the previous prop) rather than in an effect
  // body, per React's recommended pattern for "state derived from a prop
  // that should reset when the prop changes."
  const [syncedStatus, setSyncedStatus] = useState(status);
  if (status !== syncedStatus) {
    setSyncedStatus(status);
    setCooldown(status?.your_cooldown_remaining_s ?? 0);
  }

  useEffect(() => {
    if (cooldown <= 0) return;
    const id = setInterval(() => setCooldown((s) => Math.max(s - 1, 0)), 1000);
    return () => clearInterval(id);
  }, [cooldown]);

  if (!status) return null;

  const usedToday = status.global_cap - status.global_remaining_today;
  const usedPct = status.global_cap > 0 ? (usedToday / status.global_cap) * 100 : 0;

  return (
    <div className="bg-surface-container border border-outline">
      <div className="p-6">
        <p className="font-label-caps text-[11px] text-primary mb-2">TRIGGER BUDGET</p>
        <h3 className="font-data-mono text-3xl mb-4">
          {status.global_remaining_today} / {status.global_cap}{" "}
          <span className="text-on-surface-variant text-sm font-body-sm">REMAINING</span>
        </h3>

        <div className="h-3 w-full bg-outline-variant/30 mb-6">
          <div className="h-full bg-primary" style={{ width: `${usedPct}%` }} />
        </div>

        <div className="space-y-3">
          <div className="flex items-center justify-between border-t border-outline-variant/50 pt-3">
            <span className="font-body-sm text-on-surface-variant">GLOBAL LIMIT</span>
            <span className="font-data-mono">{status.global_cap} / 24H</span>
          </div>
          <div className="flex items-center justify-between bg-primary/5 p-3 border border-primary/20">
            <span className="font-body-sm text-primary">YOUR IP COOLDOWN</span>
            <span className={`font-data-mono font-bold ${cooldown > 0 ? "text-primary" : "text-[#238636]"}`}>
              {cooldown > 0 ? `${cooldown}s` : "READY"}
            </span>
          </div>
          {status.episode_in_flight && (
            <p className="text-[11px] text-on-surface-variant font-data-mono">
              An episode is currently in flight system-wide (one-at-a-time rule).
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
