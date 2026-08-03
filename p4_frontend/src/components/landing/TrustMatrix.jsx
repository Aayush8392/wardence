import { useState } from "react";
import AnimatedNumber from "../shared/AnimatedNumber";
import { useNavHistory } from "../../context/NavHistoryContext";

const PERCENT_FORMAT = { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 };

const A_STATE_LABELS = { report_only: "REPORT_ONLY", can_act: "CAN_ACT", demoted: "DEMOTED" };
const B_MODE_LABELS = { stub: "STUB", llm: "LLM_CAN_ACT" };
const C_STATE_LABELS = { deterministic_fallback: "DETERMINISTIC", llm_can_act: "LLM_CAN_ACT" };

// Real bit-history glyphs only -- "veto" is deliberately absent. The
// tool-agreement veto (2026-08-05) is never persisted to any table
// today (see publish_to_r2.py's module docstring), so there is no real
// data to render a veto glyph from. Flap (a Dimension-A durability
// revert) IS real, derived from episode_snapshots.durability_verdict.
const BIT_CLASS = { correct: "streak-green", incorrect: "streak-red", flap: "streak-flap" };

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

// pulse: real signal, true only for the live-tether row's bit-fields --
// not decorative on every row.
function BitField({ recent, pulse }) {
  const padded = Array(Math.max(12 - recent.length, 0)).fill(null);
  const pulseClass = pulse ? "bit-pip-pulse" : "";
  return (
    <div className="flex gap-px">
      {padded.map((_, i) => (
        <div key={`pad-${i}`} className={`streak-block streak-gray !h-2.5 bit-pip ${pulseClass}`} />
      ))}
      {recent.map((outcome, i) => (
        <div
          key={i}
          title={outcome}
          className={`streak-block !h-2.5 bit-pip ${pulseClass} ${BIT_CLASS[outcome] ?? "streak-gray"}`}
        />
      ))}
    </div>
  );
}

// One compact dimension cell -- a label line ("| STATE   STREAK: x / MAX
// y") over its bit-field, matching the Stitch mockup's density (two
// tight lines, not a padded card).
function DimensionCell({ label, tone, streak, maxStreak, distanceToPeak, recent, pulse }) {
  return (
    <div className="flex flex-col gap-1 w-full">
      <div className="flex items-baseline gap-2">
        <span className={`font-status-badge text-[9px] border-l-2 pl-1.5 uppercase ${tone}`}>{label}</span>
        <span className="text-[9px] text-on-surface-variant opacity-70 whitespace-nowrap">
          STREAK: {streak}
          {maxStreak != null && <span className="opacity-50"> / MAX {maxStreak}</span>}
          {/* Distance to peak: real signal, 0 means currently at its
              all-time high (omitted, nothing to show); a negative
              number is the real distance fallen from it. */}
          {distanceToPeak != null && distanceToPeak !== 0 && (
            <span className="text-error opacity-80"> ({distanceToPeak})</span>
          )}
        </span>
      </div>
      <BitField recent={recent} pulse={pulse} />
    </div>
  );
}

function SafetyEnvelope({ envelope }) {
  if (!envelope) return null;
  const proposedNum = parseFloat(envelope.proposed);
  const ceiling = envelope.ceiling;
  const peakPct = envelope.peak_observed != null ? Math.min((envelope.peak_observed / ceiling) * 100, 100) : null;
  const proposedPct = Number.isFinite(proposedNum) ? Math.min((proposedNum / ceiling) * 100, 100) : null;

  return (
    <div className="mt-3">
      <div className="flex justify-between font-label-caps text-[9px] text-on-surface-variant mb-1 uppercase tracking-wider">
        <span>{envelope.peak_observed != null ? `Peak observed: ${envelope.peak_observed}${envelope.unit}` : "No peak-usage field for this action"}</span>
        <span className="text-primary">Proposed: {envelope.proposed}</span>
        <span>Ceiling: {ceiling}{envelope.unit}</span>
      </div>
      <div className="safety-envelope-track w-full max-w-md">
        {peakPct != null && <div className="safety-envelope-marker bg-on-surface-variant" style={{ left: `${peakPct}%` }} />}
        {proposedPct != null && (
          <div className="safety-envelope-marker bg-primary" style={{ left: `${proposedPct}%`, boxShadow: "0 0 6px var(--color-primary)" }} />
        )}
      </div>
      <p className="text-[9px] text-on-surface-variant opacity-60 mt-1">
        From the most recent scored episode for this class — not a live reading.
      </p>
    </div>
  );
}

