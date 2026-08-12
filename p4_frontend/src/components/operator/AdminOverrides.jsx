import LoadingDots from "../shared/LoadingDots";
import { CLASS_LABELS } from "../../constants/faultClasses";

export default function AdminOverrides({ autoFixClasses, trustMap, highlightClass, busyClass, onPromote, onDemote }) {
  return (
    <div className="bg-surface-container border border-outline overflow-hidden">
      <div className="bg-error-container/10 border-b border-error-container/30 p-4 flex items-center gap-3">
        <span className="material-symbols-outlined text-error">admin_panel_settings</span>
        <h2 className="font-headline-md text-lg">Admin Trust Overrides</h2>
      </div>

      <div className="p-6 space-y-4">
        {autoFixClasses.map((fc) => {
          const state = trustMap[fc] ?? "unknown";
          // Proactive disabling, matching the guards added to /promote and
          // /demote in operator_api.py (2026-07-24) -- an already-can_act
          // class has nothing to promote, an already-report_only class has
          // nothing to demote. Falls back to enabled (not disabled) while
          // trustMap hasn't loaded yet ("unknown") or a state name isn't
          // recognized -- the backend's own guard is still the real
          // correctness check for those edge cases (stale trustMap, a race
          // with a real episode scoring between page load and this click),
          // this is only the common-case UX shortcut, not a replacement.
          const alreadyCanAct = state === "can_act";
          const alreadyReportOnly = state === "report_only";
          return (
            <div
              key={fc}
              className={`flex items-center justify-between p-3 bg-surface border ${
                fc === highlightClass ? "border-primary" : "border-outline"
              }`}
            >
              <div className="flex flex-col">
                <span className="font-bold">{CLASS_LABELS[fc] ?? fc}</span>
                <span className="font-data-mono text-[10px] text-on-surface-variant">
                  Current: {state.toUpperCase()}
                </span>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => onPromote(fc)}
                  disabled={busyClass === fc || alreadyCanAct}
                  title={alreadyCanAct ? "Already can-act -- nothing to promote" : undefined}
                  className="w-[140px] px-4 py-1.5 bg-[#238636] text-white font-label-caps text-[11px] hover:brightness-110 transition-all disabled:opacity-40 flex items-center justify-center"
                >
                  {busyClass === fc ? <LoadingDots /> : "FORCE PROMOTE"}
                </button>
                <button
                  onClick={() => onDemote(fc)}
                  disabled={busyClass === fc || alreadyReportOnly}
                  title={alreadyReportOnly ? "Already report-only -- nothing to demote" : undefined}
                  className="w-[140px] px-4 py-1.5 bg-error-container text-white font-label-caps text-[11px] hover:brightness-110 transition-all disabled:opacity-40 flex items-center justify-center"
                >
                  {busyClass === fc ? <LoadingDots /> : "FORCE DEMOTE"}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
