import { useEffect, useRef, useState } from "react";
import { reasoningStreamUrl } from "../../api/operator";
import { useTickingElapsed } from "../../hooks/useTickingElapsed";
import LoadingDots from "../shared/LoadingDots";

// Fills the necklace ring's real empty center (wardence_frontend.md's
// "Central Thinking Hub" sessions) -- merges v0's real, working
// OrbitalPulse/HandoffScan mechanics with Stitch's Obsidian-HUD visual
// language. Every animated state here is driven by a REAL event from
// GET /trigger/reasoning-stream (provider_attempt/reasoning_chunk/done),
// never a fixed decorative loop -- if no episode is diagnosing, this
// renders the quiet idle state and nothing more.
//
// Real, deliberate scope cut (per the redundancy discussion): this
// widget does NOT show step-state (ActiveSession already does),
// cluster telemetry (SystemStatusRibbon already does), or the 3
// invisible classes' live evidence (LiveStatusDetail already does).
// Its only real job is the one thing shown nowhere else: live model
// reasoning + real provider-handoff visibility.

const RESOLUTION_HOLD_MS = 2500; // how long the Resolution frame stays up before returning to idle
const HANDOFF_FLASH_MS = 1400; // matches index.css's .hub-beam-sweep duration
const MAX_PIPS = 5; // PROVIDER_CHAIN has 7 real entries; 5 pips is a real, honest "several tried" cap, not a hard ceiling on real attempts

function OrbitalCore({ throughputCps }) {
  // Real, measured chars/sec of streamed reasoning text drives both the
  // orbit speed and the core's pulse period -- higher real throughput
  // (more model output arriving) reads as a tighter, faster pulse.
  // Deliberately real-data-driven, not decorative: this is the same
  // class of bug the v0 export shipped with (pulseDuration moving the
  // WRONG direction relative to its own input) -- guarded here by
  // simply not inventing a fake "latency" number at all, only ever
  // using what's genuinely measured from real chunk arrival timing.
  const pulseDurationS = Math.max(1.2, 2.8 - throughputCps / 25).toFixed(2);
  const orbitDurationS = Math.max(1.4, 6 - throughputCps / 15).toFixed(2);
  // Real, defensive fix, applied fully inline: the CSS-class-level fix
  // (longhand animation-name/timing-function/iteration-count in
  // index.css, leaving only animation-duration to be set inline) was
  // NOT confirmed working live -- rather than guess at a second
  // class-level theory, every animation property is now set directly
  // in the inline style, so there is nothing left in the CSS class that
  // could conflict with or override it, regardless of shorthand/
  // longhand cascade quirks, PostCSS/Lightning CSS optimization, or
  // anything else class-based. If this still doesn't spin, the bug is
  // NOT in this component's styling at all -- worth checking devtools'
  // actual computed `animation` value at that point.
  const orbitAnim = (durationS, reverse) => ({
    animationName: "hub-orbit-spin",
    animationDuration: `${durationS}s`,
    animationTimingFunction: "linear",
    animationIterationCount: "infinite",
    animationDirection: reverse ? "reverse" : "normal",
  });
  return (
    <div className="relative w-full h-full flex items-center justify-center">
      <div
        className="hub-orbit"
        style={{ width: "78%", height: "78%", ...orbitAnim(orbitDurationS, false) }}
      />
      <div
        className="hub-orbit"
        style={{ width: "62%", height: "62%", ...orbitAnim((orbitDurationS * 0.7).toFixed(2), true) }}
      />
      <div
        className="hub-core"
        style={{
          width: "38%", height: "38%",
          animationName: "hub-core-pulse",
          animationDuration: `${pulseDurationS}s`,
          animationTimingFunction: "ease-in-out",
          animationIterationCount: "infinite",
        }}
      />
    </div>
  );
}