function MatrixRow({ row, episodes, highlighted, isLiveTarget, isOpen, onToggleDossier }) {
  const { navigateTo } = useNavHistory();
  const episodesForClass = episodes.filter((e) => e.fault_class === row.fault_class);
  const lastElapsed = formatElapsed(episodesForClass[0]?.t0);

  return (
    <div
      className={`matrix-row-hover group relative overflow-hidden flex flex-col border-b border-outline-variant/60 cursor-pointer ${
        isLiveTarget ? "bg-surface-container" : "bg-surface-container-low"
      } ${highlighted ? "border-l-2 border-l-primary" : ""}`}
      onClick={() => onToggleDossier(row.fault_class)}
    >
      <div className="scanline pointer-events-none hidden group-hover:block left-0 right-0" />

      <div className="flex items-center px-6 py-2.5 gap-6">
        <div className="w-52 shrink-0 flex items-center gap-2">
          {row.dimension_a.state === "can_act" && <span className="w-1.5 h-1.5 bg-primary rounded-full animate-ping shrink-0" />}
          <span
            className="font-data-mono text-[13px] text-primary font-semibold hover:underline truncate"
            onClick={(e) => {
              e.stopPropagation();
              navigateTo("/replay", { type: "faultClass", faultClass: row.fault_class }, "Trust Ladder");
            }}
          >
            {row.fault_class}
          </span>
          {row.provider && (
            <span
              title="Most frequent historical diagnoser for this class (retrospective, not active routing)"
              className="font-label-caps text-[9px] bg-surface-container-high border border-outline-variant text-on-surface-variant px-1 rounded-sm shrink-0"
            >
              {row.provider.slice(0, 1).toUpperCase()}
            </span>
          )}
          {row.safety_hold?.active && (
            <span
              title={row.safety_hold.reason ?? "Misdispatch safety hold active"}
              className="font-status-badge text-[8px] bg-error/10 border border-error text-error px-1 rounded-sm shrink-0 flex items-center gap-0.5"
            >
              <span className="material-symbols-outlined text-[10px]">lock</span>
              LOCKED
            </span>
          )}
        </div>

        <div className="flex-1 grid grid-cols-3 gap-6">
          <DimensionCell
            label={A_STATE_LABELS[row.dimension_a.state] ?? row.dimension_a.state}
            tone={row.dimension_a.state === "can_act" ? "text-primary border-primary" : "text-on-surface-variant border-outline-variant"}
            streak={row.dimension_a.streak ?? 0}
            maxStreak={row.dimension_a.max_streak}
            distanceToPeak={row.dimension_a.distance_to_peak}
            recent={row.dimension_a.recent}
            pulse={isLiveTarget}
          />
          <DimensionCell
            label={B_MODE_LABELS[row.dimension_b.mode] ?? row.dimension_b.mode}
            tone={row.dimension_b.mode === "llm" ? "text-primary border-primary" : "text-on-surface-variant border-outline-variant"}
            streak={row.dimension_b.streak}
            maxStreak={row.dimension_b.max_streak}
            distanceToPeak={row.dimension_b.distance_to_peak}
            recent={row.dimension_b.recent}
            pulse={isLiveTarget}
          />
          <DimensionCell
            label={C_STATE_LABELS[row.dimension_c.state] ?? row.dimension_c.state}
            tone={row.dimension_c.state === "llm_can_act" ? "text-warning-amber border-warning-amber" : "text-on-surface-variant border-outline-variant"}
            streak={row.dimension_c.streak}
            maxStreak={row.dimension_c.max_streak}
            distanceToPeak={row.dimension_c.distance_to_peak}
            recent={row.dimension_c.recent}
            pulse={isLiveTarget}
          />
        </div>

        <div className="w-32 shrink-0 flex items-center justify-end gap-6">
          <div className="text-right">
            <div className="font-data-mono text-sm text-on-surface">
              {row.diagnosis_accuracy != null ? `${(row.diagnosis_accuracy * 100).toFixed(1)}%` : "0.0%"}
            </div>
            <div className="font-label-caps text-[8px] text-on-surface-variant uppercase">Trust</div>
          </div>
          <div className="text-right">
            <div className="font-data-mono text-[11px] text-primary">{lastElapsed}</div>
            <div className="font-label-caps text-[8px] text-on-surface-variant uppercase">Last</div>
          </div>
          <span className={`material-symbols-outlined text-base transition-colors duration-200 ${isOpen ? "text-primary" : "text-on-surface-variant"}`}>
            chevron_right
          </span>
        </div>
      </div>
    </div>
  );
}

