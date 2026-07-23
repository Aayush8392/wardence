import { useEffect, useState, useCallback } from "react";
import { useAuth } from "../../context/AuthContext";
import { useNavHistory } from "../../context/NavHistoryContext";
import {
  fetchTriggerStatus,
  fetchSystemStatus,
  fetchLiveTrust,
  injectFault,
  resolveFault,
  promoteClass,
  demoteClass,
} from "../../api/operator";
import SystemStatusRibbon from "../../components/operator/SystemStatusRibbon";
import TriggerControlCenter from "../../components/operator/TriggerControlCenter";
import AdminOverrides from "../../components/operator/AdminOverrides";
import TriggerBudget from "../../components/operator/TriggerBudget";
import ActiveSession from "../../components/operator/ActiveSession";

// Real, locked constants -- not fabricated. Matches operator_api.py's
// IMPLEMENTED_CLASSES/SAFE_DEMO_CLASSES (2026-07-24, Phase B: all 3 locked
// auto-fix classes are now live-triggerable and demo-safe, giving demo
// users a real feel for the project's depth rather than just crash-loop).
const TRIGGERABLE_CLASSES = ["crash-loop", "oom", "disk-full"];
const SAFE_DEMO_CLASSES = ["crash-loop", "oom", "disk-full"];
const AUTO_FIX_CLASSES = ["crash-loop", "oom", "disk-full"];

export default function Operator() {
  const { token, role } = useAuth();
  const { currentContext, navigateTo } = useNavHistory();

  const [triggerStatus, setTriggerStatus] = useState(null);
  const [systemStatus, setSystemStatus] = useState(null);
  const [trustMap, setTrustMap] = useState({});

  // Two-phase trigger flow (2026-07-24, see wardence_frontend.md): inject
  // and resolve are now two separate, user-paced actions instead of one
  // blocking call. injectedEpisode holds the real episode landed by
  // inject, awaiting the user's deliberate Diagnose & Fix click.
  const [injectingClass, setInjectingClass] = useState(null);
  const [injectStartedAt, setInjectStartedAt] = useState(null);
  const [injectedEpisode, setInjectedEpisode] = useState(null); // { faultClass, episodeId, t0 }
  const [resolvingClass, setResolvingClass] = useState(null);
  const [resolveStartedAt, setResolveStartedAt] = useState(null);
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

  const handleInject = async (faultClass) => {
    setErrorMsg(null);
    setLastResult(null);
    setInjectedEpisode(null);
    setInjectingClass(faultClass);
    setInjectStartedAt(Date.now());
    try {
      const result = await injectFault(faultClass, token);
      // Real fault has landed -- hand off to the "awaiting fix" state.
      // Nothing auto-advances from here; the user must confirm on the
      // live frontend and click DIAGNOSE & FIX deliberately.
      setInjectedEpisode({ faultClass, episodeId: result.episode_id, t0: result.t0 });
      loadTriggerStatus();
    } catch (e) {
      setErrorMsg(e.message);
    } finally {
      setInjectingClass(null);
      setInjectStartedAt(null);
    }
  };

  const handleResolve = async (faultClass) => {
    if (!injectedEpisode || injectedEpisode.faultClass !== faultClass) return;
    setErrorMsg(null);
    setResolvingClass(faultClass);
    setResolveStartedAt(Date.now());
    const startedAt = Date.now();
    try {
      // Real pipeline: the backend silently pads out any remaining settle
      // time before diagnosing -- see operator_api.py's /trigger/resolve
      // and wardence_frontend.md's "Two-Phase Trigger Flow" section. Can
      // take a few minutes total, not seconds -- deliberately shown as one
      // continuous busy state, never split into "waiting" vs "diagnosing".
      const result = await resolveFault(injectedEpisode.episodeId, token);
      setLastResult({ ...result, totalElapsedMs: Date.now() - startedAt });
      setInjectedEpisode(null);
      loadTriggerStatus();
    } catch (e) {
      setErrorMsg(e.message);
      // 410 = operator_api.py's RESOLVE_WINDOW_MAX_S hard block -- this
      // specific episode will NEVER be scorable now (elapsed only grows),
      // so retrying "Diagnose & Fix" on it is pointless. Clear it so the
      // card reverts to "Trigger Injection" instead of leaving the user
      // stuck retrying a dead episode forever. Deliberately NOT done for
      // other errors (e.g. a transient agent hiccup) -- those episodes
      // may still be genuinely resolvable, so the user should be able to
      // retry the SAME episode rather than lose it.
      if (e.status === 410) {
        setInjectedEpisode(null);
      }
    } finally {
      setResolvingClass(null);
      setResolveStartedAt(null);
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

  // Real bug found during Phase B testing (2026-07-24): triggerStatus is
  // only fetched from the server on mount + after this session's own
  // actions complete -- so "an episode is in flight" only appeared on
  // cards/widgets AFTER injector.py's subprocess finished and a fresh
  // fetch landed, not the instant the user clicked "Trigger Injection".
  // Fixed by overlaying what THIS session already knows client-side
  // (injecting/awaiting-fix/resolving) onto the server's own flag --
  // true the moment either side thinks something is happening, cleared
  // the instant both agree nothing is. Doesn't touch the server's other
  // fields (cooldown/cap numbers), only episode_in_flight.
  const clientKnownBusy = Boolean(injectingClass || injectedEpisode || resolvingClass);
  const effectiveTriggerStatus = triggerStatus
    ? { ...triggerStatus, episode_in_flight: triggerStatus.episode_in_flight || clientKnownBusy }
    : triggerStatus;

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
            triggerStatus={effectiveTriggerStatus}
            triggeringClass={injectingClass}
            awaitingFixClass={injectedEpisode?.faultClass ?? null}
            resolvingClass={resolvingClass}
            onTrigger={handleInject}
            onResolve={handleResolve}
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
          <TriggerBudget status={effectiveTriggerStatus} />
          <ActiveSession
            injectingClass={injectingClass}
            injectStartedAt={injectStartedAt}
            injectedEpisode={injectedEpisode}
            resolvingClass={resolvingClass}
            resolveStartedAt={resolveStartedAt}
            lastResult={lastResult}
            onViewReplay={(episodeId) => navigateTo(`/replay/${episodeId}`, null, "Operator")}
          />
        </div>
      </div>
    </div>
  );
}
