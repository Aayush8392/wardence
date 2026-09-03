import { Fragment, useEffect, useRef, useState } from "react";
import { reasoningStreamUrl, fetchFaultStatus } from "../../api/operator";
import { fetchEpisodes, fetchRadarDossier } from "../../api/r2";
import { useTickingElapsed } from "../../hooks/useTickingElapsed";
import { CLASS_LABELS, INVISIBLE_CLASSES, STOREFRONT_SYMPTOM } from "../../constants/faultClasses";
import LoadingDots from "../shared/LoadingDots";
import RadarChart from "../charts/RadarChart";

// Axis keys pulled straight from publish_to_r2.py's build_radar_dossier
// output -- kept here (not imported) since RadarChart.jsx already
// defines its own AXES list; this is only used to build the
// allDossierValuesByAxis min-max input for the two unbounded axes.
const RADAR_MINMAX_AXES = ["avg_response_time_ms", "confidence_stdev"];

// Real per-class slide-out panel. Replaces the ring-center Central
// Thinking Hub (shelved in ./_shelved/CentralThinkingHub.jsx) + the
// standalone ActiveSession/LiveStatusDetail cards (both retired, their
// jobs absorbed here) with ONE panel, opened either by clicking a
// FaultGrid card body (dossier-only, no trigger) or by its TRIGGER button
// (injects + opens, reasoning-first). The parent (Operator/index.jsx)
// owns which class is "open" and auto-retargets the panel to whichever
// class currently has the live episode -- this component only renders
// whatever `faultClass`/`episodeId`/`live` it's handed.
//
// Per explicit correction: NOT two switchable tabs -- one continuous
// view. Reasoning stream is always the primary content; the trust
// dossier is a collapsible section underneath, independent of whether an
// episode is live (defaults collapsed when live, open when idle -- there
// being no live reasoning to show yet).

const STEPS = ["injecting", "holding", "awaiting_fix", "resolving", "resolved"];
const STEP_LABELS = {
  injecting: "INJECTING",
  holding: "LIVE",
  awaiting_fix: "AWAITING FIX",
  resolving: "FIXING",
  resolved: "RESOLVED",
};
const RESOLUTION_HOLD_MS = 2500;
const HANDOFF_FLASH_MS = 1400;
const MAX_PIPS = 5;

// Real episode summary, shown once the R2 publish for THIS episode
// genuinely lands (live.republished_at non-null) -- /trigger/live-status
// itself carries no diagnosis/confidence/verdict fields at all (confirmed
// by reading operator_api.py's trigger_live_status directly), so this
// fetches the full published record via the same fetchEpisodes() call
// Replay Viewer's CaseFile already uses, and finds this one episode by
// id. Deliberately excludes trust-delta (per explicit ask) -- streak
// before/after belongs to the Trust Ladder tab's own job, not a
// per-episode summary.
function EpisodeSummary({ episodeId, republishedAt }) {
  const [episode, setEpisode] = useState(null);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    if (!episodeId || !republishedAt) {
      setEpisode(null);
      setNotFound(false);
      return;
    }
    let cancelled = false;
    fetchEpisodes()
      .then((rows) => {
        if (cancelled) return;
        const match = rows.find((r) => r.episode_id === episodeId);
        if (match) setEpisode(match);
        else setNotFound(true);
      })
      .catch(() => { if (!cancelled) setNotFound(true); });
    return () => { cancelled = true; };
  }, [episodeId, republishedAt]);

  if (!republishedAt) return null;
  if (notFound) return null; // real publish landed but this specific record isn't in the fetched set -- fail quiet, not a broken UI
  if (!episode) {
    return (
      <div className="border border-outline-variant/20 p-3">
        <span className="font-data-mono text-xs text-on-surface-variant">Loading summary…</span>
      </div>
    );
  }

  const durabilityVerdict = episode.snapshot_durability_verdict ?? episode.scores_durability_verdict;

  return (
    <div className="border border-outline-variant/20 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="font-data-mono text-xs text-on-surface-variant">{episode.episode_id.slice(0, 8)}</span>
        <span className={`px-2 py-0.5 font-label-caps text-[9px] ${episode.correct ? "bg-correct-green/20 text-correct-green" : "bg-error-red/20 text-error-red"}`}>
          {episode.correct ? "CORRECT" : "WRONG"}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 font-data-mono text-[11px]">
        <div>
          <div className="font-label-caps text-[9px] text-on-surface-variant">CONFIDENCE</div>
          <div className="text-on-surface">{episode.score_confidence?.toFixed(2) ?? "n/a"}</div>
        </div>
        <div>
          <div className="font-label-caps text-[9px] text-on-surface-variant">PROVIDER</div>
          <div className="text-on-surface">{episode.provider ? `${episode.model ?? episode.provider}` : "stub"}</div>
        </div>
        {durabilityVerdict && (
          <div>
            <div className="font-label-caps text-[9px] text-on-surface-variant">DURABILITY</div>
            <div className={durabilityVerdict === "confirmed" ? "text-correct-green" : "text-error-red"}>
              {durabilityVerdict.toUpperCase()}
            </div>
          </div>
        )}
        {episode.scores_action_taken && (
          <div>
            <div className="font-label-caps text-[9px] text-on-surface-variant">ACTION</div>
            <div className="text-on-surface">{episode.scores_action_taken.replace(/_/g, " ")}</div>
          </div>
        )}
      </div>
    </div>
  );
}