// Right-edge sliding overlay -- replaces the old in-row accordion
// expand. A single shared panel, mounted once regardless of which row
// is open (matches the Stitch export's real interaction pattern), so
// the CSS transform transition has something to slide from/to rather
// than a fresh mount/unmount snapping into place.
function DossierPanel({ row, latestEpisode, isOpen, onClose }) {
  const isAutoFix = row?.tier === "auto-fix";
  return (
    <>
      <div
        className={`fixed inset-0 bg-black/50 z-50 transition-opacity duration-300 ${isOpen ? "opacity-100" : "opacity-0 pointer-events-none"}`}
        onClick={onClose}
      />
      <div className={`dossier-overlay bg-surface-container border-l border-outline-variant flex flex-col ${isOpen ? "open" : ""}`}>
        {row && (
          <>
            <div className="p-6 border-b border-outline-variant flex justify-between items-center shrink-0">
              <h3 className="font-label-caps text-primary font-bold tracking-widest text-sm uppercase">
                Class Dossier
              </h3>
              <button onClick={onClose} className="text-on-surface-variant hover:text-on-surface transition-colors">
                <span className="material-symbols-outlined text-xl">close</span>
              </button>
            </div>
            <div className="p-6 flex-1 overflow-y-auto space-y-6">
              <div>
                <div className="font-label-caps text-[10px] text-on-surface-variant uppercase mb-1">Target Class</div>
                <div className="font-data-mono text-lg text-on-surface">{row.fault_class}</div>
              </div>

              <div className="border border-outline-variant bg-surface-container-low p-4 rounded-sm">
                <div className="font-label-caps text-[10px] text-on-surface-variant uppercase mb-2 border-b border-outline-variant pb-1">
                  Episode Record
                </div>
                <ul className="font-data-mono text-[11px] space-y-2">
                  <li className="flex justify-between"><span className="text-on-surface-variant">Target:</span> <span className="text-on-surface">{latestEpisode?.target ?? "—"}</span></li>
                  <li className="flex justify-between"><span className="text-on-surface-variant">Namespace:</span> <span className="text-on-surface">{latestEpisode?.namespace ?? "—"}</span></li>
                  <li className="flex justify-between"><span className="text-on-surface-variant">Episodes scored:</span> <span className="text-on-surface">{row.episodes_scored}</span></li>
                </ul>
              </div>

              {row.safety_hold?.active && (
                <div className="border border-error bg-error/5 p-4 rounded-sm">
                  <div className="font-label-caps text-[10px] text-error uppercase mb-2 border-b border-error/40 pb-1 flex items-center gap-1">
                    <span className="material-symbols-outlined text-xs">lock</span>
                    Misdispatch Safety Hold — ACTIVE
                  </div>
                  <p className="text-[11px] text-on-surface">{row.safety_hold.reason}</p>
                  <p className="text-[9px] text-on-surface-variant opacity-70 mt-1">
                    Held since {row.safety_hold.held_since ?? "—"}. Distinct from Dimension A demotion — this
                    class's own action mechanism hasn't failed, but it keeps getting invoked for faults that
                    turn out not to be its own.
                  </p>
                </div>
              )}

              <div className="border border-outline-variant bg-surface-container-low p-4 rounded-sm">
                <div className="font-label-caps text-[10px] text-on-surface-variant uppercase mb-2 border-b border-outline-variant pb-1">
                  Efficiency Index
                </div>
                {row.efficiency ? (
                  <ul className="font-data-mono text-[11px] space-y-2">
                    <li className="flex justify-between">
                      <span className="text-on-surface-variant">Avg tokens / diagnosis:</span>
                      <span className="text-on-surface">{row.efficiency.avg_tokens.toLocaleString()}</span>
                    </li>
                    {row.efficiency.avg_neurons != null && (
                      <li className="flex justify-between">
                        <span className="text-on-surface-variant">Avg Neurons / diagnosis:</span>
                        <span className="text-on-surface">{row.efficiency.avg_neurons}</span>
                      </li>
                    )}
                    <li className="flex justify-between">
                      <span className="text-on-surface-variant">Provider:</span>
                      <span className="text-on-surface">{row.efficiency.provider}</span>
                    </li>
                    <li className="flex justify-between">
                      <span className="text-on-surface-variant">Sample size:</span>
                      <span className="text-on-surface">{row.efficiency.sample_count}</span>
                    </li>
                  </ul>
                ) : (
                  <p className="text-[10px] text-on-surface-variant opacity-60">
                    No token-usage data recorded yet for this class's primary provider.
                  </p>
                )}
              </div>

              <div className="border border-outline-variant bg-surface-container-low p-4 rounded-sm">
                <div className="font-label-caps text-[10px] text-on-surface-variant uppercase mb-2 border-b border-outline-variant pb-1">
                  Safety Envelope
                </div>
                {isAutoFix && row.safety_envelope ? (
                  <SafetyEnvelope envelope={row.safety_envelope} />
                ) : (
                  <p className="text-[10px] text-on-surface-variant opacity-60">
                    {isAutoFix
                      ? "No scored episode with a real action-result yet."
                      : "Not applicable — report-only class, no autonomous action to bound."}
                  </p>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </>
  );
}

// Real headline banner, always bound to the LIVE_TETHER target (the
// class of the most-recently-scored episode across the whole roster --
// already computed in TrustLadder/index.jsx as `latestEpisode`, real
// and honest, not arbitrary). Sparkline points are the row's own real
// Dimension-A recent-outcome bits (correct=1/incorrect=0/flap=0.5), not
// a fabricated waveform.
function HeadlineStrip({ row }) {
  if (!row) return null;
  const pts = row.dimension_a.recent.map((o, i) => {
    const y = o === "correct" ? 2 : o === "flap" ? 10 : 18;
    const x = (i / Math.max(row.dimension_a.recent.length - 1, 1)) * 100;
    return `${x},${y}`;
  });

  return (
    <section className="w-full bg-surface-container-low border border-outline-variant relative flex items-center px-8 py-5 gap-12 mb-2">
      <div className="scanning-line" />
      <div className="flex flex-col">
        <div className="flex items-center gap-3">
          <span className="material-symbols-outlined text-primary text-3xl">bolt</span>
          <h2 className="font-headline-md text-2xl text-on-surface font-extrabold uppercase tracking-widest">
            {row.fault_class}
          </h2>
        </div>
        <span className="font-label-caps text-[9px] text-primary/70 ml-11">LIVE_TETHER_TARGET — most recently scored episode</span>
      </div>

      <div className="flex flex-col items-start">
        <span className="font-label-caps text-[9px] text-on-surface-variant uppercase mb-1">Status</span>
        <div className={`flex items-center gap-2 px-3 py-1 rounded border ${row.dimension_a.state === "can_act" ? "bg-primary/10 border-primary/30" : "bg-surface-container border-outline-variant"}`}>
          <div className={`w-2 h-2 rounded-full ${row.dimension_a.state === "can_act" ? "bg-primary" : "bg-on-surface-variant"}`} />
          <span className={`font-status-badge text-[10px] uppercase ${row.dimension_a.state === "can_act" ? "text-primary" : "text-on-surface-variant"}`}>
            {A_STATE_LABELS[row.dimension_a.state] ?? row.dimension_a.state}
          </span>
        </div>
      </div>

      <div className="flex flex-col items-start flex-1">
        <span className="font-label-caps text-[9px] text-on-surface-variant uppercase mb-1">
          Dimension A streak: {row.dimension_a.streak ?? 0}
        </span>
        <div className="w-64 h-8 bg-surface-container-lowest border border-outline-variant/40 relative">
          <svg className="w-full h-full" preserveAspectRatio="none" viewBox="0 0 100 20">
            <polyline fill="none" points={pts.join(" ")} stroke="var(--color-primary)" strokeWidth="1.5" />
          </svg>
        </div>
      </div>

      <div className="text-right flex flex-col">
        <span className="font-label-caps text-[9px] text-on-surface-variant uppercase">Diagnosis accuracy</span>
        <span className="font-headline-md text-2xl text-primary">
          {row.diagnosis_accuracy != null ? (
            <AnimatedNumber value={row.diagnosis_accuracy} format={PERCENT_FORMAT} />
          ) : (
            "—"
          )}
        </span>
      </div>
    </section>
  );
}

export default function TrustMatrix({ rows, episodes, highlightClass, liveTargetClass }) {
  const [openClass, setOpenClass] = useState(null);
  const toggleDossier = (faultClass) => setOpenClass((prev) => (prev === faultClass ? null : faultClass));
  const closeDossier = () => setOpenClass(null);

  const liveRow = rows.find((r) => r.fault_class === liveTargetClass) ?? null;
  const openRow = rows.find((r) => r.fault_class === openClass) ?? null;
  const openRowLatestEpisode = openRow
    ? episodes.find((e) => e.fault_class === openRow.fault_class)
    : null;

  const autoFix = rows.filter((r) => r.tier === "auto-fix").sort((a, b) => (b.dimension_a.streak ?? 0) - (a.dimension_a.streak ?? 0));
  const reportOnly = rows
    .filter((r) => r.tier !== "auto-fix")
    .sort((a, b) => (b.diagnosis_accuracy ?? 0) - (a.diagnosis_accuracy ?? 0));

  return (
    <section className="flex flex-col gap-2 mt-8">
      <HeadlineStrip row={liveRow} />

      <div className="flex justify-between items-center px-1 mb-1">
        <h2 className="font-label-caps text-[11px] text-on-surface-variant tracking-widest uppercase">
          Autonomous_Fault_Matrix
        </h2>
        <span className="font-data-mono text-[10px] text-primary flex items-center gap-1.5">
          <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse" />
          LIVE_TETHER_ACTIVE
        </span>
      </div>

      <div className="border border-outline-variant bg-surface-container-lowest">
        <div className="flex items-center px-6 py-2 gap-6 bg-surface-container text-on-surface-variant font-label-caps text-[10px] uppercase tracking-wider border-b border-outline-variant">
          <span className="w-52 shrink-0">Class_ID</span>
          <div className="flex-1 grid grid-cols-3 gap-6">
            <span>Dimension_A (Action)</span>
            <span>Dimension_B (Diagnoser)</span>
            <span>Dimension_C (LLM_Action)</span>
          </div>
          <span className="w-32 shrink-0 text-right">Trust / Last</span>
        </div>

        <div className="font-label-caps text-[9px] text-on-surface-variant uppercase tracking-wide px-6 py-1.5 opacity-70">
          Auto-fix — earns real write access
        </div>
        {autoFix.map((r) => (
          <MatrixRow
            key={r.fault_class}
            row={r}
            episodes={episodes}
            highlighted={r.fault_class === highlightClass}
            isLiveTarget={r.fault_class === liveTargetClass}
            isOpen={openClass === r.fault_class}
            onToggleDossier={toggleDossier}
          />
        ))}

        <div className="font-label-caps text-[9px] text-on-surface-variant uppercase tracking-wide px-6 py-1.5 opacity-70 mt-1">
          Report-only — diagnoses only, by design (not a failure state)
        </div>
        {reportOnly.map((r) => (
          <MatrixRow
            key={r.fault_class}
            row={r}
            episodes={episodes}
            highlighted={r.fault_class === highlightClass}
            isLiveTarget={r.fault_class === liveTargetClass}
            isOpen={openClass === r.fault_class}
            onToggleDossier={toggleDossier}
          />
        ))}
      </div>

      <DossierPanel
        row={openRow}
        latestEpisode={openRowLatestEpisode}
        isOpen={openClass != null}
        onClose={closeDossier}
      />
    </section>
  );
}
