import { useEffect, useState, useCallback, useRef } from "react";
import { useAuth } from "../../context/AuthContext";
import { useNavHistory } from "../../context/NavHistoryContext";
import {
  fetchTriggerStatus,
  fetchSystemStatus,
  fetchLiveTrust,
  injectFault,
  fetchLiveStatus,
  stopHold,
  resolveFault,
  promoteClass,
  demoteClass,
} from "../../api/operator";
import { AUTO_FIX_CLASSES } from "../../constants/faultClasses";
import SystemStatusRibbon from "../../components/operator/SystemStatusRibbon";
import FaultGrid from "../../components/operator/FaultGrid";
import EpisodePanel from "../../components/operator/EpisodePanel";
import AdminOverrides from "../../components/operator/AdminOverrides";
import TriggerBudget from "../../components/operator/TriggerBudget";

const STATUS_POLL_MS = 15_000;
const LIVE_POLL_MS = 4_000;

export default function Operator() {
  const { token, role } = useAuth();
  const { currentContext, navigateTo } = useNavHistory();

  const [triggerStatus, setTriggerStatus] = useState(null);
  const [systemStatus, setSystemStatus] = useState(null);
  const [trustMap, setTrustMap] = useState({});

  // Real async state-machine episode (Phase 1/2, 2026-08-1x/13). Polled
  // ONCE here and shared with both FaultGrid (card-level action buttons)
  // and EpisodePanel (header/reasoning stream) -- neither makes its own
  // duplicate /trigger/live-status calls.
  const [activeEpisode, setActiveEpisode] = useState(null); // { episodeId, faultClass }
  const [live, setLive] = useState(null);
  const [errorMsg, setErrorMsg] = useState(null);
  const doneRef = useRef(false);

  // Real slide-out panel state -- which class's panel is open, independent
  // of whether that class has a live episode. Opened either by clicking a
  // FaultGrid card body (dossier-only, set directly) or by triggering a
  // fault (set in handleTrigger below). Per the locked design: the panel
  // always auto-retargets to whichever class currently has the live
  // episode, on whatever section (reasoning/dossier-open) is currently
  // shown -- so triggering a NEW class while a different class's panel is
  // open just reassigns this value, EpisodePanel re-renders for the new
  // faultClass/episodeId/live combination.
  const [openPanelClass, setOpenPanelClass] = useState(null);

  // Real UX fix, 2026-08-1x: "Diagnose & Fix" is now ONE button that
  // both stops an extended hold (crash-loop/cpu-throttling) AND kicks
  // off the real fix, instead of two separate clicks ("Stop & Fix" then
  // a second "Diagnose & Fix"). Stopping a hold doesn't move the
  // episode to awaiting_fix instantly -- the hold loop only notices the
  // stop-flag at its own ~10s tick -- so the actual /trigger/resolve
  // call has to wait for that real transition. autoResolveRequestedRef
  // records "the user already asked for this, fire resolve the moment
  // it's real"; autoResolveFiredRef guards against firing it twice if
  // more than one poll observes awaiting_fix before the request clears.
  const autoResolveRequestedRef = useRef(false);
  const autoResolveFiredRef = useRef(false);
  // Real bug found via live testing, fixed here rather than in FaultGrid:
  // the bead's OWN "pending" flag used to clear the instant `episode_state`
  // changed at all -- but the real holding->awaiting_fix transition (fired
  // by the hold loop noticing the stop request, NOT by the resolveFault()
  // call landing yet) happens BEFORE the chained resolveFault() call above
  // actually completes. That state change alone was enough to re-enable
  // the button for the real gap between "awaiting_fix first observed" and
  // "resolveFault() actually lands" -- a few real seconds, not a display
  // glitch. Fixed by tracking "the user already committed to fixing this
  // episode" as its own durable flag, independent of which raw state value
  // is currently polled -- cleared only on a genuinely NEW episode.
  const [fixRequested, setFixRequested] = useState(false);

  const [busyClass, setBusyClass] = useState(null); // promote/demote in flight
  const [lastOverride, setLastOverride] = useState(null); // { type: "promoted" | "demoted", faultClass }
  const [overrideBannerDismissed, setOverrideBannerDismissed] = useState(false);
  const [bannerDismissed, setBannerDismissed] = useState(false);

  const highlightClass = currentContext?.type === "promoteClass" ? currentContext.faultClass : null;

  // Real rehydration, added 2026-08-15: a page refresh mid-episode used
  // to lose ALL local knowledge of which class was running -- the grid
  // just showed every card as a bare disabled IDLE/TRIGGER with no
  // explanation, even though the backend knew exactly what was live.
  // /trigger/status now carries the real in-flight episode's id/class;
  // `prev ?? ...` never clobbers activeEpisode if it's already known
  // (e.g. this tab is the one that triggered it) -- only fills it in
  // when it's genuinely missing.
  const loadTriggerStatus = useCallback(() => {
    fetchTriggerStatus()
      .then((data) => {
        setTriggerStatus(data);
        if (data.episode_in_flight && data.in_flight_episode_id && data.in_flight_fault_class) {
          setActiveEpisode((prev) => prev ?? { episodeId: data.in_flight_episode_id, faultClass: data.in_flight_fault_class });
        }
      })
      .catch((e) => setErrorMsg(e.message));
  }, []);

  useEffect(() => {
    loadTriggerStatus();
    const id = setInterval(loadTriggerStatus, STATUS_POLL_MS);
    return () => clearInterval(id);
  }, [loadTriggerStatus]);

  const loadSystemStatus = useCallback(() => {
    if (!token) return;
    fetchSystemStatus(token).then(setSystemStatus).catch((e) => setErrorMsg(e.message));
  }, [token]);

  useEffect(() => {
    if (!token) return;
    loadSystemStatus();
    const id = setInterval(loadSystemStatus, STATUS_POLL_MS);
    return () => clearInterval(id);
  }, [token, loadSystemStatus]);

  const loadTrustMap = useCallback(() => {
    if (!token) return;
    fetchLiveTrust(token)
      .then((states) => setTrustMap(Object.fromEntries(states.map((s) => [s.fault_class, s.state]))))
      .catch(() => {});
  }, [token]);

  useEffect(() => { loadTrustMap(); }, [loadTrustMap]);

  // Real episode-state-machine poll, shared by FaultGrid + EpisodePanel.
  useEffect(() => {
    if (!activeEpisode || !token) return;
    doneRef.current = false;
    let cancelled = false;

    const poll = () => {
      fetchLiveStatus(activeEpisode.episodeId, token)
        .then((d) => {
          if (cancelled) return;
          setLive(d);
          const terminal =
            (d.episode_state === "resolved" && d.republished_at) || d.episode_state === "failed";
          if (terminal && !doneRef.current) {
            doneRef.current = true;
            loadTriggerStatus();
            loadTrustMap();
            setFixRequested(false); // defensive -- handleTrigger already resets this for the NEXT episode, this just keeps a just-finished episode's beads from lingering disabled
          }
          // Real chained "Diagnose & Fix" for a held episode -- the stop
          // request already went out (autoResolveRequestedRef set in
          // handleDiagnoseAndFix below); this poll is what actually
          // notices the moment the hold loop's own ~10s tick genuinely
          // moves the episode to awaiting_fix, and fires the real
          // /trigger/resolve call the instant that's true -- never
          // guessed off a fixed delay.
          if (
            autoResolveRequestedRef.current &&
            !autoResolveFiredRef.current &&
            d.episode_state === "awaiting_fix"
          ) {
            autoResolveFiredRef.current = true;
            autoResolveRequestedRef.current = false;
            resolveFault(activeEpisode.episodeId, token).catch((e) => {
              if (!cancelled && e.status !== 409) setErrorMsg(e.message);
            });
          }
        })
        .catch((e) => { if (!cancelled) setErrorMsg(e.message); });
    };
    poll();
    const id = setInterval(poll, LIVE_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loadTriggerStatus/loadTrustMap are stable useCallbacks, re-including them would just re-run this identically on every render
  }, [activeEpisode, token]);

  // Real toggle: clicking the card that's already the open panel's target
  // closes it instead of re-opening the same panel -- per explicit ask.
  // Triggering a fault always opens/retargets regardless (handleTrigger
  // below calls setOpenPanelClass directly, never through this toggle).
  const handleCardClick = (faultClass) => {
    setOpenPanelClass((current) => (current === faultClass ? null : faultClass));
  };

  const handleTrigger = async (faultClass) => {
    setErrorMsg(null);
    setLive(null);
    autoResolveRequestedRef.current = false;
    autoResolveFiredRef.current = false;
    setFixRequested(false);
    try {
      const result = await injectFault(faultClass, token);
      setActiveEpisode({ episodeId: result.episode_id, faultClass });
      setOpenPanelClass(faultClass); // real auto-retarget -- a newly-triggered fault always takes the panel
      loadTriggerStatus();
    } catch (e) {
      setErrorMsg(e.message);
    }
  };

  const handleStopHold = async () => {
    if (!activeEpisode) return;
    setErrorMsg(null);
    try {
      await stopHold(activeEpisode.episodeId, token);
    } catch (e) {
      if (e.status !== 409) setErrorMsg(e.message); // 409 = window already closed naturally, not a real error
    }
  };

  const handleResolve = async () => {
    if (!activeEpisode) return;
    setErrorMsg(null);
    try {
      await resolveFault(activeEpisode.episodeId, token);
    } catch (e) {
      setErrorMsg(e.message);
    }
  };

  // Real, single "Diagnose & Fix" action -- replaces the old separate
  // "Stop & Fix" (holding classes) + "Diagnose & Fix" (awaiting_fix
  // classes) two-button flow, per explicit UX correction: one button,
  // one click, whichever real transition the current state needs.
  const handleDiagnoseAndFix = async () => {
    if (!activeEpisode || !live) return;
    // Set BEFORE either branch, BEFORE any await -- this is the durable
    // "user already committed" flag that fixes the real re-enable race
    // (see its own declaration comment above): it must be true for the
    // entire window from this click through to the episode resolving,
    // regardless of how many intermediate real state values get polled
    // in between.
    setFixRequested(true);
    if (live.episode_state === "holding") {
      // Real chain, not synchronous: stopping the hold only sets a
      // flag the injector's own loop notices on its next ~10s tick --
      // the actual resolve fires later, from the live-status poll
      // above, the instant it genuinely observes awaiting_fix.
      autoResolveRequestedRef.current = true;
      await handleStopHold();
    } else if (live.episode_state === "awaiting_fix") {
      await handleResolve();
    }
  };

  const handlePromote = async (faultClass) => {
    setBusyClass(faultClass);
    setErrorMsg(null);
    try {
      await promoteClass(faultClass, token);
      loadTrustMap();
      setLastOverride({ type: "promoted", faultClass });
      setOverrideBannerDismissed(false);
    } catch (e) {
      setErrorMsg(e.message);
    } finally {
      setBusyClass(null);
    }
  };

  const handleDemote = async (faultClass) => {
    setBusyClass(faultClass);
    setErrorMsg(null);
    try {
      await demoteClass(faultClass, token);
      loadTrustMap();
      setLastOverride({ type: "demoted", faultClass });
      setOverrideBannerDismissed(false);
    } catch (e) {
      setErrorMsg(e.message);
    } finally {
      setBusyClass(null);
    }
  };

  const storefrontUrl = import.meta.env.VITE_STOREFRONT_URL || "http://localhost:8079";

  return (
    <div className="flex items-start gap-0">
    <div className="min-w-0 flex-1 max-w-[1600px] mx-auto">
      <div className="sticky top-0 z-40 bg-surface-container-lowest">
        <div className="flex items-center justify-between pt-2 pb-1">
          <span className="font-label-caps text-[10px] text-on-surface-variant">
            Every trigger's real effect (or lack of one, for the 3 invisible classes) can be verified live here.
          </span>
          <a
            href={storefrontUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="font-label-caps text-[10px] text-primary hover:underline flex items-center gap-1 shrink-0"
          >
            OPEN STOREFRONT <span className="material-symbols-outlined text-xs">open_in_new</span>
          </a>
        </div>
        {highlightClass && !bannerDismissed && (
          <div className="bg-primary/10 border border-primary/30 px-4 py-2 mb-4 flex items-center justify-between">
            <p className="font-body-sm text-sm text-primary flex items-center gap-2">
              <span className="material-symbols-outlined text-sm">info</span>
              Arrived from Replay Viewer: <span className="font-data-mono font-bold">{highlightClass}</span> was
              diagnosed correctly while report-only — review for promotion eligibility below.
            </p>
            <button onClick={() => setBannerDismissed(true)} className="text-on-surface-variant hover:text-on-surface">
              <span className="material-symbols-outlined text-sm">close</span>
            </button>
          </div>
        )}

        {lastOverride && !overrideBannerDismissed && (
          lastOverride.type === "promoted" ? (
            <div className="bg-primary/10 border border-primary/30 px-4 py-2 mb-4 flex items-center justify-between">
              <p className="font-body-sm text-sm text-primary flex items-center gap-2">
                <span className="material-symbols-outlined text-sm">check_circle</span>
                <span className="font-data-mono font-bold">{lastOverride.faultClass}</span> was just promoted to
                CAN-ACT.
              </p>
              <div className="flex items-center gap-3">
                <button
                  onClick={() => navigateTo("/", { type: "faultClass", faultClass: lastOverride.faultClass }, "Operator")}
                  className="font-label-caps text-[11px] text-primary hover:underline"
                >
                  VIEW TRUST LADDER →
                </button>
                <button onClick={() => setOverrideBannerDismissed(true)} className="text-on-surface-variant hover:text-on-surface">
                  <span className="material-symbols-outlined text-sm">close</span>
                </button>
              </div>
            </div>
          ) : (
            <div className="bg-error-container/10 border border-error-container/30 px-4 py-2 mb-4 flex items-center justify-between">
              <p className="font-body-sm text-sm text-error">
                <span className="font-data-mono font-bold">{lastOverride.faultClass}</span> was just demoted.
              </p>
              <div className="flex items-center gap-3">
                <button
                  onClick={() => navigateTo("/", { type: "faultClass", faultClass: lastOverride.faultClass }, "Operator")}
                  className="font-label-caps text-[11px] text-primary hover:underline"
                >
                  REVIEW DECISION →
                </button>
                <button onClick={() => setOverrideBannerDismissed(true)} className="text-on-surface-variant hover:text-on-surface">
                  <span className="material-symbols-outlined text-sm">close</span>
                </button>
              </div>
            </div>
          )
        )}
      </div>

      {errorMsg && <p className="text-error text-sm mb-4">{errorMsg}</p>}

      {!token && (
        <p className="text-on-surface-variant text-sm mb-4">
          Sign in (top right) to trigger faults or view live system status. Trigger budget below is visible to
          everyone, no login needed.
        </p>
      )}

      {/* Unified top status strip -- SystemStatusRibbon's 3 tiles + Trigger
          Budget as a 4th tile, sharing one grid (moved up from a separate
          sidebar column per explicit ask; topology gets the full content
          width below instead of splitting 8/4). */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {token && systemStatus && <SystemStatusRibbon status={systemStatus} />}
        <TriggerBudget status={triggerStatus} />
      </div>

      <div className="space-y-6">
        <FaultGrid
          token={token}
          role={role}
          trustMap={trustMap}
          episodeInFlight={Boolean(triggerStatus?.episode_in_flight)}
          crashLoopReady={triggerStatus?.crash_loop_ready}
          activeEpisode={activeEpisode}
          selectedFaultClass={openPanelClass}
          live={live}
          fixRequested={fixRequested}
          onTrigger={handleTrigger}
          onDiagnoseAndFix={handleDiagnoseAndFix}
          onOpenPanel={handleCardClick}
        />

        {role === "admin" && (
          <AdminOverrides
            autoFixClasses={AUTO_FIX_CLASSES}
            trustMap={trustMap}
            highlightClass={highlightClass}
            busyClass={busyClass}
            onPromote={handlePromote}
            onDemote={handleDemote}
          />
        )}
      </div>
    </div>

      {openPanelClass && (
        <EpisodePanel
          faultClass={openPanelClass}
          episodeId={activeEpisode?.faultClass === openPanelClass ? activeEpisode.episodeId : null}
          live={activeEpisode?.faultClass === openPanelClass ? live : null}
          token={token}
          onClose={() => setOpenPanelClass(null)}
          onViewReplay={(episodeId) => navigateTo(`/replay/${episodeId}`, null, "Operator")}
        />
      )}
    </div>
  );
}