function InvisibleClassEvidence({ faultClass, token }) {
  const [status, setStatus] = useState(null);
  useEffect(() => {
    if (!token || !INVISIBLE_CLASSES.includes(faultClass)) return undefined;
    let cancelled = false;
    const poll = () => {
      fetchFaultStatus(faultClass, token)
        .then((d) => { if (!cancelled) setStatus(d); })
        .catch(() => {});
    };
    poll();
    const id = setInterval(poll, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [faultClass, token]);

  if (!INVISIBLE_CLASSES.includes(faultClass)) return null;

  return (
    <div className="border border-outline-variant/30 p-3">
      <p className="font-label-caps text-[10px] text-on-surface-variant mb-2">
        LIVE EVIDENCE — no visible storefront symptom for this class
      </p>
      {!status && <span className="font-data-mono text-xs text-on-surface-variant">…</span>}
      {status?.warning && <span className="font-data-mono text-xs text-error">unavailable</span>}
      {status && !status.warning && faultClass === "disk-full" && (
        <span className={`font-label-caps text-[10px] px-2 py-1 border ${status.indication === "no_eviction_detected" ? "border-outline-variant text-on-surface-variant" : "border-primary text-primary"}`}>
          {status.indication.replaceAll("_", " ").toUpperCase()}
        </span>
      )}
    </div>
  );
}

export default function EpisodePanel({ faultClass, episodeId, live, token, onClose, onViewReplay }) {
  const episodeState = live?.episode_state ?? null;
  const stepIndex = STEPS.indexOf(episodeState);
  // Real fix (2026-09-03): elapsed_in_state_s counts from the instant the
  // episode entered "holding" (injector.py launch), not from the real hold
  // start -- for classes with a settle/ramp/confirm phase first
  // (memory-leak's saturation wait is the worst case, ~79s), that made a
  // correctly-running 180s hold display up to 78s "ahead" of itself. The
  // backend now surfaces `hold_started_at` (from injector.py's own
  // evidence-file timestamp) once the real hold has actually begun; until
  // then it's null and this falls back to the previous elapsed_in_state_s
  // behavior, which is what the settle/ramp/confirm phase itself should
  // still show.
  const holdStartedAtS = live?.hold_started_at
    ? (Date.now() - new Date(live.hold_started_at).getTime()) / 1000
    : null;
  const showingHoldElapsed = episodeState === "holding" && holdStartedAtS != null;
  const elapsedS = useTickingElapsed(
    showingHoldElapsed ? holdStartedAtS : live?.elapsed_in_state_s,
    Boolean(episodeId) && episodeState != null
  );

  const [dossierOpen, setDossierOpen] = useState(!episodeId);

  // Real Trust Dossier radar data -- fetched once, lazily, the first
  // time the collapsible section is actually opened (not on mount --
  // most episode panel opens are reasoning-first per the class's own
  // default, so fetching this unconditionally would be wasted for the
  // common case). radarDossier holds the FULL per-class object (every
  // fault class), not just this one, since the two unbounded axes
  // (response time / confidence spread) are normalized relative to the
  // real range observed across the whole roster, not a guessed fixed
  // ceiling -- see RadarChart.jsx's own normalize() docstring.
  const [radarDossier, setRadarDossier] = useState(null);
  const [radarError, setRadarError] = useState(null);
  useEffect(() => {
    if (!dossierOpen || radarDossier || radarError) return;
    let cancelled = false;
    fetchRadarDossier()
      .then((data) => {
        if (!cancelled) setRadarDossier(data);
      })
      .catch((err) => {
        if (!cancelled) setRadarError(err.message || "failed to load");
      });
    return () => {
      cancelled = true;
    };
  }, [dossierOpen, radarDossier, radarError]);

  // Real reasoning-stream state, ported from the shelved CentralThinkingHub
  // -- same phase machine (idle/ripple/ignition/handshake/resolution),
  // same real-event-driven transitions, same polling-race fix (open the
  // SSE connection the moment episodeId exists, not on polled state).
  const [phase, setPhase] = useState("idle");
  const [reasoningLog, setReasoningLog] = useState("");
  const [providerLog, setProviderLog] = useState([]);

  const esRef = useRef(null);
  const gotFirstEventRef = useRef(false);
  const handoffTimerRef = useRef(null);
  const resolutionTimerRef = useRef(null);
  const rippleTimerRef = useRef(null);
  const logRef = useRef(null);

  const closeStream = () => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
  };

  useEffect(() => {
    if (!episodeId || !token) {
      setPhase("idle");
      return undefined;
    }

    setReasoningLog("");
    setProviderLog([]);
    gotFirstEventRef.current = false;
    setPhase("idle");
    setDossierOpen(false); // a live episode just started -- default the panel to reasoning, dossier collapses

    const es = new EventSource(reasoningStreamUrl(episodeId, token));
    esRef.current = es;
    es.onmessage = (e) => {
      let event;
      try {
        event = JSON.parse(e.data);
      } catch {
        return;
      }

      if (!gotFirstEventRef.current && event.type !== "done") {
        gotFirstEventRef.current = true;
        setPhase("ripple");
        rippleTimerRef.current = setTimeout(() => setPhase("ignition"), 900);
      }

      if (event.type === "provider_attempt") {
        setProviderLog((log) => [...log, { provider: event.provider, model: event.model, tier: event.tier }]);
        setPhase("handshake");
        clearTimeout(handoffTimerRef.current);
        handoffTimerRef.current = setTimeout(() => setPhase("ignition"), HANDOFF_FLASH_MS);
      } else if (event.type === "reasoning_chunk") {
        setReasoningLog((text) => text + event.text);
      } else if (event.type === "done") {
        closeStream();
      }
    };
    es.onerror = () => closeStream();

    return () => {
      closeStream();
      clearTimeout(rippleTimerRef.current);
      clearTimeout(handoffTimerRef.current);
    };
  }, [episodeId, token]);

  useEffect(() => {
    if ((episodeState === "resolved" || episodeState === "failed") && phase !== "idle" && phase !== "resolution") {
      closeStream();
      setPhase("resolution");
    }
  }, [episodeState, phase]);

  useEffect(() => {
    if (phase !== "resolution") return undefined;
    const readyToHold = episodeState === "failed" || (episodeState === "resolved" && Boolean(live?.republished_at));
    if (!readyToHold) return undefined;
    resolutionTimerRef.current = setTimeout(() => setPhase("idle"), RESOLUTION_HOLD_MS);
    return () => clearTimeout(resolutionTimerRef.current);
  }, [phase, episodeState, live?.republished_at]);

  useEffect(() => {
    return () => {
      closeStream();
      clearTimeout(handoffTimerRef.current);
      clearTimeout(resolutionTimerRef.current);
      clearTimeout(rippleTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [reasoningLog]);

  if (!faultClass) return null;

  const currentProvider = providerLog[providerLog.length - 1];

  // "Verify it yourself" hint -- only while the fault is genuinely still
  // present on the storefront (injected, not yet being fixed). Skipped for
  // the invisible class (disk-full), which has its own live-status readout.
  const faultLiveNow = ["injecting", "holding", "awaiting_fix"].includes(episodeState);
  const storefrontSymptom = STOREFRONT_SYMPTOM[faultClass];
  const showStorefrontHint =
    Boolean(episodeId) &&
    faultLiveNow &&
    !INVISIBLE_CLASSES.includes(faultClass) &&
    Boolean(storefrontSymptom);

  return (
    // Real docked-sidebar behavior on desktop (per explicit ask -- an
    // earlier `fixed` overlay covered grid cards behind it): at sm+ this
    // becomes a normal-flow flex sibling (sticky, so it stays in view
    // while the grid scrolls) that the parent's flex row pushes the grid
    // over for, so no card is ever physically hidden. Below sm there's no
    // room to show both at once, so it stays a real full-width fixed
    // overlay -- the standard, expected mobile-drawer pattern.
    <div className="fixed inset-y-0 right-0 z-50 w-full sm:sticky sm:z-auto sm:top-0 sm:self-start sm:h-screen sm:w-[420px] sm:shrink-0 bg-surface-container-lowest border-l border-outline-variant/30 flex flex-col shadow-2xl">
      {/* Persistent header -- absorbs ActiveSession's step-dots/elapsed job */}
      <div className="p-4 border-b border-outline-variant/30 shrink-0">
        <div className="flex items-center justify-between mb-3">
          <span className="font-data-mono text-sm text-on-surface">{CLASS_LABELS[faultClass] ?? faultClass}</span>
          <button onClick={onClose} className="text-on-surface-variant hover:text-on-surface">
            <span className="material-symbols-outlined text-lg">close</span>
          </button>
        </div>

        {episodeId ? (
          <>
            <div className="flex items-start mb-3">
              {STEPS.map((s, i) => (
                <Fragment key={s}>
                  <div className="flex flex-col items-center gap-1 shrink-0">
                    <div
                      className={`w-5 h-5 rounded-full flex items-center justify-center border ${
                        i < stepIndex
                          ? "bg-[#238636] border-[#238636]"
                          : i === stepIndex
                          ? "border-primary bg-primary/10"
                          : "border-outline-variant/40"
                      }`}
                    >
                      {i < stepIndex && <span className="material-symbols-outlined text-[12px] text-on-primary">check</span>}
                      {i === stepIndex && <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse" />}
                    </div>
                    <span
                      className={`font-label-caps text-[7px] text-center leading-tight whitespace-nowrap ${
                        i <= stepIndex ? "text-on-surface-variant" : "text-on-surface-variant/40"
                      }`}
                    >
                      {STEP_LABELS[s]}
                    </span>
                  </div>
                  {i < STEPS.length - 1 && (
                    <div
                      className={`flex-1 h-px mx-1 ${i < stepIndex ? "bg-[#238636]" : "bg-outline-variant/30"}`}
                      style={{ marginTop: 9 }}
                    />
                  )}
                </Fragment>
              ))}
            </div>
            <div className="flex items-center justify-between">
              <span className="font-label-caps text-[10px] text-on-surface-variant">
                {(episodeState ?? "injecting").replace("_", " ").toUpperCase()}
                {elapsedS != null && showingHoldElapsed && live?.hold_duration_s != null
                  ? ` · ${elapsedS}s / ${live.hold_duration_s}s`
                  : elapsedS != null && ` · ${elapsedS}s`}
              </span>
              {episodeState === "resolved" && live?.republished_at && (
                <button
                  onClick={() => onViewReplay?.(episodeId)}
                  className="font-label-caps text-[10px] text-primary hover:underline"
                >
                  VIEW FULL REPLAY →
                </button>
              )}
              {episodeState === "resolved" && !live?.republished_at && (
                <span className="font-label-caps text-[10px] text-on-surface-variant">Publishing…</span>
              )}
            </div>
          </>
        ) : (
          <span className="font-label-caps text-[10px] text-on-surface-variant">NO ACTIVE EPISODE</span>
        )}
      </div>

      {/* Episode summary -- real data, only once the R2 publish for this
          episode has genuinely landed. Sits between the step progression
          (in the header above) and the reasoning stream below, per
          explicit placement ask. */}
      {episodeId && <EpisodeSummary episodeId={episodeId} republishedAt={live?.republished_at} />}

      {/* Reasoning stream -- primary content. Real fix, per explicit ask:
          the transcript itself must stay visible once an episode
          finishes (someone may want to read how the LLM reasoned) --
          the log box below renders whenever there's real reasoning text
          OR a live phase in progress, never just swapped out for a
          terminal status. Phase-specific accents (ripple/handshake/
          resolution banner) render ABOVE the log, not instead of it. */}
      <div className="flex-1 min-h-0 flex flex-col p-4 overflow-y-auto">
        {showStorefrontHint && (
          <div className="mb-3 border border-warning-amber/40 bg-warning-amber/5 p-3">
            <p className="font-label-caps text-[10px] text-warning-amber mb-1">
              VERIFY ON THE STOREFRONT
            </p>
            <p className="font-data-mono text-[11px] text-on-surface leading-snug">
              {storefrontSymptom}
            </p>
          </div>
        )}
        <p className="font-label-caps text-[10px] text-on-surface-variant mb-2">LLM REASONING</p>

        {phase === "idle" && !reasoningLog && (
          <div className="flex flex-col items-center justify-center gap-2 text-on-surface-variant py-10 border border-outline-variant/20">
            <span className="material-symbols-outlined text-3xl opacity-40">radio_button_unchecked</span>
            <span className="font-label-caps text-[10px] opacity-60">
              {episodeId ? "AWAITING_TRIGGER" : "No active episode — trigger a fault to see live reasoning"}
            </span>
          </div>
        )}

        {(reasoningLog || phase !== "idle") && (
          <div className="border border-outline-variant/20 flex flex-col relative" style={{ minHeight: 220 }}>
            {phase === "ripple" && <div className="hub-ripple" />}

            {phase === "handshake" && (
              <div className="relative flex flex-col items-center justify-center gap-2 py-4 border-b border-outline-variant/20">
                <div className="hub-beam" />
                <span className="material-symbols-outlined text-xl text-warning-amber">sync_alt</span>
                <span className="font-label-caps text-[9px] text-warning-amber text-center px-2">
                  {currentProvider ? `${currentProvider.provider} / ${currentProvider.tier?.toUpperCase()}` : "HANDOFF"}
                </span>
                <div className="flex items-center justify-center gap-1.5">
                  {Array.from({ length: MAX_PIPS }).map((_, i) => (
                    <span
                      key={i}
                      className={`w-1.5 h-1.5 rounded-full ${
                        i < providerLog.length ? "bg-warning-amber shadow-[0_0_6px_var(--color-warning-amber)]" : "bg-outline-variant"
                      }`}
                    />
                  ))}
                </div>
              </div>
            )}

            {phase === "resolution" && (
              <div className="flex items-center justify-center gap-2 py-3 border-b border-outline-variant/20">
                {episodeState === "resolved" && !live?.republished_at ? (
                  <>
                    <LoadingDots />
                    <span className="font-label-caps text-[9px] text-primary">DOCUMENTING EPISODE</span>
                  </>
                ) : (
                  <>
                    <span className={`material-symbols-outlined text-lg ${episodeState === "resolved" ? "text-correct-green" : "text-error-red"}`}>
                      {episodeState === "resolved" ? "check_circle" : "error"}
                    </span>
                    <span className="font-label-caps text-[9px] text-on-surface-variant">EPISODE_{episodeState?.toUpperCase()}</span>
                  </>
                )}
              </div>
            )}

            <div
              ref={logRef}
              className="flex-1 min-h-[180px] overflow-y-auto bg-surface-container-lowest/90 px-2 py-2 font-data-mono text-[10px] leading-tight text-warning-amber"
            >
              {reasoningLog ? (
                <p className="whitespace-pre-wrap break-words">{reasoningLog}</p>
              ) : (
                <p className="opacity-40">&gt; awaiting real reasoning tokens...</p>
              )}
            </div>
          </div>
        )}

        {episodeId && <InvisibleClassEvidence faultClass={faultClass} token={token} />}

        {/* Collapsible dossier -- always available, independent of live state */}
        <div className="mt-4 border-t border-outline-variant/30 pt-3">
          <button
            onClick={() => setDossierOpen((v) => !v)}
            className="w-full flex items-center justify-between font-label-caps text-[10px] text-on-surface-variant hover:text-on-surface"
          >
            <span>TRUST DOSSIER — HISTORIC DATA</span>
            <span className="material-symbols-outlined text-base">{dossierOpen ? "expand_less" : "expand_more"}</span>
          </button>
          {dossierOpen && (
            <div className="mt-3 border border-outline-variant/20 p-2 flex flex-col items-center justify-center gap-2">
              {radarError && (
                <span className="font-label-caps text-[10px] text-on-surface-variant opacity-60">
                  Failed to load dossier — {radarError}
                </span>
              )}
              {!radarError && !radarDossier && <LoadingDots />}
              {!radarError && radarDossier && (
                <RadarChart
                  // Keyed on faultClass so switching classes (the panel
                  // auto-retargets without unmounting itself) remounts
                  // RadarChart fresh -- otherwise its entrance animation
                  // fires once, on this component's first mount, and
                  // never again for subsequent classes viewed in the
                  // same panel session.
                  key={faultClass}
                  dossierEntry={radarDossier[faultClass] ?? null}
                  allDossierValuesByAxis={RADAR_MINMAX_AXES.reduce((acc, axis) => {
                    acc[axis] = Object.values(radarDossier).map((entry) => entry?.[axis] ?? null);
                    return acc;
                  }, {})}
                />
              )}
              {!radarError && radarDossier && (
                <span className="font-label-caps text-[8px] text-on-surface-variant opacity-50 text-center">
                  * response speed / confidence spread ticks are relative to the fastest/lowest
                  seen across the current roster, not a fixed target like the other axes
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
