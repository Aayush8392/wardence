import { useEffect, useState } from "react";

// Compact tile version -- moved from a tall sidebar card into the top
// status strip alongside SystemStatusRibbon, per explicit ask (topology
// gets the full width now). Real countdown, not a fake hardcoded timer --
// ticks down client-side from the real your_cooldown_remaining_s
// (operator_api.py's /trigger/status), resyncing whenever a fresh status
// object arrives rather than drifting forever from one snapshot.
export default function TriggerBudget({ status }) {
  const [cooldown, setCooldown] = useState(status?.your_cooldown_remaining_s ?? 0);

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
    <div className="bg-surface-container border border-outline p-4 flex flex-col justify-between">
      <p className="font-label-caps text-[11px] text-primary mb-2">TRIGGER BUDGET</p>
      <div className="flex items-baseline gap-2 mb-2">
        <span className="font-data-mono text-3xl">{status.global_remaining_today}</span>
        <span className="font-label-caps text-[11px] text-on-surface-variant">/ {status.global_cap} REMAINING</span>
      </div>
      <div className="h-1.5 w-full bg-outline-variant/30 mb-2">
        <div className="h-full bg-primary" style={{ width: `${usedPct}%` }} />
      </div>
      <div className="flex items-center justify-between font-data-mono text-[10px]">
        <span className="text-on-surface-variant">YOUR COOLDOWN</span>
        <span className={cooldown > 0 ? "text-primary font-bold" : "text-[#238636] font-bold"}>
          {cooldown > 0 ? `${cooldown}s` : "READY"}
        </span>
      </div>
    </div>
  );
}