export default function CentralThinkingHub({ episodeId, episodeState, live, token, width = 400, height = 280 }) {
  const [phase, setPhase] = useState("idle"); // idle | ripple | ignition | handshake | resolution
  // Real, smoothly-ticking elapsed time in the current state -- same
  // shared hook TopologyMap's Bead uses, same real base value
  // (live.elapsed_in_state_s). Shown during ignition/handshake (the
  // whole real resolving window, including the ~12s R2-republish tail
  // that used to show nothing at all).
  const elapsedS = useTickingElapsed(live?.elapsed_in_state_s, Boolean(episodeId) && episodeState != null);
  const [reasoningLog, setReasoningLog] = useState("");
  const [providerLog, setProviderLog] = useState([]); // real [{provider, model, tier}]
  const [throughputCps, setThroughputCps] = useState(0); // real, smoothed chars/sec

  const esRef = useRef(null);
  const gotFirstEventRef = useRef(false);
  const lastChunkRef = useRef(null); // {time}
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

  // Real fix for a real race found via live testing: the ORIGINAL version
  // of this effect waited for a POLLED episode_state to say "resolving"
  // before opening the SSE connection -- but live-status is only polled
  // every 4s (LIVE_POLL_MS in Operator/index.jsx), while a real diagnosis
  // call (especially a clean single-provider success) can finish in 1-5
  // real seconds, well inside one poll interval. That meant the Hub often
  // never observed "resolving" until it was already over, connected to an
  // already-finished stream, and read every event in one instant burst --
  // confirmed live, matches this project's own repeated "polling race"
  // bug shape (disk-full's settle-wait race, network-partition's timing
  // bugs), not a display bug in this component.
  //
  // Real fix: open the connection the moment an episode STARTS existing
  // (episodeId set, right after inject), long before resolving could ever
  // begin -- it sits quietly listening through injecting/holding/
  // awaiting_fix (operator_api.py's own tail loop just polls for the file
  // to exist, essentially free while nothing's been written yet). The
  // ACTUAL Ripple->Ignition transition is triggered by the real FIRST
  // event arriving on the wire, never by polled state.
  useEffect(() => {
    if (!episodeId || !token) return undefined;

    setReasoningLog("");
    setProviderLog([]);
    setThroughputCps(0);
    lastChunkRef.current = null;
    gotFirstEventRef.current = false;
    setPhase("idle"); // stays idle/quiet until the real first event arrives

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
        rippleTimerRef.current = setTimeout(() => setPhase("ignition"), 900); // matches .hub-ripple-expand duration
      }

      if (event.type === "provider_attempt") {
        setProviderLog((log) => [...log, { provider: event.provider, model: event.model, tier: event.tier }]);
        setPhase("handshake");
        clearTimeout(handoffTimerRef.current);
        handoffTimerRef.current = setTimeout(() => setPhase("ignition"), HANDOFF_FLASH_MS);
      } else if (event.type === "reasoning_chunk") {
        const now = performance.now();
        if (lastChunkRef.current) {
          const dt = now - lastChunkRef.current.time;
          if (dt > 0) {
            const instantRate = (event.text.length / dt) * 1000;
            // Real exponential smoothing over real measured samples --
            // never a fabricated/interpolated number.
            setThroughputCps((prev) => (prev === 0 ? instantRate : prev * 0.7 + instantRate * 0.3));
          }
        }
        lastChunkRef.current = { time: now };
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
    // Deliberately keyed on episodeId only, NOT episodeState -- opening
    // this connection must never depend on a polled value arriving in
    // time, that's the exact race being fixed.
  }, [episodeId, token]);

  // Real terminal transition -- shows a brief, honest Resolution frame
  // (state only, no fabricated verdict/confidence: that real data isn't
  // available from live-status, only from the full R2 replay once
  // republished_at lands -- see ActiveSession's own "View full replay"
  // link for that). Still driven by polled episodeState -- fine here,
  // unlike the connection-open race above, since missing the EXACT poll
  // that first observes "resolved" only delays the Resolution flash by
  // up to one real poll interval, it never causes lost/skipped data the
  // way missing "resolving" did.
  //
  // Real fix, per explicit correction: entering "resolved" does NOT mean
  // this widget's job is done -- the real R2 republish (the exact thing
  // this widget was built to keep hidden as a separate visible phase)
  // still takes a real ~12s. So the return-to-idle timer only starts
  // once `live.republished_at` genuinely lands, not the instant
  // episode_state flips -- the render below shows an honest
  // "documenting episode" sub-state for that real gap instead of
  // jumping straight to a checkmark that implies everything is done.
  useEffect(() => {
    if ((episodeState === "resolved" || episodeState === "failed") && phase !== "idle" && phase !== "resolution") {
      closeStream();
      setPhase("resolution");
    }
  }, [episodeState, phase]);

  useEffect(() => {
    if (phase !== "resolution") return undefined;
    // "failed" has no real republish step to wait on; "resolved" waits
    // for the real republished_at signal.
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

  const currentProvider = providerLog[providerLog.length - 1];

  return (
    <div
      className="relative rounded-2xl glass-module border border-outline-variant/30 flex flex-col items-center justify-center overflow-hidden"
      style={{ width, height }}
    >
      {phase === "ripple" && <div className="hub-ripple" />}

      {phase === "idle" && (
        <div className="flex flex-col items-center gap-2 text-on-surface-variant">
          <span className="material-symbols-outlined text-3xl opacity-40">radio_button_unchecked</span>
          <span className="font-label-caps text-[10px] opacity-60">AWAITING_TRIGGER</span>
        </div>
      )}

      {(phase === "ignition" || phase === "ripple") && (
        <div className="w-full h-full flex flex-col">
          {elapsedS != null && (
            <div className="absolute top-2 right-3 font-data-mono text-[9px] text-on-surface-variant z-10">
              {elapsedS}s ELAPSED
            </div>
          )}
          {/* Real fix, per "make it bigger, rectangular" feedback: the
              orbit rings/core must stay a true SQUARE regardless of the
              panel's own (now rectangular, wider-than-tall) shape --
              stretching OrbitalCore's own w-full/h-full percentages
              across a non-square area would turn circles into ellipses.
              Sized off the real available height (the limiting
              dimension once the log strip below takes its own third),
              centered horizontally. */}
          <div className="flex-1 min-h-0 flex items-center justify-center py-2">
            <div className="h-full aspect-square max-w-full">
              <OrbitalCore throughputCps={throughputCps} />
            </div>
          </div>
          <div
            ref={logRef}
            className="h-1/3 min-h-0 overflow-y-auto bg-surface-container-lowest/90 px-2 py-1 font-data-mono text-[9px] leading-tight text-warning-amber"
          >
            <div className="text-on-surface-variant text-[8px] mb-0.5 opacity-70">MODEL_REASONING_LOG</div>
            {reasoningLog ? (
              <p className="whitespace-pre-wrap break-words">{reasoningLog}</p>
            ) : (
              <p className="opacity-40">&gt; awaiting real reasoning tokens...</p>
            )}
          </div>
        </div>
      )}

      {phase === "handshake" && (
        <div className="w-full h-full flex flex-col relative">
          <div className="hub-beam" />
          {elapsedS != null && (
            <div className="absolute top-2 right-3 font-data-mono text-[9px] text-on-surface-variant z-10">
              {elapsedS}s ELAPSED
            </div>
          )}
          <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-1">
            <span className="material-symbols-outlined text-xl text-warning-amber">sync_alt</span>
            <span className="font-label-caps text-[9px] text-warning-amber text-center px-2">
              {currentProvider ? `${currentProvider.provider} / ${currentProvider.tier?.toUpperCase()}` : "HANDOFF"}
            </span>
          </div>
          <div className="px-2 pb-2">
            <div className="text-on-surface-variant text-[8px] mb-1 opacity-70 text-center">
              PROVIDER_CHAIN_ATTEMPTS
            </div>
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
        </div>
      )}

      {phase === "resolution" && (
        episodeState === "resolved" && !live?.republished_at ? (
          // Real, honest wait -- episode_state says resolved, but the
          // real ~12s R2 republish (the exact gap this widget exists to
          // narrate rather than hide entirely) hasn't landed yet.
          <div className="flex flex-col items-center gap-2">
            <LoadingDots />
            <span className="font-label-caps text-[9px] text-primary">DOCUMENTING EPISODE</span>
            {elapsedS != null && (
              <span className="font-data-mono text-[9px] text-on-surface-variant">{elapsedS}s elapsed</span>
            )}
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2">
            <span
              className={`material-symbols-outlined text-2xl ${
                episodeState === "resolved" ? "text-correct-green" : "text-error-red"
              }`}
            >
              {episodeState === "resolved" ? "check_circle" : "error"}
            </span>
            <span className="font-label-caps text-[9px] text-on-surface-variant">
              EPISODE_{episodeState?.toUpperCase()}
            </span>
          </div>
        )
      )}
    </div>
  );
}
