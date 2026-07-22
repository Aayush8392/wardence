import { useEffect, useState, useCallback } from "react";
import { useAuth } from "../../context/AuthContext";
import { useNavHistory } from "../../context/NavHistoryContext";
import { fetchTriggerStatus, fetchSystemStatus, triggerFault, promoteClass, demoteClass } from "../../api/operator";
import LoginModal from "../../components/auth/LoginModal";

// Real, locked constants -- not fabricated. injector.py only implements
// crash-loop so far (operator_api.py's IMPLEMENTED_CLASSES/SAFE_DEMO_CLASSES
// both = {"crash-loop"} today); the 3 auto-fix classes are the locked
// taxonomy (wardence_context.md) that have a real promotion/demotion path.
const TRIGGERABLE_CLASSES = ["crash-loop"];
const AUTO_FIX_CLASSES = ["crash-loop", "oom", "disk-full"];

export default function Operator() {
  const { token, role, user, logout } = useAuth();
  const { currentContext, navigateTo } = useNavHistory();
  const [showLogin, setShowLogin] = useState(false);
  const [triggerStatus, setTriggerStatus] = useState(null);
  const [systemStatus, setSystemStatus] = useState(null);
  const [actionMsg, setActionMsg] = useState(null);
  const [triggering, setTriggering] = useState(null); // fault_class currently in flight, or null
  const [lastEpisodeId, setLastEpisodeId] = useState(null); // for "View full replay" (pair #5)
  const [demotedClass, setDemotedClass] = useState(null); // for "Review Decision" (pair #3)

  // Consume a cross-tab jump from Replay Viewer's "Promote Class" link (pair #4)
  const highlightClass = currentContext?.type === "promoteClass" ? currentContext.faultClass : null;

  const loadTriggerStatus = useCallback(() => {
    fetchTriggerStatus().then(setTriggerStatus).catch((e) => setActionMsg(e.message));
  }, []);

  useEffect(() => {
    loadTriggerStatus();
  }, [loadTriggerStatus]);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    fetchSystemStatus(token)
      .then((d) => { if (!cancelled) setSystemStatus(d); })
      .catch((e) => { if (!cancelled) setActionMsg(e.message); });
    return () => { cancelled = true; };
  }, [token]);

  const handleTrigger = async (faultClass) => {
    setActionMsg(null);
    setTriggering(faultClass);
    try {
      // Real pipeline against the live cluster: inject -> 35s settle wait
      // (kube-state-metrics scrape cycle) -> diagnose+act+score. Can take
      // a few minutes total, not seconds -- the pending state is real.
      const result = await triggerFault(faultClass, token);
      if (result.status === "scored") {
        setActionMsg(
          `Episode ${result.episode_id}: predicted "${result.predicted_class}" — ` +
          `${result.correct ? "correct" : "incorrect"}` +
          (result.action_taken ? `, action: ${result.action_taken} (${result.durability_verdict ?? "pending"})` : "")
        );
        setLastEpisodeId(result.episode_id); // pair #5
      } else if (result.status === "triggered_but_unscored") {
        setActionMsg(`Episode ${result.episode_id} triggered, but scoring failed: ${result.scorer_error}`);
      } else {
        setActionMsg(`Triggered ${faultClass}: episode ${result.episode_id ?? "(see response)"}`);
      }
      loadTriggerStatus();
    } catch (e) {
      setActionMsg(e.message);
    } finally {
      setTriggering(null);
    }
  };

  const handlePromote = async (faultClass) => {
    try {
      await promoteClass(faultClass, token);
      setActionMsg(`${faultClass} promoted to can_act.`);
    } catch (e) {
      setActionMsg(e.message);
    }
  };

  const handleDemote = async (faultClass) => {
    try {
      await demoteClass(faultClass, token);
      setActionMsg(`${faultClass} demoted.`);
      setDemotedClass(faultClass); // pair #3
    } catch (e) {
      setActionMsg(e.message);
    }
  };

  return (
    <div>
      <h1>Operator</h1>

      {/* Public — visible to everyone regardless of login state. */}
      {triggerStatus && (
        <p style={{ fontSize: 13, opacity: 0.8 }}>
          {triggerStatus.global_remaining_today} of {triggerStatus.global_cap} daily triggers left
          {triggerStatus.your_cooldown_remaining_s > 0 &&
            ` — your cooldown: ${Math.round(triggerStatus.your_cooldown_remaining_s)}s`}
          {triggerStatus.episode_in_flight && " — an episode is currently in flight"}
        </p>
      )}

      {!token ? (
        <div>
          <p>Log in to trigger faults or view live system status.</p>
          {!showLogin ? (
            <button onClick={() => setShowLogin(true)}>Log in</button>
          ) : (
            <LoginModal onClose={() => setShowLogin(false)} />
          )}
        </div>
      ) : (
        <div>
          <p>Logged in as <strong>{user}</strong> ({role}) — <button onClick={logout}>Log out</button></p>

          {systemStatus && (
            <p style={{ fontSize: 13, opacity: 0.8 }}>
              Request rate: {systemStatus.request_rate_per_s}/s · Error rate: {(systemStatus.error_rate * 100).toFixed(1)}% ·
              {" "}Pods: {Object.entries(systemStatus.pods_by_phase).map(([p, n]) => `${p}:${n}`).join(", ")}
            </p>
          )}

          <h3>Trigger a fault</h3>
          {TRIGGERABLE_CLASSES.map((fc) => (
            <button
              key={fc}
              onClick={() => handleTrigger(fc)}
              disabled={triggering !== null}
              style={{ marginRight: 8 }}
            >
              {triggering === fc ? "Running… (inject + settle + diagnose, can take a few minutes)" : `Trigger ${fc}`}
            </button>
          ))}

          {role === "admin" && (
            <>
              <h3>Trust overrides (admin only)</h3>
              {highlightClass && (
                <p style={{ fontSize: 12, opacity: 0.8 }}>
                  Arrived from Replay Viewer to review <strong>{highlightClass}</strong>.
                </p>
              )}
              {AUTO_FIX_CLASSES.map((fc) => (
                <span
                  key={fc}
                  style={{
                    marginRight: 12,
                    outline: fc === highlightClass ? "2px solid #4caf50" : "none",
                    padding: 2,
                  }}
                >
                  {fc}: <button onClick={() => handlePromote(fc)}>Promote</button>{" "}
                  <button onClick={() => handleDemote(fc)}>Demote</button>
                </span>
              ))}
            </>
          )}

          {actionMsg && <p style={{ marginTop: 12 }}>{actionMsg}</p>}

          {lastEpisodeId && (
            <p>
              <button onClick={() => navigateTo(`/replay/${lastEpisodeId}`, null, "Operator")}>
                View full replay →
              </button>
            </p>
          )}

          {demotedClass && (
            <p>
              <strong>{demotedClass}</strong> was just demoted —{" "}
              <button onClick={() => navigateTo("/", { type: "faultClass", faultClass: demotedClass }, "Operator")}>
                Review Decision →
              </button>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
