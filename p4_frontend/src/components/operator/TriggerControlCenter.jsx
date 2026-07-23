const NOT_YET_TRIGGERABLE = [{ label: "Network Latency" }];
// oom/disk-full moved out of this list 2026-07-24 (Phase B) -- both are
// now real, live-triggerable classes (injector.py + p3_agent.py's
// ACTION_MAP already fully supported them; the only gap was
// operator_api.py's IMPLEMENTED_CLASSES). network-latency stays here --
// it's report-only by design (no fix action exists), and wiring it into
// live /trigger was never part of this pass.

function FaultCard({
  faultClass,
  token,
  role,
  safeDemoClasses,
  triggerStatus,
  running,
  resolving,
  awaitingFix,
  onTrigger,
  onResolve,
}) {
  const cooldownActive = (triggerStatus?.your_cooldown_remaining_s ?? 0) > 0;
  const blocked = triggerStatus?.episode_in_flight || cooldownActive;

  // awaiting-fix and running BOTH take priority over "blocked" -- once
  // THIS card's fault has landed, its button must become "Diagnose &
  // Fix" regardless of cooldown/cap state; and while THIS card's own
  // inject call is in flight, it must keep showing its own "INJECTING…"
  // state, not "EPISODE IN FLIGHT" -- even though episode_in_flight is
  // now optimistically true the instant the click fires (2026-07-24 fix,
  // see Operator/index.jsx's effectiveTriggerStatus), that flag is true
  // BECAUSE of this card's own click, not a reason to grey ITSELF out.
  // It's still the correct reason to grey out every OTHER card.
  let state = "active";
  if (awaitingFix) state = "awaiting-fix";
  else if (running) state = "active";
  else if (!token) state = "locked";
  else if (role === "demo-trigger" && !safeDemoClasses.includes(faultClass)) state = "admin-only";
  else if (blocked) state = "cooldown";

  const label = faultClass.toUpperCase().replaceAll("-", "_");

  if (state === "locked") {
    return (
      <div className="relative overflow-hidden border border-outline bg-surface-container-low p-4 flex flex-col gap-4 group">
        <div className="absolute inset-0 bg-surface/80 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity z-10">
          <span className="font-label-caps text-[11px] text-primary">LOGIN REQUIRED</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="font-bold">{label}</span>
          <span className="material-symbols-outlined text-outline text-sm">lock</span>
        </div>
        <button disabled className="w-full py-2 border border-outline text-on-surface-variant font-label-caps text-[11px]">
          LOGIN REQUIRED
        </button>
      </div>
    );
  }

  if (state === "admin-only") {
    return (
      <div className="relative overflow-hidden border border-outline bg-surface-container-low p-4 flex flex-col gap-4 group">
        <div className="absolute inset-0 bg-surface/80 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity z-10">
          <span className="font-label-caps text-[11px] text-primary">ADMIN ACCESS ONLY</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="font-bold">{label}</span>
          <span className="material-symbols-outlined text-outline text-sm">lock</span>
        </div>
        <button disabled className="w-full py-2 border border-outline text-on-surface-variant font-label-caps text-[11px]">
          ADMIN ACCESS ONLY
        </button>
      </div>
    );
  }

  if (state === "cooldown") {
    return (
      <div className="border border-outline bg-surface-container-low p-4 flex flex-col gap-4 opacity-60">
        <div className="flex items-center justify-between">
          <span className="font-bold">{label}</span>
          <span className="material-symbols-outlined text-on-surface-variant text-sm">timer</span>
        </div>
        <button disabled className="w-full py-2 border border-outline text-on-surface-variant font-label-caps text-[11px]">
          {triggerStatus?.episode_in_flight
            ? "EPISODE IN FLIGHT"
            : `COOLDOWN (${triggerStatus.your_cooldown_remaining_s}s)`}
        </button>
      </div>
    );
  }

  if (state === "awaiting-fix") {
    // A real fault this card just injected has landed and is waiting for
    // the user to confirm it on the frontend and deliberately click to
    // fix it -- distinct border color so it's visually unmistakable which
    // card is actionable right now, especially since every other card is
    // simultaneously locked by the system-wide episode_in_flight guard.
    return (
      <div className="border-2 border-[#238636] bg-[#238636]/5 p-4 flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <span className="font-bold">{label}</span>
          <span className="material-symbols-outlined text-[#238636] text-sm">bug_report</span>
        </div>
        <button
          onClick={() => onResolve(faultClass)}
          disabled={resolving}
          className="w-full py-2 bg-[#238636] text-white font-label-caps text-[11px] font-bold hover:brightness-110 transition-all disabled:opacity-60"
        >
          {resolving ? "DIAGNOSING… (can take a few minutes)" : "DIAGNOSE & FIX"}
        </button>
      </div>
    );
  }

  return (
    <div className="border border-primary bg-primary/5 p-4 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <span className="font-bold">{label}</span>
        <span className="material-symbols-outlined text-primary text-sm">check_circle</span>
      </div>
      <button
        onClick={() => onTrigger(faultClass)}
        disabled={running}
        className="w-full py-2 bg-primary text-on-primary font-label-caps text-[11px] font-bold hover:brightness-110 transition-all disabled:opacity-60"
      >
        {running ? "INJECTING…" : "TRIGGER INJECTION"}
      </button>
    </div>
  );
}

export default function TriggerControlCenter({
  triggerableClasses,
  token,
  role,
  safeDemoClasses,
  triggerStatus,
  triggeringClass,
  awaitingFixClass,
  resolvingClass,
  onTrigger,
  onResolve,
}) {
  return (
    <div className="bg-surface-container border border-outline">
      <div className="border-b border-outline p-4 flex items-center justify-between">
        <h2 className="font-headline-md text-lg">Trigger Control Center</h2>
      </div>

      <div className="p-6">
        <div className="mb-8">
          <p className="font-label-caps text-[11px] text-on-surface-variant mb-4">IMPLEMENTED FAULTS</p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            {triggerableClasses.map((fc) => (
              <FaultCard
                key={fc}
                faultClass={fc}
                token={token}
                role={role}
                safeDemoClasses={safeDemoClasses}
                triggerStatus={triggerStatus}
                running={triggeringClass === fc}
                resolving={resolvingClass === fc}
                awaitingFix={awaitingFixClass === fc}
                onTrigger={onTrigger}
                onResolve={onResolve}
              />
            ))}
          </div>
        </div>

        <div>
          <p className="font-label-caps text-[11px] text-on-surface-variant mb-4">
            NOT YET TRIGGERABLE FROM THIS DASHBOARD
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            {NOT_YET_TRIGGERABLE.map((c) => (
              <div key={c.label} className="border border-outline-variant border-dashed p-4 flex flex-col gap-4 opacity-40">
                <span>{c.label}</span>
                <div className="h-8 bg-outline-variant/20" />
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
