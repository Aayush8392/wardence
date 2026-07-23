import { useNavHistory } from "../../context/NavHistoryContext";

const STATE_LABELS = {
  report_only: "REPORT-ONLY",
  can_act: "CAN-ACT",
  demoted: "DEMOTED",
};

const BADGE_CLASS = {
  report_only: "trust-badge-report",
  can_act: "trust-badge-act",
  demoted: "trust-badge-demoted",
};

const TRUST_TEXT_CLASS = {
  report_only: "text-on-surface-variant",
  can_act: "text-primary",
  demoted: "text-error",
};

// Locked decision: cap the streak history strip at the most recent 20-30
// episodes rather than showing every episode ever run -- a class with 100+
// episodes would otherwise render an unreadable, ever-growing row. Full
// history stays one click away via the existing Trust Ladder -> Replay
// Viewer cross-tab link (pair #1), this strip is deliberately "recent
// trend," not "complete record."
const STREAK_WINDOW = 20;

function formatElapsed(t0) {
  if (!t0) return "—";
  const ms = Date.now() - new Date(t0).getTime();
  const totalSeconds = Math.max(Math.floor(ms / 1000), 0);
  const hours = Math.floor(totalSeconds / 3600);
  const mins = Math.floor((totalSeconds % 3600) / 60);
  const secs = totalSeconds % 60;
  if (hours > 0) return `${hours}h`;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function StreakStrip({ episodesForClass }) {
  // episodesForClass is already sorted most-recent-first (t0 DESC, per
  // publish_to_r2.py's query) -- take the most recent N, then reverse so
  // the strip reads left-to-right as oldest-to-newest, matching how a
  // reader naturally scans "progress over time."
  const recent = episodesForClass.slice(0, STREAK_WINDOW).slice().reverse();
  const padded = Array(Math.max(STREAK_WINDOW - recent.length, 0)).fill(null);

  return (
    <div className="flex gap-px">
      {padded.map((_, i) => (
        <div key={`pad-${i}`} className="streak-block streak-gray" />
      ))}
      {recent.map((ep) => (
        <div
          key={ep.episode_id}
          title={`${ep.episode_id.slice(0, 8)} — ${ep.trust_correct ? "correct" : "incorrect"}`}
          className={`streak-block ${ep.trust_correct ? "streak-green" : "streak-red"}`}
        />
      ))}
    </div>
  );
}

function MatrixRow({ row, episodes, highlighted, isLiveTarget }) {
  const { navigateTo } = useNavHistory();
  const episodesForClass = episodes.filter((e) => e.fault_class === row.fault_class);
  const lastElapsed = formatElapsed(episodesForClass[0]?.t0);

  return (
    <div
      onClick={() =>
        navigateTo("/replay", { type: "faultClass", faultClass: row.fault_class }, "Trust Ladder")
      }
      className={`group relative overflow-hidden flex flex-col md:flex-row items-center p-4 gap-6 transition-all cursor-pointer ${
        isLiveTarget
          ? "bg-surface-container border border-primary/40 ring-1 ring-primary/20"
          : `bg-surface-container-low border ${highlighted ? "border-primary" : "border-outline-variant"} hover:border-primary/50`
      }`}
    >
      {/* Hover-only on every row, no exceptions -- previously the live-target
          row kept this permanently visible, which read as broken/inconsistent
          rather than intentional. Also needed `overflow-hidden` on the row
          above: without it the absolutely-positioned scanline wasn't clipped
          to the row's own box and visibly overflowed past its edges. */}
      {/* .scanline sets position:absolute but no left/right -- without an
          explicit inset it just sat at its static inline position (pushed
          right by the row's own p-4 padding) instead of spanning the row's
          full width from the true left edge. */}
      <div className="scanline pointer-events-none hidden group-hover:block left-0 right-0" />

      <div className="w-full md:w-1/4 flex flex-col gap-1">
        <div className="flex items-center gap-2">
          {/* Pulsing dot = "currently earning/holding real write access,"
              so it's tied to the class's actual state (can-act), not to
              whichever row happens to be the live-tether target. */}
          {row.state === "can_act" && <span className="w-1.5 h-1.5 bg-primary rounded-full animate-ping" />}
          <span className="font-data-mono text-sm text-primary font-bold">
            {row.fault_class.toUpperCase().replaceAll("-", "_")}
          </span>
        </div>
        <div className={`${BADGE_CLASS[row.state]} px-2 py-0.5 text-[9px] w-fit uppercase`}>
          {STATE_LABELS[row.state] ?? row.state}
        </div>
      </div>

      <div className="w-full md:w-2/4 flex flex-col gap-2">
        <div className="flex justify-between items-center px-1">
          <span className="font-label-caps text-[9px] text-on-surface-variant">STREAK_VAL</span>
          <span className={`font-data-mono text-[9px] ${row.state === "demoted" ? "text-error" : "text-primary"}`}>
            S_{String(row.tier === "auto-fix" ? row.streak ?? 0 : row.episodes_scored).padStart(2, "0")}
          </span>
        </div>
        <StreakStrip episodesForClass={episodesForClass} />
      </div>

      <div className="w-full md:w-1/4 flex items-center justify-end gap-8 border-l border-outline-variant pl-6">
        <div className="text-right">
          <div className={`font-data-mono text-lg ${TRUST_TEXT_CLASS[row.state] ?? "text-on-surface"}`}>
            {row.diagnosis_accuracy != null ? (row.diagnosis_accuracy * 100).toFixed(1) : "0.0"}
            <span className="text-[10px] opacity-40">%</span>
          </div>
          <div className="font-label-caps text-[9px] text-on-surface-variant uppercase">TRUST</div>
        </div>
        <div className="text-right">
          <div className="font-data-mono text-xs text-primary">{lastElapsed}</div>
          <div className="font-label-caps text-[9px] text-on-surface-variant uppercase">LAST</div>
        </div>
      </div>
    </div>
  );
}

export default function TrustMatrix({ rows, episodes, highlightClass, liveTargetClass }) {
  // Grouped by bucket (locked decision) -- auto-fix classes first since
  // they're the ones actually earning/losing real write access, report-only
  // classes after. Report-only is a correct, intentional outcome for most
  // classes here (per the thesis), not a gap.
  const autoFix = rows.filter((r) => r.tier === "auto-fix").sort((a, b) => (b.streak ?? 0) - (a.streak ?? 0));
  const reportOnly = rows
    .filter((r) => r.tier !== "auto-fix")
    .sort((a, b) => (b.diagnosis_accuracy ?? 0) - (a.diagnosis_accuracy ?? 0));

  return (
    <section className="flex flex-col gap-2 mt-8">
      <div className="flex justify-between items-center px-1 mb-2">
        <h2 className="font-label-caps text-[11px] text-on-surface-variant tracking-widest uppercase">
          Autonomous_Fault_Matrix
        </h2>
        <span className="font-data-mono text-[10px] text-primary flex items-center gap-1.5">
          <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse" />
          LIVE_TETHER_ACTIVE
        </span>
      </div>

      <div className="font-label-caps text-[9px] text-on-surface-variant uppercase tracking-wide mb-1">
        Auto-fix — earns real write access
      </div>
      {autoFix.map((r, i) => (
        <div key={r.fault_class}>
          <MatrixRow
            row={r}
            episodes={episodes}
            highlighted={r.fault_class === highlightClass}
            isLiveTarget={r.fault_class === liveTargetClass}
          />
          {i < autoFix.length - 1 && (
            <div className="flex justify-center -my-2 relative z-10">
              <div className="tether-line" />
            </div>
          )}
        </div>
      ))}

      <div className="font-label-caps text-[9px] text-on-surface-variant uppercase tracking-wide mt-4 mb-1">
        Report-only — diagnoses only, by design (not a failure state)
      </div>
      {reportOnly.map((r) => (
        <MatrixRow
          key={r.fault_class}
          row={r}
          episodes={episodes}
          highlighted={r.fault_class === highlightClass}
          isLiveTarget={r.fault_class === liveTargetClass}
        />
      ))}
    </section>
  );
}
