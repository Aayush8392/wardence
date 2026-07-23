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
  // Single "last override" slot, not two independent promoted/demoted
  // variables -- two separate state values meant nothing ever cleared the
  // OTHER one, so promoting then demoting (or vice versa) left both
  // banners stacked visible at once. Collapsing to one slot makes only-one-
  // at-a-time true by construction, not something to remember to enforce.
  const [lastOverride, setLastOverride] = useState(null); // { type: "promoted" | "demoted", faultClass }
  const [overrideBannerDismissed, setOverrideBannerDismissed] = useState(false);
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
    const startedAt = Date.now();
    try {
      // Real pipeline: inject -> 35s settle -> diagnose+act+score. Can take
      // a few minutes total, not seconds.
      const result = await triggerFault(faultClass, token);
      // Real, client-measured total elapsed -- captured here (before the
      // finally block clears triggerStartedAt) since ActiveSession's own
      // completed-view needs a number to display, not just the in-progress
      // timeline's live ticker.
      setLastResult({ ...result, totalElapsedMs: Date.now() - startedAt });
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
      setLastOverride({ type: "promoted", faultClass });
      setOverrideBannerDismissed(false); // re-arm in case a prior banner was dismissed
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
      setLastOverride({ type: "demoted", faultClass }); // pair #3
      setOverrideBannerDismissed(false); // re-arm in case a prior banner was dismissed
    } catch (e) {
      setErrorMsg(e.message);
    } finally {
      setBusyClass(null);
    }
  };

  return (
    <div className="max-w-[1600px] mx-auto">
      {/* Sticky, not static -- these banners are the visual confirmation
          for an action the user just took (promote/demote/arrival), but
          they render at the top of the PAGE CONTENT, not the viewport. If
          you'd already scrolled down (e.g. to click a button further down
          the page), a static banner is off-screen and easy to miss
          entirely. Sticking to the viewport top once scrolled past its
          natural position keeps it visible regardless of scroll position.
          Solid bg (not the banners' own translucent backgrounds) so
          content scrolling underneath doesn't show through once pinned. */}
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
              <button
                onClick={() => setOverrideBannerDismissed(true)}
                className="text-on-surface-variant hover:text-on-surface"
              >
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
              <button
                onClick={() => setOverrideBannerDismissed(true)}
                className="text-on-surface-variant hover:text-on-surface"
              >
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
