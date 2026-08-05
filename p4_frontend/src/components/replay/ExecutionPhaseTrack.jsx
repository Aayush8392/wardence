const STEP_STATUS = {
  done: { icon: "check", css: "bg-correct-green text-on-primary border-correct-green" },
  in_progress: { icon: "sync", css: "border-primary text-primary bg-primary/20" },
  failed: { icon: "close", css: "bg-error text-on-error border-error" },
  timed_out: { icon: "warning", css: "bg-error text-on-error border-error" },
};

function Node({ icon, label, css, muted }) {
  return (
    <div className={`flex flex-col items-center gap-1.5 bg-surface px-2 z-10 ${muted ? "opacity-40" : ""}`}>
      <div className={`w-6 h-6 rounded flex items-center justify-center border ${css}`}>
        <span className="material-symbols-outlined text-[15px]">{icon}</span>
      </div>
      <span className="font-label-caps text-[9px] text-on-surface-variant text-center whitespace-nowrap">{label}</span>
    </div>
  );
}

// The two leading "gate" nodes (semantic validation, RBAC) are a real, fixed
// mechanism every dispatched action passes through (tool_call_validator.py
// in front of the blast-radius cage, same static-config framing as
// SecurityCage) -- not per-episode measured data, so shown as a constant
// pair of nodes rather than fabricated per-episode pass/fail. The real
// per-episode data is action_result.progress_log's ordered steps.
export default function ExecutionPhaseTrack({ episode, revealCount }) {
  const progressLog = episode.action_result?.progress_log;
  if (!episode.scores_action_taken) return null;

  const steps = progressLog && progressLog.length > 0
    ? progressLog.map((s) => ({ label: s.step.replace(/_/g, " ").toUpperCase(), status: s.status }))
    : [{ label: episode.scores_action_taken.replace(/_/g, " ").toUpperCase(), status: episode.action_applied ? "done" : "failed" }];

  const allNodes = [
    { label: "SEMANTIC_VAL", status: "done" },
    { label: "RBAC_CHECK", status: "done" },
    ...steps,
    { label: "COMPLETE", status: episode.action_applied ? "done" : "failed" },
  ];

  const visible = revealCount != null ? allNodes.slice(0, Math.max(0, revealCount)) : allNodes;
  const lastDoneIdx = visible.reduce((acc, n, i) => (n.status === "done" ? i : acc), -1);
  const progressPct = allNodes.length > 1 ? (lastDoneIdx / (allNodes.length - 1)) * 100 : 0;

  return (
    <div className="border border-outline-variant bg-surface-container-lowest p-4 relative">
      <div className="absolute top-0 left-3 -translate-y-1/2 px-2 py-0.5 bg-surface-variant font-label-caps text-[9px] text-on-surface-variant">
        EXECUTION_PHASE_TRACK
      </div>
      <div className="flex items-center justify-between relative mt-2 px-4">
        <div className="absolute top-1/2 left-4 right-4 h-px bg-outline-variant/30 -z-0 -translate-y-1/2" />
        <div
          className="absolute top-1/2 left-4 h-[2px] bg-correct-green -z-0 -translate-y-1/2 transition-all duration-500"
          style={{ width: `${progressPct}%`, maxWidth: "calc(100% - 2rem)" }}
        />
        {visible.map((n, i) => {
          const style = STEP_STATUS[n.status] ?? STEP_STATUS.done;
          return <Node key={i} icon={style.icon} css={style.css} label={n.label} muted={i === visible.length - 1 && n.status !== "done" && n.status !== "failed"} />;
        })}
      </div>
    </div>
  );
}
