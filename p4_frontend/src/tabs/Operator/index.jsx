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
import TopologyMap from "../../components/operator/TopologyMap";
import LiveStatusDetail from "../../components/operator/LiveStatusDetail";
import AdminOverrides from "../../components/operator/AdminOverrides";
import TriggerBudget from "../../components/operator/TriggerBudget";
import ActiveSession from "../../components/operator/ActiveSession";

const STATUS_POLL_MS = 15_000;
const LIVE_POLL_MS = 4_000;

export default function Operator() {
  const { token, role } = useAuth();
  const { currentContext, navigateTo } = useNavHistory();

  const [triggerStatus, setTriggerStatus] = useState(null);
  const [systemStatus, setSystemStatus] = useState(null);
  const [trustMap, setTrustMap] = useState({});

  // Real async state-machine episode (Phase 1/2, 2026-08-1x/13). Polled
  // ONCE here and shared with both TopologyMap (node-level action
  // buttons) and ActiveSession (progress panel) -- neither makes its own
  // duplicate /trigger/live-status calls.
  const [activeEpisode, setActiveEpisode] = useState(null); // { episodeId, faultClass }
  const [live, setLive] = useState(null);
  const [errorMsg, setErrorMsg] = useState(null);
  const doneRef = useRef(false);

  const [busyClass, setBusyClass] = useState(null); // promote/demote in flight
  const [lastOverride, setLastOverride] = useState(null); // { type: "promoted" | "demoted", faultClass }
  const [overrideBannerDismissed, setOverrideBannerDismissed] = useState(false);
  const [bannerDismissed, setBannerDismissed] = useState(false);

  const highlightClass = currentContext?.type === "promoteClass" ? currentContext.faultClass : null;

  const loadTriggerStatus = useCallback(() => {
    fetchTriggerStatus().then(setTriggerStatus).catch((e) => setErrorMsg(e.message));
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

  // Real episode-state-machine poll, shared by TopologyMap + ActiveSession.
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
          }
        })
        .catch((e) => { if (!cancelled) setErrorMsg(e.message); });
    };
    poll();
    const id = setInterval(poll, LIVE_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loadTriggerStatus/loadTrustMap are stable useCallbacks, re-including them would just re-run this identically on every render
  }, [activeEpisode, token]);

  const handleTrigger = async (faultClass) => {
    setErrorMsg(null);
    setLive(null);
    try {
      const result = await injectFault(faultClass, token);
      setActiveEpisode({ episodeId: result.episode_id, faultClass });
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

  return (
    <div className="max-w-[1600px] mx-auto">
      <div className="sticky top-0 z-40 bg-surface-container-lowest">
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

      {activeEpisode && (
        <div className="mb-6">
          <ActiveSession
            episodeId={activeEpisode.episodeId}
            faultClass={activeEpisode.faultClass}
            live={live}
            onViewReplay={(episodeId) => navigateTo(`/replay/${episodeId}`, null, "Operator")}
          />
        </div>
      )}

      <div className="space-y-6">
        <TopologyMap
          token={token}
          role={role}
          trustMap={trustMap}
          episodeInFlight={Boolean(triggerStatus?.episode_in_flight)}
          activeEpisode={activeEpisode}
          live={live}
          onTrigger={handleTrigger}
          onResolve={handleResolve}
          onStopHold={handleStopHold}
        />

        {token && <LiveStatusDetail token={token} />}

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
  );
}
