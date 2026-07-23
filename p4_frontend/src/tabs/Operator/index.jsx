import { useEffect, useState, useCallback } from "react";
import { useAuth } from "../../context/AuthContext";
import { useNavHistory } from "../../context/NavHistoryContext";
import {
  fetchTriggerStatus,
  fetchSystemStatus,
  fetchLiveTrust,
  triggerFault,
  promoteClass,
  demoteClass,
} from "../../api/operator";
import SystemStatusRibbon from "../../components/operator/SystemStatusRibbon";
import TriggerControlCenter from "../../components/operator/TriggerControlCenter";
import AdminOverrides from "../../components/operator/AdminOverrides";
import TriggerBudget from "../../components/operator/TriggerBudget";
import ActiveSession from "../../components/operator/ActiveSession";

// Real, locked constants -- not fabricated. injector.py only implements
// crash-loop's live /trigger path so far (operator_api.py's
// IMPLEMENTED_CLASSES/SAFE_DEMO_CLASSES both = {"crash-loop"} today); the
// 3 auto-fix classes are the locked taxonomy (wardence_context.md) that
// have a real promotion/demotion path.
const TRIGGERABLE_CLASSES = ["crash-loop"];
const SAFE_DEMO_CLASSES = ["crash-loop"];
const AUTO_FIX_CLASSES = ["crash-loop", "oom", "disk-full"];

export default function Operator() {
  const { token, role } = useAuth();
  const { currentContext, navigateTo } = useNavHistory();

  const [triggerStatus, setTriggerStatus] = useState(null);
  const [systemStatus, setSystemStatus] = useState(null);
  const [trustMap, setTrustMap] = useState({});

  const [triggeringClass, setTriggeringClass] = useState(null);
  const [triggerStartedAt, setTriggerStartedAt] = useState(null);
  const [lastResult, setLastResult] = useState(null);
  const [errorMsg, setErrorMsg] = useState(null);

  const [busyClass, setBusyClass] = useState(null); // promote/demote in flight
  const [demotedClass, setDemotedClass] = useState(null); // for "Review Decision" (pair #3)
  const [bannerDismissed, setBannerDismissed] = useState(false);

  // Consume a cross-tab jump from Replay Viewer's "Promote Class" link (pair #4)
  const highlightClass = currentContext?.type === "promoteClass" ? currentContext.faultClass : null;

  const loadTriggerStatus = useCallback(() => {
    fetchTriggerStatus().then(setTriggerStatus).catch((e) => setErrorMsg(e.message));
  }, []);

  useEffect(() => { loadTriggerStatus(); }, [loadTriggerStatus]);

  useEffect(() => {
    // Only fetch when there's a token -- if it's cleared (logout), the
    // stale value just doesn't get re-fetched. Rendering is separately
    // gated on `token` below, so a stale value never actually displays.
    if (!token) return;
    let cancelled = false;
    fetchSystemStatus(token)
      .then((d) => { if (!cancelled) setSystemStatus(d); })
      .catch((e) => { if (!cancelled) setErrorMsg(e.message); });
    return () => { cancelled = true; };
  }, [token]);

  const loadTrustMap = useCallback(() => {
    if (!token) return;
    fetchLiveTrust(token)
      .then((states) => setTrustMap(Object.fromEntries(states.map((s) => [s.fault_class, s.state]))))
      .catch(() => {}); // non-critical -- overrides panel just shows "unknown"
  }, [token]);

  useEffect(() => { loadTrustMap(); }, [loadTrustMap]);

  const handleTrigger = async (faultClass) => {
    setErrorMsg(null);
    setLastResult(null);
    setTriggeringClass(faultClass);
    setTriggerStartedAt(Date.now());
    try {
      // Real pipeline: inject -> 35s settle -> diagnose+act+score. Can take
      // a few minutes total, not seconds.
      const result = await triggerFault(faultClass, token);
      setLastResult(result);
      loadTriggerStatus();
    } catch (e) {
      setErrorMsg(e.message);
    } finally {
      setTriggeringClass(null);
      setTriggerStartedAt(null);
    }
  };

  const handlePromote = async (faultClass) => {
    setBusyClass(faultClass);
    setErrorMsg(null);
    try {
      await promoteClass(faultClass, token);
      loadTrustMap();
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
      setDemotedClass(faultClass); // pair #3
    } catch (e) {
      setErrorMsg(e.message);
    } finally {
      setBusyClass(null);
    }
  };

  return (
    <div className="max-w-[1600px] mx-auto">
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

      {demotedClass && (
        <div className="bg-error-container/10 border border-error-container/30 px-4 py-2 mb-4 flex items-center justify-between">
          <p className="font-body-sm text-sm text-error">
            <span className="font-data-mono font-bold">{demotedClass}</span> was just demoted.
          </p>
          <button
            onClick={() => navigateTo("/", { type: "faultClass", faultClass: demotedClass }, "Operator")}
            className="font-label-caps text-[11px] text-primary hover:underline"
          >
            REVIEW DECISION →
          </button>
        </div>
      )}

      {errorMsg && <p className="text-error text-sm mb-4">{errorMsg}</p>}

      {!token && (
        <p className="text-on-surface-variant text-sm mb-4">
          Sign in (top right) to trigger faults or view live system status. Trigger budget below is visible to
          everyone, no login needed.
        </p>
      )}

      {token && systemStatus && (
        <div className="mb-6">
          <SystemStatusRibbon status={systemStatus} />
        </div>
      )}

      <div className="grid grid-cols-12 gap-6">
        <div className="col-span-12 lg:col-span-8 space-y-6">
          <TriggerControlCenter
            triggerableClasses={TRIGGERABLE_CLASSES}
            token={token}
            role={role}
            safeDemoClasses={SAFE_DEMO_CLASSES}
            triggerStatus={triggerStatus}
            triggeringClass={triggeringClass}
            onTrigger={handleTrigger}
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

        <div className="col-span-12 lg:col-span-4 space-y-6">
          <TriggerBudget status={triggerStatus} />
          <ActiveSession
            triggeringClass={triggeringClass}
            triggerStartedAt={triggerStartedAt}
            lastResult={lastResult}
            onViewReplay={(episodeId) => navigateTo(`/replay/${episodeId}`, null, "Operator")}
          />
        </div>
      </div>
    </div>
  );
}
